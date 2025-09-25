from flask import Flask, request, jsonify, Blueprint, Response
import socket
from create_db import connect_to_rds
import uuid
from datetime import datetime, timezone
from utils.s3_utils import (
    delete_folder_from_s3,
    upload_any_file,
    read_json_from_s3,
)





ai_assistant_chat_bp = Blueprint("ai_assistant_chat", __name__)

@ai_assistant_chat_bp.route("/verify_domain", methods=["POST"])
def verify_domain():

    """
    Verify if the domain part of an email address is valid.
    
    Args:
        email (str): Email address to verify
        
    Returns:
        bool: True if domain is valid, False otherwise
    """
    data = request.get_json()
    email = data.get("email")
   
    try:
    
        domain = email.split('@')[1]
        
        # Try to resolve the domain using DNS lookup
        socket.gethostbyname(domain)
        return True
    
    except (IndexError, socket.gaierror):
        # IndexError: Invalid email format (no @ symbol)
        # socket.gaierror: Domain does not exist
        return False

def save_new_contact(cursor, user_id, first_name, last_name, phone_number, email_id):
                
        print("creating new user client and communication table")
        communication_id = str(uuid.uuid4())
        users_clients_id = str(uuid.uuid4())

        dt_utc = datetime.now(timezone.utc)
        created_date = dt_utc.strftime("%Y-%m-%d %H:%M:%S")  # For database (string)
        updated_date = dt_utc.isoformat()

        last_name = last_name or None
        phone_number = phone_number or None


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
                        email_id,
                        type,
                        created_in,
                        updated_in,
                        snooze
                        
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
        cursor.execute(
                    insert_sql,
                    (
                        users_clients_id,
                        communication_id,
                        first_name,
                        last_name,
                        phone_number,
                        email_id,
                        "Lead",
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

        return users_clients_id



@ai_assistant_chat_bp.route("/verify_contact", methods=["POST"])
def verify_contact():
    try:
        # Get JSON data from request
        data = request.get_json()
        name = data.get("name", "").strip()
        email = data.get("email", "").strip()
        phone = data.get("phone", None)  # optional
        user_id = data.get("user_id")

        if not name or not email:
            return jsonify({"error": "Name and email are required"}), 400

        # Split name into first name and last name
        name_parts = name.split()
        first_name = name_parts[0]
        last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else None

        # Connect to DB
        conn = connect_to_rds()
        cursor = conn.cursor()

        # Check if email already exists
        cursor.execute("SELECT 1 FROM users_clients WHERE email = %s", (email,))
        existing_client = cursor.fetchone()

        if existing_client:
            return jsonify({"message": "Client already exists"}), 200

        # Insert new client
        client_id = save_new_contact(cursor, user_id, first_name, last_name, phone, email)

        return jsonify({"message": "Client added successfully",
                        "client_id": client_id}), 201

    except Exception as e:
        print(f"Error in verify_contact: {e}")
        return jsonify({"error": "Something went wrong"}), 500

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()


def check_for_conv_file(user_id, client_id):
        
    s3_config_key = f"{user_id}/messages/{client_id}/config.json"
    s3_data = read_json_from_s3(s3_config_key)
    conv_id = ""
    if s3_data:
        conv_id = s3_data.get("ai_assistant_convid", "")
        if not conv_id:
            return None

    conversation = get_conv_from_lance()
