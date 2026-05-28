"""Nightly heal of ``policy_hub_documents`` against the S3 source of truth.

The metadata index is written-through on every persist/delete, but a transient
RDS outage or a partial deploy can leave it out of sync. This reconcile diffs
each user's S3 ``{user_id}/policies/*.yaml`` set against the indexed rows:

  - YAMLs without an index row  → upsert (read the YAML, write to index).
  - Index rows without a YAML   → delete (the doc was removed in S3).

Runs from a Celery beat task (see ``utils/celery_base.py``) at 02:00 UTC, and
is also callable directly via ``celery -A utils.celery_base call
tasks.reconcile_policy_hub_documents``. Mirrors ``tab_tracker/reconcile.py``.
"""

from utils.base_logger import get_logger

logger = get_logger(__name__)


def _extract_policy_id_from_key(key: str) -> str | None:
    """``user_id/policies/<policy_id>.yaml`` → ``<policy_id>``."""
    if not key or not key.endswith(".yaml"):
        return None
    if "/jobs/" in key or "/raw/" in key:
        return None
    tail = key.rsplit("/", 1)[-1]
    pid = tail[:-5]  # strip .yaml
    return pid or None


def reconcile_user(user_id: str) -> dict:
    """Diff one user's S3 prefix against ``policy_hub_documents`` and heal."""
    from utils.s3_utils import list_all_files
    from policy_hub.doc_index import (
        delete_document,
        list_document_ids,
        upsert_document,
    )
    from policy_hub.routes import _read_policy_yaml

    summary = {"upserted": 0, "deleted": 0, "errors": 0}

    s3_ids: set[str] = set()
    s3_keys_by_id: dict[str, str] = {}
    for obj in list_all_files(folder=f"{user_id}/policies/") or []:
        key = obj.get("Key", "")
        pid = _extract_policy_id_from_key(key)
        if not pid:
            continue
        s3_ids.add(pid)
        s3_keys_by_id[pid] = key

    try:
        indexed_ids = list_document_ids(user_id)
    except Exception as exc:
        logger.warning("reconcile: list_document_ids failed for user=%s: %s", user_id, exc)
        return summary

    # YAMLs without an index row → upsert.
    for pid in s3_ids - indexed_ids:
        try:
            data = _read_policy_yaml(user_id, s3_keys_by_id[pid])
            if data and data.get("policy_id"):
                upsert_document(user_id, data)
                summary["upserted"] += 1
        except Exception as exc:
            summary["errors"] += 1
            logger.warning("reconcile: upsert failed user=%s pid=%s: %s", user_id, pid, exc)

    # Index rows without a YAML → delete (doc removed in S3).
    for pid in indexed_ids - s3_ids:
        try:
            delete_document(pid)
            summary["deleted"] += 1
        except Exception as exc:
            summary["errors"] += 1
            logger.warning("reconcile: delete failed user=%s pid=%s: %s", user_id, pid, exc)

    return summary


def reconcile_all() -> dict:
    """Reconcile every user. Returns aggregate ``{users, upserted, deleted, errors}``."""
    import pymysql.cursors
    from db.rds_db import connect_to_rds

    totals = {"users": 0, "upserted": 0, "deleted": 0, "errors": 0}
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT user_id FROM users")
            user_ids = [r["user_id"] for r in (cur.fetchall() or []) if r.get("user_id")]
    finally:
        conn.close()

    for uid in user_ids:
        s = reconcile_user(uid)
        totals["users"] += 1
        totals["upserted"] += s["upserted"]
        totals["deleted"] += s["deleted"]
        totals["errors"] += s["errors"]

    logger.info("policy_hub_documents reconcile complete: %s", totals)
    return totals
