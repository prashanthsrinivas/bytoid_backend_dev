from flask import g, jsonify, request, session
from functools import wraps
import asyncio
import inspect
import json
import pymysql
from db.rds_db import connect_to_rds


def _get_user_id_from_context():
    """Extract user_id from g.user_id, session, or request body/args (session middleware fallback)."""
    user_id = getattr(g, "user_id", None)
    if not user_id:
        user_id = session.get("user_id")
    if not user_id and request.is_json:
        user_id = (request.get_json(silent=True) or {}).get("user_id")
    if not user_id:
        user_id = request.args.get("user_id")
    if not user_id:
        user_id = request.form.get("user_id")
    return user_id


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


def permission_required(required_permission):
    """
    Decorator for URL-parameterized routes: @permission_required("permission.name")
    Expects owner_user_id as a URL kwarg: /resource/<owner_user_id>/...
    Checks current user's permission against the target owner's role.
    """

    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            user_id = _get_user_id_from_context()
            if not user_id:
                return jsonify({"error": "Unauthorized"}), 401

            conn = connect_to_rds()
            try:
                with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                    cursor.execute(
                        """
                        SELECT user_id, user_type, launch_id_fk
                        FROM users
                        WHERE user_id=%s
                    """,
                        (user_id,),
                    )
                    user = cursor.fetchone()

                    if not user:
                        return jsonify({"error": "User not found"}), 404

                    org_id = user["launch_id_fk"]

                    # ADMIN PATH
                    if user["user_type"] == "admin":
                        owner_user_id = _get_owner_user_id_from_context(kwargs)

                        # Self-access: allow
                        if not owner_user_id or owner_user_id == user_id:
                            return f(*args, **kwargs)

                        # Fetch target owner
                        cursor.execute(
                            """
                            SELECT user_type, launch_id_fk, email
                            FROM users
                            WHERE user_id=%s
                        """,
                            (owner_user_id,),
                        )
                        owner = cursor.fetchone()

                        if not owner or owner["launch_id_fk"] != org_id:
                            return jsonify({"error": "Cross-org access denied"}), 403

                        # Target is normal user: allow
                        if owner["user_type"] == "user":
                            return f(*args, **kwargs)

                        # Target is admin: require special_access delegation
                        if owner["user_type"] == "admin":
                            cursor.execute(
                                """
                                SELECT 1 FROM special_access
                                WHERE grantor_admin_id=%s AND target_admin_id=%s
                            """,
                                (owner_user_id, user_id),
                            )

                            if not cursor.fetchone():
                                return (
                                    jsonify({"error": "Admin access restricted"}),
                                    403,
                                )

                            g.acting_on_behalf_of_user_id = owner_user_id
                            g.acting_on_behalf_of_email = owner.get("email")
                            return f(*args, **kwargs)

                    # NORMAL USER PATH
                    # Normal users have their role stored in their own permissions JSON
                    cursor.execute(
                        """
                        SELECT permissions
                        FROM users
                        WHERE user_id=%s
                    """,
                        (user_id,),
                    )
                    user_row = cursor.fetchone()

                    if not user_row:
                        return jsonify({"error": "User not found"}), 404

                    permissions = json.loads(user_row["permissions"] or "{}")
                    role = permissions.get("role", {})

                    if not role or permissions.get("status") != "active":
                        return jsonify({"error": "No active role assigned"}), 403

                    if required_permission not in role.get("permissions", []):
                        return jsonify({"error": "Permission denied"}), 403

                    return f(*args, **kwargs)

            finally:
                conn.close()

        return wrapper

    return decorator


def permission_required_body(required_permission):
    """
    Decorator for request-body-parameterized routes: @permission_required_body("permission.name")
    Reads owner_user_id from request.json.get("user_id") or request.args.get("user_id").
    Checks current user's permission against the target owner's role.

    Usage:
        @bp.route("/tracker/create", methods=["POST"])
        @permission_required_body("trackers.table.create")
        def create_tracker():
            data = request.get_json()
            user_id = data.get("user_id")  # This is owner_user_id
    """

    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            user_id = _get_user_id_from_context()
            if not user_id:
                return jsonify({"error": "Unauthorized"}), 401

            # Extract owner_user_id from request context
            owner_user_id = None
            if request.is_json:
                owner_user_id = request.get_json(silent=True).get("user_id")
            if not owner_user_id:
                owner_user_id = request.form.get("user_id")
            if not owner_user_id:
                owner_user_id = request.args.get("user_id")
            if not owner_user_id:
                owner_user_id = kwargs.get("user_id")

            # API-key-based identity resolution for KB/scrape/audio routes
            if not owner_user_id:
                api_key = None
                if request.is_json:
                    api_key = (request.get_json(silent=True) or {}).get("api_key")
                if not api_key:
                    api_key = request.form.get("api_key")
                if not api_key:
                    api_key = request.args.get("api_key")
                if api_key:
                    conn_api = connect_to_rds()
                    try:
                        with conn_api.cursor(pymysql.cursors.DictCursor) as cur:
                            cur.execute(
                                "SELECT user_id FROM users WHERE api_key=%s", (api_key,)
                            )
                            row = cur.fetchone()
                            if row:
                                owner_user_id = row["user_id"]
                    finally:
                        conn_api.close()

            # Check delegation context as fallback
            if not owner_user_id:
                owner_user_id = session.get("active_workspace_id")

            if not owner_user_id:
                owner_user_id = user_id  # Default to self-access for GET requests without explicit workspace

            conn = connect_to_rds()
            try:
                with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                    cursor.execute(
                        """
                        SELECT user_id, user_type, launch_id_fk
                        FROM users
                        WHERE user_id=%s
                    """,
                        (user_id,),
                    )
                    user = cursor.fetchone()

                    if not user:
                        return jsonify({"error": "User not found"}), 404

                    org_id = user["launch_id_fk"]

                    # ADMIN PATH
                    if user["user_type"] == "admin":
                        # Self-access: allow
                        if owner_user_id == user_id:
                            return f(*args, **kwargs)

                        # Fetch target owner
                        cursor.execute(
                            """
                            SELECT user_type, launch_id_fk, email
                            FROM users
                            WHERE user_id=%s
                        """,
                            (owner_user_id,),
                        )
                        owner = cursor.fetchone()

                        if not owner or owner["launch_id_fk"] != org_id:
                            return jsonify({"error": "Cross-org access denied"}), 403

                        # Target is normal user: allow (admins manage their users)
                        if owner["user_type"] == "user":
                            return f(*args, **kwargs)

                        # Target is admin: require special_access delegation
                        if owner["user_type"] == "admin":
                            cursor.execute(
                                """
                                SELECT 1 FROM special_access
                                WHERE grantor_admin_id=%s AND target_admin_id=%s
                            """,
                                (owner_user_id, user_id),
                            )

                            if not cursor.fetchone():
                                return (
                                    jsonify({"error": "Admin access restricted"}),
                                    403,
                                )

                            g.acting_on_behalf_of_user_id = owner_user_id
                            g.acting_on_behalf_of_email = owner.get("email")
                            return f(*args, **kwargs)

                    # NORMAL USER PATH
                    # Verify the user owns/has access to the owner_user_id workspace
                    # Normal users can only operate within their owner's workspace
                    cursor.execute(
                        """
                        SELECT permissions
                        FROM users
                        WHERE user_id=%s
                    """,
                        (user_id,),
                    )
                    user_row = cursor.fetchone()

                    if not user_row:
                        return jsonify({"error": "User not found"}), 404

                    permissions = json.loads(user_row["permissions"] or "{}")
                    role = permissions.get("role", {})

                    if not role or permissions.get("status") != "active":
                        return jsonify({"error": "No active role assigned"}), 403

                    if required_permission not in role.get("permissions", []):
                        return jsonify({"error": "Permission denied"}), 403

                    return f(*args, **kwargs)

            finally:
                conn.close()

        return wrapper

    return decorator
