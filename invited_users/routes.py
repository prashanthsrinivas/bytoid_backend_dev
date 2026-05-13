from time import time
from flask import Blueprint, request, jsonify, redirect, g, session
import pymysql
from services.gmail_service import GmailService
import uuid
from db.rds_db import connect_to_rds
import json
from flask import redirect
from datetime import datetime
from gmail_route.routes import delete_all_user_data
from utils.permissions_map import PERMISSIONS
from invited_users.uszr_helper import (
    create_invited_user,
    dehashed_url,
    generate_hashed_url,
)
from utils.base_logger import get_logger
from utils.auth_resolver import get_user_from_request
from dotenv import load_dotenv
from services.outlook_service import OutlookService
from utils.permission_resolver import resolve_permissions
import os

inv_users_bp = Blueprint("invited_users", __name__)

logger = get_logger(__name__)
from services.audit_log_service import (
    log_audit_event, build_audit_actor,
    SPECIAL_ACCESS_GRANTED, SPECIAL_ACCESS_REVOKED, SPECIAL_ACCESS_REQUESTED,
    WORKSPACE_ACCESS_ENTERED,
    ROLE_CREATED, ROLE_UPDATED, ROLE_DELETED,
    USER_INVITED, INVITE_CANCELLED, INVITE_RESENT, USER_INVITE_ACCEPTED,
    USER_ROLE_CHANGED, USER_ACCESS_REVOKED, USER_ACCESS_ACTIVATED, USER_DELETED,
)
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
    raw_permissions = data.get("permissions", [])
    permissions = resolve_permissions(raw_permissions)

    if not userid or not name or not permissions:
        return (
            jsonify({"error": "Missing required fields: userid, name, permissions"}),
            400,
        )

    conn = None
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
                return jsonify({"error": "Access denied"}), 403
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

        actor_uid, actor_email, behalf_uid, behalf_email = build_audit_actor(userid)
        log_audit_event(
            action=ROLE_CREATED, endpoint="/admin/roles-add",
            ip=request.remote_addr, status="success",
            actor_user_id=actor_uid,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=behalf_uid,
            acting_on_behalf_of_email=behalf_email,
            metadata={"role_name": name, "permissions_count": len(permissions)},
        )
        g.audit_logged = True
        return jsonify({"message": "Role added successfully", "role": roles}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@inv_users_bp.route("/admin/roles-get/<userid>", methods=["GET"])
def get_roles(userid):
    """Get all roles and invited users for a user"""
    conn = None
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
                return jsonify({"error": "Access denied"}), 403

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
            invites = []
            shared = []
            
            for user in all_users:
                if user["user_type"] != "admin":
                    continue
                email = user["email"]
                cursor.execute("""
                    SELECT 1 FROM special_access
                    WHERE (grantor_admin_id=%s AND target_admin_id=%s)
                    OR (grantor_admin_id=%s AND target_admin_id=%s)
                """, (userid, user["user_id"], user["user_id"], userid))
                access = cursor.fetchone()
                has_access = bool(access)
            
                emails.append(email)
                special_access_status[email] = has_access
                user_obj = {
                   "email": email,
                   "role": {
                       "id": "admin_access",
                       "name": "Admin",
                       "permissions": []
                   },
                   "status": "active" if has_access else "pending"
               }
                if has_access:
                   shared.append(user_obj)
                else:
                   invites.append(user_obj)
                
        return (
            jsonify(
                {
                    "roles": roles,
                    "invited_users": emails,
                    "invited_users_structured": {
                        "invites": invites,
                        "shared": shared
                    },
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
    if permissions is not None:
        permissions = resolve_permissions(permissions)

    conn = None
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
                return jsonify({"error": "Access denied"}), 403
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
        userid = data.get("userid")
        actor_uid, actor_email, behalf_uid, behalf_email = build_audit_actor(userid)
        log_audit_event(
            action=ROLE_UPDATED, endpoint="/admin/roles-update",
            ip=request.remote_addr, status="success",
            actor_user_id=actor_uid,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=behalf_uid,
            acting_on_behalf_of_email=behalf_email,
            metadata={"role_id": data.get("role_id"), "role_name": data.get("name")},
        )
        g.audit_logged = True
        return jsonify({"message": "Role updated successfully"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@inv_users_bp.route("/admin/roles-delete/<userid>/<role_id>", methods=["DELETE"])
def delete_role(userid, role_id):
    """Delete role by role_id (only if not associated with invites or shared users)"""
    conn = None
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
                return jsonify({"error": "Access denied"}), 403

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

        actor_uid, actor_email, behalf_uid, behalf_email = build_audit_actor(userid)
        log_audit_event(
            action=ROLE_DELETED, endpoint="/admin/roles-delete",
            ip=request.remote_addr, status="success",
            actor_user_id=actor_uid,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=behalf_uid,
            acting_on_behalf_of_email=behalf_email,
            metadata={"role_id": role_id},
        )
        g.audit_logged = True
        return jsonify({"message": "Role deleted successfully"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()


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
                return jsonify({"error": "Access denied"}), 403

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

            # check outlook connection while cursor is open
            outlook_connected = has_outlook_connected(userid, cursor)

            # generate invite link
            base_invitation_link = generate_hashed_url(
                base_url=f"{os.getenv('BASE_FRNT_URL')}/invite",
                invited_to=email,
                invited_by=user_email,
            )

        # Commit invite to DB before attempting email
        conn.commit()

        # Attempt email — failure is non-fatal (invite is saved, user can resend)
        email_error = None
        try:
            if user_source == "google":
                gmail_service = GmailService(user_id=userid)
                gmail_service.send_invite_mail(
                    receipent_emails=email,
                    role=role,
                    invite_link=base_invitation_link,
                    business_info=business_info,
                )
            else:
                if outlook_connected:
                    outlook_service = OutlookService(user_id=userid)
                    outlook_service.send_invitation_email(
                        invitee=email,
                        inviter=user_email,
                        role=role,
                        invite_link=base_invitation_link,
                        business_info=business_info,
                    )
                else:
                    email_error = "Email not sent: mail provider not connected. The invite was saved — use Resend to deliver it once connected."
        except Exception as email_exc:
            logger.error(f"[invite_user] Email send failed for {email}: {email_exc}")
            email_error = f"Invite saved but email delivery failed: {email_exc}"

        actor_uid, actor_email_v, behalf_uid, behalf_email = build_audit_actor(userid)
        log_audit_event(
            action=USER_INVITED, endpoint="/admin/invite_user",
            ip=request.remote_addr, status="success",
            actor_user_id=actor_uid,
            actor_email=actor_email_v,
            acting_on_behalf_of_user_id=behalf_uid,
            acting_on_behalf_of_email=behalf_email,
            target_email=email,
            metadata={"role_id": role_id, "role_name": role.get("name"), "email_error": email_error},
        )
        g.audit_logged = True

        if email_error:
            return jsonify({"message": "Invitation saved.", "warning": email_error}), 200
        return jsonify({"message": "Invitation sent successfully"}), 200

    except Exception as e:
        if conn:
            conn.rollback()
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
                return jsonify({"error": "Access denied"}), 403

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

        actor_uid, actor_email, behalf_uid, behalf_email = build_audit_actor(userid)
        invite_data = next((i for i in permissions.get("invites", []) if i.get("email") == invited_email), {})
        log_audit_event(
            action=INVITE_CANCELLED, endpoint="/admin/delete-invite",
            ip=request.remote_addr, status="success",
            actor_user_id=actor_uid,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=behalf_uid,
            acting_on_behalf_of_email=behalf_email,
            target_email=invited_email,
            metadata={"role_id": invite_data.get("role"), "invite_email": invited_email},
        )
        g.audit_logged = True

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
                return jsonify({"error": "Access denied"}), 403

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

        actor_uid, actor_email, behalf_uid, behalf_email = build_audit_actor(user_id)
        invite_data = next((i for i in permissions.get("invites", []) if i.get("email") == invited_email), {})
        log_audit_event(
            action=INVITE_RESENT, endpoint="/admin/resend-invite",
            ip=request.remote_addr, status="success",
            actor_user_id=actor_uid,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=behalf_uid,
            acting_on_behalf_of_email=behalf_email,
            target_email=invited_email,
            metadata={"role_id": invite_data.get("role"), "invite_email": invited_email},
        )
        g.audit_logged = True

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

@inv_users_bp.route("/admin/accept_from_link", methods=["GET"])
def accept_from_link():
   token = request.args.get("token")
   if not token:
       return jsonify({"error": "Missing token"}), 400
   conn = None
   try:
       # ✅ decode token
       invited_by, invited_to, expiry = dehashed_url(token)
       # ⛔ expiry check
       if int(time()) > expiry:
           return jsonify({"error": "Link expired"}), 400
       conn = connect_to_rds()
       with conn.cursor(pymysql.cursors.DictCursor) as cursor:
           # 👉 requester (Admin A)
           cursor.execute(
               "SELECT user_id, email FROM users WHERE email=%s",
               (invited_by,)
           )
           requester = cursor.fetchone()
           # 👉 target (Admin B)
           cursor.execute(
               "SELECT user_id, email FROM users WHERE email=%s",
               (invited_to,)
           )
           target = cursor.fetchone()
           if not requester or not target:
               return jsonify({"error": "Invalid users"}), 400
           # ✅ GIVE ACCESS (B → A)
           cursor.execute("""
               INSERT IGNORE INTO special_access
               (grantor_admin_id, target_admin_id)
               VALUES (%s, %s)
           """, (target["user_id"], requester["user_id"]))
           # ✅ notification
           cursor.execute("""
               INSERT INTO notifications (user_id, message)
               VALUES (%s, %s)
           """, (
               requester["user_id"],
               "Your admin access request was accepted"
           ))
           conn.commit()

       log_audit_event(
           action=SPECIAL_ACCESS_GRANTED, endpoint="/admin/accept_from_link",
           ip=request.remote_addr, status="success",
           actor_user_id=target["user_id"] if target else None,
           actor_email=invited_to,
           target_user_id=requester["user_id"] if requester else None,
           target_email=invited_by,
           metadata={"grant_type": "email_link"},
       )
       g.audit_logged = True

       # ✅ SEND EMAIL BACK TO REQUESTER (IMPORTANT)
       try:
           outlook_service = OutlookService(user_id=target["user_id"])
           outlook_service.send_invitation_email(
               invitee=requester["email"],  # send to Admin A
               inviter=target["email"],    # from Admin B
               role={"name": "Admin Access Granted"},
               invite_link="",
               business_info={}
           )
       except Exception as e:
           logger.error(f"Email sending failed: {str(e)}")
       # ✅ REDIRECT TO FRONTEND
       return redirect(f"{os.getenv('BASE_FRNT_URL')}/admin-access?success=true")
   except Exception as e:
       logger.error(f"Accept link error: {str(e)}")
       return jsonify({"error": str(e)}), 500
   finally:
       if conn:
           conn.close()
 

@inv_users_bp.route("/admin/grant_special_access", methods=["POST"])
def grant_special_access():
    data = request.get_json()

    current_admin_id = data.get("user_id")
    target_admin_id = data.get("target_admin_id")

    conn = None
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:

            # check current admin
            cursor.execute(
                "SELECT user_type, company_name, email FROM users WHERE user_id=%s",
                (current_admin_id,),
            )
            current = cursor.fetchone()

            # check target admin
            cursor.execute(
                 "SELECT user_type, company_name, email FROM users WHERE user_id=%s",
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
            """, (target_admin_id, current_admin_id))

            conn.commit()

        actor_uid, actor_email, behalf_uid, behalf_email = build_audit_actor(current_admin_id)
        log_audit_event(
            action=SPECIAL_ACCESS_GRANTED,
            endpoint="/admin/grant_special_access",
            ip=request.remote_addr, status="success",
            actor_user_id=actor_uid,
            actor_email=actor_email,
            target_user_id=target_admin_id,
            target_email=target.get("email") if target else None,
            acting_on_behalf_of_user_id=behalf_uid,
            acting_on_behalf_of_email=behalf_email,
            metadata={"grant_type": "direct"},
        )
        g.audit_logged = True
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
            """, (target["user_id"], requester_id))

            if cursor.fetchone():
                return jsonify({"error": "Access already exists"}), 400
            #if requester_id == target["user_id"]:
             #   return jsonify({"error": "Cannot request yourself"}), 400
            
            # 🔔 notification for Admin B
            cursor.execute("""
                INSERT INTO notifications (user_id, message)
                VALUES (%s, %s)
            """, (target["user_id"], "Admin requested access to your data"))

            # get user source

            cursor.execute(
               "SELECT social, email FROM users WHERE user_id=%s",
               (requester_id,)
            )
            source_row = cursor.fetchone()
        
            if not source_row:
               return jsonify({"error": "Requester not found"}), 400
            
            inviter_email = source_row["email"]
            user_source = (source_row.get("social") or "").strip().lower()
            
            # generate link (reuse your invite system)
            link = generate_hashed_url(
                base_url=f"{os.getenv('BASE_API_URL')}/admin/accept_from_link",
                invited_to=target_email,
                invited_by=inviter_email
            )

            print("DEBUG user_source:", user_source)
            # ✅ GET TOKEN + SOCIAL
            # check outlook connection
            if not has_outlook_connected(requester_id, cursor):
                conn.commit()
                return jsonify({
                    "error": "Outlook not connected. Please connect Outlook first."
                }), 400
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

                actor_uid, actor_email, behalf_uid, behalf_email = build_audit_actor(requester_id)
                log_audit_event(
                    action=SPECIAL_ACCESS_REQUESTED, endpoint="/admin/request_special_access",
                    ip=request.remote_addr, status="success",
                    actor_user_id=actor_uid,
                    actor_email=actor_email,
                    target_user_id=target["user_id"],
                    target_email=target_email,
                    acting_on_behalf_of_user_id=behalf_uid,
                    acting_on_behalf_of_email=behalf_email,
                    metadata={"target_found": True},
                )
                g.audit_logged = True

                return jsonify({"message": "Request sent via Outlook"}), 200

            except Exception as e:
                logger.error(f"Outlook error: {str(e)}")
                conn.rollback()
                return jsonify({"error": "Failed to send email"}), 500
    finally:
        if conn:
            conn.close()

@inv_users_bp.route("/admin/accept_special_access", methods=["GET", "POST"])
def accept_special_access():
    data = request.get_json()

    requester_id = data.get("requester_id")  # Admin A
    target_id = data.get("target_id")        # Admin B

    conn = None
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:

            # ✅ 1. ADMIN CHECK
            cursor.execute("SELECT user_type, company_name, email FROM users WHERE user_id=%s", (requester_id,))
            req = cursor.fetchone()

            cursor.execute("SELECT user_type, company_name, email FROM users WHERE user_id=%s", (target_id,))
            tgt = cursor.fetchone()

            if not req or not tgt:
                return jsonify({"error": "User(s) not found"}), 404

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

        log_audit_event(
            action=SPECIAL_ACCESS_GRANTED,
            endpoint="/admin/accept_special_access",
            ip=request.remote_addr, status="success",
            actor_user_id=target_id,
            actor_email=tgt.get("email") if tgt else None,
            target_user_id=requester_id,
            target_email=req.get("email") if req else None,
            metadata={"grant_type": "accept_request"},
        )
        g.audit_logged = True
        return jsonify({"message": "Access granted"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()

@inv_users_bp.route("/admin/revoke_special_access", methods=["POST"])

def revoke_special_access():

    data = request.get_json()
    grantor_id = data.get("user_id")   # logged-in data owner (grantor_admin_id)
    target_id = data.get("target_id")  # accessor being revoked (target_admin_id)

    conn = None
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            # Validate admins
            cursor.execute(
                "SELECT user_type, company_name, email FROM users WHERE user_id=%s",
                (grantor_id,)
            )
            grantor = cursor.fetchone()
            cursor.execute(
                "SELECT user_type, company_name, email FROM users WHERE user_id=%s",
                (target_id,)
            )
            target = cursor.fetchone()
            if not grantor or not target:
                return jsonify({"error": "Users not found"}), 404
            if grantor["user_type"] != "admin" or target["user_type"] != "admin":
                return jsonify({"error": "Only admins allowed"}), 403
            if grantor["company_name"] != target["company_name"]:
                return jsonify({"error": "Different organization"}), 403
            # Delete access: (grantor = data owner, target = accessor being revoked)
            cursor.execute("""
                DELETE FROM special_access
                WHERE grantor_admin_id=%s AND target_admin_id=%s
            """, (grantor_id, target_id))
            if cursor.rowcount == 0:
                return jsonify({"error": "Access record not found — no matching grant exists"}), 404
            # Notification (only fires when a row was actually deleted)
            cursor.execute("""
                INSERT INTO notifications (user_id, message)
                VALUES (%s, %s)
            """, (
                target_id,
                "Your admin access has been revoked"
            ))
            conn.commit()
        actor_uid, actor_email, behalf_uid, behalf_email = build_audit_actor(grantor_id)
        log_audit_event(
            action=SPECIAL_ACCESS_REVOKED,
            endpoint="/admin/revoke_special_access",
            ip=request.remote_addr, status="success",
            actor_user_id=actor_uid,
            actor_email=actor_email,
            target_user_id=target_id,
            target_email=target.get("email") if target else None,
            acting_on_behalf_of_user_id=behalf_uid,
            acting_on_behalf_of_email=behalf_email,
            metadata={"revocation_type": "manual"},
        )
        g.audit_logged = True
        return jsonify({"message": "Access revoked successfully"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@inv_users_bp.route("/admin/access-workspace", methods=["POST"])
def access_workspace():
    """
    Called by the frontend when a secondary-access admin explicitly enters another admin's workspace.
    Fires a WORKSPACE_ACCESS_ENTERED audit event with actor=Kavya, workspace_owner=Test.
    The session user (Kavya) must have a special_access grant from workspace_user_id (Test).
    """
    data = request.get_json()
    workspace_user_id = data.get("workspace_user_id")

    import sys
    print(
        f"[ACCESS-WORKSPACE] incoming workspace_user_id={data.get('workspace_user_id')!r}"
        f" | session_user_id={session.get('user_id')!r}"
        f" | active_workspace_id BEFORE={session.get('active_workspace_id')!r}",
        file=sys.stderr, flush=True,
    )

    if not workspace_user_id:
        return jsonify({"error": "workspace_user_id required"}), 400

    # Read session identity directly. build_audit_actor cannot be used here because
    # session["active_workspace_id"] is not yet set, so it would fall to the self-access
    # branch and return actor_uid = workspace_user_id, triggering the 403 guard below.
    actor_uid = (
        session.get("user_id")
        or getattr(g, "session_user_id", None)
        or getattr(g, "user_id", None)
    )

    if not actor_uid:
        return jsonify({"error": "Not authenticated"}), 401

    if actor_uid == workspace_user_id:
        return jsonify({"error": "No delegation context — session user matches workspace owner"}), 403

    conn = None
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                "SELECT 1 FROM special_access WHERE grantor_admin_id=%s AND target_admin_id=%s",
                (workspace_user_id, actor_uid),
            )
            if not cursor.fetchone():
                return jsonify({"error": "No special access grant found"}), 403

        session["active_workspace_id"] = workspace_user_id

        print(
            f"[ACCESS-WORKSPACE] STORED active_workspace_id={session.get('active_workspace_id')!r}"
            f" | actor_uid={actor_uid!r}",
            file=sys.stderr, flush=True,
        )

        from db.db_checkers import get_email_by_id as _get_email
        actor_email = _get_email(actor_uid)
        obo_email = _get_email(workspace_user_id)

        log_audit_event(
            action=WORKSPACE_ACCESS_ENTERED,
            endpoint="/admin/access-workspace",
            ip=request.remote_addr,
            status="success",
            actor_user_id=actor_uid,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=workspace_user_id,
            acting_on_behalf_of_email=obo_email,
        )
        g.audit_logged = True

        return jsonify({"message": "Workspace access recorded"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@inv_users_bp.route("/admin/exit-workspace", methods=["POST"])
def exit_workspace():
    """Clear the active workspace delegation session."""
    session.pop("active_workspace_id", None)
    return jsonify({"status": "exited"}), 200


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

        log_audit_event(
            action=USER_INVITE_ACCEPTED, endpoint="/admin/validate_invite",
            ip=request.remote_addr, status="success",
            actor_user_id=user_created.get("user_id") if isinstance(user_created, dict) else None,
            actor_email=invited_to,
            target_user_id=inviter_row["user_id"] if inviter_row else None,
            target_email=invited_by,
            metadata={"invite_accepted_via_token": True},
        )
        g.audit_logged = True

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
                return jsonify({"error": "Access denied"}), 403

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

        actor_uid, actor_email, behalf_uid, behalf_email = build_audit_actor(user_id)
        log_audit_event(
            action=USER_ROLE_CHANGED, endpoint="/admin/edit_shared_user_role",
            ip=request.remote_addr, status="success",
            actor_user_id=actor_uid,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=behalf_uid,
            acting_on_behalf_of_email=behalf_email,
            target_email=email,
            metadata={"new_role_id": role_id},
        )
        g.audit_logged = True

        return jsonify({"message": "Role updated successfully"}), 200

    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        if conn:
            conn.close()

@inv_users_bp.route("/notifications/<user_id>", methods=["GET"])
def get_notifications(user_id):
    current_user_id = get_user_from_request() or user_id

    # Allow self-access; require admin for cross-user access
    if current_user_id != user_id:
        conn = None
        try:
            conn = connect_to_rds()
            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute("SELECT user_type FROM users WHERE user_id = %s", (current_user_id,))
                current_user = cursor.fetchone()
                if not current_user or current_user["user_type"] != "admin":
                    return jsonify({"error": "Forbidden"}), 403
        except Exception:
            return jsonify({"error": "Forbidden"}), 403
        finally:
            if conn:
                conn.close()

    conn = None
    try:
        conn = connect_to_rds()
        if conn is None:
            return jsonify({"notifications": []}), 200
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute("""
                SELECT id, message, is_read, created_at
                FROM notifications
                WHERE user_id=%s
                ORDER BY created_at DESC
                LIMIT 50
            """, (user_id,))
            rows = cursor.fetchall()
            for row in rows:
                if row.get("created_at") is not None and hasattr(row["created_at"], "isoformat"):
                    row["created_at"] = row["created_at"].isoformat()
        return jsonify({"notifications": rows}), 200

    except Exception as e:
        logger.error("Notifications fetch error for user %s: %s", user_id, e)
        return jsonify({"notifications": []}), 200

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
                return jsonify({"error": "Access denied"}), 403

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

        actor_uid, actor_email, behalf_uid, behalf_email = build_audit_actor(user_id)
        log_audit_event(
            action=USER_ACCESS_REVOKED, endpoint="/admin/revoke_shared_user_role",
            ip=request.remote_addr, status="success",
            actor_user_id=actor_uid,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=behalf_uid,
            acting_on_behalf_of_email=behalf_email,
            target_email=email,
            metadata={"email": email, "action": "access_revoked"},
        )
        g.audit_logged = True

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

        actor_uid, actor_email, behalf_uid, behalf_email = build_audit_actor(user_id)
        log_audit_event(
            action=USER_DELETED, endpoint="/admin/delete_shared_user_role",
            ip=request.remote_addr, status="success",
            actor_user_id=actor_uid,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=behalf_uid,
            acting_on_behalf_of_email=behalf_email,
            target_user_id=invited_user_id,
            target_email=email,
            metadata={"permanent_deletion": True},
        )
        g.audit_logged = True

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
                return jsonify({"error": "Access denied"}), 403

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

        actor_uid, actor_email, behalf_uid, behalf_email = build_audit_actor(user_id)
        log_audit_event(
            action=USER_ACCESS_ACTIVATED, endpoint="/admin/activate_shared_user_role",
            ip=request.remote_addr, status="success",
            actor_user_id=actor_uid,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=behalf_uid,
            acting_on_behalf_of_email=behalf_email,
            target_email=email,
            metadata={"email": email, "action": "access_activated"},
        )
        g.audit_logged = True

        return jsonify({"message": "Role activated successfully"}), 200

    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        if conn:
            conn.close()


@inv_users_bp.route("/admin/all_special_access_users/<userid>", methods=["GET"])
def all_special_access_users(userid):
    conn = None
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:

            cursor.execute(
                "SELECT user_type FROM users WHERE user_id=%s", (userid,)
            )
            row = cursor.fetchone()
            if not row:
                return jsonify({"error": "User not found"}), 404
            if row["user_type"] == "user":
                return jsonify({"error": "Unauthorized access"}), 403

            cursor.execute(
                """
                SELECT sa.target_admin_id, u.email
                FROM special_access sa
                JOIN users u ON u.user_id = sa.target_admin_id
                WHERE sa.grantor_admin_id = %s
                """,
                (userid,),
            )
            rows = cursor.fetchall()

        result = {r["email"]: r["target_admin_id"] for r in rows}
        return jsonify({"special_access_users": result}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        if conn:
            conn.close()


@inv_users_bp.route("/admin/all_special_access_sources/<userid>", methods=["GET"])
def all_special_access_sources(userid):
    """Get all admins whose data I can access (I am target, they are grantor)"""
    conn = None
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:

            cursor.execute(
                "SELECT user_type FROM users WHERE user_id=%s", (userid,)
            )
            row = cursor.fetchone()
            if not row:
                return jsonify({"error": "User not found"}), 404
            if row["user_type"] == "user":
                return jsonify({"error": "Unauthorized access"}), 403

            cursor.execute(
                """
                SELECT sa.grantor_admin_id, u.email
                FROM special_access sa
                JOIN users u ON u.user_id = sa.grantor_admin_id
                WHERE sa.target_admin_id = %s
                """,
                (userid,),
            )
            rows = cursor.fetchall()

        result = {r["email"]: r["grantor_admin_id"] for r in rows}
        return jsonify({"special_access_sources": result}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        if conn:
            conn.close()


@inv_users_bp.route("/admin/audit-logs", methods=["GET"])
def get_audit_logs():
    """
    Serve audit log entries from logs/audit.log as paginated JSON.
    Admin-only. Frontend expects: items, total, page, pageSize.
    """
    try:
        # 1. AUTH CHECK
        user_id = session.get("user_id")
        if not user_id:
            return jsonify({"error": "Unauthorized"}), 401

        # 2. ADMIN TYPE CHECK (DB)
        conn = connect_to_rds()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute(
                    "SELECT user_type FROM users WHERE user_id = %s", (user_id,)
                )
                row = cursor.fetchone()
            if not row or row["user_type"] != "admin":
                return jsonify({"error": "Unauthorized"}), 403
        finally:
            conn.close()

        # 3. PARSE QUERY PARAMS
        try:
            page = max(1, int(request.args.get("page", 1)))
        except (ValueError, TypeError):
            page = 1

        try:
            page_size = min(200, max(1, int(request.args.get("pageSize", 50))))
        except (ValueError, TypeError):
            page_size = 50

        action_filter      = request.args.get("action")
        category_filter    = request.args.get("category")
        status_filter      = request.args.get("status")
        actor_filter       = request.args.get("actor_user_id")
        workspace_filter   = request.args.get("workspace_user_id")  # primary ownership filter
        from_ts            = request.args.get("from_ts")
        to_ts              = request.args.get("to_ts")

        # 4. READ + PARSE logs/audit.log
        log_path = os.path.join(os.path.dirname(__file__), "..", "logs", "audit.log")
        log_path = os.path.normpath(log_path)

        entries = []
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        # Normalize missing fields for older entries
                        entry.setdefault("category", "api_activity")
                        entry.setdefault("acting_on_behalf_of_user_id", None)
                        entry.setdefault("acting_on_behalf_of_email", None)
                        entry.setdefault("metadata", {})
                        # Back-fill audit_owner_id for entries written before this field existed.
                        if "audit_owner_id" not in entry:
                            entry["audit_owner_id"] = (
                                entry.get("acting_on_behalf_of_user_id")
                                or entry.get("actor_user_id")
                            )
                        entries.append(entry)
                    except (json.JSONDecodeError, ValueError):
                        continue  # skip malformed lines silently

        # 5. APPLY FILTERS
        def matches(e):
            if action_filter   and e.get("action")       != action_filter:    return False
            if category_filter and e.get("category")     != category_filter:  return False
            if status_filter   and e.get("status")       != status_filter:    return False
            if from_ts         and e.get("timestamp","") <  from_ts:          return False
            if to_ts           and e.get("timestamp","") >  to_ts:            return False

            # workspace_user_id: primary ownership filter — returns all entries that belong
            # to this user's audit page (their own actions + actions taken inside their workspace).
            # back-fill above already handles old entries (no key → populated from acting_on_behalf_of/actor_user_id).
            if workspace_filter:
                owner = e.get("audit_owner_id")
                if owner != workspace_filter:
                    return False
            elif actor_filter:
                # Legacy: filter directly by actor (only shows self-actions, misses delegation).
                if e.get("actor_user_id") != actor_filter:
                    return False

            return True

        filtered = [e for e in entries if matches(e)]

        # 6. SORT (newest first)
        filtered.sort(key=lambda e: e.get("timestamp", ""), reverse=True)

        # 7. PAGINATE
        total  = len(filtered)
        offset = (page - 1) * page_size
        items  = filtered[offset : offset + page_size]

        # 8. RETURN
        return jsonify({
            "items": items,
            "total": total,
            "page": page,
            "pageSize": page_size,
        }), 200

    except Exception as e:
        logger.error(f"Error in get_audit_logs: {e}", exc_info=True)
        import traceback
        traceback.print_exc()
        return jsonify({"error": "Internal server error"}), 500
