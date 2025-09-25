import base64
from datetime import datetime, timedelta
import json
import os
from db.rds_db import connect_to_rds
from flask import Blueprint, request, jsonify
from .suggest_helper import suggest_helper_base

from utils.celery_base import delayed_trigger
import pymysql

from utils.celery_base import umail_sync, addbase

assist_suggest_bp = Blueprint("assistsuggest", __name__)


@assist_suggest_bp.route("/ai_suggest", methods=["POST"])
def triggersuggest():
    data = request.json
    userid = data["user_id"]
    email_msg = data["msg_body"]
    conv_id = data["conversation_id"]
    # Fetch email conversation data
    ai_reply = suggest_helper_base(userid=userid, email_msg=email_msg, conv_id=conv_id)
    return jsonify({"message": ai_reply.strip()}), 200


# trigger = DelayTrigger(wait_seconds=30)


@assist_suggest_bp.route("/gmail/webhook", methods=["POST", "GET"])
def receive_gmail_notification():
    WEBHOOK_LOG_DIR = "data/test"
    WEBHOOK_LOG_FILE = os.path.join(WEBHOOK_LOG_DIR, "webhook_log.json")

    if request.method == "GET":
        return "Webhook is live!", 200

    if not request.json or "message" not in request.json:
        return "Invalid request", 400

    pubsub_message = request.json["message"]

    decoded_data = {}
    if "data" in pubsub_message:
        decoded_data_raw = base64.b64decode(pubsub_message["data"]).decode("utf-8")
        decoded_data = json.loads(decoded_data_raw)

    os.makedirs(WEBHOOK_LOG_DIR, exist_ok=True)

    # Load existing grouped logs
    if os.path.isfile(WEBHOOK_LOG_FILE):
        try:
            with open(WEBHOOK_LOG_FILE, "r") as f:
                log_data = json.load(f)
            # Force it to dict if file had a list
            if not isinstance(log_data, dict):
                log_data = {}
        except json.JSONDecodeError:
            log_data = {}
    else:
        log_data = {}

    # Build the new entry
    user_email = decoded_data.get("emailAddress", "unknown")
    new_entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "raw_pubsub_message": pubsub_message,
        "decoded_data": decoded_data,
    }

    # Append to email group
    if user_email not in log_data:
        log_data[user_email] = []
    log_data[user_email].append(new_entry)

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

    # Your existing flow
    history_id = decoded_data.get("historyId")
    if user_email and history_id:
        delayed_trigger.delay(user_email, history_id)
        return "OK", 200
    else:
        return "Invalid Pub/Sub message data", 400


@assist_suggest_bp.route("/ai_autopilot", methods=["POST"])
def triggerassist():
    data = request.get_json(force=True)
    userid = data.get("user_id")
    from_email = data.get("email")
    email_msg = data.get("msg_body")
    conv_id = data.get("conversation_id")

    if not all([userid, from_email, email_msg, conv_id]):
        return jsonify({"error": "Missing required fields"}), 400

    connection = connect_to_rds()
    try:
        with connection.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                "SELECT email, autopilot FROM users WHERE user_id = %s LIMIT 1",
                (userid,),
            )
            user_row = cursor.fetchone()
            if not user_row:
                return jsonify({"error": "user not found"}), 404

            # current autopilot JSON from DB (could be string or dict)
            autopilot_data = user_row["autopilot"] or {}
            if isinstance(autopilot_data, str):
                try:
                    autopilot_data = json.loads(autopilot_data)
                except json.JSONDecodeError:
                    autopilot_data = {}

            # update / add entry for from_email
            autopilot_data[from_email] = {
                "status": "active",
                "updated_at": datetime.datetime.utcnow().isoformat(),
            }

            # persist back to DB
            cursor.execute(
                "UPDATE users SET autopilot = %s WHERE user_id = %s",
                (json.dumps(autopilot_data), userid),
            )
        connection.commit()
    finally:
        connection.close()

    # Call your AI helper
    ai_reply = suggest_helper_base(userid=userid, email_msg=email_msg, conv_id=conv_id)

    return jsonify({"message": ai_reply.strip()}), 200

@assist_suggest_bp.route("/ai_autopilot-revoke", methods=["POST"])
def revoke_autopilot():
    data = request.get_json(force=True)
    userid = data.get("user_id")
    target_email = data.get("email")  # the email to revoke

    if not all([userid, target_email]):
        return jsonify({"error": "Missing required fields"}), 400

    connection = connect_to_rds()
    try:
        with connection.cursor(pymysql.cursors.DictCursor) as cursor:
            # fetch current autopilot JSON
            cursor.execute(
                "SELECT autopilot FROM users WHERE user_id = %s LIMIT 1",
                (userid,),
            )
            user_row = cursor.fetchone()
            if not user_row:
                return jsonify({"error": "user not found"}), 404

            autopilot_data = user_row["autopilot"] or {}
            if isinstance(autopilot_data, str):
                try:
                    autopilot_data = json.loads(autopilot_data)
                except json.JSONDecodeError:
                    autopilot_data = {}

            # revoke the email if exists
            if target_email in autopilot_data:
                autopilot_data[target_email]["status"] = "revoked"
                autopilot_data[target_email]["updated_at"] = datetime.datetime.utcnow().isoformat()
            else:
                return jsonify({"error": "email not found in autopilot"}), 404

            # update DB
            cursor.execute(
                "UPDATE users SET autopilot = %s WHERE user_id = %s",
                (json.dumps(autopilot_data), userid),
            )
        connection.commit()
    finally:
        connection.close()

    return jsonify({
        "message": f"Email {target_email} revoked successfully",
        "autopilot": autopilot_data
    }), 200
@assist_suggest_bp.route("/testbase-all", methods=["POST"])
def trigger_all_jobs():
    print("TRIGGER API VAL")
    payload = request.json or {}
    try:
        res = addbase.delay(2, 3)
        print("RRR", res)
        t_a = umail_sync.delay(payload.userid)
        print("AAA", t_a)
        return jsonify({"job_a_task_id": t_a.id, "add_task_id": res.id}), 202
    except Exception as e:
        return jsonify({"error": str(e)}), 500


sameple_autopilotjson = {
    "email1": {"status": "active", "updated_at": "date"},  # revoked,
    "email2": {"status": "active", "updated_at": "date"},  # revoked,
}
