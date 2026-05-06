from flask import g, jsonify
from functools import wraps
import json
import pymysql
from db.rds_db import connect_to_rds


def permission_required(required_permission):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):

            user_id = getattr(g, "user_id", None)
            if not user_id:
                return jsonify({"error": "Unauthorized"}), 401

            conn = connect_to_rds()

            try:
                with conn.cursor(pymysql.cursors.DictCursor) as cursor:

                    # 1. Get current user
                    cursor.execute("""
                        SELECT user_id, user_type, launch_id_fk
                        FROM users
                        WHERE user_id=%s
                    """, (user_id,))
                    user = cursor.fetchone()

                    if not user:
                        return jsonify({"error": "User not found"}), 404

                    org_id = user["launch_id_fk"]

                    #  ADMIN LOGIC (IMPORTANT FIX)
                    if user["user_type"] == "admin":

                        owner_user_id = kwargs.get("owner_user_id")

                        # self access
                        if not owner_user_id or owner_user_id == user_id:
                            return f(*args, **kwargs)

                        # check same org
                        cursor.execute("""
                            SELECT user_type, launch_id_fk 
                            FROM users 
                            WHERE user_id=%s
                        """, (owner_user_id,))
                        owner = cursor.fetchone()

                        if not owner or owner["launch_id_fk"] != org_id:
                            return jsonify({"error": "Cross-org access denied"}), 403

                        # ✅ CASE 1: target is NORMAL USER → allow
                        if owner["user_type"] == "user":
                            return f(*args, **kwargs)

                        # ✅ CASE 2: target is ADMIN → require special_access
                        if owner["user_type"] == "admin":
                            cursor.execute("""
                                SELECT 1 FROM special_access
                                WHERE grantor_admin_id=%s AND target_admin_id=%s
                            """, (owner_user_id, user_id))

                            access = cursor.fetchone()

                            if not access:
                                return jsonify({"error": "Admin access restricted"}), 403

                            # Tag request context with cross-admin context (for audit logging)
                            g.acting_on_behalf_of_user_id = owner_user_id
                            return f(*args, **kwargs)

                        # check same org (old logic - kept as you asked)
                        cursor.execute("""
                            SELECT launch_id_fk FROM users WHERE user_id=%s
                        """, (owner_user_id,))
                        owner = cursor.fetchone()

                        if not owner or owner["launch_id_fk"] != org_id:
                            return jsonify({"error": "Cross-org access denied"}), 403

                        # check special access (old logic - kept)
                        cursor.execute("""
                            SELECT 1 FROM special_access
                            WHERE grantor_admin_id=%s AND target_admin_id=%s
                        """, (owner_user_id, user_id))

                        access = cursor.fetchone()

                        if not access:
                            return jsonify({"error": "Admin access restricted"}), 403

                        return f(*args, **kwargs)

                    #  NORMAL USER FLOW

                    cursor.execute("""
                        SELECT role_id, owner_user_id
                        FROM shared_users
                        WHERE shared_user_id=%s
                        AND status='active'
                        AND owner_user_id IN (
                            SELECT user_id FROM users WHERE launch_id_fk=%s
                        )
                    """, (user_id, org_id))

                    shared = cursor.fetchone()

                    if not shared:
                        return jsonify({"error": "No role assigned"}), 403

                    role_id = shared["role_id"]
                    owner_user_id = shared["owner_user_id"]

                    # get roles
                    cursor.execute("""
                        SELECT roles_creation
                        FROM users
                        WHERE user_id=%s
                    """, (owner_user_id,))

                    row = cursor.fetchone()

                    roles = json.loads(row["roles_creation"] or "[]") if row else []

                    role = next((r for r in roles if r["id"] == role_id), None)

                    if not role:
                        return jsonify({"error": "Role not found"}), 403

                    if required_permission not in role.get("permissions", []):
                        return jsonify({"error": "Permission denied"}), 403

                    return f(*args, **kwargs)

            finally:
                conn.close()

        return wrapper
    return decorator