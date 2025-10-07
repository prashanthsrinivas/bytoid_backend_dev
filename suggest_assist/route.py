import base64
from datetime import datetime, timedelta, timezone
import json
import os
from db.db_checkers import get_users_clients_id
from db.rds_db import connect_to_rds
from flask import Blueprint, request, jsonify
from gmail_route.gmail_service import GmailService
from umail_helper.asyn_functions import get_datewise_info_base
from utils.base_logger import get_logger
from utils.normal import can_reply_to_email
from .suggest_helper import (
    getselectedconv,
    send_pilot_messages,
    suggest_helper_base,
    umail_get_sorted_lance_emails,
)
from utils.celery_base import delayed_trigger, lock_client
import pymysql
from utils.celery_base import umail_sync, addbase

assist_suggest_bp = Blueprint("assistsuggest", __name__)
logger = get_logger(__name__)


@assist_suggest_bp.route("/ai_suggest", methods=["POST"])
def triggersuggest():
    try:
        data = request.json
        userid = data["user_id"]
        email_msg = data["msg_body"]
        conv_id = data["conversation_id"]
        # Fetch email conversation data
        umail_conversations = getselectedconv(conv_id=conv_id, userid=userid)
        umail_bodies = [item.get("body", "") for item in umail_conversations]
        ai_reply = suggest_helper_base(
            userid=userid,
            email_msg=email_msg,
            umail_conversations=umail_conversations,
            umail_bodies=umail_bodies,
        )
        return jsonify({"message": ai_reply.strip()}), 200
    except Exception as e:
        logger.error("error: %s", e)
        return jsonify({"error": "cant make ai suggest"}), 500


# trigger = DelayTrigger(wait_seconds=30)


@assist_suggest_bp.route("/gmail/webhook", methods=["POST", "GET"])
def receive_gmail_notification():
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

    # Deduplication: Only trigger Celery if no other recent webhook for this user
    dedup_key = f"webhook_dedup:{user_email}"
    # Set a short TTL to ensure only the latest webhook triggers
    recent = lock_client.get(dedup_key)
    lock_client.set(dedup_key, history_id, ex=DEDUP_WINDOW)
    if recent:
        # A recent webhook exists; skip this one
        return "Duplicate webhook skipped", 200

    # Trigger Celery for this webhook
    delayed_trigger.delay(user_email, history_id)
    return "OK", 200


smae_autopilotjson = {"method": "ALL"}
# for all new messages we will reply to all
sameple_autopilotjson = {
    "method": "ALL / mixed",
    "internals": {
        "email1": {"status": "active", "updated_at": "date"},  # revoked,
        "email2": {"status": "active", "updated_at": "date"},  # revoked,
    },
}
# 3 new messages i have 2 auto pilot present  we reply to specific one
"""
user can make  auto pilot to 
    1. ALL
    2. selected emails
    3. mixed like all and selected emails(some changed selected agents)

for documents needed for ai suggest 
    1.if all -> handle base users document
    2.selected emails will have dcumented userid attached
    3. with all and also have selected emails

"""


def _fetch_autopilot(userid, cursor):
    cursor.execute("SELECT autopilot FROM users WHERE user_id = %s LIMIT 1", (userid,))
    user_row = cursor.fetchone()
    if not user_row:
        return None, {"error": "user not found"}, 404

    autopilot_data = user_row.get("autopilot") or {}
    if isinstance(autopilot_data, str):
        try:
            autopilot_data = json.loads(autopilot_data)
        except json.JSONDecodeError:
            autopilot_data = {}

    # Ensure mode/logs structure
    if "mode" not in autopilot_data:
        autopilot_data["mode"] = "dynamic"
    if "logs" not in autopilot_data or not isinstance(autopilot_data["logs"], list):
        autopilot_data["logs"] = []

    return autopilot_data, None, None


def _persist_autopilot(userid, autopilot_data, cursor):
    cursor.execute(
        "UPDATE users SET autopilot = %s WHERE user_id = %s",
        (json.dumps(autopilot_data), userid),
    )


@assist_suggest_bp.route("/ai_autopilot", methods=["POST"])
def triggerassist():
    data = request.get_json(force=True)
    userid = data.get("user_id")
    from_email = data.get("email")  # 'ALL', str, or list
    selected_agents = data.get("selected_agent", userid)
    pilot_override = data.get("pilot_override", False)

    if not userid or not from_email:
        return jsonify({"error": "Missing required fields"}), 400

    now = datetime.utcnow().isoformat()
    already_active = True

    connection = connect_to_rds()
    try:
        with connection.cursor(pymysql.cursors.DictCursor) as cursor:
            autopilot_data, err, code = _fetch_autopilot(userid, cursor)
            if err:
                return jsonify(err), code

            # Set mode to ALL or DYNAMIC based on request
            mode = "all" if from_email == "ALL" else "dynamic"
            autopilot_data["mode"] = mode

            emails = [from_email] if isinstance(from_email, str) else from_email
            for email in emails:
                # Find existing log
                existing_entry = next(
                    (e for e in autopilot_data["logs"] if e["email"] == email), None
                )
                if (
                    not existing_entry
                    or existing_entry.get("status") != "active"
                    or existing_entry.get("selected_agent") != selected_agents
                ):
                    update_data = {
                        "email": email,
                        "status": "active",
                        "last-conv": None,
                        "last-msg": None,
                        "updated_at": now,
                        "selected_agent": selected_agents,
                    }
                    if existing_entry:
                        idx = autopilot_data["logs"].index(existing_entry)
                        autopilot_data["logs"][idx] = update_data
                    else:
                        autopilot_data["logs"].append(update_data)
                    already_active = False

            if not already_active:
                _persist_autopilot(userid, autopilot_data, cursor)
                connection.commit()

    finally:
        connection.close()

    msg = "autopilot already active" if already_active else "autopilot activated"
    return jsonify({"message": msg, "autopilot": autopilot_data}), 200


@assist_suggest_bp.route("/ai_autopilot-revoke", methods=["POST"])
def revoke_autopilot():
    data = request.get_json(force=True)
    userid = data.get("user_id")
    target_email = data.get("email")  # str, list, or 'ALL'
    pilot_override = data.get("pilot_override", False)

    if not all([userid, target_email]):
        return jsonify({"error": "Missing required fields"}), 400

    emails = [target_email] if isinstance(target_email, str) else target_email
    now = datetime.utcnow().isoformat()

    connection = connect_to_rds()
    try:
        with connection.cursor(pymysql.cursors.DictCursor) as cursor:
            autopilot_data, err, code = _fetch_autopilot(userid, cursor)
            if err:
                return jsonify(err), code

            revoked_any = False
            logs = autopilot_data.get("logs", [])

            for email in emails:
                if email == "ALL":
                    # Revoke all logs
                    for log in logs:
                        log["status"] = "revoked"
                        log["updated_at"] = now
                    revoked_any = True
                    if pilot_override:
                        # Force mode to dynamic
                        autopilot_data["mode"] = "dynamic"

                else:
                    # Revoke specific email log
                    entry = next((e for e in logs if e["email"] == email), None)
                    if entry:
                        entry["status"] = "revoked"
                        entry["updated_at"] = now
                        revoked_any = True
                        if pilot_override and autopilot_data.get("mode") == "all":
                            autopilot_data["mode"] = "dynamic"

            if not revoked_any:
                return jsonify({"error": "email(s) not found in autopilot logs"}), 404

            autopilot_data["logs"] = logs
            _persist_autopilot(userid, autopilot_data, cursor)
            connection.commit()
    finally:
        connection.close()

    return jsonify({"message": "Autopilot revoked", "autopilot": autopilot_data}), 200


@assist_suggest_bp.route("/ai_autopilot/<int:userid>", methods=["GET"])
def get_autopilot(userid):
    connection = connect_to_rds()
    try:
        with connection.cursor(pymysql.cursors.DictCursor) as cursor:
            autopilot_data, err, code = _fetch_autopilot(userid, cursor)
            if err:
                return jsonify(err), code
    finally:
        connection.close()

    return jsonify({"user_id": userid, "autopilot": autopilot_data}), 200


@assist_suggest_bp.route("/ai_autopilot-reset/<int:userid>", methods=["GET"])
def reset_autopilot(userid):

    if not userid:
        return jsonify({"error": "Missing user_id"}), 400

    connection = connect_to_rds()
    try:
        with connection.cursor(pymysql.cursors.DictCursor) as cursor:
            # Check if user exists
            cursor.execute(
                "SELECT user_id FROM users WHERE user_id = %s LIMIT 1", (userid,)
            )
            user_row = cursor.fetchone()
            if not user_row:
                return jsonify({"error": "User not found"}), 404

            # Reset autopilot
            cursor.execute(
                "UPDATE users SET autopilot = NULL WHERE user_id = %s", (userid,)
            )
            connection.commit()

    finally:
        connection.close()

    return jsonify({"message": f"Autopilot data reset for user {userid}"}), 200


@assist_suggest_bp.route("/ai_autopilot-update-agent", methods=["POST"])
def update_selected_agent():
    data = request.get_json(force=True)
    userid = data.get("user_id")
    target_email = data.get("email")  # str, list, or 'ALL'
    selected_agents = data.get("selected_agent")

    if not all([userid, target_email, selected_agents]):
        return jsonify({"error": "Missing required fields"}), 400

    if isinstance(selected_agents, str):
        selected_agents = [selected_agents]

    emails = [target_email] if isinstance(target_email, str) else target_email
    now = datetime.utcnow().isoformat()

    connection = connect_to_rds()
    try:
        with connection.cursor(pymysql.cursors.DictCursor) as cursor:
            autopilot_data, err, code = _fetch_autopilot(userid, cursor)
            if err:
                return jsonify(err), code

            updated_any = False
            logs = autopilot_data.get("logs", [])

            for email in emails:
                if email == "ALL":
                    for log in logs:
                        log["selected_agent"] = selected_agents
                        log["updated_at"] = now
                        updated_any = True
                else:
                    entry = next((e for e in logs if e["email"] == email), None)
                    if entry:
                        entry["selected_agent"] = selected_agents
                        entry["updated_at"] = now
                        updated_any = True

            if not updated_any:
                return jsonify({"error": "email(s) not found in autopilot logs"}), 404

            autopilot_data["logs"] = logs
            _persist_autopilot(userid, autopilot_data, cursor)
            connection.commit()
    finally:
        connection.close()

    return (
        jsonify(
            {
                "message": "selected_agent updated successfully",
                "autopilot": autopilot_data,
            }
        ),
        200,
    )


@assist_suggest_bp.route("/auto-reply-email", methods=["POST"])
def make_reply_email(baseuserid=None, baseemail=None, n_connection=None):
    """
    If the last message across all conversations is inbound → we can reply.
    If outbound → already replied.
    """
    try:
        if baseemail or baseuserid:
            from_email = baseemail
            userid = baseuserid
        else:
            data = request.get_json(force=True)
            userid = data.get("user_id")
            from_email = data.get("email")

        if n_connection is None:
            connection = connect_to_rds()

        clientid = get_users_clients_id(email=from_email, user_id=userid)
        if not clientid:
            return jsonify({"error": "No email communication found"}), 404

        # This is already sorted by your code (earliest → latest)
        sorted_conversations = umail_get_sorted_lance_emails(
            connection=connection, user_id=userid, client_id=clientid
        )

        if not sorted_conversations:
            return jsonify({"error": "No conversations found"}), 404

        # collect all messages from the sorted conversations
        all_messages = []
        for conv in sorted_conversations:
            all_messages.extend(conv.get("messages", []))

        if not all_messages:
            return jsonify({"error": "No messages found"}), 404

        # your sorted_conversations is sorted, but messages inside might not be,
        # so still pick the latest by timestamp from all_messages:
        latest_msg = all_messages[-1]
        client_email = latest_msg.get("from")

        if not can_reply_to_email(client_email):
            return (
                jsonify(
                    {
                        "status": "cannot_reply",
                        "reason": f"Do not reply to: {client_email}",
                    }
                ),
                200,
            )
        # return jsonify({"allmsg": all_messages, "latestmsg": latest_msg}), 200

        if latest_msg.get("direction") == "inbound":
            # last message from customer → we can reply
            ai_reply = suggest_helper_base(
                userid=userid,
                email_msg=latest_msg["body"],
                umail_conversations=all_messages,
                umail_bodies=[msg.get("body") for msg in all_messages],
            )
            send_val = send_pilot_messages(
                user_id=userid,
                channel="gmail",
                text=ai_reply,
                conversation_id=latest_msg["conversation_id"],
                b_connection=connection,
                client_id=clientid,
                user_email=latest_msg["to"],
                client_email=latest_msg["from"],
                subject=latest_msg["subject"],
                thread_id=latest_msg["thread_id"],
                ticket_id=latest_msg["ticket_id"],
                ticket_name=latest_msg["ticket_name"],
                is_reply=True,
            )
            if baseuserid or baseemail:
                return True
            return (
                jsonify(
                    {
                        "status": "sent",
                        "info": send_val,
                    }
                ),
                200,
            )
        else:
            # last message from you → already replied
            if baseuserid or baseemail:
                return False
            return (
                jsonify(
                    {
                        "status": "already_replied",
                    }
                ),
                200,
            )

    except Exception as e:
        logger.info("ERROR %s", e)
        if baseemail or baseuserid:
            return None
        return jsonify({"error": str(e)}), 500
    finally:
        if n_connection is None and connection:
            connection.close()


import asyncio


@assist_suggest_bp.route("/test_gmail_mess/<userid>/<hist>", methods=["GET"])
def messcheckgmail(userid, hist):
    # Connect to DB
    connection = connect_to_rds()
    if connection is None:
        return jsonify({"error": "Database connection failed", "status": "failed"})

    # Inner async function to run async tasks
    async def main():
        # Fetch total messages info
        end_date = datetime.now(timezone.utc).date()
        today = end_date - timedelta(days=3)

        total_messages = await get_datewise_info_base(
            userid=userid, connection=connection, months=6
        )
        threads_info = total_messages.get("threadsTotal", {})
        threads_max = threads_info.get("count", 0)
        threads = threads_info.get("threads", [])
        my_email = total_messages.get("email")

        if not threads:
            return {"res": [], "val": None, "status": "no threads found"}

        # Gmail service instance
        gmail_service = GmailService(userid, connection)

        # Get Gmail changes (synchronous)
        # val = gmail_service.get_gmail_changes(hist)

        # Fetch all messages in threads (async)
        # threads = [
        #     {
        #         "historyId": "13510",
        #         "id": "19855fad53ec843d",
        #         "snippet": "this is the re reply to the test message On Tue, Jul 29, 2025 at 5:10 PM Service Account &lt;service@bytoid.ca&gt; wrote: yes reply to that test message On Tue, Jul 29, 2025 at 5:09 PM Bytoid Test",
        #     },
        #     {"historyId": "13810", "id": "198659fcb35d870b", "snippet": "idk"},
        # ]
        # my_email = "service@bytoid.ca"
        # results = await v2fetch_gmail_messages_batch(
        #     userid, threads, my_email, len(threads), connection
        # )
        results = await gmail_service.process_threads_batch(
            threads, my_email, threads_max
        )
        # all_messages = []
        # for thread_id, res in results.items():
        #     thread_data, err = res

        #     if not res:
        #         print(f"⚠️ No response for thread {thread_id}")
        #         continue

        #     thread_data, err = res  # ✅ unpack tuple

        #     if err:
        #         print(f"⚠️ Thread {thread_id} error: {err}")
        #         continue

        #     if thread_data:
        #         all_messages.extend(thread_data)

        return {
            "res": results,
            "status": "success",
            "rescount": len(results),
            # "changed": all_messages,
            # "chan_count": len(all_messages),
        }

    # Run the async main function in a synchronous route
    response_data = asyncio.run(main())
    connection.close()
    return jsonify(response_data)
