"""Nightly reconciliation of the statement↔tracker reverse-lookup graph.

The S3 ``tracker.json`` blob is the source of truth for which statements each
policy cell maps to; ``statement_tracker_refs`` (RDS) is the queryable
projection. Writes persist RDS-first then S3, so the only possible drift is an
RDS row whose backing cell vanished, or a cell whose RDS row never landed.

This job rebuilds RDS from S3 per tracker (``sync_tracker_refs`` does a
delete-all + reinsert), which heals both directions. It is idempotent and safe
to run repeatedly.
"""

from utils.base_logger import get_logger

logger = get_logger(__name__)


def reconcile_user(
    user_id: str,
    *,
    config_loader=None,
    tracker_loader=None,
    syncer=None,
) -> dict:
    """Reconcile every tracker owned by *user_id*.

    Loaders are injectable for testing; in production they default to the real
    S3 + RDS implementations. Returns ``{trackers, cells_synced, errors}``.
    """
    if config_loader is None:
        from tab_tracker.helper import check_config_exist

        def config_loader(uid):
            _path, data = check_config_exist(uid)
            return data
    if tracker_loader is None:
        from utils.s3_utils import load_yaml_from_s3

        def tracker_loader(uid, tracker_id):
            return load_yaml_from_s3(f"{uid}/tracker/{tracker_id}/tracker.json")
    if syncer is None:
        from tab_tracker.refs_sync import sync_tracker_refs
        syncer = sync_tracker_refs

    summary = {"trackers": 0, "cells_synced": 0, "errors": 0}
    config = config_loader(user_id)
    if not config:
        return summary

    for meta in config.get("trackers", []) or []:
        tracker_id = meta.get("tracker_id")
        if not tracker_id:
            continue
        summary["trackers"] += 1
        try:
            tracker_data = tracker_loader(user_id, tracker_id)
            if not tracker_data:
                continue
            synced = syncer(tracker_id, meta.get("tracker_abbrev"), tracker_data)
            summary["cells_synced"] += synced
        except Exception as exc:
            summary["errors"] += 1
            logger.error("reconcile: tracker=%s user=%s failed: %s", tracker_id, user_id, exc)

    return summary


def _all_user_ids() -> list[str]:
    import pymysql.cursors

    from db.rds_db import connect_to_rds

    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT user_id FROM users")
            return [r["user_id"] for r in cur.fetchall() if r.get("user_id")]
    finally:
        conn.close()


def reconcile_all(user_ids=None) -> dict:
    """Reconcile every user's trackers. Emits one audit row summarising the run."""
    user_ids = user_ids if user_ids is not None else _all_user_ids()
    totals = {"users": 0, "trackers": 0, "cells_synced": 0, "errors": 0}
    for uid in user_ids:
        s = reconcile_user(uid)
        totals["users"] += 1
        totals["trackers"] += s["trackers"]
        totals["cells_synced"] += s["cells_synced"]
        totals["errors"] += s["errors"]

    try:
        from services.audit_log_service import log_audit_event
        log_audit_event(
            action="STATEMENT_TRACKER_REF_RECONCILED",
            endpoint="cron/reconcile_statement_tracker_refs",
            ip="cron",
            status="success",
            metadata=totals,
        )
    except Exception as exc:
        logger.debug("reconcile audit emit failed: %s", exc)

    logger.info("statement_tracker_refs reconcile complete: %s", totals)
    return totals
