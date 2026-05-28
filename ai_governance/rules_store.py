"""DB access layer for AI Governance guardrail rules and violations.

Rules are scoped per ``org_admin_id`` and read on every LLM call, so an
in-process TTL cache sits in front of the read path.  Mutations bust the
cache for the affected org.
"""

import json
import logging
import threading
import time
import uuid

import pymysql.cursors

from db.db_checkers import safe_execute
from db.rds_db import connect_to_rds

logger = logging.getLogger(__name__)

_CACHE_TTL = 60.0  # seconds — rules change rarely; enforcer hits this on every LLM call
_cache: dict = {}  # {org_admin_id: (rules_list, expires_at)}
_cache_lock = threading.Lock()


def _safe_close(conn) -> None:
    if conn is None:
        return
    try:
        conn.close()
    except Exception:
        logger.debug("rules_store: connection close suppressed", exc_info=True)


def _row_to_rule(row: dict) -> dict:
    """Decode JSON columns and coerce booleans."""
    config = row.get("config")
    scope = row.get("scope")
    return {
        "rule_id": row["rule_id"],
        "org_admin_id": row.get("org_admin_id"),
        "name": row.get("name"),
        "description": row.get("description"),
        "rule_type": row.get("rule_type"),
        "config": json.loads(config) if isinstance(config, str) else (config or {}),
        "applies_to": row.get("applies_to", "both"),
        "action": row.get("action", "audit"),
        "scope": json.loads(scope) if isinstance(scope, str) else (scope or {}),
        "enabled": bool(row.get("enabled", 1)),
        "created_by": row.get("created_by"),
        "created_at": row.get("created_at").isoformat()
        if row.get("created_at")
        else None,
        "updated_at": row.get("updated_at").isoformat()
        if row.get("updated_at")
        else None,
    }


def _invalidate(org_admin_id: str) -> None:
    with _cache_lock:
        _cache.pop(org_admin_id, None)


def invalidate_all() -> None:
    """Drop the entire rule cache (used in tests / hot-reload)."""
    with _cache_lock:
        _cache.clear()


# ── Read path ─────────────────────────────────────────────────────────────────


def list_rules_cached(org_admin_id: str) -> list[dict]:
    """Enabled rules for an org, cached for ``_CACHE_TTL`` seconds.

    Returns ``[]`` if the DB is unreachable so the enforcer fails open.
    """
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(org_admin_id)
        if entry and entry[1] > now:
            return entry[0]

    rules = _fetch_enabled_rules(org_admin_id)

    with _cache_lock:
        _cache[org_admin_id] = (rules, now + _CACHE_TTL)
    return rules


def _fetch_enabled_rules(org_admin_id: str) -> list[dict]:
    conn = connect_to_rds()
    if conn is None:
        return []
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM ai_guardrail_rules "
                "WHERE org_admin_id = %s AND enabled = 1 "
                "ORDER BY created_at ASC",
                (org_admin_id,),
            )
            rows = cur.fetchall()
        conn.commit()
        return [_row_to_rule(r) for r in rows]
    except Exception as exc:
        logger.warning("rules_store: fetch_enabled_rules failed: %s", exc)
        return []
    finally:
        _safe_close(conn)


def list_rules(org_admin_id: str, include_disabled: bool = True) -> list[dict]:
    """Full rule list for the CRUD UI (bypasses cache)."""
    conn = connect_to_rds()
    if conn is None:
        return []
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            if include_disabled:
                cur.execute(
                    "SELECT * FROM ai_guardrail_rules "
                    "WHERE org_admin_id = %s ORDER BY updated_at DESC",
                    (org_admin_id,),
                )
            else:
                cur.execute(
                    "SELECT * FROM ai_guardrail_rules "
                    "WHERE org_admin_id = %s AND enabled = 1 "
                    "ORDER BY updated_at DESC",
                    (org_admin_id,),
                )
            rows = cur.fetchall()
        conn.commit()
        return [_row_to_rule(r) for r in rows]
    finally:
        _safe_close(conn)


def get_rule(org_admin_id: str, rule_id: str) -> dict | None:
    conn = connect_to_rds()
    if conn is None:
        return None
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM ai_guardrail_rules "
                "WHERE rule_id = %s AND org_admin_id = %s",
                (rule_id, org_admin_id),
            )
            row = cur.fetchone()
        conn.commit()
        return _row_to_rule(row) if row else None
    finally:
        _safe_close(conn)


# ── Write path ────────────────────────────────────────────────────────────────

_VALID_TYPES = {
    "blocked_phrase",
    "regex",
    "pii",
    "topic",
    "max_tokens",
    "model_allowlist",
}
_VALID_APPLIES = {"input", "output", "both"}
_VALID_ACTIONS = {"block", "redact", "warn", "audit"}


def _validate(payload: dict) -> str | None:
    if payload.get("rule_type") not in _VALID_TYPES:
        return f"rule_type must be one of {sorted(_VALID_TYPES)}"
    if payload.get("applies_to", "both") not in _VALID_APPLIES:
        return f"applies_to must be one of {sorted(_VALID_APPLIES)}"
    if payload.get("action", "audit") not in _VALID_ACTIONS:
        return f"action must be one of {sorted(_VALID_ACTIONS)}"
    if not payload.get("name"):
        return "name is required"
    if not isinstance(payload.get("config"), dict):
        return "config must be an object"
    return None


def create_rule(org_admin_id: str, payload: dict, created_by: str | None = None) -> dict:
    err = _validate(payload)
    if err:
        raise ValueError(err)

    rule_id = str(uuid.uuid4())
    conn = connect_to_rds()
    try:
        with conn.cursor() as cur:
            safe_execute(
                cur,
                """
                INSERT INTO ai_guardrail_rules
                    (rule_id, org_admin_id, name, description, rule_type,
                     config, applies_to, action, scope, enabled, created_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    rule_id,
                    org_admin_id,
                    payload["name"],
                    payload.get("description"),
                    payload["rule_type"],
                    json.dumps(payload.get("config") or {}),
                    payload.get("applies_to", "both"),
                    payload.get("action", "audit"),
                    json.dumps(payload.get("scope") or {}),
                    1 if payload.get("enabled", True) else 0,
                    created_by,
                ),
            )
        conn.commit()
    finally:
        _safe_close(conn)

    _invalidate(org_admin_id)
    return get_rule(org_admin_id, rule_id) or {"rule_id": rule_id}


_UPDATABLE = {
    "name",
    "description",
    "rule_type",
    "config",
    "applies_to",
    "action",
    "scope",
    "enabled",
}


def update_rule(org_admin_id: str, rule_id: str, payload: dict) -> dict | None:
    existing = get_rule(org_admin_id, rule_id)
    if not existing:
        return None

    merged = {**existing, **{k: v for k, v in payload.items() if k in _UPDATABLE}}
    err = _validate(merged)
    if err:
        raise ValueError(err)

    fields, values = [], []
    for key in _UPDATABLE:
        if key not in payload:
            continue
        if key in ("config", "scope"):
            fields.append(f"{key} = %s")
            values.append(json.dumps(payload[key] or {}))
        elif key == "enabled":
            fields.append("enabled = %s")
            values.append(1 if payload[key] else 0)
        else:
            fields.append(f"{key} = %s")
            values.append(payload[key])

    if not fields:
        return existing

    values.extend([rule_id, org_admin_id])
    # fields come from _UPDATABLE (closed allowlist), values via %s — safe from injection.
    set_clause = ", ".join(fields)
    sql = f"UPDATE ai_guardrail_rules SET {set_clause} WHERE rule_id = %s AND org_admin_id = %s"  # noqa: S608
    conn = connect_to_rds()
    try:
        with conn.cursor() as cur:
            safe_execute(cur, sql, tuple(values))
        conn.commit()
    finally:
        _safe_close(conn)

    _invalidate(org_admin_id)
    return get_rule(org_admin_id, rule_id)


def delete_rule(org_admin_id: str, rule_id: str) -> bool:
    conn = connect_to_rds()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM ai_guardrail_rules "
                "WHERE rule_id = %s AND org_admin_id = %s",
                (rule_id, org_admin_id),
            )
            deleted = cur.rowcount > 0
        conn.commit()
    finally:
        _safe_close(conn)

    if deleted:
        _invalidate(org_admin_id)
    return deleted


# ── Violations ────────────────────────────────────────────────────────────────


def record_violation(violation: dict) -> str:
    """Insert a violation row. Returns the new violation_id.

    Never raises — guardrail enforcement must continue even if logging fails.
    """
    violation_id = str(uuid.uuid4())
    try:
        conn = connect_to_rds()
        with conn.cursor() as cur:
            safe_execute(
                cur,
                """
                INSERT INTO ai_guardrail_violations
                    (violation_id, rule_id, rule_name, org_admin_id, user_id,
                     feature, model, direction, action_taken, excerpt,
                     trace_id, request_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    violation_id,
                    violation.get("rule_id"),
                    violation.get("rule_name"),
                    violation.get("org_admin_id"),
                    violation.get("user_id"),
                    violation.get("feature"),
                    violation.get("model"),
                    violation.get("direction"),
                    violation.get("action_taken"),
                    (violation.get("excerpt") or "")[:2000],
                    violation.get("trace_id"),
                    violation.get("request_id"),
                ),
            )
        conn.commit()
        _safe_close(conn)
    except Exception as exc:
        logger.warning("rules_store: record_violation failed: %s", exc)
    return violation_id


def list_violations(
    org_admin_id: str,
    limit: int = 50,
    offset: int = 0,
    feature: str | None = None,
    rule_id: str | None = None,
) -> list[dict]:
    conn = connect_to_rds()
    if conn is None:
        return []
    try:
        clauses = ["org_admin_id = %s"]
        params: list = [org_admin_id]
        if feature:
            clauses.append("feature = %s")
            params.append(feature)
        if rule_id:
            clauses.append("rule_id = %s")
            params.append(rule_id)
        params.extend([int(limit), int(offset)])
        # clauses built from an allowlist (org_admin_id/feature/rule_id), values via %s.
        where = " AND ".join(clauses)
        sql = f"SELECT * FROM ai_guardrail_violations WHERE {where} ORDER BY created_at DESC LIMIT %s OFFSET %s"  # noqa: S608

        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
        conn.commit()
        for r in rows:
            if r.get("created_at"):
                r["created_at"] = r["created_at"].isoformat()
        return list(rows)
    finally:
        _safe_close(conn)
