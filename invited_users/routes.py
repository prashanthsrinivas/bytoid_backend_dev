from time import time
from flask import Blueprint, request, jsonify
import pymysql
from gmail_route.gmail_service import GmailService
import uuid
from db.rds_db import connect_to_rds
import json
from datetime import datetime
from invited_users.uszr_helper import (
    create_invited_user,
    dehashed_url,
    generate_hashed_url,
)
from utils.base_logger import get_logger

inv_users_bp = Blueprint("invited_users", __name__)

logger = get_logger(__name__)


import pymysql, json

# BASE ROLES APIS FOR AMIN


@inv_users_bp.route("/admin/roles-add", methods=["POST"])
def add_role_admin():
    """Create a new role for a user"""
    data = request.get_json()
    userid = data.get("userid")
    name = data.get("name")
    permissions = data.get("permissions", [])

    if not userid or not name or not permissions:
        return (
            jsonify({"error": "Missing required fields: userid, name, permissions"}),
            400,
        )

    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            # fetch current roles
            cursor.execute(
                "SELECT roles_creation FROM users WHERE user_id=%s", (userid,)
            )
            row = cursor.fetchone()
            roles = (
                json.loads(row["roles_creation"])
                if row and row["roles_creation"]
                else []
            )

            # create new role with uuid
            new_role = {
                "id": str(uuid.uuid4()),
                "name": name,
                "permissions": permissions,
            }
            roles.append(new_role)

            # update db
            cursor.execute(
                "UPDATE users SET roles_creation=%s WHERE user_id=%s",
                (json.dumps(roles), userid),
            )
            conn.commit()

        conn.close()
        return jsonify({"message": "Role added successfully", "role": roles}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@inv_users_bp.route("/admin/roles-get/<userid>", methods=["GET"])
def get_roles(userid):
    """Get all roles and invited users for a user"""
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                "SELECT roles_creation, permissions FROM users WHERE user_id=%s",
                (userid,),
            )
            row = cursor.fetchone()

        conn.close()

        if not row:
            return jsonify({"error": "User not found"}), 404

        roles = json.loads(row["roles_creation"]) if row.get("roles_creation") else []
        permissions = json.loads(row["permissions"]) if row.get("permissions") else []

        return jsonify({"roles": roles, "invited_users": permissions}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@inv_users_bp.route("/admin/roles-update", methods=["POST"])
def update_role():
    """Update role by role_id"""
    data = request.get_json()
    userid = data.get("userid")
    role_id = data.get("role_id")
    name = data.get("name")
    permissions = data.get("permissions")

    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                "SELECT roles_creation FROM users WHERE user_id=%s", (userid,)
            )
            row = cursor.fetchone()
            roles = (
                json.loads(row["roles_creation"])
                if row and row["roles_creation"]
                else []
            )

            updated = False
            for role in roles:
                if role["id"] == role_id:
                    if name:
                        role["name"] = name
                    if permissions is not None:
                        role["permissions"] = permissions
                    updated = True
                    break

            if not updated:
                return jsonify({"error": "Role not found"}), 404

            cursor.execute(
                "UPDATE users SET roles_creation=%s WHERE user_id=%s",
                (json.dumps(roles), userid),
            )
            conn.commit()
        conn.close()
        return jsonify({"message": "Role updated successfully"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@inv_users_bp.route("/admin/roles-delete/<userid>/<role_id>", methods=["DELETE"])
def delete_role(userid, role_id):
    """Delete role by role_id"""
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                "SELECT roles_creation FROM users WHERE user_id=%s", (userid,)
            )
            row = cursor.fetchone()
            roles = (
                json.loads(row["roles_creation"])
                if row and row["roles_creation"]
                else []
            )

            new_roles = [role for role in roles if role["id"] != role_id]

            if len(new_roles) == len(roles):
                return jsonify({"error": "Role not found"}), 404

            cursor.execute(
                "UPDATE users SET roles_creation=%s WHERE user_id=%s",
                (json.dumps(new_roles), userid),
            )
            conn.commit()
        conn.close()
        return jsonify({"message": "Role deleted successfully"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# INVITE USER APIS


@inv_users_bp.route("/admin/invite_user", methods=["POST"])
def send_invite_user():
    data = request.get_json()
    print("invite data", data)
    userid = data.get("userid")
    email = data.get("email")
    role_id = data.get("role_id")

    if not userid or not email or not role_id:
        return (
            jsonify({"error": "Missing required fields: userid, email, role_id"}),
            400,
        )

    conn = None
    try:
        conn = connect_to_rds()
        conn.begin()  # start transaction

        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            # fetch user details
            cursor.execute(
                "SELECT email, roles_creation, permissions FROM users WHERE user_id=%s FOR UPDATE",
                (userid,),
            )
            row = cursor.fetchone()
            if not row:
                conn.rollback()
                return jsonify({"error": "User not found"}), 404

            user_email = row["email"]

            # load roles
            roles = json.loads(row["roles_creation"]) if row["roles_creation"] else []
            role = next((r for r in roles if r["id"] == role_id), None)
            if not role:
                conn.rollback()
                return jsonify({"error": "Role not found"}), 404

            # load existing permissions
            permissions = json.loads(row["permissions"]) if row["permissions"] else []
            if not isinstance(permissions, dict):
                # if it was stored as a list earlier, migrate to dict
                permissions = {"invites": permissions}

            # Ensure "invites" key exists
            if "invites" not in permissions:
                permissions["invites"] = []

            # Check if email already invited
            if any(
                perm["email"].lower() == email.lower()
                for perm in permissions["invites"]
            ):
                conn.rollback()
                return jsonify({"error": "User already invited"}), 400

            # Create new permission entry
            new_permission = {
                "email": email,
                "role": role,
                "invited_by": user_email,
                "status": "pending",
                "created_at": datetime.utcnow().isoformat(),
            }

            # Append inside "invites"
            permissions["invites"].append(new_permission)

            # update DB
            cursor.execute(
                "UPDATE users SET permissions=%s WHERE user_id=%s",
                (json.dumps(permissions), userid),
            )

            # fetch business info (not critical for rollback, but include before commit)
            cursor.execute(
                """
                SELECT BusinessID, BusinessName, Age, Sex, LineOfBusiness, BusinessImage, businessLocation
                FROM business_info WHERE user_id_fk=%s
                """,
                (userid,),
            )
            business_info = cursor.fetchone() or {}

        # generate invite link
        base_invitation_link = generate_hashed_url(
            base_url="https://www.bytoid.ai/invite",
            invited_to=email,
            invited_by=user_email,
        )

        # send email *after* updating DB, but still inside try
        gmail_service = GmailService(user_id=userid)
        gmail_service.send_invite_mail(
            inviter=user_email,
            invitee=email,
            role=role,
            invite_link=base_invitation_link,
            business_info=business_info,
        )

        # if everything succeeds -> commit
        conn.commit()
        return jsonify({"message": "Invitation sent successfully"}), 200

    except Exception as e:
        if conn:
            conn.rollback()  # undo DB changes
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@inv_users_bp.route("/admin/delete-invite", methods=["DELETE"])
def delete_invite():
    """Delete an invited user from permissions by email"""
    try:
        data = request.get_json()
        userid = data.get("user_id")
        invited_email = data.get("email")

        if not invited_email:
            return jsonify({"error": "Invited user email is required"}), 400

        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute("SELECT permissions FROM users WHERE user_id=%s", (userid,))
            row = cursor.fetchone()

            if not row:
                conn.close()
                return jsonify({"error": "User not found"}), 404

            permissions = (
                json.loads(row["permissions"])
                if row["permissions"]
                else {"invites": []}
            )
            if "invites" not in permissions:
                permissions["invites"] = []

            original_count = len(permissions["invites"])
            permissions["invites"] = [
                p
                for p in permissions["invites"]
                if p.get("email", "").lower() != invited_email.lower()
            ]

            if len(permissions["invites"]) == original_count:
                conn.close()
                return jsonify({"error": "Invitation not found for this email"}), 404

            cursor.execute(
                "UPDATE users SET permissions=%s WHERE user_id=%s",
                (json.dumps(permissions), userid),
            )
            conn.commit()

        conn.close()
        return (
            jsonify(
                {"message": f"Invitation for {invited_email} deleted successfully"}
            ),
            200,
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@inv_users_bp.route("/admin/resend-invite", methods=["POST"])
def resend_invite():
    """Resend invite link to an already invited user"""
    try:
        data = request.get_json()
        user_id = data.get("user_id")
        invited_email = data.get("email")

        if not user_id or not invited_email:
            return jsonify({"error": "user_id and invited email are required"}), 400

        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                "SELECT permissions, email, roles_creation FROM users WHERE user_id=%s",
                (user_id,),
            )
            row = cursor.fetchone()
            if not row:
                return jsonify({"error": "User not found"}), 404

            permissions = (
                json.loads(row["permissions"])
                if row["permissions"]
                else {"invites": []}
            )
            if "invites" not in permissions:
                permissions["invites"] = []

            inviter_email = row.get("email")

            invited_user = next(
                (
                    p
                    for p in permissions["invites"]
                    if p.get("email", "").lower() == invited_email.lower()
                ),
                None,
            )
            if not invited_user:
                return jsonify({"error": "No invite found for this email"}), 404
            if invited_user["status"].lower() == "completed":
                return jsonify({"error": "User has already accepted the invite"}), 400

            # fetch business info
            cursor.execute(
                """
                SELECT BusinessID, BusinessName, Age, Sex, LineOfBusiness, BusinessImage, businessLocation
                FROM business_info WHERE user_id_fk=%s
                """,
                (user_id,),
            )
            business_info = cursor.fetchone() or {}

            invite_link = generate_hashed_url(
                base_url="https://www.bytoid.ai/invite",
                invited_to=invited_email,
                invited_by=inviter_email,
            )

            gmail_service = GmailService(user_id=user_id)
            gmail_service.send_invite_mail(
                inviter=inviter_email,
                invitee=invited_email,
                role=invited_user.get("role", "Member"),
                invite_link=invite_link,
                business_info=business_info,
            )

            # update "last sent" timestamp
            invited_user["created_at"] = datetime.utcnow().isoformat()
            cursor.execute(
                "UPDATE users SET permissions=%s WHERE user_id=%s",
                (json.dumps(permissions), user_id),
            )
            conn.commit()

        return (
            jsonify(
                {
                    "message": f"Invitation resent to {invited_email}",
                    "invite_link": invite_link,
                }
            ),
            200,
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        if conn:
            conn.close()


# SHARED USER ROLES APIS

@inv_users_bp.route("/admin/validate_invite/token=<token>", methods=["GET"])
def validate_invite(token):
    if not token:
        return jsonify({"error": "Token is required"}), 400

    try:
        invited_by, invited_to, expiry = dehashed_url(token)
        print(invited_by, invited_to, expiry)

        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            # 1️⃣ Fetch inviter
            cursor.execute(
                "SELECT permissions, launch_id_fk FROM users WHERE email = %s",
                (invited_by,),
            )
            inviter = cursor.fetchone()
            if not inviter:
                return jsonify({"error": "Inviting user not found"}), 404

            permissions = (
                json.loads(inviter["permissions"])
                if inviter["permissions"]
                else {"invites": [], "shared": []}
            )
            if "invites" not in permissions:
                permissions["invites"] = []
            if "shared" not in permissions:
                permissions["shared"] = []

            launch_id_fk = inviter["launch_id_fk"]

            # 2️⃣ Find invitation entry
            permission_entry = next(
                (
                    p
                    for p in permissions["invites"]
                    if p["email"].lower() == invited_to.lower()
                ),
                None,
            )
            if not permission_entry:
                return jsonify({"error": "No invitation found for this email"}), 404

            # 3️⃣ Expiry check
            if int(time()) > expiry:
                permission_entry["status"] = "expired"
                cursor.execute(
                    "UPDATE users SET permissions = %s WHERE email = %s",
                    (json.dumps(permissions), invited_by),
                )
                conn.commit()
                conn.close()
                return jsonify({"status": "expired", "error": "Token has expired"}), 400

            # 4️⃣ Already used?
            if permission_entry["status"].lower() != "pending":
                return jsonify(
                    {"error": f"Invitation already {permission_entry['status']}"}, 400
                )

            # Mark completed
            permission_entry["status"] = "completed"

            # 5️⃣ Check if invited user already exists
            cursor.execute("SELECT user_id FROM users WHERE email = %s", (invited_to,))
            existing = cursor.fetchone()
            if existing:
                return jsonify({"error": "The account already exists"}), 400

            # 6️⃣ Create invited user
            user_created = create_invited_user(
                email=invited_to,
                connection=conn,
                permission=json.dumps(permission_entry),
                launch_id_fk=launch_id_fk,
            )
            if not user_created:
                return jsonify({"error": "Failed to create invited user"}), 500

            # 7️⃣ Move invite → shared
            permissions["invites"] = [
                p
                for p in permissions["invites"]
                if p["email"].lower() != invited_to.lower()
            ]
            permissions["shared"].append(
                {
                    "email": invited_to,
                    "role": permission_entry["role"],
                    "invited_by": permission_entry["invited_by"],
                    "status": "active",
                    "accepted_at": datetime.utcnow().isoformat(),
                }
            )

            # 8️⃣ Update inviter permissions
            cursor.execute(
                "UPDATE users SET permissions = %s WHERE email = %s",
                (json.dumps(permissions), invited_by),
            )
            conn.commit()

        conn.close()
        return (
            jsonify({"message": "Invitation accepted and user created successfully"}),
            200,
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# @inv_users_bp.route("/admin/edit_shared_user_role", methods=["POST"])
# def edit_shared_user_role():
#     data = request.get_json()
#     user_id = data.get("user_id")
#     email = data.get("email")
#     role = data.get("role_id")
#     try:
#         conn = connect_to_rds()
#         conn.start()
#         with conn.cursor(pymysql.cursors.DictCursor) as cursor:
#             cursor.execute(
#                 "SELECT email,roles_creation,permissions FROM users where user_id = %s"(
#                     user_id,
#                 ),
#             )
#             admin_Das = cursor.fetchone()
#             if not admin_Das:
#                 conn.rollback()
#                 return jsonify({"error": "User not found"}), 404

#     except Exception as e:
#         return jsonify({"error": f"base{e}"})
#     pass
