from .rds_db import connect_to_rds
from datetime import datetime
import uuid


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
    Returns True if it exists, False otherwise.
    """
    own_connection = False
    try:
        if connection is None:
            connection = connect_to_rds()
            own_connection = True

        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM users WHERE user_id = %s", (userid,))
            return cursor.fetchone() is not None

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
