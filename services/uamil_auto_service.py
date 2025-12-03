import json
from datetime import datetime
from create_db import connect_to_rds
from db.db_checkers import get_users_clients_id
from flask import jsonify
import pymysql
from suggest_assist.suggest_helper import (
    getselectedconv,
    helper_make_reply_email,
    send_pilot_messages,
    suggest_helper_base,
    umail_get_sorted_lance_emails,
)
from utils.base_logger import get_logger
from utils.normal import (
    can_reply_to_email,
)

logger = get_logger(__name__)


class UmailAutoService:
    def __init__(self, userid, testing=False, workflow=None, wf_id=None):
        self.userid = userid
        self.connection = connect_to_rds()
        self.autopilot_data = None
        self.testing = testing
        self.workflow = workflow
        self.current_wf_id = wf_id

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.connection:
            self.connection.close()

    # -------------------- Autopilot Data Fetch/Save --------------------
    def fetch_autopilot(self):
        with self.connection.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                "SELECT autopilot FROM users WHERE user_id = %s LIMIT 1", (self.userid,)
            )
            row = cursor.fetchone()
            if not row:
                return None, {"error": "user not found"}, 404

            autopilot_data = row.get("autopilot") or {}
            if isinstance(autopilot_data, str):
                try:
                    autopilot_data = json.loads(autopilot_data)
                except json.JSONDecodeError:
                    autopilot_data = {}

            # Ensure structure
            autopilot_data.setdefault("mode", "dynamic")
            autopilot_data.setdefault("logs", [])

            self.autopilot_data = autopilot_data
            return autopilot_data, None, None

    def persist_autopilot(self):
        with self.connection.cursor() as cursor:
            cursor.execute(
                "UPDATE users SET autopilot = %s WHERE user_id = %s",
                (json.dumps(self.autopilot_data), self.userid),
            )
        self.connection.commit()

    def suggest_umail_reply(self, email_msg, conv_id):
        try:
            umail_conversations = getselectedconv(conv_id=conv_id, userid=self.userid)
            umail_bodies = [msg.get("body", "") for msg in umail_conversations]
            # print("umail conversations", umail_bodies)
            ai_reply = suggest_helper_base(
                userid=self.userid,
                email_msg=email_msg,
                umail_conversations=umail_conversations,
                umail_bodies=umail_bodies,
            )
            if ai_reply:
                return jsonify({"message": ai_reply.strip()}), 200
            return jsonify({"error": "Cannot generate AI suggestion"}), 400
        except Exception as e:
            logger.error("AI suggest error: %s", e)
            return jsonify({"error": "Cannot generate AI suggestion"}), 500

    # -------------------- AI Suggestion & Auto-reply --------------------
    def auto_reply_umail_email(self, from_email):
        try:
            clientid = get_users_clients_id(email=from_email, user_id=self.userid)
            if not clientid:
                return jsonify({"error": "No email communication found"}), 404

            sorted_conversations = umail_get_sorted_lance_emails(
                connection=self.connection, user_id=self.userid, client_id=clientid
            )
            all_messages = [
                msg for conv in sorted_conversations for msg in conv.get("messages", [])
            ]
            if not all_messages:
                return jsonify({"error": "No messages found"}), 404

            latest_msg = all_messages[-1]
            if not can_reply_to_email(latest_msg.get("from")):
                return jsonify({"status": "cannot_reply"}), 200

            if latest_msg.get("direction") == "inbound":
                ai_reply = suggest_helper_base(
                    userid=self.userid,
                    email_msg=latest_msg["body"],
                    umail_conversations=all_messages,
                    umail_bodies=[msg.get("body") for msg in all_messages],
                )
                if ai_reply:
                    send_pilot_messages(
                        user_id=self.userid,
                        channel="gmail",
                        text=ai_reply,
                        conversation_id=latest_msg["conversation_id"],
                        b_connection=self.connection,
                        client_id=clientid,
                        user_email=latest_msg["to"],
                        client_email=latest_msg["from"],
                        subject=latest_msg["subject"],
                        thread_id=latest_msg["thread_id"],
                        ticket_id=latest_msg["ticket_id"],
                        ticket_name=latest_msg["ticket_name"],
                        is_reply=True,
                    )
                    return True
                return False
            return False
        except Exception as e:
            logger.error("Auto-reply error: %s", e)
            return None

    # -------------------- Autopilot Operations --------------------
    def activate_autopilot(self, from_email, selected_agents=None):
        autopilot_data, err, code = self.fetch_autopilot()
        if err:
            return jsonify(err), code

        mode = "all" if from_email == "ALL" else "dynamic"
        self.autopilot_data["mode"] = mode

        emails = [from_email] if isinstance(from_email, str) else from_email
        already_active = True

        for email in emails:
            existing_entry = next(
                (e for e in autopilot_data["logs"] if e["email"] == email), None
            )
            if (
                not existing_entry
                or existing_entry.get("status") != "active"
                or existing_entry.get("selected_agent") != selected_agents
            ):
                now = datetime.utcnow().isoformat()
                update_data = {
                    "email": email,
                    "status": "active",
                    "last-conv": None,
                    "last-msg": None,
                    "updated_at": now,
                    "selected_agent": selected_agents or self.userid,
                }
                if existing_entry:
                    idx = autopilot_data["logs"].index(existing_entry)
                    autopilot_data["logs"][idx] = update_data
                else:
                    autopilot_data["logs"].append(update_data)
                already_active = False

            helper_make_reply_email(
                userid=self.userid, from_email=email, n_connection=self.connection
            )

        if not already_active:
            self.persist_autopilot()

        msg = "autopilot already active" if already_active else "autopilot activated"
        return jsonify({"message": msg, "autopilot": autopilot_data}), 200

    def revoke_autopilot(self, target_email, pilot_override=False):
        autopilot_data, err, code = self.fetch_autopilot()
        if err:
            return jsonify(err), code

        emails = [target_email] if isinstance(target_email, str) else target_email
        logs = autopilot_data.get("logs", [])
        now = datetime.utcnow().isoformat()
        revoked_any = False

        for email in emails:
            if email == "ALL":
                for log in logs:
                    log["status"] = "revoked"
                    log["updated_at"] = now
                revoked_any = True
                if pilot_override:
                    autopilot_data["mode"] = "dynamic"
            else:
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
        self.autopilot_data = autopilot_data
        self.persist_autopilot()
        return (
            jsonify({"message": "Autopilot revoked", "autopilot": autopilot_data}),
            200,
        )

    def change_autopilot_mode(self, new_mode):
        autopilot_data, err, code = self.fetch_autopilot()
        if err:
            return jsonify(err), code

        current_mode = autopilot_data.get("mode")
        if current_mode == new_mode:
            return (
                jsonify({"message": f"Autopilot mode already set to '{new_mode}'"}),
                200,
            )

        autopilot_data["mode"] = new_mode
        self.autopilot_data = autopilot_data
        self.persist_autopilot()
        msg = f"Autopilot mode changed from '{current_mode}' to '{new_mode}'"
        return jsonify({"message": msg, "autopilot": autopilot_data}), 200

    def reset_autopilot(self):
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT user_id FROM users WHERE user_id = %s LIMIT 1", (self.userid,)
            )
            if not cursor.fetchone():
                return jsonify({"error": "User not found"}), 404

            cursor.execute(
                "UPDATE users SET autopilot = NULL WHERE user_id = %s", (self.userid,)
            )
            self.connection.commit()
        return jsonify({"message": f"Autopilot data reset for user {self.userid}"}), 200
