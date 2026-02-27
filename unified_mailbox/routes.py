from db.db_checkers import get_notes_data
from flask import Blueprint, request, jsonify, session
import asyncio
from microsoft_route.routes import microsoft_list_drafts
from gmail_route.routes import list_drafts
from db.rds_db import connect_to_rds
from utils.s3_utils import read_json_from_s3
from datetime import datetime
import json
import traceback
import uuid


def get_user_login_method(user_id):
    """
    Get the login method (google/microsoft) for a user based on their social field
    """
    try:
        connection = connect_to_rds()
        if connection is None:
            # print("❌ [DEBUG] Database connection failed in get_user_login_method")
            return "gmail"  # Default fallback

        cursor = connection.cursor()
        cursor.execute("SELECT social FROM users WHERE user_id = %s", (user_id,))
        row = cursor.fetchone()

        cursor.close()
        connection.close()

        if row and row[0]:
            social = row[0].lower()
            if social == "microsoft":
                return "microsoft"
            elif social == "google":
                return "gmail"

        # Default to gmail if no social field or unrecognized value
        return "gmail"

    except Exception as e:
        # print(f"❌ [ERROR] Error in get_user_login_method: {str(e)}")
        return "gmail"  # Default fallback


unified_bp = Blueprint("unified", __name__)


@unified_bp.route("/unified_drafts")
def unified_drafts():
    emails = asyncio.run(get_all_drafts())
    return jsonify(emails)


async def get_all_drafts():

    gmail_task = list_drafts()
    outlook_task = microsoft_list_drafts()
    gmail_emails, outlook_emails = await asyncio.gather(gmail_task, outlook_task)
    return {"gmail": gmail_emails, "outlook": outlook_emails}


def get_latest_msg(content_dict, user_id):

    messages = []

    for msg_id, content_ref in content_dict.items():

        s3_conv_key = content_ref
        raw_data = read_json_from_s3(s3_conv_key)

        # ✅ Fix: Skip if S3 returned None
        if not raw_data:
            print(f"[WARNING] No data found for key: {s3_conv_key}")
            continue

        input_data = raw_data.get("input_data", [])
        if isinstance(input_data, list) and input_data:
            messages.append(input_data[-1])

        elif isinstance(input_data, dict) and input_data:
            messages.append(input_data)

    return messages


@unified_bp.route("/get_active_customers", methods=["POST"])
def get_active_customers():

    data = request.get_json()
    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"error": "user id needed"}), 400

    connection = connect_to_rds()
    if connection is None:
        # print("❌ [DEBUG] Database connection failed")
        return jsonify({"error": "Database connection failed"}), 500
    cursor = connection.cursor()

    content_dict = {}
    query = """
        SELECT 
            m.sender_id,
            m.content_ref as content
        FROM messages m
        JOIN users_clients uc ON m.sender_id = uc.users_clients_id
        JOIN communication c ON uc.communication_id_fk = c.communication_id
        WHERE uc.type = 'Customer'
        AND m.message_type = 'inbound'
        AND m.created_at >= NOW() - INTERVAL 7 DAY
        AND c.user_id_fk = %s
        GROUP BY m.sender_id
        ORDER BY MAX(m.created_at) DESC

        """
    cursor.execute(query, (user_id,))
    rows = cursor.fetchall()

    if rows:
        for row in rows:
            msg_id = row[0]
            content_ref = row[1]
            content_dict[msg_id] = content_ref

    # print(f"content_dict lenght : {len(content_dict)}")
    messages = get_latest_msg(content_dict, user_id)

    return jsonify(messages)


@unified_bp.route("/get_dormant_customers", methods=["POST"])
def get_dormant_customers():

    data = request.get_json()
    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"error": "user id needed"}), 400

    connection = connect_to_rds()
    if connection is None:
        # print("❌ [DEBUG] Database connection failed")
        return jsonify({"error": "Database connection failed"}), 500
    cursor = connection.cursor()

    content_dict = {}
    query = """
        SELECT 
            m.sender_id,
            m.content_ref as content
        FROM messages m
        JOIN users_clients uc ON m.sender_id = uc.users_clients_id
        JOIN communication c ON uc.communication_id_fk = c.communication_id
        WHERE uc.type = 'Customer'
        AND m.message_type = 'inbound'
        AND m.created_at <= NOW() - INTERVAL 7 DAY
        AND c.user_id_fk = %s
        GROUP BY m.sender_id
        ORDER BY MAX(m.created_at) DESC
        """
    cursor.execute(query, (user_id,))
    rows = cursor.fetchall()

    if rows:
        for row in rows:
            msg_id = row[0]
            content_ref = row[1]
            content_dict[msg_id] = content_ref

    # print(f"content_dict lenght : {len(content_dict)}")
    messages = get_latest_msg(content_dict, user_id)

    return jsonify(messages)


@unified_bp.route("/get_active_leads", methods=["POST"])
def get_active_leads():

    data = request.get_json()
    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"error": "user id needed"}), 400

    connection = connect_to_rds()
    if connection is None:
        # print("❌ [DEBUG] Database connection failed")
        return jsonify({"error": "Database connection failed"}), 500
    cursor = connection.cursor()

    content_dict = {}
    query = """
        SELECT 
            m.sender_id,
            m.content_ref as content
        FROM messages m
        JOIN users_clients uc ON m.sender_id = uc.users_clients_id
        JOIN communication c ON uc.communication_id_fk = c.communication_id
        WHERE uc.type = 'Lead'
        AND m.message_type = 'inbound'
        AND m.created_at >= NOW() - INTERVAL 7 DAY
        AND c.user_id_fk = %s
        GROUP BY m.sender_id
        ORDER BY MAX(m.created_at) DESC

        """
    cursor.execute(query, (user_id,))
    rows = cursor.fetchall()

    if rows:
        for row in rows:
            msg_id = row[0]
            content_ref = row[1]
            content_dict[msg_id] = content_ref

    # print(f"content_dict lenght : {len(content_dict)}")
    messages = get_latest_msg(content_dict, user_id)

    return jsonify(messages)


@unified_bp.route("/get_dormant_leads", methods=["POST"])
def get_dormant_leads():

    data = request.get_json()
    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"error": "user id needed"}), 400

    connection = connect_to_rds()
    if connection is None:
        # print("❌ [DEBUG] Database connection failed")
        return jsonify({"error": "Database connection failed"}), 500
    cursor = connection.cursor()

    content_dict = {}
    query = """
        SELECT 
            m.message_id,
            m.content_ref as content
        FROM messages m
        JOIN users_clients uc ON m.sender_id = uc.users_clients_id
        JOIN communication c ON uc.communication_id_fk = c.communication_id
        WHERE uc.type = 'Lead'
        AND m.message_type = 'inbound'
        AND m.created_at <= NOW() - INTERVAL 7 DAY
        AND c.user_id_fk = %s
        GROUP BY m.sender_id
        ORDER BY MAX(m.created_at) DESC
    
        """
    cursor.execute(query, (user_id,))
    rows = cursor.fetchall()

    if rows:
        for row in rows:
            msg_id = row[0]
            content_ref = row[1]
            content_dict[msg_id] = content_ref

    print(f"content_dict lenght : {len(content_dict)}")
    messages = get_latest_msg(content_dict, user_id)

    return jsonify(messages)


@unified_bp.route("/get_snoozed_customers", methods=["POST"])
def get_snoozed_customers():

    try:

        data = request.get_json()
        user_id = data.get("user_id")
        if not user_id:
            return jsonify({"error": "user id needed"}), 400

        connection = connect_to_rds()
        if connection is None:
            # print("❌ [DEBUG] Database connection failed")
            return jsonify({"error": "Database connection failed"}), 500
        cursor = connection.cursor()

        content_dict = {}
        query = """
            SELECT 
                m.sender_id,
                m.content_ref as content
            FROM messages m
            JOIN users_clients uc ON m.sender_id = uc.users_clients_id
            JOIN communication c ON uc.communication_id_fk = c.communication_id
            WHERE uc.snooze=1
            AND c.user_id_fk = %s
            GROUP BY m.sender_id
            ORDER BY MAX(m.created_at) DESC
            """
        cursor.execute(query, (user_id,))
        rows = cursor.fetchall()

        if rows:
            for row in rows:
                msg_id = row[0]
                content_ref = row[1]
                content_dict[msg_id] = content_ref

        # print(f"content_dict lenght : {len(content_dict)}")
        messages = get_latest_msg(content_dict, user_id)

        return jsonify(messages)

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        if cursor:
            cursor.close()


@unified_bp.route("/snooze_customer", methods=["POST"])
def snooze_customer():

    try:
        data = request.get_json()
        user_id = data.get("user_id")
        conversation_id = data.get("conversation_id")
        # print(f"conversation_id :{conversation_id}")
        # print(f"usrs_id: {user_id}")

        connection = connect_to_rds()
        if connection is None:
            # print("❌ [DEBUG] Database connection failed")
            return jsonify({"error": "Database connection failed"}), 500
        cursor = connection.cursor()

        cursor.execute(
            "SELECT sender_id FROM messages WHERE conversation_id_fk = %s",
            (conversation_id,),
        )
        client_id_row = cursor.fetchone()
        if client_id_row:
            client_id = client_id_row[0]

        else:
            return (
                jsonify(
                    {
                        "message": f"⚠️ No sender_id found for conversation_id {conversation_id}"
                    }
                ),
                404,
            )

        # print(f"client_id:{client_id}")

        cursor.execute(
            "UPDATE users_clients SET snooze = CASE WHEN snooze = 0 THEN 1 ELSE 0 END WHERE users_clients_id = %s",
            (client_id,),
        )

        connection.commit()
        return jsonify({"message": "success", "client_id": client_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@unified_bp.route("/get_no_of_customers", methods=["POST"])
def get_no_of_customers():

    try:

        data = request.get_json()
        user_id = data.get("user_id")
        if not user_id:
            return jsonify({"error": "user id needed"}), 400

        connection = connect_to_rds()
        if connection is None:
            # print("❌ [DEBUG] Database connection failed")
            return jsonify({"error": "Database connection failed"}), 500
        cursor = connection.cursor()

        query = """
        SELECT
                -- Active Customers
            COUNT(DISTINCT CASE 
                WHEN uc.type = 'Customer'
                AND m.message_type = 'inbound'
                AND m.created_at >= NOW() - INTERVAL 7 DAY
                THEN m.sender_id END) AS active_customers,

            -- Dormant Customers
            COUNT(DISTINCT CASE 
                WHEN uc.type = 'Customer'
                AND m.message_type = 'inbound'
                AND m.created_at < NOW() - INTERVAL 7 DAY
                THEN m.sender_id END) AS dormant_customers,

            -- Active Leads
            COUNT(DISTINCT CASE 
                WHEN uc.type = 'Lead'
                AND m.message_type = 'inbound'
                AND m.created_at >= NOW() - INTERVAL 7 DAY
                THEN m.sender_id END) AS active_leads,

            -- Dormant Leads
            COUNT(DISTINCT CASE 
                WHEN uc.type = 'Lead'
                AND m.message_type = 'inbound'
                AND m.created_at < NOW() - INTERVAL 7 DAY
                THEN m.sender_id END) AS dormant_leads

        FROM messages m
        JOIN users_clients uc ON m.sender_id = uc.users_clients_id
        JOIN communication c ON uc.communication_id_fk = c.communication_id
        WHERE c.user_id_fk = %s;
        """

        cursor.execute(query, (user_id,))
        row = cursor.fetchone()

        if row:
            active_customers = row[0]
            dormant_customers = row[1]
            active_leads = row[2]
            dormant_leads = row[3]

        # count of snoozed cusomters
        snoozed_cust_query = """ SELECT COUNT(*) AS message_count 
                                FROM users_clients uc 
                                JOIN communication c ON uc.communication_id_fk = c.communication_id 
                                WHERE  uc.snooze=1 
                                AND c.user_id_fk = %s; 
                            """
        cursor.execute(snoozed_cust_query, (user_id,))
        rows = cursor.fetchone()
        if rows:
            snoozed_customers = rows[0]
        return jsonify(
            {
                "active_customers": active_customers,
                "dormant_customers": dormant_customers,
                "snoozed_customers": snoozed_customers,
                "active_leads": active_leads,
                "dormant_leads": dormant_leads,
            }
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


# Notes Related Routes


@unified_bp.route("/create_note", methods=["POST"])
def create_note():
    """Create a new note for a conversation"""
    try:
        # print("[DEBUG] Starting create_note function")
        data = request.get_json()
        # print(f"[DEBUG] create_note received data: {data}")

        if not data:
            # print("[DEBUG] No JSON data received")
            return jsonify({"error": "No JSON data provided"}), 400

        user_id = (data.get("user_id") or "").strip()
        conversation_id = (data.get("conversation_id") or "").strip()
        sender_id = (data.get("sender_id") or "").strip()  # Optional field
        note_content = (data.get("note_content") or "").strip()
        note_type = (data.get("note_type") or "").strip()

        # print(
        #     f"[DEBUG] Extracted fields - user_id: '{user_id}', conversation_id: '{conversation_id}', sender_id: '{sender_id}', note_content: '{note_content}', note_type: '{note_type}'"
        # )

        # Check for required fields (empty strings are invalid)
        if not user_id or not conversation_id or not note_content or not note_type:
            missing_fields = []
            if not user_id:
                missing_fields.append("user_id")
            if not conversation_id:
                missing_fields.append("conversation_id")
            if not note_content:
                missing_fields.append("note_content")
            if not note_type:
                missing_fields.append("note_type")
            # print(f"[DEBUG] Missing required fields: {missing_fields}")
            return (
                jsonify(
                    {"error": f"Missing required fields: {', '.join(missing_fields)}"}
                ),
                400,
            )

        if note_type not in ["private", "shared"]:
            # print(f"[DEBUG] Invalid note type: {note_type}")
            return (
                jsonify({"error": "Invalid note type. Must be 'private' or 'shared'"}),
                400,
            )

        connection = connect_to_rds()
        if connection is None:
            return jsonify({"error": "Database connection failed"}), 500

        cursor = connection.cursor()
        note_id = str(uuid.uuid4())
        created_at = datetime.now()

        # Insert into conversation_notes table
        # print(f"[DEBUG] About to insert note with ID: {note_id}")
        cursor.execute(
            """INSERT INTO conversation_notes 
               (note_id, conversation_id, user_id, sender_id, note_content, note_type, created_at, updated_at, is_active) 
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                note_id,
                conversation_id,
                user_id,
                sender_id,
                note_content,
                note_type,
                created_at,
                created_at,
                True,
            ),
        )
        connection.commit()
        # print("[DEBUG] Note inserted successfully, fetching to verify...")

        # Verify the note was created
        cursor.execute(
            """SELECT note_id, note_content, note_type, is_active 
               FROM conversation_notes 
               WHERE note_id = %s""",
            (note_id,),
        )
        verification = cursor.fetchone()
        # print(f"[DEBUG] Verification result: {verification}")

        return (
            jsonify(
                {
                    "note_id": note_id,
                    "message": "Note created successfully",
                    "note_type": note_type,
                    "is_active": True,
                }
            ),
            201,
        )

    except Exception as e:
        traceback.print_exc()
        # print(f"[DEBUG] Exception in create_note: {str(e)}")
        return jsonify({"error": str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@unified_bp.route("/get_conversation_notes", methods=["POST"])
def get_conversation_notes():
    """Get all notes for a conversation"""
    try:
        # print("[DEBUG] Starting get_conversation_notes function")
        data = request.get_json()
        # print(f"[DEBUG] get_conversation_notes received data: {data}")

        if not data:
            return jsonify({"error": "No JSON data provided"}), 400

        user_id = data.get("user_id")
        conversation_id = data.get("conversation_id")
        # print(f"[DEBUG] user_id: {user_id}, conversation_id: {conversation_id}")

        if not all([user_id, conversation_id]):
            return jsonify({"error": "Missing required fields"}), 400

        connection = connect_to_rds()
        if connection is None:
            # print("[DEBUG] Database connection failed")
            return jsonify({"error": "Database connection failed"}), 500

        cursor = connection.cursor()
        # print("[DEBUG] About to execute SQL query")

        # First check if any notes exist for this conversation at all
        cursor.execute(
            """SELECT COUNT(*) FROM conversation_notes 
               WHERE conversation_id = %s""",
            (conversation_id,),
        )
        total_notes = cursor.fetchone()[0]
        # print(
        #     f"[DEBUG] Total notes found for conversation {conversation_id}: {total_notes}"
        # )

        # Then check how many are active
        cursor.execute(
            """SELECT COUNT(*) FROM conversation_notes 
               WHERE conversation_id = %s AND is_active = TRUE""",
            (conversation_id,),
        )
        active_notes = cursor.fetchone()[0]
        # print(f"[DEBUG] Active notes found: {active_notes}")

        # Get all notes user has access to (private notes of the user + shared notes)
        # print(
        #     f"[DEBUG] Fetching notes for user {user_id} in conversation {conversation_id}"
        # )
        cursor.execute(
            """SELECT n.note_id, n.conversation_id, n.user_id, n.sender_id, n.note_content, n.note_type, n.created_at, n.updated_at,
                      COALESCE(u.first_name, '') as first_name, COALESCE(u.last_name, '') as last_name
               FROM conversation_notes n
               LEFT JOIN users u ON n.user_id = u.user_id
               WHERE n.conversation_id = %s 
               AND (n.user_id = %s OR n.note_type IN ('shared', 'team'))
               AND n.is_active = TRUE
               ORDER BY n.created_at DESC""",
            (conversation_id, user_id),
        )

        # print("[DEBUG] SQL query executed successfully")
        rows = cursor.fetchall()
        # print(f"[DEBUG] Found {len(rows)} rows")

        notes = []
        for row in rows:
            note_id = row[0]
            note_user_id = row[2]
            sender_id = row[3]
            content = row[4]
            note_type = row[5]
            created_at = row[6].isoformat() if row[6] else None
            updated_at = row[7].isoformat() if row[7] else None
            first_name = row[8] or ""
            last_name = row[9] or ""
            author_name = f"{first_name} {last_name}".strip() or note_user_id

            notes.append(
                {
                    "note_id": note_id,
                    "user_id": note_user_id,
                    "sender_id": sender_id,
                    "content": content,
                    "type": note_type,
                    "created_at": created_at,
                    "updated_at": updated_at,
                    "author": author_name,
                }
            )

        # print(f"[DEBUG] Returning {len(notes)} notes")
        return jsonify({"notes": notes})

    except Exception as e:
        # print(f"[DEBUG] Exception in get_conversation_notes: {str(e)}")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@unified_bp.route("/update_note", methods=["POST"])
def update_note():
    """Update an existing note"""
    data = request.get_json()
    note_id = data.get("note_id")
    user_id = data.get("user_id")
    note_content = data.get("note_content")

    if not all([note_id, user_id, note_content]):
        return jsonify({"error": "Missing required fields"}), 400

    try:
        connection = connect_to_rds()
        if connection is None:
            return jsonify({"error": "Database connection failed"}), 500

        cursor = connection.cursor()

        # Check if user has permission to edit the note
        cursor.execute(
            """SELECT user_id, note_content FROM conversation_notes 
               WHERE note_id = %s AND is_active = TRUE""",
            (note_id,),
        )
        note = cursor.fetchone()

        if not note:
            return jsonify({"error": "Note not found"}), 404

        note_owner = note[0]
        old_content = note[1]

        # Check user permissions
        if note_owner != user_id:
            cursor.execute(
                """SELECT permission_type FROM note_permissions 
                   WHERE note_id = %s AND user_id = %s AND is_active = TRUE""",
                (note_id, user_id),
            )
            permission = cursor.fetchone()
            if not permission or permission[0] not in ["write", "admin"]:
                return jsonify({"error": "Permission denied"}), 403

        # Update the note
        cursor.execute(
            "UPDATE conversation_notes SET note_content = %s, updated_at = %s WHERE note_id = %s",
            (note_content, datetime.now(), note_id),
        )

        # Log the change in note_history
        cursor.execute(
            """INSERT INTO note_history 
               (history_id, note_id, user_id, action, old_content, new_content, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                str(uuid.uuid4()),
                note_id,
                user_id,
                "updated",
                old_content,
                note_content,
                datetime.now(),
            ),
        )

        connection.commit()
        return jsonify({"message": "Note updated successfully"})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@unified_bp.route("/delete_note", methods=["POST"])
def delete_note():
    """Soft delete a note"""
    data = request.get_json()
    note_id = data.get("note_id")
    user_id = data.get("user_id")

    if not all([note_id, user_id]):
        return jsonify({"error": "Missing required fields"}), 400

    try:
        connection = connect_to_rds()
        if connection is None:
            return jsonify({"error": "Database connection failed"}), 500

        cursor = connection.cursor()

        # Check if user has permission to delete the note
        cursor.execute(
            """SELECT user_id, note_content FROM conversation_notes 
               WHERE note_id = %s AND is_active = TRUE""",
            (note_id,),
        )
        note = cursor.fetchone()

        if not note:
            return jsonify({"error": "Note not found"}), 404

        note_owner = note[0]
        old_content = note[1]

        # Check user permissions
        if note_owner != user_id:
            cursor.execute(
                """SELECT permission_type FROM note_permissions 
                   WHERE note_id = %s AND user_id = %s AND is_active = TRUE""",
                (note_id, user_id),
            )
            permission = cursor.fetchone()
            if not permission or permission[0] != "admin":
                return jsonify({"error": "Permission denied"}), 403

        # Soft delete the note
        cursor.execute(
            "UPDATE conversation_notes SET is_active = FALSE, updated_at = %s WHERE note_id = %s",
            (datetime.now(), note_id),
        )

        # Log the deletion in note_history
        cursor.execute(
            """INSERT INTO note_history 
               (history_id, note_id, user_id, action, old_content, new_content, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                str(uuid.uuid4()),
                note_id,
                user_id,
                "deleted",
                old_content,
                None,
                datetime.now(),
            ),
        )

        connection.commit()
        return jsonify({"message": "Note deleted successfully"})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@unified_bp.route("/search_users_for_sharing", methods=["POST"])
def search_users_for_sharing():
    """Search for users by name or email"""
    data = request.get_json()
    user_id = data.get("user_id")
    search_query = data.get("search_query")

    # Validate required fields - check for None, empty string, and falsy values
    if not user_id or not search_query:
        return (
            jsonify({"error": "Missing required fields: user_id and search_query"}),
            400,
        )

    # Ensure search_query is a string and strip whitespace
    search_query = str(search_query).strip()
    if not search_query:
        return jsonify({"error": "Search query cannot be empty"}), 400

    try:
        connection = connect_to_rds()
        if connection is None:
            return jsonify({"error": "Database connection failed"}), 500

        cursor = connection.cursor()

        # Search by name or email - safely convert to lowercase
        search_term = f"%{search_query.lower()}%"
        cursor.execute(
            """SELECT user_id, first_name, last_name, email 
               FROM users 
               WHERE user_id != %s 
               AND (LOWER(first_name) LIKE %s 
                    OR LOWER(last_name) LIKE %s
                    OR LOWER(email) LIKE %s)
               LIMIT 10""",
            (user_id, search_term, search_term, search_term),
        )

        users = []
        for row in cursor.fetchall():
            current_user_id = row[0]
            first_name = row[1]
            last_name = row[2]
            email = row[3]

            # 🔴 DEFENSIVE CHECK: Ensure current user never appears
            # (belt and suspenders - query should already exclude them)
            if current_user_id == user_id:
                # print(
                #     f"[DEBUG] WARNING: Current user {user_id} appeared in search results. Skipping."
                # )
                continue

            users.append(
                {
                    "user_id": current_user_id,
                    "name": f"{first_name or ''} {last_name or ''}".strip(),
                    "email": email,
                }
            )

        return jsonify({"users": users})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


def check_user_sharing_permissions(cursor, user_id, target_email):
    """Check if a user has permission to share notes with the target email"""
    # Get user info
    cursor.execute(
        "SELECT user_type, permissions FROM users WHERE user_id = %s", (user_id,)
    )
    user_data = cursor.fetchone()

    if not user_data:
        return False, "User not found"

    user_type, permissions_json = user_data

    try:
        permissions = json.loads(permissions_json)
        shared_items = permissions.get("shared", [])
    except (json.JSONDecodeError, AttributeError):
        return False, "Invalid permissions format"

    # Admin can share with anyone in their team
    if user_type == "admin":
        for item in shared_items:
            if (
                item.get("role", {}).get("permissions", [])
                and "mailbox" in item["role"]["permissions"]
                and item.get("email") == target_email
            ):
                return True, "Admin sharing with team member"
        return False, "Target user not in admin's team"

    # Regular user - check if target is in their team
    elif user_type == "user":
        # Get the admin who invited this user
        invited_by_emails = []
        for item in shared_items:
            if (
                "mailbox" in item.get("role", {}).get("permissions", [])
                and "invited_by" in item
            ):
                invited_by_emails.append(item["invited_by"])

        # Check each admin's team
        for invited_email in invited_by_emails:
            cursor.execute(
                "SELECT permissions FROM users WHERE email = %s AND user_type = 'admin'",
                (invited_email,),
            )
            admin_data = cursor.fetchone()

            if admin_data:
                try:
                    admin_permissions = json.loads(admin_data[0])
                    admin_shared = admin_permissions.get("shared", [])

                    # Check if target user is in the same team
                    for item in admin_shared:
                        if (
                            item.get("role", {}).get("permissions", [])
                            and "mailbox" in item["role"]["permissions"]
                            and item.get("email") == target_email
                        ):
                            return True, "User sharing with team member"
                except:
                    continue

        return False, "Target user not in same team"

    return False, "Invalid user type"


@unified_bp.route("/share_note_by_email", methods=["POST"])
def share_note_by_email():
    """Share a note with another user based on hierarchical permissions"""
    data = request.get_json()
    note_id = data.get("note_id")
    user_id = data.get("user_id")
    share_with_email = data.get("share_with_email")
    permission_type = data.get("permission_type")

    if not all([note_id, user_id, share_with_email, permission_type]):
        return jsonify({"error": "Missing required fields"}), 400

    if permission_type not in ["read", "write", "admin"]:
        return (
            jsonify(
                {
                    "error": "Invalid permission type. Must be 'read', 'write', or 'admin'"
                }
            ),
            400,
        )

    try:
        connection = connect_to_rds()
        if connection is None:
            return jsonify({"error": "Database connection failed"}), 500

        cursor = connection.cursor()

        # Check hierarchical sharing permissions
        can_share, message = check_user_sharing_permissions(
            cursor, user_id, share_with_email
        )
        if not can_share:
            return jsonify({"error": f"Permission denied: {message}"}), 403

        # Check if user has permission to share the note
        cursor.execute(
            """SELECT user_id FROM conversation_notes 
               WHERE note_id = %s AND is_active = TRUE""",
            (note_id,),
        )
        note = cursor.fetchone()

        if not note:
            return jsonify({"error": "Note not found"}), 404

        note_owner = note[0]

        # Only note owner or users with admin permission can share
        if note_owner != user_id:
            cursor.execute(
                """SELECT permission_type FROM note_permissions 
                   WHERE note_id = %s AND user_id = %s AND is_active = TRUE""",
                (note_id, user_id),
            )
            permission = cursor.fetchone()
            if not permission or permission[0] != "admin":
                return jsonify({"error": "Permission denied - not note owner"}), 403

        # Get target user ID
        cursor.execute(
            "SELECT user_id FROM users WHERE email = %s", (share_with_email,)
        )
        target_user = cursor.fetchone()
        if not target_user:
            return jsonify({"error": "Target user not found"}), 404

        target_user_id = target_user[0]

        # Update or create permission
        cursor.execute(
            """SELECT permission_id FROM note_permissions 
               WHERE note_id = %s AND user_id = %s AND is_active = TRUE""",
            (note_id, target_user_id),
        )
        existing = cursor.fetchone()

        if existing:
            cursor.execute(
                """UPDATE note_permissions 
                   SET permission_type = %s, granted_at = %s 
                   WHERE note_id = %s AND user_id = %s""",
                (permission_type, datetime.now(), note_id, target_user_id),
            )
        else:
            permission_id = str(uuid.uuid4())
            cursor.execute(
                """INSERT INTO note_permissions 
                   (permission_id, note_id, user_id, permission_type, granted_by, granted_at, is_active)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (
                    permission_id,
                    note_id,
                    target_user_id,
                    permission_type,
                    user_id,
                    datetime.now(),
                    True,
                ),
            )

        connection.commit()
        return jsonify(
            {
                "message": "Note shared successfully",
                "shared_with": {"user_id": target_user_id, "email": share_with_email},
            }
        )

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@unified_bp.route("/get_note_permissions", methods=["POST"])
def get_note_permissions():
    """Get all users who have permissions for a note"""
    data = request.get_json()
    note_id = data.get("note_id")
    user_id = data.get("user_id")

    if not all([note_id, user_id]):
        return jsonify({"error": "Missing required fields"}), 400

    try:
        connection = connect_to_rds()
        if connection is None:
            return jsonify({"error": "Database connection failed"}), 500

        cursor = connection.cursor()

        # Check if user has access to the note
        cursor.execute(
            """SELECT user_id FROM conversation_notes 
               WHERE note_id = %s AND is_active = TRUE""",
            (note_id,),
        )
        note = cursor.fetchone()

        if not note:
            return jsonify({"error": "Note not found"}), 404

        note_owner = note[0]

        if note_owner != user_id:
            cursor.execute(
                """SELECT permission_type FROM note_permissions 
                   WHERE note_id = %s AND user_id = %s AND is_active = TRUE""",
                (note_id, user_id),
            )
            permission = cursor.fetchone()
            if not permission:
                return jsonify({"error": "Permission denied"}), 403

        # Get all users who have access to the note
        cursor.execute(
            """SELECT u.user_id, u.first_name, u.last_name, u.email, 
                      np.permission_type, np.granted_by, np.granted_at
               FROM note_permissions np
               JOIN users u ON np.user_id = u.user_id
               WHERE np.note_id = %s AND np.is_active = TRUE""",
            (note_id,),
        )

        permissions = []
        for row in cursor.fetchall():
            permissions.append(
                {
                    "user_id": row[0],
                    "name": f"{row[1] or ''} {row[2] or ''}".strip(),
                    "email": row[3],
                    "permission_type": row[4],
                    "granted_by": row[5],
                    "granted_at": row[6].isoformat() if row[6] else None,
                }
            )

        return jsonify({"note_owner": note_owner, "shared_with": permissions})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@unified_bp.route("/change_assignee", methods=["POST"])
def change_assignee():

    data = request.get_json()
    assignee_user_id = data.get("assignee_id")
    tickets_id = data.get("ticket_id")
    # print(f"assignee_user_id :{assignee_user_id}")
    # print(f"tickets_id : {tickets_id}")

    if not assignee_user_id:
        return jsonify({"error": "user id needed"}), 400
    if not tickets_id:
        return jsonify({"error": "tickets_id needed"}), 400

    connection = connect_to_rds()
    if connection is None:
        # print("❌ [DEBUG] Database connection failed")
        return jsonify({"error": "Database connection failed"}), 500
    cursor = connection.cursor()

    cursor.execute(
        "UPDATE tickets SET assignee = %s WHERE tickets_id = %s ",
        (assignee_user_id, tickets_id),
    )

    connection.commit()

    return jsonify({"message": "success"}), 200


def add_user_by_id(cursor, result_emails, user_id_val):
    """Helper to add user details by user_id"""
    try:
        cursor.execute(
            "SELECT first_name, last_name, user_id FROM users WHERE user_id = %s",
            (user_id_val,),
        )
        names = cursor.fetchone()

        if names:
            first_name = names[0] or ""
            last_name = names[1] or ""
            user_id_result = names[2]

            # Handle NULL first names
            if not first_name or first_name == "None":
                # Try to get email as fallback
                cursor.execute(
                    "SELECT email FROM users WHERE user_id = %s", (user_id_result,)
                )
                email_row = cursor.fetchone()
                first_name = email_row[0].split("@")[0] if email_row else "User"

            if not last_name or last_name == "None":
                last_name = ""

            details = {
                "name": f"{first_name} {last_name}".strip(),
                "id": user_id_result,
            }
            result_emails.append(details)
            # print(f"[DEBUG] Added user by ID: {details}")
    except Exception as e:
        print(f"[ERROR] add_user_by_id failed: {e}")

    return result_emails


def add_user_by_email(cursor, result_emails, email):
    """Helper to add user details by email"""
    try:
        cursor.execute(
            "SELECT first_name, last_name, user_id FROM users WHERE email = %s",
            (email,),
        )
        names = cursor.fetchone()

        if names:
            first_name = names[0] or ""
            last_name = names[1] or ""
            user_id_result = names[2]

            if not first_name or first_name == "None":
                first_name = email.split("@")[0]

            if not last_name or last_name == "None":
                last_name = ""

            details = {
                "name": f"{first_name} {last_name}".strip(),
                "id": user_id_result,
            }
            result_emails.append(details)
            # print(f"[DEBUG] Added user by email: {details}")
    except Exception as e:
        print(f"[ERROR] add_user_by_email failed: {e}")

    return result_emails


@unified_bp.route("/get_assignee_list", methods=["POST"])
def get_assignee_list():
    """Get list of assignees (team members) for current user"""
    result_emails = []
    data = request.get_json()
    user_id = data.get("user_id")

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    connection = connect_to_rds()
    if connection is None:
        # print("❌ [DEBUG] Database connection failed")
        return jsonify({"error": "Database connection failed"}), 500

    cursor = connection.cursor()

    try:
        # Get user data
        # print(f"[DEBUG] /get_assignee_list called for user: {user_id}")
        cursor.execute(
            "SELECT user_type, permissions FROM users WHERE user_id = %s", (user_id,)
        )
        user_data = cursor.fetchone()

        if not user_data:
            # print(f"[DEBUG] User {user_id} not found")
            return jsonify({"assignees": []})

        user_type, permissions_json = user_data
        # print(f"[DEBUG] user_type={user_type}")

        # Parse permissions safely
        try:
            if permissions_json:
                permissions = json.loads(permissions_json)
                shared_items = permissions.get("shared", [])
            else:
                shared_items = []
            # print(f"[DEBUG] Parsed {len(shared_items)} shared items")
        except (json.JSONDecodeError, AttributeError, TypeError) as parse_err:
            # print(f"[DEBUG] Permission parsing failed: {parse_err}")
            shared_items = []

        # Helper to check mailbox permission
        def has_mailbox_permission(item):
            return (
                item.get("role", {}).get("permissions", [])
                and "mailbox" in item["role"]["permissions"]
            )

        # === CASE 1: Admin User ===
        if user_type == "admin":
            # print(f"[DEBUG] Processing admin user {user_id}")

            # Add self (the admin)
            result_emails = add_user_by_id(cursor, result_emails, user_id)

            # Add all team members
            for item in shared_items:
                if has_mailbox_permission(item) and "email" in item:
                    email = item["email"]
                    # print(f"[DEBUG] Adding admin team member: {email}")
                    result_emails = add_user_by_email(cursor, result_emails, email)

        # === CASE 2: Regular User ===
        elif user_type == "user":
            # print(f"[DEBUG] Processing regular user {user_id}")

            # Add self
            result_emails = add_user_by_id(cursor, result_emails, user_id)

            # Find who invited this user (their admin)
            invited_by_emails = []
            for item in shared_items:
                if has_mailbox_permission(item) and "invited_by" in item:
                    invited_by_emails.append(item["invited_by"])

            # print(f"[DEBUG] User invited by: {invited_by_emails}")

            # Get team members from the admin(s) who invited them
            for invited_email in invited_by_emails:
                cursor.execute(
                    "SELECT user_type, permissions FROM users WHERE email = %s",
                    (invited_email,),
                )
                invited_data = cursor.fetchone()

                if invited_data and invited_data[0] == "admin":
                    try:
                        # Parse admin's permissions
                        if invited_data[1]:
                            invited_permissions = json.loads(invited_data[1])
                            invited_shared = invited_permissions.get("shared", [])
                        else:
                            invited_shared = []

                        # Add all team members from this admin
                        for item in invited_shared:
                            if has_mailbox_permission(item) and "email" in item:
                                email = item["email"]
                                # print(f"[DEBUG] Adding team member: {email}")
                                result_emails = add_user_by_email(
                                    cursor, result_emails, email
                                )
                    except Exception as e:
                        # print(f"[ERROR] Failed to process admin {invited_email}: {e}")
                        continue

        # print(f"[DEBUG] Returning {len(result_emails)} assignees")
        return jsonify({"assignees": result_emails}), 200

    except Exception as e:
        # print(f"[ERROR] Exception in get_assignee_list: {e}")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@unified_bp.route("/get_user_notes", methods=["POST"])
def get_user_notes():
    """Get all notes for a user including those shared with them, grouped by conversation"""
    # print("[DEBUG] Starting get_user_notes function")
    data = request.get_json()
    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"error": "user id needed"}), 400

    # print(f"[DEBUG] Processing request for user_id: {user_id}")
    val = get_notes_data(user_id)
    if "error" in val:
        return jsonify(val), 500
    return jsonify(val)


@unified_bp.route("/check_notes_tables", methods=["GET"])
def check_notes_tables():
    """Check the structure of existing notes tables"""
    try:
        connection = connect_to_rds()
        if connection is None:
            return jsonify({"error": "Database connection failed"}), 500

        cursor = connection.cursor()

        # Check conversation_notes table structure
        cursor.execute("DESCRIBE conversation_notes")
        conversation_notes_structure = [
            {
                "Field": row[0],
                "Type": row[1],
                "Null": row[2],
                "Key": row[3],
                "Default": row[4],
                "Extra": row[5],
            }
            for row in cursor.fetchall()
        ]

        # Check note_permissions table structure
        cursor.execute("DESCRIBE note_permissions")
        note_permissions_structure = [
            {
                "Field": row[0],
                "Type": row[1],
                "Null": row[2],
                "Key": row[3],
                "Default": row[4],
                "Extra": row[5],
            }
            for row in cursor.fetchall()
        ]

        # Check note_history table structure
        cursor.execute("DESCRIBE note_history")
        note_history_structure = [
            {
                "Field": row[0],
                "Type": row[1],
                "Null": row[2],
                "Key": row[3],
                "Default": row[4],
                "Extra": row[5],
            }
            for row in cursor.fetchall()
        ]

        # Count existing records
        cursor.execute("SELECT COUNT(*) FROM conversation_notes")
        notes_count = cursor.fetchone()[0]

        return jsonify(
            {
                "conversation_notes_structure": conversation_notes_structure,
                "note_permissions_structure": note_permissions_structure,
                "note_history_structure": note_history_structure,
                "total_notes": notes_count,
            }
        )

    except Exception as e:
        return jsonify({"error": str(e), "traceback": traceback.format_exc()}), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()
