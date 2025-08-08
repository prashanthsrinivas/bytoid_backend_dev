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


# CONTACTS = {
#     "Riya Gijo": {
#         "name": "Riya Gijo",
#         "channels": {
#             "outlook": "riyatest1231@outlook.com",
#             "gmail": "123002@chintech.ac.in",
#             "whatsapp": "+918289953540",
#             "messages": "+16318597277",
#             # "slack": "U12345678",
#             "slack": "C094N4EDKNC"
#         }
#     },
#     "Kurikyala Mahender Yadav": {
#         "name": "Kurikyala Mahender Yadav",
#         "channels": {
#             "gmail": "mahender@bytoid.ai",
#         }
#     },
#     "You":{
#         "name": "You",
#         "channels": {
#             "outlook": "riya@bytoid.io",
#             "gmail": "riyagijo2@gmail.com",
#             "whatsapp": "+14155238886",
#             # "messages": "+918289953540",
#             # "slack": "C094N4EDKNC"
#             # "slack": "U12345678"}
#     }}
# }


# file: contact_lookup.py


# def find_contact_by_identity(user_id, identity,direction):
#     return ensure_contact_loaded(user_id, identity,direction=direction)


# def build_grouped(user_id):
#     grouped = defaultdict(dict)
#     now = datetime.now(timezone.utc)

#     prefix = f"{user_id}/messages/"

#     file_list = list_all_files(prefix)
#     # print(f"file list: {file_list}")

#     # for file_obj in file_list.values()
#     for file_obj in file_list:
#         key = file_obj["Key"]
#         input_data = read_json_from_s3(key).get("input_data", {})

#         if isinstance(input_data, dict):
#             messages = input_data.values()
#         elif isinstance(input_data, list):
#             messages = input_data
#         else:
#             messages = []

#         for msg in messages:

#             # for msg in input_data.values():
#             # for msg in input_data:

#             # --- Determine participant based on source ---
#             source = msg.get("source")
#             direction = msg.get("direction")
#             participant = None

#             if source == "slack":
#                 # Slack channels (C = channel, G = group/DM)
#                 if msg.get("to", "").startswith(("C", "G")):
#                     participant = msg["to"]
#             elif source == "outlook":
#                 participant = (
#                     msg.get("from_email")
#                     if direction == "inbound"
#                     else msg.get("to_email")
#                 )
#             elif source == "gmail":
#                 address_field = (
#                     msg.get("from") if direction == "inbound" else msg.get("to")
#                 )
#                 participant = parseaddr(address_field)[1] if address_field else None
#             elif source == "whatsapp":
#                 number = msg.get("from") if direction == "inbound" else msg.get("to")
#                 if number:
#                     participant = number.replace("whatsapp:", "")
#             else:
#                 participant = (
#                     msg.get("from") if direction == "inbound" else msg.get("to")
#                 )

#             # --- Skip messages with no identifiable participant ---
#             if not participant:
#                 continue

#             print(f"participant: {participant}")

#             # --- Check if participant is a known contact ---
#             contact = find_contact_by_identity(
#                 user_id, participant, direction=direction
#             )
#             if not contact or contact["id"] == user_id:
#                 continue

#             contact_id = contact["id"]
#             contact_name = contact["name"]
#             channels = contact.get("channels", {})

#             print(f"[MATCH] {participant} → {contact['id'] if contact else 'UNKNOWN'}")

#             # --- Cache contact info ---
#             if user_id not in CONTACTS:
#                 CONTACTS[user_id] = {}

#             if contact_id not in CONTACTS[user_id]:
#                 CONTACTS[user_id][contact_id] = {
#                     "name": contact_name,
#                     "channels": channels,
#                 }

#             # --- Add message to group if not already there ---
#             if msg["id"] not in grouped[contact_id]:
#                 msg["contact_id"] = contact_id
#                 msg["contact_name"] = contact_name
#                 grouped[contact_id][msg["id"]] = msg

#     return grouped


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




# def ensure_contact_loaded(user_id, identity: str,direction):
#     if user_id in IDENTITY_MAP and identity in IDENTITY_MAP[user_id]:
#         print(f"inseide :if user_id in IDENTITY_MAP and identity in IDENTITY_MAP")
#         return IDENTITY_MAP[user_id][identity]

#     contact_data = get_contact_by_identity(user_id, identity,direction=direction)

#     if contact_data:
#         # Already added in get_contact_by_identity
#         return contact_data

#     return None


# send messages to whatsapp through twilio
# @twilio_bp.route('/twilio_webhook/send_whatsapp', methods=['POST'])
def send_whatsapp(from_number, to_number, body, conversation_id, subject):

    print(
        f"Sending WhatsApp message from {from_number} to {to_number} with body: {body}"
    )
    if not to_number or not body:
        print("Missing 'to' or 'body' fields")

    if not to_number.startswith("whatsapp:"):
        print("Recipient number must start with 'whatsapp:'")

    # data = request.get_json()
    # user_number = data.get('to')
    # from_number = data.get('from')
    # body = data.get('body')
    # conversation_sid = data.get("conversation_sid")  # for sending reply in a conversation

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
    # print("Received payload from Twilio: {data}")
    # print(f"WhatsApp message sent: {message.sid} to: {user_number} with body: {body} source:{source} direction:{direction}")
    print(f"messages are: {MESSAGES}")

    return {"sid": message.sid, "status": "sent", "channel": "whatsapp"}

    # except Exception as e:
    #     print(f"Error sending WhatsApp message: {e}")
    #     return "Internal Server Error", 500


# send messages to sms through twilio
# @twilio_bp.route('/twilio_webhook/send-sms', methods=['POST'])
def send_sms(from_number, to_number, body, conversation_id, subject):
    print(
        f"Sending sms message from {from_number} to {to_number} with body: {body} conversation_id :{conversation_id} subject: {subject}"
    )
    if not to_number or not body:
        print("Missing 'to' or 'body' fields")

    if to_number.startswith("whatsapp:"):
        print("Recipient number cannot start with 'whatsapp:'")

    # data = request.get_json()
    # user_number = data.get('to')
    # from_number = data.get('from')
    # body = data.get('body')
    # conversation_sid = data.get("conversation_sid")  # for sending reply in a conversation
    try:

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
    # if sid in MESSAGES:
    #     MESSAGES[sid]["status"] = status
    #     MESSAGES[sid]["last_updated"] = timestamp
    #     print(f"Updated message {sid} status to {status}")
    # else:
    #     # Optional: if you want to store callbacks for messages you didn't send from your app
    #     MESSAGES[sid] = {
    #         "id": str(uuid.uuid4()),
    #         "from": from_name,
    #         "to": to,
    #         "body": None,
    #         "status": status,
    #         "source": "whatsapp",
    #         "direction": "outbound",
    #         "timestamp": timestamp,
    #     }
    #     print(f"Created new record for unknown message {sid}")
    # print(f"Current messages: {MESSAGES}")

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

    # print(f"Message from {sender_name}: number:{from_number}  body : {content}  source:{source} (SID: {message_sid}) at {timestamp}")
    # print(f"Current messages: {MESSAGES}")

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

    # response = VoiceResponse()
    # response.say("Thank you. Your message has been recorded.")
    # response.hangup()

    # return Response(str(response), mimetype='application/xml')
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

        #################   - uncomment this later. create a table in rds to store slack user_id and app_user_id
        # Look up your app user and their "self" Slack user_id
        # conn = connect_to_rds()
        # cursor = conn.cursor()
        # cursor.execute("""
        #     SELECT app_user_id, slack_user_id
        #     FROM slack_accounts
        #     WHERE slack_team_id = %s
        #     LIMIT 1
        # """, (slack_team_id,))
        # row = cursor.fetchone()

        # if not row:
        #     return "Unknown workspace", 403

        # app_user_id, customer_slack_user_id = row

        # # Determine direction
        # if slack_user_id == customer_slack_user_id:
        #     direction = "outbound"
        # else:
        #     direction = "inbound"

        ##############

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

    new_messages = []

    for client_id, channel_data in grouped_messages.items():
        print(f"\n🔍 Processing client_id: {client_id}")
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
        print(f"📧 Client email: {client_email}")

        config_folder = os.path.join(pathconfig.basepath, "messages", user_id, client_id)
        ensure_dir(config_folder)
        print(f"📁 Ensured config folder exists: {config_folder}")

        config_filepath = os.path.join(config_folder, "config.json")
        try:
            with open(config_filepath, "r", encoding="utf-8") as f:
                config_data = json.load(f)
            print("✅ Loaded config.json successfully")
        except FileNotFoundError:
            config_data = {}
            print("⚠️ Config file not found")

        existing_channels = {
            convo.get("channel")
            for convo in config_data.get("conversations", [])
            if convo.get("channel")
        }
        print(f"📡 Existing channels in config: {existing_channels}")

        for channel, channel_msgs in channel_data.items():
            print(f"\n📨 Processing channel: {channel} with {len(channel_msgs)} messages")
            channel_msgs.sort(key=lambda x: x.get("timestamp", ""))  # optional
            latest_msg = channel_msgs[-1] if channel_msgs else None

            output_filename = f"{channel}_new_messages.json"
            output_path = os.path.join(config_folder, output_filename)
            print(f"📂 Output file path: {output_path}")

            new_msg_data = {}
            try:
                with open(output_path, "r", encoding="utf-8") as f:
                    new_msg_data = json.load(f)
                print("📖 Opened existing new_messages file")
            except Exception as e:
                print(f"⚠️ Couldn't read existing messages: {e}")

            existing_new_msg = {
                msg.get("msg_id"): msg for msg in new_msg_data.get("new_messages", [])
            }
            merged_messages = new_msg_data.get("new_messages", [])

            for m in channel_msgs:
                msg_id = m.get("id")
                if msg_id in existing_new_msg:
                    print(f"🔄 Message {msg_id} already exists — skipping")
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
                print(f"➕ Added new message {msg_id}")

            with open(output_path, "w", encoding="utf-8") as f:
                json.dump({"new_messages": merged_messages}, f, indent=2)
            print(f"💾 Saved updated messages to {output_filename}")

            print(f"🔧 Generating subjects for channel: {channel}")
            print(f"existing_new_msg sent to ai: {existing_new_msg}")
            subjects = generate_subject(user_id, output_path, channel)

            print(f"🧩 Appending subjects to messages for channel: {channel}")
            grouped_messages = append_subject_to_messages(grouped_messages,channel,subjects,user_id,existing_new_msg)

            # with open(user_filepath, "w", encoding="utf-8") as f:
            #     json.dump(grouped_messages, f, indent=2)

        # upload_any_file(
        #             file_path=user_filepath, user_id=user_id, type="messages", file_name=filename
        #         )
        
        print(f"📝 Injected subject metadata into user message file: {filename}")
        print(f"📦 Stored {len(merged_messages)} unique messages in {output_filename} for client_id: {client_id}")

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
            print(f"🔍 Filtered {len(filtered_messages)} messages for channel: {channel}")
        else:
            filtered_messages = all_messages
            print(f"📦 Processing all {len(filtered_messages)} messages (no channel filter)")

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
        # message_payload = json.dumps(data.get("new_messages", []), indent=2)
        message_payload = json.dumps(filtered_messages, indent=2)
        full_prompt = update_prompt_template.replace("{full_text_message_body}", message_payload)
        print(f"📨 Full prompt:\n{full_prompt}")

        # Generate YAML output from model
        modified_yaml = get_fireworks_response(full_prompt, role="system")
        print(f"🤖 Raw output for channel {channel}: {modified_yaml}")


        try:
            parsed_yaml = yaml.safe_load(modified_yaml.strip())
            if "subject_groups" in parsed_yaml:
                print("✅ Subject groups successfully extracted")
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

            print("✅ Subject groups successfully extracted")
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
    print(f"subject map is :{subject_map}")

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
            # with open(config_filepath, "r", encoding="utf-8") as f:
            #     config_data = json.load(f)
            config_data = read_json_from_s3(s3_config_key)

        except FileNotFoundError:
            config_data = {}
            print("no config file")

        # for channel, messages in channels.items():
        messages = grouped_messages.get(client_id, {}).get(channel, [])
        print(f"[INFO] Processing {len(messages)} messages for channel: {channel}")

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
                        
                        print(f"***msg is : {msg}")
                        msg_id = msg.get("id")

                        if msg_id in existing_new_msg:
                            print(f"🔄 Message {msg_id} already exists — skipping")
                            continue
                        subject = msg.get("subject", "") 
                        print(f"******subject of msg : {subject}")                   
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
                                        print(f"🔁 Updated existing conversation entry in config")
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

                                                        print(f"🔁 Updated existing conversation entry in config")  
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

                        print(f"inserted new row into threads table - msg_id: {msg_id}")

                        # 3. new for inbound msg
                        if direction == "inbound":
                            print("direction is inbound. so new id and name")
                            new_ticket_id = str(uuid.uuid4())
                            
                            print(f"🔍 Looking up subject for msg_id: {repr(msg_id)}")
                            print(f"🎯 Subject map keys: {list(subject_map.keys())}")

                            ticket_name = subject_map.get(str(msg_id))
                            subject = msg.get("subject")
                            msg["ticket_id"] = new_ticket_id
                            msg["ticket_name"] = ticket_name
                            msg["conversation_id"] = new_conversation_id
                            

                            cursor.execute(
                                "INSERT INTO tickets (tickets_id, ticket_name, conversation_id_fk,status,priority) VALUES (%s, %s, %s,%s,%s)",
                                (new_ticket_id, ticket_name, new_conversation_id,"Open","Medium")
                            )
                            print(f"inserted new row into tickets table - msg_id: {msg_id}")

                            cursor.execute(
                                """
                                UPDATE threads
                                SET ticket_id_fk = %s
                                WHERE conversation_id = %s
                                """,
                                (new_ticket_id, new_conversation_id)
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
                            
                        print("direction is oubound. so no id and name")

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


# def update_config_file(user_id, client_id, config_data,process, conv_id=None, updated_date=None ):

#     config_folder = os.path.join(
#                             pathconfig.basepath, "messages", user_id, client_id
#                         )
#     ensure_dir(config_folder)
#     config_filepath = os.path.join(config_folder, "config.json")
    

#     s3_config_key = f"{user_id}/messages/{client_id}/config.json"

#     s3_data = read_json_from_s3(s3_config_key)
#     input_data = s3_data.get("conversations", [])

#     if process == "append":
#         input_data.extend(config_data)
#         with open(config_filepath, "w", encoding="utf-8") as f:
#             json.dump({"userclients_id": client_id, "conversations": input_data}, f, indent=2)
#     else:
#         for conv in input_data:
#             if conv.get("conv_id") == conv_id:
#                 conv["updated_date"] = updated_date


#     upload_any_file(
#                         config_filepath,
#                         user_id,
#                         type="messages",
#                         s3_key_C=s3_config_key,
#                                 )





def update_or_create_conversation_file(grouped_messages, user_id, client_id, channel):
    prefix = f"{user_id}/messages/{client_id}"
    config_key = f"{prefix}/config.json"
    print(f"[DEBUG] Reading config from: {config_key}")

    config_data = read_json_from_s3(config_key)
    print(f"[DEBUG] Config data loaded: {config_data}")

    # Pull existing conversation IDs for the specified channel
    existing_conversations = {
        conv["conv_id"] for conv in config_data.get("conversations", [])
        if conv.get("channel") == channel and conv.get("conv_id")
    }
    print(f"[DEBUG] Existing conv_ids for channel={channel}: {existing_conversations}")

    channel_messages = grouped_messages.get(client_id, {}).get(channel, [])
    print(f"[DEBUG] Retrieved {len(channel_messages)} messages for client={client_id}, channel={channel}")

    if not channel_messages:
        print(f"[INFO] No messages found for client={client_id}, channel={channel}")
        return

    # Group messages by conversation_id
    conversation_groups = {}
    for msg in channel_messages:
        conv_id = msg.get("conversation_id")
        if conv_id:
            conversation_groups.setdefault(conv_id, []).append(msg)
    print(f"[DEBUG] Grouped messages by conversation_id: {list(conversation_groups.keys())}")

    for conv_id, messages in conversation_groups.items():
        file_key = f"{prefix}/{conv_id}.json"
        print(f"[DEBUG] Processing conv_id={conv_id} with {len(messages)} messages")

        conv_folder = os.path.join(
                            pathconfig.basepath, "messages", user_id, client_id
                        )
        ensure_dir(conv_folder)
        conv_file_name = f"{conv_id}.json"
        conv_filepath = os.path.join(conv_folder, conv_file_name)
        s3_config_key = f"{user_id}/messages/{client_id}/{conv_id}.json"


        if conv_id in existing_conversations:
            print(f"[INFO] Appending to existing file: {file_key}")
            raw_data = read_json_from_s3(file_key)
            # print(f"[DEBUG] Existing file data loaded with {len(raw_data.get('input_data', []))} messages")

            input_data = raw_data.get("input_data", [])
            # input_data = raw_data
            input_data.extend(messages)

            # with open(conv_filepath, "w", encoding="utf-8") as f:
            #                     json.dump(input_data, f, indent=2)

            with open(conv_filepath, "w", encoding="utf-8") as f:
                json.dump({"input_data": input_data}, f, indent=2)


            print(f"*************✅ Config file created at {conv_filepath} in zoho")
            upload_any_file(
                                    conv_filepath,
                                    user_id,
                                    type="messages",
                                    s3_key_C=s3_config_key,
                                ) 
            print(f"[DEBUG] Uploaded updated message list to: {file_key}")
        else:

            print(f"[INFO] Creating new file: {file_key}")

            # with open(conv_filepath, "w", encoding="utf-8") as f:
            #                     json.dump(messages, f, indent=2)

            with open(conv_filepath, "w", encoding="utf-8") as f:
                json.dump({"input_data": messages}, f, indent=2)

            upload_any_file(
                                    conv_filepath,
                                    user_id,
                                    type="messages",
                                    s3_key_C=s3_config_key,
                                )            
            print(f"[DEBUG] Uploaded new message list to: {file_key}")
            # Optionally update config here, if needed



# @twilio_bp.route("/conversations/<user_id>", methods=["GET"])
# def get_recent_inbound_conversations(user_id):
#     grouped = build_grouped(user_id=user_id)
#     now = datetime.now(timezone.utc)

#     conversations = []

#     for contact_id, msg_dict in grouped.items():
#         messages = list(msg_dict.values())

#         # ✅ Sort messages from oldest to newest (for processing)
#         messages.sort(key=lambda x: x["timestamp"])

#         # ✅ Skip if only outbound and more than 1 message
#         has_inbound = any(m["direction"] == "inbound" for m in messages)
#         has_outbound = any(m["direction"] == "outbound" for m in messages)
#         # if not has_inbound and len(messages) > 5:
#         #     print(f"[SKIPPED] → {contact_id} (Only outbound, more than 1 message)")
#         #     continue

#         # ✅ Sort again to pick the latest message
#         messages.sort(
#             key=lambda x: isoparse(x["timestamp"]).astimezone(timezone.utc),
#             reverse=True,
#         )
#         last = messages[0]

#         # ✅ Get contact info
#         user_contacts = CONTACTS.get(user_id, {})

#         # ⚠️ Skip if no inbound messages and contact not in CONTACTS
#         if not has_outbound and contact_id not in user_contacts:
#             print(f"[SKIPPED] → {contact_id} (Inbound only and not in CONTACTS)")
#             continue

#         # ⚠️ Skip inbound messages if sender is not in CONTACTS
#         if has_inbound and contact_id not in user_contacts:
#             print(f"[SKIPPED] → {contact_id} (Inbound but unknown contact)")
#             continue

#         contact_data = user_contacts.get(contact_id, {})
#         contact_name = last.get("contact_name") or contact_data.get("name", "Unknown")

#         # 🕒 Relative time
#         message_time = datetime.fromisoformat(last["timestamp"].replace("Z", "+00:00"))
#         delta = now - message_time
#         relative_time = humanize.naturaltime(delta)

#         conversations.append(
#             {
#                 "contact_id": contact_id,
#                 "name": contact_name,
#                 "lastMessage": last["body"][:100],
#                 "timestamp": relative_time,
#                 "isoTimestamp": last["timestamp"],
#                 "unread": any(m["status"] == "received" for m in messages),
#                 "channel": last.get("source"),
#                 "subject": last.get("subject"),
#             }
#         )

#         print(f"\n[DEBUG] → Conversation with {contact_id}")
#         for m in messages:
#             print(f"{m['timestamp']} | {m['direction']} | {m['body'][:50]}")
#         print(
#             f"→ Last Message Picked: {last['timestamp']} | {last['direction']} | {last['body'][:50]}"
#         )

#     conversations.sort(
#         key=lambda x: datetime.fromisoformat(x["isoTimestamp"].replace("Z", "+00:00")),
#         reverse=True,
#     )

#     print(f"[DONE] → Returning {len(conversations)} grouped conversations by contact")
#     return jsonify(conversations)


def get_latest_convo_info(config):
    """
    Get the latest conversation from a config file based on parsed_timestamp
    """
    print("[DEBUG] Entered get_latest_convo_info()")

    if not config or "conversations" not in config:
        print("[DEBUG] Missing config or conversations key")
        return None

    config_data = config.get("conversations", [])
    print(f"[DEBUG] Found {len(config_data)} config_data in input_data")
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
                print(f"[DEBUG] Updated latest timestamp for conv_id={conv_id} → {msg_ts.isoformat()}")
        except Exception as e:
            print(f"[WARN] Failed to parse parsed_timestamp '{ts_str}': {e}")
            continue

    if not conversations:
        print("[DEBUG] No valid conversations found after grouping")
        return None

    latest_conv = max(conversations.values(), key=lambda x: x["parsed_timestamp"])
    latest_msg = latest_conv["message"]

    print(f"[DEBUG] Selected latest conversation → conv_id={latest_msg.get('conv_id')} at {latest_conv['parsed_timestamp'].isoformat()}")

    return {
        "updated_date": latest_conv["parsed_timestamp"].isoformat(),
        "conv_id": latest_msg.get("conv_id"),
        "client_id": client_id,  
    }


def extract_unique_client_folders(file_list, base_prefix):
    client_folders = set()
    for obj in file_list:
        key = obj["Key"]
        if key.startswith(base_prefix):
            rest = key[len(base_prefix):]
            parts = rest.split("/")
            if len(parts) > 1:
                client_folders.add(parts[0])  # First subfolder after prefix
    return sorted(client_folders)


@twilio_bp.route("/conversations/<user_id>", methods=["GET"])
def get_latest_conversations(user_id):
    """
    Get the latest conversation from each client's config file and load full conversation data
    """
    print(f"[DEBUG] Entered get_latest_conversations() for user_id : {user_id}")
    client_prefix = f"{user_id}/messages/"

    raw_file_list = list_all_files(client_prefix)
    client_ids = extract_unique_client_folders(raw_file_list, client_prefix)
    print(f"[DEBUG] Extracted client folders: {client_ids}")

    print(f"[DEBUG] Found {len(client_ids)} folders under {client_prefix}")

    conversations = []
    disp_messages = []
    
    for client_id in client_ids:
        print(f"\n[PROCESSING CLIENT] {client_id}")
        config_path = f"{client_prefix}{client_id}/config.json"
        print(f"[DEBUG] Reading config from path: {config_path}")
        
        try:
            config = read_json_from_s3(config_path)
            print("[DEBUG] Successfully read config")
            recent_msg = get_latest_convo_info(config)
            
            if recent_msg:
                conversations.append(recent_msg)
                print(f"  [SUCCESS] Latest conversation: {recent_msg['conv_id']} at {recent_msg['updated_date']}")
                
                conv_id = recent_msg['conv_id']
                convo_path = f"{client_prefix}{client_id}/{conv_id}.json"
                print(f"[DEBUG] Loading full conversation from path: {convo_path}")
                
                try:
                    convo_data = read_json_from_s3(convo_path)
                    print("[DEBUG] Successfully read full conversation")
                    convo_messages = convo_data.get("input_data", [])
                    print(f"[DEBUG] Found {len(convo_messages)} messages in conversation file")

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
                                    print(f"[DEBUG] New latest timestamp for conv_id={conv_id}: {msg_ts.isoformat()}")
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
                            # print(f"  [SUCCESS] Added display message for conversation {conv_id} : {disp_message}")
                    
                except Exception as e:
                    print(f"  [WARN] Failed to read conversation {conv_id}: {e}")
            else:
                print(f"  [INFO] No conversations found in config for client: {client_id}")
                
        except Exception as e:
            print(f"  [WARN] Skipping config for client {client_id}: {e}")
            continue
    
    disp_messages.sort(key=lambda x: x['isoTimestamp'], reverse=True)

    
    print(f"\n[SUMMARY] Found {len(disp_messages)} latest conversations across all clients")
    # print(f"latest message to be displayed is: {disp_messages}")
    
    return disp_messages


def get_conv_order(config):
    """
    Return sorted list of conv_ids based on parsed_timestamp
    """
    print("[DEBUG] Entered get_conv_order()")

    if not config or "conversations" not in config:
        print("[DEBUG] Missing config or conversations key")
        return None

    config_data = config.get("conversations", [])
    print(f"[DEBUG] Found {len(config_data)} config_data in input_data")

    if not config_data:
        print("[DEBUG] input_data is empty")
        return None

    conversations = []

    for msg in config_data:
        conv_id = msg.get("conv_id")
        ts_str = msg.get("parsed_timestamp")

        if not conv_id or not ts_str:
            print(f"[DEBUG] Skipping message due to missing conv_id or parsed_timestamp")
            continue

        try:
            msg_ts = datetime.fromisoformat(ts_str)
            conversations.append((conv_id, msg_ts))
            print(f"[DEBUG] Added conv_id={conv_id} with timestamp={msg_ts.isoformat()}")
        except Exception as e:
            print(f"[WARN] Failed to parse parsed_timestamp '{ts_str}': {e}")
            continue

    if not conversations:
        print("[DEBUG] No valid conversations found after timestamp parsing")
        return None

    sorted_convos = sorted(conversations, key=lambda x: x[1], reverse=True)
    sorted_ids = [conv_id for conv_id, _ in sorted_convos]

    print(f"[DEBUG] Final sorted conv_ids: {sorted_ids}")
    return sorted_ids


    # latest_conv = max(conversations.values(), key=lambda x: x["parsed_timestamp"])
    # latest_msg = latest_conv["message"]

    # print(f"[DEBUG] Selected latest conversation → conv_id={latest_msg.get('conv_id')} at {latest_conv['parsed_timestamp'].isoformat()}")

    # return {
    #     "updated_date": latest_conv["parsed_timestamp"].isoformat(),
    #     "conv_id": latest_msg.get("conv_id"),
    #     "client_id": client_id,  
    # }


@twilio_bp.route("/conversations/<conversation_id>/<user_id>", methods=["GET"])
def get_selected_conv(conversation_id, user_id):
    print(f"[DEBUG] Entered get_selected_conv() for a : {conversation_id}, b :{user_id}")

    try:
        connection = connect_to_rds()
        if connection is None:
            print("⚠️ DB connection failed inside get_selected_conv()")
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
            return jsonify({"error": "Failed to retrieve client_id"}), 500

        config_path = f"{user_id}/messages/{client_id}/config.json"
        try:
            config = read_json_from_s3(config_path)
            print("[DEBUG] Successfully read config")
        except Exception as e:
            print(f"❌ Failed to read config from S3: {config_path} — {e}")
            return jsonify({"error": "Failed to load config"}), 500

        try:
            recent_msg = get_conv_order(config)
            print(f"[DEBUG] Extracted {len(recent_msg)} ordered conversation IDs : recent_msg")
        except Exception as e:
            print(f"❌ Failed to get conversation order from config: {e}")
            return jsonify({"error": "Invalid config format"}), 500

        messages = []
        for conv in recent_msg:
            try:
                convo_path = f"{user_id}/messages/{client_id}/{conv}.json"
                print(f"[DEBUG] Loading full conversation from path: {convo_path}")
                convo_data = read_json_from_s3(convo_path)
                print("[DEBUG] Successfully read full conversation")
                convo_messages = convo_data.get("input_data", [])
                print(f"[DEBUG] Found {len(convo_messages)} messages in conversation file")

                # messages.extend(convo_messages)
                channel = convo_messages[0].get("source") if convo_messages else "unknown"
                messages.append({
                    "id": conv,
                    "channel": channel,
                    "messages": convo_messages
                })

            except Exception as e:
                print(f"❌ Failed to read or parse {convo_path}: {e}")
                continue  # Skip to next conversation
        # print(f"********messages are: {messages}")
        return jsonify(messages)

    except Exception as e:
        print(f"❌ Unexpected error in get_selected_conv(): {e}")
        return jsonify({"error": "Internal server error"}), 500


    



# @twilio_bp.route("/conversations/<conversation_id>/<user_id>", methods=["GET"])
# def get_conversation(conversation_id, user_id):
#     print(f"[INFO] → Entered /conversations/{conversation_id}")

#     grouped = build_grouped(user_id=user_id)
#     grouped_by_conv_id = defaultdict(list)

#     print(f"[DEBUG] → Keys in grouped: {list(grouped.keys())}")
#     seen_msg_ids = set()

#     for contact_id, msg_dict in grouped.items():
#         for m in msg_dict.values():
#             msg_id = m.get("id")
#             if not msg_id or msg_id in seen_msg_ids:
#                 continue
#             seen_msg_ids.add(msg_id)

#             conv_id = m.get("conversation_id")
#             if conv_id:
#                 grouped_by_conv_id[conv_id].append(m)

#     print(
#         f"[DEBUG] → Grouped by conversation ID keys: {list(grouped_by_conv_id.keys())}"
#     )

#     # ✅ Step 1: Lookup contact from CONTACTS
#     user_contacts = CONTACTS.get(user_id, {})
#     contact = None
#     for cid, c in user_contacts.items():
#         all_channels = list(c.get("channels", {}).values())
#         if conversation_id == cid or conversation_id in all_channels:
#             contact = c
#             conversation_id = cid  # normalize the ID
#             break

#     # Optional fallback using IDENTITY_MAP (if you still keep that structure)
#     if not contact:
#         contact = IDENTITY_MAP.get(user_id, {}).get(conversation_id)

#     new_name = contact["name"] if contact else "Unknown"

#     if contact:
#         identities = set(contact.get("channels", {}).values())
#         identities.add(conversation_id)
#         print(f"[DEBUG] → Found contact: {contact}")
#     else:
#         identities = {conversation_id}
#         print(f"[WARN] → No contact found for id: {conversation_id}")

#     print(f"[DEBUG] → Resolved identities: {identities}")

#     selected_threads = []
#     now = datetime.now(timezone.utc)

#     for conv_id, msgs in grouped_by_conv_id.items():
#         _, parsed_email = parseaddr(conv_id)
#         print(f"[LOOP] → Checking conv_id={conv_id}, parsed_email={parsed_email}")

#         if conv_id in identities or parsed_email in identities:
#             print(f"[MATCH] → conv_id matched identities → {conv_id}")
#             msgs.sort(key=lambda m: m["timestamp"])
#             last = msgs[-1]

#             name = new_name

#             subject = last.get("subject")
#             from_raw = last.get("from_email") or last.get("from") or ""
#             _, raw_email = parseaddr(from_raw)
#             from_addr = raw_email or from_raw
#             channel = last.get("source")

#             print(
#                 f"[INFO] → Thread Info — name: {name}, id: {conv_id}, channel: {channel}"
#             )

#             thread = {
#                 "id": conv_id,
#                 "name": name,
#                 "channel": channel,
#                 "from_addr": from_addr,
#                 "messages": [],
#                 "subject": subject,
#             }

#             for m in msgs:
#                 msg_time = datetime.fromisoformat(m["timestamp"].replace("Z", "+00:00"))
#                 delta = now - msg_time
#                 relative_time = humanize.naturaltime(delta)

#                 thread["messages"].append(
#                     {
#                         "id": m["id"],
#                         "subject": m["subject"],
#                         "text": m["body"],
#                         "senderName": (
#                             new_name if m["direction"] == "inbound" else "You"
#                         ),
#                         "timestamp": relative_time,
#                         "isoTimestamp": m["timestamp"],
#                         "channel": m["source"],
#                     }
#                 )

#             selected_threads.append(thread)
#             print(
#                 f"[INFO] → Thread created for ID: {conv_id} with {len(thread['messages'])} messages"
#             )

#     selected_threads.sort(
#         key=lambda t: max(
#             datetime.fromisoformat(m["isoTimestamp"].replace("Z", "+00:00"))
#             for m in t["messages"]
#         ),
#         reverse=True,
#     )

#     print(f"[DONE] → Returning {len(selected_threads)} threads")
#     return jsonify(selected_threads)


@twilio_bp.route("/start-conversation", methods=["POST"])
def start_conversation():
    """
    Starts a conversation (returns contact identity list and contact_id as conversationId).
    Input: {
        "user_id": "...",
        "contact_id": "users_clients_id"
    }
    Output: {
        "identities": ["...", "..."],
        "status": "existing" | "new",
        "conversationId": contact_id
    }
    """
    try:
        print("[INFO] → /start-conversation triggered")

        data = request.get_json() or {}
        user_id = data.get("user_id")
        contact_id = data.get("contact_id")

        print(f"[DEBUG] → Received user_id: {user_id}, contact_id: {contact_id}")

        if not user_id or not contact_id:
            print("[ERROR] → Missing user_id or contact_id")
            return jsonify({"error": "Missing user_id or contact_id"}), 400

        connection = connect_to_rds()
        cursor = connection.cursor()

        print("[INFO] → Fetching contact channels from DB")
        cursor.execute(
            """
            SELECT email_id, phone_number, whatsapp_number, slack_id
            FROM users_clients
            WHERE users_clients_id = %s
        """,
            (contact_id,),
        )
        row = cursor.fetchone()

        if not row:
            print(f"[ERROR] → Contact not found for ID: {contact_id}")
            return jsonify({"error": "Contact not found"}), 404

        email, phone, whatsapp, slack = row
        identities = [i for i in [email, phone, whatsapp, slack] if i]
        print(f"[DEBUG] → Contact identities: {identities}")

        print("[INFO] → Building grouped messages")
        grouped = build_grouped(user_id=user_id)
        messages = grouped.get(contact_id, {}).values()
        print(f"[DEBUG] → Total messages for contact_id {contact_id}: {len(messages)}")

        matched = False
        for msg in messages:
            from_raw = parseaddr(msg.get("from") or "")[1]
            from_email_raw = parseaddr(msg.get("from_email") or "")[1]
            to_raw = parseaddr(msg.get("to") or "")[1]
            to_email_raw = parseaddr(msg.get("to_email") or "")[1]

            print(
                f"[TRACE] → Checking message {msg.get('id')} from: {from_raw or from_email_raw}, to: {to_raw or to_email_raw}"
            )

            if any(
                identity in [from_raw, from_email_raw, to_raw, to_email_raw]
                for identity in identities
            ):
                matched = True
                print(f"[MATCH] → Message matched for contact_id: {contact_id}")
                break

        status = "existing" if matched else "new"
        print(f"[INFO] → Returning status '{status}' for contact_id: {contact_id}")

        return jsonify(
            {"identities": identities, "status": status, "conversationId": contact_id}
        )

    except Exception as e:
        print("[EXCEPTION] → An error occurred:")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# @twilio_bp.route("/send-reply", methods=["POST"])
# def send_reply():
#     print("[INFO] → Received request to send reply")
#     data = request.json
#     print(f"[DEBUG] → Payload: {data}")

#     user_id = data.get("user_id")
#     # user_id='60044116526.100657136'
#     channel = data.get("channel")
#     text = data.get("text")
#     conversation_id = data.get("conversation_id")
#     contact_id = data.get("contact_id")
#     print(f"user id is:{user_id}")
#     # contact = ensure_contact_loaded(contact_id)
#     # ensure_contact_loaded("You")

#     contact = ensure_contact_loaded(user_id, contact_id, direction="outbound")
#     ensure_contact_loaded(user_id, user_id, direction="outbound")
#     if not conversation_id:

#         print(f"contact found is:{contact}")
#         if contact:
#             if channel in ["gmail", "outlook", "zoho"]:
#                 conversation_id = contact["channels"].get("email")
#             # elif channel == "zoho":
#             #     conversation_id = contact["channels"].get("zoho")
#             elif channel == "whatsapp":
#                 conversation_id = contact["channels"].get("whatsapp")
#             elif channel == "messages":
#                 conversation_id = contact["channels"].get("messages")
#             elif channel == "slack":
#                 conversation_id = contact["channels"].get("slack")
#             elif channel == "slackworkspace":
#                 conversation_id = contact["channels"].get("slackworkspace")

#         # fallback if still none
#         # if not conversation_id:
#         #     conversation_id = f"new:{channel}:{contact_id}"

#     print(f"[INFO] → Generated conversation_id: {conversation_id}")
#     print(
#         f"[DEBUG] → user_id: {user_id}, channel: {channel}, text: {text}, conversation_id: {conversation_id}"
#     )

#     if not channel or not text or not conversation_id:
#         print("[ERROR] → Missing required fields")
#         return jsonify({"error": "Missing required fields"}), 400

#     grouped = build_grouped(user_id=user_id)

#     msg_dict = grouped.get(conversation_id)

#     # fallback: loop through keys to find matching identity
#     if not msg_dict:
#         print(
#             "[WARN] → conversation_id not found directly in grouped keys, checking inside identities"
#         )
#         for cid, messages in grouped.items():
#             for m in messages.values():
#                 identities = {
#                     m.get("conversation_id"),
#                     m.get("from"),
#                     m.get("to"),
#                     m.get("from_email"),
#                     m.get("to_email"),
#                 }
#                 if conversation_id in identities:
#                     print(
#                         f"[MATCH] → Found fallback match for conversation_id '{conversation_id}' in contact '{cid}'"
#                     )
#                     msg_dict = messages
#                     break
#             if msg_dict:
#                 break

#     if not msg_dict:
#         print(
#             f"[ERROR] → No messages found for conversation_id or fallback: {conversation_id}"
#         )
#         msg_dict = {}

#     relevant_messages = list(msg_dict.values())

#     print(
#         f"[DEBUG] → Found {len(relevant_messages)} messages for conversation_id: {conversation_id}"
#     )

#     messages_for_channel = [m for m in relevant_messages if m["source"] == channel]
#     original_message = (
#         sorted(messages_for_channel, key=lambda m: m["timestamp"], reverse=True)[0]
#         if messages_for_channel
#         else None
#     )

#     if original_message:
#         print(f"[DEBUG] → Selected original message: {original_message}")
#     else:
#         print(
#             "[WARN] → No previous message found for this channel; will construct fresh reply"
#         )
#     print(f"user_id before calling contacts.get :{user_id}")
#     my_contact = ensure_contact_loaded(user_id, user_id, direction="outbound")
#     print(f"my_contact is:{my_contact}")
#     from_user_email = None

#     if my_contact:
#         if channel in ["gmail", "outlook", "zoho"]:
#             from_user_email = my_contact["channels"].get("email")
#         elif channel == "whatsapp" and from_user_email:
#             from_user_email = my_contact["channels"].get(channel)
#             from_user_email = f"whatsapp:{from_user_email}"
#         else:
#             from_user_email = my_contact["channels"].get(channel)

#         print(f"[DEBUG] → From user email: {from_user_email} for channel {channel}")

#     if not from_user_email:
#         print("[WARN] → No from_user_email found for this channel")

#     contact = ensure_contact_loaded(user_id, contact_id, direction="outbound")
#     print(f"***contact is {contact}")

#     recipient_email = None
#     recipient_phone = None
#     slack_channel_id = None

#     if contact:
#         if channel in ["gmail", "outlook", "zoho"]:
#             recipient_email = contact["channels"].get("email")
#             print(f"[DEBUG] → Recipient email: {recipient_email}")
#         # elif channel == "zoho":
#         #     recipient_email = contact["channels"].get("zoho")
#         elif channel == "whatsapp":
#             whatsapp_value = contact["channels"].get("whatsapp")
#             recipient_phone = f"whatsapp:{whatsapp_value}" if whatsapp_value else None
#             print(f"[DEBUG] → Recipient phone: {recipient_phone}")
#         elif channel == "messages":
#             recipient_phone = contact["channels"].get("messages")
#             print(f"[DEBUG] → Recipient phone: {recipient_phone}")
#         elif channel == "slackworkspace":
#             slack_channel_id = contact["channels"].get("slackworkspace")
#             print(f"[DEBUG] → Slack workspace: {slack_channel_id}")
#         elif channel == "slack":
#             slack_channel_id = contact["channels"].get("slack")
#             print(f"[DEBUG] → Slack DM: {slack_channel_id}")

#     if not recipient_email and original_message:
#         recipient_email = (
#             original_message.get("from_email") or original_message.get("from")
#             if original_message.get("direction") == "inbound"
#             else original_message.get("to_email") or original_message.get("to")
#         )

#     if not recipient_phone and original_message:
#         recipient_phone = (
#             original_message.get("from")
#             if original_message.get("direction") == "inbound"
#             else original_message.get("to")
#         )

#     if not slack_channel_id and original_message:
#         slack_channel_id = original_message.get("to")

#     # Determine correct conversation ID reuse logic
#     conv_id = None
#     for msg in relevant_messages:
#         if msg["source"] == channel:
#             conv_id = msg.get("conversation_id") or msg.get("id")
#             print(
#                 f"[INFO] → Reusing existing conversation_id: {conv_id} for channel: {channel}"
#             )
#             break

#     if not conv_id:
#         if channel in ["whatsapp", "messages"]:
#             conv_id = recipient_phone or f"new:{channel}:{conversation_id}"
#         elif channel in ["gmail", "outlook", "zoho"]:
#             conv_id = recipient_email or f"new:{channel}:{conversation_id}"
#         elif channel in ["slack", "slackworkspace"]:
#             conv_id = slack_channel_id or f"new:{channel}:{conversation_id}"
#         else:
#             conv_id = f"new:{channel}:{conversation_id}"
#         print(f"[INFO] → Created new conversation_id: {conv_id}")

#     try:
#         if channel == "whatsapp":
#             print(f"[INFO] → Sending WhatsApp to {recipient_phone} | body: {text}")
#             sid = send_whatsapp(
#                 from_number=from_user_email,
#                 to_number=recipient_phone,
#                 body=text,
#                 conversation_id=conv_id,
#                 subject="Reply",
#             )
#             return jsonify({"status": "sent", "sid": sid, "channel": "whatsapp"})

#         elif channel == "messages":
#             print(f"[INFO] → Sending SMS to {recipient_phone} | body: {text}")
#             sid = send_sms(
#                 from_number=from_user_email,
#                 to_number=recipient_phone,
#                 body=text,
#                 conversation_id=conv_id,
#                 subject="Reply",
#             )
#             return jsonify({"status": "sent", "sid": sid, "channel": "messages"})

#         elif channel == "gmail":
#             print(f"[INFO] → Sending Gmail to {recipient_email}")

#             if user_id:
#                 if original_message and original_message["source"] == "gmail":
#                     to_address = (
#                         recipient_email
#                         if original_message.get("from") == from_user_email
#                         else original_message.get("from")
#                     )
#                     sent = gmail_reply(
#                         user_id,
#                         conv_id,
#                         to=to_address,
#                         subject=original_message.get("subject", "No subject"),
#                         thread_id=original_message.get("thread_id"),
#                         in_reply_to=original_message.get("message_id"),
#                         body_text=text,
#                     )
#                 else:

#                     sent = send_mail(
#                         user_id,
#                         conv_id,
#                         to=recipient_email,
#                         subject="Reply",
#                         body_text=text,
#                     )
#                 sent_id = sent[0] if isinstance(sent, tuple) else sent.get("id")
#                 return jsonify({"status": "sent", "id": sent_id, "channel": "gmail"})
#             else:
#                 print("[WARN] → Missing user_id for Gmail send")

#         elif channel == "zoho":
#             print(f"usr_id :{user_id}")
#             print(f"[INFO] → Sending zoho email to {recipient_email}")
#             sent = send_zoho_email(
#                 user_id=user_id,
#                 to_email=recipient_email,
#                 subject="Reply",
#                 body_text=text,
#                 from_user_email=from_user_email,
#                 conversation_id=conv_id,
#             )
#             if not sent:
#                 return jsonify({"error": "Zoho send failed, no response"}), 500
#             sent_id = sent[0] if isinstance(sent, tuple) else sent.get("id")
#             return jsonify({"status": "sent", "id": sent_id, "channel": "zoho"})

#         elif channel == "outlook":
#             print(f"[INFO] → Sending Outlook email to {recipient_email}")
#             sent = send_outlook_email(
#                 to_email=recipient_email,
#                 subject="Reply",
#                 body_text=text,
#                 from_user_email=from_user_email,
#                 conversation_id=conv_id,
#             )
#             if not sent:
#                 return jsonify({"error": "Outlook send failed, no response"}), 500
#             sent_id = sent[0] if isinstance(sent, tuple) else sent.get("id")
#             return jsonify({"status": "sent", "id": sent_id, "channel": "outlook"})

#         elif channel == "slack":
#             sender_name = (
#                 original_message.get("sender_name") if original_message else "MyAppBot"
#             )
#             send_slack_message(
#                 channel_id=slack_channel_id,
#                 text=text,
#                 subject="Reply",
#                 sender_name=sender_name,
#             )
#             return jsonify({"status": "sent", "channel": "slack"})

#         else:
#             print(f"[ERROR] → Unsupported channel: {channel}")
#             return jsonify({"error": "Unsupported channel"}), 400

#     except Exception as e:
#         tb = traceback.format_exc()
#         print(f"[EXCEPTION] → {tb}")
#         return jsonify({"error": str(e), "traceback": tb}), 500


def match_email_to_channel(email, channel):
    """Returns True if the email matches the given channel."""
    if not email or "@" not in email:
        return False
    domain = email.lower().split("@")[1]
    return channel.lower() in domain

@twilio_bp.route("/send-reply", methods=["POST"])
def send_messages():
    print("[INFO] → Received request to send reply")

    try:
        data = request.json
        print(f"[DEBUG] → Payload: {data}")

        user_id = data.get("user_id")
        channel = data.get("channel")
        text = data.get("text")
        conversation_id = data.get("conversation_id")
        print(f"****conv id : {conversation_id}")
        contact_id = data.get("contact_id")

        if not all([user_id, channel, text]):
            print("[WARN] → Missing one or more required fields")
            return jsonify({"error": "Missing required payload fields"}), 400

        print(f"\n🚀 Starting message analysis for sending msg for user_id: {user_id}")
        connection = connect_to_rds()
        if connection is None:
            print("⚠️ DB connection failed")
            return jsonify({"error": "Database connection failed"}), 500

        cursor = connection.cursor()

        is_reply = False
        client_id = None

         # getting client id
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
            print(f"❌ SQL Error: {e}")
            return jsonify({"error": "Failed to retrieve client_id"}), 500
        

        # conv_folder = os.path.join(pathconfig.basepath, user_id,"messages",client_id)
        # ensure_dir(conv_folder)
        # file_name = f"{conversation_id}.json"
        # conv_filepath = os.path.join(conv_folder, file_name)

        if conversation_id:
            print(f"inside if conv id : {conversation_id}")
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
                    print(f"********conv in config file: {conv}")
                    if conv.get("channel") == channel:
                        is_reply = True
                        print(f"✅ Found matching conversation in config file — this is a reply")
                        conversation_id = conv.get("conv_id")
                        break
                    else:
                        print(f"⚠️ Conversation_id not found or channel mismatch — treating as user-initiated")

            except FileNotFoundError:
                print(f"⚠️ Config file not found at {conv_filepath} — treating as user-initiated")
            except Exception as e:
                print(f"❌ Error checking config file for reply status: {e}")
                
        if not is_reply:
            print(f"🆕 Processing user-initiated message")
            conversation_id = str(uuid.uuid4())
            print(f"🆔 Generated new conversation_id: {conversation_id}")

       


     # getting email from tables to check which one to use
        cursor.execute("SELECT email FROM users WHERE user_id = %s", (user_id,))
        u_email = cursor.fetchone()
        if not u_email:
            print("error : No email found from user table")
                    # return {"error": "No token found for business email"}, 404

        user_email = u_email[0]       
        print("user email found:", user_email)


        cursor.execute("SELECT BusinessEmail FROM business_info WHERE user_id_fk = %s", (user_id,))
        b_email = cursor.fetchone()
        if not b_email:
            print("error : No email found from business_info table")
                    # return {"error": "No token found for business email"}, 404
        business_email = b_email[0]       
        print("user email found:", business_email)


        try:
            # Choose email based on channel
            selected_email = None
            if match_email_to_channel(user_email, channel):
                selected_email = user_email
            elif match_email_to_channel(business_email, channel):
                selected_email = business_email

            if selected_email:
                print(f"📬 Selected email for channel '{channel}':", selected_email)
            else:
                print(f"⚠️ No email matched the channel '{channel}'")
                # return {"error": f"No email matched the channel '{desired_channel}'"}, 404

        except Exception as e:
            print("🔥 Exception occurred while selecting email by channel:", str(e))

        # getting client email
        cursor.execute("SELECT email_id FROM users_clients WHERE users_clients_id = %s", (client_id,))
        c_email = cursor.fetchone()
        if not c_email:
            print("error : No email found from users_client table")
        client_email = c_email[0]       
        print("client email found:", client_email)

        conv_folder = os.path.join(pathconfig.basepath, "messages", user_id, client_id)
        ensure_dir(conv_folder)
        file_name = f"{conversation_id}.json"
        conv_filepath = os.path.join(conv_folder, file_name)

        # Handle subject, ticket info, and thread_id based on message type
        if is_reply:
            print("📋 Processing reply - getting info from config file")
            
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

                        print(f"***conv is : {conv}")
                        ticket_id = conv.get("ticket_id")
                        ticket_name = conv.get("ticket_name")
                        subject = conv.get("subject")
                        thread_id = conv.get("thread_id") 
                        break
                    
                print(f"[DEBUG] Found conversation config: ticket_id={ticket_id}, subject={subject}, thread_id={thread_id}")
                # if not subject:
                #     print("[WARN] → Subject not found in config")
                #     return jsonify({"error": "Subject not found for this conversation"}), 400
                    
            except Exception as e:
                print(f"❌ Failed to read config file: {e}")
                return jsonify({"error": "Failed to read conversation config"}), 500
                

            print(f"************thread id {thread_id}")
        else:
            print("🆕 Processing user-initiated message - generating subject")

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
            subject = ticket_name = None
            ticket_id = str(uuid.uuid4())
            
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
        sent_message_id = None

        # sending messages
        if channel == "gmail":
            print(f"[INFO] → Dispatching Gmail message to {client_email}")
            try:
                sent_id = gmail_reply(
                    user_id,
                    to=client_email,
                    subject=subject,
                    thread_id=thread_id,
                    body_text=text,
                )
                return jsonify({"status": "sent", "id": sent_id, "channel": "gmail"}), 200
            except Exception as e:
                print(f"❌ Gmail send failed: {e}")
                return jsonify({"error": "Gmail send failed"}), 500

        elif channel == "zoho":
            print(f"[INFO] → Dispatching Zoho message to {client_email}")
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
                    print(f" received msg_id: {message_id}")
                
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
            print("🔄 Starting DB operations for message persistence")

            if not is_reply:
                print("🆕 New conversation — inserting thread and ticket")
                cursor.execute(
                    """
                    INSERT INTO threads (conversation_id, started_at, status, last_message_at)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (conversation_id, created_date, "Open", updated_date)
                )
                # You can add ticket insert here if needed
            else:
                print("🔁 Existing conversation — updating thread and ticket status")
                cursor.execute(
                    "UPDATE tickets SET updated_in = %s, status = %s WHERE conversation_id_fk = %s",
                    (updated_date, "In-Progress", conversation_id)
                )
                cursor.execute(
                    "UPDATE threads SET last_message_at = %s WHERE conversation_id = %s",
                    (updated_date, conversation_id)
                )

            print("📨 Inserting message into messages table")
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
            print("✅ Database operations committed successfully")

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
            print(f"📁 Updating conversation file at: {conv_filepath}")
            try:
                raw_data = read_json_from_s3(s3_conv_key)
                input_data = raw_data.get("input_data", [])
                print(f"[DEBUG] Found existing conversation with {len(input_data)} messages")
            except Exception as e:
                print(f"[DEBUG] No existing conversation file found — initializing new: {e}")
                input_data = []

            input_data.append(message)
            conversation_data = {"input_data": input_data}

            with open(conv_filepath, "w", encoding="utf-8") as f:
                json.dump(conversation_data, f, indent=2)
            print("✅ Local conversation file updated")

            upload_any_file(
                conv_filepath,
                user_id,
                type="messages",
                s3_key_C=s3_conv_key,
            )
            print("✅ Conversation file uploaded to S3")

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
            print(f"[DEBUG] Loaded config from S3: {s3_config_key}")
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
            "thread_id": thread_id,
            "updated_date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "parsed_timestamp": parsed_ts.isoformat()
        }

        conversation_exists = False
        for i, conv in enumerate(config_data.get("conversations", [])):
            if conv.get("conv_id") == conversation_id:
                config_data["conversations"][i] = updated_entry
                conversation_exists = True
                print(f"🔁 Updated existing conversation entry in config")
                break

        if not conversation_exists:
            config_data.setdefault("conversations", []).append(updated_entry)
            print(f"🆕 Added new conversation entry to config")

        config_data["userclients_id"] = client_id
        update_config_file(user_id, client_id, config_data)
        print("✅ Config file updated successfully")

        # ------------------ Final Response ------------------

        print("✅ Message sent successfully")
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
                print("🔒 DB connection closed")
            

                

   


@twilio_bp.route("/tickets", methods=["GET"])
def get_user_tickets():
    user_id = request.args.get("user_id")
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
            SELECT t.ticket_id, t.priority, t.status, 
                   t.created_in, t.updated_in, t.conversation_id_fk
            FROM tickets t
            JOIN communication c ON t.communication_id = c.communication_id
            WHERE c.user_id = %s
            ORDER BY t.updated_in DESC
        """
        cursor.execute(query, (user_id,))
        rows = cursor.fetchall()
            
        tickets = []
        
        for row in rows:
            ticket_id, priority, status, created_in, updated_in, conversation_id = row

            key = conv_key_map.get(conversation_id)
            if key:
                        # Get conversation details from JSON file
                    conversation_data = get_conversation_details(key)
                        
                        # Build ticket response
                    ticket_info = {
                            "ticket_id": ticket_id,
                            "priority": priority,
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
        print(f"[DEBUG] Loaded config from S3: {key}")

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
