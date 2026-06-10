"""App-side collection spine: launch the collector Lambda + process its callback.

``trigger_collection`` fires the cross-account SG-audit collector Lambda
(fire-and-forget) once an audit is ready, guarded by a base-session TTL check, an
in-flight lock, and scope change-detection dedup. ``process_callback`` verifies
the HMAC-signed snapshot, persists it to S3, and advances the audit state.

SECURITY: the Lambda invoke payload carries the caller's short-lived base STS
credentials + the HMAC secret + the per-tenant ExternalId. None of these are ever
logged. The base credentials let the Lambda assume the read-only audit role in
each member account; they are not the Lambda's own identity.
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from datetime import datetime, timezone

import pymysql

from db.rds_db import connect_to_rds
from utils.base_logger import get_logger
from sg_audit import config as sg_config
from sg_audit.analysis.normalize import validate_finding
from sg_audit.helpers import (
    acquire_inflight,
    consume_nonce,
    inputs_unchanged,
    record_fingerprint,
    release_inflight,
    scope_fingerprint,
)
from sg_audit.schema import SCAN_COMPLETE, SCAN_FAILED, SCAN_IN_FLIGHT, SCAN_PENDING
from sg_audit.service import SgAuditService
from sg_audit.signing import (
    NONCE_HEADER,
    SIG_HEADER,
    TS_HEADER,
    signature_valid,
    timestamp_within_skew,
)

logger = get_logger(__name__)

_CALLBACK_PATH = "/sg-audit/callback"


def _callback_url() -> str:
    base = sg_config.SG_CALLBACK_BASE_URL
    if not base:
        try:
            from utils.app_configs import BACKURL

            base = BACKURL
        except Exception:
            base = ""
    return f"{base.rstrip('/')}{_CALLBACK_PATH}" if base else ""


def _lambda_client():
    import boto3

    return boto3.client("lambda", region_name=sg_config.AWS_REGION)


def _base_session(user_id: str, min_ttl_seconds: int):
    """Resolve the caller's base AWS session, enforcing a minimum remaining TTL.

    Returns ``(status, row)`` where status is "ok" | "no_session" | "expiring".
    The TTL check runs in SQL (``NOW() + INTERVAL``) so it is immune to app/DB
    clock-tz drift. We never trust an STS session that could expire mid-scan.
    """
    conn = None
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                """
                SELECT aws_access_key_id, aws_secret_access_key, aws_session_token,
                       aws_region, aws_account_id, aws_role_arn, expires_at
                FROM aws_saml_sessions
                WHERE user_id=%s AND expires_at > NOW()
                LIMIT 1
                """,
                (user_id,),
            )
            row = cur.fetchone()
            if not row:
                return "no_session", None
            cur.execute(
                """
                SELECT 1 FROM aws_saml_sessions
                WHERE user_id=%s AND expires_at > (NOW() + INTERVAL %s SECOND)
                LIMIT 1
                """,
                (user_id, int(min_ttl_seconds)),
            )
            if not cur.fetchone():
                return "expiring", None
            return "ok", row
    except Exception:
        logger.warning("SG-audit base session lookup failed", exc_info=True)
        return "no_session", None
    finally:
        if conn:
            conn.close()


async def trigger_collection(
    user_id: str,
    audit_id: str,
    *,
    service: SgAuditService | None = None,
    lambda_client=None,
    force: bool = False,
) -> dict:
    """Launch a cross-account SG audit. Idempotent + cost-guarded.

    Returns a status dict; never raises for control-flow conditions
    (disabled/not-ready/no-session/expiring/unchanged/already-running).
    """
    service = service or SgAuditService()
    record = service.get_audit(user_id, audit_id)
    if not record:
        return {"status": "error", "message": "audit not found"}
    if not service.ready_for_collection(record):
        return {"status": "skipped", "reason": "no target accounts or discovery configured"}

    # A base AWS session is required for BOTH paths: the Lambda assumes member
    # roles FROM these creds, and the in-process fallback uses them directly.
    status, session_row = _base_session(user_id, sg_config.SG_MIN_SESSION_TTL_SECONDS)
    if status == "no_session":
        return {"status": "no_session", "reason": "connect an AWS account first via /aws/saml/login"}
    if status == "expiring":
        return {
            "status": "session_expiring",
            "reason": "AWS session expires too soon; re-authenticate via /aws/saml/login",
        }

    scope = service.scope_of(record)
    fingerprint = scope_fingerprint(scope)
    if not force and await inputs_unchanged(audit_id, fingerprint):
        return {"status": "unchanged", "reason": "scope matches last audit"}

    if not await acquire_inflight(audit_id):
        return {"status": "already_running"}

    scan_id = uuid.uuid4().hex
    # Pre-write the in-flight state BEFORE launching so the callback's
    # (user_id, audit_id) consistency guard always finds the record.
    record["scan_state"] = SCAN_IN_FLIGHT
    service.storage.save_audit(user_id, record)

    base_credentials = {
        "access_key_id": session_row["aws_access_key_id"],
        "secret_access_key": session_row["aws_secret_access_key"],
        "session_token": session_row.get("aws_session_token"),
        "region": session_row.get("aws_region") or sg_config.AWS_REGION,
    }
    management_account_id = session_row.get("aws_account_id", "")

    # --- Preferred path: hand off to the isolated collector Lambda ----------
    if sg_config.collection_enabled():
        callback_url = _callback_url()
        if not callback_url:
            record["scan_state"] = SCAN_PENDING
            service.storage.save_audit(user_id, record)
            await release_inflight(audit_id)
            return {"status": "error", "message": "no callback URL configured"}

        payload = {
            "scan_id": scan_id,
            "audit_id": audit_id,
            "user_id": user_id,
            "callback_url": callback_url,
            "hmac_secret": sg_config.SG_HMAC_SECRET,
            "external_id": record.get("external_id", ""),
            "scope": scope,
            "base_credentials": base_credentials,
            "management_account_id": management_account_id,
        }
        try:
            client = lambda_client or _lambda_client()
            client.invoke(
                FunctionName=sg_config.SG_LAMBDA_ARN,
                InvocationType="Event",  # async fire-and-forget
                Payload=json.dumps(payload).encode("utf-8"),
            )
        except Exception as exc:
            logger.warning("SG-audit Lambda invoke failed: %s", exc, exc_info=True)
            record["scan_state"] = SCAN_PENDING
            service.storage.save_audit(user_id, record)
            await release_inflight(audit_id)
            return {"status": "error", "message": f"invoke failed: {exc}"}

        # NOTE: do not log `payload` — it contains base credentials + the HMAC secret.
        logger.info("Launched SG audit scan %s for audit %s (lambda)", scan_id, audit_id)
        return {"status": "launched", "scan_id": scan_id, "mode": "lambda"}

    # --- Fallback: no collector Lambda configured — run in-process ----------
    # Uses the same collector (`runner.run_collection`) the Lambda runs, in a
    # background thread, so the audit works with just an AWS connection (no
    # Lambda deploy). The frontend poller flips in_flight -> complete when the
    # snapshot is persisted, identical to the Lambda async UX.
    threading.Thread(
        target=_run_inprocess_collection,
        args=(user_id, audit_id, scan_id, scope, record.get("external_id", ""),
              base_credentials, management_account_id),
        name=f"sg-audit-{scan_id[:8]}",
        daemon=True,
    ).start()
    logger.info("Launched SG audit scan %s for audit %s (in-process)", scan_id, audit_id)
    return {"status": "launched", "scan_id": scan_id, "mode": "in_process"}


def _run_inprocess_collection(
    user_id, audit_id, scan_id, scope, external_id, base_credentials, management_account_id
):
    """Background-thread in-process collection (no collector Lambda configured).

    Runs the same `runner.run_collection` the Lambda uses, then persists via the
    shared `_store_and_advance` path. Owns its own event loop (the redis client
    is a sync client wrapped with to_thread, so a fresh loop here is safe). On any
    failure the audit is marked failed and the in-flight lock released.
    """
    service = SgAuditService()
    try:
        from sg_audit.collector_lambda.runner import run_collection

        snapshot = run_collection(
            scan_id=scan_id,
            audit_id=audit_id,
            scope=scope,
            external_id=external_id,
            base_credentials=base_credentials,
            management_account_id=management_account_id,
        )
        snapshot["user_id"] = user_id
        asyncio.run(_store_and_advance(service, user_id, audit_id, snapshot))
    except Exception:
        logger.warning("In-process SG audit %s failed", scan_id, exc_info=True)
        try:
            record = service.get_audit(user_id, audit_id)
            if record:
                record["scan_state"] = SCAN_FAILED
                record["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                service.storage.save_audit(user_id, record)
        finally:
            try:
                asyncio.run(release_inflight(audit_id))
            except Exception:
                logger.debug("release_inflight after in-process failure failed", exc_info=True)


async def _store_and_advance(service, user_id: str, audit_id: str, snapshot: dict) -> tuple[str, int]:
    """Validate + persist a snapshot and advance the audit record.

    Shared by the HMAC callback (Lambda path) and the in-process fallback. The
    audit record is assumed to already exist (pre-written before launch).
    Returns (scan_state, n_findings).
    """
    snapshot["findings"] = [f for f in (snapshot.get("findings") or []) if validate_finding(f)]
    service.storage.save_snapshot(user_id, snapshot)

    from sg_audit.scheduler import compute_next_scan_at

    collector_status = snapshot.get("collector_status") or {}
    base_err = str(collector_status.get("_base", "")).startswith("error")
    failed = bool(snapshot.get("fatal")) or (base_err and not snapshot["findings"])

    record = service.get_audit(user_id, audit_id) or {"audit_id": audit_id, "user_id": user_id}
    record["scan_state"] = SCAN_FAILED if failed else SCAN_COMPLETE
    record["latest_scan_id"] = snapshot.get("scan_id")
    record["last_scan_at"] = snapshot.get("scanned_at")
    record["latest_risk_score"] = snapshot.get("risk_score", 0.0)
    record["latest_posture_score"] = snapshot.get("posture_score", 0.0)
    record["next_scan_at"] = compute_next_scan_at()
    record["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    service.storage.save_audit(user_id, record)

    fingerprint = scope_fingerprint(service.scope_of(record))
    await record_fingerprint(audit_id, fingerprint)
    await release_inflight(audit_id)

    logger.info(
        "Stored SG-audit snapshot %s (%d findings, state=%s)",
        snapshot.get("scan_id"), len(snapshot["findings"]), record["scan_state"],
    )
    return record["scan_state"], len(snapshot["findings"])


async def process_callback(
    raw_body: bytes,
    headers: dict,
    *,
    service: SgAuditService | None = None,
) -> tuple[int, dict]:
    """Verify + persist a signed snapshot. Returns ``(http_status, body)``."""
    secret = sg_config.SG_HMAC_SECRET
    if not secret:
        return 503, {"status": "error", "message": "callback not configured"}

    if len(raw_body) > sg_config.SG_CALLBACK_MAX_BYTES:
        return 413, {"status": "error", "message": "payload too large"}

    # Case-insensitive header lookup: WSGI/Werkzeug normalizes header names, so an
    # exact-case .get() on a plain dict would miss and wrongly 401.
    hdr = {str(k).lower(): v for k, v in (headers or {}).items()}
    ts = hdr.get(TS_HEADER.lower(), "")
    nonce = hdr.get(NONCE_HEADER.lower(), "")
    sig = hdr.get(SIG_HEADER.lower(), "")

    if not timestamp_within_skew(ts, sg_config.SG_CALLBACK_MAX_SKEW):
        return 401, {"status": "error", "message": "timestamp out of range"}
    if not signature_valid(secret, ts, nonce, raw_body, sig):
        return 401, {"status": "error", "message": "bad signature"}
    if not await consume_nonce(nonce, sg_config.SG_CALLBACK_MAX_SKEW):
        return 409, {"status": "error", "message": "replayed nonce"}

    try:
        snapshot = json.loads(raw_body)
    except (ValueError, TypeError):
        return 400, {"status": "error", "message": "invalid json"}

    user_id = snapshot.get("user_id")
    audit_id = snapshot.get("audit_id")
    scan_id = snapshot.get("scan_id")
    if not (user_id and audit_id and scan_id):
        return 400, {"status": "error", "message": "missing identifiers"}

    service = service or SgAuditService()
    record = service.get_audit(user_id, audit_id)
    # Consistency guard: the (user_id, audit_id) pair must already exist —
    # prevents a signed payload from writing into an unrelated user's prefix.
    if not record:
        await release_inflight(audit_id)
        return 404, {"status": "error", "message": "unknown audit"}

    state, n = await _store_and_advance(service, user_id, audit_id, snapshot)
    return 200, {"status": "success", "scan_id": scan_id, "findings": n, "scan_state": state}
