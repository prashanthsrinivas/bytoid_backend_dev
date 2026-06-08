"""App-side collection spine: launch the Lambda + process its signed callback.

``trigger_collection`` fires the OSINT collector Lambda (fire-and-forget) once a
vendor is ready, guarded by an in-flight lock + change-detection dedup.
``process_callback`` verifies the HMAC-signed snapshot, persists it to S3, and
advances the assessment state. Both are async (the Redis helpers are async);
routes wrap them with the repo's ``asyncio.run`` pattern.
"""

from __future__ import annotations

import json
import uuid

from utils.base_logger import get_logger
from vra import config as vra_config
from vra.helpers import (
    acquire_inflight,
    consume_nonce,
    domain_fingerprint,
    inputs_unchanged,
    record_fingerprint,
    release_inflight,
)
from vra.osint.normalize import validate_finding
from vra.osint.signing import (
    NONCE_HEADER,
    SIG_HEADER,
    TS_HEADER,
    signature_valid,
    timestamp_within_skew,
)
from vra.schema import SCAN_COMPLETE, SCAN_IN_FLIGHT, SCAN_PENDING
from vra.service import VraService

logger = get_logger(__name__)

_CALLBACK_PATH = "/vra/osint/callback"


def _callback_url() -> str:
    base = vra_config.VRA_CALLBACK_BASE_URL
    if not base:
        try:
            from utils.app_configs import BACKURL

            base = BACKURL
        except Exception:
            base = ""
    return f"{base.rstrip('/')}{_CALLBACK_PATH}" if base else ""


def _lambda_client():
    import boto3

    return boto3.client("lambda", region_name=vra_config.AWS_REGION)


async def trigger_collection(
    user_id: str,
    assessment_id: str,
    *,
    service: VraService | None = None,
    lambda_client=None,
    force: bool = False,
) -> dict:
    """Launch an OSINT scan for an assessment. Idempotent + cost-guarded.

    Returns a status dict; never raises for control-flow conditions
    (disabled/not-ready/unchanged/already-running).
    """
    service = service or VraService()
    record = service.get_assessment(user_id, assessment_id)
    if not record:
        return {"status": "error", "message": "assessment not found"}
    if not service.ready_for_collection(record):
        return {"status": "skipped", "reason": "vendor name/domain not complete"}
    if not vra_config.collection_enabled():
        return {"status": "disabled", "reason": "collector Lambda/HMAC not configured"}

    callback_url = _callback_url()
    if not callback_url:
        return {"status": "error", "message": "no callback URL configured"}

    fingerprint = domain_fingerprint(record["vendor_name"], record["vendor_domain"])
    if not force and await inputs_unchanged(assessment_id, fingerprint):
        return {"status": "unchanged", "reason": "inputs match last scan"}

    if not await acquire_inflight(assessment_id):
        return {"status": "already_running"}

    scan_id = uuid.uuid4().hex
    record["scan_state"] = SCAN_IN_FLIGHT
    service.storage.save_assessment(user_id, record)

    payload = {
        "scan_id": scan_id,
        "assessment_id": assessment_id,
        "user_id": user_id,
        "vendor_name": record["vendor_name"],
        "vendor_domain": record["vendor_domain"],
        "callback_url": callback_url,
        "hmac_secret": vra_config.VRA_HMAC_SECRET,
    }

    try:
        client = lambda_client or _lambda_client()
        client.invoke(
            FunctionName=vra_config.VRA_LAMBDA_ARN,
            InvocationType="Event",  # async fire-and-forget
            Payload=json.dumps(payload).encode("utf-8"),
        )
    except Exception as exc:
        logger.warning("VRA Lambda invoke failed: %s", exc, exc_info=True)
        record["scan_state"] = SCAN_PENDING
        service.storage.save_assessment(user_id, record)
        await release_inflight(assessment_id)
        return {"status": "error", "message": f"invoke failed: {exc}"}

    logger.info("Launched VRA scan %s for assessment %s", scan_id, assessment_id)
    return {"status": "launched", "scan_id": scan_id}


async def process_callback(
    raw_body: bytes,
    headers: dict,
    *,
    service: VraService | None = None,
) -> tuple[int, dict]:
    """Verify + persist a signed snapshot. Returns ``(http_status, body)``."""
    secret = vra_config.VRA_HMAC_SECRET
    if not secret:
        return 503, {"status": "error", "message": "callback not configured"}

    if len(raw_body) > vra_config.VRA_CALLBACK_MAX_BYTES:
        return 413, {"status": "error", "message": "payload too large"}

    # Case-insensitive header lookup: WSGI/Werkzeug normalizes header names
    # (e.g. X-VRA-Timestamp -> X-Vra-Timestamp), so an exact-case .get() on a
    # plain dict would miss and wrongly 401. Normalize to lowercase keys.
    hdr = {str(k).lower(): v for k, v in (headers or {}).items()}
    ts = hdr.get(TS_HEADER.lower(), "")
    nonce = hdr.get(NONCE_HEADER.lower(), "")
    sig = hdr.get(SIG_HEADER.lower(), "")

    if not timestamp_within_skew(ts, vra_config.VRA_CALLBACK_MAX_SKEW):
        return 401, {"status": "error", "message": "timestamp out of range"}
    if not signature_valid(secret, ts, nonce, raw_body, sig):
        return 401, {"status": "error", "message": "bad signature"}
    if not await consume_nonce(nonce, vra_config.VRA_CALLBACK_MAX_SKEW):
        return 409, {"status": "error", "message": "replayed nonce"}

    try:
        snapshot = json.loads(raw_body)
    except (ValueError, TypeError):
        return 400, {"status": "error", "message": "invalid json"}

    user_id = snapshot.get("user_id")
    assessment_id = snapshot.get("assessment_id")
    scan_id = snapshot.get("scan_id")
    if not (user_id and assessment_id and scan_id):
        return 400, {"status": "error", "message": "missing identifiers"}

    service = service or VraService()
    record = service.get_assessment(user_id, assessment_id)
    # Consistency guard: the (user_id, assessment_id) pair must exist — prevents
    # a signed payload from writing into an unrelated user's prefix.
    if not record:
        await release_inflight(assessment_id)
        return 404, {"status": "error", "message": "unknown assessment"}

    # Defense in depth: drop malformed findings before persisting.
    snapshot["findings"] = [f for f in (snapshot.get("findings") or []) if validate_finding(f)]

    service.storage.save_snapshot(user_id, snapshot)

    from vra.scheduler import compute_next_scan_at

    record["scan_state"] = SCAN_COMPLETE
    record["latest_scan_id"] = scan_id
    record["last_scan_at"] = snapshot.get("scanned_at")
    record["latest_risk_score"] = snapshot.get("risk_score", 0.0)
    record["next_scan_at"] = compute_next_scan_at()
    service.storage.save_assessment(user_id, record)

    fingerprint = domain_fingerprint(record["vendor_name"], record["vendor_domain"])
    await record_fingerprint(assessment_id, fingerprint)
    await release_inflight(assessment_id)

    # Weave the VRA questions into the linked questionnaire (best-effort): the two
    # locked vendor questions + freshly-derived OSINT follow-ups, as their own
    # "Vendor Intelligence" section. replace_osint refreshes prior derived
    # questions on a re-scan rather than piling up. Never breaks the callback.
    playbook_id = record.get("playbook_id")
    if playbook_id:
        try:
            from vra.workflow_inject import (
                derive_osint_questions,
                inject_into_workflow,
                vendor_question_items,
            )

            inject_into_workflow(
                user_id,
                playbook_id,
                vendor_items=vendor_question_items(),
                osint_items=derive_osint_questions(snapshot),
                replace_osint=True,
            )
        except Exception:
            logger.warning("VRA question injection failed for %s", assessment_id, exc_info=True)

    logger.info("Stored VRA snapshot %s (%d findings)", scan_id, len(snapshot["findings"]))
    return 200, {"status": "success", "scan_id": scan_id, "findings": len(snapshot["findings"])}
