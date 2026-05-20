import json
import traceback
from flask import request, jsonify, session, Blueprint, g
from db.rds_db import connect_to_rds
import uuid
from datetime import datetime, timezone
from utils.normal import parse_composite_user_id
from umail_helper.helper import delete_user_sync_time, delete_from_cache_sync
from utils.s3_utils import (
    delete_folder_from_s3,
)
from umail_helper.ticketalloc import TicketAllocator
from threading import Thread
from services.audit_log_service import (
    log_audit_event,
    CONTACT_BULK_DELETED,
    CONTACT_GROUP_DELETED,
    build_audit_actor,
    CONTACT_CREATED,
    CONTACT_UPDATED,
    CONTACT_GROUP_CREATED,
    CONTACT_GROUP_UPDATED,
)
from db.db_checkers import get_email_by_id
from utils.permission_required import permission_required_body

# from session_middleware import session_check


contacts_bp = Blueprint("contacts", __name__)


@permission_required_body("team.search")
@contacts_bp.route("/contacts/save", methods=["POST"])
@permission_required_body("team.add_vendor")
def save_contact():

    connection = None
    try:
        data = request.get_json() or {}

        user_id = data.get("user_id")
        # print(f"user is is:{user_id}")
        first_name = data.get("firstName")
        last_name = data.get("lastName") or None
        phone_number = data.get("phone") or None
        whatsapp_number = data.get("whatsappNumber") or None
        email_id = data.get("email") or None
        facebook_id = data.get("facebookId") or None
        instagram_id = data.get("instagramId") or None
        slack_id = data.get("slackId") or None
        slack_workspace = data.get("slackWorkspace") or None
        isLead = data.get("isLead")
        type = "Lead" if isLead else "Customer"
        company = data.get("company") or None
        subject = data.get("subject") or None
        status = data.get("status") or None
        source = data.get("source") or None

        status_val = status.lower() if status and status.strip() else None
        source_val = source.lower() if source and source.strip() else None

        if not user_id:
            return jsonify({"error": "User not logged in"}), 400
        if not first_name:
            return jsonify({"error": "First name is required"}), 400
        if not type:
            return jsonify({"error": "Type is required"}), 400

        logged_in_user_id, user_id = parse_composite_user_id(user_id)

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
        cursor = connection.cursor()

        cursor.execute(
            "SELECT 1 FROM integrations WHERE email = %s",
            (email_id,),
        )
        row = cursor.fetchone()
        if row:
            return (
                jsonify(
                    {
                        "error": "This email id is already registered as an integration account. Please use another mail id for creating a new user"
                    }
                ),
                400,
            )

        dt_utc = datetime.now(timezone.utc)
        created_date = dt_utc.strftime("%Y-%m-%d %H:%M:%S")  # For database (string)
        updated_date = dt_utc.isoformat()  # For parsing (ISO format with timezone)

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
                # print("creating new user client and communication table")
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
                        snooze,
                        company,
                        subject,
                        status,
                        source
                        
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,%s,%s,%s,%s,%s,%s,%s,%s)
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
                        company.lower() if company else None,
                        subject.lower() if subject else None,
                        status_val,
                        source_val,
                    ),
                )

                link_sql = """
                    UPDATE communication
                    SET users_clients_id_fk = %s
                    WHERE communication_id = %s
                """
                cursor.execute(link_sql, (users_clients_id, communication_id))
                connection.commit()

                # Audit logging
                (
                    actor_user_id,
                    actor_email,
                    acting_on_behalf_of_user_id,
                    acting_on_behalf_of_email,
                ) = build_audit_actor(user_id)
                log_audit_event(
                    action=CONTACT_CREATED,
                    endpoint="/contacts/save",
                    ip=request.remote_addr,
                    status="success",
                    actor_user_id=actor_user_id,
                    actor_email=actor_email,
                    acting_on_behalf_of_user_id=acting_on_behalf_of_user_id,
                    acting_on_behalf_of_email=acting_on_behalf_of_email,
                    metadata={
                        "contact_type": type,
                        "has_email": bool(email_id),
                        "has_phone": bool(phone_number),
                    },
                )
                g.audit_logged = True

            else:
                return jsonify({"message": "This contact is already saved"}), 201
            return jsonify(
                {
                    "status": "success",
                }
            )

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        if connection:
            connection.close()


@permission_required_body("team.search")
@contacts_bp.route("/contacts/save_edit", methods=["POST"])
@permission_required_body("team.member.edit")
def save_edit_contact():

    connection = None
    try:
        data = request.get_json() or {}

        baseuser = data.get("user_id")
        # print(f"user is is:{user_id}")
        first_name = data.get("firstName")
        last_name = data.get("lastName") or None
        phone_number = data.get("phone") or None
        whatsapp_number = data.get("whatsappNumber") or None
        email_id = data.get("email") or None
        facebook_id = data.get("facebookId") or None
        instagram_id = data.get("instagramId") or None
        slack_id = data.get("slackId") or None
        slack_workspace = data.get("slackWorkspace") or None
        isLead = data.get("isLead")
        type = "Lead" if isLead else "Customer"
        # print(f"type : {type}")
        company = data.get("company") or None
        subject = data.get("subject") or None
        status = data.get("status") or None
        source = data.get("source") or None

        status_val = status.lower() if status and status.strip() else None
        source_val = source.lower() if source and source.strip() else None

        if not baseuser:
            return jsonify({"error": "User not logged in"}), 400
        if not first_name:
            return jsonify({"error": "First name is required"}), 400
        if not type:
            return jsonify({"error": "Type is required"}), 400
        logged_in_user_id, user_id = parse_composite_user_id(baseuser)

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
                # print("creating new user client and communication table")
                return jsonify({"error": "The contact was not found"}), 404

            else:
                # print("updating existing communiaction and users_clients")
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
                    "type": type,
                    "company": company,
                    "subject": subject,
                    "status": status_val,
                    "source": source_val,
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

                # Audit logging
                (
                    actor_user_id,
                    actor_email,
                    acting_on_behalf_of_user_id,
                    acting_on_behalf_of_email,
                ) = build_audit_actor(baseuser)
                log_audit_event(
                    action=CONTACT_UPDATED,
                    endpoint="/contacts/save_edit",
                    ip=request.remote_addr,
                    status="success",
                    actor_user_id=actor_user_id,
                    actor_email=actor_email,
                    acting_on_behalf_of_user_id=acting_on_behalf_of_user_id,
                    acting_on_behalf_of_email=acting_on_behalf_of_email,
                    metadata={
                        "contact_type": type,
                        "fields_changed": len(
                            [k for k, v in fields.items() if v is not None]
                        ),
                    },
                )
                g.audit_logged = True

            return jsonify(
                {
                    "status": "successfully updated",
                }
            )

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        if connection:
            connection.close()


@permission_required_body("team.search")
@contacts_bp.route("/users/delete_contacts", methods=["POST"])
@permission_required_body("team.member.delete")
def delete_contacts():
    connection = None
    try:
        data = request.get_json() or {}

        baseuser = data.get("user_id")
        client_ids = data.get("contact_ids")  # list

        if not user_id:
            return jsonify({"error": "user_id is required"}), 400
        if not client_ids or not isinstance(client_ids, list):
            return jsonify({"error": "client_ids must be a list"}), 400
        logged_in_user_id, user_id = parse_composite_user_id(baseuser)

        connection = connect_to_rds()
        client_placeholders = ",".join(["%s"] * len(client_ids))
        client_params = client_ids

        with connection.cursor() as cursor:
            # -------------------------------
            # STEP 1. Get ticket IDs assigned to the user
            # -------------------------------
            ticket_ids = []
            get_ticket_ids = f"""
                SELECT ticket_id_fk FROM assigned 
                WHERE users_clients_id_fk IN ({client_placeholders})
                """
            cursor.execute(get_ticket_ids, client_params)
            ticket_ids = [row[0] for row in cursor.fetchall() if row[0]]

            if ticket_ids:
                tickets_placeholders = ",".join(["%s"] * len(ticket_ids))
                ticket_params = ticket_ids

            # -------------------------------
            # STEP 2 : Get conversation IDs from those tickets
            # -------------------------------
            conv_ids = []
            if ticket_ids:
                get_conv_ids = f"""
                    SELECT conversation_id_fk FROM tickets 
                    WHERE tickets_id IN ({tickets_placeholders})
                    """
                cursor.execute(get_conv_ids, ticket_params)
                conv_ids = [row[0] for row in cursor.fetchall() if row[0]]

            if conv_ids:
                conv_placeholders = ",".join(["%s"] * len(conv_ids))
                conv_params = conv_ids

            # -------------------------------
            # STEP 3: Remove from messages table
            # -------------------------------

            if conv_ids:

                delete_sql = f"""
                        DELETE FROM messages
                        WHERE conversation_id_fk IN ({conv_placeholders})
                    """
                cursor.execute(delete_sql, conv_params)

            # -------------------------------
            # STEP 4: Remove from assigned table
            # -------------------------------
            if ticket_ids:

                delete_sql = f"""
                        DELETE FROM assigned
                        WHERE ticket_id_fk IN ({tickets_placeholders})
                    """
                params = ticket_ids
                cursor.execute(delete_sql, ticket_params)

            # -------------------------------
            # STEP 5: Remove from tickets table
            # -------------------------------
            if ticket_ids:

                delete_sql = f"""
                        DELETE FROM tickets
                        WHERE tickets_id IN ({tickets_placeholders})
                    """
                cursor.execute(delete_sql, ticket_params)

            # -------------------------------
            # STEP 6: Remove from conversation table
            # -------------------------------
            if conv_ids:

                delete_sql = f"""
                        DELETE FROM threads
                        WHERE conversation_id IN ({conv_placeholders})
                    """
                cursor.execute(delete_sql, conv_params)

            # -------------------------------
            # STEP 7: Remove from communication table
            # -------------------------------

            delete_sql = f"""
                    DELETE FROM communication
                    WHERE users_clients_id_fk IN ({client_placeholders})
                """
            cursor.execute(delete_sql, client_params)

            # -------------------------------
            # STEP 8: Remove from users_clients table
            # -------------------------------

            delete_sql = f"""
                    DELETE FROM users_clients
                    WHERE users_clients_id IN ({client_placeholders})
                """
            cursor.execute(delete_sql, client_params)

            # -------------------------------
            # STEP 9: Remove from groups JSON
            # -------------------------------
            cursor.execute(
                "SELECT groups_json FROM users WHERE user_id = %s", (user_id,)
            )
            row = cursor.fetchone()

            if row and row[0]:
                try:
                    groups_json = json.loads(row[0])
                except:
                    groups_json = {}
            else:
                groups_json = {}

            updated = False
            now = datetime.utcnow().isoformat()

            for gid, gdata in groups_json.items():
                old_list = gdata.get("client_ids", [])
                new_list = [cid for cid in old_list if cid not in client_ids]

                if len(old_list) != len(new_list):
                    updated = True
                    gdata["client_ids"] = new_list
                    gdata["count"] = len(new_list)
                    gdata["updated_at"] = now

            if updated:
                cursor.execute(
                    "UPDATE users SET groups_json = %s WHERE user_id = %s",
                    (json.dumps(groups_json), user_id),
                )

            # -------------------------------
            # STEP 10: Commit all changes
            # -------------------------------
            connection.commit()

        # Outside the cursor context: delete S3 folder + update ticket allocator
        folder_path = f"{user_id}/messages"
        Thread(target=delete_folder_from_s3, args=(folder_path,)).start()
        client_ticket = TicketAllocator(user_id)
        client_ticket.update_ticket(value=0)

        # remove from outlook sync file
        result = delete_user_sync_time(user_id)
        # if not result:
        #    #print(f"could not delete using delete_user_sync_time")
        #     return jsonify({"error": "unable to delete contact"}), 500

        # remove the contact messages from redis
        # result = delete_from_cache_sync(user_id)
        # if result == 1:
        #    #print("Cache deleted")
        # else:
        #    #print("No cache found")

        actor_uid, actor_email, behalf_uid, behalf_email = build_audit_actor(baseuser)
        log_audit_event(
            action=CONTACT_BULK_DELETED,
            endpoint="/users/delete_contacts",
            ip=request.remote_addr,
            status="success",
            actor_user_id=actor_uid,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=behalf_uid,
            acting_on_behalf_of_email=behalf_email,
            metadata={"contact_ids": client_ids, "count": len(client_ids)},
        )
        g.audit_logged = True

        return (
            jsonify(
                {"message": "Contacts deleted successfully", "deleted_ids": client_ids}
            ),
            200,
        )

    except Exception as e:
        traceback.print_exc()
        # print(f"error: {str(e)}")
        return jsonify({"error": "unable to delete contact"}), 500

    finally:
        if connection:
            connection.close()


def add_synced_contact(user_id, cursor, participant, first_name, last_name):

    # print("creating new user client and communication table")
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


@permission_required_body("team.search")
@contacts_bp.route("/contacts/list", methods=["GET"])
@permission_required_body("team.search")
def get_contacts_by_user(userid=None):
    """
    Fetch all contacts (name, gmail) linked to a user_id through communication.
    """
    try:
        user_id = request.args.get("user_id") or session.get("user_id") or userid
        connection = connect_to_rds()
        logged_in_user_id, user_id = parse_composite_user_id(user_id)
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


@permission_required_body("team.search")
@contacts_bp.route("/contacts/basic_info", methods=["POST"])
@permission_required_body("team.member.view")
def get_basic_info():
    connection = None
    try:
        data = request.get_json() or {}
        id = data.get("id")

        if not id:
            return jsonify({"error": "id needed"}), 400
        connection = connect_to_rds()
        with connection.cursor() as cursor:
            sql = """
                SELECT first_name, last_name, phone_number, whatsapp_number, email_id,
                    facebook_id, instagram_id, slack_id, slack_workspace, type
                FROM users_clients
                WHERE users_clients_id = %s
            """
            cursor.execute(sql, (id,))
            row = cursor.fetchone()

        if not row:
            return jsonify({"error": "Contact not found"}), 404

        else:
            (
                first_name,
                last_name,
                phone_number,
                whatsapp_number,
                email_id,
                facebook_id,
                instagram_id,
                slack_id,
                slack_workspace,
                contact_type,
            ) = row

            full_name = (
                f"{(first_name or '').strip()} {(last_name or '').strip()}".strip()
            )

            basic_info = {
                "id": id,
                "name": full_name,
                "first_name": first_name,
                "last_name": last_name,
                "phone_number": phone_number,
                "whatsapp_number": whatsapp_number,
                "email_id": email_id,
                "facebook_id": facebook_id,
                "instagram_id": instagram_id,
                "slack_id": slack_id,
                "slack_workspace": slack_workspace,
                "type": contact_type,
            }
            return basic_info

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

    finally:
        if connection:
            connection.close()


# ------------------- GROUPS ---------------------#


@permission_required_body("team.search")
@contacts_bp.route("/users/save_group", methods=["POST"])
@permission_required_body("team.group.create")
def save_group():
    connection = None
    try:
        data = request.get_json() or {}

        baseuser = data.get("user_id")
        group_name = data.get("group_name")
        client_ids = data.get("client_ids")  # list

        if not baseuser:
            return jsonify({"error": "user_id is required"}), 400
        if not group_name:
            return jsonify({"error": "group_name is required"}), 400
        if not client_ids or not isinstance(client_ids, list):
            return jsonify({"error": "client_ids must be a list"}), 400
        logged_in_user_id, user_id = parse_composite_user_id(baseuser)

        now = datetime.utcnow().isoformat()  # timestamp

        connection = connect_to_rds()
        with connection.cursor() as cursor:

            # Fetch existing JSON
            cursor.execute(
                "SELECT groups_json FROM users WHERE user_id = %s", (user_id,)
            )
            existing = cursor.fetchone()

            if existing and existing[0]:
                try:
                    existing_json = json.loads(existing[0])
                except:
                    existing_json = {}
            else:
                existing_json = {}

            group_id = str(uuid.uuid4())

            # Build new/updated group entry
            existing_json[group_id] = {
                "group_name": group_name,
                "client_ids": client_ids,
                "count": len(client_ids),
                "created_at": now,
                "updated_at": now,
            }

            # Save to DB
            cursor.execute(
                "UPDATE users SET groups_json = %s WHERE user_id = %s",
                (json.dumps(existing_json), user_id),
            )
            connection.commit()

            # Audit logging
            (
                actor_user_id,
                actor_email,
                acting_on_behalf_of_user_id,
                acting_on_behalf_of_email,
            ) = build_audit_actor(baseuser)
            log_audit_event(
                action=CONTACT_GROUP_CREATED,
                endpoint="/users/save_group",
                ip=request.remote_addr,
                status="success",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                acting_on_behalf_of_user_id=acting_on_behalf_of_user_id,
                acting_on_behalf_of_email=acting_on_behalf_of_email,
                metadata={
                    "group_id": group_id,
                    "group_name": group_name,
                    "contact_count": len(client_ids),
                },
            )
            g.audit_logged = True

        return (
            jsonify({"message": "Group saved successfully", "group_id": group_id}),
            200,
        )

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

    finally:
        if connection:
            connection.close()


@permission_required_body("team.search")
@contacts_bp.route("/users/edit_group", methods=["POST"])
@permission_required_body("team.group.edit")
def edit_group():
    connection = None
    try:
        data = request.get_json() or {}

        baseuser = data.get("user_id")
        group_name = data.get("group_name")
        client_ids = data.get("client_ids")
        group_id = data.get("group_id")

        if not baseuser:
            return jsonify({"error": "user_id is required"}), 400
        if not group_name:
            return jsonify({"error": "group_name is required"}), 400
        if not client_ids or not isinstance(client_ids, list):
            return jsonify({"error": "client_ids must be a list"}), 400
        logged_in_user_id, user_id = parse_composite_user_id(baseuser)

        connection = connect_to_rds()
        with connection.cursor() as cursor:

            # Fetch data
            cursor.execute(
                "SELECT groups_json FROM users WHERE user_id = %s", (user_id,)
            )
            existing = cursor.fetchone()

            if existing and existing[0]:
                try:
                    existing_json = json.loads(existing[0])
                except:
                    existing_json = {}
            else:
                existing_json = {}

            # ❌ Group does not exist
            if group_id not in existing_json:
                return jsonify({"error": "group not found"}), 404

            # Update group
            now = datetime.utcnow().isoformat()
            existing_json[group_id]["client_ids"] = client_ids
            existing_json[group_id]["group_name"] = group_name
            existing_json[group_id]["count"] = len(client_ids)
            existing_json[group_id]["updated_at"] = now

            cursor.execute(
                "UPDATE users SET groups_json = %s WHERE user_id = %s",
                (json.dumps(existing_json), user_id),
            )
            connection.commit()

            # Audit logging
            (
                actor_user_id,
                actor_email,
                acting_on_behalf_of_user_id,
                acting_on_behalf_of_email,
            ) = build_audit_actor(baseuser)
            log_audit_event(
                action=CONTACT_GROUP_UPDATED,
                endpoint="/users/edit_group",
                ip=request.remote_addr,
                status="success",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                acting_on_behalf_of_user_id=acting_on_behalf_of_user_id,
                acting_on_behalf_of_email=acting_on_behalf_of_email,
                metadata={
                    "group_id": group_id,
                    "group_name": group_name,
                    "contact_count": len(client_ids),
                },
            )
            g.audit_logged = True

        return (
            jsonify({"message": "Group updated successfully", "groups": existing_json}),
            200,
        )

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

    finally:
        if connection:
            connection.close()


@permission_required_body("team.search")
@contacts_bp.route("/users/get_group", methods=["POST"])
@permission_required_body("team.group.view")
def get_group():
    connection = None
    try:
        data = request.get_json() or {}

        baseuser = data.get("user_id")
        group_id = data.get("group_id")

        if not baseuser:
            return jsonify({"error": "user_id is required"}), 400
        if not group_id:
            return jsonify({"error": "group_id is required"}), 400
        logged_in_user_id, user_id = parse_composite_user_id(baseuser)

        connection = connect_to_rds()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT groups_json FROM users WHERE user_id = %s", (user_id,)
            )
            row = cursor.fetchone()

            if not row or not row[0]:
                return jsonify({"error": "no groups found for this user"}), 404

            try:
                groups_json = json.loads(row[0])
            except:
                return jsonify({"error": "invalid groups JSON"}), 500

            # Check if group exists
            if group_id not in groups_json:
                return jsonify({"error": "group not found"}), 404

            group_data = groups_json[group_id]
            client_ids = group_data.get("client_ids", [])
            member_list = []

            for cid in client_ids:
                query = """
                SELECT  first_name, last_name, email_id, type
                FROM users_clients 
                WHERE users_clients_id = %s
                """
                cursor.execute(query, (cid,))
                rows = cursor.fetchall()

                for (
                    first_name,
                    last_name,
                    email,
                    type,
                ) in rows:
                    full_name = f"{(first_name or '').strip()} {(last_name or '').strip()}".strip()
                    member_list.append(
                        {
                            "name": full_name,
                            "email": email,
                            "type": type,
                        }
                    )

        return (
            jsonify(
                {
                    "group_name": group_data.get("group_name", ""),
                    "created_at": group_data.get("created_at", ""),
                    "updated_at": group_data.get("updated_at", ""),
                    "member_count": group_data.get("count", ""),
                    "member_details": member_list,
                }
            ),
            200,
        )

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

    finally:
        if connection:
            connection.close()


@permission_required_body("team.search")
@contacts_bp.route("/users/delete_group", methods=["POST"])
@permission_required_body("team.group.delete")
def delete_group():
    connection = None
    try:
        data = request.get_json() or {}

        baseuser = data.get("user_id")
        group_id = data.get("group_ids")

        if not baseuser:
            return jsonify({"error": "user_id is required"}), 400
        if not group_id:
            return jsonify({"error": "group_id is required"}), 400
        logged_in_user_id, user_id = parse_composite_user_id(baseuser)

        connection = connect_to_rds()
        with connection.cursor() as cursor:

            # Fetch existing groups JSON
            cursor.execute(
                "SELECT groups_json FROM users WHERE user_id = %s", (user_id,)
            )
            row = cursor.fetchone()

            if not row or not row[0]:
                return jsonify({"error": "no groups found for this user"}), 404

            try:
                groups_json = json.loads(row[0])
            except:
                return jsonify({"error": "invalid groups JSON"}), 500

            # Check if group exists
            for g_id in group_id:
                if g_id not in groups_json:
                    return jsonify({"error": "group not found"}), 404

                # Delete the group
                del groups_json[g_id]

                # Save updated JSON back to DB
                cursor.execute(
                    "UPDATE users SET groups_json = %s WHERE user_id = %s",
                    (json.dumps(groups_json), user_id),
                )
                connection.commit()

        actor_uid, actor_email, behalf_uid, behalf_email = build_audit_actor(baseuser)
        log_audit_event(
            action=CONTACT_GROUP_DELETED,
            endpoint="/users/delete_group",
            ip=request.remote_addr,
            status="success",
            actor_user_id=actor_uid,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=behalf_uid,
            acting_on_behalf_of_email=behalf_email,
            metadata={"group_ids": group_id, "count": len(group_id)},
        )
        g.audit_logged = True

        return (
            jsonify({"message": "Group deleted successfully", "groups": groups_json}),
            200,
        )

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

    finally:
        if connection:
            connection.close()


@permission_required_body("team.search")
@contacts_bp.route("/users/get_all_groups", methods=["POST"])
@permission_required_body("team.group.view")
def get_all_groups():
    connection = None
    try:
        data = request.get_json() or {}
        baseuser = data.get("user_id")

        if not baseuser:
            return jsonify({"error": "user_id is required"}), 400
        logged_in_user_id, user_id = parse_composite_user_id(baseuser)

        connection = connect_to_rds()
        with connection.cursor() as cursor:

            cursor.execute(
                "SELECT groups_json FROM users WHERE user_id = %s", (user_id,)
            )
            row = cursor.fetchone()

            if not row or not row[0]:
                return jsonify({"groups": []}), 200  # return empty list, not error

            try:
                groups_json = json.loads(row[0])
            except:
                return jsonify({"error": "invalid groups JSON"}), 500

            response = []

            for group_id, info in groups_json.items():
                response.append(
                    {
                        "group_id": group_id,
                        "group_name": info.get("group_name"),
                        "count": info.get("count", 0),
                    }
                )

        return jsonify({"groups": response}), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

    finally:
        if connection:
            connection.close()
