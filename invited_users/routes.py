from time import time
from flask import Blueprint, request, jsonify
import pymysql
from services.gmail_service import GmailService
import uuid
from db.rds_db import connect_to_rds
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

def has_outlook_connected(user_id, cursor):
   cursor.execute("""
       SELECT token FROM users
       WHERE user_id = %s
   """, (user_id,))
   row = cursor.fetchone()
   return bool(row and row.get("token"))

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
                "SELECT roles_creation,user_type FROM users WHERE user_id=%s",
                (userid,),
            )
            row = cursor.fetchone()
            if not row:
                return jsonify({"error": "User not found"}), 404
            if row["user_type"] == "user":
                return jsonify({"error": "unAuthrotized access"}), 404
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
        special_access_status = {}
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                "SELECT roles_creation,user_type, permissions FROM users WHERE user_id=%s",
                (userid,),
            )
            row = cursor.fetchone()

            if not row:
                return jsonify({"error": "User not found"}), 404
            if row["user_type"] == "user":
                return jsonify({"error": "unAuthrotized access"}), 404

            roles = (
                json.loads(row["roles_creation"]) if row.get("roles_creation") else []
            )
            # permissions = (
            #     json.loads(row["permissions"]) if row.get("permissions") else []
            # )

            # # Get shared emails from permissions
            # emails = [
            #     entry["email"]
            #     for entry in permissions.get("shared", [])
            #     if "email" in entry
            # ]
            # Step 1: get org id
            # get org from company_name
            cursor.execute(
                "SELECT company_name FROM users WHERE user_id=%s",
                (userid,),
            )
            org_row = cursor.fetchone()

            org = org_row["company_name"]

            # get all users in same org
            cursor.execute(
                "SELECT user_id, email, user_type FROM users WHERE company_name=%s",
                (org,),
            )
            all_users = cursor.fetchall()

            emails = [] 
            special_access_status = {}

            for user in all_users:
                if user["user_type"] != "admin":
                    continue

                if user["user_id"] == userid:
                    continue

                email = user["email"]

                # check special access (bidirectional)
                cursor.execute("""
                    SELECT 1 FROM special_access
                    WHERE (grantor_admin_id=%s AND target_admin_id=%s)
                    OR (grantor_admin_id=%s AND target_admin_id=%s)
                """, (userid, user["user_id"], user["user_id"], userid))

                access = cursor.fetchone()

                emails.append(email)
                special_access_status[email] = bool(access)
        return (
            jsonify(
                {
                    "roles": roles,
                    "invited_users": emails,
                    "special_access_status": special_access_status,
                }
            ),
            200,
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        if conn:
            conn.close()

@inv_users_bp.route("/admin/permissions", methods=["GET"])
def get_permissions():

    grouped = {}

    category_map = {
        "compliance": "Compliance Engine",
        "trackers": "Trackers",
        "workflow": "Workflow Builder",
        "taskbox": "Taskbox",
        "kb": "Knowledge Base",
        "apps": "My Apps",
        "team": "Team",
        "notes": "Notes",
        "calendar": "Calendar",
        "admin": "Admin",
        "workspace": "Workspace",
        "intake": "Intake Workflow",
    }

    for key, label in PERMISSIONS.items():
        category_key = key.split(".")[0]
        category_name = category_map.get(category_key, category_key)

        if category_name not in grouped:
            grouped[category_name] = []

        grouped[category_name].append({
            "key": key,
            "label": label
        })

    return jsonify(grouped), 200

@inv_users_bp.route("/admin/roles-update", methods=["POST"])
def update_role():
    """Update role by role_id and propagate changes to invites/shared/invited_users"""
    data = request.get_json()
    userid = data.get("userid")
    role_id = data.get("role_id")
    name = data.get("name")
    permissions = data.get("permissions")

    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            # 1. Get roles_creation for owner
            cursor.execute(
                "SELECT roles_creation,user_type, permissions FROM users WHERE user_id=%s",
                (userid,),
            )
            row = cursor.fetchone()
            if not row:
                return jsonify({"error": "User not found"}), 404
            if row["user_type"] == "user":
                return jsonify({"error": "unAuthrotized access"}), 404
            roles = (
                json.loads(row["roles_creation"])
                if row and row["roles_creation"]
                else []
            )
            owner_permissions = (
                json.loads(row["permissions"])
                if row and row["permissions"]
                else {"invites": [], "shared": []}
            )

            # 2. Update role in roles_creation
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

            # 3. Update in owner's permissions.invites/shared
            affected_emails = set()
            for section in ["invites", "shared"]:
                for entry in owner_permissions.get(section, []):
                    if entry["role"]["id"] == role_id:
                        if name:
                            entry["role"]["name"] = name
                        if permissions is not None:
                            entry["role"]["permissions"] = permissions
                        affected_emails.add(entry["email"].lower())

            # Save back to owner
            cursor.execute(
                "UPDATE users SET roles_creation=%s, permissions=%s WHERE user_id=%s",
                (json.dumps(roles), json.dumps(owner_permissions), userid),
            )

            # 4. Update invited_users in all affected emails
            if affected_emails:
                cursor.execute(
                    "SELECT user_id, permissions FROM users WHERE email IN %s",
                    (tuple(affected_emails),),
                )
                invited_rows = cursor.fetchall()

                for invited in invited_rows:
                    invited_permissions = (
                        json.loads(invited["permissions"])
                        if invited and invited["permissions"]
                        else {}
                    )

                    changed = False
                    if (
                        "role" in invited_permissions
                        and invited_permissions["role"]["id"] == role_id
                    ):
                        if name:
                            invited_permissions["role"]["name"] = name
                        if permissions is not None:
                            invited_permissions["role"]["permissions"] = permissions
                        changed = True

                    if changed:
                        cursor.execute(
                            "UPDATE users SET permissions=%s WHERE user_id=%s",
                            (json.dumps(invited_permissions), invited["user_id"]),
                        )

            conn.commit()
        conn.close()
        return jsonify({"message": "Role updated successfully"}), 200
    except Exception as e:
        # print(e)
        return jsonify({"error": str(e)}), 500


@inv_users_bp.route("/admin/roles-delete/<userid>/<role_id>", methods=["DELETE"])
def delete_role(userid, role_id):
    """Delete role by role_id (only if not associated with invites or shared users)"""
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                "SELECT roles_creation, permissions,user_type FROM users WHERE user_id=%s",
                (userid,),
            )
            row = cursor.fetchone()
            if not row:
                return jsonify({"error": "User not found"}), 404

            if row["user_type"] == "user":
                return jsonify({"error": "unAuthrotized access"}), 404

            roles = (
                json.loads(row["roles_creation"])
                if row and row["roles_creation"]
                else []
            )
            permissions = (
                json.loads(row["permissions"]) if row and row["permissions"] else {}
            )

            # check if role exists
            role_exists = any(role["id"] == role_id for role in roles)
            if not role_exists:
                return jsonify({"error": "Role not found"}), 404

            # check in invites
            invites = permissions.get("invites", [])
            for invite in invites:
                if invite.get("role", {}).get("id") == role_id:
                    return (
                        jsonify(
                            {
                                "error": "Role is associated with an invited user",
                                "email": invite.get("email"),
                            }
                        ),
                        400,
                    )

            # check in shared
            shared = permissions.get("shared", [])
            for shared_user in shared:
                if shared_user.get("role", {}).get("id") == role_id:
                    return (
                        jsonify(
                            {
                                "error": "Role is associated with a shared user",
                                "email": shared_user.get("email"),
                            }
                        ),
                        400,
                    )

            # delete role
            new_roles = [role for role in roles if role["id"] != role_id]
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
    # print("invite data", data)
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
                "SELECT user_type FROM users WHERE email=%s",
                (email,),
            )
            base_check = cursor.fetchone()
            if base_check:
                return jsonify({"error": "user already exists"}), 409
            cursor.execute(
                "SELECT email, roles_creation, permissions, social,user_type FROM users WHERE user_id=%s FOR UPDATE",
                (userid,),
            )
            row = cursor.fetchone()
            if not row:
                conn.rollback()
                return jsonify({"error": "User not found"}), 404

            if row["user_type"] == "user":
                return jsonify({"error": "unAuthrotized access"}), 404

            user_email = row["email"]
            user_source = (row.get("social") or "").strip().lower()

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
                base_url=f"{os.getenv('BASE_FRNT_URL')}/invite",
                invited_to=email,
                invited_by=user_email,
            )
            
            if user_source == "google":
                gmail_service = GmailService(user_id=userid)
                gmail_service.send_invite_mail(
                    receipent_emails=email,
                    role=role,
                    invite_link=base_invitation_link,
                    business_info=business_info,
                )
            else:
            # ✅ For BOTH microsoft + saml
                if has_outlook_connected(userid, cursor):
                    from services.outlook_service import OutlookService
                    outlook_service = OutlookService(user_id=userid)
                    outlook_service.send_invitation_email(
                        invitee=email,
                        inviter=user_email,
                        role=role,
                        invite_link=base_invitation_link,
                       business_info=business_info
                    )
                else:
                   conn.rollback()
                   return jsonify({
                       "error": "Outlook not connected. Please connect Outlook first."
                   }), 400
 

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
            cursor.execute(
                "SELECT permissions,user_type FROM users WHERE user_id=%s", (userid,)
            )
            row = cursor.fetchone()

            if not row:
                conn.close()
                return jsonify({"error": "User not found"}), 404
            if row["user_type"] == "user":
                return jsonify({"error": "unAuthrotized access"}), 404

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
    conn = None
    try:
        data = request.get_json()
        user_id = data.get("user_id")
        invited_email = data.get("email")

        if not user_id or not invited_email:
            return jsonify({"error": "user_id and invited email are required"}), 400

        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            # fetch inviter details
            cursor.execute(
                "SELECT user_type FROM users WHERE email=%s",
                (invited_email,),
            )
            base_check = cursor.fetchone()
            if base_check:
                return jsonify({"error": "user already exists"}), 409
            cursor.execute(
                "SELECT permissions, email, roles_creation,user_type, social FROM users WHERE user_id=%s",
                (user_id,),
            )
            row = cursor.fetchone()
            if not row:
                return jsonify({"error": "User not found"}), 404
            if row["user_type"] == "user":
                return jsonify({"error": "unAuthrotized access"}), 404

            permissions = (
                json.loads(row["permissions"])
                if row["permissions"]
                else {"invites": []}
            )
            if "invites" not in permissions:
                permissions["invites"] = []

            inviter_email = row.get("email")

            # locate invited user
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

            # Block if already accepted/active
            if invited_user.get("status", "").lower() in ["completed", "active"]:
                return jsonify({"error": "User has already accepted the invite"}), 400

            # Reset status + created_at
            invited_user["status"] = "pending"
            invited_user["created_at"] = datetime.utcnow().isoformat()

            # fetch business info
            cursor.execute(
                """
                SELECT BusinessID, BusinessName, Age, Sex, LineOfBusiness,
                       BusinessImage, businessLocation
                FROM business_info WHERE user_id_fk=%s
                """,
                (user_id,),
            )
            business_info = cursor.fetchone() or {}

            # generate fresh invite link
            invite_link = generate_hashed_url(
                base_url=f"{os.getenv('BASE_FRNT_URL')}/invite",
                invited_to=invited_email,
                invited_by=inviter_email,
            )

            user_source = (row.get("social") or "").strip().lower()
            # GOOGLE → Gmail
            if user_source == "google":
               gmail_service = GmailService(user_id=user_id)
               gmail_service.send_invite_mail(
                   receipent_emails=invited_email,
                   role=invited_user.get("role", {"name":"Member"}),
                   invite_link=invite_link,
                   business_info=business_info,
                )
            # MICROSOFT + SAML → Outlook (if connected)
            else:
               if has_outlook_connected(user_id, cursor):
                   from services.outlook_service import OutlookService
                   outlook_service = OutlookService(user_id=user_id)
                   outlook_service.send_invitation_email(
                       invitee=invited_email,
                       inviter=inviter_email,
                       role=invited_user.get("role", {"name":"Member"}),
                       invite_link=invite_link,
                       business_info=business_info
                   )
               else:
                   return jsonify({
                       "error": "Outlook not connected. Please connect Outlook first."
               }), 400

            # persist updates back into DB
            cursor.execute(
                "UPDATE users SET permissions=%s WHERE user_id=%s",
                (json.dumps(permissions), user_id),
            )
            conn.commit()

        return (
            jsonify(
                {
                    "message": f"Invitation resent to {invited_email} (status reset to pending)",
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

@inv_users_bp.route("/admin/grant_special_access", methods=["POST"])
def grant_special_access():
    data = request.get_json()

    current_admin_id = data.get("user_id")
    target_admin_id = data.get("target_admin_id")

    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:

            # check current admin
            cursor.execute(
                "SELECT user_type, company_name FROM users WHERE user_id=%s",
                (current_admin_id,),
            )
            current = cursor.fetchone()

            # check target admin
            cursor.execute(
                 "SELECT user_type, company_name FROM users WHERE user_id=%s",
            (target_admin_id,),
            )
            target = cursor.fetchone()

            # check admin
            if current["user_type"] != "admin" or target["user_type"] != "admin":
                return jsonify({"error": "Only admins allowed"}), 403

            # same org check
            if current["company_name"] != target["company_name"]:
                return jsonify({"error": "Different organization"}), 403

            # insert access
            cursor.execute("""
                INSERT IGNORE INTO special_access
                (grantor_admin_id, target_admin_id)
                VALUES (%s, %s)
            """, (current_admin_id, target_admin_id))

            conn.commit()

        return jsonify({"message": "Access granted"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        if conn:
            conn.close()


# SHARED USER ROLES APIS
@inv_users_bp.route("/admin/request_special_access", methods=["POST"])
def request_special_access():
    data = request.get_json()

    requester_id = data.get("user_id")   # Admin A
    target_email = data.get("email")     # Admin B

    conn = None
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:

            # get requester org
            cursor.execute("""
                SELECT company_name FROM users WHERE user_id=%s
            """, (requester_id,))
            requester = cursor.fetchone()

            # get target admin (same org only)
            cursor.execute("""
                SELECT user_id FROM users 
                WHERE email=%s 
                AND user_type='admin'
                AND company_name=%s
            """, (target_email, requester["company_name"]))
            target = cursor.fetchone()

            if not target:
                return jsonify({"error": "Target admin not found"}), 404
            
            cursor.execute("""
                SELECT 1 FROM special_access
                WHERE grantor_admin_id=%s AND target_admin_id=%s
            """, (requester_id, target["user_id"]))

            if cursor.fetchone():
                return jsonify({"error": "Access already exists"}), 400
            if requester_id == target["user_id"]:
                return jsonify({"error": "Cannot request yourself"}), 400
            
            # 🔔 notification for Admin B
            cursor.execute("""
                INSERT INTO notifications (user_id, message)
                VALUES (%s, %s)
            """, (target["user_id"], "Admin requested access to your data"))
            # generate link (reuse your invite system)
            link = generate_hashed_url(
                base_url=f"{os.getenv('BASE_FRNT_URL')}/admin-access",
                invited_to=target_email,
                invited_by=requester_id
            )

            # get user source

            cursor.execute(
                "SELECT social, email FROM users WHERE user_id=%s",
                (requester_id,)
            )
            source_row = cursor.fetchone()
            if not source_row:
                return jsonify({"error": "Requester not found"}), 400
    
            user_source = (source_row.get("social") or "").strip().lower()
            inviter_email = source_row["email"]
            
            print("DEBUG user_source:", user_source)
            # ✅ GET TOKEN + SOCIAL
            # check outlook connection
            if not has_outlook_connected(requester_id, cursor):
                conn.commit()
                return jsonify({
                    "error": "Outlook not connected. Please connect Outlook first."
                }), 400
            from services.outlook_service import OutlookService
            try:
                outlook_service = OutlookService(user_id=requester_id)
                outlook_service.send_invitation_email(
                    invitee=target_email,
                    inviter=inviter_email,
                    role={
                        "name": "Admin Access",
                        "id": "admin_access"
                    },
                    invite_link=link,
                    business_info={}
                )
                conn.commit()
                return jsonify({"message": "Request sent via Outlook"}), 200
            
            except Exception as e:
                logger.error(f"Outlook error: {str(e)}")
                conn.rollback()
                return jsonify({"error": "Failed to send email"}), 500
    finally:
        if conn:
            conn.close()

@inv_users_bp.route("/admin/accept_special_access", methods=["POST"])
def accept_special_access():
    data = request.get_json()

    requester_id = data.get("requester_id")  # Admin A
    target_id = data.get("target_id")        # Admin B

    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:

            # ✅ 1. ADMIN CHECK
            cursor.execute("SELECT user_type, company_name FROM users WHERE user_id=%s", (requester_id,))
            req = cursor.fetchone()

            cursor.execute("SELECT user_type, company_name FROM users WHERE user_id=%s", (target_id,))
            tgt = cursor.fetchone()

            if req["user_type"] != "admin" or tgt["user_type"] != "admin":
                return jsonify({"error": "Only admins allowed"}), 403

            # ✅ 2. SAME ORG CHECK
            if req["company_name"] != tgt["company_name"]:
                return jsonify({"error": "Different organization"}), 403

            # ✅ 3. INSERT ACCESS
            cursor.execute("""
                INSERT IGNORE INTO special_access
                (grantor_admin_id, target_admin_id)
                VALUES (%s, %s)
            """, (target_id, requester_id))

            # ✅ 4. NOTIFICATION
            cursor.execute("""
                INSERT INTO notifications (user_id, message)
                VALUES (%s, %s)
            """, (requester_id, "Your admin access request has been approved"))

            conn.commit()
            return jsonify({"message": "Access granted"}), 200

    finally:
        if conn:
            conn.close()

@inv_users_bp.route("/admin/validate_invite/token=<token>", methods=["GET"])
def validate_invite(token):
    if not token:
        return jsonify({"error": "Token is required"}), 400

    try:
        invited_by, invited_to, expiry = dehashed_url(token)
        # print(invited_by, invited_to, expiry)

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

            # Parse permissions safely
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
                return (
                    jsonify(
                        {"status": "expired", "error": "Invitation token has expired"}
                    ),
                    400,
                )

            # 4️⃣ Already used?
            if permission_entry["status"].lower() != "pending":
                return (
                    jsonify(
                        {"error": f"Invitation already {permission_entry['status']}"}
                    ),
                    400,
                )

            # Mark as active
            permission_entry["status"] = "active"

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
            cursor.execute("SELECT user_id FROM users WHERE email=%s", (invited_by,))
            inviter_row = cursor.fetchone()
            
            if inviter_row:
               cursor.execute("""
                   INSERT INTO notifications (user_id, message)
                   VALUES (%s, %s)
               """, (
                  inviter_row["user_id"],
                  f"{invited_to} accepted your invitation"
               ))
            conn.commit()

        conn.close()
        return (
            jsonify({"message": "Invitation accepted and user created successfully"}),
            200,
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@inv_users_bp.route("/admin/edit_shared_user_role", methods=["POST"])
def edit_shared_user_role():
    data = request.get_json()
    user_id = data.get("user_id")
    email = data.get("email")
    role_id = data.get("role_id")

    try:
        conn = connect_to_rds()
        conn.begin()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            # Step 1: Fetch user_id's roles and permissions
            cursor.execute(
                "SELECT roles_creation, permissions,user_type FROM users WHERE user_id = %s",
                (user_id,),
            )
            admin_row = cursor.fetchone()
            if not admin_row:
                conn.rollback()
                return jsonify({"error": "Admin user not found"}), 404

            if admin_row["user_type"] == "user":
                return jsonify({"error": "unAuthrotized access"}), 404

            roles_creation = json.loads(admin_row["roles_creation"] or "[]")
            permissions = json.loads(admin_row["permissions"] or "{}")

            # Step 2: Find the role from roles_creation
            selected_role = next(
                (r for r in roles_creation if r["id"] == role_id), None
            )
            if not selected_role:
                conn.rollback()
                return jsonify({"error": "Role not found"}), 404

            # Step 3: Update the email user’s permissions
            cursor.execute("SELECT permissions FROM users WHERE email = %s", (email,))
            email_user = cursor.fetchone()
            if not email_user:
                conn.rollback()
                return jsonify({"error": "Email user not found"}), 404

            email_permissions = json.loads(email_user["permissions"] or "{}")

            # overwrite role in email user
            email_permissions["role"] = {
                "id": selected_role["id"],
                "name": selected_role["name"],
                "permissions": selected_role.get("permissions", []),
            }

            cursor.execute(
                "UPDATE users SET permissions=%s WHERE email=%s",
                (json.dumps(email_permissions), email),
            )

            # Step 4: Update the inviter's (user_id) permissions (shared/invites)
            # Ensure structure exists
            if "shared" not in permissions:
                permissions["shared"] = []
            if "invites" not in permissions:
                permissions["invites"] = []

            # Check if already present in shared or invites
            updated = False
            for section in ["shared", "invites"]:
                for p in permissions[section]:
                    if p.get("email") == email:
                        p["role"] = email_permissions["role"]
                        updated = True

            # If not present, add it to shared
            if not updated:
                permissions["shared"].append(
                    {
                        "email": email,
                        "role": email_permissions["role"],
                        "status": "completed",
                        "created_at": datetime.utcnow().isoformat(),
                    }
                )

            # Save back
            cursor.execute(
                "UPDATE users SET permissions=%s WHERE user_id=%s",
                (json.dumps(permissions), user_id),
            )

            conn.commit()

        return jsonify({"message": "Role updated successfully"}), 200

    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        if conn:
            conn.close()

@inv_users_bp.route("/notifications/<user_id>", methods=["GET"])
def get_notifications(user_id):
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:

            cursor.execute("""
                SELECT id, message, is_read, created_at
                FROM notifications
                WHERE user_id=%s
                ORDER BY created_at DESC
            """, (user_id,))

            data = cursor.fetchall()

        return jsonify({"notifications": data}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        if conn:
            conn.close()

@inv_users_bp.route("/admin/revoke_shared_user_role", methods=["POST"])
def revoke_shared_user_role():
    data = request.get_json()
    user_id = data.get("user_id")  # admin
    email = data.get("email")  # invited user

    try:
        conn = connect_to_rds()
        conn.begin()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            # Step 1: Fetch admin permissions
            cursor.execute(
                "SELECT permissions,user_type FROM users WHERE user_id = %s", (user_id,)
            )
            admin_row = cursor.fetchone()
            if not admin_row:
                conn.rollback()
                return jsonify({"error": "Admin user not found"}), 404
            if admin_row["user_type"] == "user":
                return jsonify({"error": "unAuthrotized access"}), 404

            permissions = json.loads(admin_row["permissions"] or "{}")

            # Step 2: Fetch invited user permissions
            cursor.execute("SELECT permissions FROM users WHERE email = %s", (email,))
            invited_row = cursor.fetchone()
            if not invited_row:
                conn.rollback()
                return jsonify({"error": "Invited user not found"}), 404

            invited_permissions = json.loads(invited_row["permissions"] or "{}")

            # Step 3: Update invited user → set role.status = revoked
            if "status" in invited_permissions:
                invited_permissions["status"] = "revoked"
            else:
                invited_permissions = {"status": "revoked"}

            cursor.execute(
                "UPDATE users SET permissions=%s WHERE email=%s",
                (json.dumps(invited_permissions), email),
            )

            # Step 4: Update admin → set status revoked in shared/invites
            for section in ["shared", "invites"]:
                if section in permissions:
                    for p in permissions[section]:
                        if p.get("email") == email:
                            p["status"] = "revoked"

            cursor.execute(
                "UPDATE users SET permissions=%s WHERE user_id=%s",
                (json.dumps(permissions), user_id),
            )

            conn.commit()

        return jsonify({"message": "Role revoked successfully"}), 200

    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        if conn:
            conn.close()


@inv_users_bp.route("/admin/delete_shared_user_role", methods=["POST"])
def delete_shared_user_role():
    data = request.get_json()
    user_id = data.get("user_id")  # admin
    email = data.get("email")  # invited user

    try:
        conn = connect_to_rds()
        conn.begin()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            # Step 1: Fetch admin permissions
            cursor.execute(
                "SELECT permissions,user_type FROM users WHERE user_id = %s", (user_id,)
            )
            admin_row = cursor.fetchone()
            if not admin_row:
                conn.rollback()
                return jsonify({"error": "Admin user not found"}), 404
            if admin_row["user_type"] == "user":
                return jsonify({"error": "Unauthorized access"}), 403

            permissions = json.loads(admin_row["permissions"] or "{}")

            # Step 2: Fetch invited user to get their user_id
            cursor.execute("SELECT user_id FROM users WHERE email = %s", (email,))
            invited_row = cursor.fetchone()
            if not invited_row:
                conn.rollback()
                return jsonify({"error": "Invited user not found"}), 404

            invited_user_id = invited_row["user_id"]

            # Step 3: Remove from admin shared/invites arrays
            for section in ["shared", "invites"]:
                if section in permissions:
                    permissions[section] = [
                        p for p in permissions[section] if p.get("email") != email
                    ]

            # Step 4: Remove from agents_hub shared_hub_users
            # if "agents_hub" in permissions:
            #     for agent in permissions["agents_hub"]:
            #         if "shared_hub_users" in agent:
            #             agent["shared_hub_users"] = [
            #                 u
            #                 for u in agent["shared_hub_users"]
            #                 if u.get("email") != email
            #             ]
            if "agents_hub" in permissions:
                new_agents = []
                for agent in permissions["agents_hub"]:
                    # Check if agent's own email is the target
                    if agent.get("email") == email:
                        continue  # skip this agent entirely

                    # Filter out the target email from shared_hub_users
                    if "shared_hub_users" in agent:
                        agent["shared_hub_users"] = [
                            u
                            for u in agent["shared_hub_users"]
                            if u.get("email") != email
                        ]

                    new_agents.append(agent)

                permissions["agents_hub"] = new_agents

            # Step 5: Save admin permissions
            cursor.execute(
                "UPDATE users SET permissions=%s WHERE user_id=%s",
                (json.dumps(permissions), user_id),
            )

            conn.commit()  # commit admin permissions changes before deletion

        # Step 6: Delete invited user completely
        # (run your existing function outside the transaction)
        deletion_result = delete_all_user_data(invited_user_id)
        if deletion_result.get("status") != "success":
            return (
                jsonify(
                    {"error": "Invited user cleanup failed", "details": deletion_result}
                ),
                500,
            )

        return jsonify({"message": "Shared user removed and deleted successfully"}), 200

    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        if conn:
            conn.close()


@inv_users_bp.route("/admin/activate_shared_user_role", methods=["POST"])
def activate_shared_user_role():
    data = request.get_json()
    user_id = data.get("user_id")  # admin
    email = data.get("email")  # invited user

    try:
        conn = connect_to_rds()
        conn.begin()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            # Step 1: Fetch admin permissions
            cursor.execute(
                "SELECT permissions,user_type FROM users WHERE user_id = %s", (user_id,)
            )
            admin_row = cursor.fetchone()
            if not admin_row:
                conn.rollback()
                return jsonify({"error": "Admin user not found"}), 404
            if admin_row["user_type"] == "user":
                return jsonify({"error": "unAuthrotized access"}), 404

            permissions = json.loads(admin_row["permissions"] or "{}")

            # Step 2: Fetch invited user permissions
            cursor.execute("SELECT permissions FROM users WHERE email = %s", (email,))
            invited_row = cursor.fetchone()
            if not invited_row:
                conn.rollback()
                return jsonify({"error": "Invited user not found"}), 404

            invited_permissions = json.loads(invited_row["permissions"] or "{}")

            # Step 3: Update invited user → set role.status = active
            if "status" in invited_permissions:
                invited_permissions["status"] = "active"
            else:
                invited_permissions = {"status": "active"}

            cursor.execute(
                "UPDATE users SET permissions=%s WHERE email=%s",
                (json.dumps(invited_permissions), email),
            )

            # Step 4: Update admin → set status active in shared/invites
            for section in ["shared", "invites"]:
                if section in permissions:
                    for p in permissions[section]:
                        if p.get("email") == email:
                            p["status"] = "active"

            cursor.execute(
                "UPDATE users SET permissions=%s WHERE user_id=%s",
                (json.dumps(permissions), user_id),
            )

            conn.commit()

        return jsonify({"message": "Role activated successfully"}), 200

    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        if conn:
            conn.close()
