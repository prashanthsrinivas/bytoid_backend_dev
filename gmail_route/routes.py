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
from utils.s3_utils import upload_any_file, read_json_from_s3
from umail_helper.helper import find_contact_by_identity, ensure_contact_loaded
from create_db import connect_to_rds
from umail_helper.helper import get_users_client_id
from collections import defaultdict





gmail_bp = Blueprint("gmail", __name__)


@gmail_bp.route("/gmail/fetch")
def fetch_gmail_messages(user_id):

    try:
        # user_id = request.args.get("user_id") or session.get("user_id")
        print(f"User ID from session: {user_id}")
        gmail_service = GmailService(user_id)
        user_email = gmail_service.user_email

        print(f"my email address is: {user_email}")
        # Fetch the latest threads (e.g., 50)
        threads = gmail_service.get_inbox()
        # print(f"Fetched threads: {threads}")

        count_new = 0

        grouped_messages = defaultdict(list)
        connection = connect_to_rds()
        if connection is None:
            return None

        cursor = connection.cursor()

        timestamp = datetime.now(timezone.utc)
        date_str = timestamp.strftime("%Y-%m-%d")
        filename = f"{date_str}.json"
        s3_key = f"{user_id}/messages/{filename}"

        # Try to load existing messages
        try:
            existing = read_json_from_s3(filepath=s3_key)
            input_data = existing.get("input_data", {})
            # print(f" ***** got input data: {input_data}")
        except Exception as e:
            print(f"⚠️ S3 file missing or unreadable: {e}")
            input_data = {}

        # Flatten all existing message IDs for deduplication
        existing_ids = set()
        for client_channels in input_data.values():
            for channel_msgs in client_channels.values():
                for msg in channel_msgs:
                    existing_ids.add(msg.get("id"))  # or "message_id" depending on structure

        count_new = 0
        for msg in threads:
            message_id = msg["messageId"]
            if message_id in existing_ids:
                print(f"⏭️ Message {message_id} already exists. Skipping.")
                continue


            dt = parsedate_to_datetime(msg["date"])
            timestamp_iso = dt.isoformat()
            direction = (
                "inbound" if msg["from"] != gmail_service.user_email else "outbound"
            )

            body_content = msg.get("body", "")
            plain_text = BeautifulSoup(body_content, "html.parser").get_text().strip()

            name, from_email = parseaddr(msg["from"])
            name, to_email = parseaddr(msg.get("to", ""))
            participant = from_email if direction == "inbound" else to_email

            client_id = get_users_client_id(participant, cursor)
            # print(f"*****client id is: {client_id}")
            if client_id:
                message = {
                    "id": message_id,
                    "from": from_email,
                    "to": user_email,
                    "body": plain_text,
                    "subject": msg["subject"],
                    "timestamp": timestamp_iso,
                    "status": "received",
                    "source": "gmail",
                    "direction": direction,
                    "user_id": user_id,
                    "thread_id": msg["id"],
                    "message_id": message_id,
                    "conversation_id": (
                        from_email if direction == "inbound" else user_email
                    ),
                }

                grouped_messages.setdefault(client_id, {}).setdefault(
                    "gmail", []
                ).append(message)

                count_new += 1

                print(
                    f"******[DEBUG] user_id={user_id} ({type(user_id)}), client_id={client_id} ({type(client_id)}), basepath={pathconfig.basepath}"
                )

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
                                "subject": "",
                                "channel": "",
                                "updated_date": "",
                            }
                        ],
                    }

                    with open(config_filepath, "w", encoding="utf-8") as f:
                        json.dump(dummy_config, f, indent=2)

                    print(f"*************✅ Config file created at {config_filepath} in gmail")
                else:
                    print(f"📁 Config file already exists: {config_filepath}")

        # Write file locally
        user_folder = os.path.join(pathconfig.basepath, "messages", user_id)
        ensure_dir(user_folder)
        filepath = os.path.join(user_folder, filename)

        existing_data = {}
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                existing_data = json.load(f)

        merged_messages = existing_data.get("input_data", {})

        # Add current Gmail messages to merged structure
        for client_id, channels in grouped_messages.items():
            for channel, messages in channels.items():
                merged_messages.setdefault(client_id, {}).setdefault("gmail", []).extend(messages)

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump({"filename": filename, "input_data": merged_messages}, f, indent=2)


        # with open(filepath, "w", encoding="utf-8") as f:
        #     json.dump(
        #         {"filename": filename, "input_data": grouped_messages}, f, indent=2
        #     )
        print("*********saved the messags json file locally for gmail")

        # # Upload to S3
        upload_any_file(
            file_path=filepath, user_id=user_id, type="messages", file_name=filename
        )
        print(" json uploaded for gmail")
        # return jsonify({"status": "ok", "new_messages": count_new})
        return {
        "status": "success",
        "new_messages": count_new
    }

    except Exception as e:
        print(f"[ERROR] → fetch_mail failed: {e}")
        return {"error": str(e), "status": "failed"}


# @gmail_bp.route("/gmail/reply", methods=["POST"])
def gmail_reply(
    user_id, conversation_id, to, subject, thread_id, in_reply_to, body_text
):

    print(f"User ID : {user_id}")
    gmail_service = GmailService(user_id)
    user_email = gmail_service.user_email

    print(f"my email address is: {user_email}")

    if not to:
        raise ValueError("Recipient email 'to' is required")
    if not subject:
        raise ValueError("Subject is required")
    if not thread_id:
        raise ValueError("Thread ID is required")
    if not in_reply_to:
        raise ValueError("In-Reply-To message ID is required")

    message = EmailMessage()
    message["To"] = to
    message["Subject"] = (
        f"Re: {subject}" if not subject.lower().startswith("re:") else subject
    )
    message["In-Reply-To"] = in_reply_to
    message["References"] = in_reply_to
    message.set_content(body_text)

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    message_body = {"raw": raw, "threadId": thread_id}

    sent = gmail_service.send_reply(
        conversation_id=conversation_id,
        to=to,
        subject=subject,
        thread_id=thread_id,
        in_reply_to=in_reply_to,
        body_text=body_text,
        user_id=user_id,
    )

    timestamp_ = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    message_id = sent["id"]

    timestamp = datetime.now(timezone.utc)
    date_str = timestamp.strftime("%Y-%m-%d")
    filename = f"{date_str}.json"
    s3_key = f"{user_id}/messages/{filename}"

    # Try to load existing messages
    try:
        existing = read_json_from_s3(filepath=s3_key)
        input_data = existing.get("input_data", {})
    except Exception as e:
        print(f"⚠️ S3 file missing or unreadable: {e}")
        input_data = {}

    input_data[message_id] = {
        "id": message_id,
        "from": user_email,
        "to": to,
        "body": body_text,
        "subject": subject,
        "timestamp": timestamp_,
        "status": "sent",
        "source": "gmail",
        "direction": "outbound",
        "user_id": user_id,
        "thread_id": thread_id,
        "message_id": message_id,
        "conversation_id": conversation_id,
    }
    print("✅ Saved sent message to MESSAGES:")
    # print(MESSAGES[message_id])

    user_folder = os.path.join(pathconfig.basepath, "messages", user_id)
    ensure_dir(user_folder)
    filepath = os.path.join(user_folder, filename)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump({"filename": filename, "input_data": input_data}, f, indent=2)

    # Upload to S3
    upload_any_file(
        file_path=filepath, user_id=user_id, type="messages", file_name=filename
    )

    return sent


# @gmail_bp.route("/gmail/send_mail", methods=["POST"])
def send_mail(user_id, conversation_id, to, subject, body_text):

    try:
        print(f"to inside send_mail is : {to}")
        print(f"User ID from session: {user_id}")
        gmail_service = GmailService(user_id)
        user_email = gmail_service.user_email

        print(f"my email address is: {user_email}")

        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body_text)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        message_body = {"raw": raw}
        sent = gmail_service.send_email(
            conversation_id=conversation_id, to=to, subject=subject, body_text=body_text
        )

        timestamp_ = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        message_id = sent["id"]
        thread_id = sent.get("threadId")

        timestamp = datetime.now(timezone.utc)
        date_str = timestamp.strftime("%Y-%m-%d")
        filename = f"{date_str}.json"
        s3_key = f"{user_id}/messages/{filename}"

        # Try to load existing messages
        try:
            existing = read_json_from_s3(filepath=s3_key)
            input_data = existing.get("input_data", {})
        except Exception as e:
            print(f"⚠️ S3 file missing or unreadable: {e}")
            input_data = {}

        input_data[message_id] = {
            "id": message_id,
            "from": user_email,
            "to": to,
            "body": body_text,
            "subject": subject,
            "timestamp": timestamp_,
            "status": "sent",
            "source": "gmail",
            "direction": "outbound",
            "user_id": "user_id",
            "message_id": message_id,
            "conversation_id": conversation_id,
            "thread_id": thread_id,
        }
        print("✅ Saved sent message to MESSAGES:")

        user_folder = os.path.join(pathconfig.basepath, "messages", user_id)
        ensure_dir(user_folder)
        filepath = os.path.join(user_folder, filename)

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump({"filename": filename, "input_data": input_data}, f, indent=2)

        # Upload to S3
        upload_any_file(
            file_path=filepath, user_id=user_id, type="messages", file_name=filename
        )

        return sent

    except Exception as e:
        print(f"[ERROR] → send_mail failed: {e}")
        return {"error": str(e), "status": "failed"}


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
        print(dict(session))
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
