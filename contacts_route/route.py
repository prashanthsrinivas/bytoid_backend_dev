import json
import traceback
from flask import request, jsonify, session, Blueprint
from db.rds_db import connect_to_rds
import uuid


contacts_bp = Blueprint("contacts", __name__)


# @contacts_bp.route('/contacts/save',methods=['POST'])
# def save_contact():
#     """
#     Saves a new contact into users_clients table.

#     - Validates required fields
#     - Inserts the record into users_clients
#     - Returns a success or error JSON response
#     """

#     try:
#         # Read JSON from frontend
#         data = request.get_json() or {}

#         user_id=data.get("user_id") #riya@bytoid.ai
#         print(f"user is is:{user_id}")
#         first_name = data.get("firstName") #mahender
#         last_name = data.get("lastName")
#         phone_number = data.get("phone","")
#         whatsapp_number = data.get("whatsappNumber","")
#         email_id = data.get("email","") #mahender@bytoid.ai
#         facebook_id = data.get("facebookId","")
#         instagram_id = data.get("instagramId","")
#         slack_id = data.get("slackId","")
#         slack_workspace = data.get("slackWorkspace","")


#         # Validate required fields
#         if not user_id:
#             return jsonify({"error": "User not logged in"}), 400
#         if not first_name:
#             return jsonify({"error": "First name is required"}), 400
#         if not last_name:
#             return jsonify({"error": "Last name is required"}), 400

#         if not any([
#             phone_number,
#             whatsapp_number,
#             email_id,
#             facebook_id,
#             instagram_id,
#             slack_id,
#             slack_workspace
#         ]):
#             return jsonify({
#                 "error": "At least one contact identifier is required (phone, email, or social handle)."
#             }), 400

#         users_clients_id = str(uuid.uuid4())
#         connection = connect_to_rds()

#         with connection.cursor() as cursor:

#             sql = """
#                 SELECT uc.users_clients_id, c.communication_id
#                 FROM users_clients uc
#                 JOIN communication c ON c.users_clients_id_fk = uc.users_clients_id
#                 WHERE c.user_id_fk = %s

#                 LIMIT 1;
#                 """


#             cursor.execute(sql, (user_id,))

#             exists = cursor.fetchone()

#             users_client_id,communication_id=exists
#             print(f"existe - {exists}")

#             if not exists:
#                 print("creating new user client and communication table")

#                 communication_id = str(uuid.uuid4())
#                 users_clients_id = str(uuid.uuid4())

#                 insert_communication_sql = """
#                 INSERT INTO communication (
#                     communication_id,
#                     user_id_fk,
#                     users_clients_id_fk
#                 )
#                 VALUES (%s, %s, NULL)
#             """
#                 cursor.execute(
#                     insert_communication_sql,
#                     (communication_id, user_id)
#                 )

#             # insert into users_clients table
#                 insert_sql = """
#                     INSERT INTO users_clients (
#                         users_clients_id,
#                         communication_id_fk,
#                         first_name,
#                         last_name,
#                         phone_number,
#                         whatsapp_number,
#                         email_id,
#                         facebook_id,
#                         instagram_id,
#                         slack_id,
#                         slack_workspace

#                     )
#                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
#                 """
#                 cursor.execute(
#                     insert_sql,
#                     (
#                         users_clients_id,
#                         communication_id,
#                         first_name,
#                         last_name,
#                         phone_number,
#                         whatsapp_number,
#                         email_id,
#                         facebook_id,
#                         instagram_id,
#                         slack_id,
#                         slack_workspace

#                     )
#                 )

#                 # Link communication to users_clients
#                 link_sql = """
#                     UPDATE communication
#                     SET users_clients_id_fk = %s
#                     WHERE communication_id = %s
#                 """
#                 cursor.execute(link_sql, (users_clients_id, communication_id))

#             else:
#                 print("updating existing communiaction and users_clients")
#                 users_clients_id, communication_id = exists

#                 # check the input email,mobile allotherfileds
#                 sql="""
#                 select users_clients_id from users_clients where email_id=%s and communication_id_fk  =%s
#                 """
#                 #users_id=cursor.execute(sql,(email_id,communication_id))
#                 cursor.execute(sql, (email_id, communication_id))
#                 users_id = cursor.fetchone()  # returns a tuple like (users_clients_id,) or None
#                 print("found id",users_id)
#                 if not users_id:
#                     print("creating new contact")
#                     users_clients_id = str(uuid.uuid4())

#                     create_users_clients = """
#                     INSERT INTO users_clients (
#                         first_name, last_name, phone_number, whatsapp_number, email_id,
#                         facebook_id, instagram_id, slack_id, slack_workspace, communication_id_fk
#                     )
#                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
#                     """

#                     cursor.execute(create_users_clients, (first_name, last_name, phone_number, whatsapp_number, email_id, facebook_id, instagram_id, slack_id, slack_workspace,  communication_id ))
#                     return jsonify({
#                             "status": "success",
#                             "communication_id": communication_id,
#                             "users_clients_id": users_clients_id
#                         })
#                 else:
#                     return {"message":"account already exists"},400


#     except Exception as e:
#         traceback.print_exc()
#         return jsonify({"error": str(e)}), 500
#     finally:
#         connection.commit()


@contacts_bp.route("/contacts/save", methods=["POST"])
def save_contact():
    try:
        data = request.get_json() or {}

        user_id = data.get("user_id")
        print(f"user is is:{user_id}")
        first_name = data.get("firstName")
        last_name = data.get("lastName")
        phone_number = data.get("phone") or None
        whatsapp_number = data.get("whatsappNumber") or None
        email_id = data.get("email") or None
        facebook_id = data.get("facebookId") or None
        instagram_id = data.get("instagramId") or None
        slack_id = data.get("slackId") or None
        slack_workspace = data.get("slackWorkspace") or None

        if not user_id:
            return jsonify({"error": "User not logged in"}), 400
        if not first_name:
            return jsonify({"error": "First name is required"}), 400
        if not last_name:
            return jsonify({"error": "Last name is required"}), 400
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
                        slack_workspace
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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


# def check_contact_field():
#     data=request.json
#     filed=data["field"]
#     value=data["value"]
#     sql=f"""
#     select 1 from users_clients where {filed}={value}
#     """
#     if this present:
#         e


@contacts_bp.route("/contacts/list", methods=["GET"])
def get_contacts_by_user(userid=None):
    """
    Fetch all contacts (name, gmail) linked to a user_id through communication.
    """

    try:
        user_id = request.args.get("user_id") or session.get("user_id") or userid
        print(f"User ID for getting contacts: {user_id}")
        connection = connect_to_rds()
        with connection.cursor() as cursor:
            query = """
                SELECT uc.users_clients_id, uc.first_name, uc.last_name, uc.email_id
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
            ) in rows:
                full_name = (
                    f"{(first_name or '').strip()} {(last_name or '').strip()}".strip()
                )
                if full_name or email:
                    contacts.append(
                        {"id": users_clients_id, "name": full_name, "email": email}
                    )
        return jsonify(contacts)

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
