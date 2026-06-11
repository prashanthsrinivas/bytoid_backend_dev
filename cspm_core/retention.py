"""Posture-snapshot retention purge (per provider, per user, S3-scoped)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from utils.base_logger import get_logger

logger = get_logger(__name__)
_RETENTION_DAYS = 365


def purge_expired(provider, user_id, *, service=None, now=None, retention_days=None) -> dict:
    if service is None:
        from cspm_core.service import CspmService
        service = CspmService(provider)
    days = retention_days or _RETENTION_DAYS
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    total, by_audit = 0, {}
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
    return {"purged": total, "by_audit": by_audit, "cutoff": cutoff}
