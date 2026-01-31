from flask import Flask, request, jsonify, Blueprint, Response, session
from twilio.rest import Client
import uuid
import os
import sys
from twilio.twiml.voice_response import VoiceResponse
from slack_sdk import WebClient
from slack_sdk.signature import SignatureVerifier
from datetime import datetime, timezone
from data import MESSAGES  # delete this later, this is just for testing
import requests

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from create_db import connect_to_rds
from typing import Dict, Any

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


# @twilio_bp.route('/twilio_webhook/send_whatsapp', methods=['POST'])
def send_whatsapp(from_number, to_number, body, conversation_id, subject):

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

    # print(f"messages are: {MESSAGES}")

    return {"sid": message.sid, "status": "sent", "channel": "whatsapp"}


# @twilio_bp.route('/twilio_webhook/send-sms', methods=['POST'])
def send_sms(from_number, to_number, body, conversation_id, subject):

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

        # print(f"messages are: {MESSAGES}")

    except Exception as e:
        # print(f"Error sending messages: {e}")
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

    return "OK", 200


# receive whatsapp messages through twilio
@twilio_bp.route("/twilio_webhook/receive_messages", methods=["POST"])
def handle_twilio_webhook():
    payload = request.form.to_dict() or request.get_json()
    # print(f"payload is: {payload}")
    ##print("helloooo")

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

    # print(f"Message from whatsapp:{MESSAGES[message_sid]} ")
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
    # print(f"Voice webhook triggered. Recording will start now.")
    return Response(str(response), mimetype="application/xml")


@twilio_bp.route("/twilio_webhook/recording-complete", methods=["POST"])
def recording_complete():

    recording_url = request.form.get("RecordingUrl")
    call_sid = request.form.get("CallSid")

    # print(f"Recording completed: {recording_url} (Call SID: {call_sid})")
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

    # print(f"Stored transcription for call {call_sid}:")
    # print(f"From: {from_number}, To: {to_number}")
    # print(f"Transcription: {transcription_text}")
    # print(f"MESSAGES: {MESSAGES}")

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
            # print(f"Unsupported channel type: {channel}")
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

        # else:
        #     #print(f"Failed to fetch user info for user ID: {user_id}")

        # print(f"Message from slack:{MESSAGES[key]} ")
    # else:
    #     #print(f"Received event: {event}")
    return "OK", 200


def get_user_profile(sender_id, page_access_token):
    url = f"https://graph.facebook.com/v19.0/{sender_id}"
    params = {"fields": "first_name,last_name", "access_token": page_access_token}
    response = requests.get(url, params=params)
    if response.ok:
        profile = response.json()
        return f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
    else:
        # print("Error fetching profile:", response.text)
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
        # print("Webhook received data:", data)

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

                            # print("Stored message:", MESSAGES[key])

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

    #print(f"Sending Slack message to {channel_id} with body: {text}")

    if not channel_id or not text:
        # print("Missing channel_id or text fields")
        return None

    try:
        # Send the message via Slack API
        response = slack_client.chat_postMessage(channel=channel_id, text=text)
        #print(f"Slack message sent. ts: {response['ts']}")

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

        # print(
        #     f"Stored Slack message in MESSAGES under key: {MESSAGES[key]['id']}: {MESSAGES[key]}"
        # )
        return response

    except Exception as e:
        # print("Error sending Slack message:", str(e))
        raise
