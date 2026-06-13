from flask import Flask, request, jsonify, Blueprint, Response
from utils.s3_utils import read_json_from_s3, list_all_files, upload_any_file
from db.rds_db import connect_to_rds
import json
import os
from cust_helpers import pathconfig
from utils.normal import ensure_dir
from datetime import datetime
from ai_assistant_chat.routes import close_ticket


tickets_bp = Blueprint("tickets", __name__)


@tickets_bp.route("/tickets/<user_id>", methods=["GET"])
def get_user_tickets(user_id):

    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400

    try:

        page_size = int(request.args.get("page_size", 10))
        last_updated_in = request.args.get("last_updated_in")
        if not last_updated_in:
            return jsonify({"error": "last_updated_in is required"}), 400
        last_updated_in = last_updated_in.replace("T", " ")
        total_count_done = (
            request.args.get("total_count_done", "false").lower() == "true"
        )

        conn = connect_to_rds()
        cursor = conn.cursor()

        last_updated_in_batch = ""

        # Get total tickets count only if first request
        total_tickets = None
        if not total_count_done:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM tickets t
                INNER JOIN assigned a ON t.tickets_id = a.ticket_id_fk
                WHERE a.user_id_fk = %s
            """,
                [user_id],
            )
            total_tickets = cursor.fetchone()[0]

        query = """
                SELECT 
                uc.first_name AS client_first_name,
                uc.email_id AS client_email,
                t.tickets_id,
                t.status,
                t.SLA,
                t.created_in,
                t.updated_in,
                t.conversation_id_fk,
                t.priority,
                t.ticket_name,
                assignee_user.first_name AS assignee_first_name,
                assignee_user.last_name  AS assignee_last_name,
                assignee_user.email      AS assignee_email,
                (
                    SELECT m.sender_type
                    FROM messages m
                    WHERE m.sender_id = uc.users_clients_id
                    LIMIT 1
                ) AS channel
            FROM tickets t
            INNER JOIN assigned a 
                ON t.tickets_id = a.ticket_id_fk
            INNER JOIN users_clients uc
                ON a.users_clients_id_fk = uc.users_clients_id
            LEFT JOIN users assignee_user
                ON t.assignee = assignee_user.user_id
            WHERE a.user_id_fk = %s

        """
        params = [user_id]

        # Add cursor condition if last_updated_in is provided
        if last_updated_in:
            query += " AND t.created_in <= %s"
            params.append(last_updated_in)

        query += " ORDER BY t.created_in DESC LIMIT %s"
        params.append(page_size + 1)  # fetch one extra to check has_next

        cursor.execute(query, params)
        rows = cursor.fetchall()

        # determine if there is a next page
        has_next = len(rows) > page_size
        tickets = rows[:page_size]

        # get the last_updated_in for the batch (smallest updated_in if ordering DESC)
        if tickets:
            last_updated_in_batch = tickets[-1][5]
        else:
            last_updated_in_batch = None

        last_updated_in_batch = (
            last_updated_in_batch.strftime("%Y-%m-%d %H:%M:%S")
            if isinstance(last_updated_in_batch, datetime)
            else str(last_updated_in_batch)
        )

        cursor.close()
        conn.close()

        # prepare JSON response
        tickets_data = []
        for row in tickets:

            (
                client_first_name,
                client_email,
                ticket_id,
                status,
                SLA,
                created_in,
                updated_in,
                conversation_id,
                priority,
                ticket_name,
                assignee_first_name,
                assignee_last_name,
                assignee_email,
                channel,
            ) = row

            assignee_full_name = ""
            if (assignee_first_name and assignee_first_name.strip()) or (
                assignee_last_name and assignee_last_name.strip()
            ):
                # Replace None with "" to avoid TypeError
                fn = assignee_first_name or ""
                ln = assignee_last_name or ""
                assignee_full_name = f"{fn} {ln}".strip()
            else:
                assignee_full_name = assignee_email

                # Build ticket response
            ticket_info = {
                "first_name": client_first_name,
                "ticket_id": ticket_id,
                "priority": priority,
                "SLA": SLA,
                "status": status,
                "created_in": created_in,
                "updated_in": updated_in,
                "conversation_id": conversation_id,
                "channel": channel,
                "subject": ticket_name,
                "from": client_email,
                "assigned_name": assignee_full_name,
            }

            tickets_data.append(ticket_info)

        # Build response
        response = {
            "page_size": page_size,
            "has_next": has_next,
            "last_updated_in": last_updated_in_batch,
            "tickets": tickets_data,
        }

        if total_tickets is not None:
            response["total_tickets"] = total_tickets

        return jsonify(response), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@tickets_bp.route("/filter_tickets/<user_id>", methods=["GET"])
def filter_tickets(user_id):
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400

    try:
        # Get query params
        status = request.args.get("status")
        priority = request.args.get("priority")
        sla = request.args.get("sla")
        start_date = request.args.get("start_date")
        if start_date:
            start_date = start_date.replace("T", " ")
        end_date = request.args.get("end_date")
        if end_date:
            end_date = end_date.replace("T", " ")
        channel = request.args.get("channel")
        page_size = int(request.args.get("page_size", 10))
        last_updated_in = request.args.get("last_updated_in")
        if not last_updated_in:
            return jsonify({"error": "last_updated_in is required"}), 400
        last_updated_in = last_updated_in.replace("T", " ")

        #print(f"****dates: {last_updated_in} : {start_date} : {end_date}")

        conn = connect_to_rds()
        cursor = conn.cursor()
        # Base query
        query = """
            SELECT 
                uc.first_name AS client_first_name,
                uc.email_id AS client_email,
                t.tickets_id,
                t.status,
                t.SLA,
                t.created_in,
                t.updated_in,
                t.conversation_id_fk,
                t.priority,
                t.ticket_name,
                assignee_user.first_name AS assignee_first_name,
                assignee_user.last_name  AS assignee_last_name,
                assignee_user.email      AS assignee_email,
                MIN(m.sender_type) AS channel            
            FROM tickets t
            INNER JOIN assigned a ON t.tickets_id = a.ticket_id_fk
            INNER JOIN users_clients uc ON a.users_clients_id_fk = uc.users_clients_id
            LEFT JOIN users assignee_user ON t.assignee = assignee_user.user_id
            LEFT JOIN messages m ON t.conversation_id_fk = m.conversation_id_fk
            WHERE a.user_id_fk = %s
        """
        params = [user_id]

        # Filters
        if status:
            query += " AND t.status = %s"
            params.append(status)
        if priority:
            query += " AND t.priority = %s"
            params.append(priority)
        if sla:
            query += " AND t.SLA = %s"
            params.append(sla)
        if start_date and end_date:
            query += " AND t.updated_in BETWEEN %s AND %s"
            params.extend([start_date, end_date])
        if start_date:
            query += " AND t.updated_in >= %s"
            params.append(start_date)
        if end_date:
            query += " AND t.updated_in <= %s"
            params.append(end_date)
        if channel:
            query += """
                AND EXISTS (
                    SELECT 1
                    FROM messages msg
                    WHERE msg.conversation_id_fk = t.conversation_id_fk
                    AND msg.sender_type = %s
                )
            """
            params.append(channel)

        # Pagination
        if last_updated_in:
            query += " AND t.created_in < %s"
            params.append(last_updated_in)

        # Order & limit
        query += " GROUP BY t.tickets_id  ORDER BY t.created_in DESC LIMIT %s"
        params.append(page_size + 1)  # fetch one extra to check has_next

        cursor.execute(query, params)
        rows = cursor.fetchall()

        # Pagination logic
        has_next = len(rows) > page_size
        tickets = rows[:page_size]

        # Prepare last_updated_in and last_ticket_id for next page
        if tickets:
            last_updated_in_batch = tickets[-1][5]
        else:
            last_updated_in_batch = None

        last_updated_in_batch = (
            last_updated_in_batch.strftime("%Y-%m-%d %H:%M:%S")
            if isinstance(last_updated_in_batch, datetime)
            else str(last_updated_in_batch)
        )

        cursor.close()
        conn.close()

        # prepare JSON response
        tickets_data = []
        for row in tickets:

            (
                client_first_name,
                client_email,
                ticket_id,
                status,
                SLA,
                created_in,
                updated_in,
                conversation_id,
                priority,
                ticket_name,
                assignee_first_name,
                assignee_last_name,
                assignee_email,
                channel,
            ) = row

            assignee_full_name = ""
            if (assignee_first_name and assignee_first_name.strip()) or (
                assignee_last_name and assignee_last_name.strip()
            ):
                # Replace None with "" to avoid TypeError
                fn = assignee_first_name or ""
                ln = assignee_last_name or ""
                assignee_full_name = f"{fn} {ln}".strip()
            else:
                assignee_full_name = assignee_email

                # Build ticket response
            ticket_info = {
                "first_name": client_first_name,
                "ticket_id": ticket_id,
                "priority": priority,
                "SLA": SLA,
                "status": status,
                "created_in": created_in,
                "updated_in": updated_in,
                "conversation_id": conversation_id,
                "channel": channel,
                "subject": ticket_name,
                "from": client_email,
                "assigned_name": assignee_full_name,
            }

            tickets_data.append(ticket_info)

        # Build response
        response = {
            "page_size": page_size,
            "has_next": has_next,
            "last_updated_in": last_updated_in_batch,
            "tickets": tickets_data,
        }

        return jsonify(response), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@tickets_bp.route("/search_tickets/<user_id>", methods=["GET"])
def search_tickets(user_id):

    try:
        search_word = request.args.get("search_word")
        search_pattern = f"%{search_word}%"
        page_size = int(request.args.get("page_size", 10))
        last_updated_in = request.args.get("last_updated_in")
        if not last_updated_in:
            return jsonify({"error": "last_updated_in is required"}), 400
        last_updated_in = last_updated_in.replace("T", " ")

        conn = connect_to_rds()
        cursor = conn.cursor()

        # Base query
        query = """
                SELECT 
                    uc.first_name AS client_first_name,
                    uc.email_id AS client_email,
                    t.tickets_id,
                    t.status,
                    t.SLA,
                    t.created_in,
                    t.updated_in,
                    t.conversation_id_fk,
                    t.priority,
                    t.ticket_name,
                    assignee_user.first_name AS assignee_first_name,
                    assignee_user.last_name  AS assignee_last_name,
                    assignee_user.email      AS assignee_email,
                    MIN(m.sender_type) AS channel            
                FROM tickets t
                INNER JOIN assigned a ON t.tickets_id = a.ticket_id_fk
                INNER JOIN users_clients uc ON a.users_clients_id_fk = uc.users_clients_id
                LEFT JOIN users assignee_user ON t.assignee = assignee_user.user_id
                LEFT JOIN messages m ON t.conversation_id_fk = m.conversation_id_fk
                WHERE a.user_id_fk = %s
                    AND (
                        t.tickets_id LIKE %s
                        OR t.ticket_name LIKE %s
                        OR uc.first_name LIKE %s
                    )
                    AND t.created_in <= %s
                GROUP BY t.tickets_id
                ORDER BY t.created_in DESC 
                LIMIT %s
            """
        params = [
            user_id,
            search_pattern,
            search_pattern,
            search_pattern,
            last_updated_in,
            page_size + 1,
        ]

        cursor.execute(query, params)
        rows = cursor.fetchall()

        # Pagination logic
        has_next = len(rows) > page_size
        tickets = rows[:page_size]

        # Prepare last_updated_in and last_ticket_id for next page
        if tickets:
            last_updated_in_batch = tickets[-1][5]
        else:
            last_updated_in_batch = None

        last_updated_in_batch = (
            last_updated_in_batch.strftime("%Y-%m-%d %H:%M:%S")
            if isinstance(last_updated_in_batch, datetime)
            else str(last_updated_in_batch)
        )

        cursor.close()
        conn.close()

        # prepare JSON response
        tickets_data = []
        for row in tickets:

            (
                client_first_name,
                client_email,
                ticket_id,
                status,
                SLA,
                created_in,
                updated_in,
                conversation_id,
                priority,
                ticket_name,
                assignee_first_name,
                assignee_last_name,
                assignee_email,
                channel,
            ) = row

            assignee_full_name = ""
            if (assignee_first_name and assignee_first_name.strip()) or (
                assignee_last_name and assignee_last_name.strip()
            ):
                # Replace None with "" to avoid TypeError
                fn = assignee_first_name or ""
                ln = assignee_last_name or ""
                assignee_full_name = f"{fn} {ln}".strip()
            else:
                assignee_full_name = assignee_email

                # Build ticket response
            ticket_info = {
                "first_name": client_first_name,
                "ticket_id": ticket_id,
                "priority": priority,
                "SLA": SLA,
                "status": status,
                "created_in": created_in,
                "updated_in": updated_in,
                "conversation_id": conversation_id,
                "channel": channel,
                "subject": ticket_name,
                "from": client_email,
                "assigned_name": assignee_full_name,
            }

            tickets_data.append(ticket_info)

        # Build response
        response = {
            "page_size": page_size,
            "has_next": has_next,
            "last_updated_in": last_updated_in_batch,
            "tickets": tickets_data,
        }

        return jsonify(response), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


def get_conversation_details(key):
    try:
        #print(f"key : {key}")
        data = read_json_from_s3(key)

        input_data = data.get("input_data", [])
        if input_data:
            if isinstance(input_data, list):
                conversation = input_data[0] if input_data else {}
            else:
                # Handle case where input_data is a single dictionary
                conversation = input_data

            return {
                "body": conversation.get("body", ""),
                "subject": conversation.get("subject", ""),
                "source": conversation.get("source", ""),
                "from": conversation.get("from", ""),
            }
        return {}

    except json.JSONDecodeError as e:
        #print(f"[ERROR] JSON decode failed for {key}: {e}")
        return {}
    except Exception as e:
        #print(f"[ERROR] Failed to read conversation file {key}: {e}")
        return {}


@tickets_bp.route("/get_status_priority/<ticket_id>/<user_id>", methods=["GET"])
def get_status_priority(ticket_id, user_id):
    if not ticket_id:
        #print(f"no ticket_id")
        return {"status": None, "priority": None, "ticket_name": None}

    conn = connect_to_rds()
    cursor = conn.cursor()

    try:
        #print(f"ticket_id : {ticket_id}, user_id : {user_id}")
        cursor.execute(
            "SELECT t.status, t.priority, t.ticket_name, t.conversation_id_fk, t.tickets_id "
            "FROM tickets t "
            "JOIN communication c ON t.communication_id_fk = c.communication_id "
            "WHERE t.tickets_id LIKE %s AND c.user_id_fk = %s",
            (f"{ticket_id}#%", user_id),
        )

        # cursor.execute(
        #     "SELECT t.status, t.priority, t.ticket_name, t.conversation_id_fk "
        #     "FROM tickets t "
        #     "WHERE t.tickets_id = %s ",
        #     (ticket_id,)
        # )

        # cursor.execute(
        #     "SELECT t.status, t.priority, t.ticket_name, t.conversation_id_fk "
        #     "FROM tickets t "
        #     "WHERE t.tickets_id LIKE %s ",
        #     (f'{ticket_id}#%')
        # )

        ticket_row = cursor.fetchone()
        if ticket_row:
            #print(f"{ticket_row[0]}, {ticket_row[1]}, {ticket_row[2]} {ticket_row[4]}")
            return {
                "status": ticket_row[0],
                "priority": ticket_row[1],
                "ticket_name": ticket_row[2],
                "ticket_conversation_id": ticket_row[3],
                "ticket_id": ticket_id,
            }
    finally:
        cursor.close()
        conn.close()

    return {"status": None, "priority": None, "ticket_name": None}


@tickets_bp.route("/change_ticket_name", methods=["POST"])
def change_ticket_name():

    data = request.get_json()

    new_ticket_name = data.get("ticket_name")
    ticket_id = data.get("ticket_id")
    #print(f"ticket_id : {ticket_id}")
    conversation_id = data.get("conversation_id")
    user_id = data.get("user_id")
    if not new_ticket_name or not ticket_id:
        return jsonify({"error": "Missing ticket_name or ticket_id"}), 400

    conn = connect_to_rds()
    cursor = conn.cursor()

    # update tickets table
    try:
        cursor.execute(
            "UPDATE tickets SET ticket_name = %s WHERE tickets_id = %s",
            (new_ticket_name, ticket_id),
        )
        conn.commit()
        # print("tickets table successfull updated")

        # update conversation file
        cursor.execute(
            "SELECT content_ref,sender_id from messages WHERE conversation_id_fk = %s",
            (conversation_id,),
        )
        message_row = cursor.fetchone()

        if not message_row:
            return jsonify({"error": "Conversation not found"}), 404

        conv_key = message_row[0]
        #print(f"conv_key is : {conv_key}")
        client_id = message_row[1]
        conv_data = read_json_from_s3(conv_key)
        if conv_data:
            for item in conv_data["input_data"]:
                if "ticket_name" in item:
                    item["ticket_name"] = new_ticket_name

        conv_folder = os.path.join(pathconfig.basepath, "messages", user_id, client_id)
        ensure_dir(conv_folder)
        conv_filepath = os.path.join(conv_folder, f"{conversation_id}.json")

        with open(conv_filepath, "w", encoding="utf-8") as f:
            json.dump(conv_data, f, indent=2)

        upload_any_file(
            conv_filepath,
            user_id,
            type="messages",
            s3_key_C=conv_key,
        )

        # update config file
        s3_config_key = f"{user_id}/messages/{client_id}/config.json"
        config_data = read_json_from_s3(s3_config_key)

        if config_data:
            for convo in config_data["conversations"]:
                if convo.get("ticket_id") == ticket_id:
                    convo["ticket_name"] = new_ticket_name

            config_folder = os.path.join(
                pathconfig.basepath, "messages", user_id, client_id
            )
            ensure_dir(config_folder)
            config_filepath = os.path.join(config_folder, "config.json")

            with open(config_filepath, "w", encoding="utf-8") as f:
                json.dump(config_data, f, indent=2)

            upload_any_file(
                config_filepath,
                user_id,
                type="messages",
                s3_key_C=s3_config_key,
            )

    except Exception as e:
        #print(f"❌ Error during ticket name update: {str(e)}")
        return jsonify({"error": "Failed to update tickets name"}), 500

    finally:
        cursor.close()
        conn.close()

    return jsonify({"message": "Ticket name updated successfully"}), 200


@tickets_bp.route("/change_ticket_priority", methods=["POST"])
def change_ticket_priority():

    data = request.get_json()

    priority = data.get("priority")
    ticket_id = data.get("ticket_id")

    #print(f"ticket_id :{ticket_id}")

    conn = connect_to_rds()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "UPDATE tickets SET priority = %s WHERE tickets_id = %s",
            (priority, ticket_id),
        )
        conn.commit()
    # print("priority successfully updated")

    except Exception as e:
        #print(f"❌ Error during priority update: {str(e)}")
        return jsonify({"error": "Failed to update priority"}), 500

    finally:
        cursor.close()
        conn.close()

    return jsonify({"message": "priority updated successfully"}), 200


@tickets_bp.route("/change_ticket_status", methods=["POST"])
def change_ticket_status():

    data = request.get_json()

    status = data.get("status")
    ticket_id = data.get("ticket_id")
    channel = data.get("channel")
    #print(f"channel : {channel}")
    #print(f"status : {status}")

    conn = connect_to_rds()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "UPDATE tickets SET status = %s WHERE tickets_id = %s", (status, ticket_id)
        )

        if channel == "website" and status == "Solved":
            #print(f"inside con")
            cursor.execute(
                "SELECT user_id_fk,users_clients_id_fk from communication c JOIN tickets t WHERE t.communication_id_fk = c.communication_id AND t.tickets_id = %s",
                (ticket_id,),
            )
            message_row = cursor.fetchone()
            user_id = message_row[0]
            client_id = message_row[1]
            #print(f"user_id : {user_id}")
            #print(f"client_id : {client_id}")

            conversation_id = close_ticket(user_id, client_id)

        conn.commit()

    except Exception as e:
        conn.rollback()
        #print(f"❌ Error during status update: {str(e)}")
        return jsonify({"error": "Failed to update status"}), 500

    finally:
        cursor.close()
        conn.close()

    return (
        jsonify(
            {
                "message": "status updated successfully",
            }
        ),
        200,
    )
