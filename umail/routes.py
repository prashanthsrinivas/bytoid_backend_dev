import asyncio
import time
from db.db_checkers import get_existing_umail_json
from flask import request, jsonify, Blueprint
from datetime import datetime, timezone
from utils.base_logger import get_logger
from cust_helpers import pathconfig
from utils.normal import ensure_dir
import json
from create_db import connect_to_rds
import os
from utils.s3_utils import upload_any_file, read_json_from_s3
import uuid
from gmail_route.routes import gmail_reply, send_mail
from zoho_routes.routes import send_zoho_email
from agent_route.task_manager import run_fetch_gmail_in_background
from umail_helper.asyn_functions import v2all_continuous
from umail_lance.umail_lance_agent import UmailLanceClient
from umail_helper.mails_process import update_config_file, generate_subject
from utils.redis_config import redis_config_glide
from utils.celery_base import acquire_user_lock, umail_sync

umail_bp = Blueprint("umail", __name__)
logger = get_logger(__name__)


# @umail_bp.route("/get_all_messages/<user_id>", methods=["GET"])
# def getall_route(user_id):
#     # enqueue Celery task
#     result = run_fetch_gmail_in_background(v2all_continuous, user_id)
#     return jsonify(result), 202


@umail_bp.route("/get_all_messages/<user_id>", methods=["GET"])
def getall_route(user_id):
    # Try to acquire lock first
    if not acquire_user_lock(user_id):
        # Lock exists → task is running or within TTL
        logger.info("get_all_messages Task already running currently for  %s", user_id)
        return (
            jsonify(
                {
                    "message": "Task already running or recently triggered",
                    "user_id": user_id,
                }
            ),
            202,  # Too Many Requests
        )

    # Lock acquired → enqueue Celery task
    async_result = umail_sync.delay(user_id)
    logger.info(
        "get_all_messages Task Started currently for  %s with task_id %s",
        user_id,
        async_result.id,
    )
    return (
        jsonify(
            {
                "message": "Fetch queued",
                "user_id": user_id,
                "task_id": async_result.id,
            }
        ),
        202,
    )


def get_latest_convo_info(config):
    """
    Get the latest conversation from a config file based on parsed_timestamp
    """

    if not config or "conversations" not in config:
        return None

    config_data = config.get("conversations", [])
    client_id = config.get("userclients_id", [])

    if not config_data:
        print("[DEBUG] input_data is empty")
        return None

    conversations = {}

    for msg in config_data:
        conv_id = msg.get("conv_id")
        ts_str = msg.get("parsed_timestamp")

        if not conv_id or not ts_str:
            continue

        try:
            msg_ts = datetime.fromisoformat(ts_str)

            if (
                conv_id not in conversations
                or msg_ts > conversations[conv_id]["parsed_timestamp"]
            ):
                conversations[conv_id] = {"message": msg, "parsed_timestamp": msg_ts}
        except Exception as e:
            print(f"[WARN] Failed to parse parsed_timestamp '{ts_str}': {e}")
            continue

    if not conversations:
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
            parts = key[len(base_prefix) :].split("/")
            if parts and parts[0]:
                client_ids.add(parts[0])
    return list(client_ids)


# @umail_bp.route("/conversations/<user_id>/<next_cursor>", methods=["GET"])
# def get_latest_conversations(user_id, next_cursor):
#     """
#     Get the latest conversation from each client's config file and load full conversation data
#     """
#     disp_messages = []
#     print(f"next_cursor from api: {next_cursor}")
#     connection = connect_to_rds()
#     if connection is None:
#         print("❌ [DEBUG] Database connection failed")
#         return jsonify({"error": "Database connection failed"}), 500
#     cursor = connection.cursor()

#     client = UmailLanceClient(user_id)

#     convo_messages, next_cursor = client.latest_messages_from_lance(
#         user_id, next_cursor
#     )

#     for folder, data in convo_messages.items():
#         latest_timestamp = data["ts"]
#         conv_id = data["conv_id"]
#         latest_msg_in_conv = data["latest_message"]

#         if not latest_timestamp:
#             continue

#         now = datetime.now(timezone.utc)
#         time_diff = now - latest_timestamp

#         if time_diff.days > 0:
#             relative_time = f"{time_diff.days} days ago"
#         elif time_diff.seconds > 3600:
#             relative_time = f"{time_diff.seconds // 3600} hours ago"
#         elif time_diff.seconds > 60:
#             relative_time = f"{time_diff.seconds // 60} minutes ago"
#         else:
#             relative_time = "Just now"

#         user_email = latest_msg_in_conv.get("user_id", "Unknown")
#         from_email = latest_msg_in_conv.get("from", "")
#         to_email = latest_msg_in_conv.get("to", "")

#         contact_email = from_email if from_email != user_email else to_email

#         cursor.execute(
#             """
#                                 SELECT uc.first_name
#                                 FROM users_clients uc
#                                 JOIN messages m ON uc.users_clients_id = m.sender_id
#                                 WHERE m.conversation_id_fk = %s
#                                 LIMIT 1
#                                 """,
#             (conv_id,),
#         )

#         client_name_row = cursor.fetchone()

#         client_name = client_name_row[0] if client_name_row else "Unknown"

#         disp_message = {
#             "contact_id": contact_email,
#             "name": client_name,
#             "lastMessage": latest_msg_in_conv.get("body", "")[:100],
#             "timestamp": relative_time,
#             "isoTimestamp": latest_msg_in_conv.get("timestamp"),
#             "channel": latest_msg_in_conv.get("source"),
#             "subject": latest_msg_in_conv.get("subject"),
#             "conv_id": conv_id,
#             "ticket_id": latest_msg_in_conv.get("ticket_id"),
#             "ticket_name": latest_msg_in_conv.get("ticket_name"),
#             "full_conversation": convo_messages,
#         }
#         # print(f"names are: {client_name}")
#         disp_messages.append(disp_message)
#     print(f"next_cursor returned to api :{next_cursor}")
#     connection.close()
#     disp_messages.sort(key=lambda x: x["isoTimestamp"], reverse=True)
#     # print(f" disp messages are: {disp_messages}")
#     return {"disp_messages": disp_messages, "next_cursor": next_cursor}

from glide import GlideClusterClient

# addresses = [
#     NodeAddress("bytoidcache-w2ofwh.serverless.cac1.cache.amazonaws.com", 6379)
# ]

# config_glide = GlideClusterClientConfiguration(addresses=addresses, use_tls=True)


def normalize_timestamp(ts):
    """
    Ensure timestamp is always timezone-aware datetime.
    Accepts string (ISO) or datetime object.
    """
    if isinstance(ts, str):
        # Convert ISO string to datetime
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    elif isinstance(ts, datetime):
        dt = ts
    else:
        return None

    # Make sure it's timezone-aware
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt


def format_relative_time(ts):
    """
    Return human-readable relative time ("2 days ago", "5 minutes ago").
    """
    if not ts:
        return "Unknown"

    now = datetime.now(timezone.utc)
    time_diff = now - ts

    if time_diff.days > 0:
        return f"{time_diff.days} days ago"
    elif time_diff.seconds > 3600:
        return f"{time_diff.seconds // 3600} hours ago"
    elif time_diff.seconds > 60:
        return f"{time_diff.seconds // 60} minutes ago"
    else:
        return "Just now"


def handle_cache_data(groupedmessages, disp_messages, next_cursor, source):
    connection = connect_to_rds()
    if connection is None:
        print("❌ [DEBUG] Database connection failed")
        return jsonify({"error": "Database connection failed"}), 500
    cursor = connection.cursor()

    for conv_id, data in groupedmessages.items():
        messages = next(iter(data.values()), [])
        if not messages:
            continue

        latest_msg_in_conv = messages[-1]
        ts = normalize_timestamp(latest_msg_in_conv.get("timestamp"))
        if not ts:
            continue

        relative_time = format_relative_time(ts)

        user_email = latest_msg_in_conv.get("user_id", "Unknown")
        from_email = latest_msg_in_conv.get("from", "")
        to_email = latest_msg_in_conv.get("to", "")
        contact_email = from_email if from_email != user_email else to_email

        cursor.execute(
            """
            SELECT uc.first_name
            FROM users_clients uc
            JOIN messages m ON uc.users_clients_id = m.sender_id
            WHERE m.conversation_id_fk = %s
            LIMIT 1
            """,
            (conv_id,),
        )
        client_name_row = cursor.fetchone()
        client_name = client_name_row[0] if client_name_row else contact_email

        disp_message = {
            "contact_id": contact_email,
            "name": client_name,
            "lastMessage": latest_msg_in_conv.get("body", "")[:100],
            "timestamp": relative_time,
            "isoTimestamp": ts.isoformat(),
            "channel": latest_msg_in_conv.get("source"),
            "subject": latest_msg_in_conv.get("subject"),
            "conv_id": conv_id,
            "ticket_id": latest_msg_in_conv.get("ticket_id") or "creating",
            "ticket_name": latest_msg_in_conv.get("ticket_name"),
            "full_conversation": data,
            "source": source,
        }
        disp_messages.append(disp_message)

    connection.close()
    disp_messages.sort(key=lambda x: x["isoTimestamp"], reverse=True)

    print(f"next_cursor returned to api: {next_cursor}")
    return {"disp_messages": disp_messages, "next_cursor": next_cursor}


def handle_lance_data(convo_messages, disp_messages, next_cursor, source):
    connection = connect_to_rds()
    if connection is None:
        print("❌ [DEBUG] Database connection failed")
        return jsonify({"error": "Database connection failed"}), 500
    cursor = connection.cursor()

    for folder, data in convo_messages.items():
        ts = normalize_timestamp(data["ts"])
        if not ts:
            continue

        relative_time = format_relative_time(ts)
        conv_id = data["conv_id"]
        latest_msg_in_conv = data["latest_message"]

        user_email = latest_msg_in_conv.get("user_id", "Unknown")
        from_email = latest_msg_in_conv.get("from", "")
        to_email = latest_msg_in_conv.get("to", "")
        contact_email = from_email if from_email != user_email else to_email

        cursor.execute(
            """
            SELECT uc.first_name
            FROM users_clients uc
            JOIN messages m ON uc.users_clients_id = m.sender_id
            WHERE m.conversation_id_fk = %s
            LIMIT 1
            """,
            (conv_id,),
        )
        client_name_row = cursor.fetchone()
        # client_name = client_name_row[0] if client_name_row else contact_email
        client_name = (
            client_name_row[0]
            if client_name_row and client_name_row[0].strip()
            else contact_email
        )

        disp_message = {
            "contact_id": contact_email,
            "name": client_name,
            "lastMessage": latest_msg_in_conv.get("body", "")[:100],
            "timestamp": relative_time,
            "isoTimestamp": ts.isoformat(),
            "channel": latest_msg_in_conv.get("source"),
            "subject": latest_msg_in_conv.get("subject"),
            "conv_id": conv_id,
            "ticket_id": latest_msg_in_conv.get("ticket_id"),
            "ticket_name": latest_msg_in_conv.get("ticket_name"),
            "full_conversation": convo_messages,
            "source": source,
        }
        # print(f"{conv_id} fetched time {relative_time}")
        disp_messages.append(disp_message)

    connection.close()
    disp_messages.sort(key=lambda x: x["isoTimestamp"], reverse=True)

    print(f"next_cursor returned to api :{next_cursor}")
    return {"disp_messages": disp_messages, "next_cursor": next_cursor}


def parse_cursor_to_datetime(cursor):
    """Return timezone-aware datetime from cursor (ISO string or epoch-ms)."""
    if cursor is None:
        return None

    # epoch in ms or s?
    if isinstance(cursor, (int, float)):
        # assume ms if > 10^11
        ts = cursor / 1000 if cursor > 1e11 else cursor
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    if isinstance(cursor, str) and cursor.isdigit():
        ts = int(cursor)
        ts = ts / 1000 if ts > 1e11 else ts
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    # otherwise ISO string
    try:
        return datetime.fromisoformat(cursor)
    except ValueError:
        return datetime.fromisoformat(cursor.replace("Z", "+00:00"))


@umail_bp.route("/conversations/<user_id>/<next_cursor>", methods=["GET"])
def get_latest_conversations(user_id, next_cursor):
    """
    Get the latest conversation from each client's config file.
    Priority:
    1. Local JSON (get_existing_umail_json)
    2. Cache (GlideClusterClient)
    3. Lance (fallback)
    Always return flattened disp_messages format.
    """

    print(f"next_cursor from api: {next_cursor}")
    display_messages = []
    convo_messages = {}
    cached = None

    # ✅ Step 1: Local JSON
    existing_json = get_existing_umail_json(user_id)
    print("data added", existing_json)
    if not existing_json:
        # ✅ Step 2: Cache
        getall_route(user_id=user_id)

        def get_from_cache_sync(user_id):
            async def _inner():
                client = await GlideClusterClient.create(redis_config_glide)
                return await client.get(f"{user_id}")

            return asyncio.run(_inner())

        cached = get_from_cache_sync(user_id)
        print("⚡ Using cached Gmail data")
        if cached:
            cached_json = json.loads(cached) or {}
            if isinstance(cached_json, list):
                cached_json = cached_json[0] if cached_json else {}
            convo_messages = cached_json.get("grouped_messages", {})
            next_cursor = cached_json.get("next_page_token")
            source = "mid"
            return handle_cache_data(
                groupedmessages=convo_messages,
                disp_messages=display_messages,
                next_cursor=next_cursor,
                source=source,
            )
        else:
            return getall_route(user_id)

    else:
        # ✅ Step 3: Lance fallback
        client = UmailLanceClient(user_id)
        convo_messages, bnext_cursor = client.latest_messages_from_lance(
            user_id, next_cursor
        )
        getall_route(user_id)
        if not convo_messages:
            if next_cursor:
                cursor_dt = parse_cursor_to_datetime(next_cursor)
                if cursor_dt and cursor_dt.date() == datetime.now(timezone.utc).date():
                    logger.info(
                        "Backfall process created if lance table deleted. started"
                    )
                    return getall_route(user_id)
            logger.info("No messages from lance")

        # If nothing matched, return a clean response
        source = "full"
        print(f"return data lenght from get_latest: {len(display_messages)}")
        return handle_lance_data(
            convo_messages=convo_messages,
            disp_messages=display_messages,
            next_cursor=bnext_cursor,
            source=source,
        )


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


@umail_bp.route("/selected_conversation/<conversation_id>/<user_id>", methods=["GET"])
def get_selected_conv(conversation_id, user_id):
    """
    Fetch selected conversation messages for a user.
    Priority:
    1. Local JSON
    2. Cache
    3. Lance fallback
    """
    snooze_flag = False
    try:
        print(
            f"inside get_selected_conv for conversation_id={conversation_id}, user_id={user_id}"
        )
        invalid_values = {None, "", "none", "null", "undefined"}
        if (
            conversation_id is None
            or str(conversation_id).strip().lower() in invalid_values
            or user_id is None
            or str(user_id).strip().lower() in invalid_values
        ):
            return jsonify({"error": "conversation_id and user_id are required"}), 400

        client_id = None

        # ✅ Step 2: Local JSON
        existing_json = get_existing_umail_json(user_id)

        if not existing_json:
            print("fetching from cache")

            # ✅ Step 3: Cache
            def get_from_cache_sync(user_id):
                async def _inner():
                    client = await GlideClusterClient.create(redis_config_glide)
                    return await client.get(f"{user_id}")

                return asyncio.run(_inner())

            cached = get_from_cache_sync(user_id)
            if cached:
                try:
                    cached_json = json.loads(cached)
                    if isinstance(cached_json, list):  # handle list case
                        cached_json = cached_json[0] if cached_json else {}

                    grouped = cached_json.get("grouped_messages", {})
                    if (
                        isinstance(grouped, dict)
                        and conversation_id in grouped
                        and grouped[conversation_id]  # non-empty
                    ):
                        messages_data = grouped[conversation_id]
                        source = "mid"
                        return _format_selected_conversation(
                            conversation_id, client_id, messages_data, source
                        )
                    else:
                        print("⚠️ Cache miss or invalid data, falling back to Lance")
                except Exception as e:
                    print(f"⚠️ Cache parse error: {e}, falling back to Lance")
            else:
                print("⚠️ No cache found, falling back to Lance")

        else:
            logger.info("fetching from lance %s", conversation_id)
            # ✅ Step 1: Try to get client_id from DB
            try:
                connection = connect_to_rds()
                if connection is None:
                    return jsonify({"error": "Database connection failed"}), 500
                cursor = connection.cursor()
                cursor.execute(
                    "SELECT sender_id FROM messages WHERE conversation_id_fk = %s",
                    (conversation_id,),
                )
                client_id_row = cursor.fetchone()
                if not client_id_row:
                    # wait a bit and try again
                    logger.info("Retrying the client fetch from db")
                    time.sleep(5)
                    cursor.execute(
                        "SELECT sender_id FROM messages WHERE conversation_id_fk = %s",
                        (conversation_id,),
                    )
                    client_id_row = cursor.fetchone()

                if not client_id_row:
                    logger.info("still cant get ID")
                    return (
                        jsonify(
                            {
                                "message": f"⚠️ No sender_id found for conversation_id {conversation_id}"
                            }
                        ),
                        404,
                    )
                client_id = client_id_row[0]
            except Exception as e:
                return (
                    jsonify({"message": f"❌ Error executing sender_id query: {e}"}),
                    500,
                )
                # print(f"❌ Error executing sender_id query: {e}")

            client = UmailLanceClient(user_id)
            recent_msg = client.get_selected_conv_from_lance(user_id, client_id)

            all_messages = []
            for conv_id, messages_list in recent_msg.items():
                try:
                    # 🔥 Deduplicate messages
                    unique_messages = {}
                    for msg in messages_list:
                        msg_id = (
                            msg.get("id")
                            or f"{msg.get('timestamp')}-{msg.get('sender')}"
                        )
                        if msg_id not in unique_messages:
                            unique_messages[msg_id] = msg

                    messages = list(unique_messages.values())
                    channel = messages[0].get("source") if messages else "unknown"
                    ticket_id = messages[0].get("ticket_id")

                    cursor.execute(
                        "SELECT assignee FROM tickets WHERE tickets_id = %s",
                        (ticket_id,),
                    )
                    t_row = cursor.fetchone()
                    assigned_id = ""
                    if t_row:
                        assigned_id = t_row[0]
                    assignee_full_name = ""
                    if assigned_id:

                        cursor.execute(
                            "SELECT first_name, last_name, email FROM users WHERE user_id = %s",
                            (assigned_id,),
                        )
                        names = cursor.fetchone()
                        if names:
                            first_name, last_name, assignee_email = (
                                names[0],
                                names[1],
                                names[2],
                            )
                            if not first_name or first_name == "None":
                                first_name = assignee_email.split("@")[0]
                            if not last_name or last_name == "None":
                                last_name = ""

                            assignee_full_name = (first_name + " " + last_name).strip()

                    all_messages.append(
                        {
                            "id": conv_id,
                            "channel": channel,
                            "messages": messages,
                            "assigned_name": assignee_full_name,
                        }
                    )
                except Exception as e:
                    print(f"❌ Failed to read or parse {e}")
                    continue

                sorted_conversations = sorted(
                    all_messages,
                    key=lambda conv: (
                        max(msg.get("timestamp") for msg in conv.get("messages", []))
                        if conv.get("messages")
                        else ""
                    ),
                    reverse=False,
                )

            # check whether the client is snoozed or not

            cursor.execute(
                "SELECT snooze FROM users_clients WHERE users_clients_id = %s",
                (client_id,),
            )
            snooze_row = cursor.fetchone()
            if snooze_row:
                snooze_flag = bool(snooze_row[0])  # Convert 0/1 to False/True
                print(f"snooze_flag : {snooze_flag}")
            else:
                print(f"[DEBUG] No snooze row found for client_id: {client_id}")

            return (
                jsonify(
                    {
                        "identities": [],
                        "status": "existing",
                        "conversationId": client_id,  # conversation_id is client_id
                        "messages": sorted_conversations,  # this is ConversationThread[]
                        "source": "full",
                        "snoozed": snooze_flag,
                    }
                ),
                200,
            )

        cursor.close()
        connection.close()
        return jsonify({"error": "Conversation not found"}), 404

    except Exception as e:
        print(f"❌ Unexpected error in get_selected_conv(): {e}")
        cursor.close()
        connection.close()
        return jsonify({"error": "Internal server error"}), 500


def _format_selected_conversation(conversation_id, client_id, messages_data, source):
    """
    Format messages_data (dict of channels -> messages OR list of messages) into API response structure
    """
    all_messages = []
    print("message data", type(messages_data))

    if isinstance(messages_data, dict):
        iterable = messages_data.items()
    elif isinstance(messages_data, list):
        # If list, wrap the whole list as one "conversation"
        iterable = [("0", messages_data)]
    else:
        iterable = []

    for conv_id, messages_list in iterable:
        try:
            # normalize messages: ensure list of dicts
            messages = []
            for m in messages_list or []:
                if isinstance(m, dict):
                    messages.append(m)
                else:
                    # fallback if it's just a string
                    messages.append(
                        {"text": str(m), "source": "unknown", "timestamp": ""}
                    )

            channel = messages[0].get("source") if messages else "unknown"

            all_messages.append(
                {
                    "id": conv_id,
                    "channel": channel,
                    "messages": messages,
                }
            )
        except Exception as e:
            print(f"❌ Failed to read or parse {e}")
            continue

    sorted_conversations = sorted(
        all_messages,
        key=lambda conv: (
            max(
                msg.get("timestamp")
                for msg in conv.get("messages", [])
                if msg.get("timestamp")
            )
            if conv.get("messages")
            else ""
        ),
        reverse=True,
    )

    return (
        jsonify(
            {
                "identities": [],
                "status": "existing",
                "conversationId": client_id or conversation_id,
                "messages": sorted_conversations,
                "source": source,
            }
        ),
        200,
    )


@umail_bp.route("/start-conversation", methods=["POST"])
def start_conversation():

    data = request.get_json() or {}
    user_id = data.get("user_id")
    client_id = data.get("contact_id")
    print(f"client_id : {client_id}")

    if not user_id or not client_id:
        return jsonify({"error": "Missing user_id or contact_id"}), 400

    try:
        connection = connect_to_rds()
        if connection is None:
            return jsonify({"error": "Database connection failed"}), 500
        cursor = connection.cursor()

        client = UmailLanceClient(user_id)
        print("calling get_selected_conv_from_lance")
        recent_msg = client.get_selected_conv_from_lance(user_id, client_id)
        if recent_msg is None:
            print(f"recent_msg is none , id : {client_id}")
            return (
                jsonify(
                    {
                        "identities": [],
                        "status": "new",
                        "conversationId": client_id,  # conversatoin_id is client_id in this function
                        "messages": [],
                    }
                ),
                200,
            )

        all_messages = []
        for conv_id, messages_list in recent_msg.items():
            try:
                messages = []
                for message in messages_list:
                    messages.append(message)

                    channel = messages[0].get("source") if messages else "unknown"

                all_messages.append(
                    {
                        "id": conv_id,
                        "channel": channel,
                        "messages": messages,
                    }
                )

            except Exception as e:
                print(f"❌ Failed to read or parse {e}")
                continue
        # return jsonify(messages)
        return (
            jsonify(
                {
                    "identities": [],
                    "status": "existing",
                    "conversationId": client_id,
                    "messages": all_messages,  # this is ConversationThread[]
                }
            ),
            200,
        )

    except Exception as e:
        print(f"❌ Unexpected error in get_selected_conv(): {e}")
        return jsonify({"error": "Internal server error"}), 500


def match_email_to_channel(email, channel):
    """Returns True if the email matches the given channel."""
    if not email or "@" not in email:
        return False
    domain = email.lower().split("@")[1]
    return channel.lower() in domain


@umail_bp.route("/send-reply", methods=["POST"])
def send_messages():
    print("🚀 [DEBUG] Starting send_messages() function")

    try:
        # Parse request data
        data = request.json
        print(f"📥 [DEBUG] Request data: {data}")

        user_id = data.get("user_id")
        channel = data.get("channel")
        text = data.get("text")
        ticket_conversation_id = data.get("ticket_conversation_id")
        contact_id = data.get("contact_id")
        conv_id = data.get("conversation_id")

        status = None
        client_id = None

        connection = connect_to_rds()
        if connection is None:
            print("❌ [DEBUG] Database connection failed")
            return jsonify({"error": "Database connection failed"}), 500
        print("✅ [DEBUG] Database connection successful")

        cursor = connection.cursor()

        c_id = ticket_conversation_id if ticket_conversation_id else conv_id
        print(f"🔍 [DEBUG] Querying for sender_id with conversation_id: {c_id}")
        cursor.execute(
            "SELECT sender_id FROM messages WHERE conversation_id_fk = %s",
            (c_id,),
        )
        client_id_row = cursor.fetchone()

        if client_id_row is None:
            status = "new"
        else:
            status = "existing"
            client_id = client_id_row[0]

        if status == "existing":
            # Validate required fields
            if not all([user_id, channel, text, status]):
                missing_fields = []
                if not user_id:
                    missing_fields.append("user_id")
                if not channel:
                    missing_fields.append("channel")
                if not text:
                    missing_fields.append("text")
                if not status:
                    missing_fields.append("status")
                print(f"❌ [DEBUG] Missing required fields: {missing_fields}")
                return jsonify({"error": "Missing required payload fields"}), 400

            print("✅ [DEBUG] All required fields present")

        # Database connection
        print("🔗 [DEBUG] Attempting database connection...")

        # Initialize variables
        ticket_id = conversation_id = ticket_name = subject = thread_id = None
        is_reply = False
        print(f"🔄 [DEBUG] Initialized variables - is_reply: {is_reply}")

        if status == "existing":
            print("🔍 [DEBUG] Processing existing conversation")

            try:
                s3_config_key = f"{user_id}/messages/{client_id}/config.json"
                print(f"🔍 [DEBUG] Reading config from S3 key: {s3_config_key}")
                config_data = read_json_from_s3(s3_config_key)
                print(
                    f"🔍 [DEBUG] Config data retrieved: {len(config_data.get('conversations', []))} conversations found"
                )

                # Checking if it is a reply and getting reply info
                conv_list = config_data.get("conversations", [])
                print(
                    f"🔍 [DEBUG] Searching for conversation in {len(conv_list)} conversations"
                )

                for i, conv in enumerate(conv_list):
                    print(
                        f"🔍 [DEBUG] Checking conversation {i}: conv_id={conv.get('conv_id')}"
                    )
                    if conv.get("conv_id") == c_id:
                        thread_id = conv.get("thread_id") or ""
                        ticket_id = conv.get("ticket_id") or ""
                        print(f"*******ticket_id : {ticket_id}")
                        ticket_name = conv.get("ticket_name") or ""
                        subject = conv.get("subject")
                        conversation_id = c_id
                        is_reply = True
                        print(
                            f"✅ [DEBUG] Found matching conversation - thread_id: {thread_id}, ticket_id: {ticket_id}"
                        )
                        print(f"✅ [DEBUG] subject: {subject}, is_reply: {is_reply}")
                        break

                if not is_reply:
                    print("⚠️ [DEBUG] No matching conversation found in config")

            except FileNotFoundError:
                print(
                    f"⚠️ [DEBUG] Config file not found at {s3_config_key} — treating as user-initiated"
                )
            except Exception as e:
                print(f"❌ [DEBUG] Error checking config file for reply status: {e}")

        else:
            print("🆕 [DEBUG] Processing new conversation")
            conversation_id = str(uuid.uuid4())
            client_id = contact_id
            print(
                f"🆕 [DEBUG] Generated new conversation_id: {conversation_id}, client_id: {client_id}"
            )

        # File path setup
        conv_folder = os.path.join(pathconfig.basepath, "messages", user_id, client_id)
        print(f"📁 [DEBUG] Conversation folder: {conv_folder}")
        ensure_dir(conv_folder)
        file_name = f"{conversation_id}.json"
        conv_filepath = os.path.join(conv_folder, file_name)
        s3_conv_key = f"{user_id}/messages/{client_id}/{conversation_id}.json"
        print(f"📁 [DEBUG] Conversation file path: {conv_filepath}")
        print(f"📁 [DEBUG] S3 conversation key: {s3_conv_key}")

        # Load existing conversation data
        try:
            print(f"📖 [DEBUG] Attempting to read existing conversation from S3...")
            raw_data = read_json_from_s3(s3_conv_key)
            input_data = raw_data.get("input_data", [])
            print(f"📖 [DEBUG] Loaded {len(input_data)} existing messages")
        except Exception as e:
            print(f"📖 [DEBUG] No existing conversation found, starting fresh: {e}")
            input_data = []

        print(f"📖 [DEBUG] input_data length: {len(input_data)}")

        # Get user email
        print("📧 [DEBUG] Retrieving user email...")
        cursor.execute("SELECT email FROM users WHERE user_id = %s", (user_id,))
        u_email = cursor.fetchone()
        user_email = None
        if u_email:
            user_email = u_email[0]
            print(f"📧 [DEBUG] User email: {user_email}")
        else:
            print("⚠️ [DEBUG] No user email found")

        # Get business email
        # print("🏢 [DEBUG] Retrieving business email...")
        # cursor.execute(
        #     "SELECT BusinessEmail FROM business_info WHERE user_id_fk = %s", (user_id,)
        # )
        # b_email = cursor.fetchone()
        # if not b_email:
        #     print("❌ [DEBUG] No email found from business_info table")
        #     return jsonify({"error": "No business email found"}), 500
        # business_email = b_email[0]
        # print(f"🏢 [DEBUG] Business email: {business_email}")

        # Select appropriate email based on channel
        try:
            print(f"🔍 [DEBUG] Matching email to channel: {channel}")
            selected_email = None
            if match_email_to_channel(user_email, channel):
                selected_email = user_email
                print(f"✅ [DEBUG] Selected user email: {selected_email}")
            # elif match_email_to_channel(business_email, channel):
            #     selected_email = business_email
            #     print(f"✅ [DEBUG] Selected business email: {selected_email}")
            else:
                print(f"⚠️ [DEBUG] No email matched to channel {channel}")

        except Exception as e:
            print(
                f"🔥 [DEBUG] Exception occurred while selecting email by channel: {str(e)}"
            )

        # Get client email
        print(f"👤 [DEBUG] Retrieving client email for client_id: {client_id}")
        cursor.execute(
            "SELECT email_id FROM users_clients WHERE users_clients_id = %s",
            (client_id,),
        )
        c_email = cursor.fetchone()
        client_email = None
        if c_email:
            client_email = c_email[0]
            print(f"👤 [DEBUG] Client email: {client_email}")
        else:
            print("❌ [DEBUG] No client email found")

        # Subject generation for new conversations
        if not is_reply:
            print("📝 [DEBUG] Generating subject for new conversation...")

            # Create initial message structure
            now_utc = datetime.now(timezone.utc)
            formatted_time = now_utc.isoformat(timespec="seconds")
            msg_id = str(uuid.uuid4())

            print(
                f"📝 [DEBUG] Created message ID: {msg_id}, timestamp: {formatted_time}"
            )

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
            print(
                f"💾 [DEBUG] Saving temporary file for subject generation: {conv_filepath}"
            )
            with open(conv_filepath, "w", encoding="utf-8") as f:
                json.dump({"new_messages": [initial_message]}, f, indent=2)

            # Generate subject using AI
            print("🤖 [DEBUG] Calling generate_subject()...")
            subjects = generate_subject(user_id, conv_filepath, channel)
            print(f"🤖 [DEBUG] Generated subjects: {subjects}")

            # Get subject from AI response
            subject = None
            for group in subjects:
                if msg_id in group.get("message_ids", []):
                    subject = group.get("summary")
                    print(f"✅ [DEBUG] Found subject: {subject}")
                    break

            if not subject:
                subject = f"Message from {client_email}"  # Fallback subject
                print(f"⚠️ [DEBUG] Using fallback subject: {subject}")
        else:
            print(f"📝 [DEBUG] Using existing subject for reply: {subject}")

        # Create final message object
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
            "ticket_name": ticket_name,
        }
        print(f"📧 [DEBUG] Final message object created: {message}")

        # CRITICAL: Check if this message already exists in input_data
        cursor.execute("SELECT 1 FROM messages WHERE message_id = %s", (msg_id,))
        m_id = cursor.fetchone()
        if m_id:
            response_data = {
                "status": "duplicate message",
                "id": msg_id,
                "channel": channel,
                "conversationId": conversation_id,
                "is_reply": is_reply,
            }
            return jsonify(response_data), 200

        # Send the message via appropriate channel
        sent_message_id, sent_thread_id = None, None

        print(f"🚀 [DEBUG] Sending message via channel: {channel}")

        if channel == "gmail":
            print("📧 [DEBUG] Processing Gmail send...")
            if thread_id:
                if isinstance(input_data, dict):
                    # input_data is a single message dict
                    latest_msg = input_data
                elif isinstance(input_data, list):
                    # input_data is a list of message dicts
                    valid_messages = [
                        msg
                        for msg in input_data
                        if isinstance(msg, dict) and "timestamp" in msg
                    ]
                    if not valid_messages:
                        print("❌ [DEBUG] No valid messages found in input_data")
                        return jsonify({"error": "No valid messages found"}), 400
                    latest_msg = max(
                        valid_messages,
                        key=lambda msg: datetime.fromisoformat(msg["timestamp"]),
                    )
                else:
                    print("❌ [DEBUG] input_data is neither dict nor list")
                    return jsonify({"error": "Invalid input_data format"}), 400
                latest_id = latest_msg["id"]
                print(f"📧 [DEBUG] Latest message ID: {latest_id}")

                # Getting the reply subject
                latest_subject = latest_msg["subject"].strip()
                if not latest_subject.lower().startswith("re:"):
                    reply_subject = f"Re: {latest_subject}"
                else:
                    reply_subject = latest_subject
                print(f"📧 [DEBUG] Reply subject: {reply_subject}")

                try:
                    print(f"📧 [DEBUG] Calling gmail_reply()...")
                    sent_message_id = gmail_reply(
                        user_id,
                        to=client_email,
                        subject=reply_subject,
                        thread_id=thread_id,
                        body_text=text,
                        in_reply_to=latest_id,
                    )
                    message["id"] = sent_message_id
                    print(
                        f"✅ [DEBUG] Gmail reply sent successfully, message_id: {sent_message_id}"
                    )

                except Exception as e:
                    print(f"❌ [DEBUG] Gmail reply failed: {e}")
                    return jsonify({"error": "Gmail send failed"}), 500

            else:
                print("📧 [DEBUG] Sending new Gmail message...")
                try:
                    print(f"📧 [DEBUG] Calling send_mail()...")
                    result = send_mail(
                        user_id,
                        to=client_email,
                        subject=subject,
                        body_text=text,
                    )
                    send_status = result.get("status")
                    if send_status == "success":

                        sent_message_id = result.get("message_id")
                        sent_thread_id = result.get("thread_id")
                        message["id"] = sent_message_id
                        message["thread_id"] = sent_thread_id
                        print(
                            f"✅ [DEBUG] Gmail message sent, message_id: {sent_message_id}, thread_id: {sent_thread_id}"
                        )
                    else:
                        print("sending through gmail failed")
                        return

                except Exception as e:
                    print(f"❌ [DEBUG] Gmail send failed: {e}")
                    return jsonify({"error": "Gmail send failed"}), 500

        elif channel == "zoho":
            print("📧 [DEBUG] Processing Zoho send...")
            try:
                print(f"📧 [DEBUG] Calling send_zoho_email()...")
                response_payload, status_code = send_zoho_email(
                    user_id=user_id,
                    to_email=client_email,
                    subject=subject,
                    body_text=text,
                    from_user_email=selected_email,
                )
                print(
                    f"📧 [DEBUG] Zoho response - status: {status_code}, payload: {response_payload}"
                )

                if status_code in [200, 201]:
                    message_id = response_payload.get("message_id")
                    print(
                        f"✅ [DEBUG] Zoho message sent successfully, message_id: {message_id}"
                    )
                else:
                    print(
                        f"❌ [DEBUG] Zoho send failed: {response_payload.get('error')}"
                    )
                    return (
                        jsonify({"error": response_payload.get("error")}),
                        status_code,
                    )

            except Exception as e:
                print(f"❌ [DEBUG] Zoho send failed: {e}")
                return jsonify({"error": "Zoho send failed"}), 500

        else:
            print(f"❌ [DEBUG] Unsupported channel: {channel}")
            return jsonify({"error": "Unsupported channel"}), 400

        # Database updates
        print("💾 [DEBUG] Starting database updates...")
        updated_date = datetime.now(timezone.utc).isoformat()
        created_date = updated_date
        print(
            f"💾 [DEBUG] Timestamps - created: {created_date}, updated: {updated_date}"
        )

        try:
            if not is_reply:
                print("💾 [DEBUG] Inserting new thread...")
                cursor.execute(
                    """
                    INSERT INTO threads (conversation_id, started_at, status, last_message_at,external_user_id )
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (conversation_id, created_date, "Open", updated_date, user_id),
                )
                print("✅ [DEBUG] New thread inserted")
            else:
                print("💾 [DEBUG] Updating existing ticket and thread...")
                cursor.execute(
                    "UPDATE tickets SET updated_in = %s, status = %s WHERE conversation_id_fk = %s",
                    (updated_date, "In-Progress", conversation_id),
                )
                print("✅ [DEBUG] Ticket updated")

                cursor.execute(
                    "UPDATE threads SET last_message_at = %s WHERE conversation_id = %s",
                    (updated_date, conversation_id),
                )
                print("✅ [DEBUG] Thread updated")

            cont_ref = f"{user_id}/messages/{client_id}/{conversation_id}.json"
            print(f"💾 [DEBUG] Content reference: {cont_ref}")

            print("💾 [DEBUG] Inserting message record...")
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
                    cont_ref,
                    "outbound",
                    subject,
                    created_date,
                    updated_date,
                ),
            )
            print("✅ [DEBUG] Message record inserted")

            connection.commit()
            print("✅ [DEBUG] Database transaction committed")

        except Exception as e:
            connection.rollback()
            print(f"❌ [DEBUG] Database operation failed — rolled back: {e}")
            return jsonify({"error": "Database operation failed"}), 500

        # Update Conversation File
        print("📄 [DEBUG] Updating conversation file...")
        try:
            if isinstance(input_data, dict):
                input_data = [input_data]
            input_data.append(message)
            conversation_data = {"input_data": input_data}
            print(f"📄 [DEBUG] Total messages in conversation: {len(input_data)}")
            print(f"conversation_data : {conversation_data}")
            print(f"💾 [DEBUG] Writing to local file: {conv_filepath}")
            with open(conv_filepath, "w", encoding="utf-8") as f:
                json.dump(conversation_data, f, indent=2)

            print(f"☁️ [DEBUG] Uploading to S3: {s3_conv_key}")
            upload_any_file(
                conv_filepath,
                user_id,
                type="messages",
                s3_key_C=s3_conv_key,
            )
            print("✅ [DEBUG] Conversation file updated successfully")

        except Exception as e:
            print(f"❌ [DEBUG] Failed to update conversation file: {e}")
            return jsonify({"error": "Failed to save conversation"}), 500

        # updating lancedb
        lance_data = conversation_data.get("input_data", [])
        client = UmailLanceClient(user_id)
        client.embed_json_file_for_reply(
            lance_data, user_id, client_id, conversation_id
        )

        # Update Config File
        print("⚙️ [DEBUG] Updating config file...")
        config_folder = os.path.join(
            pathconfig.basepath, "messages", user_id, client_id
        )
        ensure_dir(config_folder)
        config_filepath = os.path.join(config_folder, "config.json")

        s3_config_key = f"{user_id}/messages/{client_id}/config.json"

        if status == "new":
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

            s3_data = read_json_from_s3(s3_config_key)
            if s3_data is None:

                upload_any_file(
                    config_filepath,
                    user_id,
                    type="messages",
                    s3_key_C=s3_config_key,
                )

        try:
            config_data = read_json_from_s3(s3_config_key)
            print(
                f"⚙️ [DEBUG] Existing config loaded with {len(config_data.get('conversations', []))} conversations"
            )
        except Exception as e:
            print(f"⚙️ [DEBUG] No config file found — creating new: {e}")
            config_data = {"userclients_id": client_id, "conversations": []}

        # Parse timestamp
        try:
            if updated_date.endswith("Z"):
                parsed_ts = datetime.fromisoformat(updated_date.replace("Z", "+00:00"))
            else:
                parsed_ts = datetime.fromisoformat(updated_date)
            print(f"⚙️ [DEBUG] Parsed timestamp: {parsed_ts.isoformat()}")
        except Exception as e:
            print(f"⚠️ [DEBUG] Could not parse updated_date '{updated_date}': {e}")
            parsed_ts = datetime.now(timezone.utc)

        # Create updated entry
        updated_entry = {
            "conv_id": conversation_id,
            "ticket_id": ticket_id,
            "ticket_name": ticket_name,
            "subject": subject,
            "channel": channel,
            "updated_date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "parsed_timestamp": parsed_ts.isoformat(),
        }
        print(f"*****ticket_id 2 : {ticket_id}")
        if channel == "gmail":
            if sent_thread_id:
                updated_entry["thread_id"] = sent_thread_id
                print(f"⚙️ [DEBUG] Using sent_thread_id: {sent_thread_id}")
            else:
                updated_entry["thread_id"] = thread_id
                print(f"⚙️ [DEBUG] Using existing thread_id: {thread_id}")
        else:
            updated_entry["thread_id"] = ""
            print("⚙️ [DEBUG] Non-Gmail channel, no thread_id")

        print(f"⚙️ [DEBUG] Updated entry: {updated_entry}")

        # Update or add conversation in config
        conversation_exists = False
        for i, conv in enumerate(config_data.get("conversations", [])):
            if conv.get("conv_id") == conversation_id:
                config_data["conversations"][i] = updated_entry
                conversation_exists = True
                print(f"⚙️ [DEBUG] Updated existing conversation at index {i}")
                break

        if not conversation_exists:
            config_data.setdefault("conversations", []).append(updated_entry)
            print("⚙️ [DEBUG] Added new conversation to config")

        config_data["userclients_id"] = client_id
        print(f"⚙️ [DEBUG] Calling update_config_file()...")
        update_config_file(user_id, client_id, config_data)
        print("✅ [DEBUG] Config file updated successfully")

        # Final Response
        response_data = {
            "status": "sent",
            "id": sent_message_id or msg_id,
            "channel": channel,
            "conversationId": conversation_id,
            "is_reply": is_reply,
        }
        print(f"🎉 [DEBUG] Function completed successfully - Response: {response_data}")
        return jsonify(response_data), 200

    except Exception as e:
        print(f"❌ [DEBUG] Unexpected error in send_messages(): {e}")
        import traceback

        print(f"❌ [DEBUG] Full traceback: {traceback.format_exc()}")
        return jsonify({"error": "Internal server error"}), 500

    finally:
        if "connection" in locals():
            connection.close()
            print("🔗 [DEBUG] Database connection closed")


@umail_bp.route("/async_message/<userid>", methods=["GET"])
def get_inbox_info(userid):
    try:
        result = run_fetch_gmail_in_background(v2all_continuous, userid)
        return jsonify({"result": result}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
