from flask import Flask, request, jsonify, Blueprint, Response
from utils.s3_utils import  read_json_from_s3, list_all_files, upload_any_file
from create_db import connect_to_rds
import json
import os
from cust_helpers import pathconfig
from utils.normal import ensure_dir




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
                t.ticket_name,
                t.assignee
            FROM tickets t
            INNER JOIN assigned a ON t.tickets_id = a.ticket_id_fk
            INNER JOIN users_clients uc ON a.users_clients_id_fk = uc.users_clients_id
        """
        cursor.execute(query)
        rows = cursor.fetchall()
            
        tickets = []
        
        for row in rows:
            # ticket_id, priority, status, created_in, updated_in, conversation_id = row

            first_name, ticket_id, status, SLA, created_in, updated_in,conversation_id,priority,ticket_name, assignee_id = row

            key = conv_key_map.get(conversation_id)
            if key:
                        # Get conversation details from JSON file
                    conversation_data = get_conversation_details(key)

                    print(f"assignee_id : {assignee_id}")
                    cursor.execute("SELECT first_name, last_name, email FROM users WHERE user_id = %s", (assignee_id,))
                    names = cursor.fetchone()  

                    assignee_full_name = ""       
                    if names:  
                        
                        first_name, last_name, assignee_email = names[0], names[1], names[2]
                        if not first_name or first_name == "None":
                            first_name = assignee_email.split('@')[0]
                        if not last_name or last_name == "None":
                            last_name = ""

                        assignee_full_name = (first_name + " " + last_name).strip()
                                    

                        
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
                            "assigned_name": assignee_full_name,
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
        print(f"key : {key}")
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
        print(f"[ERROR] JSON decode failed for {key}: {e}")
        return {}
    except Exception as e:
        print(f"[ERROR] Failed to read conversation file {key}: {e}")
        return {}

@tickets_bp.route("/get_status_priority/<ticket_id>/<user_id>", methods=["GET"])
def get_status_priority(ticket_id, user_id):
    if not ticket_id:
        return {"status": None, "priority": None, "ticket_name": None}

    conn = connect_to_rds()
    cursor = conn.cursor()



    try:
        print(f"ticket_id : {ticket_id}, user_id : {user_id}")
        cursor.execute(
            "SELECT t.status, t.priority, t.ticket_name, t.conversation_id_fk, t.tickets_id "
            "FROM tickets t "
            "JOIN communication c ON t.communication_id_fk = c.communication_id "
            "WHERE t.tickets_id LIKE %s AND c.user_id_fk = %s",
            (f'{ticket_id}#%', user_id)
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
            print(f"{ticket_row[0]}, {ticket_row[1]}, {ticket_row[2]} {ticket_row[4]}")
            return {
                "status": ticket_row[0],
                "priority": ticket_row[1],
                "ticket_name": ticket_row[2],
                "ticket_conversation_id":ticket_row[3],
                "ticket_id":ticket_id,
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
    conversation_id = data.get("conversation_id")
    user_id = data.get("user_id")
    if not new_ticket_name or not ticket_id :
        return jsonify({"error": "Missing ticket_name or ticket_id"}), 400

    conn = connect_to_rds()
    cursor = conn.cursor()

    # update tickets table
    try:
        cursor.execute(
                "UPDATE tickets SET ticket_name = %s WHERE tickets_id = %s",
                (new_ticket_name, ticket_id)
            )
        conn.commit()
        print("tickets table successfull updated")

        # update conversation file
        cursor.execute(
                "SELECT content_ref,sender_id from messages WHERE conversation_id_fk = %s",
                (conversation_id,)
            )
        message_row = cursor.fetchone()       

        if not message_row:
            return jsonify({"error": "Conversation not found"}), 404 
         
       
        conv_key= message_row[0] 
        print(f"conv_key is : {conv_key}")
        client_id = message_row[1]         
        conv_data = read_json_from_s3(conv_key)
        if conv_data :
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

            config_folder = os.path.join(pathconfig.basepath, "messages", user_id, client_id)
            ensure_dir(config_folder)
            config_filepath = os.path.join(config_folder, "config.json")
            
            with open(config_filepath, "w", encoding="utf-8") as f:
                json.dump(config_data, f, indent=2)

            upload_any_file(
                config_filepath,
                user_id,
                type="messages",
                s3_key_C = s3_config_key,
            )
        
    except Exception as e:
        print(f"❌ Error during ticket name update: {str(e)}")        
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

    conn = connect_to_rds()
    cursor = conn.cursor()

    try:
        cursor.execute(
                "UPDATE tickets SET priority = %s WHERE tickets_id = %s",
                (priority, ticket_id)
            )
        conn.commit()
        print("priority successfully updated")

    except Exception as e:
        print(f"❌ Error during priority update: {str(e)}")        
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

    conn = connect_to_rds()
    cursor = conn.cursor()

    try:
        cursor.execute(
                "UPDATE tickets SET status = %s WHERE tickets_id = %s",
                (status, ticket_id)
            )
        conn.commit()
        print("status successfully updated")

    except Exception as e:
        print(f"❌ Error during status update: {str(e)}")        
        return jsonify({"error": "Failed to update status"}), 500
            
    finally:
        cursor.close()
        conn.close()

    return jsonify({"message": "status updated successfully"}), 20