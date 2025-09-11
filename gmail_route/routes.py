import asyncio
from flask import Blueprint, request, jsonify, session
from .gmail_service import GmailService
import uuid
from data import MESSAGES  # delete this later
from bs4 import BeautifulSoup
from email.utils import parsedate_to_datetime, parseaddr
from email.message import EmailMessage
import base64
from datetime import datetime, timezone
import json
from cust_helpers import pathconfig
import os
from utils.normal import ensure_dir
from utils.s3_utils import (
    delete_file_from_s3,
    delete_folder_from_s3,
    list_all_files,
    upload_any_file,
    read_json_from_s3,
)
from create_db import connect_to_rds
from umail_helper.helper import get_users_client_id, extract_reply_content
from collections import defaultdict
from google.auth.exceptions import RefreshError
import traceback
import re


gmail_bp = Blueprint("gmail", __name__)


# @gmail_bp.route("/gmail/fetch")


async def fetch_gmail_messages_batch(user_id, page_token=None, batch_size=100):
    """
    Fetch a single batch of Gmail messages
    """
    try:
        print(f"🚀 Starting Gmail batch fetch for user {user_id}")
        gmail_service = GmailService(user_id)
        user_email = gmail_service.user_email

        # Fetch one batch of messages
        threads, next_page_token = await gmail_service.get_threads_async(
            "INBOX", max_results=batch_size, start_page_token=page_token
        )
        print("threads fetched are", len(threads))
        if not threads:
            print("📭 No threads found in this batch")
            return {"status": "success", "new_messages": 0, "next_page_token": None}

        count_new = 0
        grouped_messages = defaultdict(list)

        # Database connection
        connection = connect_to_rds()
        if connection is None:
            return {"error": "Database connection failed", "status": "failed"}

        cursor = connection.cursor()

        # File setup
        timestamp = datetime.now(timezone.utc)
        date_str = timestamp.strftime("%Y-%m-%d")
        filename = f"{date_str}.json"

        user_folder = os.path.join(pathconfig.basepath, "messages", user_id)
        ensure_dir(user_folder)
        filepath = os.path.join(user_folder, filename)

        # Load existing data
        existing_ids_local = set()
        input_data_local = {}

        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                existing_data_local = json.load(f)
                input_data_local = existing_data_local.get("input_data", {})
            if len(input_data_local) > 0:
                try:
                    # Extract existing message IDs
                    for client_data in input_data_local.values():
                        if isinstance(client_data, dict):
                            for client_channels in client_data.values():
                                if isinstance(client_channels, dict):
                                    for channel_msgs in client_channels.values():
                                        if isinstance(channel_msgs, list):
                                            for msg in channel_msgs:
                                                if isinstance(msg, dict):
                                                    msg_id = msg.get("id")
                                                    if msg_id:
                                                        existing_ids_local.add(msg_id)
                except Exception as e:
                    print(f"⚠️ Error loading existing data: {e}")

        # Process messages (your existing logic)
        email_to_client_id = {}
        configs_created = set()

        for msg in threads:
            message_id = msg["messageId"]

            # Skip if already exists in database
            cursor.execute(
                "SELECT 1 FROM messages WHERE message_id = %s", (message_id,)
            )
            if cursor.fetchone():
                continue

            # Skip if already exists locally
            if message_id in existing_ids_local:
                continue

            # Your existing message processing logic...
            thread_id = msg["thread_id"]
            dt = parsedate_to_datetime(msg["date"])
            timestamp_iso = dt.isoformat()
            direction = "inbound" if msg["from"] != user_email else "outbound"
            subject = msg["subject"]
            body_content = msg.get("body", "")
            plain_text = (
                BeautifulSoup(body_content, "html.parser")
                .get_text(separator="\n")
                .strip()
            )
            extracted_body = extract_reply_content(plain_text)

            from_name, from_email = parseaddr(msg["from"])
            to_name, to_email = parseaddr(msg.get("to", ""))

            if direction == "inbound":
                participant = from_email
                participant_name = from_name
            else:
                participant = to_email
                participant_name = to_name

            # Get or create client
            if participant in email_to_client_id:
                client_id, type = email_to_client_id[participant]
            else:
                client_id, type = get_users_client_id(participant, user_id, cursor)
                if not client_id:
                    client_id = add_lead_contact(
                        user_id, cursor, participant, participant_name
                    )
                    type = "Lead"
                email_to_client_id[participant] = (client_id, type)

            # Create message object
            message = {
                "id": message_id,
                "from": from_email,
                "to": user_email,
                "body": extracted_body,
                "subject": subject,
                "timestamp": timestamp_iso,
                "source": "gmail",
                "direction": direction,
                "user_id": user_id,
                "thread_id": thread_id,
                "conversation_id": from_email if direction == "inbound" else user_email,
                "type": type,
            }

            grouped_messages.setdefault(client_id, {}).setdefault("gmail", []).append(
                message
            )
            count_new += 1

            if client_id not in configs_created:

                # Create config files if needed (your existing logic)
                config_folder = os.path.join(
                    pathconfig.basepath, "messages", user_id, client_id
                )
                ensure_dir(config_folder)
                config_filepath = os.path.join(config_folder, "config.json")

                if not os.path.exists(config_filepath):
                    dummy_config = {
                        "userclients_id": client_id,
                        "conversations": [
                            {
                                "conv_id": "",
                                "ticket_id": "",
                                "ticket_name": "",
                                "subject": "",
                                "channel": "",
                                "updated_date": "",
                                "parsed_timestamp": "",
                                "thread_id": "",
                            }
                        ],
                    }
                    with open(config_filepath, "w", encoding="utf-8") as f:
                        json.dump(dummy_config, f, indent=2)

                    s3_config_key = f"{user_id}/messages/{client_id}/config.json"
                    s3_data = read_json_from_s3(s3_config_key)
                    if s3_data is None:

                        upload_any_file(
                            config_filepath,
                            user_id,
                            type="messages",
                            s3_key_C=s3_config_key,
                        )
                        print(f"uploaded config for client_id: {client_id}")

                configs_created.add(client_id)

        # Merge with existing data and save
        existing_data = {}
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                existing_data = json.load(f)

        merged_messages = existing_data.get("input_data", {})

        for client_id, channels in grouped_messages.items():
            for channel, messages in channels.items():
                merged_messages.setdefault(client_id, {}).setdefault(
                    "gmail", []
                ).extend(messages)

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(
                {"filename": filename, "input_data": merged_messages}, f, indent=2
            )

        cursor.close()
        connection.close()

        print(f"✅ Batch complete: {count_new} new messages processed")
        return {
            "status": "success",
            "new_messages": count_new,
            "next_page_token": next_page_token,
            "grouped_messages": dict(grouped_messages),  # Return current batch data
        }

    except Exception as e:
        print(f"[ERROR] → fetch_gmail_messages_batch failed: {e}")
        return {
            "error": str(e),
            "status": "failed",
            "next_page_token": None,
            "grouped_messages": {},
        }


def safe_json_load(filepath):
    if not os.path.exists(filepath):
        return {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return {}
            return json.loads(content)
    except json.JSONDecodeError:
        print(f"⚠️ Corrupted JSON at {filepath}, resetting.")
        return {}


async def v2fetch_gmail_messages_batch(user_id, threads, my_email, batch_count, cursor):
    """
    Fetch a single batch of Gmail messages
    """
    try:
        print(f"🚀 Starting Gmail batch {batch_count} fetch for user {user_id}")
        gmail_service = GmailService(user_id)
        # user_email = gmail_service.user_email

        # Fetch one batch of messages
        results = await gmail_service.process_threads_batch(
            threads, my_email, batch_count
        )
        print(
            f"GOT the data from results {batch_count} in v2 fetch gmail", len(results)
        )
        all_messages = []
        for idx, thread in enumerate(threads):
            thread_id = thread["id"]
            res = results.get(thread_id)

            if not res:
                print(f"⚠️ No response for thread {thread_id}")
                continue

            thread_data, err = res  # ✅ unpack tuple

            if err:
                print(f"⚠️ Thread {thread_id} error: {err}")
                continue

            if thread_data:
                all_messages.extend(thread_data)

        if not all_messages:
            print("📭 No messages found in this batch")
            return {"status": "success", "new_messages": 0, "next_page_token": None}

        count_new = 0
        grouped_messages = defaultdict(list)
        # cursor = connection.cursor()

        # File setup
        timestamp = datetime.now(timezone.utc)
        date_str = timestamp.strftime("%Y-%m-%d")
        filename = f"{date_str}.json"

        user_folder = os.path.join(pathconfig.basepath, "messages", user_id)
        ensure_dir(user_folder)
        filepath = os.path.join(user_folder, filename)

        # Load existing data
        existing_ids_local = set()
        input_data_local = {}

        if os.path.exists(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    existing_data_local = json.load(f)
                    input_data_local = existing_data_local.get("input_data", {})

                # Extract existing message IDs
                for client_data in input_data_local.values():
                    if isinstance(client_data, dict):
                        for client_channels in client_data.values():
                            if isinstance(client_channels, dict):
                                for channel_msgs in client_channels.values():
                                    if isinstance(channel_msgs, list):
                                        for msg in channel_msgs:
                                            if isinstance(msg, dict):
                                                msg_id = msg.get("id")
                                                if msg_id:
                                                    existing_ids_local.add(msg_id)
            except Exception as e:
                print(f"⚠️ Error loading existing data: {e}")

        # Process messages (your existing logic)
        email_to_client_id = {}
        configs_created = set()

        for msg in all_messages:
            message_id = msg["messageId"]

            # Skip if already exists in database
            cursor.execute(
                "SELECT 1 FROM messages WHERE message_id = %s", (message_id,)
            )
            if cursor.fetchone():
                continue

            # Skip if already exists locally
            if message_id in existing_ids_local:
                continue

            # Your existing message processing logic...
            thread_id = msg["thread_id"]
            dt = parsedate_to_datetime(msg["date"])
            timestamp_iso = dt.isoformat()
            direction = "inbound" if msg["from"] != my_email else "outbound"
            subject = msg["subject"]
            body_content = msg.get("body", "")
            plain_text = (
                BeautifulSoup(body_content, "html.parser")
                .get_text(separator="\n")
                .strip()
            )
            extracted_body = extract_reply_content(plain_text)

            from_name, from_email = parseaddr(msg["from"])
            to_name, to_email = parseaddr(msg.get("to", ""))

            if direction == "inbound":
                participant = from_email
                participant_name = from_name
            else:
                participant = to_email
                participant_name = to_name

            # Get or create client
            if participant in email_to_client_id:
                client_id, client_type = email_to_client_id[participant]
            else:
                result = get_users_client_id(participant, user_id, cursor)

                if isinstance(result, tuple) and len(result) == 2:
                    client_id, client_type = result
                else:
                    # assume function returned only client_id
                    client_id = result if result else None
                    client_type = None

                if not client_id:
                    client_id = add_lead_contact(
                        user_id, cursor, participant, participant_name
                    )
                    client_type = "Lead"

                email_to_client_id[participant] = (client_id, client_type)

            # Create message object
            message = {
                "id": message_id,
                "from": from_email,
                "to": my_email,
                "body": extracted_body,
                "subject": subject,
                "timestamp": timestamp_iso,
                "source": "gmail",
                "direction": direction,
                "user_id": user_id,
                "thread_id": thread_id,
                "conversation_id": from_email if direction == "inbound" else my_email,
                "type": client_type,
            }

            grouped_messages.setdefault(client_id, {}).setdefault("gmail", []).append(
                message
            )
            count_new += 1

            if client_id not in configs_created:

                # Create config files if needed (your existing logic)
                config_folder = os.path.join(
                    pathconfig.basepath, "messages", user_id, client_id
                )
                ensure_dir(config_folder)
                config_filepath = os.path.join(config_folder, "config.json")
                #  {
                #                 "conv_id": "",
                #                 "ticket_id": "",
                #                 "ticket_name": "",
                #                 "subject": "",
                #                 "channel": "",
                #                 "updated_date": "",
                #                 "parsed_timestamp": "",
                #                 "thread_id": "",
                #             }

                if not os.path.exists(config_filepath):
                    dummy_config = {
                        "userclients_id": client_id,
                        "conversations": [],
                    }
                    with open(config_filepath, "w", encoding="utf-8") as f:
                        json.dump(dummy_config, f, indent=2)

                    s3_config_key = f"{user_id}/messages/{client_id}/config.json"
                    s3_data = read_json_from_s3(s3_config_key)
                    if s3_data is None:

                        upload_any_file(
                            config_filepath,
                            user_id,
                            type="messages",
                            s3_key_C=s3_config_key,
                        )
                        print(f"uploaded config for client_id: {client_id}")

                configs_created.add(client_id)

        # Merge with existing data and save
        existing_data = safe_json_load(filepath)

        merged_messages = existing_data.get("input_data", {})

        for client_id, channels in grouped_messages.items():
            for channel, messages in channels.items():
                merged_messages.setdefault(client_id, {}).setdefault(
                    "gmail", []
                ).extend(messages)

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(
                {"filename": filename, "input_data": merged_messages}, f, indent=2
            )

        # cursor.close()
        # connection.close()

        print(f"✅ Batch complete: {count_new} new messages processed")
        return {
            "status": "success",
            "new_messages": count_new,
            "next_page_token": None,
            "grouped_messages": dict(grouped_messages),  # Return current batch data
        }

    except Exception as e:
        print(f"[ERROR] → v2fetch_gmail_messages_batch failed: {e}")
        return {
            "error": str(e),
            "status": "failed",
            "next_page_token": None,
            "grouped_messages": {},
        }


# @gmail_bp.route("/gmail/sync_gmail_contacts/<user_id>")
def sync_gmail_contacts(user_id):
    print(f"🚀 Starting sync_gmail_contacts for user_id: {user_id}")

    try:
        # Initialize Gmail service
        print("📧 Initializing Gmail service...")
        gmail_service = GmailService(user_id)

        # Get contacts
        print("🔍 Fetching contacts from Gmail...")
        messages = gmail_service.get_contacts()
        print(f"📬 Retrieved {len(messages)} contact entries")

        if not messages:
            print("⚠️ No messages retrieved from Gmail")
            response = jsonify(
                {
                    "success": True,
                    "message": "No contacts found",
                    "results": [],
                    "count": 0,
                }
            )
            print(f"📤 Returning response: {response.get_json()}")
            return response

        # Database connection
        print("🗄️ Connecting to database...")
        connection = connect_to_rds()
        if connection is None:
            print("❌ Database connection failed")
            error_response = jsonify(
                {"success": False, "error": "Database connection failed", "results": []}
            )
            print(f"📤 Returning error response: {error_response.get_json()}")
            return error_response, 500

        print("✅ Database connection successful")
        cursor = connection.cursor()
        results = []
        processed_count = 0
        skipped_count = 0

        for i, item in enumerate(messages):
            try:
                print(f"🔄 Processing item {i+1}/{len(messages)}: {item}")
                processed_count += 1

                # Decode Unicode escape sequences if needed
                if "\\u003C" in item or "\\u003E" in item:
                    decoded_item = item.encode().decode("unicode-escape")
                    print(f"🔤 Decoded: {decoded_item}")
                else:
                    decoded_item = item

                # Parse email and name
                match = re.search(r"<([^<>]+)>", decoded_item)
                if match:
                    email = match.group(1).strip()
                    name_part = decoded_item.split("<")[0].strip()
                    print(f"📧 Found email in brackets: {email}, name: '{name_part}'")
                else:
                    email = decoded_item.strip()
                    name_part = ""
                    print(f"📧 Plain email format: {email}")

                # Validate email
                if not email or "@" not in email:
                    print(f"❌ Invalid email format: {email}")
                    skipped_count += 1
                    continue

                # Check if email exists in database
                print(f"🔍 Checking if email exists in database: {email}")
                cursor.execute(
                    "SELECT 1 FROM users_clients WHERE email_id = %s", (email,)
                )
                existing = cursor.fetchone()

                if existing:
                    print(f"⏭️ Email already exists, skipping: {email}")
                    skipped_count += 1
                    continue

                # Process name
                if name_part:
                    name_part = name_part.strip('"').strip("'").strip()
                    name_tokens = name_part.split()
                    first_name = name_tokens[0] if name_tokens else ""
                    last_name = (
                        " ".join(name_tokens[1:]) if len(name_tokens) > 1 else ""
                    )
                    print(
                        f"👤 Parsed name - First: '{first_name}', Last: '{last_name}'"
                    )
                else:
                    # Extract name from email
                    email_prefix = email.split("@")[0]
                    email_name = re.sub(r"[._-]", " ", email_prefix)
                    name_tokens = email_name.split()
                    first_name = name_tokens[0].title() if name_tokens else ""
                    last_name = (
                        " ".join(token.title() for token in name_tokens[1:])
                        if len(name_tokens) > 1
                        else ""
                    )
                    print(
                        f"👤 Generated name from email - First: '{first_name}', Last: '{last_name}'"
                    )

                contact_data = {
                    "email": email,
                    "first_name": first_name,
                    "last_name": last_name,
                }
                results.append(contact_data)
                print(f"✅ Added contact: {contact_data}")

                # Uncomment when ready to save
                # users_clients_id = add_synced_contact(user_id, cursor, email, first_name, last_name)

            except Exception as item_error:
                print(f"❌ Error processing item '{item}': {item_error}")
                print(f"📋 Traceback: {traceback.format_exc()}")
                skipped_count += 1
                continue

        # Close database connection
        print("🔒 Closing database connection...")
        cursor.close()
        connection.close()

        # Prepare final response
        final_response = {
            "success": True,
            "message": f"Successfully processed {len(results)} new contacts",
            "results": results,
            "stats": {
                "total_processed": processed_count,
                "new_contacts": len(results),
                "skipped": skipped_count,
                "total_retrieved": len(messages),
            },
        }

        print(f"🎉 Final response prepared: {final_response}")
        print(
            f"📊 Stats - Total: {len(messages)}, New: {len(results)}, Skipped: {skipped_count}"
        )

        response = jsonify(final_response)
        print(f"📤 Returning JSON response with status 200")
        return response

    except Exception as e:
        error_msg = f"Unexpected error in sync_gmail_contacts: {str(e)}"
        print(f"💥 {error_msg}")
        print(f"📋 Full traceback: {traceback.format_exc()}")

        error_response = {
            "success": False,
            "error": error_msg,
            "results": [],
            "traceback": traceback.format_exc() if current_app.debug else None,
        }

        print(f"📤 Returning error response: {error_response}")
        return jsonify(error_response), 500


def gmail_reply(user_id, to, subject, thread_id, body_text, in_reply_to):

    gmail_service = GmailService(user_id)
    user_email = gmail_service.user_email

    # Defensive checks
    if not to:
        raise ValueError("Recipient email 'to' is required")
    if not subject:
        raise ValueError("Subject is required")
    if not thread_id:
        raise ValueError("Thread ID is required")

    # Fetch message_id to use as in_reply_to (could be fetched externally)
    # fallback or replace with actual msg_id
    print(f"in_reply_to : {in_reply_to}")
    print(f"subjec : {subject}")
    sent = gmail_service.send_reply(
        conversation_id=None,  # optional now, can be excluded
        to=to,
        subject=subject,
        thread_id=thread_id,
        in_reply_to=in_reply_to,
        body_text=body_text,
        user_id=user_id,
    )

    message_api_id = sent["id"]

    message_id = get_message_id(gmail_service.service, user_id, message_api_id)

    return message_id


def get_message_id(service, user_id, gmail_id):
    msg = (
        service.users()
        .messages()
        .get(userId=user_id, id=gmail_id, format="metadata")
        .execute()
    )
    headers = msg.get("payload", {}).get("headers", [])
    return next(
        (h["value"] for h in headers if h["name"].lower() == "message-id"), None
    )


def send_mail(user_id, to, subject, body_text):
    message_id = None
    thread_id = None

    try:
        print(f"User ID from session: {user_id}")
        gmail_service = GmailService(user_id)
        user_email = gmail_service.user_email

        sent = gmail_service.send_email(to=to, subject=subject, body_text=body_text)

        if not sent or "id" not in sent:
            raise ValueError("No message ID returned from Gmail API")

        message_id = sent["id"]
        thread_id = sent.get("thread_id")

        return {"status": "success", "message_id": message_id, "thread_id": thread_id}

    except Exception as e:
        print(f"[ERROR] → send_mail failed: {e}")
        return {"error": str(e), "status": "failed"}


def add_lead_contact(user_id, cursor, participant, participant_name):

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
                        updated_in


                    )
                    VALUES (%s, %s, %s, %s, NULL, NULL, %s, NULL, NULL, NULL, NULL,%s,%s,%s)
                """
    cursor.execute(
        insert_sql,
        (
            users_clients_id,
            communication_id,
            participant_name,
            "( Lead )",
            participant,
            "Lead",
            created_date,
            updated_date,
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


@gmail_bp.route("/gmail/drafts", methods=["GET"])
def list_drafts():
    try:
        user_id = session.get("user_id")
        gmail_service = GmailService(user_id)
        drafts = gmail_service.get_drafts()
        return jsonify({"drafts": drafts})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@gmail_bp.route("/gmail/threads", methods=["GET"])
def list_threads():
    try:
        user_id = session.get("user_id")
        print("userID", user_id)
        gmail_service = GmailService(user_id)
        threads = gmail_service.get_inbox()
        return jsonify({"threads": threads})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@gmail_bp.route("/gmail/spam", methods=["GET"])
def list_spam():
    try:
        user_id = session.get("user_id")
        gmail_service = GmailService(user_id)
        threads = gmail_service.get_spam()
        return jsonify({"threads": threads})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@gmail_bp.route("/gmail/trash", methods=["GET"])
def list_trash():
    try:
        user_id = session.get("user_id")
        gmail_service = GmailService(user_id)
        threads = gmail_service.get_trash()
        return jsonify({"threads": threads})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@gmail_bp.route("/gmail/drafts/<draft_id>", methods=["PUT"])
def update_draft(draft_id):
    try:
        user_id = session.get("user_id")
        data = request.json
        to = data.get("to", "")
        subject = data.get("subject", "")
        body = data.get("body", "")
        if not to or not body:
            return jsonify({"error": "Missing required fields: 'to' and 'body'"}), 400

        gmail_service = GmailService(user_id)
        updated_draft = gmail_service.update_draft(draft_id, to, subject, body)
        return jsonify({"draft": updated_draft})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@gmail_bp.route("/gmail/create_draft", methods=["POST"])
def create_draft():
    data = request.json
    to = data.get("to")
    subject = data.get("subject")
    body = data.get("body")

    if not to or not subject or not body:
        return jsonify({"error": "Missing to, subject, or body"}), 400

    try:
        user_id = session.get("user_id")
        gmail_service = GmailService(user_id)
        result = gmail_service.create_draft(to, subject, body)
        return jsonify({"message": "Draft created", "id": result.get("id")}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@gmail_bp.route("/gmail/respond", methods=["POST"])
def respond_to_email():
    try:
        user_id = session.get("user_id")
        gmail_service = GmailService(user_id)
        data = request.get_json()
        to = data.get("to")
        subject = data.get("subject")
        message_text = data.get("message")
        if not all([to, subject, message_text]):
            return jsonify({"error": "Missing 'to', 'subject', or 'message'"}), 400

        label_id = gmail_service.create_label("AI Messages")
        result = gmail_service.send_message(
            to, subject, message_text, label_ids=[label_id]
        )
        return jsonify({"result": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@gmail_bp.route("/gmail/inbox_info/<userid>", methods=["GET"])
def get_inbox_info(userid):
    try:
        max_emails = 1000
        base_days = int(request.args.get("days", 30))  # default 30 days
        gmail_service = GmailService(userid)

        total_messages = 0
        final_days_used = base_days

        while True:
            inbox_count = gmail_service.get_inbox_stats(base_days)  # returns int
            total_messages = inbox_count
            final_days_used = base_days

            if total_messages >= max_emails:
                # if total_messages > (max_emails + 30):
                #     base_days -= 2
                # else:
                #     break
                break
            else:
                base_days += 10  # widen search window

        return (
            jsonify(
                {
                    "result": {
                        "total_messages": total_messages,
                        "days_covered": final_days_used,
                    }
                }
            ),
            200,
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


from flask import request, jsonify
from datetime import datetime, timezone
from dateutil.relativedelta import relativedelta


@gmail_bp.route("/gmail/datewise/<userid>", methods=["GET"])
def get_datewise_info(userid):
    try:
        Enddate_str = request.args.get(
            "end_date", datetime.now(timezone.utc).strftime("%Y-%m-%d")
        )
        Enddate = datetime.fromisoformat(Enddate_str)

        startDate_str = request.args.get(
            "start_date", (Enddate - relativedelta(months=3)).strftime("%Y-%m-%d")
        )

        gmail_service = GmailService(userid)

        inbox_count = asyncio.run(
            gmail_service.get_inbox_date_wise_stats_dynamic(
                start_date=startDate_str, end_date=Enddate_str
            )
        )

        return jsonify({"result": inbox_count}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# @gmail_bp.route("/deletedb/<user_id>", methods=["GET"])
# def delete_user_ticket_data(user_id):
#     try:
#         connection = connect_to_rds()
#         with connection.cursor() as cursor:
#             # 1. Get ticket IDs assigned to the user
#             cursor.execute(
#                 """
#                 SELECT ticket_id_fk FROM assigned WHERE user_id_fk = %s
#                 """,
#                 (user_id,),
#             )
#             ticket_ids = [row[0] for row in cursor.fetchall() if row[0]]

#             if not ticket_ids:
#                 return {"status": "success", "message": "No tickets found for user"}

#             # 2. Get conversation IDs from those tickets
#             format_strings = ",".join(["%s"] * len(ticket_ids))
#             cursor.execute(
#                 f"""
#                 SELECT conversation_id_fk FROM tickets
#                 WHERE tickets_id IN ({format_strings})
#                 """,
#                 tuple(ticket_ids),
#             )
#             conversation_ids = [row[0] for row in cursor.fetchall() if row[0]]

#             # 3. Delete messages first (based on conversation IDs)
#             if conversation_ids:
#                 format_strings = ",".join(["%s"] * len(conversation_ids))
#                 cursor.execute(
#                     f"""
#                     DELETE FROM messages
#                     WHERE conversation_id_fk IN ({format_strings})
#                     """,
#                     tuple(conversation_ids),
#                 )

#             # 4. Delete from assigned (before tickets)
#             format_strings = ",".join(["%s"] * len(ticket_ids))
#             cursor.execute(
#                 f"""
#                 DELETE FROM assigned
#                 WHERE ticket_id_fk IN ({format_strings})
#                 """,
#                 tuple(ticket_ids),
#             )

#             # 5. Delete tickets (now it's safe)
#             format_strings = ",".join(["%s"] * len(ticket_ids))
#             cursor.execute(
#                 f"""
#                 DELETE FROM tickets
#                 WHERE tickets_id IN ({format_strings})
#                 """,
#                 tuple(ticket_ids),
#             )

#             # 6. Delete threads (based on conversation IDs)
#             if conversation_ids:
#                 format_strings = ",".join(["%s"] * len(conversation_ids))
#                 cursor.execute(
#                     f"""
#                     DELETE FROM threads
#                     WHERE conversation_id IN ({format_strings})
#                     """,
#                     tuple(conversation_ids),
#                 )

#             connection.commit()
#             return {
#                 "status": "success",
#                 "message": "User-related ticket data deleted successfully",
#             }

#     except Exception as e:
#         connection.rollback()
#         return {"status": "failed", "error": str(e)}

from threading import Thread


@gmail_bp.route("/deletedb/<user_id>", methods=["GET"])
def delete_user_ticket_data(user_id):
    try:
        connection = connect_to_rds()
        with connection.cursor() as cursor:
            # 1. Get ticket IDs assigned to the user
            cursor.execute(
                """
                SELECT ticket_id_fk FROM assigned WHERE user_id_fk = %s
                """,
                (user_id,),
            )
            ticket_ids = [row[0] for row in cursor.fetchall() if row[0]]

            if not ticket_ids:
                # Still check for communication + users_clients
                cursor.execute(
                    "SELECT communication_id FROM communication WHERE user_id_fk = %s",
                    (user_id,),
                )
                comm_ids = [row[0] for row in cursor.fetchall() if row[0]]

                if comm_ids:
                    format_strings = ",".join(["%s"] * len(comm_ids))
                    cursor.execute(
                        f"""
                        DELETE FROM users_clients
                        WHERE communication_id_fk IN ({format_strings})
                        """,
                        tuple(comm_ids),
                    )
                    cursor.execute(
                        f"""
                        DELETE FROM communication
                        WHERE communication_id IN ({format_strings})
                        """,
                        tuple(comm_ids),
                    )
                    connection.commit()

                return {
                    "status": "success",
                    "message": "No tickets found, cleaned communication/users_clients",
                }

            # 2. Get conversation IDs from those tickets
            format_strings = ",".join(["%s"] * len(ticket_ids))
            cursor.execute(
                f"""
                SELECT conversation_id_fk FROM tickets
                WHERE tickets_id IN ({format_strings})
                """,
                tuple(ticket_ids),
            )
            conversation_ids = [row[0] for row in cursor.fetchall() if row[0]]

            # 3. Delete messages (based on conversation IDs)
            if conversation_ids:
                format_strings = ",".join(["%s"] * len(conversation_ids))
                cursor.execute(
                    f"""
                    DELETE FROM messages
                    WHERE conversation_id_fk IN ({format_strings})
                    """,
                    tuple(conversation_ids),
                )

            # 4. Delete from assigned
            format_strings = ",".join(["%s"] * len(ticket_ids))
            cursor.execute(
                f"""
                DELETE FROM assigned
                WHERE ticket_id_fk IN ({format_strings})
                """,
                tuple(ticket_ids),
            )

            # 5. Delete tickets
            format_strings = ",".join(["%s"] * len(ticket_ids))
            cursor.execute(
                f"""
                DELETE FROM tickets
                WHERE tickets_id IN ({format_strings})
                """,
                tuple(ticket_ids),
            )

            # 6. Delete threads (based on conversation IDs)
            if conversation_ids:
                format_strings = ",".join(["%s"] * len(conversation_ids))
                cursor.execute(
                    f"""
                    DELETE FROM threads
                    WHERE conversation_id IN ({format_strings})
                    """,
                    tuple(conversation_ids),
                )

            # 7. Delete communication & users_clients (based on user_id)
            cursor.execute(
                "SELECT communication_id FROM communication WHERE user_id_fk = %s",
                (user_id,),
            )
            comm_ids = [row[0] for row in cursor.fetchall() if row[0]]

            if comm_ids:
                format_strings = ",".join(["%s"] * len(comm_ids))
                cursor.execute(
                    f"""
                    DELETE FROM users_clients
                    WHERE communication_id_fk IN ({format_strings})
                    """,
                    tuple(comm_ids),
                )
                cursor.execute(
                    f"""
                    DELETE FROM communication
                    WHERE communication_id IN ({format_strings})
                    """,
                    tuple(comm_ids),
                )
                cursor.execute(
                    """
                    UPDATE users SET umail_json = NULL WHERE user_id = %s
                    """,
                    (user_id,),  # <-- add comma to make it a tuple
                )
                connection.commit()
            folder_path = f"{user_id}/messages"
            Thread(target=delete_folder_from_s3, args=(folder_path,)).start()

            return {
                "status": "success",
                "message": "User-related ticket, communication, and users_clients data deleted successfully",
            }

    except Exception as e:
        connection.rollback()
        return {"status": "failed", "error": str(e)}
    finally:
        if connection:
            connection.close()
