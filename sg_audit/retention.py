"""Posture-snapshot retention — purge old audit scans (per-user, S3-scoped).

Drops posture snapshots older than the retention window for each of a user's
audits, always keeping the most recent snapshot so the dashboard and report
never go blank. Storage growth from periodic re-audits is bounded without
losing the current posture.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sg_audit.config import SG_RETENTION_DAYS
from utils.base_logger import get_logger

logger = get_logger(__name__)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def purge_expired(
    user_id: str,
    *,
    service=None,
    now: datetime | None = None,
    retention_days: int | None = None,
) -> dict:
    """Purge snapshots older than the retention window. Returns a summary."""
    if service is None:
        from sg_audit.service import SgAuditService

        service = SgAuditService()
    days = SG_RETENTION_DAYS if retention_days is None else retention_days
    cutoff = _iso((now or datetime.now(timezone.utc)) - timedelta(days=days))

    total = 0
    by_audit: dict[str, int] = {}
    for rec in service.list_audits(user_id):
        aid = rec["audit_id"]
        try:
            n = service.storage.purge_snapshots_before(user_id, aid, cutoff, keep_latest=True)
        except Exception:
            logger.warning("purge failed for %s/%s", user_id, aid, exc_info=True)
            continue
        if n:
            by_audit[aid] = n
            total += n
    logger.info("SG-audit retention purge for %s removed %d snapshot(s)", user_id, total)
    return {"purged": total, "by_audit": by_audit, "cutoff": cutoff}
