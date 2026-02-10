import base64
from datetime import datetime, timedelta
import json
import os
from db.db_checkers import get_userid
from flask import Blueprint, request, jsonify
from services.redis_service import RedisService
from services.uamil_auto_service import UmailAutoService
from umail_helper.mails_process import check_mailbox_email
from utils.base_logger import get_logger
from utils.celery_base import delayed_trigger, lock_client
from db.rds_db import connect_to_rds


assist_suggest_bp = Blueprint("assistsuggest", __name__)
logger = get_logger(__name__)


@assist_suggest_bp.route("/gmail/webhook", methods=["POST", "GET"])
async def receive_gmail_notification():
    WEBHOOK_LOG_DIR = "data/test"
    WEBHOOK_LOG_FILE = os.path.join(WEBHOOK_LOG_DIR, "webhook_log.json")
    DEDUP_WINDOW = 30  # seconds

    if request.method == "GET":
        return "Webhook is live!", 200

    if not request.json or "message" not in request.json:
        return "Invalid request", 400

    pubsub_message = request.json["message"]

    # Decode the pubsub message
    decoded_data = {}
    if "data" in pubsub_message:
        decoded_data_raw = base64.b64decode(pubsub_message["data"]).decode("utf-8")
        decoded_data = json.loads(decoded_data_raw)

    user_email = decoded_data.get("emailAddress", "unknown")
    history_id = decoded_data.get("historyId")
    if not user_email or not history_id:
        return "Invalid Pub/Sub message data", 400
    print("got hook from", user_email)
    redis = RedisService()
    user_id = get_userid(user_email)
    val = await redis.exists(f"user_alive:{user_id}")
    if not val:
        print("user skipped not alive")
        return "user skipped not alive", 200
    mailcheck = check_mailbox_email(user_email)
    if not mailcheck:
        return "ok", 200

    # Ensure log directory exists
    os.makedirs(WEBHOOK_LOG_DIR, exist_ok=True)

    # Load existing logs
    if os.path.isfile(WEBHOOK_LOG_FILE):
        try:
            with open(WEBHOOK_LOG_FILE, "r") as f:
                log_data = json.load(f)
            if not isinstance(log_data, dict):
                log_data = {}
        except json.JSONDecodeError:
            log_data = {}
    else:
        log_data = {}

    # Append new entry to logs
    new_entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "raw_pubsub_message": pubsub_message,
        "decoded_data": decoded_data,
    }
    log_data.setdefault(user_email, []).append(new_entry)

    # Remove entries older than 2 days
    cutoff = datetime.utcnow() - timedelta(days=2)
    for email, entries in list(log_data.items()):
        log_data[email] = [
            e for e in entries if datetime.fromisoformat(e["timestamp"]) > cutoff
        ]
        if not log_data[email]:
            del log_data[email]

    with open(WEBHOOK_LOG_FILE, "w") as f:
        json.dump(log_data, f, indent=2)

    # ✅ FIX #1: Deduplication based on HISTORY_ID, not user_email
    # This allows multiple webhooks from the same user if they have different historyIds
    # (meaning different email arrival events, not duplicates)
    dedup_key = f"webhook_dedup:{user_email}:{history_id}"

    # Check if THIS specific history_id was already processed recently
    recent = await lock_client.get(dedup_key)
    if recent:
        # This exact history_id was already processed; skip it
        logger.info(
            f"Duplicate webhook skipped for {user_email}, historyId={history_id}"
        )
        return "Duplicate webhook skipped", 200

    # Mark this specific history_id as processed for 5 minutes
    # (to avoid processing the exact same webhook multiple times)
    await lock_client.set(dedup_key, "1", ex=300)

    # ✅ Trigger Celery for this webhook (even if same user, different history_id)
    logger.info(f"Processing webhook for {user_email}, historyId={history_id}")

    # check if integration or not
    conn = connect_to_rds()
    cursor = conn.cursor()

    integration = False
    email = ""
    cursor.execute("SELECT 1 FROM integrations WHERE email=%s", (user_email,))
    row = cursor.fetchone()
    if row:
        integration = True
    else:
        cursor.execute("SELECT 1 FROM users WHERE email=%s", (user_email,))
        row = cursor.fetchone()
        if not row:
            return ("User not found", 404)

    print(f"integratiosn passed to delayed_trigger : {integration}")

    delayed_trigger.delay(
        user_email, history_id, integration=integration, channel="google"
    )
    return "OK", 200


@assist_suggest_bp.route("/ai_autopilot", methods=["POST"])
def triggerassist():
    data = request.json
    userid = data.get("user_id")
    from_email = data.get("email")
    selected_agent = data.get("selected_agent")

    if not userid or not from_email:
        return jsonify({"error": "Missing required fields"}), 400

    with UmailAutoService(userid) as service:
        return service.activate_autopilot(from_email, selected_agent)


@assist_suggest_bp.route("/ai_autopilot-revoke", methods=["POST"])
def revoke_autopilot():
    data = request.json
    userid = data.get("user_id")
    target_email = data.get("email")
    pilot_override = data.get("pilot_override", False)

    if not userid or not target_email:
        return jsonify({"error": "Missing required fields"}), 400

    with UmailAutoService(userid) as service:
        return service.revoke_autopilot(target_email, pilot_override)


@assist_suggest_bp.route("/ai_autopilot-mode", methods=["POST"])
def changepilotmode():
    data = request.json
    userid = data.get("user_id")
    new_mode = data.get("mode")

    if not userid or not new_mode:
        return jsonify({"error": "Missing required fields"}), 400

    with UmailAutoService(userid) as service:
        return service.change_autopilot_mode(new_mode)


@assist_suggest_bp.route("/ai_autopilot-reset/<userid>", methods=["GET"])
def reset_autopilot(userid):
    with UmailAutoService(userid) as service:
        return service.reset_autopilot()


@assist_suggest_bp.route("/ai_autopilot/<userid>", methods=["GET"])
def get_autopilot(userid):
    with UmailAutoService(userid) as service:
        autopilot_data, err, code = service.fetch_autopilot()
        if err:
            return jsonify(err), code
        return jsonify({"user_id": userid, "autopilot": autopilot_data}), 200


@assist_suggest_bp.route("/ai_autopilot-update-agent", methods=["POST"])
def update_selected_agent():
    data = request.json
    userid = data.get("user_id")
    target_email = data.get("email")
    selected_agent = data.get("selected_agent")

    if not all([userid, target_email, selected_agent]):
        return jsonify({"error": "Missing required fields"}), 400

    with UmailAutoService(userid) as service:
        # Reuse activate_autopilot logic to update selected_agent
        return service.activate_autopilot(target_email, selected_agent)


# -------------------- Auto-reply / AI Suggestion Routes --------------------


@assist_suggest_bp.route("/auto-reply-email", methods=["POST"])
def make_reply_email():
    data = request.json
    userid = data.get("user_id")
    from_email = data.get("email")

    if not userid or not from_email:
        return jsonify({"error": "Missing required fields"}), 400

    with UmailAutoService(userid) as service:
        success = service.auto_reply_umail_email(from_email)
        if success is True:
            return jsonify({"status": "sent"}), 200
        elif success is False:
            return jsonify({"status": "already_replied"}), 200
        else:
            return jsonify({"error": "Unable to process auto-reply"}), 500


@assist_suggest_bp.route("/ai_suggest", methods=["POST"])
async def triggersuggest():
    data = request.json
    userid = data.get("user_id")
    msg_body = data.get("msg_body")
    conv_id = data.get("conversation_id")

    if not all([userid, msg_body, conv_id]):
        return jsonify({"error": "Missing required fields"}), 400

    with UmailAutoService(userid) as service:
        return await service.suggest_umail_reply(msg_body, conv_id)


@assist_suggest_bp.route("/test_functions", methods=["POST"])
async def messcheckgmail():
    data = request.json
    userid = data.get("user_id")
    userinput = data.get("userinput")
    ##print("userinp", userinput)
    # Connect to DB
    with UmailAutoService(userid) as service:
        return await service.generate_file_from_ai(userid, user_input=userinput)
