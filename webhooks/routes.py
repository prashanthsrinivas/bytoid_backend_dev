from flask import Flask, request, jsonify, Blueprint, Response, session
from twilio.rest import Client
import uuid
import os
import sys
from dotenv import load_dotenv
from twilio.twiml.voice_response import VoiceResponse
from slack_sdk import WebClient
from slack_sdk.signature import SignatureVerifier
from collections import defaultdict
from datetime import datetime, timezone
import humanize
from data import MESSAGES  # delete this later, this is just for testing
from gmail_route.gmail_service import GmailService
from microsoft_route.routes import send_outlook_email
from zoho_routes.routes import send_zoho_email
import requests
import traceback
from email.utils import parseaddr
from gmail_route.routes import gmail_reply, send_mail

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from create_db import connect_to_rds
from typing import Dict, Any
from dateutil.parser import isoparse
from utils.normal import ensure_dir
from utils.s3_utils import upload_any_file, read_json_from_s3, list_all_files
from cust_helpers import pathconfig
from utils.normal import ensure_dir, load_yaml_file
from utils.fireworkzz import get_fireworks_response
import yaml
import re
from dateutil.parser import parse as parse_date


from gmail_route.routes import fetch_gmail_messages
from zoho_routes.routes import fetch_zoho_emails

# import boto3
import json
from umail_helper.helper import find_contact_by_identity, ensure_contact_loaded
from umail_helper.helper import CONTACTS
from umail_helper.helper import IDENTITY_MAP


twilio_bp = Blueprint("twilio_webhook", __name__)

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")

slack_token = os.environ.get("SLACK_TOKEN")
slack_client = WebClient(token=slack_token)
# auth_info = slack_client.auth_test()
# SELF_USER_ID = auth_info["user_id"]
secret = os.environ.get("SIGNING_SECRET")
VERIFIER = SignatureVerifier(signing_secret=secret)

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

VERIFY_TOKEN = "bytoidtoken"


all_messages = {}

def build_grouped(user_id):
    from collections import defaultdict
    from email.utils import parseaddr
    from datetime import datetime, timezone

    grouped = defaultdict(dict)
    now = datetime.now(timezone.utc)
    prefix = f"{user_id}/messages/"
    file_list = list_all_files(prefix)

    print(f"\n[INFO] Found {len(file_list)} files under prefix: {prefix}\n")

    for file_obj in file_list:
        key = file_obj["Key"]
        print(f"[PROCESSING FILE] {key}")

        raw_data = read_json_from_s3(key)
        input_data = raw_data.get("input_data")

        if not isinstance(input_data, list):
            print("[WARN] Skipping file — input_data should be a list")
            continue

        for msg in input_data:
            source = msg.get("source")
            direction = msg.get("direction")
            participant = None

            # Resolve participant based on source
            if source == "slack":
                if msg.get("to", "").startswith(("C", "G")):
                    participant = msg["to"]
            elif source == "outlook":
                participant = msg.get("from_email") if direction == "inbound" else msg.get("to_email")
            elif source == "gmail":
                address_field = msg.get("from") if direction == "inbound" else msg.get("to")
                participant = parseaddr(address_field)[1] if address_field else None
            elif source == "whatsapp":
                number = msg.get("from") if direction == "inbound" else msg.get("to")
                participant = number.replace("whatsapp:", "") if number else None
            else:
                participant = msg.get("from") if direction == "inbound" else msg.get("to")

            print(f"  [PARTICIPANT] {participant or '—'}")

            if not participant:
                print("  [SKIP] No valid participant")
                continue

            contact = find_contact_by_identity(user_id, participant, direction=direction)
            if not contact or contact["id"] == user_id:
                print(f"  [SKIP] Unknown or self contact: {participant}")
                continue

            contact_id = contact["id"]
            contact_name = contact["name"]
            channels = contact.get("channels", {})
            print(f"  [MATCH] {participant} → {contact_name} ({contact_id})")

            CONTACTS.setdefault(user_id, {}).setdefault(contact_id, {
                "name": contact_name,
                "channels": channels
            })

            if msg["id"] not in grouped[contact_id]:
                msg["contact_id"] = contact_id
                msg["contact_name"] = contact_name
                grouped[contact_id][msg["id"]] = msg
                print(f"  [GROUPED] msg_id={msg['id']} added under {contact_id}")

    print(f"\n[SUMMARY] Grouped messages for {len(grouped)} contact(s)\n")
    return grouped


def build_grouped(user_id):
    from collections import defaultdict
    from email.utils import parseaddr
    from datetime import datetime, timezone

    grouped = defaultdict(dict)
    now = datetime.now(timezone.utc)
    prefix = f"{user_id}/messages/"
    file_list = list_all_files(prefix)

    print(f"\n[INFO] Found {len(file_list)} files under prefix: {prefix}\n")

    for file_obj in file_list:
        key = file_obj["Key"]
        print(f"[PROCESSING FILE] {key}")

        raw_data = read_json_from_s3(key)
        input_data = raw_data.get("input_data")

        if not isinstance(input_data, list):
            print("[WARN] Skipping file — input_data should be a list")
            continue

        for msg in input_data:
            source = msg.get("source")
            direction = msg.get("direction")
            participant = None

            # Resolve participant based on source
            if source == "slack":
                if msg.get("to", "").startswith(("C", "G")):
                    participant = msg["to"]
            elif source == "outlook":
                participant = msg.get("from_email") if direction == "inbound" else msg.get("to_email")
            elif source == "gmail":
                address_field = msg.get("from") if direction == "inbound" else msg.get("to")
                participant = parseaddr(address_field)[1] if address_field else None
            elif source == "whatsapp":
                number = msg.get("from") if direction == "inbound" else msg.get("to")
                participant = number.replace("whatsapp:", "") if number else None
            else:
                participant = msg.get("from") if direction == "inbound" else msg.get("to")

            print(f"  [PARTICIPANT] {participant or '—'}")

            if not participant:
                print("  [SKIP] No valid participant")
                continue

            contact = find_contact_by_identity(user_id, participant, direction=direction)
            if not contact or contact["id"] == user_id:
                print(f"  [SKIP] Unknown or self contact: {participant}")
                continue

            contact_id = contact["id"]
            contact_name = contact["name"]
            channels = contact.get("channels", {})
            print(f"  [MATCH] {participant} → {contact_name} ({contact_id})")

            CONTACTS.setdefault(user_id, {}).setdefault(contact_id, {
                "name": contact_name,
                "channels": channels
            })

            if msg["id"] not in grouped[contact_id]:
                msg["contact_id"] = contact_id
                msg["contact_name"] = contact_name
                grouped[contact_id][msg["id"]] = msg
                print(f"  [GROUPED] msg_id={msg['id']} added under {contact_id}")

    print(f"\n[SUMMARY] Grouped messages for {len(grouped)} contact(s)\n")
    return grouped



# @twilio_bp.route('/twilio_webhook/send_whatsapp', methods=['POST'])
def send_whatsapp(from_number, to_number, body, conversation_id, subject):

    print(
        f"Sending WhatsApp message from {from_number} to {to_number} with body: {body}"
    )
    if not to_number or not body:
        print("Missing 'to' or 'body' fields")

    if not to_number.startswith("whatsapp:"):
        print("Recipient number must start with 'whatsapp:'")

   
    message = client.messages.create(
        from_=from_number,
        to=to_number,
        body=body,
    )

    message_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    # to_number = MESSAGES[conversation_sid]["from"] # for sending reply in a conversation

    MESSAGES[message.sid] = {
        "id": message_id,
        "from": from_number,
        "to": to_number,
        "body": body,
        "status": "sent",
        "source": "whatsapp",
        "direction": "outbound",
        "timestamp": timestamp,
        "conversation_id": conversation_id,
        "subject": subject,
        "sender_name": "You",
    }
    
    print(f"messages are: {MESSAGES}")

    return {"sid": message.sid, "status": "sent", "channel": "whatsapp"}

    
# @twilio_bp.route('/twilio_webhook/send-sms', methods=['POST'])
def send_sms(from_number, to_number, body, conversation_id, subject):
    print(
        f"Sending sms message from {from_number} to {to_number} with body: {body} conversation_id :{conversation_id} subject: {subject}"
    )
    if not to_number or not body:
        print("Missing 'to' or 'body' fields")

    if to_number.startswith("whatsapp:"):
        print("Recipient number cannot start with 'whatsapp:'")

   
    try:

        message = client.messages.create(
            from_=from_number,
            to=to_number,
            body=body,
        )

        message_id = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        MESSAGES[message.sid] = {
            "id": message_id,
            "from": from_number,
            "to": to_number,
            "body": body,
            "status": "sent",
            "source": "messenger",
            "direction": "outbound",
            "timestamp": timestamp,
            "conversation_id": conversation_id,
            "subject": subject,
            "sender_name": "You",
        }

        print(f"messages are: {MESSAGES}")

    except Exception as e:
        print(f"Error sending messages: {e}")
        return "Internal Server Error", 500

    return jsonify({"sid": message.sid, "status": "sent", "channel": "whatsapp"}), 200


# get status of messages
@twilio_bp.route("/twilio_webhook/status_callback", methods=["POST"])
def status_callback():
    payload = request.form.to_dict()
    # print(f"Received status callback: {payload}")

    sid = payload.get("MessageSid")
    status = payload.get("MessageStatus")
    from_name = payload.get("From")
    to = payload.get("To")
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    return "OK", 200


@twilio_bp.route("/twilio_webhook/receive_sms_messages", methods=["POST"])
def handle_twilio_sms_webhook():
    # payload = request.form.to_dict()  # uncomment later
    payload = request.form.to_dict() or request.get_json() or {}

    # print(f"Received payload from Twilio: {payload}")

    message_sid = payload.get("SmsMessageSid")
    sender_name = payload.get("ProfileName")
    content = payload.get("Body")
    from_ = payload.get("From")
    # from_number = from_.replace("whatsapp:", "")  # cahnge later
    from_number = from_  # for now, use the full number including whatsapp:

    to = payload.get("To")
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # if not message_sid or not from_number or not content or not to:
    #     return "Missing required fields", 400
    if not message_sid:
        return "Missing MessageSid", 400
    elif not content:
        return "Missing message content", 400
    elif not from_:
        return "Missing sender information", 400
    elif not to:
        return "Missing recipient information", 400

    if to.startswith("whatsapp:"):
        source = "whatsapp"
    else:
        source = "messages"

    message_id = str(uuid.uuid4())
    # twilio_sms_number='+14155238886'
    # twilio_whatsapp_number='whatsapp:+14155238886'
    subject = "testing"
    MESSAGES[message_sid] = {
        "id": message_id,
        "from": from_number if source == "messages" else from_,
        "sender_name": sender_name,
        "to": to,
        "body": content,
        "status": "sent",
        "source": source,
        "direction": "inbound",
        "timestamp": timestamp,
        "subject": subject,
        "conversation_id": from_,
    }
    print(
        f"Message from {sender_name}: number:{from_number}  body : {content}  source:{source} (SID: {message_sid}) at {timestamp}"
    )
    print(f"Current messages: {MESSAGES}")

    return "OK", 200


# receive whatsapp messages through twilio
@twilio_bp.route("/twilio_webhook/receive_messages", methods=["POST"])
def handle_twilio_webhook():
    payload = request.form.to_dict() or request.get_json()
    print(f"payload is: {payload}")
    # print("helloooo")

    # print(f"Received payload from Twilio: {payload}")
    to = payload.get("To")
    if to.startswith("whatsapp:"):
        source = "whatsapp"
    else:
        source = "messages"
    message_sid = payload.get("SmsMessageSid")
    sender_name = payload.get("ProfileName")
    content = payload.get("Body")
    from_ = payload.get("From")
    if source == "whatsapp":
        from_number = from_.replace("whatsapp:", "")
    else:
        from_number = from_  # for now, use the full number including whatsapp:
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    message_id = str(uuid.uuid4())
    # twilio_sms_number='+14155238886'
    # twilio_whatsapp_number='whatsapp:+14155238886'
    subject = "testing"
    MESSAGES[message_sid] = {
        "id": message_id,
        "from": from_number if source == "messages" else from_,
        "sender_name": sender_name,
        "to": to,
        "body": content,
        "status": "sent",
        "source": source,
        "direction": "inbound",
        "timestamp": timestamp,
        "subject": subject,
        "conversation_id": from_ if source == "whatsapp" else from_number,
    }

    print(f"Message from whatsapp:{MESSAGES[message_sid]} ")
    return "OK", 200


# receive voice calls and record them
@twilio_bp.route("/twilio_webhook/voice-webhook", methods=["POST"])
def voice_webhook():

    response = VoiceResponse()

    response.say(
        "Hello, your call will be recorded and transcribed after this message."
    )

    response.record(
        max_length=60,  # Max recording time in seconds
        transcribe=True,
        transcribe_callback="/twilio_webhook/transcription-complete",
        action="/twilio_webhook/recording-complete",
    )
    print(f"Voice webhook triggered. Recording will start now.")
    return Response(str(response), mimetype="application/xml")


@twilio_bp.route("/twilio_webhook/recording-complete", methods=["POST"])
def recording_complete():

    recording_url = request.form.get("RecordingUrl")
    call_sid = request.form.get("CallSid")

    print(f"Recording completed: {recording_url} (Call SID: {call_sid})")
    return "OK", 200


@twilio_bp.route("/twilio_webhook/transcription-complete", methods=["POST"])
def transcription_complete():
    transcription_text = request.form.get("TranscriptionText")
    transcription_sid = request.form.get("TranscriptionSid")
    recording_sid = request.form.get("RecordingSid")
    call_sid = request.form.get("CallSid")
    from_number = request.form.get("From")
    to_number = request.form.get("To")

    # Fallback in case no transcription
    if not transcription_text:
        transcription_text = "(No transcription text available)"

    # Create a unique message ID
    message_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    MESSAGES[transcription_sid] = {
        "id": message_id,
        "from": from_number,
        "to": to_number,
        "body": transcription_text,
        "status": "received",
        "source": "phone",
        "direction": "inbound",
        "timestamp": timestamp,
    }

    print(f"Stored transcription for call {call_sid}:")
    print(f"From: {from_number}, To: {to_number}")
    print(f"Transcription: {transcription_text}")
    print(f"MESSAGES: {MESSAGES}")

    return "OK", 200


@twilio_bp.route("/make-call", methods=["GET"])
def make_call():
    call = client.calls.create(
        to="+829953540",  # Replace with the recipient's phone number
        from_="+15017122661",
        twiml="<Response><Say>Hello, this call is recorded.</Say></Response>",
        record=True,
        recording_status_callback="https://yourdomain.com/recording-events",
        recording_status_callback_event=["completed"],
    )
    return f"Call initiated. Call SID: {call.sid}"


# getting messages from slack
@twilio_bp.route("/slack/events/receive_messages", methods=["POST"])
def handle_slack_events():
    data = request.get_json()

    if data.get("type") == "url_verification":
        return jsonify({"challenge": data["challenge"]})

    event = data.get("event", {})
    if event.get("type") == "message" and event.get("subtype") is None:
        user_id = event.get("user")
        text = event.get("text")
        channel = event.get("channel")

        if channel.startswith("D"):
            source = "slack"
        elif channel.startswith("C"):
            source = "slackworkspace"
        else:
            print(f"Unsupported channel type: {channel}")
            return "Unsupported channel type", 400


        my_user_id = "U094N4DTVSL"  # Replace later with your actual Slack user ID

        message_id = str(uuid.uuid4())
        user_info = slack_client.users_info(user=user_id)
        if user_info["ok"]:
            display_name = user_info["user"]["profile"]["display_name"]
            real_name = user_info["user"]["real_name"]
            email = user_info["user"]["profile"].get("email")
            direction = (
                "outbound"
                if channel.startswith("D") and user_id == my_user_id
                else "inbound"
            )

            slack_ts = event.get("ts")
            if slack_ts:
                seconds = float(slack_ts)
                dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
                iso_timestamp = dt.isoformat().replace("+00:00", "Z")
            else:
                iso_timestamp = None

            subject = "testing"  # change this later

            MESSAGES[key] = {
                "id": message_id,
                "from": user_id,
                "sender_name": real_name,
                "to": channel,
                "body": text,
                "status": "sent",
                "source": "slack",
                "subject": subject,
                "direction": direction,
                "timestamp": iso_timestamp,
                "conversation_id": channel,  # Use channel ID as conversation ID
            }

        else:
            print(f"Failed to fetch user info for user ID: {user_id}")

        print(f"Message from slack:{MESSAGES[key]} ")
    else:
        print(f"Received event: {event}")
    return "OK", 200


def get_user_profile(sender_id, page_access_token):
    url = f"https://graph.facebook.com/v19.0/{sender_id}"
    params = {"fields": "first_name,last_name", "access_token": page_access_token}
    response = requests.get(url, params=params)
    if response.ok:
        profile = response.json()
        return f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
    else:
        print("Error fetching profile:", response.text)
        return None


@twilio_bp.route("/facebook/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        token_sent = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if token_sent == VERIFY_TOKEN:
            return challenge, 200
        return "Invalid verification token", 403

    elif request.method == "POST":
        data = request.get_json()
        print("Webhook received data:", data)

        if data["object"] == "page":
            for entry in data["entry"]:
                messaging = entry.get("messaging")
                if messaging:
                    for message_event in messaging:
                        sender_id = message_event["sender"]["id"]
                        recipient_id = message_event["recipient"]["id"]
                        timestamp_ms = message_event["timestamp"]
                        message_data = message_event.get("message")

                        if message_data:
                            message_id = message_data.get("mid")
                            text = message_data.get("text")

                            iso_timestamp = (
                                datetime.utcfromtimestamp(
                                    timestamp_ms / 1000
                                ).isoformat()
                                + "Z"
                            )

                            # Optionally fetch sender name
                            real_name = get_user_profile(sender_id, PAGE_ACCESS_TOKEN)

                            uuid_id = str(uuid.uuid4())

                            MESSAGES[message_id] = {
                                "id": uuid_id,
                                "from": sender_id,
                                "sender_name": real_name,
                                "to": recipient_id,
                                "body": text,
                                "status": "received",
                                "source": "facebook",
                                "direction": "inbound",
                                "timestamp": iso_timestamp,
                            }

                            print("Stored message:", MESSAGES[key])

                            # Respond to user
                            send_message(
                                recipient_id=sender_id,
                                message_text="Thanks for your message!",
                                page_access_token=PAGE_ACCESS_TOKEN,
                            )

        return "EVENT_RECEIVED", 200


def send_slack_message(
    channel_id, text, subject, sender_user_id=None, sender_name=None
):

    print(f"Sending Slack message to {channel_id} with body: {text}")

    if not channel_id or not text:
        print("Missing channel_id or text fields")
        return None

    try:
        # Send the message via Slack API
        response = slack_client.chat_postMessage(channel=channel_id, text=text)
        print(f"Slack message sent. ts: {response['ts']}")

        # Generate UUID for internal tracking
        message_id = str(uuid.uuid4())

        # Slack returns a timestamp like "1717834202.000300"
        slack_ts = response.get("ts")
        if slack_ts:
            seconds = float(slack_ts)
            dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
            iso_timestamp = dt.isoformat().replace("+00:00", "Z")
        else:
            iso_timestamp = None

        key = f"{channel_id}:{slack_ts}"
        my_user_id = "U094N4DTVSL"
        # Save message to your in-memory store
        MESSAGES[key] = {
            "id": message_id,
            "from": my_user_id,
            "sender_name": sender_name or "Bot",
            "to": channel_id,
            "body": text,
            "status": "sent",
            "source": "slack",
            "direction": "outbound",
            "subject": subject,
            "timestamp": iso_timestamp,
            "conversation_id": channel_id,
        }

        print(
            f"Stored Slack message in MESSAGES under key: {MESSAGES[key]['id']}: {MESSAGES[key]}"
        )
        return response

    except Exception as e:
        print("Error sending Slack message:", str(e))
        raise


@twilio_bp.route("/get_all_messages/<user_id>", methods=["GET"])
def getall(user_id):

    timestamp = datetime.now(timezone.utc)
    date_str = timestamp.strftime("%Y-%m-%d")
    file_loc = f"cust_helpers/messages/{user_id}/{date_str}"
    print("*********calling all gmail msg**********")
    gmail = fetch_gmail_messages(user_id)
    print("*********calling all zoho msg**********")
    zoho = fetch_zoho_emails(user_id)

    analyze_and_collect_messages(user_id)

    return "OK"


def get_existing_messages(user_id):
    print(f"\n🚀 Starting message analysis for user_id: {user_id}")
    user_folder = os.path.join(pathconfig.basepath, "messages", user_id)
    ensure_dir(user_folder)
    print(f"📁 Ensured user folder exists at: {user_folder}")

    timestamp = datetime.now(timezone.utc)
    date_str = timestamp.strftime("%Y-%m-%d")
    filename = f"{date_str}.json"
    user_filepath = os.path.join(user_folder, filename)
    print(f"📄 Looking for user message file: {filename}")

    with open(user_filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    grouped_messages = data.get("input_data", {})
    print(f"📦 Loaded grouped_messages for {len(grouped_messages)} clients")



@twilio_bp.route("/analyze_and_collect_messages/<user_id>", methods=["GET"])
def analyze_and_collect_messages(user_id):
    
    print(f"\n🚀 Starting message analysis for user_id: {user_id}")
    user_folder = os.path.join(pathconfig.basepath, "messages", user_id)
    ensure_dir(user_folder)

    timestamp = datetime.now(timezone.utc)
    date_str = timestamp.strftime("%Y-%m-%d")
    filename = f"{date_str}.json"
    user_filepath = os.path.join(user_folder, filename)

    try:
        with open(user_filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {"input_data": {}}
        with open(user_filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    grouped_messages = data.get("input_data", {})

    new_messages = []

    for client_id, channel_data in grouped_messages.items():
        connection = connect_to_rds()
        if connection is None:
            print("❌ Failed to connect to RDS")
            return None

        cursor = connection.cursor()
        cursor.execute(
            "SELECT email_id FROM users_clients WHERE users_clients_id = %s", (client_id,)
        )
        client_row = cursor.fetchone()
        if not client_row:
            print("❌ Error: User not found in users_clients table")
            continue

        client_email = client_row[0]

        config_folder = os.path.join(pathconfig.basepath, "messages", user_id, client_id)
        ensure_dir(config_folder)

        config_filepath = os.path.join(config_folder, "config.json")
        try:
            with open(config_filepath, "r", encoding="utf-8") as f:
                config_data = json.load(f)
        except FileNotFoundError:
            config_data = {}
            print("⚠️ Config file not found")

        existing_channels = {
            convo.get("channel")
            for convo in config_data.get("conversations", [])
            if convo.get("channel")
        }

        for channel, channel_msgs in channel_data.items():
            channel_msgs.sort(key=lambda x: x.get("timestamp", ""))  # optional
            latest_msg = channel_msgs[-1] if channel_msgs else None

            output_filename = f"{channel}_new_messages.json"
            output_path = os.path.join(config_folder, output_filename)

            new_msg_data = {}
            try:
                with open(output_path, "r", encoding="utf-8") as f:
                    new_msg_data = json.load(f)
            except Exception as e:
                print(f"⚠️ Couldn't read existing messages: {e}")

            existing_new_msg = {
                msg.get("msg_id"): msg for msg in new_msg_data.get("new_messages", [])
            }
            merged_messages = new_msg_data.get("new_messages", [])

            for m in channel_msgs:
                msg_id = m.get("id")
                
                cursor.execute("SELECT 1 FROM messages WHERE message_id = %s", (msg_id,))
                m_id = cursor.fetchone()
                if m_id:
                    continue
                
                new_msg = {
                    "msg_id": msg_id,
                    "body": m.get("body"),
                    "from": "client" if m.get("from") == client_email else "user",
                    "to": "client" if m.get("to") == client_email else "user",
                    "date": m.get("timestamp"),
                    # "status": classification,
                    "channel": channel,
                }
                merged_messages.append(new_msg)

            with open(output_path, "w", encoding="utf-8") as f:
                json.dump({"new_messages": merged_messages}, f, indent=2)
            
            subjects = generate_subject(user_id, output_path, channel)

            grouped_messages = append_subject_to_messages(grouped_messages,channel,subjects,user_id,existing_new_msg)


    print("✅ Finished message collection and classification.")
    return new_messages

            

@twilio_bp.route("/subject_summarisations/<user_id>", methods=["POST"])
def generate_subject(user_id, output_path,channel):
    try:
        if not user_id or not output_path:
            print("❌ Missing user_id or filename")
            return None

        with open(output_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        all_messages = data.get("new_messages", [])
        if channel:
            filtered_messages = [msg for msg in all_messages if msg.get("channel") == channel]
        else:
            filtered_messages = all_messages

        if not filtered_messages:
            print(f"⚠️ No messages found for channel: {channel}")
            return []
        
        # Load prompt + workflow YAML
        yaml_data = load_yaml_file(path=pathconfig.conv_template)
        update_prompt_template = yaml_data.get("summarize_message_body")
        if not update_prompt_template:
            print("❌ Prompt 'summarize_message_body' not found in template")
            return None

        # Inject message data into prompt
        message_payload = json.dumps(filtered_messages, indent=2)
        full_prompt = update_prompt_template.replace("{full_text_message_body}", message_payload)

        # Generate YAML output from model
        modified_yaml = get_fireworks_response(full_prompt, role="system")


        try:
            parsed_yaml = yaml.safe_load(modified_yaml.strip())
            if "subject_groups" in parsed_yaml:
                return parsed_yaml["subject_groups"]
            else:
                print("⚠️ Key 'subject_groups' missing after parsing")
                return None
        except Exception as e:
            print(f"🔥 YAML parse failed: {e}")
            return None

        try:
            if yaml_match:
                parsed_yaml = yaml.safe_load(yaml_match.group(0))
            else:
                raise ValueError("Could not extract valid subject_groups YAML block.")

            if "subject_groups" not in parsed_yaml:
                print("⚠️ Key 'subject_groups' missing after parsing")
                return None

            return parsed_yaml["subject_groups"]

        except Exception as e:
            print(f"🔥 Exception during summarisation: {str(e)}")
            return None
    except Exception as e:
            print(f"🔥 Exception during summarisation: {str(e)}")
            return None


def append_subject_to_messages(grouped_messages,channel, subjects, user_id,existing_new_msg):
    # Build a lookup of message_id → subject
    subject_map = {}
    for group in subjects:
        subject = group["summary"]
        for mid in group["message_ids"]:
            subject_map[str(mid)] = subject

    updated_date = datetime.now(timezone.utc).isoformat()
    created_date = updated_date

    connection = connect_to_rds()
    if connection is None:
        print("⚠️ DB connection failed inside append_subject_to_messages")
        return None

    cursor = connection.cursor()

    # Iterate through grouped_messages structure
    for client_id, channels in grouped_messages.items():
        s3_config_key = f"{user_id}/messages/{client_id}/config.json"

        try:
           
            config_data = read_json_from_s3(s3_config_key)
            if config_data is None:
                config_data = {}

        except FileNotFoundError:
            config_data = {}
            print("no config file")

        # for channel, messages in channels.items():
        messages = grouped_messages.get(client_id, {}).get(channel, [])

        if channel == "zoho":
                # Build subject lookup per client
                    config_subject_lookup = {}
                    if config_data:
                        for conv in config_data.get("conversations", []):
                            if conv.get("channel", "").lower() != "zoho":
                                continue
                            conf_subj = conv.get("subject", "")
                            conf_ticket_name = conv.get("ticket_name", "")
                            conf_conv_id = conv.get("conv_id", "")
                            conf_ticket_id = conv.get("ticket_id", "")
                            if conf_subj:
                                config_subject_lookup[conf_subj] = {
                                    "conv_id": conf_conv_id,
                                    "ticket_id": conf_ticket_id,
                                    "ticket_name": conf_ticket_name,
                                }
        for msg in messages:
                        
                        msg_id = msg.get("id")

                        cursor.execute("SELECT 1 FROM messages WHERE message_id = %s", (msg_id,))
                        m_id = cursor.fetchone()
                        if m_id:
                            continue


                        subject = msg.get("subject", "") 
                        is_reply = subject.lower().startswith("re:") or "wrote:" in msg.get("summary", "").lower()

                        # 1. for zoho reply
                        if channel == "zoho" and is_reply:
                    
                            normalized_subject = re.sub(r"^re:\s*", "", subject, flags=re.IGNORECASE)
                            config_thread = config_subject_lookup.get(normalized_subject)
                            if config_thread:
                                c_id = config_thread["conv_id"]
                                t_id = config_thread["ticket_id"]
                                t_name = config_thread["ticket_name"]
                                msg["conversation_id"] = c_id
                                msg["ticket_id"] = t_id
                                msg["ticket_name"] = t_name


                                cursor.execute(
                                        "UPDATE tickets SET updated_in = %s, status = %s WHERE conversation_id_fk = %s",
                                        (updated_date, "In-Progress", c_id)
                                    )

                                cursor.execute(
                                        "UPDATE threads SET last_message_at = %s WHERE conversation_id = %s",
                                        (updated_date, c_id)
                                    )

                                cursor.execute(
                                        "UPDATE messages SET update_at = %s WHERE conversation_id = %s",
                                        (updated_date, c_id)
                                    )

                                grouped_messages[client_id][channel] = messages
                                update_or_create_conversation_file(grouped_messages, user_id, client_id, channel)


                                try:
                                                    if updated_date.endswith('Z'):
                                                        parsed_ts = datetime.fromisoformat(updated_date.replace("Z", "+00:00"))
                                                    else:
                                                        parsed_ts = datetime.fromisoformat(updated_date)
                                except Exception as e:
                                                    print(f"[WARN] Could not parse updated_date '{updated_date}': {e}")
                                                    parsed_ts = datetime.now(timezone.utc)

                                                    
                                for i, conv in enumerate(config_data.get("conversations", [])):
                                    if conv.get("conv_id") == c_id:
                                        config_data["conversations"][i]["updated_date"] = updated_date
                                        config_data["conversations"][i]["parsed_timestamp"] = parsed_ts.isoformat()
                                        break
                                                
                                update_config_file(user_id, client_id, config_data)
                                connection.commit()

                                continue
                        
                        
                        thread_id = msg.get("thread_id")
                        direction = msg.get("direction")

                        # 2. for gmail reply
                        if thread_id:

                            if config_data:
                                for conv in config_data.get("conversations", []):
                                    if conv.get("thread_id") == thread_id:
                                        conversation_id = conv.get("conv_id")                         
                                        if conversation_id:
                                            msg["conversation_id"] = conversation_id
                                            cursor.execute(
                                                "SELECT tickets_id, ticket_name FROM tickets WHERE conversation_id_fk = %s",
                                                (conversation_id)
                                            )
                                            ticket_row = cursor.fetchone()
                                            ticket_id = ticket_row[0]
                                            ticket_name = ticket_row[1]
                                            if ticket_row:
                                                msg["ticket_id"] = ticket_id
                                                msg["ticket_name"] = ticket_name
                                                msg["conversation_id"] = conversation_id


                                                cursor.execute(
                                                    "UPDATE tickets SET updated_in = %s, status = %s WHERE conversation_id_fk = %s",
                                                    (updated_date, "In-Progress", conversation_id)
                                                )

                                                cursor.execute(
                                                    "UPDATE threads SET last_message_at = %s WHERE conversation_id = %s",
                                                    (updated_date, conversation_id)
                                                )
                                        
                                                cursor.execute(
                                                    "UPDATE messages SET update_at = %s WHERE conversation_id = %s",
                                                    (updated_date, conversation_id)
                                                )

                                                
                                                    
                                                grouped_messages[client_id][channel] = messages
                                                update_or_create_conversation_file(grouped_messages, user_id, client_id, channel)                                                                      

                                               
                                                try:
                                                                    if updated_date.endswith('Z'):
                                                                        parsed_ts = datetime.fromisoformat(updated_date.replace("Z", "+00:00"))
                                                                    else:
                                                                        parsed_ts = datetime.fromisoformat(updated_date)
                                                except Exception as e:
                                                                    print(f"[WARN] Could not parse updated_date '{updated_date}': {e}")
                                                                    parsed_ts = datetime.now(timezone.utc)

                                                for i, conv in enumerate(config_data.get("conversations", [])):
                                                    if conv.get("conv_id") == conversation_id:
                                                        config_data["conversations"][i]["updated_date"] = updated_date
                                                        config_data["conversations"][i]["parsed_timestamp"] = parsed_ts

                                                        break
                                                    
                                                update_config_file(user_id, client_id, config_data)
                                                connection.commit()
                                                continue
                                            else:
                                                print(f"⚠️ No ticket found for conversation: {conversation_id}")
                                    break

                        # Thread does not exist — create new thread and optionally ticket
                        new_conversation_id = str(uuid.uuid4())
                        msg["conversation_id"] = new_conversation_id

                        cursor.execute(
                                """
                                INSERT INTO threads (conversation_id, started_at, status, last_message_at)
                                VALUES (%s, %s, %s, %s)
                                """,
                                (new_conversation_id, created_date, "Open", updated_date)
                            )


                        # 3. new for inbound msg
                        if direction == "inbound":
                            new_ticket_id = str(uuid.uuid4())
                        

                            ticket_name = subject_map.get(str(msg_id))
                            subject = msg.get("subject")
                            msg["ticket_id"] = new_ticket_id
                            msg["ticket_name"] = ticket_name
                            msg["conversation_id"] = new_conversation_id
                            

                            cursor.execute("SELECT 1 FROM tickets WHERE tickets_id = %s", (new_ticket_id,))
                            if not cursor.fetchone():
                                
                                cursor.execute(
                                    "INSERT INTO tickets (tickets_id, ticket_name, conversation_id_fk,status,priority) VALUES (%s, %s, %s,%s,%s)",
                                    (new_ticket_id, ticket_name, new_conversation_id,"Open","Medium")
                                )

                                assigned_id = str(uuid.uuid4())  # Generate UUID for assigned_id
                                cursor.execute(
                                    """
                                    INSERT INTO assigned (assigned_id, user_id_fk, users_clients_id_fk, ticket_id_fk)
                                    VALUES (%s, %s, %s, %s)
                                    """,
                                    (assigned_id, user_id, client_id, new_ticket_id)
                                )

                                
                            cursor.execute(
                                """
                                UPDATE threads
                                SET ticket_id_fk = %s
                                WHERE conversation_id = %s
                                """,
                                (new_ticket_id, new_conversation_id)
                            )

                            cursor.execute("SELECT 1 FROM messages WHERE message_id = %s", (msg_id,))
                            if not cursor.fetchone():
                               
                                cursor.execute(
                                    """
                                    INSERT INTO messages (
                                        message_id,
                                        conversation_id_fk,
                                        sender_id,
                                        content_ref,
                                        message_type,
                                        is_summary,
                                        created_at,
                                        update_at
                                    )
                                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                                    """,
                                    (
                                        msg_id,
                                        new_conversation_id,
                                        client_id,
                                        "ref",
                                        "inbound",
                                        subject,
                                        created_date,
                                        updated_date
                                    )
                                )


                            # Update config immediately
                            try:
                                if updated_date.endswith('Z'):
                                    parsed_ts = datetime.fromisoformat(updated_date.replace("Z", "+00:00"))
                                else:
                                    parsed_ts = datetime.fromisoformat(updated_date)
                            except Exception as e:
                                    print(f"[WARN] Could not parse updated_date '{updated_date}': {e}")
                            
                            updated_entry = {
                                "conv_id": new_conversation_id,
                                "ticket_id": new_ticket_id,
                                "ticket_name": ticket_name,
                                "subject": subject_map.get(str(msg_id)),
                                "channel": channel,
                                "updated_date": updated_date,
                                "parsed_timestamp": parsed_ts.isoformat()
                            }
                            if channel == "gmail" and thread_id:
                                updated_entry["thread_id"] = thread_id

                            config_data.setdefault("userclients_id", client_id)
                            config_data.setdefault("conversations", []).append(updated_entry)

                     

                        # 4. for new outbound msg
                        else:
                            msg["ticket_id"] = None
                            msg["ticket_name"] = None
                            msg["conversation_id"] = new_conversation_id

                            cursor.execute(
                                """
                                UPDATE threads
                                SET ticket_id_fk = %s
                                WHERE conversation_id = %s
                                """,
                                (None, new_conversation_id)
                            )

                            cursor.execute(
                                """
                                INSERT INTO messages (
                                    message_id,
                                    conversation_id_fk,
                                    sender_id,
                                    content_ref,
                                    message_type,
                                    is_summary,
                                    created_at,
                                    update_at
                                )
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                                """,
                                (
                                    msg_id,
                                    new_conversation_id,
                                    client_id,
                                    "ref",
                                    "outbound",
                                    subject,
                                    created_date,
                                    updated_date
                                )
                            )

                            updated_entry = {
                                "conv_id": new_conversation_id,
                                "ticket_id": new_ticket_id,
                                "ticket_name": ticket_name,
                                "subject": subject_map.get(str(msg_id)),
                                "channel": channel,
                                "updated_date": datetime.now(timezone.utc).isoformat()
                            }
                            if channel == "gmail" and thread_id:
                                updated_entry["thread_id"] = thread_id
                            
                            config_data.setdefault("userclients_id", client_id)
                            config_data.setdefault("conversations", []).append(updated_entry)
                        
                        grouped_messages[client_id][channel] = messages
                        update_or_create_conversation_file(grouped_messages,user_id,client_id,channel)

                        update_config_file(user_id, client_id, config_data)
                            

                        connection.commit()
                    
    connection.close()
    return grouped_messages


def update_config_file(user_id, client_id, config_data):
    config_folder = os.path.join(pathconfig.basepath, "messages", user_id, client_id)
    ensure_dir(config_folder)
    config_filepath = os.path.join(config_folder, "config.json")
    s3_config_key = f"{user_id}/messages/{client_id}/config.json"

    s3_data = read_json_from_s3(s3_config_key)
    input_data = s3_data.get("conversations", [])

    # if process == "append":
    #     input_data.extend(config_data)
    # else:
    #     for conv in input_data:
    #         if conv.get("conv_id") == conv_id:
    #             conv["updated_date"] = updated_date

    # updated_config = {
    #     "userclients_id": client_id,
    #     "conversations": input_data
    # }

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




def update_or_create_conversation_file(grouped_messages, user_id, client_id, channel):
    prefix = f"{user_id}/messages/{client_id}"
    config_key = f"{prefix}/config.json"
    config_data = read_json_from_s3(config_key)

    # Pull existing conversation IDs for the specified channel
    existing_conversations = {
        conv["conv_id"] for conv in config_data.get("conversations", [])
        if conv.get("channel") == channel and conv.get("conv_id")
    }

    channel_messages = grouped_messages.get(client_id, {}).get(channel, [])
    if not channel_messages:
        print(f"[INFO] No messages found for client={client_id}, channel={channel}")
        return

    # Group messages by conversation_id
    conversation_groups = {}
    for msg in channel_messages:
        conv_id = msg.get("conversation_id")
        if conv_id:
            conversation_groups.setdefault(conv_id, []).append(msg)

    for conv_id, messages in conversation_groups.items():
        file_key = f"{prefix}/{conv_id}.json"
        conv_folder = os.path.join(
                            pathconfig.basepath, "messages", user_id, client_id
                        )
        ensure_dir(conv_folder)
        conv_file_name = f"{conv_id}.json"
        conv_filepath = os.path.join(conv_folder, conv_file_name)
        s3_config_key = f"{user_id}/messages/{client_id}/{conv_id}.json"


        if conv_id in existing_conversations:
            raw_data = read_json_from_s3(file_key)
            input_data = raw_data.get("input_data", [])
            input_data.extend(messages)

            with open(conv_filepath, "w", encoding="utf-8") as f:
                json.dump({"input_data": input_data}, f, indent=2)


            upload_any_file(
                                    conv_filepath,
                                    user_id,
                                    type="messages",
                                    s3_key_C=s3_config_key,
                                ) 
        else:

            with open(conv_filepath, "w", encoding="utf-8") as f:
                json.dump({"input_data": messages}, f, indent=2)

            upload_any_file(
                                    conv_filepath,
                                    user_id,
                                    type="messages",
                                    s3_key_C=s3_config_key,
                                )            


def get_latest_convo_info(config):
    """
    Get the latest conversation from a config file based on parsed_timestamp
    """

    if not config or "conversations" not in config:
        return None

    config_data = config.get("conversations", [])
    client_id=config.get("userclients_id", [])

    if not config_data:
        print("[DEBUG] input_data is empty")
        return None

    conversations = {}

    for msg in config_data:
        conv_id = msg.get("conv_id")
        ts_str = msg.get("parsed_timestamp")

        if not conv_id or not ts_str:
            print(f"[DEBUG] Skipping message due to missing conv_id or parsed_timestamp")
            continue

        try:
            msg_ts = datetime.fromisoformat(ts_str)

            if conv_id not in conversations or msg_ts > conversations[conv_id]["parsed_timestamp"]:
                conversations[conv_id] = {
                    "message": msg,
                    "parsed_timestamp": msg_ts
                }
        except Exception as e:
            print(f"[WARN] Failed to parse parsed_timestamp '{ts_str}': {e}")
            continue

    if not conversations:
        print("[DEBUG] No valid conversations found after grouping")
        return None

    latest_conv = max(conversations.values(), key=lambda x: x["parsed_timestamp"])
    latest_msg = latest_conv["message"]


    return {
        "updated_date": latest_conv["parsed_timestamp"].isoformat(),
        "conv_id": latest_msg.get("conv_id"),
        "client_id": client_id,  
    }


def extract_unique_client_folders(file_list, base_prefix):
    if not file_list:
        return []

    client_ids = set()
    for obj in file_list:
        key = obj.get("Key", "")
        if key.startswith(base_prefix):
            parts = key[len(base_prefix):].split("/")
            if parts and parts[0]:
                client_ids.add(parts[0])
    return list(client_ids)

@twilio_bp.route("/conversations/<user_id>", methods=["GET"])
def get_latest_conversations(user_id):
    """
    Get the latest conversation from each client's config file and load full conversation data
    """
    client_prefix = f"{user_id}/messages/"

    raw_file_list = list_all_files(client_prefix)
    client_ids = extract_unique_client_folders(raw_file_list, client_prefix)

    conversations = []
    disp_messages = []
    
    for client_id in client_ids:
        config_path = f"{client_prefix}{client_id}/config.json"
        
        try:
            config = read_json_from_s3(config_path)
            recent_msg = get_latest_convo_info(config)
            
            if recent_msg:
                conversations.append(recent_msg)
                
                conv_id = recent_msg['conv_id']
                convo_path = f"{client_prefix}{client_id}/{conv_id}.json"
                
                try:
                    convo_data = read_json_from_s3(convo_path)
                    convo_messages = convo_data.get("input_data", [])

                    if convo_messages:
                        latest_msg_in_conv = None
                        latest_timestamp = None
                        
                        for msg in convo_messages:
                            timestamp_str = msg.get("timestamp")
                            if not timestamp_str:
                                continue
                            try:
                                if timestamp_str.endswith('Z'):
                                    msg_ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                                else:
                                    msg_ts = datetime.fromisoformat(timestamp_str)
                                
                                if latest_timestamp is None or msg_ts > latest_timestamp:
                                    latest_timestamp = msg_ts
                                    latest_msg_in_conv = msg
                            except Exception as e:
                                print(f"[WARN] Failed to parse message timestamp: {e}")
                                continue
                        
                        if latest_msg_in_conv:
                            now = datetime.now(timezone.utc)
                            time_diff = now - latest_timestamp
                            
                            if time_diff.days > 0:
                                relative_time = f"{time_diff.days} days ago"
                            elif time_diff.seconds > 3600:
                                relative_time = f"{time_diff.seconds // 3600} hours ago"
                            elif time_diff.seconds > 60:
                                relative_time = f"{time_diff.seconds // 60} minutes ago"
                            else:
                                relative_time = "Just now"
                            
                            user_email = latest_msg_in_conv.get("user_id", "Unknown")
                            from_email = latest_msg_in_conv.get("from", "")
                            to_email = latest_msg_in_conv.get("to", "")
                            
                            contact_email = from_email if from_email != user_email else to_email
                            contact_name = contact_email.split('@')[0] if contact_email else "Unknown"
                            has_unread = any(m.get("status") == "received" for m in convo_messages)
                            
                            disp_message = {
                                "contact_id": contact_email,
                                "name": contact_name,
                                "lastMessage": latest_msg_in_conv.get("body", "")[:100],
                                "timestamp": relative_time,
                                "isoTimestamp": latest_msg_in_conv.get("timestamp"),
                                "unread": has_unread,
                                "channel": latest_msg_in_conv.get("source"),
                                "subject": latest_msg_in_conv.get("subject"),
                                "conv_id": conv_id,
                                "ticket_id": latest_msg_in_conv.get("ticket_id"),
                                "ticket_name": latest_msg_in_conv.get("ticket_name"),
                                "full_conversation": convo_messages
                            }
                            
                            disp_messages.append(disp_message)
                    
                except Exception as e:
                    print(f"  [WARN] Failed to read conversation {conv_id}: {e}")
            else:
                print(f"  [INFO] No conversations found in config for client: {client_id}")
                
        except Exception as e:
            print(f"  [WARN] Skipping config for client {client_id}: {e}")
            continue
    
    disp_messages.sort(key=lambda x: x['isoTimestamp'], reverse=True)    
    return disp_messages


def get_conv_order(config):
    """
    Return sorted list of conv_ids based on parsed_timestamp
    """

    if not config or "conversations" not in config:
        return None

    config_data = config.get("conversations", [])

    if not config_data:
        return None

    conversations = []

    for msg in config_data:
        conv_id = msg.get("conv_id")
        ts_str = msg.get("parsed_timestamp")

        if not conv_id or not ts_str:
            continue

        try:
            msg_ts = datetime.fromisoformat(ts_str)
            conversations.append((conv_id, msg_ts))
        except Exception as e:
            print(f"[WARN] Failed to parse parsed_timestamp '{ts_str}': {e}")
            continue

    if not conversations:
        print("[DEBUG] No valid conversations found after timestamp parsing")
        return None

    sorted_convos = sorted(conversations, key=lambda x: x[1], reverse=True)
    sorted_ids = [conv_id for conv_id, _ in sorted_convos]

    return sorted_ids



@twilio_bp.route("/conversations/<conversation_id>/<user_id>", methods=["GET"])
def get_selected_conv(conversation_id, user_id):

    try:
        connection = connect_to_rds()
        if connection is None:
            return jsonify({"error": "Database connection failed"}), 500
        cursor = connection.cursor()

        try:
            cursor.execute(
                "SELECT sender_id FROM messages WHERE conversation_id_fk = %s",
                (conversation_id,)
            )
            client_id_row = cursor.fetchone()
            if client_id_row is None:
                print(f"⚠️ No sender_id found for conversation_id {conversation_id}")
                return jsonify({"error": "Conversation not found"}), 404
            client_id = client_id_row[0]
        except Exception as e:
            print(f"❌ Error executing sender_id query: {e}")

        config_path = f"{user_id}/messages/{client_id}/config.json"
        try:
            config = read_json_from_s3(config_path)
        except Exception as e:
            return jsonify({"error": "Failed to load config"}), 500

        try:
            recent_msg = get_conv_order(config)
        except Exception as e:
            return jsonify({"error": "Invalid config format"}), 500

        messages = []
        for conv in recent_msg:
            try:
                convo_path = f"{user_id}/messages/{client_id}/{conv}.json"
                convo_data = read_json_from_s3(convo_path)
                convo_messages = convo_data.get("input_data", [])

                channel = convo_messages[0].get("source") if convo_messages else "unknown"
                messages.append({
                    "id": conv,
                    "channel": channel,
                    "messages": convo_messages
                })

            except Exception as e:
                print(f"❌ Failed to read or parse {convo_path}: {e}")
                continue
        return jsonify(messages)

    except Exception as e:
        print(f"❌ Unexpected error in get_selected_conv(): {e}")
        return jsonify({"error": "Internal server error"}), 500


    
@twilio_bp.route("/start-conversation", methods=["POST"])
def start_conversation():
        
        data = request.get_json() or {}
        user_id = data.get("user_id")
        client_id = data.get("contact_id")


        if not user_id or not client_id:
            return jsonify({"error": "Missing user_id or contact_id"}), 400

        try:
            connection = connect_to_rds()
            if connection is None:
                return jsonify({"error": "Database connection failed"}), 500
            cursor = connection.cursor()

            config_path = f"{user_id}/messages/{client_id}/config.json"
            config = None
            try:
                config = read_json_from_s3(config_path)
            except Exception as e:
                print(f"[WARN] → Config not found for {client_id}, returning minimal response")
            
            if config is None:
                return jsonify({
                    "identities": [],
                    "status": "new",
                    "conversationId": client_id,
                    "messages": []
                }), 200

            try:
                recent_msg = get_conv_order(config)
            except Exception as e:
                return jsonify({"error": "Invalid config format"}), 500

            messages = []
            for conv in recent_msg:
                try:
                    convo_path = f"{user_id}/messages/{client_id}/{conv}.json"
                    convo_data = read_json_from_s3(convo_path)
                    convo_messages = convo_data.get("input_data", [])
                    channel = convo_messages[0].get("source") if convo_messages else "unknown"

                    messages.append({
                        "id": conv,
                        "channel": channel,
                        "messages": convo_messages
                    })

                except Exception as e:
                    print(f"❌ Failed to read or parse {convo_path}: {e}")
                    continue

            # return jsonify(messages)
            return jsonify({
                "identities": config.get("identities", []),
                "status": "existing",
                "conversationId": client_id,
                "messages": messages  # this is ConversationThread[]
            }), 200



        except Exception as e:
            print(f"❌ Unexpected error in get_selected_conv(): {e}")
            return jsonify({"error": "Internal server error"}), 500



def match_email_to_channel(email, channel):
    """Returns True if the email matches the given channel."""
    if not email or "@" not in email:
        return False
    domain = email.lower().split("@")[1]
    return channel.lower() in domain

@twilio_bp.route("/send-reply", methods=["POST"])
def send_messages():

    try:
        data = request.json
        user_id = data.get("user_id")
        channel = data.get("channel")
        text = data.get("text")
        conversation_id = data.get("conversation_id")
        contact_id = data.get("contact_id")

        if not all([user_id, channel, text]):
            return jsonify({"error": "Missing required payload fields"}), 400

        connection = connect_to_rds()
        if connection is None:
            return jsonify({"error": "Database connection failed"}), 500

        cursor = connection.cursor()

        is_reply = False
        client_id = None
        thread_id = None

         # getting client id
        try:
            cursor.execute(
                "SELECT sender_id FROM messages WHERE conversation_id_fk = %s",
                (conversation_id,)
            )
            client_id_row = cursor.fetchone()
            if client_id_row is None:
                return jsonify({"error": "Conversation not found"}), 404
            client_id = client_id_row[0]
        except Exception as e:
            return jsonify({"error": "Failed to retrieve client_id"}), 500

        if conversation_id:
            # Check if conversation_id exists in threads table and matches the channel
            try:
            # Load config file
                # with open(conv_filepath, "r", encoding="utf-8") as f:
                #     config_data = json.load(f)

                s3_config_key = f"{user_id}/messages/{client_id}/config.json"
                config_data = read_json_from_s3(s3_config_key)

                # Check if conversation_id exists and channel matches
                conv_list = config_data.get("conversations", [])
                for conv in conv_list:
                    if conv.get("channel") == channel:
                        is_reply = True
                        conversation_id = conv.get("conv_id")
                        break
                    else:
                        print(f"⚠️ Conversation_id not found or channel mismatch — treating as user-initiated")

            except FileNotFoundError:
                print(f"⚠️ Config file not found at {conv_filepath} — treating as user-initiated")
            except Exception as e:
                print(f"❌ Error checking config file for reply status: {e}")
                
        if not is_reply:
            conversation_id = str(uuid.uuid4())

     # getting email from tables to check which one to use
        cursor.execute("SELECT email FROM users WHERE user_id = %s", (user_id,))
        u_email = cursor.fetchone()
        if  u_email:
            user_email = u_email[0]       

        cursor.execute("SELECT BusinessEmail FROM business_info WHERE user_id_fk = %s", (user_id,))
        b_email = cursor.fetchone()
        if not b_email:
            print("error : No email found from business_info table")
                    # return {"error": "No token found for business email"}, 404
        business_email = b_email[0]       

        try:
            # Choose email based on channel
            selected_email = None
            if match_email_to_channel(user_email, channel):
                selected_email = user_email
            elif match_email_to_channel(business_email, channel):
                selected_email = business_email

        except Exception as e:
            print("🔥 Exception occurred while selecting email by channel:", str(e))

        # getting client email
        cursor.execute("SELECT email_id FROM users_clients WHERE users_clients_id = %s", (client_id,))
        c_email = cursor.fetchone()
        if  c_email:
            client_email = c_email[0]       

        conv_folder = os.path.join(pathconfig.basepath, "messages", user_id, client_id)
        ensure_dir(conv_folder)
        file_name = f"{conversation_id}.json"
        conv_filepath = os.path.join(conv_folder, file_name)

        # Handle subject, ticket info, and thread_id based on message type
        if is_reply:
            
            # Read config file to get ticket info and subject
            config_folder = os.path.join(pathconfig.basepath, "messages", user_id, client_id)
            config_filepath = os.path.join(config_folder, "config.json")
            
            try:
                s3_config_key = f"{user_id}/messages/{client_id}/config.json"
                config_data = read_json_from_s3(s3_config_key)
                
                # Find the conversation in config
                ticket_id = ticket_name = subject = thread_id = None
                for conv in config_data.get("conversations", []):
                    if conv.get("conv_id") == conversation_id:

                        ticket_id = conv.get("ticket_id")
                        ticket_name = conv.get("ticket_name")
                        subject = conv.get("subject")
                        thread_id = conv.get("thread_id") 
                        break                
                    
            except Exception as e:
                return jsonify({"error": "Failed to read conversation config"}), 500
                
        else:
       # Create temporary conversation file for AI processing
            conv_folder = os.path.join(pathconfig.basepath, "messages", user_id, client_id)
            ensure_dir(conv_folder)
            file_name = f"{conversation_id}.json"
            conv_filepath = os.path.join(conv_folder, file_name)
            
            # Create initial message structure
            now_utc = datetime.now(timezone.utc)
            formatted_time = now_utc.isoformat(timespec="seconds")
            msg_id = str(uuid.uuid4())
            
            initial_message = {
                "id": msg_id,
                "from": "user",
                "to": "client",
                "body": text,
                "timestamp": formatted_time,
                "channel": channel,
                "direction": "outbound",
            }
            
            # Save temporary file for subject generation
            with open(conv_filepath, "w", encoding="utf-8") as f:
                json.dump({"new_messages": [initial_message]}, f, indent=2)
            
            # Generate subject using AI
            subjects = generate_subject(user_id, conv_filepath, channel)
            
            # Get subject from AI response
            subject = ticket_name = ticket_id = None
            
            
            for group in subjects:
                if msg_id in group.get("message_ids", []):
                    subject = ticket_name = group.get("summary")
                    break
                    
            if not subject:
                subject = ticket_name = f"Message from {client_email}"  # Fallback subject
                
            # thread_id = None  # New conversation, no thread_id yet
        # assigning ticket_id and ticket_name
        now_utc = datetime.now(timezone.utc)
        formatted_time = now_utc.isoformat(timespec="seconds")
        msg_id = str(uuid.uuid4())

        message = {
            "id": msg_id,
            "from": selected_email,
            "to": client_email,
            "body": text,
            "timestamp": formatted_time,
            "channel": channel,
            "direction": "outbound",
            "subject": subject,
            "status": "pending",
            "source": channel,
            "user_id": user_id,
            "thread_id": thread_id,
            "conversation_id": conversation_id,
            "ticket_id": ticket_id,
            "ticket_name": ticket_name
        }
        # Send the message via appropriate channel
        sent_message_id, sent_thread_id = None, None


        # sending messages
        if channel == "gmail" :
            if thread_id:
                # print(f"[INFO] → Dispatching reply Gmail message to {client_email}")
                try:
                    sent_message_id = gmail_reply(
                        user_id,
                        to=client_email,
                        subject=subject,
                        thread_id=thread_id,
                        body_text=text,
                    )
                    message["id"] = sent_message_id
                except Exception as e:
                    print(f"❌ Gmail send failed: {e}")
                    return jsonify({"error": "Gmail send failed"}), 500

            else:
                # print(f"[INFO] → Dispatching new Gmail message to {client_email}")
                try:
                    sent_message_id, sent_thread_id = send_mail(
                        user_id,
                        to=client_email,
                        subject=subject,
                        body_text=text,
                    )
                    message["id"] = sent_message_id
                    message["thread_id"] = sent_thread_id
                      
                except Exception as e:
                        print(f"❌ Gmail send failed: {e}")
                        return jsonify({"error": "Gmail send failed"}), 500


        elif channel == "zoho":
            try:
                response_payload, status_code = send_zoho_email(
                                                    user_id=user_id,
                                                    to_email=client_email,
                                                    subject=subject,
                                                    body_text=text,
                                                    from_user_email=selected_email,
                                                )

                if status_code in [200, 201]:
                    message_id = response_payload.get("message_id")
                
                else:
                    print(f"Zoho send failed: {response_payload.get('error')}")
                    return jsonify({"error": response_payload.get('error')}), status_code

            except Exception as e:
                print(f"❌ Zoho send failed: {e}")
                return jsonify({"error": "Zoho send failed"}), 500

        else:
            print(f"[WARN] → Unsupported channel: {channel}")
            return jsonify({"error": "Unsupported channel"}), 400
        

        updated_date = datetime.now(timezone.utc).isoformat()
        created_date = updated_date
        try:

            if not is_reply:
                cursor.execute(
                    """
                    INSERT INTO threads (conversation_id, started_at, status, last_message_at)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (conversation_id, created_date, "Open", updated_date)
                )
            else:
                cursor.execute(
                    "UPDATE tickets SET updated_in = %s, status = %s WHERE conversation_id_fk = %s",
                    (updated_date, "In-Progress", conversation_id)
                )
                cursor.execute(
                    "UPDATE threads SET last_message_at = %s WHERE conversation_id = %s",
                    (updated_date, conversation_id)
                )

            cursor.execute(
                """
                INSERT INTO messages (
                    message_id,
                    conversation_id_fk,
                    sender_id,
                    content_ref,
                    message_type,
                    is_summary,
                    created_at,
                    update_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    msg_id,
                    conversation_id,
                    client_id,
                    "ref",
                    "outbound",
                    subject,
                    created_date,
                    updated_date
                )
            )

            connection.commit()

        except Exception as e:
            connection.rollback()
            print(f"❌ Database operation failed — rolled back: {e}")
            return jsonify({"error": "Database operation failed"}), 500

        # ------------------ Update Conversation File ------------------

        conv_folder = os.path.join(pathconfig.basepath, "messages", user_id, client_id)
        ensure_dir(conv_folder)
        file_name = f"{conversation_id}.json"
        conv_filepath = os.path.join(conv_folder, file_name)
        s3_conv_key = f"{user_id}/messages/{client_id}/{conversation_id}.json"

        try:
            try:
                raw_data = read_json_from_s3(s3_conv_key)
                input_data = raw_data.get("input_data", [])
            except Exception as e:
                input_data = []

            input_data.append(message)
            conversation_data = {"input_data": input_data}

            with open(conv_filepath, "w", encoding="utf-8") as f:
                json.dump(conversation_data, f, indent=2)

            upload_any_file(
                conv_filepath,
                user_id,
                type="messages",
                s3_key_C=s3_conv_key,
            )

        except Exception as e:
            print(f"❌ Failed to update conversation file: {e}")
            return jsonify({"error": "Failed to save conversation"}), 500

        # ------------------ Update Config File ------------------

        config_folder = os.path.join(pathconfig.basepath, "messages", user_id, client_id)
        ensure_dir(config_folder)
        config_filepath = os.path.join(config_folder, "config.json")

        try:
            s3_config_key = f"{user_id}/messages/{client_id}/config.json"
            config_data = read_json_from_s3(s3_config_key)
        except Exception as e:
            print(f"[DEBUG] No config file found — creating new: {e}")
            config_data = {"userclients_id": client_id, "conversations": []}


        try:
            if updated_date.endswith('Z'):
                parsed_ts = datetime.fromisoformat(updated_date.replace("Z", "+00:00"))
            else:
                parsed_ts = datetime.fromisoformat(updated_date)
        except Exception as e:
            print(f"[WARN] Could not parse updated_date '{updated_date}': {e}")
            
        updated_entry = {
            "conv_id": conversation_id,
            "ticket_id": ticket_id,
            "ticket_name": ticket_name,
            "subject": subject,
            "channel": channel,
            "thread_id": sent_thread_id if channel =="gmail" else thread_id,
            "updated_date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "parsed_timestamp": parsed_ts.isoformat()
        }

        conversation_exists = False
        for i, conv in enumerate(config_data.get("conversations", [])):
            if conv.get("conv_id") == conversation_id:
                config_data["conversations"][i] = updated_entry
                conversation_exists = True
                break

        if not conversation_exists:
            config_data.setdefault("conversations", []).append(updated_entry)

        config_data["userclients_id"] = client_id
        update_config_file(user_id, client_id, config_data)

        # ------------------ Final Response ------------------

        return jsonify({
            "status": "sent", 
            "id": sent_message_id or msg_id, 
            "channel": channel,
            "conversation_id": conversation_id,
            "is_reply": is_reply
        }), 200

    except Exception as e:
            print(f"❌ Unexpected error: {e}")
            return jsonify({"error": "Internal server error"}), 500

    finally:
            if 'connection' in locals():
                connection.close()
            

            


@twilio_bp.route("/tickets/<user_id>", methods=["GET"])
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

        # query = """
        #     SELECT t.tickets_id, t.priority, t.status, 
        #            t.created_in, t.updated_in, t.conversation_id_fk
        #     FROM tickets t
        #     JOIN communication c ON t.communication_id_fk = c.communication_id
        #     WHERE c.user_id_fk = %s
        #     ORDER BY t.updated_in DESC
        # """
        # cursor.execute(query, (user_id,))


        query = """
            SELECT 
                uc.first_name,
                t.tickets_id,
                t.status,
                t.SLA,
                t.created_in,
                t.updated_in,
                t.conversation_id_fk
            FROM tickets t
            INNER JOIN assigned a ON t.tickets_id = a.ticket_id_fk
            INNER JOIN users_clients uc ON a.users_clients_id_fk = uc.users_clients_id
        """
        cursor.execute(query)
        rows = cursor.fetchall()
            
        tickets = []
        
        for row in rows:
            # ticket_id, priority, status, created_in, updated_in, conversation_id = row

            first_name, ticket_id, status, SLA, created_in, updated_in,conversation_id = row


            key = conv_key_map.get(conversation_id)
            if key:
                        # Get conversation details from JSON file
                    conversation_data = get_conversation_details(key)
                        
                        # Build ticket response
                    ticket_info = {
                            "first_name":first_name,
                            "ticket_id": ticket_id,
                            # "priority": priority,
                            "SLA" : SLA,
                            "status": status,
                            "created_in": created_in.isoformat() if created_in else None,
                            "updated_in": updated_in.isoformat() if updated_in else None,
                            "conversation_id": conversation_id,
                            "conversation": conversation_data.get("body", ""),
                            "channel": conversation_data.get("source", ""),
                            "subject": conversation_data.get("subject", ""),
                            "from": conversation_data.get("from", ""),
                            # "timestamp": conversation_data.get("timestamp", ""),
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
