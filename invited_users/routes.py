from time import time
from flask import Blueprint, request, jsonify, g
import pymysql
from services.gmail_service import GmailService
import uuid
from db.rds_db import connect_to_rds
from utils.permission_required import permission_required
import json
from datetime import datetime
from gmail_route.routes import delete_all_user_data
from utils.permissions_map import PERMISSIONS
from invited_users.uszr_helper import (
    create_invited_user,
    dehashed_url,
    generate_hashed_url,
)
from utils.base_logger import get_logger
from dotenv import load_dotenv
import os

inv_users_bp = Blueprint("invited_users", __name__)

logger = get_logger(__name__)
load_dotenv()

# BASE ROLES APIS FOR AMIN

def safe_json_load(data, default=None):
   try:
       return json.loads(data) if data else default
   except Exception as e:
       logger.error(f"JSON parse error: {e}")
       return default
   
def is_admin(user_id, conn):
    with conn.cursor(pymysql.cursors.DictCursor) as cursor:
        cursor.execute(
            "SELECT user_type FROM users WHERE user_id=%s",
            (user_id,)
        )
        row = cursor.fetchone()
        return row and row["user_type"] == "admin"

def get_org_id(user_id, conn):
   with conn.cursor(pymysql.cursors.DictCursor) as cursor:
       cursor.execute(
           "SELECT launch_id_fk FROM users WHERE user_id=%s",
           (user_id,)
       )
       row = cursor.fetchone()
       return row["launch_id_fk"] if row else None

@inv_users_bp.route("/admin/roles-get", methods=["GET"])
@permission_required("admin.manage_users")
def get_roles():
    conn = None
    try:
        user_id = getattr(g, "user_id", None)

        if not user_id:
            return jsonify({"error": "Unauthorized"}), 401

        conn = connect_to_rds()
        org_id = get_org_id(user_id, conn)

        if not org_id:
            return jsonify({"error": "Org not found"}), 400

        with conn.cursor(pymysql.cursors.DictCursor) as cursor:

            # ✅ GET ANY ONE ADMIN FROM ORG SAFELY
            cursor.execute("""
                SELECT roles_creation 
                FROM users 
                WHERE launch_id_fk=%s AND user_type='admin'
                LIMIT 1
            """, (org_id,))
            
            row = cursor.fetchone()

            roles = []
            if row and row.get("roles_creation"):
                try:
                    roles = json.loads(row["roles_creation"])
                except Exception:
                    roles = []

            # ✅ INVITES
            cursor.execute("""
                SELECT invited_to AS email, role_id, status, created_at
                FROM invites
                WHERE invited_by IN (
                    SELECT user_id FROM users WHERE launch_id_fk=%s
                )
            """, (org_id,))
            invites = cursor.fetchall() or []

            # ✅ SHARED USERS
            cursor.execute("""
                SELECT email, role_id, status, accepted_at
                FROM shared_users
                WHERE owner_user_id IN (
                    SELECT user_id FROM users WHERE launch_id_fk=%s
                )
            """, (org_id,))
            shared_users = cursor.fetchall() or []

        return jsonify({
            "roles": roles,
            "invites": invites,
            "shared_users": shared_users
        }), 200

    except Exception as e:
        logger.exception("ERROR in roles-get")
        return jsonify({"error": str(e)}), 500

    finally:
        if conn:
            conn.close()
           

@inv_users_bp.route("/admin/roles-delete/<role_id>", methods=["DELETE"])
@permission_required("admin.manage_users")
def delete_role(role_id):
    user_id = g.user_id
    conn = connect_to_rds()

    try:
        conn.begin()
        org_id = get_org_id(user_id, conn)
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                "SELECT roles_creation FROM users WHERE launch_id_fk=%s AND user_type='admin'ORDER BY created_at ASC LIMIT 1",
                (org_id,)
            )
            row = cursor.fetchone()
            roles = safe_json_load(row["roles_creation"], []) if row else []
            if not any(r["id"] == role_id for r in roles):
                conn.rollback()
                return jsonify({"error": "Role not found"}), 404
            # org-safe check
            cursor.execute("""
                SELECT id FROM invites
                WHERE role_id=%s
                AND invited_by IN (
                    SELECT user_id FROM users WHERE launch_id_fk=%s
                )
                AND status IN ('pending','active')
            """, (role_id, org_id))
            if cursor.fetchone():
                conn.rollback()
                return jsonify({"error": "Role used in invites"}), 400
            cursor.execute("""
                SELECT id FROM shared_users
                WHERE role_id=%s
                AND owner_user_id IN (
                    SELECT user_id FROM users WHERE launch_id_fk=%s
                )
                AND status='active'
            """, (role_id, org_id))
            if cursor.fetchone():
                conn.rollback()
                return jsonify({"error": "Role used by users"}), 400
            new_roles = [r for r in roles if r["id"] != role_id]
            cursor.execute(
                """
                UPDATE users 
                SET roles_creation=%s 
                WHERE launch_id_fk=%s AND user_type='admin'
                """,
                (json.dumps(new_roles), org_id)
            )
        conn.commit()
        return jsonify({"message": "Role deleted"}), 200

    except Exception:
        conn.rollback()
        logger.exception("delete_role error")
        return jsonify({"error": "Internal server error"}), 500

    finally:
        if conn:
            conn.close()

@inv_users_bp.route("/user/permissions", methods=["GET"])
def get_user_permissions():
    user_id = g.user_id
    conn = connect_to_rds()

    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:

            # get user
            cursor.execute("""
                SELECT user_type, launch_id_fk
                FROM users
                WHERE user_id=%s
            """, (user_id,))
            user = cursor.fetchone()

            if not user:
                return jsonify({"error": "User not found"}), 404

            # admin → full access
            if user["user_type"] == "admin":
                return jsonify({
                    "user_type": "admin",
                    "permissions": PERMISSIONS.get("admin", [])
                }), 200

            org_id = user["launch_id_fk"]

            # get role
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
                return jsonify({"permissions": []}), 200

            # get role config
            cursor.execute("""
                SELECT roles_creation
                FROM users
                WHERE user_id=%s
            """, (shared["owner_user_id"],))

            row = cursor.fetchone()

            if not row:
                return jsonify({"permissions": []}), 200

            roles = json.loads(row["roles_creation"] or "[]")
            role = next((r for r in roles if r["id"] == shared["role_id"]), None)

            if not role:
                return jsonify({"permissions": []}), 200

            return jsonify({
                "user_type": "user",
                "permissions": role.get("permissions", [])
            }), 200

    finally:
        conn.close()
 


@inv_users_bp.route("/admin/invite_user", methods=["POST"])
@permission_required("admin.invite_user")
def send_invite_user():

    user_id = g.user_id
    data = request.get_json() or {}
    email = data.get("email", "").strip().lower()
    role_id = data.get("role_id")
    if not email or not role_id:
        return jsonify({"error": "email & role_id required"}), 400
    conn = None
    try:
        conn = connect_to_rds()
        conn.begin()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                "SELECT email, roles_creation, social FROM users WHERE user_id=%s",
                (user_id,)
            )
            row = cursor.fetchone()
            if not row:
                conn.rollback()
                return jsonify({"error": "User not found"}), 404
            # duplicate checks
            cursor.execute(
                "SELECT user_id FROM users WHERE email=%s",
                (email,)
            )
            if cursor.fetchone():
                conn.rollback()
                return jsonify({"error": "User exists"}), 400
            cursor.execute("""
                SELECT id FROM invites
                WHERE invited_to=%s AND invited_by=%s
                AND status IN ('pending','active')
            """, (email, user_id))
            if cursor.fetchone():
                conn.rollback()
                return jsonify({"error": "Already invited"}), 400
            invite_id = str(uuid.uuid4())
            roles = safe_json_load(row["roles_creation"], [])
            if not isinstance(roles, list):
                roles = []
            if not any(r["id"] == role_id for r in roles):
                conn.rollback()
                return jsonify({"error": "Invalid role"}), 400
            cursor.execute("""
                INSERT INTO invites
                (id, invited_by, invited_to, role_id, status, created_at)
                VALUES (%s,%s,%s,%s,%s,%s)
            """, (
                invite_id,
                user_id,
                email,
                role_id,
                "pending",
                datetime.utcnow()
            ))
            role = next((r for r in roles if r["id"] == role_id), None)
        invite_link = generate_hashed_url(
            base_url=f"{os.getenv('BASE_FRNT_URL')}/invite",
            invited_to=email,
            invited_by=row["email"],
        )
        conn.commit()

        if row["social"] == "google":
            GmailService(user_id=user_id).send_invite_mail(
                recipient_emails=email,
                role=role,
                invite_link=invite_link,
                business_info={}
            )
        return jsonify({"message": "Invite sent"}), 200

    except Exception:
        conn.rollback()
        logger.exception("invite error")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        if conn:
            conn.close()
 

@inv_users_bp.route("/admin/delete-invite", methods=["DELETE"])
@permission_required("admin.invite_user")
def delete_invite():
    conn = None
    """Delete an invited user from permissions by email"""
    try:
        data = request.get_json() or {}
        user_id = g.user_id
        invited_email = (data.get("email") or "").strip().lower()

        if not invited_email:
            return jsonify({"error": "Invited user email is required"}), 400

        conn = connect_to_rds()
        conn.begin()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            org_id = get_org_id(user_id, conn)
            cursor.execute("""
                DELETE FROM invites
                WHERE invited_to=%s
                AND invited_by IN (
                    SELECT user_id FROM users WHERE launch_id_fk=%s
                )
        """, (invited_email, org_id))
            if cursor.rowcount == 0:
                conn.rollback()
                return jsonify({"error": "Invited not"}), 404
            conn.commit()
            return jsonify({"message": "Invite deleted successfully"}), 200
    except Exception as e:
        logger.exception("Error in delete_invite")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        if conn:
            conn.close()


@inv_users_bp.route("/admin/resend-invite", methods=["POST"])
@permission_required("admin.invite_user")
def resend_invite():
   conn = None
   try:
       data = request.get_json() or {}
       user_id = g.user_id
       invited_email = (data.get("email") or "").strip().lower()
       if not invited_email:
           return jsonify({"error": "Email is required"}), 400
       conn = connect_to_rds()
       conn.begin()
       with conn.cursor(pymysql.cursors.DictCursor) as cursor:
           # 🔹 Get invite
            org_id = get_org_id(user_id, conn)
            cursor.execute("""
                SELECT * FROM invites
                WHERE invited_to=%s
                AND invited_by IN (
                    SELECT user_id FROM users WHERE launch_id_fk=%s
                )
            """, (invited_email, org_id))
        
            invite = cursor.fetchone()
            if not invite:
               conn.rollback()
               return jsonify({"error": "No invite found"}), 404
            if invite["status"] == "active":
               conn.rollback()
               return jsonify({"error": "User already accepted"}), 400
           # 🔹 Reset invite
            cursor.execute(
               """
               UPDATE invites
               SET status='pending', created_at=%s
               WHERE id=%s
               """,
               (datetime.utcnow(), invite["id"]),
           )
           # 🔹 Get inviter email
            cursor.execute(
               "SELECT email, social FROM users WHERE user_id=%s",
               (user_id,),
           )
            user = cursor.fetchone()
           # 🔹 Generate link
            invite_link = generate_hashed_url(
               base_url=f"{os.getenv('BASE_FRNT_URL')}/invite",
               invited_to=invited_email,
               invited_by=user["email"],
           )
            conn.commit()
           # 🔹 Send mail
            if user["social"] == "google":
               GmailService(user_id=user_id).send_invite_mail(
                   recipient_emails=invited_email,
                   role={"id": invite["role_id"]},
                   invite_link=invite_link,
                   business_info={},
               )
       return jsonify({
           "message": "Invite resent successfully",
           "invite_link": invite_link
       }), 200
   except Exception:
       if conn:
           conn.rollback()
       logger.exception("Error in resend_invite")
       return jsonify({"error": "Internal server error"}), 500
   finally:
       if conn:
           conn.close()


@inv_users_bp.route("/admin/validate_invite/token=<token>", methods=["GET"])
def validate_invite(token):

   if not token:
       return jsonify({"error": "Token required"}), 400

   conn = connect_to_rds()

   try:
       invited_by, invited_to, expiry = dehashed_url(token)

       if not invited_by or not invited_to or not expiry:
           return jsonify({"error": "Invalid token"}), 400

       if int(time()) > expiry:
           return jsonify({"error": "Expired"}), 400

       conn.begin()

       with conn.cursor(pymysql.cursors.DictCursor) as cursor:

           # inviter check
           cursor.execute(
               "SELECT user_id, launch_id_fk FROM users WHERE email=%s",
               (invited_by,)
           )
           inviter = cursor.fetchone()
           if not inviter:
               conn.rollback()
               return jsonify({"error": "Inviter not found"}), 404

           # invite check
           cursor.execute("""
              SELECT * FROM invites 
              WHERE invited_to=%s 
              AND invited_by=%s 
              AND status='pending'
            """, (invited_to, inviter["user_id"]))
            
           
           invite = cursor.fetchone()
           if not invite:
               conn.rollback()
               return jsonify({"error": "No valid invite"}), 404

           # activate
           cursor.execute(
               "UPDATE invites SET status='active' WHERE id=%s",
               (invite["id"],)
           )

           # create user
           new_user_id = create_invited_user(
               email=invited_to,
               connection=conn,
               permission=json.dumps({"role_id": invite["role_id"]}),
               launch_id_fk=inviter["launch_id_fk"]
           )

           if not new_user_id:
               conn.rollback()
               return jsonify({"error": "User creation failed"}), 500

           cursor.execute("""
               INSERT INTO shared_users
               (owner_user_id, shared_user_id, email, role_id, status, accepted_at)
               VALUES (%s,%s,%s,%s,%s,%s)
           """, (
               inviter["user_id"],
               new_user_id,
               invited_to,
               invite["role_id"],
               "active",
               datetime.utcnow()
           ))

       conn.commit()
       return jsonify({"message": "Invite accepted"}), 200

   except Exception:
       conn.rollback()
       logger.exception("validate error")
       return jsonify({"error": "Internal server error"}), 500

   finally:
       conn.close()
 


@inv_users_bp.route("/admin/edit_shared_user_role", methods=["POST"])
@permission_required("admin.edit_user")
def edit_shared_user_role():
   data = request.get_json() or {}
   user_id = g.user_id
   role_id = data.get("role_id")
   conn = None
   email = (data.get("email") or "").strip().lower()
   if not role_id or not email:
       return jsonify({"error": "role_id and email required"}), 400
   try:
       conn = connect_to_rds()
       conn.begin()
       with conn.cursor(pymysql.cursors.DictCursor) as cursor:
           org_id = get_org_id(user_id, conn)
           # 🔹 Validate role exists
           cursor.execute(
               "SELECT roles_creation FROM users WHERE launch_id_fk=%s AND user_type='admin' ORDER BY created_at ASC LIMIT 1",
               (org_id,)
           )
           row = cursor.fetchone()
           roles = safe_json_load(row["roles_creation"], []) or []
           if not isinstance(roles, list):
               roles = []
           if not any(r["id"] == role_id for r in roles):
               conn.rollback()
               return jsonify({"error": "Invalid role"}), 400
           # 🔹 Update shared user
           cursor.execute("""
                UPDATE shared_users
                SET role_id=%s
                WHERE email=%s
                AND owner_user_id IN (
                    SELECT user_id FROM users WHERE launch_id_fk=%s
                )
            """, (role_id, email, org_id))
           if cursor.rowcount == 0:
               conn.rollback()
               return jsonify({"error": "Shared user not found"}), 404
       conn.commit()
       return jsonify({"message": "Role updated successfully"}), 200
   except Exception:
       conn.rollback()
       logger.exception("Error in edit_shared_user_role")
       return jsonify({"error": "Internal server error"}), 500
   finally:
       if conn:
            conn.close()
 

@inv_users_bp.route("/admin/revoke_shared_user_role", methods=["POST"])
@permission_required("admin.edit_user")
def revoke_shared_user_role():
   data = request.get_json() or {}
   user_id = g.user_id
   email = (data.get("email") or "").strip().lower()
   conn = None
   try:
       conn = connect_to_rds()
       conn.begin()
       with conn.cursor() as cursor:
            org_id = get_org_id(user_id, conn)
            cursor.execute("""
                UPDATE shared_users
                SET status='revoked'
                WHERE email=%s
                AND owner_user_id IN (
                    SELECT user_id FROM users WHERE launch_id_fk=%s
                )
            """, (email, org_id))

            if cursor.rowcount == 0:
               conn.rollback()
               return jsonify({"error": "User not found"}), 404
       conn.commit()
       return jsonify({"message": "User revoked"}), 200
   except Exception:
       conn.rollback()
       logger.exception("Error in revoke_shared_user_role")
       return jsonify({"error": "Internal server error"}), 500
   finally:
       if conn:
            conn.close()


@inv_users_bp.route("/admin/delete_shared_user_role", methods=["POST"])
@permission_required("admin.delete_user")
def delete_shared_user_role():
    data = request.get_json() or {}
    user_id = g.user_id
    email = (data.get("email") or "").strip().lower()
    conn = None
    try:
        conn = connect_to_rds()
        conn.begin()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            org_id = get_org_id(user_id, conn)
            cursor.execute("""
                SELECT shared_user_id FROM shared_users
                WHERE email=%s
                AND owner_user_id IN (
                    SELECT user_id FROM users WHERE launch_id_fk=%s
                )
            """, (email, org_id))
            row = cursor.fetchone()
            if not row:
                conn.rollback()
                return jsonify({"error": "User not found"}), 404
            shared_user_id = row["shared_user_id"]
            cursor.execute("""
                DELETE FROM shared_users
                WHERE email=%s
                AND owner_user_id IN (
                    SELECT user_id FROM users WHERE launch_id_fk=%s
                )
            """, (email, org_id))
        conn.commit()
        # delete actual user
        delete_all_user_data(shared_user_id)
        return jsonify({"message": "User deleted"}), 200
    except Exception:        
        conn.rollback()
        logger.exception("Error in delete_shared_user_role")
        return jsonify({"error": "Internal server error"}), 500

    finally:
        if conn:
            conn.close()
 

@inv_users_bp.route("/admin/activate_shared_user_role", methods=["POST"])
@permission_required("admin.edit_user")
def activate_shared_user_role():
    data = request.get_json() or {}
    user_id = g.user_id
    email = (data.get("email") or "").strip().lower()
    conn = None

    try:
        conn = connect_to_rds()
        conn.begin()
        with conn.cursor() as cursor:
            org_id = get_org_id(user_id, conn)
            cursor.execute("""
                UPDATE shared_users
                SET status='active'
                WHERE email=%s
                AND owner_user_id IN (
                    SELECT user_id FROM users WHERE launch_id_fk=%s
                )
            """, (email, org_id))
            if cursor.rowcount == 0:
                conn.rollback()
                return jsonify({"error": "User not found"}), 404
        conn.commit()
        return jsonify({"message": "User activated"}), 200
    except Exception:
        conn.rollback()
        logger.exception("Error in activate_shared_user_role")
        return jsonify({"error": "Internal server error"}), 500

    finally:
        if conn:
            conn.close()
 
