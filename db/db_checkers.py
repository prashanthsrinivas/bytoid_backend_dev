import json

from .rds_db import connect_to_rds, get_cursor
from datetime import datetime, timezone, timedelta
import uuid
import pymysql


def fetch_userid_from_launch(apikey, connection=None):
    """
    Fetches user_id_fk from 'launch' table using the provided API key.
    """
    own_connection = False
    try:
        if connection is None:
            connection = connect_to_rds()
            own_connection = True

        with connection.cursor() as cursor:
            cursor.execute("SELECT user_id_fk FROM launch WHERE api_id = %s", (apikey,))
            result = cursor.fetchone()
            return result[0] if result else None

    except Exception as e:
        print(f"Error fetching user ID from launch: {e}")
        return None
    finally:
        if own_connection and connection:
            connection.close()


def fetch_apikey_from_launch(userid, connection=None):
    """
    Fetches api_id from 'launch' table using the user_id.
    """
    own_connection = False
    try:
        if connection is None:
            connection = connect_to_rds()
            own_connection = True

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT api_id FROM launch WHERE user_id_fk = %s LIMIT 1", (userid,)
            )
            result = cursor.fetchone()
            return result[0] if result else None

    except Exception as e:
        print(f"Error fetching API key from launch: {e}")
        return None
    finally:
        if own_connection and connection:
            connection.close()


def check_userid_valid(userid, connection=None):
    """
    Checks if user_id exists in the 'users' table.
    Rules:
    - If user does not exist → return False
    - If user_type != 'user' → return True
    - If user_type == 'user' → check permissions['status'] == 'active'
    """
    own_connection = False
    try:
        if connection is None:
            connection = connect_to_rds()
            own_connection = True

        with connection.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                "SELECT user_type, permissions FROM users WHERE user_id = %s",
                (userid,),
            )
            row = cursor.fetchone()

            if not row:
                return False  # user not found

            user_type = row.get("user_type")
            permissions = row.get("permissions")

            if user_type != "user":
                return True  # owner/admin/other valid roles

            # user_type == "user" → need to check permissions.status
            try:
                permissions_data = json.loads(permissions) if permissions else {}
            except Exception:
                permissions_data = {}

            status = permissions_data.get("status")
            print(f"user access {userid}", status)
            return status == "active"

    except Exception as e:
        print(f"Error checking user ID validity: {e}")
        return False

    finally:
        if own_connection and connection:
            connection.close()


def check_onboarding_user(userid, connection=None):
    """
    Checks if the user has onboarding data in business_info table.
    Returns True if onboarded, False otherwise.
    """
    own_connection = False
    try:
        if connection is None:
            connection = connect_to_rds()
            own_connection = True

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT business_info_id FROM business_info WHERE user_id_fk = %s",
                (userid,),
            )
            result = cursor.fetchone()
            return bool(result and result[0])

    except Exception as e:
        print(f"Error checking onboarding status: {e}")
        return False
    finally:
        if own_connection and connection:
            connection.close()


def get_line_of_business(userid, connection=None):
    """
    Returns LineOfBusiness for the given user_id.
    Returns None if not found or on error.
    """
    own_connection = False
    try:
        if connection is None:
            connection = connect_to_rds()
            own_connection = True

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT LineOfBusiness FROM business_info WHERE user_id_fk = %s",
                (userid,),
            )
            val = cursor.fetchone()
            return val[0] if val else None

    except Exception as e:
        print(f"DB error during LineOfBusiness fetch: {e}")
        return None
    finally:
        if own_connection and connection:
            connection.close()


def get_subagent_by_userid(userid, connection=None):
    """
    Returns sub_agent_id_fk for the given user_id.
    """
    own_connection = False
    try:
        if connection is None:
            connection = connect_to_rds()
            own_connection = True

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT sub_agent_id_fk FROM launch WHERE user_id_fk = %s", (userid,)
            )
            result = cursor.fetchone()
            return result[0] if result else None

    except Exception as e:
        print(f"Error fetching sub_agent_id_fk: {e}")
        return None
    finally:
        if own_connection and connection:
            connection.close()


def check_subagent_by_playbook(subagentid, connection=None):
    """
    Returns (playbook_id, file_path) for the given sub_agent_id.
    """
    own_connection = False
    try:
        if connection is None:
            connection = connect_to_rds()
            own_connection = True

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT playbook_id, file_path FROM playbook WHERE sub_agent_id = %s",
                (subagentid,),
            )
            result = cursor.fetchone()
            return (result[0], result[1]) if result else (None, None)

    except Exception as e:
        print(f"Error fetching playbook: {e}")
        return None, None
    finally:
        if own_connection and connection:
            connection.close()


def create_subagent_to_playbook(
    playbook_id, subagent_id, config_s3_path, connection=None
):
    own_connection = False
    try:
        if connection is None:
            connection = connect_to_rds()
            own_connection = True
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO playbook (playbook_id, sub_agent_id, file_path, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s)
            """,
                (
                    playbook_id,
                    subagent_id,
                    config_s3_path,
                    datetime.utcnow(),
                    datetime.utcnow(),
                ),
            )

            connection.commit()
            print(f"✅ New playbook created: {playbook_id}")

        return playbook_id, config_s3_path
    except Exception as e:
        # Optional: logging
        print(f"Error fetching user ID from launch table: {e}")
        return None, None
    finally:
        if own_connection and connection:
            connection.close()


def create_ticket_Communication_assigned(
    communication_id, priority, status, connection=None
):
    own_connection = False
    try:
        if connection is None:
            connection = connect_to_rds()
            own_connection = True
        with connection.cursor() as cursor:

            # 1. Find the user associated with the communication
            cursor.execute(
                "SELECT users_clients_id FROM users_clients WHERE communication_id_fk = %s",
                (communication_id,),
            )
            clients_id_row = cursor.fetchone()
            if not clients_id_row:
                return None

            clients_id = clients_id_row[0]  # fetchone() returns a tuple

            # 2. Create the ticket
            ticket_id = str(uuid.uuid4())
            cursor.execute(
                """
                INSERT INTO tickets (tickets_id, communication_id_fk, priority, status, created_in, updated_in)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    ticket_id,
                    communication_id,
                    priority,
                    status,
                    datetime.utcnow(),
                    datetime.utcnow(),
                ),
            )

            # 3. Assign the ticket to the user
            cursor.execute(
                """
                INSERT INTO assigned(users_clients_id, ticket_id_fk)
                VALUES (%s, %s)
                """,
                (clients_id, ticket_id),
            )

            # 4. Commit changes
            connection.commit()

        return ticket_id

    except Exception as e:
        print(f"Error creating ticket and assignment: {e}")
        return None

    finally:
        if own_connection and connection:
            connection.close()


def updateTicketConversation(conversation_id, ticket_id, connection=None):
    own_connection = False
    try:
        if connection is None:
            connection = connect_to_rds()
            own_connection = True

        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE threads
                SET ticket_id_fk = %s
                WHERE conversation_id = %s
                """,
                (ticket_id, conversation_id),
            )

        connection.commit()
        return {"status": "success", "message": "Ticket ID updated successfully"}

    except Exception as e:
        print(f"Error updating ticket_id_fk: {e}")
        return {"status": "error", "message": str(e)}

    finally:
        if own_connection and connection:
            connection.close()


def get_userinfo(userid, connection=None):
    from google_route.routes import get_token

    own_connection = False
    try:
        if connection is None:
            connection = connect_to_rds()
            own_connection = True

        with connection.cursor() as cursor:
            query = """
                SELECT
                    u.first_name,
                    u.last_name,
                    u.email,
                    u.sociallinks,
                    b.business_info_id,
                    b.BusinessName,
                    b.LineOfBusiness,
                    b.BillingAddress,
                    b.BusinessEmail,
                    b.WebsiteUrl
                FROM users u
                LEFT JOIN business_info b ON u.user_id = b.user_id_fk
                WHERE u.user_id = %s
            """
            cursor.execute(query, (userid,))
            row = cursor.fetchone()

        connection.commit()

        if not row:
            return {"status": "error", "message": "User not found"}

        field_names = [
            "first_name",
            "last_name",
            "email",
            "sociallinks",
            "business_info_id",
            "BusinessName",
            "LineOfBusiness",
            "BillingAddress",
            "BusinessEmail",
            "WebsiteUrl",
        ]
        token_access = get_token(userid, value=True, in_connection=connection)
        result = dict(zip(field_names, row))

        # 🔽 Decode sociallinks JSON string
        if result.get("sociallinks"):
            try:
                result["sociallinks"] = json.loads(result["sociallinks"])
            except Exception as e:
                # print("Could not decode sociallinks:", e)
                result["sociallinks"] = {}
        if token_access:
            result["token"] = token_access

        return result

    except Exception as e:
        print(f"Error fetching user info: {e}")
        return {"status": "error", "message": str(e)}

    finally:
        if own_connection and connection:
            connection.close()


def fetch_contacts_by_user(userid):
    """
    Fetch all contacts (name, gmail) linked to a user_id through communication.
    """

    print(f"User ID for getting contacts: {userid}")
    connection = connect_to_rds()
    with connection.cursor() as cursor:
        query = """
            SELECT uc.users_clients_id, uc.first_name, uc.last_name, uc.email_id
            FROM users_clients uc
            JOIN communication c
                ON uc.communication_id_fk = c.communication_id
            WHERE c.user_id_fk = %s
        """
        cursor.execute(query, (userid,))
        rows = cursor.fetchall()

        contacts = []
        for (
            users_clients_id,
            first_name,
            last_name,
            email,
        ) in rows:
            full_name = (
                f"{(first_name or '').strip()} {(last_name or '').strip()}".strip()
            )
            if full_name or email:
                contacts.append(
                    {"id": users_clients_id, "name": full_name, "email": email}
                )
    connection.close()
    return contacts


def fetch_document_link(agent_id):
    """
    Fetch all contacts (name, gmail) linked to a user_id through communication.
    """

    print(f"User ID for getting doc links: {agent_id}")
    connection = connect_to_rds()
    with connection.cursor() as cursor:
        # agent_id = get_subagent_by_userid(userid, connection)
        # if not agent_id:
        #     return []
        query = """
            SELECT documentation_link from subagents
            WHERE sub_agent_id = %s
        """
        cursor.execute(query, (agent_id,))
        row = cursor.fetchone()
    connection.close()
    return row[0] if row and row[0] else None


def update_agent_document_link(new_link: str, agent_id):
    """
    Update the documentation_link for a given sub_agent_id.
    """
    print(f"Updating documentation link for agent ID: {agent_id}")
    connection = connect_to_rds()
    try:
        # agent_id = get_subagent_by_userid(userid, connection)
        with connection.cursor() as cursor:
            query = """
                UPDATE subagents
                SET documentation_link = %s
                WHERE sub_agent_id = %s
            """
            cursor.execute(query, (new_link, agent_id))
        connection.commit()
        return True
    except Exception as e:
        print(f"Error updating documentation link: {e}")
        return False
    finally:
        connection.close()


def delete_agent_document_link(agent_id):
    """
    Update the documentation_link for a given sub_agent_id.
    """
    print(f"Updating documentation link for agent ID: {agent_id}")
    connection = connect_to_rds()
    try:
        # agent_id = get_subagent_by_userid(userid, connection)
        with connection.cursor() as cursor:
            query = """
                UPDATE subagents
                SET documentation_link = %s
                WHERE sub_agent_id = %s
            """
            cursor.execute(query, (None, agent_id))
        connection.commit()
        return True
    except Exception as e:
        print(f"Error updating documentation link: {e}")
        return False
    finally:
        connection.close()


def get_user_agent_id(apikey):
    """
    Fetch user_id_fk and sub_agent_id_fk for a given api_id.
    Returns a tuple (user_id_fk, sub_agent_id_fk) if found, else None.
    """
    print(f"Fetching user/sub_agent IDs for API key: {apikey}")
    connection = connect_to_rds()
    try:
        with connection.cursor() as cursor:
            query = """
                SELECT user_id_fk, sub_agent_id_fk FROM launch
                WHERE api_id = %s
            """
            cursor.execute(query, (apikey,))
            result = cursor.fetchone()  # get single record

        if result:
            return result  # (user_id_fk, sub_agent_id_fk)
        else:
            return None
    except Exception as e:
        print(f"Error fetching user/sub_agent IDs: {e}")
        return None
    finally:
        connection.close()


def get_existing_umail_json(user_id, connection=None):
    """Fetch existing umail_json for a user."""
    try:
        # print("----inside get_existing_umail_json")
        own_conn = False
        if connection is None:
            connection = connect_to_rds()
            own_conn = True

        with get_cursor(connection) as cursor:
            cursor.execute(
                "SELECT umail_json FROM users WHERE user_id = %s", (user_id,)
            )
            row = cursor.fetchone()

        if row and row[0]:
            try:
                return json.loads(row[0])
            except Exception:
                return None

        else:
            # try to get from integrations table:
            with get_cursor(connection) as cursor:
                cursor.execute(
                    "SELECT umail_json FROM integrations WHERE user_id = %s", (user_id,)
                )
                row = cursor.fetchone()

            if row and row[0]:
                try:
                    return json.loads(row[0])
                except Exception:
                    return None

        return None
    finally:
        if own_conn:
            connection.close()


def get_existing_umail_json_integration(user_id, connection=None):
    """Fetch existing umail_json for a user."""
    try:
        # print("----inside get_existing_umail_json")
        own_conn = False
        if connection is None:
            connection = connect_to_rds()
            own_conn = True

        with get_cursor(connection) as cursor:
            cursor.execute(
                "SELECT umail_json FROM integrations WHERE user_id = %s", (user_id,)
            )
            row = cursor.fetchone()

        if row and row[0]:
            try:
                return json.loads(row[0])
            except Exception:
                return None
        return None
    finally:
        if own_conn:
            connection.close()


def update_umail_json(user_id, new_count, connection=None, keep_days=10):
    """
    Update umail_json for a user:
      - Keep max `keep_days` days of history.
      - Merge counts into today's entry instead of creating duplicates.
      - `processed_threads` = today's total.
      - `timestamp` = last update time.

    Args:
        new_count (int): number of new threads processed.
    """
    own_conn = False
    if connection is None:
        connection = connect_to_rds()
        connection.autocommit(True)
        own_conn = True

    try:
        existing = get_existing_umail_json(user_id, connection) or {}
        if "history" not in existing or not isinstance(existing["history"], list):
            existing["history"] = []

        now = datetime.now(timezone.utc)
        today = now.date()

        # remove entries older than keep_days
        cutoff_date = today - timedelta(days=keep_days)
        filtered_history = []
        for h in existing["history"]:
            ts = h.get("timestamp")
            if not ts:
                continue
            try:
                ts_date = datetime.fromisoformat(ts).date()
                if ts_date >= cutoff_date:
                    filtered_history.append(h)
            except Exception:
                continue

        # merge into today's entry if exists
        today_entry = None
        for h in filtered_history:
            ts = h.get("timestamp")
            if ts and datetime.fromisoformat(ts).date() == today:
                today_entry = h
                break

        if today_entry:
            today_entry["processed_threads"] = new_count
            today_entry["timestamp"] = now.isoformat()
            today_entry["date_end"] = today.isoformat()
        else:
            today_entry = {
                "date_start": today.isoformat(),
                "date_end": today.isoformat(),
                "timestamp": now.isoformat(),
                "newly_creation": False,
                "processed_threads": new_count,
            }
            filtered_history.append(today_entry)

        # sort history by date
        filtered_history.sort(key=lambda h: h.get("timestamp", ""))

        # update the JSON
        existing["history"] = filtered_history
        existing["processed_threads"] = today_entry["processed_threads"]
        existing["timestamp"] = now.isoformat()

        # save back
        with get_cursor(connection) as cursor:
            cursor.execute(
                "UPDATE users SET umail_json = %s WHERE user_id = %s",
                (json.dumps(existing), user_id),
            )
        # if not connection.get_autocommit():
        connection.commit()

    finally:
        if own_conn:
            connection.close()


def update_umail_json_integration(user_id, new_count, connection=None, keep_days=10):
    """
    Update umail_json for a user:
      - Keep max `keep_days` days of history.
      - Merge counts into today's entry instead of creating duplicates.
      - `processed_threads` = today's total.
      - `timestamp` = last update time.

    Args:
        new_count (int): number of new threads processed.
    """
    own_conn = False
    if connection is None:
        connection = connect_to_rds()
        connection.autocommit(True)
        own_conn = True

    try:
        print(f"inside update umail for integration")
        existing = get_existing_umail_json_integration(user_id, connection) or {}
        print(f"existing: {existing}")
        if "history" not in existing or not isinstance(existing["history"], list):
            existing["history"] = []

        now = datetime.now(timezone.utc)
        today = now.date()

        # remove entries older than keep_days
        cutoff_date = today - timedelta(days=keep_days)
        filtered_history = []
        for h in existing["history"]:
            ts = h.get("timestamp")
            if not ts:
                continue
            try:
                ts_date = datetime.fromisoformat(ts).date()
                if ts_date >= cutoff_date:
                    filtered_history.append(h)
            except Exception:
                continue

        # merge into today's entry if exists
        today_entry = None
        for h in filtered_history:
            ts = h.get("timestamp")
            if ts and datetime.fromisoformat(ts).date() == today:
                today_entry = h
                break

        if today_entry:
            today_entry["processed_threads"] = new_count
            today_entry["timestamp"] = now.isoformat()
            today_entry["date_end"] = today.isoformat()
        else:
            today_entry = {
                "date_start": today.isoformat(),
                "date_end": today.isoformat(),
                "timestamp": now.isoformat(),
                "newly_creation": False,
                "processed_threads": new_count,
            }
            filtered_history.append(today_entry)

        # sort history by date
        filtered_history.sort(key=lambda h: h.get("timestamp", ""))

        # update the JSON
        existing["history"] = filtered_history
        existing["processed_threads"] = today_entry["processed_threads"]
        existing["timestamp"] = now.isoformat()

        # save back
        with get_cursor(connection) as cursor:
            cursor.execute(
                "UPDATE integrations SET umail_json = %s WHERE user_id = %s",
                (json.dumps(existing), user_id),
            )
        # if not connection.get_autocommit():
        connection.commit()

    finally:
        if own_conn:
            connection.close()


def get_userid(email, connection=None):
    """Get the user_id for a given email."""
    own_conn = False
    if connection is None:
        connection = connect_to_rds()
        own_conn = True

    try:
        with get_cursor(connection) as cursor:
            cursor.execute(
                "SELECT user_id FROM users WHERE email = %s",
                (email,),
            )
            result = cursor.fetchone()
            if result:
                return result[0]  # user_id

            # check if the email is from an integration
            cursor.execute(
                "SELECT user_id FROM integrations WHERE email = %s",
                (email,),
            )
            result = cursor.fetchone()
            if result:
                return result[0]  # user_id
            # not present in user and integrations table
            return None
    finally:
        if own_conn:
            connection.close()


def get_users_clients_id(email, user_id, connection=None):
    """
    Get users_clients_id for a given email and user_id_fk.
    """
    own_conn = False
    if connection is None:
        connection = connect_to_rds()
        own_conn = True

    try:
        with get_cursor(connection) as cursor:
            cursor.execute(
                """
                SELECT uc.users_clients_id
                FROM users_clients uc
                JOIN communication c
                  ON uc.users_clients_id = c.users_clients_id_fk
                WHERE c.user_id_fk = %s
                  AND uc.email_id = %s
                """,
                (user_id, email),
            )
            result = cursor.fetchone()
            if result:
                return result[0]  # users_clients_id
            return None
    finally:
        if own_conn:
            connection.close()


def get_existing_autopilot_json(user_id, connection=None):
    """Fetch existing autopilot for a user."""
    try:
        own_conn = False
        if connection is None:
            connection = connect_to_rds()
            own_conn = True

        with get_cursor(connection) as cursor:
            cursor.execute("SELECT autopilot FROM users WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()

        if row and row[0]:
            try:
                return json.loads(row[0])
            except Exception:
                return None
        return None
    finally:
        if own_conn:
            connection.close()


def get_business_info(userid, connection):
    businessdata = {}
    with connection.cursor(pymysql.cursors.DictCursor) as cursor:
        cursor.execute(
            "SELECT user_type,permissions from users where user_id = %s LIMIT 1",
            (userid,),
        )
        user_row = cursor.fetchone()
        if user_row:
            if user_row["user_type"] == "user":
                user_permissions = (
                    json.loads(user_row["permissions"])
                    if user_row.get("permissions")
                    else {}
                )
                invited_by_email = user_permissions.get("invited_by")

                base_user_id = None
                if invited_by_email:
                    cursor.execute(
                        "SELECT user_id from users where email = %s",
                        (invited_by_email,),
                    )
                    base = cursor.fetchone()
                    base_user_id = base.get("user_id") if base else None

                if base_user_id:
                    cursor.execute(
                        "SELECT BusinessName, BillingAddress, WebsiteUrl FROM business_info WHERE user_id_fk = %s LIMIT 1",
                        (base_user_id,),
                    )
                    businessdata = cursor.fetchone() or {}
            else:
                cursor.execute(
                    "SELECT BusinessName, BillingAddress, WebsiteUrl FROM business_info WHERE user_id_fk = %s LIMIT 1",
                    (userid,),
                )
                businessdata = cursor.fetchone() or {}
    return businessdata


def fetch_user_Social(user_id, connection=None):
    try:
        own_conn = False
        if connection is None:
            connection = connect_to_rds()
            own_conn = True

        with get_cursor(connection) as cursor:
            cursor.execute("SELECT social FROM users WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()

        if row and row[0]:
            try:
                return row[0]
            except Exception:
                return None
        return None
    finally:
        if own_conn:
            connection.close()


def save_or_update_workflow_schedule(user_id, filename, activation_schedule, contacts):
    connection = connect_to_rds()
    if not connection:
        return None

    cursor = connection.cursor()

    try:
        query = """
        INSERT INTO user_workflows (user_id_fk, filename, activation_schedule, contacts, outlog)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            activation_schedule = VALUES(activation_schedule),
            contacts = VALUES(contacts);
        """

        cursor.execute(
            query,
            (
                user_id,
                filename,
                json.dumps(activation_schedule),
                json.dumps(contacts),
                json.dumps({}),  # default empty outlog
            ),
        )

        connection.commit()
        return True

    except Exception as e:
        # print("❌ DB Error:", e)
        return False

    finally:
        cursor.close()
        connection.close()


def get_notes_data(user_id):
    connection = connect_to_rds()
    if connection is None:
        # print("❌ [DEBUG] Database connection failed")
        return {"error": "Database connection failed"}
    cursor = connection.cursor()

    try:
        # print("[DEBUG] Preparing to execute query")
        # First get all notes for the user
        notes_query = """
            SELECT
                n.note_id,
                n.conversation_id,
                n.user_id as creator_id,
                n.note_content,
                n.note_type,
                n.created_at,
                n.updated_at,
                COALESCE(u.first_name, '') as creator_first_name,
                COALESCE(u.last_name, '') as creator_last_name,
                COALESCE(np.permission_type, 'owner') as permission_level
            FROM conversation_notes n
            LEFT JOIN users u ON n.user_id = u.user_id
            LEFT JOIN note_permissions np ON n.note_id = np.note_id AND np.user_id = %s AND np.is_active = TRUE
            WHERE n.is_active = TRUE
            AND (
                n.user_id = %s
                OR (np.note_id IS NOT NULL AND np.permission_type IN ('viewer', 'editor'))
            )
            ORDER BY n.created_at DESC
        """
        cursor.execute(notes_query, (user_id, user_id))
        rows = cursor.fetchall()
        print(f"[DEBUG] Found {len(rows)} notes")

        # Process notes first
        notes = []
        conversation_ids = set()
        for row in rows:
            note_id = row[0]
            conversation_id = row[1]
            creator_id = row[2]
            content = row[3]
            note_type = row[4]
            created_at = row[5].isoformat() if row[5] else None
            updated_at = row[6].isoformat() if row[6] else None
            creator_first_name = row[7] or ""
            creator_last_name = row[8] or ""
            permission_level = row[9]

            creator_name = (
                f"{creator_first_name} {creator_last_name}".strip() or creator_id
            )
            conversation_ids.add(conversation_id)

            note = {
                "note_id": note_id,
                "conversation_id": conversation_id,
                "user_id": creator_id,
                "user_name": creator_name,
                "note_content": content,
                "note_type": note_type,
                "created_at": created_at,
                "updated_at": updated_at,
                "is_active": True,
                "permission_level": permission_level,
                "is_shared": note_type == "shared" or permission_level != "owner",
            }
            notes.append(note)

        # Now get conversation details in a separate query
        if conversation_ids:
            conversations_query = """
                SELECT 
                    m.conversation_id_fk,
                    m.conversation_id_fk as conversation_subject,
                    m.content_ref,
                    m.created_at,
                    uc.email_id,
                    uc.first_name,
                    uc.last_name
                FROM messages m
                JOIN users_clients uc ON m.sender_id = uc.users_clients_id
                WHERE m.conversation_id_fk IN ({})
                AND m.message_type = 'inbound'
                ORDER BY m.conversation_id_fk, m.created_at DESC
            """.format(
                ",".join(["%s"] * len(conversation_ids))
            )

            cursor.execute(conversations_query, list(conversation_ids))
            conversation_rows = cursor.fetchall()

            def format_subject(subject):
                """Clean up and format email subject line"""
                if not subject:
                    return "No Subject"
                # Remove technical prefix patterns
                if "_<" in subject:
                    subject = subject.split("_<")[0]
                # Remove email addresses from subject
                subject = subject.split("@")[0]
                # Clean up any remaining special characters
                subject = subject.replace("_", " ").strip()
                return subject or "No Subject"

            # Create a map of conversation details (keep only first message per conversation)
            conversation_details = {}
            for crow in conversation_rows:
                conv_id = crow[0]
                if conv_id not in conversation_details:
                    # Use the sender's name as the conversation subject
                    sender_name = f"{crow[5] or ''} {crow[6] or ''}".strip()
                    conversation_details[conv_id] = {
                        "conversation_subject": sender_name or format_subject(crow[1]),
                        "content_ref": crow[2],
                        "msg_created_at": crow[3].isoformat() if crow[3] else None,
                        "contact_email": crow[4] or "",
                        "contact_name": sender_name,
                    }

            # Add conversation details and navigation info to each note
            for note in notes:
                conv_id = note["conversation_id"]
                if conv_id in conversation_details:
                    note.update(
                        {
                            "contact_email": conversation_details[conv_id][
                                "contact_email"
                            ],
                            "contact_name": conversation_details[conv_id][
                                "contact_name"
                            ],
                            "conversation_subject": conversation_details[conv_id][
                                "conversation_subject"
                            ],
                            "navigate_url": "/umail",  # Base URL for unified mailbox
                            "content_ref": conversation_details[conv_id]["content_ref"],
                            "msg_created_at": conversation_details[conv_id][
                                "msg_created_at"
                            ],
                            # Adding these fields for frontend routing
                            "conversation_data": {
                                "id": conv_id,
                                "content_ref": conversation_details[conv_id][
                                    "content_ref"
                                ],
                            },
                        }
                    )

        # Sort notes by created_at descending
        notes.sort(
            key=lambda x: x["created_at"] if x["created_at"] else "", reverse=True
        )
        all_notes = notes

        response = {"notes": all_notes, "total_notes": len(all_notes)}
        print(f"[DEBUG] Returning response with {len(all_notes)} notes")
        return response

    except Exception as e:
        return {"error": str(e)}
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()
