import json

from .rds_db import connect_to_rds, get_cursor
from datetime import datetime
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
                print("Could not decode sociallinks:", e)
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
    own_conn = False
    if connection is None:
        connection = connect_to_rds()
        own_conn = True

    with get_cursor(connection, close_after=own_conn) as cursor:
        cursor.execute("SELECT umail_json FROM users WHERE user_id = %s", (user_id,))
        row = cursor.fetchone()

    if row and row[0]:
        try:
            return json.loads(row[0])
        except Exception:
            return None
    return None


def update_umail_json(user_id, new_entry, connection=None):
    """Update umail_json with a new entry."""
    own_conn = False
    if connection is None:
        connection = connect_to_rds()
        own_conn = True

    existing = get_existing_umail_json(user_id, connection) or {"history": []}
    existing["history"].append(new_entry)

    with get_cursor(connection, close_after=own_conn) as cursor:
        cursor.execute(
            "UPDATE users SET umail_json = %s WHERE user_id = %s",
            (json.dumps(existing), user_id),
        )
