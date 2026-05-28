"""DB access layer for AI Governance scan runs and per-user results.

Backs the platform-wide Giskard sweep.  Mirrors ``rules_store`` patterns:
``connect_to_rds`` + ``safe_execute`` + ``DictCursor`` + a ``_safe_close``
finally block.

PRIVACY INVARIANT: callers must pass only redacted excerpts / issue metadata /
counts in the ``result`` and ``summary`` payloads.  Raw decrypted user text must
never reach this layer (see ``scan_sources.extract_pii_docs``).
"""

from __future__ import annotations

import json
import logging
import uuid

import pymysql.cursors

from db.rds_db import connect_to_rds, safe_execute

logger = logging.getLogger(__name__)

# Statuses that mean a run is still occupying the platform (idempotency guard).
_ACTIVE_STATUSES = ("queued", "running")


def _safe_close(conn) -> None:
    if conn is None:
        return
    try:
        conn.close()
    except Exception:
        logger.debug("scan_results_store: connection close suppressed", exc_info=True)


def new_run_id() -> str:
    return str(uuid.uuid4())


# ── Write path ──────────────────────────────────────────────────────────────────


def has_active_platform_run() -> bool:
    """True if a platform sweep is already queued/running (idempotency guard)."""
    conn = connect_to_rds()
    if conn is None:
        return False
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM ai_governance_scan_runs "
                "WHERE scope = 'platform' AND status IN %s",
                (_ACTIVE_STATUSES,),
            )
            row = cur.fetchone()
        conn.commit()
        return bool(row and row["n"])
    except Exception as exc:
        logger.warning("scan_results_store: active-run check failed: %s", exc)
        return False
    finally:
        _safe_close(conn)


def create_run(
    run_id: str,
    *,
    scope: str,
    modes: list[str],
    started_by: str | None,
    status: str = "queued",
) -> bool:
    """Insert a scan-run row. Returns False on DB failure (caller decides)."""
    conn = connect_to_rds()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            safe_execute(
                cur,
                "INSERT INTO ai_governance_scan_runs "
                "(run_id, scope, status, modes, started_by) VALUES (%s, %s, %s, %s, %s)",
                (run_id, scope, status, json.dumps(modes or []), started_by),
            )
        conn.commit()
        return True
    except Exception as exc:
        logger.warning("scan_results_store: create_run failed: %s", exc)
        return False
    finally:
        _safe_close(conn)


def set_run_status(run_id: str, status: str, *, user_count: int | None = None) -> None:
    conn = connect_to_rds()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            if user_count is None:
                safe_execute(
                    cur,
                    "UPDATE ai_governance_scan_runs SET status = %s WHERE run_id = %s",
                    (status, run_id),
                )
            else:
                safe_execute(
                    cur,
                    "UPDATE ai_governance_scan_runs SET status = %s, user_count = %s "
                    "WHERE run_id = %s",
                    (status, user_count, run_id),
                )
        conn.commit()
    except Exception as exc:
        logger.warning("scan_results_store: set_run_status failed: %s", exc)
    finally:
        _safe_close(conn)


def record_user_result(run_id: str, user_result: dict) -> None:
    """Persist one user's scan result. Never raises (sweep must continue)."""
    conn = connect_to_rds()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            safe_execute(
                cur,
                "INSERT INTO ai_governance_scan_user_results "
                "(result_id, run_id, user_id, org_admin_id, status, result) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    str(uuid.uuid4()),
                    run_id,
                    str(user_result.get("user_id") or ""),
                    user_result.get("org_admin_id"),
                    user_result.get("status", "ok"),
                    json.dumps(user_result, default=str),
                ),
            )
        conn.commit()
    except Exception as exc:
        logger.warning("scan_results_store: record_user_result failed: %s", exc)
    finally:
        _safe_close(conn)


def finalize_run(run_id: str, summary: dict, *, status: str = "completed") -> None:
    conn = connect_to_rds()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            safe_execute(
                cur,
                "UPDATE ai_governance_scan_runs "
                "SET status = %s, summary = %s, completed_at = CURRENT_TIMESTAMP "
                "WHERE run_id = %s",
                (status, json.dumps(summary, default=str), run_id),
            )
        conn.commit()
    except Exception as exc:
        logger.warning("scan_results_store: finalize_run failed: %s", exc)
    finally:
        _safe_close(conn)


# ── Read path ───────────────────────────────────────────────────────────────────


def _decode_run(row: dict) -> dict:
    for col in ("modes", "summary"):
        val = row.get(col)
        if isinstance(val, str):
            try:
                row[col] = json.loads(val)
            except (ValueError, TypeError):
                row[col] = None
    for col in ("created_at", "completed_at"):
        if row.get(col) is not None and hasattr(row[col], "isoformat"):
            row[col] = row[col].isoformat()
    return row


def get_run(run_id: str) -> dict | None:
    conn = connect_to_rds()
    if conn is None:
        return None
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM ai_governance_scan_runs WHERE run_id = %s", (run_id,)
            )
            row = cur.fetchone()
        conn.commit()
        return _decode_run(row) if row else None
    finally:
        _safe_close(conn)


def list_runs(limit: int = 50) -> list[dict]:
    conn = connect_to_rds()
    if conn is None:
        return []
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM ai_governance_scan_runs "
                "ORDER BY created_at DESC LIMIT %s",
                (int(limit),),
            )
            rows = cur.fetchall()
        conn.commit()
        return [_decode_run(r) for r in rows]
    finally:
        _safe_close(conn)


def list_user_results(
    run_id: str,
    *,
    org_admin_id: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    conn = connect_to_rds()
    if conn is None:
        return []
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            if org_admin_id:
                cur.execute(
                    "SELECT * FROM ai_governance_scan_user_results "
                    "WHERE run_id = %s AND org_admin_id = %s "
                    "ORDER BY created_at ASC LIMIT %s OFFSET %s",
                    (run_id, org_admin_id, int(limit), int(offset)),
                )
            else:
                cur.execute(
                    "SELECT * FROM ai_governance_scan_user_results "
                    "WHERE run_id = %s ORDER BY created_at ASC LIMIT %s OFFSET %s",
                    (run_id, int(limit), int(offset)),
                )
            rows = cur.fetchall()
        conn.commit()
        out = []
        for r in rows:
            if isinstance(r.get("result"), str):
                try:
                    r["result"] = json.loads(r["result"])
                except (ValueError, TypeError):
                    r["result"] = None
            if r.get("created_at") is not None and hasattr(r["created_at"], "isoformat"):
                r["created_at"] = r["created_at"].isoformat()
            out.append(r)
        return out
    finally:
        _safe_close(conn)
