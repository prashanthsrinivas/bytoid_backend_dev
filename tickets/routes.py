from flask import Flask, request, jsonify, Blueprint, Response
from utils.s3_utils import  read_json_from_s3, list_all_files
from create_db import connect_to_rds
import json



tickets_bp = Blueprint("tickets", __name__)

@tickets_bp.route("/tickets/<user_id>", methods=["GET"])
def get_user_tickets(user_id):
    
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400

    try:

        # list out the conv files and build lookup
        prefix = f"{user_id}/messages/"
        file_list = list_all_files(prefix)

        conv_key_map = {}
        for file_obj in file_list:
            key = file_obj["Key"]
            parts = key.split("/")
            if len(parts) >= 4 and parts[-1].endswith(".json"):
                conv_id = parts[-1].replace(".json", "")
                conv_key_map[conv_id] = key

        # fetch the details from table
        conn = connect_to_rds()
        cursor = conn.cursor()

        query = """
            SELECT 
                uc.first_name,
                t.tickets_id,
                t.status,
                t.SLA,
                t.created_in,
                t.updated_in,
                t.conversation_id_fk,
                t.priority,
                t.ticket_name
            FROM tickets t
            INNER JOIN assigned a ON t.tickets_id = a.ticket_id_fk
            INNER JOIN users_clients uc ON a.users_clients_id_fk = uc.users_clients_id
        """
        cursor.execute(query)
        rows = cursor.fetchall()
            
        tickets = []
        
        for row in rows:
            # ticket_id, priority, status, created_in, updated_in, conversation_id = row

            first_name, ticket_id, status, SLA, created_in, updated_in,conversation_id,priority,ticket_name = row

            key = conv_key_map.get(conversation_id)
            if key:
                        # Get conversation details from JSON file
                    conversation_data = get_conversation_details(key)
                        
                        # Build ticket response
                    ticket_info = {
                            "first_name":first_name,
                            "ticket_id": ticket_id,
                            "priority": priority,
                            "SLA" : SLA,
                            "status": status,
                            "created_in": created_in,
                            "updated_in": updated_in,
                            "conversation_id": conversation_id,
                            "conversation": conversation_data.get("body", ""),
                            "channel": conversation_data.get("source", ""),
                            "subject": ticket_name,
                            "from": conversation_data.get("from", ""),
                        }
                        
                    tickets.append(ticket_info)

            else:
                    print(f"[WARN] No S3 file found for conversation_id: {conversation_id}")
                        
        
        cursor.close()
        conn.close()
        return jsonify({
            "success": True,
            "tickets": tickets,
            "total_count": len(tickets)
        }), 200
        
    except Exception as e:
        print(f"Error fetching tickets: {str(e)}")
        return jsonify({"error": "Failed to fetch tickets"}), 500


def get_conversation_details(key):
    try:
        data = read_json_from_s3(key)

        input_data = data.get("input_data", [])
        if input_data:
            conversation = input_data[0]
            return {
                "body": conversation.get("body", ""),
                "subject": conversation.get("subject", ""),
                "source": conversation.get("source", ""),
                "from": conversation.get("from", ""),
            }
        return {}

    except json.JSONDecodeError as e:
        print(f"[ERROR] JSON decode failed for {key}: {e}")
        return {}
    except Exception as e:
        print(f"[ERROR] Failed to read conversation file {key}: {e}")
        return {}

@tickets_bp.route("/get_status_priority/<ticket_id>", methods=["GET"])
def get_status_priority(ticket_id):
    if not ticket_id:
        return {"status": None, "priority": None, "ticket_name": None}

    conn = connect_to_rds()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "SELECT status, priority, ticket_name FROM tickets WHERE tickets_id = %s",
            (ticket_id,)
        )
        ticket_row = cursor.fetchone()
        if ticket_row:
            return {
                "status": ticket_row[0],
                "priority": ticket_row[1],
                "ticket_name": ticket_row[2]
            }
    finally:
        cursor.close()
        conn.close()

    return {"status": None, "priority": None, "ticket_name": None}
