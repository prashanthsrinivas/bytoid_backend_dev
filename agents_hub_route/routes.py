from db.rds_db import connect_to_rds
from flask import Blueprint, request, jsonify, session, redirect
from utils.base_logger import get_logger
import pymysql
import json

agent_hub_bp = Blueprint("agent_hub", __name__)
logger = get_logger(__name__)


@agent_hub_bp.route("/get_all_user_permissionsbased/<userid>", methods=["GET"])
def get_all_user_permissionsbased(userid):
    try:
        conn = connect_to_rds()
        conn.begin()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            # Step 1: Fetch user_id's roles and permissions
            cursor.execute(
                "SELECT permissions, user_type FROM users WHERE user_id = %s",
                (userid,),
            )
            admin_row = cursor.fetchone()
            if not admin_row:
                conn.rollback()
                return jsonify({"error": "Admin user not found"}), 404

            if admin_row["user_type"] == "user":
                conn.rollback()
                return jsonify({"error": "Unauthorized access"}), 403

            # Parse permissions JSON safely
            owner_permissions = (
                json.loads(admin_row["permissions"])
                if admin_row and admin_row["permissions"]
                else {"shared": []}
            )

        return jsonify(owner_permissions), 200

    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        conn.close()


@agent_hub_bp.route("/get_all_user_agents/<userid>", methods=["GET"])
def get_all_user_agents(userid):
    try:
        conn = connect_to_rds()
        conn.begin()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            # Step 1: Fetch requesting user's roles and permissions
            cursor.execute(
                "SELECT permissions, user_type FROM users WHERE user_id = %s",
                (userid,),
            )
            admin_row = cursor.fetchone()
            if not admin_row:
                conn.rollback()
                return jsonify({"error": "User not found"}), 404

            if admin_row["user_type"] == "user":
                conn.rollback()
                return jsonify({"error": "Unauthorized access"}), 403

            # Parse permissions JSON safely - handle null/empty permissions
            permissions_json = admin_row.get("permissions")
            if permissions_json:
                try:
                    owner_permissions = json.loads(permissions_json)
                except (json.JSONDecodeError, TypeError):
                    owner_permissions = {"invites": [], "shared": [], "agents_hub": []}
            else:
                owner_permissions = {"invites": [], "shared": [], "agents_hub": []}

            # Normalize permissions structure
            if "agents_hub" not in owner_permissions:
                owner_permissions["agents_hub"] = []

            affected_emails = {
                entry["email"].lower() for entry in owner_permissions.get("shared", [])
            }

            # 🔹 Base user’s own agents
            cursor.execute(
                """
                SELECT 
                    u.email,
                    u.user_id,
                    l.launch_id,
                    l.sub_agent_id_fk,
                    l.website_name,
                    s.name,
                    s.description,
                    s.voice_type,
                    s.model_version
                FROM users u
                JOIN launch l ON u.user_id = l.user_id_fk
                JOIN subagents s ON l.launch_id = s.launch_id_fk
                WHERE u.user_id = %s
                """,
                (userid,),
            )
            base_user_agents = cursor.fetchall()

            invited_rows = []
            # 🔹 Fetch shared users' agents (only if they exist)
            if affected_emails:
                email_tuple = tuple(affected_emails)
                if len(email_tuple) == 1:
                    email_tuple = (email_tuple[0],)

                cursor.execute(
                    """
                    SELECT 
                        u.email,
                        u.user_id,
                        l.launch_id,
                        l.sub_agent_id_fk,
                        l.website_name,
                        s.name,
                        s.description,
                        s.voice_type,
                        s.model_version
                    FROM users u
                    JOIN launch l ON u.user_id = l.user_id_fk
                    JOIN subagents s ON l.launch_id = s.launch_id_fk
                    WHERE u.email IN %s
                    """,
                    (email_tuple,),
                )
                invited_rows = cursor.fetchall()

            # Merge base + invited agents (ensure both are lists)
            base_user_agents = list(base_user_agents) if base_user_agents else []
            invited_rows = list(invited_rows) if invited_rows else []
            all_agents = base_user_agents + invited_rows

            # Add `shared_hub_users` field
            for agent in all_agents:
                agent["shared_hub_users"] = []

            # Deduplicate by email + launch_id
            existing_hub = {
                (a.get("email"), a.get("launch_id"))
                for a in owner_permissions.get("agents_hub", [])
            }
            new_added = False
            for agent in all_agents:
                key = (agent["email"], agent["launch_id"])
                if key not in existing_hub:  # only add if launch exists
                    owner_permissions["agents_hub"].append(agent)
                    new_added = True

            # 🔹 Persist only if new agents were added
            if new_added:
                cursor.execute(
                    "UPDATE users SET permissions = %s WHERE user_id = %s",
                    (json.dumps(owner_permissions), userid),
                )
                conn.commit()
            else:
                conn.rollback()
            shared = owner_permissions.get("shared", [])

            # Build map + collect emails in one pass
            shared_map = {s["email"]: s["status"] for s in shared}
            allemails = [s["email"] for s in shared]

            for agent in owner_permissions.get("agents_hub", []):
                agent["status"] = (
                    "active"
                    if agent.get("user_id") == userid
                    else shared_map.get(agent.get("email"))
                )
        return (
            jsonify(
                {
                    "userId": userid,
                    "agents_hub": owner_permissions["agents_hub"],
                    "emails": allemails,
                }
            ),
            200,
        )

    except Exception as e:
        print(f"❌ [ERROR] agents_hub route: {str(e)}")
        import traceback
        traceback.print_exc()
        try:
            conn.rollback()
        except:
            pass
        return jsonify({"error": str(e)}), 500

    finally:
        conn.close()


@agent_hub_bp.route("/agents_hub/share", methods=["POST"])
def share_agent_hub_user():
    """
    Add invited users (emails list) to shared_hub_users of all agents
    in agents_hub for a given main user_id.
    """
    data = request.get_json()
    user_id = data.get("user_id")  # main user
    emails = data.get("email")  # invited users (list expected)
    launch_id = data.get("launch_id")

    if not user_id or not emails or not launch_id:
        return (
            jsonify({"error": "user_id, email(list), and launch_id are required"}),
            400,
        )

    if not isinstance(emails, list):
        return jsonify({"error": "email must be a list"}), 400

    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            # 1️⃣ Fetch permissions for the main user
            cursor.execute(
                "SELECT permissions FROM users WHERE user_id = %s", (user_id,)
            )
            row = cursor.fetchone()
            if not row:
                return jsonify({"error": "Main user not found"}), 404

            permissions = json.loads(row["permissions"]) if row["permissions"] else {}
            agents_hub = permissions.get("agents_hub", [])

            updated = False
            for agent in agents_hub:
                if agent.get("launch_id") == launch_id:
                    if "shared_hub_users" not in agent:
                        agent["shared_hub_users"] = []
                    existing_emails = {
                        u.get("email") for u in agent["shared_hub_users"]
                    }
                    for email in emails:
                        if email not in existing_emails:
                            agent["shared_hub_users"].append({"email": email})
                            updated = True

            if updated:
                cursor.execute(
                    "UPDATE users SET permissions = %s WHERE user_id = %s",
                    (json.dumps(permissions), user_id),
                )
                conn.commit()

        return (
            jsonify(
                {
                    "success": True,
                    "message": f"{', '.join(emails)} added to shared_hub_users.",
                }
            ),
            200,
        )

    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        conn.close()


@agent_hub_bp.route("/agents_hub/unshare", methods=["POST"])
def unshare_agent_hub_user():
    """
    Remove invited users (emails list) from shared_hub_users of all agents
    in agents_hub for a given main user_id.
    """
    data = request.get_json()
    user_id = data.get("user_id")  # main user
    emails = data.get("email")  # invited users (list expected)
    launch_id = data.get("launch_id")

    if not user_id or not emails or not launch_id:
        return (
            jsonify({"error": "user_id, email(list), and launch_id are required"}),
            400,
        )

    if not isinstance(emails, list):
        return jsonify({"error": "email must be a list"}), 400

    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            # 1️⃣ Fetch permissions for the main user
            cursor.execute(
                "SELECT permissions FROM users WHERE user_id = %s", (user_id,)
            )
            row = cursor.fetchone()
            if not row:
                return jsonify({"error": "Main user not found"}), 404

            permissions = json.loads(row["permissions"]) if row["permissions"] else {}
            agents_hub = permissions.get("agents_hub", [])

            updated = False
            for agent in agents_hub:
                if agent.get("launch_id") == launch_id and "shared_hub_users" in agent:
                    before_count = len(agent["shared_hub_users"])
                    agent["shared_hub_users"] = [
                        u
                        for u in agent["shared_hub_users"]
                        if u.get("email") not in emails
                    ]
                    if len(agent["shared_hub_users"]) != before_count:
                        updated = True

            if updated:
                cursor.execute(
                    "UPDATE users SET permissions = %s WHERE user_id = %s",
                    (json.dumps(permissions), user_id),
                )
                conn.commit()

        return (
            jsonify(
                {
                    "success": True,
                    "message": f"{', '.join(emails)} removed from shared_hub_users.",
                }
            ),
            200,
        )

    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        conn.close()


@agent_hub_bp.route("/delete_user_with_agents", methods=["POST"])
def delete_user_with_agents():
    """
    Delete a user by email along with their launches, subagents,
    and cleanup from permissions of any owner.
    """
    data = request.get_json()
    email = data.get("email")

    if not email:
        return jsonify({"error": "Email is required"}), 400

    try:
        conn = connect_to_rds()
        conn.begin()

        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            # 1️⃣ Find user_id from email
            cursor.execute("SELECT user_id FROM users WHERE email = %s", (email,))
            user_row = cursor.fetchone()

            if not user_row:
                conn.rollback()
                return jsonify({"error": "User not found"}), 404

            user_id = user_row["user_id"]

            # 2️⃣ Get all launch_ids for this user
            cursor.execute(
                "SELECT launch_id FROM launch WHERE user_id_fk = %s", (user_id,)
            )
            launches = cursor.fetchall()
            launch_ids = [row["launch_id"] for row in launches]

            # 3️⃣ Delete subagents tied to those launch_ids
            if launch_ids:
                cursor.execute(
                    "DELETE FROM subagents WHERE launch_id_fk IN %s",
                    (tuple(launch_ids),),
                )

            # 4️⃣ Delete launches
            cursor.execute("DELETE FROM launch WHERE user_id_fk = %s", (user_id,))

            # 5️⃣ Delete user
            cursor.execute("DELETE FROM users WHERE user_id = %s", (user_id,))

            # 6️⃣ Cleanup from all owners’ permissions (shared + agents_hub)
            cursor.execute("SELECT user_id, permissions FROM users")
            all_users = cursor.fetchall()

            for u in all_users:
                perms = (
                    json.loads(u["permissions"])
                    if u and u["permissions"]
                    else {"invites": [], "shared": [], "agents_hub": []}
                )
                changed = False

                # Remove from shared
                before_shared = len(perms.get("shared", []))
                perms["shared"] = [
                    entry
                    for entry in perms.get("shared", [])
                    if entry.get("email") != email
                ]
                if len(perms["shared"]) != before_shared:
                    changed = True

                # Remove from agents_hub
                before_hub = len(perms.get("agents_hub", []))
                perms["agents_hub"] = [
                    entry
                    for entry in perms.get("agents_hub", [])
                    if entry.get("email") != email
                ]
                if len(perms["agents_hub"]) != before_hub:
                    changed = True

                if changed:
                    cursor.execute(
                        "UPDATE users SET permissions = %s WHERE user_id = %s",
                        (json.dumps(perms), u["user_id"]),
                    )

            conn.commit()

        return jsonify({"success": f"User {email} and related data deleted."}), 200

    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        conn.close()


@agent_hub_bp.route("/get_all_mini_agents/<userid>", methods=["GET"])
def get_all_mini_agents(userid):
    try:
        conn = connect_to_rds()
        conn.begin()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            # Step 1: Fetch requesting user's roles and permissions
            cursor.execute(
                "SELECT permissions,email, user_type FROM users WHERE user_id = %s",
                (userid,),
            )
            base_row = cursor.fetchone()
            if not base_row:
                conn.rollback()
                return jsonify({"error": "User not found"}), 404

            agents_hub_data = []
            if base_row["user_type"] == "user":
                base_permissions = (
                    json.loads(base_row["permissions"])
                    if base_row and base_row["permissions"]
                    else {"permissions": []}
                )
                invited_user = base_permissions["invited_by"]
                cursor.execute(
                    "SELECT permissions, user_type FROM users WHERE email = %s",
                    (invited_user,),
                )
                admin_row = cursor.fetchone()
                if not admin_row:
                    conn.rollback()
                    return jsonify({"error": "admin not found"}), 404
                owner_permissions = (
                    json.loads(admin_row["permissions"])
                    if admin_row and admin_row["permissions"]
                    else {"invites": [], "shared": [], "agents_hub": []}
                )
                # Normalize permissions structure
                if "agents_hub" not in owner_permissions:
                    owner_permissions["agents_hub"] = []
                shared = owner_permissions["shared"] or []
                status_map = {
                    entry.get("email"): entry.get("status") for entry in shared
                }
                for agent in owner_permissions.get("agents_hub", []):
                    shared_users = agent.get("shared_hub_users", [])
                    # check if base_row["email"] exists in any dict's "email" field
                    if any(u.get("email") == base_row["email"] for u in shared_users):
                        agents_hub_data.append(
                            {
                                "email": agent.get("email"),
                                "name": agent.get("name"),
                                "id": agent.get("launch_id"),
                                "userid": agent.get("user_id"),
                                "status": status_map.get(
                                    base_row["email"], "revoked"
                                ),  # default revoked
                            }
                        )
                    if base_row["email"] in agent["email"]:
                        agents_hub_data.append(
                            {
                                "email": agent.get("email"),
                                "name": agent.get("name"),
                                "id": agent.get("launch_id"),
                                "userid": agent.get("user_id"),
                                "status": status_map.get(
                                    base_row["email"], "revoked"
                                ),  # default revoked
                            }
                        )

            else:
                # Parse permissions JSON safely
                owner_permissions = (
                    json.loads(base_row["permissions"])
                    if base_row and base_row["permissions"]
                    else {"invites": [], "shared": [], "agents_hub": []}
                )

                # Normalize permissions structure
                if "agents_hub" not in owner_permissions:
                    owner_permissions["agents_hub"] = []
                shared = owner_permissions["shared"] or []
                status_map = {
                    entry.get("email"): entry.get("status") for entry in shared
                }

                for agent in owner_permissions["agents_hub"]:
                    cursor.execute(
                        "SELECT first_name, last_name FROM users WHERE user_id = %s",
                        (agent.get("user_id"),),
                    )
                    user_row = cursor.fetchone()

                    # Build full name (fallbacks to "" if missing)
                    full_name = ""
                    if user_row:
                        first_name = user_row.get("first_name") or ""
                        last_name = user_row.get("last_name") or ""
                        full_name = f"{first_name} {last_name}".strip()

                    agents_hub_data.append(
                        {
                            "email": agent.get("email"),
                            "name": agent.get("name"),
                            "username": full_name,
                            "id": agent.get("launch_id"),
                            "userid": agent.get("user_id"),
                            "status": status_map.get(
                                base_row["email"], "revoked"
                            ),  # default revoked
                        }
                    )

        return (
            jsonify(
                {
                    "userId": userid,
                    "mini_agents_data": agents_hub_data,
                }
            ),
            200,
        )

    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        conn.close()
