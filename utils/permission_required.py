from flask import g, jsonify, request, session
from functools import wraps
import logging
import inspect
import json
import pymysql
from utils.app_configs import IS_DEV
from db.rds_db import connect_to_rds
from utils.normal import parse_composite_user_id
from utils.permission_resolver import resolve_permissions

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG if IS_DEV else logging.INFO)


def _get_user_id_from_context():
    """
    Extract user_id from:
    - g
    - session
    - route params
    - JSON body
    - query params
    - form data

    Supports both:
    - user_id
    - userid

    NOTE: kept for backward compatibility. Authorization no longer relies on
    this (it conflated the *acting* identity with the *requested* one, which is
    what enabled cross-user access). Use `_get_acting_user_id` for the trusted
    caller identity and `_get_requested_user_id` for the workspace being
    accessed.
    """

    def pick(source):
        if not source:
            return None
        return source.get("user_id") or source.get("userid")

    # g
    user_id = getattr(g, "user_id", None) or getattr(g, "userid", None)

    # session
    if not user_id:
        user_id = pick(session)

    # route params
    if not user_id and request.view_args:
        user_id = pick(request.view_args)

    # json body
    if not user_id and request.is_json:
        user_id = pick(request.get_json(silent=True) or {})

    # query params
    if not user_id:
        user_id = pick(request.args)

    # form data
    if not user_id:
        user_id = pick(request.form)

    logger.info("found user_id=%s", user_id)

    return user_id


def _get_requested_user_id():
    """
    Extract the user_id supplied by the REQUEST ITSELF — route params, JSON
    body, query string, or form data. Deliberately ignores `g` and `session`.

    This is the workspace/owner the caller is asking to operate on. It is
    untrusted: the caller's real identity comes from the session, never from
    this value. The two are reconciled in `_evaluate_access`.
    """

    def pick(source):
        if not source:
            return None
        return source.get("user_id") or source.get("userid")

    val = None
    if request.view_args:
        val = pick(request.view_args)
    if not val and request.is_json:
        val = pick(request.get_json(silent=True) or {})
    if not val:
        val = pick(request.args)
    if not val:
        val = pick(request.form)
    return val


def _get_acting_user_id():
    """The authenticated caller's identity.

    Trusts the session first (set by login/2FA), falling back to `g`. Returns
    None when there is no session — callers must treat that as unauthenticated.
    """
    return (
        getattr(g, "user_id", None)
        or getattr(g, "userid", None)
        or session.get("user_id")
    )


def _get_owner_user_id_from_context(kwargs=None):
    """
    Extract owner_user_id (the workspace admin being accessed).
    Priority:
    1. session["active_workspace_id"] (delegation context)
    2. kwargs.get("owner_user_id") (URL param)
    3. None
    """
    # Check delegation context first
    active_workspace_id = session.get("active_workspace_id")
    if active_workspace_id:
        return active_workspace_id

    # Check URL kwargs
    if kwargs:
        return kwargs.get("owner_user_id")

    return None


def _actor_has_share_with(actor_id, owner_id):
    """True when `owner_id` has shared at least one resource with `actor_id`.

    This is the gate that lets a normal user reach another user's workspace:
    without an active share, cross-owner access is denied. Imported lazily so
    the (S3-backed) sharing module isn't pulled in for self-access requests.
    """
    if not actor_id or not owner_id:
        return False
    try:
        from shared_configuration import get_user_shared_reports

        shared = get_user_shared_reports(actor_id) or {}
        for entry in shared.values():
            if isinstance(entry, dict) and entry.get("mainuser_id") == owner_id:
                return True
        return False
    except Exception:
        logger.warning(
            "share lookup failed for actor=%s owner=%s",
            actor_id,
            owner_id,
            exc_info=True,
        )
        return False


def _evaluate_access(required_permission):
    """Shared authorization core for both decorators.

    Returns None when access is allowed, or a ``(response, status)`` tuple to
    deny.

    Identity rules:
      - The ACTING user is the authenticated session identity. The
        request-supplied id is only trusted as the actor when there is no
        session (legacy unauthenticated flows + unit tests) — this means a
        logged-in caller can never claim to be someone else by editing the URL
        or body.
      - The OWNER (whose workspace is being accessed) comes from the request
        (URL/body/query). A composite ``<actor>##SU##<owner>`` carries the owner
        on its right side; a plain id means self-access.

    The split is what closes the cross-user (IDOR) hole: route handlers scope
    their data by the *requested* id, so the decorator must validate the
    *session* actor against that same owner — not against a self-referential
    value.
    """
    requested = _get_requested_user_id()
    _, req_owner = parse_composite_user_id(requested)

    # The caller's identity is the authenticated session — NEVER the
    # request-supplied id. A request-supplied id is only ever the target/owner.
    # This is what enforces authentication: a caller who has not completed login
    # (e.g. password ok but 2FA still pending) has no session user_id, so even
    # though the frontend keeps sending user_id in the URL/body, the request is
    # rejected here instead of being treated as that user.
    actor_id = _get_acting_user_id()
    if not actor_id:
        return jsonify({"error": "Unauthorized"}), 401

    # 2FA gate: a session that authenticated by password but has not yet
    # completed TOTP must not reach any protected resource. Returned as 403
    # (authenticated-but-forbidden), NOT 401 — the caller IS logged in, they
    # just haven't finished 2FA. A 401 makes the frontend treat the session as
    # dead and hard-redirect to the login URL (which is hardcoded to prod),
    # bouncing the user off the 2FA page. 403 keeps them on /totp-verify while
    # still blocking every protected route until /totp_verify clears the flag.
    if session.get("totp_pending"):
        return (
            jsonify(
                {
                    "error": "Two-factor authentication required",
                    "redirect": "/totp-verify",
                    "totp_required": True,
                }
            ),
            403,
        )

    owner_id = req_owner or actor_id

    conn = connect_to_rds()
    if conn is None:
        # No DB connection (e.g. pool exhausted) — fail closed with a clean,
        # transient error instead of crashing on `conn.cursor()` / `conn.close()`
        # (which would surface as an uncaught 500).
        logger.error("permission check: no DB connection available")
        return jsonify({"error": "Service temporarily unavailable"}), 503
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                """
                SELECT user_id, user_type, launch_id_fk
                FROM users
                WHERE user_id=%s
                """,
                (actor_id,),
            )
            user = cursor.fetchone()

            if not user:
                return jsonify({"error": "User not found"}), 404

            actor_org = user.get("launch_id_fk")

            # ----------------------------- ADMIN PATH -----------------------------
            if user["user_type"] == "admin":

                # Self-access: an admin operating on their own workspace.
                if not owner_id or owner_id == actor_id:
                    return None

                cursor.execute(
                    """
                    SELECT user_type, launch_id_fk, email
                    FROM users
                    WHERE user_id=%s
                    """,
                    (owner_id,),
                )
                owner = cursor.fetchone()

                if not owner:
                    return jsonify({"error": "Owner not found"}), 404

                # Cross-org access is never allowed (when both orgs are known).
                owner_org = owner.get("launch_id_fk")
                if actor_org and owner_org and actor_org != owner_org:
                    return jsonify({"error": "Cross-org access denied"}), 403

                # Target is a normal user in the same org: allow.
                if owner["user_type"] == "user":
                    return None

                # Target is another admin: require an approved special_access grant.
                if owner["user_type"] == "admin":
                    cursor.execute(
                        """
                        SELECT access_level FROM special_access
                        WHERE grantor_admin_id=%s AND target_admin_id=%s AND status='approved'
                        """,
                        (owner_id, actor_id),
                    )

                    sa_row = cursor.fetchone()
                    if not sa_row:
                        return jsonify({"error": "Admin access restricted"}), 403

                    g.acting_on_behalf_of_user_id = owner_id
                    g.acting_on_behalf_of_email = owner.get("email")
                    g.access_level = sa_row["access_level"]

                    # Viewer delegation is read-only.
                    if g.access_level == "viewer" and request.method in (
                        "POST",
                        "PUT",
                        "PATCH",
                        "DELETE",
                    ):
                        return (
                            jsonify(
                                {"error": "Viewer access cannot modify resources"}
                            ),
                            403,
                        )

                    return None

                return jsonify({"error": "Access denied"}), 403

            # -------------------------- NORMAL USER PATH ---------------------------
            # A normal user may only reach another owner's workspace through an
            # active share — this is the cross-user gate.
            if owner_id != actor_id and not _actor_has_share_with(actor_id, owner_id):
                return (
                    jsonify({"error": "Access to this resource is not shared with you"}),
                    403,
                )

            cursor.execute(
                """
                SELECT permissions
                FROM users
                WHERE user_id=%s
                """,
                (owner_id,),
            )
            user_row = cursor.fetchone()

            if not user_row:
                return jsonify({"error": "User not found"}), 404

            permissions = json.loads(user_row["permissions"] or "{}")
            role = permissions.get("role", {})

            if not role or permissions.get("status") != "active":
                return jsonify({"error": "No active role assigned"}), 403

            effective_perms = resolve_permissions(role.get("permissions", []))
            if required_permission not in effective_perms:
                return jsonify({"error": "Permission denied"}), 403

            return None

    except Exception as exc:
        # Any failure in the authorization check itself (DB error, malformed
        # permissions JSON, …) must fail CLOSED with a clean response — never an
        # uncaught exception that 500s the route before the handler even runs.
        logger.error("permission check failed: %s", exc, exc_info=True)
        return jsonify({"error": "Authorization check failed"}), 500
    finally:
        try:
            conn.close()
        except Exception as close_err:
            logger.debug("permission check: connection close failed: %s", close_err)


def permission_required(required_permission):
    """
    Decorator for URL-parameterized routes: @permission_required("permission.name")
    Validates the authenticated caller's right to access the requested owner.
    """

    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            # CORS preflight is unauthenticated by spec — never auth-gate it.
            if request.method == "OPTIONS":
                return ("", 204)
            denial = _evaluate_access(required_permission)
            if denial is not None:
                return denial
            return f(*args, **kwargs)

        return wrapper

    return decorator


def permission_required_body(required_permission):
    """
    Same authorization rules as `permission_required`, for routes that carry the
    user_id in the request body/query (and supports async handlers).
    """

    def decorator(f):

        if inspect.iscoroutinefunction(f):

            @wraps(f)
            async def async_wrapper(*args, **kwargs):
                # CORS preflight is unauthenticated by spec — never auth-gate it.
                if request.method == "OPTIONS":
                    return ("", 204)
                denial = _evaluate_access(required_permission)
                if denial is not None:
                    return denial
                return await f(*args, **kwargs)

            return async_wrapper

        @wraps(f)
        def sync_wrapper(*args, **kwargs):
            # CORS preflight is unauthenticated by spec — never auth-gate it.
            if request.method == "OPTIONS":
                return ("", 204)
            denial = _evaluate_access(required_permission)
            if denial is not None:
                return denial
            return f(*args, **kwargs)

        return sync_wrapper

    return decorator
