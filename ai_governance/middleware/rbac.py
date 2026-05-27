"""
RBAC middleware for the AI Governance module.

Two tiers:
  "guardrails" — any admin user OR service@bytoid.ca
  "superuser"  — ONLY service@bytoid.ca (all other admins are explicitly denied)

User identity is resolved via the same five-source chain used by
permission_required_body: g.user_id → session → URL params → JSON body → query.

The DB lookup is cached in two layers to avoid hammering the database:
  Layer 1: Flask's g (per-request, free for repeated decorator calls)
  Layer 2: In-process TTL dict (30 s, per Gunicorn worker — up to N workers
           may each cache independently, which is fine and expected)
"""

import inspect
import threading
import time
from functools import wraps

import pymysql.cursors
from flask import g, jsonify, request, session

from services.audit_log_service import (
    AI_GOVERNANCE_ACCESS_DENIED,
    log_audit_event,
)
from utils.app_configs import FRAMEWORK_OWNER

# ── In-process TTL cache (Layer 2) ───────────────────────────────────────────

_USER_ROW_CACHE: dict = {}  # {user_id: (row_or_None, expires_at)}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 30.0  # seconds


# ── Identity resolution ───────────────────────────────────────────────────────


def _resolve_user_id() -> str | None:
    """Return the authenticated user_id using the same fallback chain as
    permission_required_body, without importing that module (keeps rbac.py
    independently mutable by mutmut)."""
    uid = getattr(g, "user_id", None)
    if uid:
        return str(uid)
    try:
        uid = session.get("user_id")
    except RuntimeError:
        uid = None
    if uid:
        return str(uid)
    uid = request.view_args.get("user_id") if request.view_args else None
    if uid:
        return str(uid)
    if request.is_json:
        body = request.get_json(silent=True) or {}
        uid = body.get("user_id") or body.get("userid")
    if not uid:
        uid = request.args.get("user_id")
    return str(uid) if uid else None


def _raw_db_query_user_row(user_id: str) -> dict | None:
    """Execute the SELECT against the DB — patchable independently in tests."""
    try:
        from db.rds_db import connect_to_rds

        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT user_type, email FROM users WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        conn.close()
        return row
    except Exception:
        return None


def _db_fetch_user_row(user_id: str) -> dict | None:
    """TTL-cache wrapper around _raw_db_query_user_row (Layer 2)."""
    now = time.monotonic()
    with _CACHE_LOCK:
        entry = _USER_ROW_CACHE.get(user_id)
        if entry is not None and entry[1] > now:
            return entry[0]

    row = _raw_db_query_user_row(user_id)

    with _CACHE_LOCK:
        _USER_ROW_CACHE[user_id] = (row, now + _CACHE_TTL)
    return row


def _fetch_user_row(user_id: str) -> dict | None:
    """Fetch user row with two-layer caching:
    Layer 1 — Flask g (per-request, zero cost for repeated calls in one request).
    Layer 2 — In-process TTL dict (avoids repeated DB calls across requests).
    """
    cached = getattr(g, "_ai_gov_user_row", None)
    if cached is not None:
        return cached
    row = _db_fetch_user_row(user_id)
    try:
        g._ai_gov_user_row = row
    except RuntimeError:
        pass  # Outside request context (e.g., Celery tasks / tests)
    return row


# ── Core access check ─────────────────────────────────────────────────────────


def _check_access(tier: str) -> tuple | None:
    """Return a (response, status_code) error tuple if access is denied,
    or None if access is granted.  Called by both sync and async wrappers."""
    uid = _resolve_user_id()
    if not uid:
        return jsonify({"error": "Unauthorized"}), 401

    row = _fetch_user_row(uid)
    if not row:
        return jsonify({"error": "User not found"}), 404

    email = row.get("email", "")
    user_type = row.get("user_type", "")

    if tier == "superuser":
        if email != FRAMEWORK_OWNER:
            log_audit_event(
                AI_GOVERNANCE_ACCESS_DENIED,
                endpoint=request.path,
                ip=request.remote_addr,
                status="denied",
                actor_user_id=uid,
                actor_email=email,
            )
            return jsonify({"error": "Access restricted to service account"}), 403
        return None

    # tier == "guardrails"
    if email == FRAMEWORK_OWNER or user_type == "admin":
        return None

    log_audit_event(
        AI_GOVERNANCE_ACCESS_DENIED,
        endpoint=request.path,
        ip=request.remote_addr,
        status="denied",
        actor_user_id=uid,
        actor_email=email,
    )
    return jsonify({"error": "Admin access required"}), 403


# ── Public decorator ──────────────────────────────────────────────────────────


def ai_governance_required(tier: str = "guardrails"):
    """Decorator enforcing AI Governance RBAC.

    Args:
        tier: "guardrails" (admin or service account) or
              "superuser" (service@bytoid.ca only).
    """

    def decorator(f):
        if inspect.iscoroutinefunction(f):
            @wraps(f)
            async def async_wrapper(*args, **kwargs):
                err = _check_access(tier)
                if err is not None:
                    return err
                return await f(*args, **kwargs)

            return async_wrapper

        @wraps(f)
        def sync_wrapper(*args, **kwargs):
            err = _check_access(tier)
            if err is not None:
                return err
            return f(*args, **kwargs)

        return sync_wrapper

    return decorator
