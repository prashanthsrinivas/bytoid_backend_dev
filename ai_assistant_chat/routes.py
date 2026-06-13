from db.rds_db import safe_execute
from flask import Flask, request, jsonify, Blueprint, Response
import socket
from db.rds_db import connect_to_rds
import uuid
from datetime import datetime, timezone
from utils.s3_utils import (
    delete_folder_from_s3,
    upload_any_file,
    read_json_from_s3,
)
from cust_helpers import pathconfig
import os
import json
from umail_helper.mails_process import generate_subject
from utils.normal import ensure_dir, parse_composite_user_id
from utils.key_rotation_manager import SecureKMSService as _ChatKMSService
_chat_kms = _ChatKMSService()


def _enc_body(user_id, v):
    if not v:
        return v
    enc = _chat_kms.encrypt(user_id, str(v))
    return {"ciphertext": enc["ciphertext"], "iv": enc["iv"], "encrypted_key": enc["encrypted_key"]}


def _dec_body(user_id, v):
    if isinstance(v, dict) and "encrypted_key" in v:
        return _chat_kms.decrypt(user_id, v["encrypted_key"], v["iv"], v["ciphertext"])
    return v

import dns.resolver
import smtplib
import re
from umail_lance.umail_lance_agent import UmailLanceClient
from umail_helper.ticketalloc import TicketAllocator
import asyncio
import traceback
import logging
from functools import partial
from utils.celery_base import enqueue_user_task

ai_assistant_chat_bp = Blueprint("ai_assistant_chat", __name__)


COMMON_MAIL_PROVIDERS = {
    "gmail.com",
    "googlemail.com",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "msn.com",
    "yahoo.com",
    "ymail.com",
    "rocketmail.com",
    "icloud.com",
    "me.com",
    "mac.com",
    "aol.com",
    "zoho.com",
    "protonmail.com",
    "proton.me",
    "gmx.com",
    "gmx.net",
    "mail.com",
}

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


@ai_assistant_chat_bp.route("/verify_domain", methods=["POST"])
def verify_domain():

    data = request.get_json()
    email = data.get("email", "").strip()

    if "@" not in email:
        return jsonify({"result": "False", "reason": "Invalid email format"}), 400

    domain = email.split("@")[1].lower()

    # ✅ Step 1: check against common providers
    if domain in COMMON_MAIL_PROVIDERS:
        return (
            jsonify(
                {"result": "True", "reason": "Common provider, no further check needed"}
            ),
            200,
        )

    # ⬇️ Step 2: Add your custom domain verification (MX, Verifalia, etc.)
    # TODO: implement your further checking logic here
    return jsonify({"result": "Pending check", "domain": domain}), 200


def save_new_contact(conn, user_id, first_name, last_name, phone_number, email_id):
    cursor = None
    try:
        cursor = conn.cursor()

        communication_id = str(uuid.uuid4())
        users_clients_id = str(uuid.uuid4())

        dt_utc = datetime.now(timezone.utc)
        created_date = dt_utc.strftime("%Y-%m-%d %H:%M:%S")  # For DB string
        updated_date = dt_utc.isoformat()

        last_name = last_name or None
        phone_number = phone_number or None

        # Step 1: Insert into communication table
        safe_execute(
            cursor,
            """
            INSERT INTO communication (
                communication_id,
                user_id_fk,
                users_clients_id_fk
            )
            VALUES (%s, %s, NULL)
            """,
            (communication_id, user_id),
        )

        # Step 2: Insert into users_clients table
        safe_execute(
            cursor,
            """
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
            """,
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

        # Step 3: Link communication → user client
        safe_execute(
            cursor,
            """
            UPDATE communication
            SET users_clients_id_fk = %s
            WHERE communication_id = %s
            """,
            (users_clients_id, communication_id),
        )

        # Commit once after all steps
        conn.commit()
        return users_clients_id

    except Exception as e:
        if conn:
            conn.rollback()
        # print(f"❌ Error saving new contact: {e}")
        raise

    finally:
        if cursor:
            cursor.close()


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
        logged_in_user_id, userid = parse_composite_user_id(userid)

        # Check if email already exists
        cursor.execute(
            """  SELECT users_clients_id FROM users_clients uc 
                            JOIN communication c 
                            ON c.users_clients_id_fk = uc.users_clients_id 
                            WHERE c.user_id_fk = %s 
                            AND uc.email_id = %s
                        """,
            (user_id, email),
        )
        existing_client = cursor.fetchone()

        if existing_client:
            client_id = existing_client[0]
            return (
                jsonify({"message": "Client already exists", "client_id": client_id}),
                200,
            )

        # Insert new client
        client_id = save_new_contact(conn, user_id, first_name, last_name, phone, email)

        return (
            jsonify({"message": "Client added successfully", "client_id": client_id}),
            201,
        )

    except Exception as e:
        # print(f"Error in verify_contact: {e}")
        return jsonify({"error": "Something went wrong"}), 500

    finally:
        if "cursor" in locals():
            cursor.close()
        if "conn" in locals():
            conn.close()


def check_for_conv_file(user_id, client_id):

    s3_config_key = f"{user_id}/messages/{client_id}/config.json"
    s3_data = read_json_from_s3(s3_config_key)

    if s3_data:
        return s3_data.get("ai_assistant_convid")  # returns None if key missing
    return None


def update_existing_conv(user_id, client_id, conv_id, email, query, bot_response):
    s3_key = f"{user_id}/messages/{client_id}/{conv_id}.json"
    s3_data = read_json_from_s3(s3_key)
    input_data = s3_data.get("input_data", [])
    # Lazy migration: decrypt existing messages that are in plaintext
    was_migrated = False
    for msg in input_data:
        raw_body = msg.get("body")
        if isinstance(raw_body, str) and raw_body:
            was_migrated = True
        msg["body"] = _dec_body(user_id, raw_body) if raw_body is not None else raw_body

    conversation_id = ticket_id = ticket_name = "", "", ""

    dt = datetime.now(timezone.utc).replace(microsecond=0)
    timestamp = dt.isoformat()

    conv_folder = os.path.join(pathconfig.basepath, "messages", user_id, client_id)
    ensure_dir(conv_folder)
    conv_file_name = f"{conv_id}.json"
    conv_filepath = os.path.join(conv_folder, conv_file_name)
    s3_config_key = f"{user_id}/messages/{client_id}/{conv_id}.json"

    if input_data:
        if isinstance(input_data, list):
            last_message = input_data[-1]
            conversation_id = last_message.get("conversation_id")
            ticket_id = last_message.get("ticket_id")
            ticket_name = last_message.get("ticket_name")

        elif isinstance(input_data, dict):
            last_message = input_data
            conversation_id = last_message.get("conversation_id")
            ticket_id = last_message.get("ticket_id")
            ticket_name = last_message.get("ticket_name", "")

    input_messages = [(query, "inbound"), (bot_response, "outbound")]
    for item, direction in input_messages:
        from_ = "Assistant" if direction == "outbound" else email
        to = "Assistant" if direction == "inbound" else email
        msg_id = str(uuid.uuid4())

        message = {
            "id": msg_id,
            "from": from_,
            "to": to,
            "body": item,
            "timestamp": timestamp,
            "source": "website",
            "direction": direction,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "type": "Customer",
            "ticket_id": ticket_id,
            "ticket_name": ticket_name,
        }

        input_data.append(message)

    # Encrypt body fields before persisting
    import copy
    input_data_to_save = copy.deepcopy(input_data)
    for msg in input_data_to_save:
        if isinstance(msg.get("body"), str):
            msg["body"] = _enc_body(user_id, msg["body"])

    with open(conv_filepath, "w", encoding="utf-8") as f:
        json.dump({"input_data": input_data_to_save}, f, indent=2)

    upload_any_file(
        conv_filepath,
        user_id,
        type="messages",
        s3_key_C=s3_config_key,
    )

    return input_data


async def create_new_conv(user_id, client_id, email, query, bot_response):

    ticket_allocator = await TicketAllocator.create(user_id)
    ticket_number = await ticket_allocator.next_ticket()
    ticket_id = f"TKT-{ticket_number}#{uuid.uuid4()}"
    await ticket_allocator.finalize()

    conversation_id = str(uuid.uuid4())
    ticket_name = ""

    # dt_utc = datetime.now(timezone.utc)
    # timestamp = dt_utc.strftime("%Y-%m-%d %H:%M:%S")

    dt = datetime.now(timezone.utc).replace(microsecond=0)
    timestamp = dt.isoformat()

    conv_folder = os.path.join(pathconfig.basepath, "messages", user_id, client_id)
    ensure_dir(conv_folder)
    conv_file_name = f"{conversation_id}.json"
    conv_filepath = os.path.join(conv_folder, conv_file_name)
    s3_config_key = f"{user_id}/messages/{client_id}/{conversation_id}.json"

    # generating ticket name using query
    sample_id = str(uuid.uuid4())
    initial_message = {
        "id": sample_id,
        "from": "user",
        "to": "client",
        "body": query,
        "timestamp": timestamp,
        "channel": "browser",
        "direction": "inbound",
    }

    # Save temporary file for subject generation
    with open(conv_filepath, "w", encoding="utf-8") as f:
        json.dump({"new_messages": [initial_message]}, f, indent=2)

    # Generate subject using AI
    subject = await generate_subject(user_id, conv_filepath, "browser")
    ticket_name = subject[0]["summary"]

    s3_key = f"{user_id}/messages/{client_id}/{conversation_id}.json"
    s3_data = read_json_from_s3(s3_key)

    input_data = []
    messages_id = []
    input_messages = [(query, "inbound"), (bot_response, "outbound")]
    for item, direction in input_messages:
        from_ = "Assistant" if direction == "outbound" else email
        to = "Assistant" if direction == "inbound" else email
        msg_id = str(uuid.uuid4())

        message = {
            "id": msg_id,
            "from": from_,
            "to": to,
            "body": item,
            "timestamp": timestamp,
            "source": "website",
            "direction": direction,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "type": "Customer",
            "ticket_id": ticket_id,
            "ticket_name": ticket_name,
        }

        input_data.append(message)
        messages_id.append(msg_id)

    # Encrypt body fields before persisting
    import copy
    input_data_to_save = copy.deepcopy(input_data)
    for msg in input_data_to_save:
        if isinstance(msg.get("body"), str):
            msg["body"] = _enc_body(user_id, msg["body"])

    with open(conv_filepath, "w", encoding="utf-8") as f:
        json.dump({"input_data": input_data_to_save}, f, indent=2)

    upload_any_file(
        conv_filepath,
        user_id,
        type="messages",
        s3_key_C=s3_config_key,
    )

    return input_data


def table_insertion(status, conn, client_id, user_id, input_data):
    """
    Safely insert or update threads, tickets, messages, and assigned tables.
    Uses safe_execute to handle deadlocks.
    """
    cursor = None
    try:
        cursor = conn.cursor()

        input_msg = input_data[0]
        ticket_id = input_msg["ticket_id"]
        ticket_name = input_msg["ticket_name"]
        conversation_id = input_msg["conversation_id"]
        timestamp = input_msg["timestamp"]
        user_id = input_msg["user_id"]
        cont_ref = f"{user_id}/messages/{client_id}/{conversation_id}.json"

        communication_id = ""

        # Fetch communication_id for this user-client pair
        cursor.execute(
            "SELECT communication_id FROM communication WHERE user_id_fk = %s AND users_clients_id_fk = %s",
            (user_id, client_id),
        )
        comm_row = cursor.fetchone()
        if comm_row:
            communication_id = comm_row[0]

        for msg in input_data:
            # Skip if message already exists
            cursor.execute("SELECT 1 FROM messages WHERE message_id = %s", (msg["id"],))
            if cursor.fetchone():
                continue

            if status == "new":
                # Skip if thread already exists
                cursor.execute(
                    "SELECT 1 FROM tickets WHERE tickets_id = %s",
                    (ticket_id,),
                )
                if not cursor.fetchone():
                    safe_execute(
                        cursor,
                        """
                        INSERT INTO tickets (
                            tickets_id, ticket_name, status,
                            priority, created_in, updated_in, communication_id_fk
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            ticket_id,
                            ticket_name,
                            "Open",
                            "Medium",
                            timestamp,
                            timestamp,
                            communication_id,
                        ),
                    )

                    safe_execute(
                        cursor,
                        """
                        INSERT INTO threads (
                            conversation_id, started_at, status,
                            last_message_at, external_user_id, ticket_id_fk
                        )
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            conversation_id,
                            timestamp,
                            "Open",
                            timestamp,
                            user_id,
                            ticket_id,
                        ),
                    )

                    safe_execute(
                        cursor,
                        """
                        UPDATE tickets
                        SET conversation_id_fk = %s
                        WHERE tickets_id = %s
                        """,
                        (conversation_id, ticket_id),
                    )

                    assigned_id = str(uuid.uuid4())
                    safe_execute(
                        cursor,
                        """
                        INSERT INTO assigned (assigned_id, user_id_fk, users_clients_id_fk, ticket_id_fk)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (assigned_id, user_id, client_id, ticket_id),
                    )

            else:
                # Update existing thread/ticket
                safe_execute(
                    cursor,
                    "UPDATE threads SET last_message_at = %s WHERE conversation_id = %s",
                    (timestamp, conversation_id),
                )

                safe_execute(
                    cursor,
                    "UPDATE tickets SET updated_in = %s, status = %s WHERE conversation_id_fk = %s",
                    (timestamp, "In-Progress", conversation_id),
                )

            # Insert message safely
            safe_execute(
                cursor,
                """
                INSERT INTO messages (
                    message_id, conversation_id_fk, sender_id, content_ref,
                    message_type, is_summary, created_at, update_at, sender_type
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    msg["id"],
                    conversation_id,
                    client_id,
                    cont_ref,
                    msg["direction"],
                    ticket_name,
                    timestamp,
                    timestamp,
                    msg["source"],
                ),
            )

        # Commit once after all inserts/updates
        conn.commit()
        return conversation_id

    except Exception as e:
        if conn:
            conn.rollback()
        # print("⚠️ Error while adding entries into table:", e)
        raise
    finally:
        if cursor:
            cursor.close()


def update_config_file(client_id, parsed_timestamp, status, input_data):

    input_msg = input_data[0]
    user_id = input_msg["user_id"]
    conversation_id = input_msg["conversation_id"]
    timestamp = input_msg["timestamp"]
    ticket_name = input_msg["ticket_name"]
    ticket_id = input_msg["ticket_id"]

    config_folder = os.path.join(pathconfig.basepath, "messages", user_id, client_id)
    ensure_dir(config_folder)
    config_filepath = os.path.join(config_folder, "config.json")

    s3_config_key = f"{user_id}/messages/{client_id}/config.json"
    try:
        config_data = read_json_from_s3(s3_config_key) or {}
    except FileNotFoundError:
        config_data = {}

    if config_data:
        conversations = config_data.setdefault("conversations", [])

        # Flag to track if conversation exists
        updated = False

        for conv in conversations:
            if conv.get("conv_id") == conversation_id:
                # Update existing conversation
                conv["updated_date"] = timestamp
                conv["parsed_timestamp"] = parsed_timestamp
                conv["ticket_name"] = conv.get("ticket_name") or ticket_name
                updated = True
                break  # Found and updated, exit loop

        # If not found, append a new conversation
        if not updated:
            config_entry = {
                "conv_id": conversation_id,
                "ticket_id": ticket_id,
                "ticket_name": ticket_name,
                "channel": "website",
                "updated_date": timestamp,
                "parsed_timestamp": parsed_timestamp,
            }
            conversations.append(config_entry)
    else:
        config_entry = {
            "conv_id": conversation_id,
            "ticket_id": ticket_id,
            "ticket_name": ticket_name,
            "channel": "website",
            "updated_date": timestamp,
            "parsed_timestamp": parsed_timestamp,
        }
        config_data.setdefault("userclients_id", client_id)
        config_data.setdefault("conversations", []).append(config_entry)

    if not config_data.get("ai_assistant_convid"):
        config_data["ai_assistant_convid"] = conversation_id

    # Save locally
    with open(config_filepath, "w", encoding="utf-8") as f:
        json.dump(config_data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())

    # Upload to S3
    upload_any_file(
        config_filepath,
        user_id,
        type="messages",
        s3_key_C=s3_config_key,
    )

    return config_data


@ai_assistant_chat_bp.route("/process_website_msg", methods=["POST"])
async def process_website_msg():
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    client_id = data.get("client_id")
    query = data.get("query")
    bot_response = data.get("response")
    if not user_id or not client_id or not query:
        return jsonify({"error": "user_id, client_id, and query are required"}), 400
    logged_in_user_id, user_id = parse_composite_user_id(user_id)

    conn = connect_to_rds()
    cursor = conn.cursor()

    status = ""
    email = ""
    dt_utc = datetime.now(timezone.utc)
    timestamp = dt_utc.strftime("%Y-%m-%d %H:%M:%S")
    parsed_timestamp = dt_utc.isoformat()

    conv_id = ""
    conversation_id = ""

    cursor.execute(
        "SELECT email_id FROM users_clients WHERE users_clients_id = %s", (client_id,)
    )
    email_row = cursor.fetchone()
    if email_row:
        email = email_row[0]

    # checking if existing conversation file exist
    conv_id = check_for_conv_file(user_id, client_id)

    # checking for existing conv file or creating new one
    if conv_id:
        input_data = update_existing_conv(
            user_id, client_id, conv_id, email, query, bot_response
        )
        status = "existing"
    # print("existing")
    else:
        input_data = await create_new_conv(
            user_id, client_id, email, query, bot_response
        )
        status = "new"
    # print("new")
    if not input_data:
        return jsonify({"error": "Failed to prepare conversation data"}), 500

    # insert into tables in db
    conversation_id = table_insertion(status, conn, client_id, user_id, input_data)
    if not conversation_id:
        return jsonify({"error": "Failed to insert into tables"}), 500

    # update config file
    config_data = update_config_file(client_id, parsed_timestamp, status, input_data)
    if not config_data:
        return jsonify({"error": "Failed to update config data"}), 500

    # After conversation_id and input_data are ready
    payload = {
        "user_id": user_id,
        "client_id": client_id,
        "conversation_id": conversation_id,
        "input_data": input_data,
    }

    await enqueue_user_task(user_id, payload)

    return jsonify({"message": "success", "conversation_id": conversation_id})


@ai_assistant_chat_bp.route("/get_website_msg", methods=["POST"])
def get_website_msg():

    data = request.json
    # print(f"📥 [DEBUG] Request data: {data}")

    user_id = data.get("user_id")
    client_id = data.get("client_id")
    conversation_id = ""
    logged_in_user_id, user_id = parse_composite_user_id(user_id)

    conversation_id = check_for_conv_file(user_id, client_id)
    if conversation_id:
        client = UmailLanceClient(user_id)
        results = client.get_conv_from_lance(conversation_id, user_id, client_id)

        s3_key = f"{user_id}/messages/{client_id}/{conversation_id}.json"
        s3_data = read_json_from_s3(s3_key)
        raw_summary = s3_data.get("conversation_summary", "")
        conversation_summary = _dec_body(user_id, raw_summary)

        return jsonify(
            {"results": results, "conversation_summary": conversation_summary}
        )

    return jsonify([])


def close_ticket(user_id, client_id):

    config_folder = os.path.join(pathconfig.basepath, "messages", user_id, client_id)
    ensure_dir(config_folder)
    config_filepath = os.path.join(config_folder, "config.json")

    s3_config_key = f"{user_id}/messages/{client_id}/config.json"
    config_data = read_json_from_s3(s3_config_key)

    if config_data:
        conv_id = config_data.get("ai_assistant_convid")
        if conv_id:  # only if it has a value
            config_data["ai_assistant_convid"] = ""  # reset

            # Save locally
            with open(config_filepath, "w", encoding="utf-8") as f:
                json.dump(config_data, f, indent=2)

            # Upload to S3
            upload_any_file(
                config_filepath,
                user_id,
                type="messages",
                s3_key_C=s3_config_key,
            )
            return conv_id  # return old value

    return None


@ai_assistant_chat_bp.route("/close_ticket_for_assistant", methods=["POST"])
def close_ticket_for_assistant():

    data = request.json
    # print(f"📥 [DEBUG] Request data: {data}")

    user_id = data.get("user_id")
    client_id = data.get("client_id")

    return jsonify({"message": "ticket not found"}), 404


@ai_assistant_chat_bp.route("/update_summary", methods=["POST"])
def update_summary():

    try:

        data = request.get_json(silent=True) or {}
        # print(f"📥 [DEBUG] Request data: {data}")

        user_id = data.get("user_id")
        client_id = data.get("client_id")
        conversation_summary = data.get("conversation_summary")
        if not user_id or not client_id:
            return jsonify({"error": "user_id and client_id are required"}), 400
        logged_in_user_id, user_id = parse_composite_user_id(user_id)

        conversation_id = check_for_conv_file(user_id, client_id)
        s3_key = f"{user_id}/messages/{client_id}/{conversation_id}.json"
        s3_data = read_json_from_s3(s3_key)

        conv_folder = os.path.join(pathconfig.basepath, "messages", user_id, client_id)
        ensure_dir(conv_folder)
        conv_file_name = f"{conversation_id}.json"
        conv_filepath = os.path.join(conv_folder, conv_file_name)
        s3_config_key = f"{user_id}/messages/{client_id}/{conversation_id}.json"

        s3_data["conversation_summary"] = _enc_body(user_id, conversation_summary) if conversation_summary else conversation_summary

        # Save locally
        with open(conv_filepath, "w", encoding="utf-8") as f:
            json.dump(s3_data, f, indent=2)

        # Upload to S3
        upload_any_file(
            conv_filepath,
            user_id,
            type="messages",
            s3_key_C=s3_config_key,
        )

        return jsonify({"message": "conversation_summary update successful"}), 200

    except Exception as e:
        # print(f"Error in update summary: {e}")
        return jsonify({"error": "Something went wrong", "details": e}), 500
