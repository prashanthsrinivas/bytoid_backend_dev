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
                return jsonify({"error": "Unauthorized - session missing"}), 401
            if not hasattr(g, "user_id"):
                return jsonify({"error": "Session not initialized"}), 401
            conn = connect_to_rds()

            try:
                with conn.cursor(pymysql.cursors.DictCursor) as cursor:

                    # 1. Get user
                    cursor.execute("""
                        SELECT user_id, user_type, launch_id_fk
                        FROM users
                        WHERE user_id=%s
                    """, (user_id,))
                    user = cursor.fetchone()

                    if not user:
                        return jsonify({"error": "User not found"}), 404

                    # ✅ ADMIN FULL ACCESS (org-wide)
                    if user["user_type"] == "admin":
                        return f(*args, **kwargs)

                    org_id = user["launch_id_fk"]

                    # 2. Get role from shared_users
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

                    # 3. Get roles from admin
                    cursor.execute("""
                        SELECT roles_creation
                        FROM users
                        WHERE user_id=%s
                    """, (owner_user_id,))

                    row = cursor.fetchone()
                    if not row:
                        return jsonify({"error": "Role config not found"}), 403

                    try:
                        roles = json.loads(row["roles_creation"] or "[]")
                    except Exception:
                        roles = []

                    role = next((r for r in roles if r["id"] == role_id), None)

                    if not role:
                        return jsonify({"error": "Role not found"}), 403

                    role_permissions = role.get("permissions", [])

                    # 4. Check permission
                    if required_permission not in role_permissions:
                        return jsonify({"error": "Permission denied"}), 403

                return f(*args, **kwargs)

            finally:
                conn.close()

        return wrapper
    return decorator