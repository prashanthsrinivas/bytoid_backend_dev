"""RDS-backed reverse-lookup graph: which tracker rows reference a statement.

The tracker cell (which statements a row maps to) lives in the encrypted S3
``tracker.json`` blob. This table is the *queryable* projection of that data so
the policy hub can answer "which trackers/rows reference statement X?" without
scanning every tracker.

RDS is the source of truth for the graph. Callers persist RDS first, then S3
(see the wiring in ``tab_tracker/routes.py``); a nightly reconcile cron heals
any drift. Every mutation emits an audit event (best-effort, never raises).

Primary key ``(tracker_id, row_id, column_id, statement_id)`` makes upserts
idempotent.
"""

from datetime import datetime, timezone

import pymysql.cursors

from db.rds_db import connect_to_rds
from utils.base_logger import get_logger

logger = get_logger(__name__)


def _ensure_table(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS statement_tracker_refs (
            statement_id   VARCHAR(64)  NOT NULL,
            policy_id      VARCHAR(64)  NOT NULL,
            doc_type       VARCHAR(16)  NOT NULL,
            tracker_id     VARCHAR(64)  NOT NULL,
            tracker_abbrev VARCHAR(16)  NULL,
            row_id         VARCHAR(64)  NOT NULL,
            column_id      VARCHAR(64)  NOT NULL,
            status         VARCHAR(24)  NOT NULL DEFAULT 'active',
            updated_at     DATETIME     NOT NULL,
            PRIMARY KEY (tracker_id, row_id, column_id, statement_id),
            KEY idx_statement (statement_id),
            KEY idx_policy (policy_id),
            KEY idx_tracker (tracker_id)
        )
        """
    )


def _emit_audit(action: str, metadata: dict) -> None:
    """Best-effort audit emission — never raises."""
    try:
        from services.audit_log_service import log_audit_event
        log_audit_event(
            action=action,
            endpoint="statement_tracker_refs",
            ip="background",
            status="success",
            metadata=metadata,
        )
    except Exception as exc:
        logger.debug("statement_tracker_refs audit emit failed (%s): %s", action, exc)


def replace_cell_refs(
    tracker_id: str,
    row_id: str,
    column_id: str,
    policy_id: str,
    doc_type: str,
    tracker_abbrev: str | None,
    statements: list[dict],
) -> int:
    """Replace all refs for one tracker cell with *statements*.

    ``statements`` is a list of ``{statement_id, status?}`` dicts. Deletes any
    existing refs for ``(tracker_id, row_id, column_id)`` first, then inserts
    the new set — keeping RDS consistent with the cell's current contents.
    Returns the number of refs written.
    """
    now = datetime.now(timezone.utc)
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            _ensure_table(cur)
            cur.execute(
                "DELETE FROM statement_tracker_refs "
                "WHERE tracker_id=%s AND row_id=%s AND column_id=%s",
                (tracker_id, row_id, column_id),
            )
            written = 0
            for st in statements or []:
                sid = st.get("statement_id")
                if not sid:
                    continue
                cur.execute(
                    """
                    INSERT INTO statement_tracker_refs
                        (statement_id, policy_id, doc_type, tracker_id,
                         tracker_abbrev, row_id, column_id, status, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        policy_id=VALUES(policy_id),
                        doc_type=VALUES(doc_type),
                        tracker_abbrev=VALUES(tracker_abbrev),
                        status=VALUES(status),
                        updated_at=VALUES(updated_at)
                    """,
                    (
                        sid, policy_id, doc_type, tracker_id, tracker_abbrev,
                        row_id, column_id, st.get("status", "active"), now,
                    ),
                )
                written += 1
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception as rb_exc:
            logger.debug("replace_cell_refs rollback failed: %s", rb_exc)
        raise
    finally:
        conn.close()

    _emit_audit(
        "STATEMENT_TRACKER_REF_UPSERT",
        {"tracker_id": tracker_id, "row_id": row_id, "column_id": column_id,
         "policy_id": policy_id, "count": written},
    )
    return written


def delete_refs_for_policy(tracker_id: str, policy_id: str) -> int:
    """Delete all refs for one policy within one tracker (policy unlinked)."""
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            _ensure_table(cur)
            cur.execute(
                "DELETE FROM statement_tracker_refs WHERE tracker_id=%s AND policy_id=%s",
                (tracker_id, policy_id),
            )
            deleted = cur.rowcount
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception as rb_exc:
            logger.debug("delete_refs_for_policy rollback failed: %s", rb_exc)
        raise
    finally:
        conn.close()

    _emit_audit(
        "STATEMENT_TRACKER_REF_DELETE",
        {"tracker_id": tracker_id, "policy_id": policy_id, "count": deleted},
    )
    return deleted


def delete_refs_for_tracker(tracker_id: str) -> int:
    """Cascade-delete all refs for a tracker (tracker deleted)."""
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            _ensure_table(cur)
            cur.execute(
                "DELETE FROM statement_tracker_refs WHERE tracker_id=%s",
                (tracker_id,),
            )
            deleted = cur.rowcount
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception as rb_exc:
            logger.debug("delete_refs_for_tracker rollback failed: %s", rb_exc)
        raise
    finally:
        conn.close()

    _emit_audit("STATEMENT_TRACKER_REF_DELETE", {"tracker_id": tracker_id, "count": deleted})
    return deleted


def set_status_for_statement(policy_id: str, statement_id: str, status: str) -> int:
    """Propagate a status change (e.g. 'superseded') to all refs of a statement."""
    now = datetime.now(timezone.utc)
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            _ensure_table(cur)
            cur.execute(
                "UPDATE statement_tracker_refs SET status=%s, updated_at=%s "
                "WHERE policy_id=%s AND statement_id=%s",
                (status, now, policy_id, statement_id),
            )
            updated = cur.rowcount
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception as rb_exc:
            logger.debug("set_status_for_statement rollback failed: %s", rb_exc)
        raise
    finally:
        conn.close()
    return updated


def get_trackers_for_statement(
    statement_id: str,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict], int]:
    """Return ``(rows, total)`` of tracker refs for one statement, paged."""
    page = max(1, page)
    page_size = min(200, max(1, page_size))
    offset = (page - 1) * page_size
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            _ensure_table(cur)
            cur.execute(
                "SELECT COUNT(*) AS c FROM statement_tracker_refs WHERE statement_id=%s",
                (statement_id,),
            )
            total = (cur.fetchone() or {}).get("c", 0)
            cur.execute(
                "SELECT tracker_id, tracker_abbrev, row_id, column_id, policy_id, "
                "doc_type, status, updated_at FROM statement_tracker_refs "
                "WHERE statement_id=%s ORDER BY tracker_abbrev, row_id "
                "LIMIT %s OFFSET %s",
                (statement_id, page_size, offset),
            )
            rows = [dict(r) for r in (cur.fetchall() or [])]
    finally:
        conn.close()
    return rows, int(total)


def get_refs_for_policy(policy_id: str, tracker_id: str | None = None) -> list[dict]:
    """Return all refs for a policy, optionally scoped to one tracker.

    Powers ``GET /policy-hub/<policy_id>/tracker-map``.
    """
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            _ensure_table(cur)
            if tracker_id:
                cur.execute(
                    "SELECT statement_id, tracker_id, tracker_abbrev, row_id, "
                    "column_id, doc_type, status FROM statement_tracker_refs "
                    "WHERE policy_id=%s AND tracker_id=%s",
                    (policy_id, tracker_id),
                )
            else:
                cur.execute(
                    "SELECT statement_id, tracker_id, tracker_abbrev, row_id, "
                    "column_id, doc_type, status FROM statement_tracker_refs "
                    "WHERE policy_id=%s",
                    (policy_id,),
                )
            return [dict(r) for r in (cur.fetchall() or [])]
    finally:
        conn.close()


def get_trackers_for_policy(policy_id: str) -> list[dict]:
    """Return distinct trackers referencing a policy, with mapped row counts.

    Powers ``GET /policy-hub/<policy_id>/trackers`` (the drag-and-drop tag set).
    """
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            _ensure_table(cur)
            cur.execute(
                "SELECT tracker_id, tracker_abbrev, "
                "COUNT(DISTINCT row_id) AS mapped_row_count "
                "FROM statement_tracker_refs WHERE policy_id=%s "
                "GROUP BY tracker_id, tracker_abbrev "
                "ORDER BY tracker_abbrev",
                (policy_id,),
            )
            return [dict(r) for r in (cur.fetchall() or [])]
    finally:
        conn.close()
