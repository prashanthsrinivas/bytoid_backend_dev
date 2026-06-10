"""Re-audit scheduling + the reconciliation safety net (per-user, S3-scoped).

Pure helpers (``compute_next_scan_at``/``is_due_for_rescan``) plus two async
drivers that iterate a user's audits and (re)launch collection:

  * ``rescan_due`` — periodic refresh: audits whose ``next_scan_at`` has passed.
  * ``reconcile_pending`` — safety net: audits that are collection-ready but were
    never scanned (e.g. the frontend trigger was missed), so collection never
    silently stalls.

Both delegate to ``trigger_collection`` (which enforces the base-session TTL
check + in-flight lock + change-detection dedup), so they are idempotent.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sg_audit.config import SG_RESCAN_CADENCE_DAYS
from sg_audit.schema import SCAN_COMPLETE, SCAN_PENDING


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_next_scan_at(now: datetime | None = None, cadence_days: int | None = None) -> str | None:
    """Next scheduled audit time (ISO), or None when auto re-audit is disabled."""
    cadence = SG_RESCAN_CADENCE_DAYS if cadence_days is None else cadence_days
    if cadence <= 0:
        return None
    return _iso((now or _now()) + timedelta(days=cadence))


def is_due_for_rescan(record: dict, now_iso: str | None = None) -> bool:
    if record.get("scan_state") != SCAN_COMPLETE:
        return False
    nxt = record.get("next_scan_at")
    if not nxt:
        return False
    return nxt <= (now_iso or _iso(_now()))


async def rescan_due(user_id: str, *, service=None, trigger=None, now_iso: str | None = None) -> list[dict]:
    """Launch a re-audit for every audit whose cadence has elapsed."""
    if service is None:
        from sg_audit.service import SgAuditService

        service = SgAuditService()
    if trigger is None:
        from sg_audit.collect import trigger_collection as trigger
    now_iso = now_iso or _iso(_now())

    results = []
    for rec in service.list_audits(user_id):
        if is_due_for_rescan(rec, now_iso):
            res = await trigger(user_id, rec["audit_id"], service=service)
            results.append({"audit_id": rec["audit_id"], **res})
    return results


async def reconcile_pending(user_id: str, *, service=None, trigger=None) -> list[dict]:
    """Safety net: trigger collection for ready-but-never-scanned audits."""
    if service is None:
        from sg_audit.service import SgAuditService

        service = SgAuditService()
    if trigger is None:
        from sg_audit.collect import trigger_collection as trigger

    results = []
    for rec in service.list_audits(user_id):
        if (
            service.ready_for_collection(rec)
            and rec.get("scan_state") == SCAN_PENDING
            and not rec.get("latest_scan_id")
        ):
            res = await trigger(user_id, rec["audit_id"], service=service)
            results.append({"audit_id": rec["audit_id"], **res})
    return results
