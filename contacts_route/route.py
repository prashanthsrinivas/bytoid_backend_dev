import json
import traceback
from flask import request, jsonify, session, Blueprint
from db.rds_db import connect_to_rds
import uuid
from datetime import datetime, timezone

# from session_middleware import session_check


contacts_bp = Blueprint("contacts", __name__)


@contacts_bp.route("/contacts/save", methods=["POST"])
def save_contact():

    connection = None
    try:
        data = request.get_json() or {}

        user_id = data.get("user_id")
        print(f"user is is:{user_id}")
        first_name = data.get("firstName")
        last_name = data.get("lastName") or None
        phone_number = data.get("phone") or None
        whatsapp_number = data.get("whatsappNumber") or None
        email_id = data.get("email") or None
        facebook_id = data.get("facebookId") or None
        instagram_id = data.get("instagramId") or None
        slack_id = data.get("slackId") or None
        slack_workspace = data.get("slackWorkspace") or None
        type =data.get("type")

        if not user_id:
            return jsonify({"error": "User not logged in"}), 400
        if not first_name:
            return jsonify({"error": "First name is required"}), 400
        if not type:
            return jsonify({"error": "Type is required"}), 400
        
        
        if not any(
            [
                phone_number,
                whatsapp_number,
                email_id,
                facebook_id,
                instagram_id,
                slack_id,
                slack_workspace,
            ]
        ):
            return (
                jsonify(
                    {
                        "error": "At least one contact identifier is required (phone, email, or social handle)."
                    }
                ),
                400,
            )

        dt_utc = datetime.now(timezone.utc)
        created_date = dt_utc.strftime("%Y-%m-%d %H:%M:%S")  # For database (string)
        updated_date = dt_utc.isoformat()  # For parsing (ISO format with timezone)

        connection = connect_to_rds()

        with connection.cursor() as cursor:
            check_contact_sql = """
                SELECT uc.users_clients_id, c.communication_id
                FROM users_clients uc
                JOIN communication c ON c.users_clients_id_fk = uc.users_clients_id
                WHERE c.user_id_fk = %s AND (
                    (%s IS NOT NULL AND uc.phone_number = %s) OR
                    (%s IS NOT NULL AND uc.whatsapp_number = %s) OR
                    (%s IS NOT NULL AND uc.email_id = %s) OR
                    (%s IS NOT NULL AND uc.facebook_id = %s) OR
                    (%s IS NOT NULL AND uc.instagram_id = %s) OR
                    (%s IS NOT NULL AND uc.slack_id = %s) OR
                    (%s IS NOT NULL AND uc.slack_workspace = %s) 
                
                )
                LIMIT 1;
            """
            cursor.execute(
                check_contact_sql,
                (
                    user_id,
                    phone_number,
                    phone_number,
                    whatsapp_number,
                    whatsapp_number,
                    email_id,
                    email_id,
                    facebook_id,
                    facebook_id,
                    instagram_id,
                    instagram_id,
                    slack_id,
                    slack_id,
                    slack_workspace,
                    slack_workspace,
                  
                ),
            )
            exists = cursor.fetchone()

            if not exists:
                print("creating new user client and communication table")
                communication_id = str(uuid.uuid4())
                users_clients_id = str(uuid.uuid4())

                insert_communication_sql = """
                        INSERT INTO communication (
                        communication_id,
                        user_id_fk,
                        users_clients_id_fk
                    )
                    VALUES (%s, %s, NULL)
                """
                cursor.execute(insert_communication_sql, (communication_id, user_id))

                insert_sql = """
                    INSERT INTO users_clients (
                        users_clients_id,
                        communication_id_fk,
                        first_name,
                        last_name,
                        phone_number,
                        whatsapp_number,
                        email_id,
                        facebook_id,
                        instagram_id,
                        slack_id,
                        slack_workspace,
                        type,
                        created_in,
                        updated_in,
                        snooze
                        
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,%s,%s,%s,%s)
                """
                cursor.execute(
                    insert_sql,
                    (
                        users_clients_id,
                        communication_id,
                        first_name,
                        last_name,
                        phone_number,
                        whatsapp_number,
                        email_id,
                        facebook_id,
                        instagram_id,
                        slack_id,
                        slack_workspace,
                        type,
                        created_date,
                        updated_date,
                        False,
                    ),
                )

                link_sql = """
                    UPDATE communication
                    SET users_clients_id_fk = %s
                    WHERE communication_id = %s
                """
                cursor.execute(link_sql, (users_clients_id, communication_id))

            else:
                print("updating existing communiaction and users_clients")
                users_clients_id, communication_id = exists
                fields = {
                    "first_name": first_name,
                    "last_name": last_name,
                    "phone_number": phone_number,
                    "whatsapp_number": whatsapp_number,
                    "email_id": email_id,
                    "facebook_id": facebook_id,
                    "instagram_id": instagram_id,
                    "slack_id": slack_id,
                    "slack_workspace": slack_workspace,
                    "updated_in": updated_date,
                    "type":type,
                }
                set_clauses = []
                values = []
                for col, val in fields.items():
                    if val is not None:
                        set_clauses.append(f"{col} = %s")
                        values.append(val)
                if set_clauses:
                    update_query = f"""
                        UPDATE users_clients
                        SET {', '.join(set_clauses)}
                        WHERE users_clients_id = %s
                    """
                    values.append(users_clients_id)
                    cursor.execute(update_query, tuple(values))

                 # clear company, subject, status, source if the lead is changed to a customer
                if type == "Customer":
                    clear_query = """
                        UPDATE users_clients
                        SET company = %s,
                            subject = %s,
                            status = %s,
                            source = %s
                        WHERE users_clients_id = %s
                    """
                    clear_values = ("", "", "", "", users_clients_id)
                    cursor.execute(clear_query, clear_values)

            connection.commit()
            return jsonify(
                {
                    "status": "success",
                    "communication_id": communication_id,
                    "users_clients_id": users_clients_id,
                }
            )

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        if connection:
            connection.close()

def add_synced_contact(user_id, cursor, participant, first_name, last_name):

    print("creating new user client and communication table")
    communication_id = str(uuid.uuid4())
    users_clients_id = str(uuid.uuid4())

    dt_utc = datetime.now(timezone.utc)
    created_date = dt_utc.strftime("%Y-%m-%d %H:%M:%S")  # For database (string)
    updated_date = dt_utc.isoformat()  # For parsing (ISO format with timezone)

    insert_communication_sql = """
                    INSERT INTO communication (
                        communication_id,
                        user_id_fk,
                        users_clients_id_fk
                    )
                    VALUES (%s, %s, NULL)
                """
    cursor.execute(insert_communication_sql, (communication_id, user_id))

    insert_sql = """
                    INSERT INTO users_clients (
                        users_clients_id,
                        communication_id_fk,
                        first_name,
                        last_name,
                        phone_number,
                        whatsapp_number,
                        email_id,
                        facebook_id,
                        instagram_id,
                        slack_id,
                        slack_workspace,
                        type,
                        created_in,
                        updated_in,
                        snooze


                    )
                    VALUES (%s, %s, %s, %s, NULL, NULL, %s, NULL, NULL, NULL, NULL,%s,%s,%s,%s)
                """
    cursor.execute(
        insert_sql,
        (
            users_clients_id,
            communication_id,
            first_name,
            last_name,
            participant,
            "Customer",
            created_date,
            updated_date,
            False,
        ),
    )

    link_sql = """
                    UPDATE communication
                    SET users_clients_id_fk = %s
                    WHERE communication_id = %s
                """
    cursor.execute(link_sql, (users_clients_id, communication_id))

    cursor.connection.commit()

    return users_clients_id


@contacts_bp.route("/contacts/list", methods=["GET"])
def get_contacts_by_user(userid=None):
    """
    Fetch all contacts (name, gmail) linked to a user_id through communication.
    """
    try:
        user_id = request.args.get("user_id") or session.get("user_id") or userid
        connection = connect_to_rds()
        with connection.cursor() as cursor:
            query = """
                SELECT uc.users_clients_id, uc.first_name, uc.last_name, uc.email_id, uc.type
                FROM users_clients uc
                JOIN communication c
                  ON uc.communication_id_fk = c.communication_id
                WHERE c.user_id_fk = %s
            """
            cursor.execute(query, (user_id,))
            rows = cursor.fetchall()

            contacts = []
            for (
                users_clients_id,
                first_name,
                last_name,
                email,
                type,
            ) in rows:
                full_name = (
                    f"{(first_name or '').strip()} {(last_name or '').strip()}".strip()
                )
                if full_name or email:
                    contacts.append(
                        {
                            "id": users_clients_id,
                            "name": full_name,
                            "email": email,
                            "type": type,
                        }
                    )
        return jsonify(contacts)

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
