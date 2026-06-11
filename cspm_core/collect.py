"""Collection spine — trigger an in-process scan + persist the result.

``trigger_collection`` resolves credentials (provider mints fresh tokens), guards
with a Redis in-flight lock + scope change-detection, pre-writes the in-flight
state, then runs the engine in a background thread and persists via
``_store_and_advance``. Never raises for control-flow conditions.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import datetime, timezone

from utils.base_logger import get_logger
from cspm_core import engine
from cspm_core.helpers import (
    acquire_inflight,
    inputs_unchanged,
    record_fingerprint,
    release_inflight,
    scope_fingerprint,
)
from cspm_core.normalize import validate_finding
from cspm_core.schema import SCAN_COMPLETE, SCAN_FAILED, SCAN_IN_FLIGHT
from cspm_core.service import CspmService

logger = get_logger(__name__)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def trigger_collection(provider, user_id, audit_id, *, service=None, force=False) -> dict:
    service = service or CspmService(provider)
    ns = provider.redis_namespace
    record = service.get_audit(user_id, audit_id)
    if not record:
        return {"status": "error", "message": "audit not found"}
    if not service.ready_for_collection(record):
        return {"status": "skipped", "reason": "audit not ready"}

    try:
        creds = provider.resolve_credentials(user_id)
    except Exception as exc:
        logger.warning("%s credential resolution failed: %s", provider.key, exc, exc_info=True)
        creds = None
    if not creds:
        return {"status": "no_session",
                "reason": f"connect {provider.label} first via the {provider.label} Integration"}

    scope = service.scope_of(record)
    fingerprint = scope_fingerprint(scope)
    if not force and await inputs_unchanged(ns, audit_id, fingerprint):
        return {"status": "unchanged", "reason": "scope matches last audit"}
    if not await acquire_inflight(ns, audit_id):
        return {"status": "already_running"}

    scan_id = uuid.uuid4().hex
    record["scan_state"] = SCAN_IN_FLIGHT
    service.storage.save_audit(user_id, record)

    threading.Thread(
        target=_run, args=(provider, user_id, audit_id, scan_id, scope, creds),
        name=f"{provider.key}-audit-{scan_id[:8]}", daemon=True,
    ).start()
    logger.info("Launched %s audit scan %s for %s", provider.key, scan_id, audit_id)
    return {"status": "launched", "scan_id": scan_id}


def _run(provider, user_id, audit_id, scan_id, scope, creds):
    service = CspmService(provider)
    try:
        snapshot = engine.run_collection(provider, scan_id=scan_id, audit_id=audit_id, scope=scope, creds=creds)
        asyncio.run(_store_and_advance(provider, service, user_id, audit_id, snapshot))
    except Exception:
        logger.warning("%s in-process audit %s failed", provider.key, scan_id, exc_info=True)
        try:
            record = service.get_audit(user_id, audit_id)
            if record:
                record["scan_state"] = SCAN_FAILED
                record["updated_at"] = _now()
                service.storage.save_audit(user_id, record)
        finally:
            try:
                asyncio.run(release_inflight(provider.redis_namespace, audit_id))
            except Exception:
                logger.debug("release_inflight after failure failed", exc_info=True)


async def _store_and_advance(provider, service, user_id, audit_id, snapshot) -> tuple:
    snapshot["findings"] = [f for f in (snapshot.get("findings") or []) if validate_finding(f)]
    service.storage.save_snapshot(user_id, snapshot)

    collector_status = snapshot.get("collector_status") or {}
    base_err = str(collector_status.get("_discovery", "")).startswith("error")
    failed = bool(snapshot.get("fatal")) or (base_err and not snapshot["findings"])

    record = service.get_audit(user_id, audit_id) or {"audit_id": audit_id, "user_id": user_id}
    record["scan_state"] = SCAN_FAILED if failed else SCAN_COMPLETE
    record["latest_scan_id"] = snapshot.get("scan_id")
    record["last_scan_at"] = snapshot.get("scanned_at")
    record["latest_risk_score"] = snapshot.get("risk_score", 0.0)
    record["latest_posture_score"] = snapshot.get("posture_score", 0.0)
    record["updated_at"] = _now()
    service.storage.save_audit(user_id, record)

    await record_fingerprint(provider.redis_namespace, audit_id, scope_fingerprint(service.scope_of(record)))
    await release_inflight(provider.redis_namespace, audit_id)
    logger.info("Stored %s snapshot %s (%d findings, state=%s)",
                provider.key, snapshot.get("scan_id"), len(snapshot["findings"]), record["scan_state"])
    return record["scan_state"], len(snapshot["findings"])
