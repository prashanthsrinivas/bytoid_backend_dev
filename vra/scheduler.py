"""Re-scan scheduling + the reconciliation safety net (per-user, S3-scoped).

Pure helpers (``compute_next_scan_at``/``is_due_for_rescan``) plus two async
drivers that iterate a user's assessments and (re)launch collection:

  * ``rescan_due`` — periodic refresh: assessments whose ``next_scan_at`` has
    passed. Called from a scheduled job per user.
  * ``reconcile_pending`` — the gap-4 safety net: assessments that are
    collection-ready but were never scanned (e.g. the frontend trigger was
    missed), so collection never silently stalls.

Both delegate to ``trigger_collection`` (which already enforces the in-flight
lock + change-detection dedup + credit/cost guards), so they are idempotent.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from vra.config import VRA_RESCAN_CADENCE_DAYS
from vra.schema import SCAN_COMPLETE, SCAN_PENDING


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_next_scan_at(now: datetime | None = None, cadence_days: int | None = None) -> str | None:
    """Next scheduled scan time (ISO), or None when auto re-scan is disabled."""
    cadence = VRA_RESCAN_CADENCE_DAYS if cadence_days is None else cadence_days
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


async def rescan_due(
    user_id: str, *, service=None, trigger=None, now_iso: str | None = None
) -> list[dict]:
    """Launch a re-scan for every assessment whose cadence has elapsed."""
    if service is None:
        from vra.service import VraService

        service = VraService()
    if trigger is None:
        from vra.collect import trigger_collection as trigger
    now_iso = now_iso or _iso(_now())

    results = []
    for rec in service.list_assessments(user_id):
        if is_due_for_rescan(rec, now_iso):
            res = await trigger(user_id, rec["assessment_id"], service=service)
            results.append({"assessment_id": rec["assessment_id"], **res})
    return results


async def reconcile_pending(user_id: str, *, service=None, trigger=None) -> list[dict]:
    """Safety net: trigger collection for ready-but-never-scanned assessments."""
    if service is None:
        from vra.service import VraService

        service = VraService()
    if trigger is None:
        from vra.collect import trigger_collection as trigger

    results = []
    for rec in service.list_assessments(user_id):
        if (
            service.ready_for_collection(rec)
            and rec.get("scan_state") == SCAN_PENDING
            and not rec.get("latest_scan_id")
        ):
            res = await trigger(user_id, rec["assessment_id"], service=service)
            results.append({"assessment_id": rec["assessment_id"], **res})
    return results
