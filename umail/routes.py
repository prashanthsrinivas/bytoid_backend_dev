import asyncio
import time
from db.db_checkers import get_email_by_id, get_existing_umail_json
from flask import request, jsonify, Blueprint
from datetime import datetime, timezone
from services.redis_service import get_redis
from utils.base_logger import get_logger
from utils.permission_required import permission_required_body
from cust_helpers import pathconfig
from utils.normal import ensure_dir, parse_composite_user_id
import json
from db.rds_db import connect_to_rds
import os
from utils.s3_utils import upload_any_file, read_json_from_s3
import uuid
from gmail_route.routes import gmail_reply, send_mail
from zoho_routes.routes import send_zoho_email
from agent_route.task_manager import run_fetch_gmail_in_background
from umail_helper.asyn_functions import v2all_continuous
from umail_lance.umail_lance_agent import UmailLanceClient
from umail_helper.mails_process import (
    update_config_file,
    generate_subject,
    check_mailbox,
    get_integration_users,
)

from utils.celery_base import acquire_user_lock, next_monthemails, umail_sync
from umail_helper.sync_manager import SyncManager
from umail_helper.attachment_handler import (
    handle_attachment_upload,
    handle_multiple_attachments,
    create_attachment_metadata_for_message,
)
from microsoft_route.routes import outlook_send_mail
from request_context import current_user_id
from services.gmail_service import GmailService
from microsoft_route.microsoft_helpers import OutlookSubscriptionManager

umail_bp = Blueprint("umail", __name__)
logger = get_logger(__name__)


@umail_bp.route("/get_all_messages2/<user_id>", methods=["GET"])
@permission_required_body("taskbox.email.view")
def getall_route2(user_id):
    logged_in_user_id, user_id = parse_composite_user_id(user_id)
    # enqueue Celery task
    mailbox_setting = check_mailbox(user_id)
    if not mailbox_setting:
        return {"disp_messages": "Restricted"}

    result = run_fetch_gmail_in_background(v2all_continuous, user_id)
    return jsonify(result), 202


@umail_bp.route("/check_redis", methods=["GET"])
@permission_required_body("admin.manage_users")
async def check_redis():
    print("check redis initalized")
    val = get_redis()
    # print("in the checker redis", val)
    res = await val.checker()
    return jsonify({"redis state": res})


@umail_bp.route("/check_umail/<userid>", methods=["GET"])
@permission_required_body("taskbox.email.view")
async def check_uamil(userid):
    try:
        logged_in_user_id, user_id = parse_composite_user_id(userid)

        mailbox_setting = check_mailbox(user_id)

        return jsonify({"is_mail": mailbox_setting})

    except Exception as e:
        logger.info("error on check_umail %s", e)
        return jsonify({"error": str(e)}), 500


@umail_bp.route("/get_all_messages/<user_id>", methods=["GET"])
@permission_required_body("taskbox.email.view")
def getall_route(user_id):
    logged_in_user_id, user_id = parse_composite_user_id(user_id)
    # Try to acquire lock first
    mailbox_setting = check_mailbox(user_id)
    if not mailbox_setting:
        return {"disp_messages": "Restricted"}

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
        # print("[DEBUG] input_data is empty")
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
            # print(f"[WARN] Failed to parse parsed_timestamp '{ts_str}': {e}")
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
        # print("❌ [DEBUG] Database connection failed")
        return jsonify({"error": "Database connection failed"}), 500
    cursor = connection.cursor()

    for conv_id, data in groupedmessages.items():
        messages = next(iter(data.values()), [])
        if not messages:
            continue

        # Find the actual newest message by timestamp, not by position
        latest_msg_in_conv = max(messages, key=lambda x: x.get("timestamp", ""))
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
            # "full_conversation": data,
            "source": source,
        }
        disp_messages.append(disp_message)

    connection.close()
    disp_messages.sort(key=lambda x: x["isoTimestamp"], reverse=True)

    # print(f"next_cursor returned to api: {next_cursor}")
    return {"disp_messages": disp_messages, "next_cursor": next_cursor}


def handle_lance_data(convo_messages, next_cursor, userid, source):

    if not convo_messages:
        return {"disp_messages": [], "next_cursor": None}

    connection = connect_to_rds()
    if connection is None:
        return jsonify({"error": "Database connection failed"}), 500

    disp_messages = []
    main_email = get_email_by_id(userid)

    try:
        with connection.cursor() as cursor:

            for folder, data in convo_messages.items():

                ts = normalize_timestamp(data.get("ts"))
                if not ts:
                    continue

                conv_id = data.get("conv_id")
                latest_msg = data.get("latest_message", {})
                from_email = latest_msg.get("from", "")
                to_email = latest_msg.get("to", "")

                # Determine contact email correctly
                contact_email = from_email if from_email != main_email else to_email

                # Fetch client name
                cursor.execute(
                    """
                    SELECT uc.first_name
                    FROM users_clients uc
                    JOIN messages m
                      ON uc.users_clients_id = m.sender_id
                    WHERE m.conversation_id_fk = %s
                    LIMIT 1
                    """,
                    (conv_id,),
                )

                row = cursor.fetchone()
                client_name = (
                    row[0].strip()
                    if row and row[0] and row[0].strip()
                    else contact_email
                )

                message_data = {
                    "contact_id": contact_email,
                    "name": client_name,
                    "lastMessage": latest_msg.get("body", "")[:100],
                    "timestamp": format_relative_time(ts),
                    "isoTimestamp": ts.isoformat(),
                    "channel": latest_msg.get("source"),
                    "subject": latest_msg.get("subject"),
                    "conv_id": conv_id,
                    "ticket_id": latest_msg.get("ticket_id"),
                    "ticket_name": latest_msg.get("ticket_name"),
                    "source": source,
                }

                disp_messages.append(message_data)

    finally:
        connection.close()

    # 🔥 Global sort (newest first)
    disp_messages.sort(
        key=lambda x: datetime.fromisoformat(x["isoTimestamp"]),
        reverse=True,
    )

    # 🔥 Proper next cursor = oldest item in this page
    new_next_cursor = None
    if disp_messages:
        new_next_cursor = min(
            disp_messages,
            key=lambda x: datetime.fromisoformat(x["isoTimestamp"]),
        )["isoTimestamp"]

    return {
        "disp_messages": disp_messages,
        "next_cursor": new_next_cursor,
    }


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


@umail_bp.route("/conversations_og/<user_id>/<next_cursor>", methods=["GET"])
@permission_required_body("taskbox.email.view")
def get_latest_conversations_og(user_id, next_cursor):
    """
    Get the latest conversation from each client's config file.
    Priority:
    1. Fetch fresh Gmail emails (real-time)
    2. Local JSON (get_existing_umail_json)
    3. Cache (GlideClusterClient)
    4. Lance (fallback)
    Always return flattened disp_messages format.
    """

    # print(f"next_cursor from api: {next_cursor}")
    logged_in_user_id, user_id = parse_composite_user_id(user_id)
    display_messages = []
    convo_messages = {}
    cached = None

    def rediscync():
        def get_from_cache_sync(user_id):
            async def _inner():
                ## client = await GlideClusterClient.create(redis_config_glide)
                # print("fetching from cache ")
                client = get_redis()
                return await client.get(f"umail_{user_id}")

            return asyncio.run(_inner())

        cached = get_from_cache_sync(user_id)
        # print("⚡ Using cached Gmail data")
        if cached:
            # print("cached got")
            cached_json = cached  # already a dict
            if isinstance(cached_json, list):
                cached_json = cached_json[0] if cached_json else {}
            convo_messages = cached_json.get("grouped_messages", {})
            next_cursor = cached_json.get("next_page_token")
            # print("conv", len(convo_messages))
            source = "mid"
            return handle_cache_data(
                groupedmessages=convo_messages,
                disp_messages=display_messages,
                next_cursor=next_cursor,
                source=source,
            )

        else:
            return getall_route(user_id)

    # return rediscync()
    # ✅ Step 1: Local JSON
    existing_json = get_existing_umail_json(user_id)
    # print("----------------------------")

    # print(f"user_id : {user_id}")
    # print("data added", existing_json)
    if not existing_json:
        # ✅ Step 2: Cache
        getall_route(user_id=user_id)
        return rediscync()

    else:
        # print("before calling lance")
        # ✅ Step 3: Lance fallback
        client = UmailLanceClient(user_id)
        convo_messages, bnext_cursor = client.latest_messages_from_lance(
            user_id, next_cursor
        )
        # print("convo messages length", len(convo_messages))
        # print("in lance fetch next cursor", bnext_cursor)
        # getall_route(user_id)
        if not convo_messages:
            # If LanceDB returned a date-like cursor
            try:
                cursor_date = datetime.fromisoformat(str(next_cursor)).date()
            except:
                cursor_date = None

            today_date = datetime.today().date()

            # If next_cursor is NOT today → trigger next month Gmail fetch
            if cursor_date and cursor_date != today_date:
                # next_monthemails.delay(user_id, cursor_date)
                pass
            else:
                # next_cursor matches today OR not a date → stop Gmail paging
                getall_route(user_id=user_id)

            return rediscync()

        # If nothing matched, return a clean response
        source = "full"
        # print(f"return data lenght from get_latest: {len(display_messages)}")
        return handle_lance_data(
            convo_messages=convo_messages,
            next_cursor=bnext_cursor,
            userid=user_id,
            source=source,
        )


@umail_bp.route("/conversations_test/<user_id>/<next_cursor>", methods=["GET"])
@permission_required_body("taskbox.email.view")
def conversations_test(user_id, next_cursor):
    logged_in_user_id, user_id = parse_composite_user_id(user_id)
    client = UmailLanceClient(user_id)
    convo_messages = client.latest_messages_from_lance_test(user_id, next_cursor)

    return convo_messages


from concurrent.futures import ThreadPoolExecutor

executor = ThreadPoolExecutor(max_workers=5)


# -----------------------
# Background Task
# -----------------------
def background_convo_fetch(user_id, next_cursor):
    try:
        print("🚀 Background Lance fetch...")

        client = UmailLanceClient(user_id)
        convo_messages, bnext_cursor = client.latest_messages_from_lance(
            user_id, next_cursor
        )

        print("📦 Background fetched:", len(convo_messages))

        if not convo_messages:
            try:
                cursor_date = datetime.fromisoformat(str(next_cursor)).date()
            except:
                cursor_date = None

            today_date = datetime.today().date()

            if not cursor_date or cursor_date == today_date:
                getall_route(user_id=user_id)

    except Exception as e:
        print("❌ Background error:", e)


# -----------------------
# Redis Sync Wrapper
# -----------------------
def get_cache_sync(user_id):
    async def _inner():
        client = get_redis()
        return await client.get(f"umail_{user_id}")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(_inner())
    loop.close()
    return result


# -----------------------
# API
# -----------------------
@umail_bp.route(
    "/conversations/<user_id>", defaults={"next_cursor": None}, methods=["GET"]
)
@umail_bp.route("/conversations/<user_id>/<next_cursor>", methods=["GET"])
@permission_required_body("taskbox.email.view")
def get_latest_conversations(user_id, next_cursor):
    logged_in_user_id, user_id = parse_composite_user_id(user_id)
    if next_cursor is None:
        next_cursor = datetime.now(timezone.utc)

    mailbox_setting = check_mailbox(user_id)
    if not mailbox_setting:
        return {"disp_messages": "Restricted"}

    display_messages = []
    convo_messages = {}

    # -----------------------
    # Step 1: Local JSON
    # -----------------------
    existing_json = get_existing_umail_json(user_id)

    if not existing_json:
        # -----------------------
        # Step 2: New user — serve from cache
        # -----------------------
        cached = get_cache_sync(user_id)

        if cached:
            if isinstance(cached, list):
                cached = cached[0] if cached else {}

            convo_messages = cached.get("grouped_messages", {})
            cache_cursor = cached.get("next_page_token")

            executor.submit(background_convo_fetch, user_id, next_cursor)

            return handle_cache_data(
                groupedmessages=convo_messages,
                disp_messages=display_messages,
                next_cursor=cache_cursor,
                source="mid",
            )

    # -----------------------
    # Step 3: Existing user (or no cache) → fetch fresh from Lance synchronously
    # -----------------------
    print("⚡ Existing user or no cache → fetching from Lance")

    try:
        client = UmailLanceClient(user_id)
        convo_messages, bnext_cursor = client.latest_messages_from_lance(
            user_id, next_cursor
        )
    except Exception as e:
        print("❌ Lance fetch error:", e)
        convo_messages, bnext_cursor = [], None

    return handle_lance_data(
        convo_messages=convo_messages,
        next_cursor=bnext_cursor,
        userid=user_id,
        source="full",
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
            # print(f"[WARN] Failed to parse parsed_timestamp '{ts_str}': {e}")
            continue

    if not conversations:
        # print("[DEBUG] No valid conversations found after timestamp parsing")
        return None

    sorted_convos = sorted(conversations, key=lambda x: x[1], reverse=True)
    sorted_ids = [conv_id for conv_id, _ in sorted_convos]

    return sorted_ids


def get_sorted_lance_emails(connection, user_id, client_id):
    client = UmailLanceClient(user_id)
    recent_msg = client.get_selected_conv_from_lance(user_id, client_id)
    all_messages = []
    sorted_conversations = []

    # open a cursor once and reuse
    with connection.cursor() as cursor:
        if recent_msg:
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

                    assigned_id = ""
                    assignee_full_name = ""
                    ticket_status = ""

                    if ticket_id:
                        cursor.execute(
                            "SELECT assignee, status FROM tickets WHERE tickets_id = %s",
                            (ticket_id,),
                        )
                        t_row = cursor.fetchone()
                        if t_row:
                            assigned_id = t_row[0]
                            ticket_status = t_row[1]

                    if assigned_id:
                        cursor.execute(
                            "SELECT first_name, last_name, email FROM users WHERE user_id = %s",
                            (assigned_id,),
                        )
                        names = cursor.fetchone()
                        if names:
                            first_name, last_name, assignee_email = names
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
                            "ticket_status": ticket_status,
                        }
                    )
                except Exception as e:
                    # print(f"❌ Failed to read or parse {e}")
                    continue
        else:
            return []

    if all_messages:
        sorted_conversations = sorted(
            all_messages,
            key=lambda conv: (
                max(msg.get("timestamp") for msg in conv.get("messages", []))
                if conv.get("messages")
                else ""
            ),
            reverse=False,
        )

    return sorted_conversations


@umail_bp.route("/selected_conversation/<conversation_id>/<user_id>", methods=["GET"])
@permission_required_body("taskbox.email.view")
async def get_selected_conv(conversation_id, user_id):
    """
    Fetch selected conversation messages for a user.
    Priority:
    1. Local JSON
    2. Cache
    3. Lance fallback
    """
    snooze_flag = False
    logged_in_user_id, user_id = parse_composite_user_id(user_id)
    try:
        # user_id = conversation_id.split("_", 1)[0]

        # print(
        #     f"inside get_selected_conv for conversation_id={conversation_id}, user_id={user_id}"
        # )
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
        # print(f"existing_json : {existing_json}")

        async def get_cahced_data():
            redis_service = get_redis()
            cached_json = await redis_service.get(
                f"umail_{user_id}"
            )  # auto JSON decoded
            # print(f"cached_json : {cached_json}")

            # print("#########")
            # print(f"cached_json  for user_id : {user_id}: {cached_json}")

            if cached_json and isinstance(cached_json, dict):
                # print(f"inside if condition in 589")
                grouped = cached_json.get("grouped_messages", {})

                # Check conversation exists and has messages
                if (
                    isinstance(grouped, dict)
                    and conversation_id in grouped
                    and grouped[conversation_id]
                ):
                    messages_data = grouped[conversation_id]
                    source = "mid"
                    return _format_selected_conversation(
                        conversation_id, client_id, messages_data, source
                    )
                else:
                    # print("at line 605")
                    # print(
                    #     "⚠️ Cache miss or conversation not found, falling back to Lance"
                    # )
                    return jsonify({"message": "Conversation not found"}), 200
            else:
                # print("⚠️ No cache or invalid cache format, falling back to Lance")
                return jsonify({"message": "Conversation not found"}), 200

        if not existing_json:
            # --- Step 3: Fetch from RedisService cache ---
            return await get_cahced_data()
        else:
            # print(f" at line 618")
            logger.info("fetching from lance %s", conversation_id)
            # ✅ Step 1: Try to get client_id from DB
            sorted_conversations = []
            try:
                connection = connect_to_rds()
                if connection is None:
                    print("Database connection failed")
                    return jsonify({"error": "Error Occured"}), 500
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
                    return await get_cahced_data()
                client_id = client_id_row[0]
            except Exception as e:
                return (
                    jsonify({"message": f"❌ Error executing sender_id query: {e}"}),
                    500,
                )
                # print(f"❌ Error executing sender_id query: {e}")

            # check whether the client is snoozed or not
            sorted_conversations = get_sorted_lance_emails(
                connection=connection, user_id=user_id, client_id=client_id
            )
            cursor.execute(
                "SELECT snooze FROM users_clients WHERE users_clients_id = %s",
                (client_id,),
            )
            snooze_row = cursor.fetchone()
            if snooze_row:
                snooze_flag = bool(snooze_row[0])  # Convert 0/1 to False/True
                # print(f"snooze_flag : {snooze_flag}")
            # else:
            # print(f"[DEBUG] No snooze row found for client_id: {client_id}")

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

        # cursor.close()
        # connection.close()
        # return jsonify({"error": "Conversation not found"}), 404

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
    # print("message data", type(messages_data))

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
            # print(f"❌ Failed to read or parse {e}")
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
@permission_required_body("taskbox.email.send")
def start_conversation():

    data = request.get_json() or {}
    user_id = data.get("user_id")
    logged_in_user_id, user_id = parse_composite_user_id(user_id)
    client_id = data.get("contact_id")
    # print(f"client_id : {client_id}")

    mailbox_setting = check_mailbox(user_id)
    if not mailbox_setting:
        return {"disp_messages": "Restricted"}

    if not user_id or not client_id:
        return jsonify({"error": "Missing user_id or contact_id"}), 400

    try:
        connection = connect_to_rds()
        if connection is None:
            return jsonify({"error": "Database connection failed"}), 500
        cursor = connection.cursor()

        client = UmailLanceClient(user_id)
        # print("calling get_selected_conv_from_lance")
        recent_msg = client.get_selected_conv_from_lance(user_id, client_id)
        if recent_msg is None:
            # print(f"recent_msg is none , id : {client_id}")
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
                    if messages:
                        first_msg = messages[0]
                        channel = first_msg.get("source") or first_msg.get("channel")
                    else:
                        channel = "unknown"

                all_messages.append(
                    {
                        "id": conv_id,
                        "channel": channel,
                        "messages": messages,
                    }
                )

            except Exception as e:
                # print(f"❌ Failed to read or parse {e}")
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
        # print(f"❌ Unexpected error in get_selected_conv(): {e}")
        return jsonify({"error": "Internal server error"}), 500


def match_email_to_channel(email, channel):
    """Returns True if the email matches the given channel."""
    if not email or "@" not in email:
        return False
    domain = email.lower().split("@")[1]
    return channel.lower() in domain


import base64
import requests
import pymysql
from datetime import datetime


@umail_bp.route("/send-reply", methods=["POST"])
@permission_required_body("taskbox.email.send")
async def send_messages():
    # print("🚀 [DEBUG] Starting send_messages() function")

    try:
        # Parse request data
        data = request.json
        print(f"📥 [DEBUG] Request data: {data}")

        user_id = data.get("user_id")
        logged_in_user_id, user_id = parse_composite_user_id(user_id)
        channel = data.get("channel")
        text = data.get("text")
        ticket_conversation_id = data.get("ticket_conversation_id")
        contact_id = data.get("contact_id")
        conv_id = data.get("conversation_id")

        # CC and BCC support for replies
        cc_list = data.get("cc", [])
        bcc_list = data.get("bcc", [])
        if isinstance(cc_list, str):
            cc_list = [cc_list] if cc_list else []
        if isinstance(bcc_list, str):
            bcc_list = [bcc_list] if bcc_list else []

        # Extract attachments from request
        attachments = data.get("attachments", [])
        # print(f"📎 [DEBUG] Received {len(attachments)} attachment(s)")
        # if cc_list:
        #     #print(f"📧 [DEBUG] CC recipients: {cc_list}")
        # if bcc_list:
        #     #print(f"📧 [DEBUG] BCC recipients: {bcc_list}")
        # if attachments:
        #     #print(
        #         f"📎 [DEBUG] Attachment details: {[{'filename': att.get('filename'), 'size': att.get('file_size')} for att in attachments]}"
        #     )

        status = None
        client_id = None
        connection = connect_to_rds()
        if connection is None:
            print("❌ [DEBUG] Database connection failed")
            return jsonify({"error": "Database connection failed"}), 500
        # print("✅ [DEBUG] Database connection successful")

        cursor = connection.cursor()

        # ✅ CRITICAL: Use the conversation the user ACTUALLY CLICKED on
        # Priority: conversation_id (clicked) > ticket_conversation_id (tracking)
        if conv_id:
            c_id = conv_id
            # print(f"🎯 [DEBUG] Using conversation_id from request: {c_id}")
        elif ticket_conversation_id:
            c_id = ticket_conversation_id
            # print(f"🎯 [DEBUG] Falling back to ticket_conversation_id: {c_id}")
        else:
            print(f"❌ [DEBUG] No conversation_id or ticket_conversation_id provided")
            return jsonify({"error": "Missing conversation_id"}), 400

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
            print(f"🔍 [DEBUG] Using client_id from database: {client_id}")

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

            ##print("✅ [DEBUG] All required fields present")

        # Database connection
        ##print("🔗 [DEBUG] Attempting database connection...")

        # Initialize variables
        ticket_id = conversation_id = ticket_name = subject = thread_id = None
        is_reply = False
        # print(f"🔄 [DEBUG] Initialized variables - is_reply: {is_reply}")

        token = current_user_id.set(user_id)
        try:
            if status == "existing":
                print("🔍 [DEBUG] Processing existing conversation")

                try:
                    s3_config_key = f"{user_id}/messages/{client_id}/config.json"
                    # print(f"🔍 [DEBUG] Reading config from S3 key: {s3_config_key}")
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
                            # print(f"*******ticket_id : {ticket_id}")
                            ticket_name = conv.get("ticket_name") or ""
                            subject = conv.get("subject")
                            conversation_id = c_id
                            is_reply = True
                            print(
                                f"✅ [DEBUG] Found matching conversation - thread_id: {thread_id}, ticket_id: {ticket_id}"
                            )
                            print(
                                f"✅ [DEBUG] subject: {subject}, is_reply: {is_reply}"
                            )
                            break
                        elif conv.get("conv_id") == ticket_conversation_id:
                            thread_id = conv.get("thread_id") or ""
                            ticket_id = conv.get("ticket_id") or ""
                            # print(f"*******ticket_id : {ticket_id}")
                            ticket_name = conv.get("ticket_name") or ""
                            subject = conv.get("subject")
                            conversation_id = ticket_conversation_id
                            is_reply = True
                            print(
                                f"✅ [DEBUG] Found matching conversation - thread_id: {thread_id}, ticket_id: {ticket_id}"
                            )
                            print(
                                f"✅ [DEBUG] subject: {subject}, is_reply: {is_reply}"
                            )
                            break

                    if not is_reply:
                        # print("⚠️ [DEBUG] No matching conversation found in config - attempting to reconstruct from conversation file")
                        # FALLBACK: If conversation not in config but exists in DB, try to get data from conversation file
                        try:
                            s3_conv_key = f"{user_id}/messages/{client_id}/{c_id}.json"
                            conv_file_data = read_json_from_s3(s3_conv_key)
                            input_data_from_file = conv_file_data.get("input_data", [])

                            if input_data_from_file:
                                # Use latest message from conversation file to extract subject and thread_id
                                latest_msg = (
                                    input_data_from_file[-1]
                                    if isinstance(input_data_from_file, list)
                                    else input_data_from_file
                                )

                                if isinstance(latest_msg, dict):
                                    # Extract subject and thread_id from message if available
                                    file_subject = latest_msg.get("subject")
                                    file_thread_id = latest_msg.get("thread_id", "")

                                    if file_subject:
                                        subject = file_subject

                                    # If thread_id not in message, try to extract from conversation_id
                                    # Format: {user_id}_{thread_id}
                                    if file_thread_id:
                                        thread_id = file_thread_id
                                    elif "_" in c_id:
                                        # Extract thread_id from conversation_id (second part after user_id)
                                        parts = c_id.split("_", 1)
                                        if len(parts) == 2:
                                            thread_id = parts[1]
                                            # print(
                                            #     f"📋 [DEBUG] Extracted thread_id from conversation_id: {thread_id}"
                                            # )

                                    # Mark as reply since conversation file exists
                                    is_reply = True
                                    conversation_id = c_id
                                    # print(
                                    #     f"✅ [DEBUG] Reconstructed conversation data from file - thread_id: {thread_id}, subject: {subject}"
                                    # )
                                # else:
                                #     print(
                                #         f"⚠️ [DEBUG] Conversation file data invalid format"
                                #     )
                            # else:
                            #     print(f"⚠️ [DEBUG] Conversation file empty or not found")
                        except Exception as fallback_err:
                            print(
                                f"⚠️ [DEBUG] Could not reconstruct from conversation file: {fallback_err}"
                            )

                except FileNotFoundError:
                    print(
                        f"⚠️ [DEBUG] Config file not found at {s3_config_key} — treating as user-initiated"
                    )
                except Exception as e:
                    print(
                        f"❌ [DEBUG] Error checking config file for reply status: {e}"
                    )

            else:
                # print("🆕 [DEBUG] Processing new conversation")
                conversation_id = str(uuid.uuid4())
                client_id = contact_id
                # print(
                #     f"🆕 [DEBUG] Generated new conversation_id: {conversation_id}, client_id: {client_id}"
                # )

            # File path setup
            conv_folder = os.path.join(
                pathconfig.basepath, "messages", user_id, client_id
            )
            # print(f"📁 [DEBUG] Conversation folder: {conv_folder}")
            ensure_dir(conv_folder)
            file_name = f"{conversation_id}.json"
            conv_filepath = os.path.join(conv_folder, file_name)
            s3_conv_key = f"{user_id}/messages/{client_id}/{conversation_id}.json"
            # print(f"📁 [DEBUG] Conversation file path: {conv_filepath}")
            # print(f"📁 [DEBUG] S3 conversation key: {s3_conv_key}")

            # Load existing conversation data
            try:
                # print(f"📖 [DEBUG] Attempting to read existing conversation from S3...")
                raw_data = read_json_from_s3(s3_conv_key)
                input_data = raw_data.get("input_data", [])
                # print(f"📖 [DEBUG] Loaded {len(input_data)} existing messages")
            except Exception as e:
                print(f"📖 [DEBUG] No existing conversation found, starting fresh: {e}")
                input_data = []

            # print(f"📖 [DEBUG] input_data length: {len(input_data)}")

            # Get user email
            # print("📧 [DEBUG] Retrieving user email...")
            cursor.execute("SELECT email FROM users WHERE user_id = %s", (user_id,))
            u_email = cursor.fetchone()
            user_email = None
            if u_email:
                user_email = u_email[0]
            #     print(f"📧 [DEBUG] User email: {user_email}")
            # else:
            #     print("⚠️ [DEBUG] No user email found")

            # Get business email
            ##print("🏢 [DEBUG] Retrieving business email...")
            # cursor.execute(
            #     "SELECT BusinessEmail FROM business_info WHERE user_id_fk = %s", (user_id,)
            # )
            # b_email = cursor.fetchone()
            # if not b_email:
            #    #print("❌ [DEBUG] No email found from business_info table")
            #     return jsonify({"error": "No business email found"}), 500
            # business_email = b_email[0]
            # print(f"🏢 [DEBUG] Business email: {business_email}")

            # Select appropriate email based on channel
            integration = False
            try:
                # print(f"🔍 [DEBUG] Matching email to channel: {channel}")
                selected_email = None
                if match_email_to_channel(user_email, channel):
                    selected_email = user_email
                    # print(f"✅ [DEBUG] Selected user email: {selected_email}")
                # elif match_email_to_channel(business_email, channel):
                #     selected_email = business_email
                #     print(f"✅ [DEBUG] Selected business email: {selected_email}")
                else:
                    # print(f"⚠️ [DEBUG] No email matched to channel {channel}")
                    cursor.execute(
                        "SELECT email FROM integrations WHERE primary_user_id_fk = %s",
                        (user_id,),
                    )
                    row = cursor.fetchone()
                    selected_email = row[0]
                    integration = True

            except Exception as e:
                print(
                    f"🔥 [DEBUG] Exception occurred while selecting email by channel: {str(e)}"
                )

            # Get client email
            # print(f"👤 [DEBUG] Retrieving client email for client_id: {client_id}")
            # get the mail id from the users table
            cursor.execute(
                "SELECT email_id FROM users_clients WHERE users_clients_id = %s",
                (client_id,),
            )
            c_email = cursor.fetchone()
            client_email = None
            if c_email:
                client_email = c_email[0]
                # print(f"👤 [DEBUG] Client email: {client_email}")

                # ✅ VALIDATION: Ensure the email from request matches what we found
                # if contact_id and "@" not in str(contact_id):
                #     # contact_id is a UUID, validate it matches
                #     if contact_id != client_id:
                #         print(
                #             f"⚠️ [DEBUG] Contact ID mismatch - request sent: {contact_id}, DB found: {client_id}"
                #         )
                #         print(
                #             f"⚠️ [DEBUG] Using database value: {client_id} to ensure correct recipient"
                #         )
                #     else:
                #         print(
                #             f"⚠️ [DEBUG] Frontend sent email instead of contact UUID: {contact_id}"
                #         )
                #         print(
                #             f"✅ [DEBUG] Using correct client_id from database: {client_id}"
                #         )
                # else:
                #     print("❌ [DEBUG] No client email found")

            # Subject generation for new conversations
            if not is_reply:
                ##print("📝 [DEBUG] Generating subject for new conversation...")

                # Create initial message structure
                now_utc = datetime.now(timezone.utc)
                formatted_time = now_utc.isoformat(timespec="seconds")
                msg_id = str(uuid.uuid4())

                # print(
                #     f"📝 [DEBUG] Created message ID: {msg_id}, timestamp: {formatted_time}"
                # )

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
                # print(
                #     f"💾 [DEBUG] Saving temporary file for subject generation: {conv_filepath}"
                # )
                with open(conv_filepath, "w", encoding="utf-8") as f:
                    json.dump({"new_messages": [initial_message]}, f, indent=2)

                # Generate subject using AI
                ##print("🤖 [DEBUG] Calling generate_subject()...")
                subjects = await generate_subject(user_id, conv_filepath, channel)
                # print(f"🤖 [DEBUG] Generated subjects: {subjects}")

                # Get subject from AI response
                subject = None
                for group in subjects:
                    if msg_id in group.get("message_ids", []):
                        subject = group.get("summary")
                        # print(f"✅ [DEBUG] Found subject: {subject}")
                        break

                if not subject:
                    subject = f"Message from {client_email}"  # Fallback subject
                    # print(f"⚠️ [DEBUG] Using fallback subject: {subject}")
            # else:
            #     print(f"📝 [DEBUG] Using existing subject for reply: {subject}")

            # Create final message object
            now_utc = datetime.now(timezone.utc)
            formatted_time = now_utc.isoformat(timespec="seconds")
            msg_id = str(uuid.uuid4())

            message = {
                "id": msg_id,
                "from": selected_email or user_email,
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

            # print(f"messages at line 1998:")
            # print(f"{message}")

            # Add attachments to message if present
            if attachments:
                # Ensure attachments have proper structure for frontend download
                enhanced_attachments = []
                for i, att in enumerate(attachments):
                    enhanced_att = {
                        "filename": att.get("original_filename")
                        or att.get("filename", f"attachment_{i}"),
                        "mime_type": att.get("mime_type", "application/octet-stream"),
                        "s3_key": att.get("s3_key"),
                        "size": att.get("file_size") or att.get("size", 0),
                        # These will be populated when message is retrieved from Gmail
                        "message_id": att.get("message_id"),
                        "attachment_id": att.get("attachment_id"),
                    }
                    enhanced_attachments.append(enhanced_att)

                message["attachments"] = enhanced_attachments
                message["has_attachments"] = True
                # print(
                #     f"📎 [DEBUG] Added {len(enhanced_attachments)} attachment(s) to message object"
                # )
                # for att in enhanced_attachments:
                #     print(
                #         f"   📎 Attachment: {att.get('filename')} ({att.get('mime_type')})"
                #     )
            else:
                message["has_attachments"] = False
                message["attachments"] = []

            # print(f"📧 [DEBUG] Final message object created: {message}")

            # # CRITICAL: Check if this message already exists in input_data
            # cursor.execute("SELECT 1 FROM messages WHERE message_id = %s", (msg_id,))
            # m_id = cursor.fetchone()
            # if m_id:
            #     response_data = {
            #         "status": "duplicate message",
            #         "id": msg_id,
            #         "channel": channel,
            #         "conversationId": conversation_id,
            #         "is_reply": is_reply,
            #     }
            #     return jsonify(response_data), 200

            # Send the message via appropriate channel
            sent_message_id, sent_thread_id = None, None

            # print(f"🚀 [DEBUG] Sending message via channel: {channel}")

            if channel == "gmail":
                # print("📧 [DEBUG] Processing Gmail send...")
                # Only send as reply if we have a valid thread_id
                if thread_id and is_reply:
                    # print(f"📧 [DEBUG] Sending as REPLY with thread_id: {thread_id}")
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
                    # print(f"📧 [DEBUG] Latest message ID: {latest_id}")

                    # Getting the reply subject
                    latest_subject = latest_msg["subject"].strip()
                    if not latest_subject.lower().startswith("re:"):
                        reply_subject = f"Re: {latest_subject}"
                    else:
                        reply_subject = latest_subject
                    # print(f"📧 [DEBUG] Reply subject: {reply_subject}")

                    try:
                        # print(f"📧 [DEBUG] Calling gmail_reply()...")
                        sent_message_id = gmail_reply(
                            user_id,
                            to=client_email,
                            subject=reply_subject,
                            thread_id=thread_id,
                            body_text=text,
                            in_reply_to=latest_id,
                            attachments=attachments or [],
                            cc=cc_list or None,
                            bcc=bcc_list or None,
                        )
                        msg_id = sent_message_id
                        message["id"] = sent_message_id
                        # print(
                        #     f"✅ [DEBUG] Gmail reply sent successfully, message_id: {sent_message_id}"
                        # )

                    except Exception as e:
                        # print(f"❌ [DEBUG] Gmail reply failed: {e}")
                        return jsonify({"error": "Gmail send failed"}), 500

                else:
                    ##print("📧 [DEBUG] Sending new Gmail message...")
                    try:
                        # print(f"📧 [DEBUG] Calling send_mail()...")
                        result = send_mail(
                            user_id,
                            to=client_email,
                            subject=subject,
                            body_text=text,
                            attachments=attachments or [],
                        )
                        send_status = result.get("status")
                        if send_status == "success":

                            sent_message_id = result.get("message_id")
                            sent_thread_id = result.get("thread_id")
                            message["id"] = sent_message_id
                            message["thread_id"] = sent_thread_id
                            # print(
                            #     f"✅ [DEBUG] Gmail message sent, message_id: {sent_message_id}, thread_id: {sent_thread_id}"
                            # )
                        else:
                            # print("sending through gmail failed")
                            return jsonify({"error": "Gmail send failed"}), 500

                    except Exception as e:
                        # print(f"❌ [DEBUG] Gmail send failed: {e}")
                        return jsonify({"error": "Gmail send failed"}), 500

            elif channel == "outlook":
                # print(f"integration : {integration}")
                if integration:
                    sent = outlook_send_mail(
                        user_id=user_id,
                        to=client_email,
                        subject=subject,
                        body_text=text,
                        # thread_id=thread_id,
                        # in_reply_to=latest_id,
                        attachments=attachments or [],
                        integration=integration,
                    )

                else:
                    sent = outlook_send_mail(
                        user_id=user_id,
                        to=client_email,
                        subject=reply_subject,
                        body_text=text,
                        thread_id=thread_id,
                        in_reply_to=latest_id,
                        attachments=attachments or [],
                        cc=cc_list or None,
                        bcc=bcc_list or None,
                        integration=integration,
                    )

            elif channel == "teams":
                from microsoft_route.routes import teams_send_message

                try:
                    result = teams_send_message(
                        user_id=user_id,
                        chat_id=thread_id,
                        message_text=text,
                        integration=integration,
                    )
                    if not result or result.get("status") != "success":
                        return jsonify({"error": "Teams send failed"}), 500
                except Exception as e:
                    logger.error("Teams send failed: %s", e)
                    return jsonify({"error": "Teams send failed"}), 500

            elif channel == "zoho":
                # print("📧 [DEBUG] Processing Zoho send...")
                try:
                    # print(f"📧 [DEBUG] Calling send_zoho_email()...")
                    response_payload, status_code = send_zoho_email(
                        user_id=user_id,
                        to_email=client_email,
                        subject=subject,
                        body_text=text,
                        from_user_email=selected_email,
                    )
                    # print(
                    #     f"📧 [DEBUG] Zoho response - status: {status_code}, payload: {response_payload}"
                    # )

                    if status_code in [200, 201]:
                        message_id = response_payload.get("message_id")
                        # print(
                        #     f"✅ [DEBUG] Zoho message sent successfully, message_id: {message_id}"
                        # )
                    else:
                        # print(
                        #     f"❌ [DEBUG] Zoho send failed: {response_payload.get('error')}"
                        # )
                        return (
                            jsonify({"error": response_payload.get("error")}),
                            status_code,
                        )

                except Exception as e:
                    # print(f"❌ [DEBUG] Zoho send failed: {e}")
                    return jsonify({"error": "Zoho send failed"}), 500

            else:
                print(f"❌ [DEBUG] Unsupported channel: {channel}")
                return jsonify({"error": "Unsupported channel"}), 400

            # Database updates
            # print("💾 [DEBUG] Starting database updates...")
            updated_date = datetime.now(timezone.utc).isoformat()
            created_date = updated_date
            # print(
            #     f"💾 [DEBUG] Timestamps - created: {created_date}, updated: {updated_date}"
            # )

            try:
                if not is_reply:
                    ##print("💾 [DEBUG] Inserting new thread...")
                    cursor.execute(
                        """
                        INSERT INTO threads (conversation_id, started_at, status, last_message_at,external_user_id )
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (conversation_id, created_date, "Open", updated_date, user_id),
                    )
                    ##print("✅ [DEBUG] New thread inserted")
                else:
                    ##print("💾 [DEBUG] Updating existing ticket and thread...")
                    cursor.execute(
                        "UPDATE tickets SET updated_in = %s, status = %s WHERE conversation_id_fk = %s",
                        (updated_date, "In-Progress", conversation_id),
                    )
                    ##print("✅ [DEBUG] Ticket updated")

                    cursor.execute(
                        "UPDATE threads SET last_message_at = %s WHERE conversation_id = %s",
                        (updated_date, conversation_id),
                    )
                    ##print("✅ [DEBUG] Thread updated")

                cont_ref = f"{user_id}/messages/{client_id}/{conversation_id}.json"
                # print(f"💾 [DEBUG] Content reference: {cont_ref}")

                ##print("💾 [DEBUG] Inserting message record...")
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
                        update_at,
                        sender_type
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                        channel,
                    ),
                )
                # print("✅ [DEBUG] Message record inserted")

                connection.commit()
                ##print("✅ [DEBUG] Database transaction committed")

            except Exception as e:
                connection.rollback()
                logger.error("Database operation failed — rolled back: %s", e)
                return jsonify({"error": "Database operation failed"}), 500

            # Update Conversation File
            try:
                if isinstance(input_data, dict):
                    input_data = [input_data]
                input_data.append(message)
                conversation_data = {"input_data": input_data}
                with open(conv_filepath, "w", encoding="utf-8") as f:
                    json.dump(conversation_data, f, indent=2)

                # print(f"☁️ [DEBUG] Uploading to S3: {s3_conv_key}")
                upload_any_file(
                    conv_filepath,
                    user_id,
                    type="messages",
                    s3_key_C=s3_conv_key,
                )
                ##print("✅ [DEBUG] Conversation file updated successfully")

            except Exception as e:
                # print(f"❌ [DEBUG] Failed to update conversation file: {e}")
                return jsonify({"error": "Failed to save conversation"}), 500

            # updating lancedb
            lance_data = conversation_data.get("input_data", [])
            client = UmailLanceClient(user_id)
            await client.embed_json_file_for_reply(
                lance_data, user_id, client_id, conversation_id
            )

            # Update Config File
            ##print("⚙️ [DEBUG] Updating config file...")
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
                # print(
                #     f"⚙️ [DEBUG] Existing config loaded with {len(config_data.get('conversations', []))} conversations"
                # )
            except Exception as e:
                # print(f"⚙️ [DEBUG] No config file found — creating new: {e}")
                config_data = {"userclients_id": client_id, "conversations": []}

            # Parse timestamp
            try:
                if updated_date.endswith("Z"):
                    parsed_ts = datetime.fromisoformat(
                        updated_date.replace("Z", "+00:00")
                    )
                else:
                    parsed_ts = datetime.fromisoformat(updated_date)
                # print(f"⚙️ [DEBUG] Parsed timestamp: {parsed_ts.isoformat()}")
            except Exception as e:
                # print(f"⚠️ [DEBUG] Could not parse updated_date '{updated_date}': {e}")
                parsed_ts = datetime.now(timezone.utc)

        finally:
            current_user_id.reset(token)

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
        # print(f"*****ticket_id 2 : {ticket_id}")
        if channel == "gmail":
            if sent_thread_id:
                updated_entry["thread_id"] = sent_thread_id
                # print(f"⚙️ [DEBUG] Using sent_thread_id: {sent_thread_id}")
            else:
                updated_entry["thread_id"] = thread_id
                # print(f"⚙️ [DEBUG] Using existing thread_id: {thread_id}")
        else:
            updated_entry["thread_id"] = ""
        # print("⚙️ [DEBUG] Non-Gmail channel, no thread_id")

        # print(f"⚙️ [DEBUG] Updated entry: {updated_entry}")

        # Update or add conversation in config
        conversation_exists = False
        for i, conv in enumerate(config_data.get("conversations", [])):
            if conv.get("conv_id") == conversation_id:
                config_data["conversations"][i] = updated_entry
                conversation_exists = True
                # print(f"⚙️ [DEBUG] Updated existing conversation at index {i}")
                break

        if not conversation_exists:
            config_data.setdefault("conversations", []).append(updated_entry)
        # print("⚙️ [DEBUG] Added new conversation to config")

        config_data["userclients_id"] = client_id
        # print(f"⚙️ [DEBUG] Calling update_config_file()...")
        update_config_file(user_id, client_id, config_data)
        ##print("✅ [DEBUG] Config file updated successfully")

        # Final Response
        response_data = {
            "status": "sent",
            "id": sent_message_id or msg_id,
            "channel": channel,
            "conversationId": conversation_id,
            "is_reply": is_reply,
        }
        # print(f"🎉 [DEBUG] Function completed successfully - Response: {response_data}")
        return jsonify(response_data), 200

    except Exception as e:
        # print(f"❌ [DEBUG] Unexpected error in send_messages(): {e}")
        import traceback

        # print(f"❌ [DEBUG] Full traceback: {traceback.format_exc()}")
        return jsonify({"error": "Internal server error"}), 500

    finally:
        if "connection" in locals():
            connection.close()
        # print("🔗 [DEBUG] Database connection closed")


@umail_bp.route("/send-reply_test", methods=["POST"])
@permission_required_body("taskbox.email.send")
async def send_messages_test():
    # print("🚀 [DEBUG] Starting send_messages() function")

    try:
        # Parse request data
        data = request.json
        # print(f"📥 [DEBUG] Request data: {data}")

        user_id = data.get("user_id")
        logged_in_user_id, user_id = parse_composite_user_id(user_id)
        channel = data.get("channel")
        text = data.get("text")
        ticket_conversation_id = data.get("ticket_conversation_id")
        contact_id = data.get("contact_id")
        conv_id = data.get("conversation_id")

        # CC and BCC support for replies
        cc_list = data.get("cc", [])
        bcc_list = data.get("bcc", [])
        if isinstance(cc_list, str):
            cc_list = [cc_list] if cc_list else []
        if isinstance(bcc_list, str):
            bcc_list = [bcc_list] if bcc_list else []

        # Extract attachments from request
        attachments = data.get("attachments", [])
        # print(f"📎 [DEBUG] Received {len(attachments)} attachment(s)")
        # if cc_list:
        #     #print(f"📧 [DEBUG] CC recipients: {cc_list}")
        # if bcc_list:
        #     #print(f"📧 [DEBUG] BCC recipients: {bcc_list}")
        # if attachments:
        #     #print(
        #         f"📎 [DEBUG] Attachment details: {[{'filename': att.get('filename'), 'size': att.get('file_size')} for att in attachments]}"
        #     )

        status = None
        client_id = None
        connection = connect_to_rds()
        if connection is None:
            # print("❌ [DEBUG] Database connection failed")
            return jsonify({"error": "Database connection failed"}), 500
        # print("✅ [DEBUG] Database connection successful")

        cursor = connection.cursor()

        # ✅ CRITICAL: Use the conversation the user ACTUALLY CLICKED on
        # Priority: conversation_id (clicked) > ticket_conversation_id (tracking)
        if conv_id:
            c_id = conv_id
            # print(f"🎯 [DEBUG] Using conversation_id from request: {c_id}")
        elif ticket_conversation_id:
            c_id = ticket_conversation_id
            # print(f"🎯 [DEBUG] Falling back to ticket_conversation_id: {c_id}")
        else:
            print(f"❌ [DEBUG] No conversation_id or ticket_conversation_id provided")
            return jsonify({"error": "Missing conversation_id"}), 400

        # print(f"🔍 [DEBUG] Querying for sender_id with conversation_id: {c_id}")
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
            # print(f"🔍 [DEBUG] Using client_id from database: {client_id}")

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
                # print(f"❌ [DEBUG] Missing required fields: {missing_fields}")
                return jsonify({"error": "Missing required payload fields"}), 400

            ##print("✅ [DEBUG] All required fields present")

        # Database connection
        ##print("🔗 [DEBUG] Attempting database connection...")

        # Initialize variables
        ticket_id = conversation_id = ticket_name = subject = thread_id = None
        is_reply = False
        # print(f"🔄 [DEBUG] Initialized variables - is_reply: {is_reply}")

        if status == "existing":
            # print("🔍 [DEBUG] Processing existing conversation")

            try:
                s3_config_key = f"{user_id}/messages/{client_id}/config.json"
                # print(f"🔍 [DEBUG] Reading config from S3 key: {s3_config_key}")
                config_data = read_json_from_s3(s3_config_key)
                # print(
                #     f"🔍 [DEBUG] Config data retrieved: {len(config_data.get('conversations', []))} conversations found"
                # )

                # Checking if it is a reply and getting reply info
                conv_list = config_data.get("conversations", [])
                # print(
                #     f"🔍 [DEBUG] Searching for conversation in {len(conv_list)} conversations"
                # )

                for i, conv in enumerate(conv_list):
                    # print(
                    #     f"🔍 [DEBUG] Checking conversation {i}: conv_id={conv.get('conv_id')}"
                    # )
                    if conv.get("conv_id") == c_id:
                        thread_id = conv.get("thread_id") or ""
                        ticket_id = conv.get("ticket_id") or ""
                        # print(f"*******ticket_id : {ticket_id}")
                        ticket_name = conv.get("ticket_name") or ""
                        subject = conv.get("subject")
                        conversation_id = c_id
                        is_reply = True
                        # print(
                        #     f"✅ [DEBUG] Found matching conversation - thread_id: {thread_id}, ticket_id: {ticket_id}"
                        # )
                        # print(f"✅ [DEBUG] subject: {subject}, is_reply: {is_reply}")
                        break

                if not is_reply:
                    # print("⚠️ [DEBUG] No matching conversation found in config - attempting to reconstruct from conversation file")
                    # FALLBACK: If conversation not in config but exists in DB, try to get data from conversation file
                    try:
                        s3_conv_key = f"{user_id}/messages/{client_id}/{c_id}.json"
                        conv_file_data = read_json_from_s3(s3_conv_key)
                        input_data_from_file = conv_file_data.get("input_data", [])

                        if input_data_from_file:
                            # Use latest message from conversation file to extract subject and thread_id
                            latest_msg = (
                                input_data_from_file[-1]
                                if isinstance(input_data_from_file, list)
                                else input_data_from_file
                            )

                            if isinstance(latest_msg, dict):
                                # Extract subject and thread_id from message if available
                                file_subject = latest_msg.get("subject")
                                file_thread_id = latest_msg.get("thread_id", "")

                                if file_subject:
                                    subject = file_subject

                                # If thread_id not in message, try to extract from conversation_id
                                # Format: {user_id}_{thread_id}
                                if file_thread_id:
                                    thread_id = file_thread_id
                                elif "_" in c_id:
                                    # Extract thread_id from conversation_id (second part after user_id)
                                    parts = c_id.split("_", 1)
                                    if len(parts) == 2:
                                        thread_id = parts[1]
                                        # print(
                                        #     f"📋 [DEBUG] Extracted thread_id from conversation_id: {thread_id}"
                                        # )

                                # Mark as reply since conversation file exists
                                is_reply = True
                                conversation_id = c_id
                                # print(
                                #     f"✅ [DEBUG] Reconstructed conversation data from file - thread_id: {thread_id}, subject: {subject}"
                                # )
                        #     else:
                        #         print(
                        #             f"⚠️ [DEBUG] Conversation file data invalid format"
                        #         )
                        # else:
                        #     print(f"⚠️ [DEBUG] Conversation file empty or not found")
                    except Exception as fallback_err:
                        print(
                            f"⚠️ [DEBUG] Could not reconstruct from conversation file: {fallback_err}"
                        )

            except FileNotFoundError:
                print(
                    f"⚠️ [DEBUG] Config file not found at {s3_config_key} — treating as user-initiated"
                )
            except Exception as e:
                print(f"❌ [DEBUG] Error checking config file for reply status: {e}")

        else:
            # print("🆕 [DEBUG] Processing new conversation")
            conversation_id = str(uuid.uuid4())
            client_id = contact_id
            # print(
            #     f"🆕 [DEBUG] Generated new conversation_id: {conversation_id}, client_id: {client_id}"
            # )

        # File path setup
        conv_folder = os.path.join(pathconfig.basepath, "messages", user_id, client_id)
        # print(f"📁 [DEBUG] Conversation folder: {conv_folder}")
        ensure_dir(conv_folder)
        file_name = f"{conversation_id}.json"
        conv_filepath = os.path.join(conv_folder, file_name)
        s3_conv_key = f"{user_id}/messages/{client_id}/{conversation_id}.json"
        # print(f"📁 [DEBUG] Conversation file path: {conv_filepath}")
        # print(f"📁 [DEBUG] S3 conversation key: {s3_conv_key}")

        # Load existing conversation data
        try:
            # print(f"📖 [DEBUG] Attempting to read existing conversation from S3...")
            raw_data = read_json_from_s3(s3_conv_key)
            input_data = raw_data.get("input_data", [])
            # print(f"📖 [DEBUG] Loaded {len(input_data)} existing messages")
        except Exception as e:
            print(f"📖 [DEBUG] No existing conversation found, starting fresh: {e}")
            input_data = []

        # decide whether integration or not:

        integration = False
        cursor.execute("SELECT email FROM users WHERE user_id = %s", (user_id,))
        u_email = cursor.fetchone()
        if u_email:
            user_email = u_email[0]
            # print(f"📧 [DEBUG] User email: {user_email}")
        else:
            cursor.execute(
                "SELECT email FROM integrations WHERE user_id = %s", (user_id,)
            )
            u_email = cursor.fetchone()
            if u_email:
                user_email = u_email[0]
                integration = True
            #     print(f"📧 [DEBUG] User email: {user_email}")
            # else:
            #     print("⚠️ [DEBUG] No user email found")

        # Get business email
        ##print("🏢 [DEBUG] Retrieving business email...")
        # cursor.execute(
        #     "SELECT BusinessEmail FROM business_info WHERE user_id_fk = %s", (user_id,)
        # )
        # b_email = cursor.fetchone()
        # if not b_email:
        #    #print("❌ [DEBUG] No email found from business_info table")
        #     return jsonify({"error": "No business email found"}), 500
        # business_email = b_email[0]
        # print(f"🏢 [DEBUG] Business email: {business_email}")

        # Select appropriate email based on channel
        try:
            # print(f"🔍 [DEBUG] Matching email to channel: {channel}")
            selected_email = None
            if match_email_to_channel(user_email, channel):
                selected_email = user_email
                # print(f"✅ [DEBUG] Selected user email: {selected_email}")
            # elif match_email_to_channel(business_email, channel):
            #     selected_email = business_email
            #     print(f"✅ [DEBUG] Selected business email: {selected_email}")
            # else:
            #     print(f"⚠️ [DEBUG] No email matched to channel {channel}")

        except Exception as e:
            print(
                f"🔥 [DEBUG] Exception occurred while selecting email by channel: {str(e)}"
            )

        # Get client email
        # print(f"👤 [DEBUG] Retrieving client email for client_id: {client_id}")
        # get the mail id from the users table
        cursor.execute(
            "SELECT email_id FROM users_clients WHERE users_clients_id = %s",
            (client_id,),
        )
        c_email = cursor.fetchone()
        client_email = None
        if c_email:
            client_email = c_email[0]
            # print(f"👤 [DEBUG] Client email: {client_email}")

            # # ✅ VALIDATION: Ensure the email from request matches what we found
            # if contact_id and "@" not in str(contact_id):
            #     # contact_id is a UUID, validate it matches
            #     if contact_id != client_id:
            #         print(
            #             f"⚠️ [DEBUG] Contact ID mismatch - request sent: {contact_id}, DB found: {client_id}"
            #         )
            #         print(
            #             f"⚠️ [DEBUG] Using database value: {client_id} to ensure correct recipient"
            #         )
            #     else:
            #         print(
            #             f"⚠️ [DEBUG] Frontend sent email instead of contact UUID: {contact_id}"
            #         )
            #         print(
            #             f"✅ [DEBUG] Using correct client_id from database: {client_id}"
            #         )
            # else:
            #     print("❌ [DEBUG] No client email found")

        # Subject generation for new conversations
        if not is_reply:
            ##print("📝 [DEBUG] Generating subject for new conversation...")

            # Create initial message structure
            now_utc = datetime.now(timezone.utc)
            formatted_time = now_utc.isoformat(timespec="seconds")
            msg_id = str(uuid.uuid4())

            # print(
            #     f"📝 [DEBUG] Created message ID: {msg_id}, timestamp: {formatted_time}"
            # )

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
            # print(
            #     f"💾 [DEBUG] Saving temporary file for subject generation: {conv_filepath}"
            # )
            with open(conv_filepath, "w", encoding="utf-8") as f:
                json.dump({"new_messages": [initial_message]}, f, indent=2)

            # Generate subject using AI
            ##print("🤖 [DEBUG] Calling generate_subject()...")
            subjects = await generate_subject(user_id, conv_filepath, channel)
            # print(f"🤖 [DEBUG] Generated subjects: {subjects}")

            # Get subject from AI response
            subject = None
            for group in subjects:
                if msg_id in group.get("message_ids", []):
                    subject = group.get("summary")
                    # print(f"✅ [DEBUG] Found subject: {subject}")
                    break

            if not subject:
                subject = f"Message from {client_email}"  # Fallback subject
                # print(f"⚠️ [DEBUG] Using fallback subject: {subject}")
        # else:
        #     print(f"📝 [DEBUG] Using existing subject for reply: {subject}")

        # Create final message object
        now_utc = datetime.now(timezone.utc)
        formatted_time = now_utc.isoformat(timespec="seconds")
        msg_id = str(uuid.uuid4())

        message = {
            "id": msg_id,
            "from": selected_email or user_email,
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

        # print(f"messages at line 1998:")
        # print(f"{message}")

        # Add attachments to message if present
        if attachments:
            # Ensure attachments have proper structure for frontend download
            enhanced_attachments = []
            for i, att in enumerate(attachments):
                enhanced_att = {
                    "filename": att.get("original_filename")
                    or att.get("filename", f"attachment_{i}"),
                    "mime_type": att.get("mime_type", "application/octet-stream"),
                    "s3_key": att.get("s3_key"),
                    "size": att.get("file_size") or att.get("size", 0),
                    # These will be populated when message is retrieved from Gmail
                    "message_id": att.get("message_id"),
                    "attachment_id": att.get("attachment_id"),
                }
                enhanced_attachments.append(enhanced_att)

            message["attachments"] = enhanced_attachments
            message["has_attachments"] = True
            # print(
            #     f"📎 [DEBUG] Added {len(enhanced_attachments)} attachment(s) to message object"
            # )
            # for att in enhanced_attachments:
            #     print(
            #         f"   📎 Attachment: {att.get('filename')} ({att.get('mime_type')})"
            #     )
        else:
            message["has_attachments"] = False
            message["attachments"] = []

        # print(f"📧 [DEBUG] Final message object created: {message}")

        sent_message_id, sent_thread_id = None, None

        # print(f"🚀 [DEBUG] Sending message via channel: {channel}")

        if channel == "gmail":
            # print("📧 [DEBUG] Processing Gmail send...")
            # Only send as reply if we have a valid thread_id
            if thread_id and is_reply:
                # print(f"📧 [DEBUG] Sending as REPLY with thread_id: {thread_id}")
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
                        ##print("❌ [DEBUG] No valid messages found in input_data")
                        return jsonify({"error": "No valid messages found"}), 400
                    latest_msg = max(
                        valid_messages,
                        key=lambda msg: datetime.fromisoformat(msg["timestamp"]),
                    )
                else:
                    ##print("❌ [DEBUG] input_data is neither dict nor list")
                    return jsonify({"error": "Invalid input_data format"}), 400
                latest_id = latest_msg["id"]
                # print(f"📧 [DEBUG] Latest message ID: {latest_id}")

                # Getting the reply subject
                latest_subject = latest_msg["subject"].strip()
                if not latest_subject.lower().startswith("re:"):
                    reply_subject = f"Re: {latest_subject}"
                else:
                    reply_subject = latest_subject
                # print(f"📧 [DEBUG] Reply subject: {reply_subject}")

                try:
                    # print(f"📧 [DEBUG] Calling gmail_reply()...")
                    sent_message_id = gmail_reply(
                        user_id,
                        to=client_email,
                        subject=reply_subject,
                        thread_id=thread_id,
                        body_text=text,
                        in_reply_to=latest_id,
                        attachments=attachments or [],
                        cc=cc_list or None,
                        bcc=bcc_list or None,
                    )
                    msg_id = sent_message_id
                    message["id"] = sent_message_id
                    # print(
                    #     f"✅ [DEBUG] Gmail reply sent successfully, message_id: {sent_message_id}"
                    # )

                except Exception as e:
                    # print(f"❌ [DEBUG] Gmail reply failed: {e}")
                    return jsonify({"error": "Gmail send failed"}), 500

            else:
                ##print("📧 [DEBUG] Sending new Gmail message...")
                try:
                    # print(f"📧 [DEBUG] Calling send_mail()...")
                    result = send_mail(
                        user_id,
                        to=client_email,
                        subject=subject,
                        body_text=text,
                        attachments=attachments or [],
                    )
                    send_status = result.get("status")
                    if send_status == "success":

                        sent_message_id = result.get("message_id")
                        sent_thread_id = result.get("thread_id")
                        message["id"] = sent_message_id
                        message["thread_id"] = sent_thread_id
                        # print(
                        #     f"✅ [DEBUG] Gmail message sent, message_id: {sent_message_id}, thread_id: {sent_thread_id}"
                        # )
                    else:
                        # print("sending through gmail failed")
                        return jsonify({"error": "Gmail send failed"}), 500

                except Exception as e:
                    # print(f"❌ [DEBUG] Gmail send failed: {e}")
                    return jsonify({"error": "Gmail send failed"}), 500

        elif channel == "outlook":
            # print(f"integration : {integration}")

            user_from_conv_id = conversation_id.partition("_")[0]

            outlook_integration = user_from_conv_id != user_id

            # print(f"outlook_integration : {outlook_integration}")

            if thread_id and is_reply:
                # print(f"📧 [DEBUG] Sending as REPLY with thread_id: {thread_id}")
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
                        ##print("❌ [DEBUG] No valid messages found in input_data")
                        return jsonify({"error": "No valid messages found"}), 400
                    latest_msg = max(
                        valid_messages,
                        key=lambda msg: datetime.fromisoformat(msg["timestamp"]),
                    )
                else:
                    ##print("❌ [DEBUG] input_data is neither dict nor list")
                    return jsonify({"error": "Invalid input_data format"}), 400
                latest_id = latest_msg["id"]
                # print(f"📧 [DEBUG] Latest message ID: {latest_id}")

                # Getting the reply subject
                latest_subject = latest_msg["subject"].strip()
                # print(f"latest_subject : {latest_subject}")
                if not latest_subject.lower().startswith("re:"):
                    reply_subject = f"Re: {latest_subject}"
                else:
                    reply_subject = latest_subject
                # print(f"📧 [DEBUG] Reply subject: {reply_subject}")

            if outlook_integration:

                sent = outlook_send_mail(
                    user_id=user_id,
                    to=client_email,
                    subject=subject,
                    body_text=text,
                    # thread_id=thread_id,
                    # in_reply_to=latest_id,
                    attachments=attachments or [],
                    integration=integration,
                    outlook_integration=outlook_integration,
                )

            else:
                sent = outlook_send_mail(
                    user_id=user_id,
                    to=client_email,
                    subject=reply_subject,
                    body_text=text,
                    thread_id=thread_id,
                    in_reply_to=latest_id,
                    attachments=attachments or [],
                    cc=cc_list or None,
                    bcc=bcc_list or None,
                    integration=integration,
                    outlook_integration=outlook_integration,
                )

        elif channel == "zoho":
            # print("📧 [DEBUG] Processing Zoho send...")
            try:
                # print(f"📧 [DEBUG] Calling send_zoho_email()...")
                response_payload, status_code = send_zoho_email(
                    user_id=user_id,
                    to_email=client_email,
                    subject=subject,
                    body_text=text,
                    from_user_email=selected_email,
                )
                # print(
                #     f"📧 [DEBUG] Zoho response - status: {status_code}, payload: {response_payload}"
                # )

                if status_code in [200, 201]:
                    message_id = response_payload.get("message_id")
                    # print(
                    #     f"✅ [DEBUG] Zoho message sent successfully, message_id: {message_id}"
                    # )
                else:
                    # print(
                    #     f"❌ [DEBUG] Zoho send failed: {response_payload.get('error')}"
                    # )
                    return (
                        jsonify({"error": response_payload.get("error")}),
                        status_code,
                    )

            except Exception as e:
                # print(f"❌ [DEBUG] Zoho send failed: {e}")
                return jsonify({"error": "Zoho send failed"}), 500

        else:
            # print(f"❌ [DEBUG] Unsupported channel: {channel}")
            return jsonify({"error": "Unsupported channel"}), 400

        # Database updates
        # print("💾 [DEBUG] Starting database updates...")
        updated_date = datetime.now(timezone.utc).isoformat()
        created_date = updated_date
        # print(
        #     f"💾 [DEBUG] Timestamps - created: {created_date}, updated: {updated_date}"
        # )

        try:
            if not is_reply:
                ##print("💾 [DEBUG] Inserting new thread...")
                cursor.execute(
                    """
                    INSERT INTO threads (conversation_id, started_at, status, last_message_at,external_user_id )
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (conversation_id, created_date, "Open", updated_date, user_id),
                )
                ##print("✅ [DEBUG] New thread inserted")
            else:
                ##print("💾 [DEBUG] Updating existing ticket and thread...")
                cursor.execute(
                    "UPDATE tickets SET updated_in = %s, status = %s WHERE conversation_id_fk = %s",
                    (updated_date, "In-Progress", conversation_id),
                )
                ##print("✅ [DEBUG] Ticket updated")

                cursor.execute(
                    "UPDATE threads SET last_message_at = %s WHERE conversation_id = %s",
                    (updated_date, conversation_id),
                )
                ##print("✅ [DEBUG] Thread updated")

            cont_ref = f"{user_id}/messages/{client_id}/{conversation_id}.json"
            # print(f"💾 [DEBUG] Content reference: {cont_ref}")

            ##print("💾 [DEBUG] Inserting message record...")
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
                    update_at,
                    sender_type
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                    channel,
                ),
            )
            # print("✅ [DEBUG] Message record inserted")

            connection.commit()
            ##print("✅ [DEBUG] Database transaction committed")

        except Exception as e:
            connection.rollback()
            logger.error("Database operation failed — rolled back: %s", e)
            return jsonify({"error": "Database operation failed"}), 500

        # Update Conversation File
        ##print("📄 [DEBUG] Updating conversation file...")
        try:
            if isinstance(input_data, dict):
                input_data = [input_data]
            input_data.append(message)
            conversation_data = {"input_data": input_data}
            # print(f"📄 [DEBUG] Total messages in conversation: {len(input_data)}")
            # print(f"conversation_data : {conversation_data}")
            # print(f"💾 [DEBUG] Writing to local file: {conv_filepath}")
            with open(conv_filepath, "w", encoding="utf-8") as f:
                json.dump(conversation_data, f, indent=2)

            # print(f"☁️ [DEBUG] Uploading to S3: {s3_conv_key}")
            upload_any_file(
                conv_filepath,
                user_id,
                type="messages",
                s3_key_C=s3_conv_key,
            )
            ##print("✅ [DEBUG] Conversation file updated successfully")

        except Exception as e:
            # print(f"❌ [DEBUG] Failed to update conversation file: {e}")
            return jsonify({"error": "Failed to save conversation"}), 500

        # updating lancedb
        lance_data = conversation_data.get("input_data", [])
        client = UmailLanceClient(user_id)
        await client.embed_json_file_for_reply(
            lance_data, user_id, client_id, conversation_id
        )

        # Update Config File
        ##print("⚙️ [DEBUG] Updating config file...")
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
            # print(
            #     f"⚙️ [DEBUG] Existing config loaded with {len(config_data.get('conversations', []))} conversations"
            # )
        except Exception as e:
            # print(f"⚙️ [DEBUG] No config file found — creating new: {e}")
            config_data = {"userclients_id": client_id, "conversations": []}

        # Parse timestamp
        try:
            if updated_date.endswith("Z"):
                parsed_ts = datetime.fromisoformat(updated_date.replace("Z", "+00:00"))
            else:
                parsed_ts = datetime.fromisoformat(updated_date)
            # print(f"⚙️ [DEBUG] Parsed timestamp: {parsed_ts.isoformat()}")
        except Exception as e:
            # print(f"⚠️ [DEBUG] Could not parse updated_date '{updated_date}': {e}")
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
        # print(f"*****ticket_id 2 : {ticket_id}")
        if channel == "gmail":
            if sent_thread_id:
                updated_entry["thread_id"] = sent_thread_id
                # print(f"⚙️ [DEBUG] Using sent_thread_id: {sent_thread_id}")
            else:
                updated_entry["thread_id"] = thread_id
                # print(f"⚙️ [DEBUG] Using existing thread_id: {thread_id}")
        else:
            updated_entry["thread_id"] = ""
        # print("⚙️ [DEBUG] Non-Gmail channel, no thread_id")

        # print(f"⚙️ [DEBUG] Updated entry: {updated_entry}")

        # Update or add conversation in config
        conversation_exists = False
        for i, conv in enumerate(config_data.get("conversations", [])):
            if conv.get("conv_id") == conversation_id:
                config_data["conversations"][i] = updated_entry
                conversation_exists = True
                # print(f"⚙️ [DEBUG] Updated existing conversation at index {i}")
                break

        if not conversation_exists:
            config_data.setdefault("conversations", []).append(updated_entry)
        # print("⚙️ [DEBUG] Added new conversation to config")

        config_data["userclients_id"] = client_id
        # print(f"⚙️ [DEBUG] Calling update_config_file()...")
        update_config_file(user_id, client_id, config_data)
        ##print("✅ [DEBUG] Config file updated successfully")

        # Final Response
        response_data = {
            "status": "sent",
            "id": sent_message_id or msg_id,
            "channel": channel,
            "conversationId": conversation_id,
            "is_reply": is_reply,
        }
        # print(f"🎉 [DEBUG] Function completed successfully - Response: {response_data}")
        return jsonify(response_data), 200

    except Exception as e:
        # print(f"❌ [DEBUG] Unexpected error in send_messages(): {e}")
        import traceback

        # print(f"❌ [DEBUG] Full traceback: {traceback.format_exc()}")
        return jsonify({"error": "Internal server error"}), 500

    finally:
        if "connection" in locals():
            connection.close()
        # print("🔗 [DEBUG] Database connection closed")


@umail_bp.route("/async_message/<userid>", methods=["GET"])
@permission_required_body("taskbox.email.view")
def get_inbox_info(userid):
    logged_in_user_id, user_id = parse_composite_user_id(userid)
    mailbox_setting = check_mailbox(userid)
    if not mailbox_setting:
        return {"disp_messages": "Restricted"}
    try:
        result = run_fetch_gmail_in_background(v2all_continuous, userid)
        return jsonify({"result": result}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================================
# SYNC MANAGEMENT ENDPOINTS - For Frontend Login & Manual Sync Triggers
# ============================================================================


@umail_bp.route("/sync/check_should_sync/<user_id>", methods=["GET"])
@permission_required_body("taskbox.email.view")
def check_should_sync(user_id):
    """
    Check if a sync should happen for a user without triggering it.
    Useful for frontend to decide whether to call /get_all_messages

    Query Parameters:
    - context: 'login' (default) or 'manual'
      - 'login': Always triggers sync on first call, respects 30min for subsequent
      - 'manual': Checks 30min interval for button clicks/refreshes

    Returns:
    {
        "should_sync": bool,
        "reason": string explaining the decision,
        "last_sync": ISO timestamp of last sync or null,
        "next_allowed_sync": ISO timestamp of when next sync is allowed,
        "time_until_next_sync": seconds to wait,
        "context": "login" or "manual"
    }
    """
    logged_in_user_id, user_id = parse_composite_user_id(user_id)

    try:
        context = request.args.get("context", "login").lower()

        if context == "login":
            sync_info = asyncio.run(SyncManager.should_sync_on_login(user_id))
        elif context == "manual":
            sync_info = asyncio.run(SyncManager.should_sync_on_manual_action(user_id))
        else:
            return jsonify({"error": "Invalid context. Use 'login' or 'manual'"}), 400

        logger.info(f"Sync check for {user_id} (context={context}): {sync_info}")
        return jsonify(sync_info), 200
    except Exception as e:
        logger.error(f"Error in check_should_sync: {e}")
        return jsonify({"error": str(e)}), 500


@umail_bp.route("/sync/trigger_on_login/<user_id>", methods=["GET"])
@permission_required_body("taskbox.email.view")
def trigger_sync_on_login(user_id):
    """
    Trigger a sync on user login if 30 minutes have passed since last sync.
    Should be called by frontend after user successfully logs in.

    This endpoint:
    1. Checks if 30 minutes have passed since last sync
    2. If yes: triggers email fetch and records the sync time
    3. If no: returns rate_limited status
    4. Returns status and task info

    Returns:
    {
        "status": "syncing" | "rate_limited" | "already_syncing",
        "message": description,
        "task_id": celery task ID (if syncing),
        "last_sync": previous sync time,
        "next_allowed_sync": when next sync is allowed,
        "time_until_next_sync": seconds to wait,
        "reason": explanation,
        "context": "login"
    }
    """
    logged_in_user_id, user_id = parse_composite_user_id(user_id)

    try:
        sync_check = asyncio.run(SyncManager.should_sync_on_login(user_id))

        logger.info(f"Login sync trigger for {user_id}: {sync_check}")

        if not sync_check["should_sync"]:
            logger.info(f"Login sync skipped for {user_id}: {sync_check['reason']}")
            return (
                jsonify(
                    {
                        "status": "rate_limited",
                        "message": sync_check["reason"],
                        "user_id": user_id,
                        "last_sync": sync_check.get("last_sync"),
                        "next_allowed_sync": sync_check.get("next_allowed_sync"),
                        "time_until_next_sync": sync_check.get("time_until_next_sync"),
                        "context": "login",
                    }
                ),
                429,
            )

        if not acquire_user_lock(user_id):
            logger.info(f"Sync task already running for {user_id}")
            return (
                jsonify(
                    {
                        "status": "already_syncing",
                        "message": "Sync task is already running for this user",
                        "user_id": user_id,
                        "context": "login",
                    }
                ),
                202,
            )

        async_result = umail_sync.delay(user_id)

        asyncio.run(SyncManager.record_sync_time(user_id))

        logger.info(f"Login sync triggered for {user_id}, task_id={async_result.id}")

        return (
            jsonify(
                {
                    "status": "syncing",
                    "message": "Email sync triggered on login",
                    "user_id": user_id,
                    "task_id": async_result.id,
                    "last_sync": sync_check.get("last_sync"),
                    "context": "login",
                }
            ),
            202,
        )
    except Exception as e:
        logger.error(f"Error in trigger_sync_on_login: {e}")
        return jsonify({"error": str(e), "context": "login"}), 500


@umail_bp.route("/sync/trigger_manual/<user_id>", methods=["GET"])
@permission_required_body("taskbox.email.view")
def trigger_sync_manual(user_id):
    """
    Manually trigger a sync (from button click or page refresh).
    Respects the 30-minute interval - will reject if too soon.

    Returns:
    {
        "status": "syncing" | "rate_limited" | "already_running",
        "message": description,
        "task_id": celery task ID (if syncing),
        "next_allowed_sync": ISO timestamp,
        "time_until_next_sync": seconds,
        "context": "manual"
    }
    """
    logged_in_user_id, user_id = parse_composite_user_id(user_id)

    try:
        # Check if manual sync is allowed
        sync_check = asyncio.run(SyncManager.should_sync_on_manual_action(user_id))

        if not sync_check["should_sync"]:
            logger.info(f"Manual sync rejected for {user_id}: {sync_check['reason']}")
            return (
                jsonify(
                    {
                        "status": "rate_limited",
                        "message": sync_check["reason"],
                        "next_allowed_sync": sync_check["next_allowed_sync"],
                        "time_until_next_sync": sync_check["time_until_next_sync"],
                        "user_id": user_id,
                        "context": "manual",
                    }
                ),
                429,  # Too Many Requests
            )

        # Check if already running
        if not acquire_user_lock(user_id):
            logger.info(f"Sync task already running for {user_id} (manual trigger)")
            return (
                jsonify(
                    {
                        "status": "already_running",
                        "message": "Sync task is already running for this user",
                        "user_id": user_id,
                        "context": "manual",
                    }
                ),
                202,
            )

        # Trigger the sync
        async_result = umail_sync.delay(user_id)

        # Record this sync time
        asyncio.run(SyncManager.record_sync_time(user_id))

        next_sync = datetime.now(timezone.utc) + asyncio.run(
            _get_timedelta_until_next()
        )

        logger.info(f"Manual sync triggered for {user_id}, task_id={async_result.id}")

        return (
            jsonify(
                {
                    "status": "syncing",
                    "message": "Email sync triggered manually",
                    "user_id": user_id,
                    "task_id": async_result.id,
                    "context": "manual",
                }
            ),
            202,
        )
    except Exception as e:
        logger.error(f"Error in trigger_sync_manual: {e}")
        return jsonify({"error": str(e), "context": "manual"}), 500


@umail_bp.route("/sync/status/<user_id>", methods=["GET"])
@permission_required_body("taskbox.email.view")
def get_sync_status(user_id):
    """
    Get the current sync status for a user.
    Shows when the last sync happened and when the next one is allowed.

    Returns:
    {
        "user_id": user_id,
        "last_sync": ISO timestamp or null,
        "next_allowed_sync": ISO timestamp,
        "time_until_next_sync": seconds,
        "sync_interval": 1800 (seconds, 30 minutes),
        "can_manual_sync": bool
    }
    """
    logged_in_user_id, user_id = parse_composite_user_id(user_id)

    try:
        sync_info = asyncio.run(SyncManager.get_last_sync_time(user_id))

        return (
            jsonify(
                {
                    "user_id": user_id,
                    "last_sync": sync_info.get("last_sync"),
                    "next_allowed_sync": sync_info.get("next_allowed_sync"),
                    "time_until_next_sync": sync_info.get("time_until_next_sync"),
                    "sync_interval": 1800,  # 30 minutes in seconds
                    "can_manual_sync": sync_info.get("should_sync", False),
                }
            ),
            200,
        )
    except Exception as e:
        logger.error(f"Error in get_sync_status: {e}")
        return jsonify({"error": str(e)}), 500


@umail_bp.route("/sync/reset_timer/<user_id>", methods=["POST"])
@permission_required_body("taskbox.email.view")
def reset_sync_timer(user_id):
    """
    Admin endpoint to reset the sync timer for a user (for testing/troubleshooting).

    Returns:
    {
        "status": "success" | "failed",
        "message": description
    }
    """
    logged_in_user_id, user_id = parse_composite_user_id(user_id)

    try:
        # Optional: Add authentication check here for production
        success = asyncio.run(SyncManager.clear_sync_timer(user_id))

        if success:
            logger.info(f"Sync timer reset for {user_id}")
            return (
                jsonify(
                    {
                        "status": "success",
                        "message": f"Sync timer cleared for {user_id}",
                    }
                ),
                200,
            )
        else:
            return (
                jsonify(
                    {
                        "status": "failed",
                        "message": f"Failed to clear sync timer for {user_id}",
                    }
                ),
                500,
            )
    except Exception as e:
        logger.error(f"Error in reset_sync_timer: {e}")
        return jsonify({"error": str(e)}), 500


async def _get_timedelta_until_next():
    """Helper to get timedelta until next sync"""
    from datetime import timedelta

    return timedelta(seconds=1800)  # 30 minutes


# ============================================================================
# ATTACHMENT HANDLING ENDPOINTS
# ============================================================================


@umail_bp.route("/attachment-test", methods=["GET"])
@permission_required_body("taskbox.email.attachments.view")
def attachment_test():
    """
    Simple test endpoint to verify attachment endpoints are accessible
    """
    return (
        jsonify(
            {
                "status": "ok",
                "message": "Attachment endpoints are accessible",
                "endpoints": [
                    "/umail/attach-file (POST)",
                    "/umail/attach-files (POST)",
                    "/umail/send-reply-with-attachments (POST)",
                ],
            }
        ),
        200,
    )


@umail_bp.route("/attach-file", methods=["POST", "OPTIONS"])
@permission_required_body("taskbox.email.attachments.view")
def upload_attachment():
    # Handle CORS preflight
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"}), 200

    """
    Upload a single attachment file for email
    
    Expected multipart/form-data payload:
    - user_id: str (optional - will use session if not provided)
    - conversation_id: str (optional - will use fallback if not provided)
    - client_id: str (optional)
    - file: FileStorage (required) - the file to upload
    
    Returns:
        {
            'status': 'success' | 'error',
            'attachment_id': str (if success),
            'filename': str (if success),
            'original_filename': str (if success),
            'file_size': int (if success),
            'mime_type': str (if success),
            's3_key': str (if success),
            'upload_timestamp': str (if success),
            'error': str (if error),
            'message': str (if error)
        }
    """
    try:
        # Log incoming request for debugging
        logger.info(f"[ATTACH-FILE] Request received")
        logger.info(f"[ATTACH-FILE] Form data keys: {list(request.form.keys())}")
        logger.info(f"[ATTACH-FILE] Files keys: {list(request.files.keys())}")

        # Get required fields - with fallbacks
        user_id = request.form.get("user_id") or "anonymous-user"
        logged_in_user_id, user_id = parse_composite_user_id(user_id)

        conversation_id = request.form.get("conversation_id") or "temp-conversation"
        client_id = request.form.get("client_id", "default-client")

        logger.info(
            f"[ATTACH-FILE] Extracted - user_id: {user_id}, conv_id: {conversation_id}, client_id: {client_id}"
        )

        # Check if file is in request
        if "file" not in request.files:
            logger.warning("[ATTACH-FILE] No file part in attachment request")
            return (
                jsonify(
                    {
                        "status": "error",
                        "error": "no_file",
                        "message": "No file provided in request. Please select a file to upload.",
                    }
                ),
                400,
            )

        file = request.files["file"]

        # Allow empty user_id/conversation_id for now - use defaults
        if not user_id:
            user_id = "anonymous-user"
            logger.warning(f"[ATTACH-FILE] No user_id provided, using: {user_id}")

        logger.info(f"[ATTACH-FILE] Processing file: {file.filename}")

        # Handle attachment
        result = handle_attachment_upload(user_id, conversation_id, client_id, file)

        logger.info(f"[ATTACH-FILE] Result status: {result.get('status')}")

        # Check if upload was successful (status can be 'success' or 'ready')
        if result.get("status") in ["success", "ready"]:
            logger.info(
                f"[ATTACH-FILE] Attachment uploaded successfully - ID: {result.get('attachment_id')}"
            )
            return jsonify(result), 200
        else:
            logger.warning(
                f"[ATTACH-FILE] Attachment upload failed: {result.get('message')}"
            )
            return jsonify(result), 400

    except Exception as e:
        logger.error(
            f"[ATTACH-FILE] Error in upload_attachment endpoint: {str(e)}",
            exc_info=True,
        )
        return (
            jsonify(
                {
                    "status": "error",
                    "error": "server_error",
                    "message": f"Server error: {str(e)}",
                }
            ),
            500,
        )


@umail_bp.route("/attach-files", methods=["POST", "OPTIONS"])
@permission_required_body("taskbox.email.attachments.view")
def upload_multiple_attachments():
    # Handle CORS preflight
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"}), 200

    """
    Upload multiple attachment files for email
    
    Expected multipart/form-data payload:
    - user_id: str (optional - will use fallback if not provided)
    - conversation_id: str (optional - will use fallback if not provided)
    - client_id: str (optional)
    - files: FileStorage[] (required) - multiple files to upload
    
    Returns:
        {
            'status': 'success' | 'partial' | 'error',
            'attachments': [attachment_metadata],
            'failed': [{'filename': str, 'error': str}],
            'total_uploaded': int,
            'total_failed': int,
            'total_size': int,
            'message': str
        }
    """
    try:
        # Log incoming request for debugging
        logger.info(f"[ATTACH-FILES] Request received")
        logger.info(f"[ATTACH-FILES] Form data keys: {list(request.form.keys())}")
        logger.info(f"[ATTACH-FILES] Files keys: {list(request.files.keys())}")

        # Get required fields - with fallbacks
        user_id = request.form.get("user_id") or "anonymous-user"
        logged_in_user_id, user_id = parse_composite_user_id(user_id)
        conversation_id = request.form.get("conversation_id") or "temp-conversation"
        client_id = request.form.get("client_id", "default-client")

        logger.info(
            f"[ATTACH-FILES] Extracted - user_id: {user_id}, conv_id: {conversation_id}, client_id: {client_id}"
        )

        # Check if files are in request
        if "files" not in request.files:
            logger.warning("[ATTACH-FILES] No files part in attachment request")
            return (
                jsonify(
                    {
                        "status": "error",
                        "error": "no_files",
                        "message": "No files provided in request. Please select files to upload.",
                    }
                ),
                400,
            )

        files = request.files.getlist("files")

        if not files or all(f.filename == "" for f in files):
            logger.warning("[ATTACH-FILES] Empty files list in attachment request")
            return (
                jsonify(
                    {
                        "status": "error",
                        "error": "no_files",
                        "message": "No files provided in request. Please select files to upload.",
                    }
                ),
                400,
            )

        logger.info(f"[ATTACH-FILES] Processing {len(files)} files")

        # Handle multiple attachments
        result = handle_multiple_attachments(user_id, conversation_id, client_id, files)

        logger.info(f"[ATTACH-FILES] Result status: {result.get('status')}")

        # Check if upload was successful (status can be 'success', 'partial', or 'ready')
        if result.get("status") in ["success", "partial", "ready"]:
            logger.info(
                f"[ATTACH-FILES] Multiple attachments uploaded - Success: {result.get('total_uploaded')}, Failed: {result.get('total_failed')}"
            )
            return jsonify(result), 200
        else:
            logger.warning(
                f"[ATTACH-FILES] Multiple attachments upload failed: {result.get('message')}"
            )
            return jsonify(result), 400

    except Exception as e:
        logger.error(
            f"[ATTACH-FILES] Error in upload_multiple_attachments endpoint: {str(e)}",
            exc_info=True,
        )
        return (
            jsonify(
                {
                    "status": "error",
                    "error": "server_error",
                    "message": f"Server error: {str(e)}",
                }
            ),
            500,
        )


@umail_bp.route("/send-reply-with-attachments", methods=["POST", "OPTIONS"])
@permission_required_body("taskbox.email.send")
async def send_reply_with_attachments():
    """
    Send a reply with attachments (enhanced version of /send-reply)

    Expected JSON payload:
    {
        'user_id': str (required),
        'channel': str (required) - 'gmail' or 'zoho',
        'text': str (required),
        'ticket_conversation_id': str (optional),
        'conversation_id': str (optional),
        'contact_id': str (optional),
        'attachments': [
            {
                'attachment_id': str,
                's3_key': str,
                'filename': str,
                'mime_type': str,
                'file_size': int
            }
        ] (optional)
    }

    Returns:
        {
            'status': 'sent' | 'error',
            'id': str,
            'channel': str,
            'conversationId': str,
            'is_reply': bool,
            'attachments_count': int,
            'attachments': [attachment_info],
            'message': str (if error)
        }
    """
    # Handle CORS preflight requests
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"}), 200

    try:
        logger.info("📎 [SEND-REPLY-ATTACHMENTS] Endpoint called")

        # Extract request data
        data = request.json
        attachments_list = data.get("attachments", [])

        logger.info(
            f"📎 [SEND-REPLY-ATTACHMENTS] Processing request with {len(attachments_list)} attachment(s)"
        )

        # Simply call send_messages() - it will handle attachments from request.json
        # send_messages() is a Flask view function, so we call it directly via make_response

        # Call the send_messages function which already handles attachments
        response = await send_messages()

        # Extract response data
        if isinstance(response, tuple):
            response_json, status_code = response
            response_dict = (
                response_json.get_json()
                if hasattr(response_json, "get_json")
                else response_json
            )
        else:
            response_dict = response
            status_code = 200

        # If send was successful, enhance response with attachment info
        if status_code == 200 and response_dict.get("status") == "sent":
            response_dict["attachments_count"] = len(attachments_list)
            response_dict["attachments"] = [
                {
                    "id": att.get("attachment_id"),
                    "filename": att.get("filename"),
                    "size": att.get("file_size"),
                    "s3_key": att.get("s3_key"),
                }
                for att in attachments_list
            ]

            logger.info(
                f"📎 [SEND-REPLY-ATTACHMENTS] Message sent successfully with {len(attachments_list)} attachments - "
                f"Message ID: {response_dict.get('id')}"
            )

        return jsonify(response_dict), status_code

    except Exception as e:
        logger.error(f"❌ [SEND-REPLY-ATTACHMENTS] Error: {str(e)}")
        import traceback

        logger.error(f"❌ [SEND-REPLY-ATTACHMENTS] Traceback: {traceback.format_exc()}")
        return (
            jsonify(
                {
                    "status": "error",
                    "message": f"Error sending reply with attachments: {str(e)}",
                }
            ),
            500,
        )


@umail_bp.route("/check-lastmsg/<user_id>/<thread_id>", methods=["GET"])
@permission_required_body("taskbox.email.view")
def checkgmail_last_msg(user_id, thread_id):
    logged_in_user_id, user_id = parse_composite_user_id(user_id)

    import asyncio

    val = GmailService(user_id=user_id)
    data = asyncio.run(val.get_thread_last_message_direction(thread_id=thread_id))
    return data, 200


@umail_bp.route("/set_mailbox_setting", methods=["POST"])
@permission_required_body("taskbox.email.view")
def set_mailbox_setting():

    body = request.get_json() or {}
    primary_user_id = body.get("user_id")
    logged_in_user_id, primary_user_id = parse_composite_user_id(primary_user_id)

    setting = body.get("setting")
    social = body.get("social")

    # print(f"{primary_user_id} | {setting}  | {social}")

    if primary_user_id is None or setting not in ("true", "false"):
        return {"error": "user_id and setting required"}, 400

    mailbox_value = 1 if setting == "true" else 0

    connection = connect_to_rds()
    cursor = connection.cursor()

    try:
        integrations = get_integration_users(primary_user_id, connection)
        users_to_update = []
        for integration in integrations:

            platform = integration["platform"]

            if platform != social:
                continue

            uid = integration["user_id"]
            access_token = integration["access_token"]
            email = integration["email"]
            primary_user_id = integration["primary_user_id_fk"]
            success = False
            # print(f"uid: {uid} ")
            # print(f"primary_user_id: {primary_user_id} ")
            if platform == "google":
                service = GmailService(user_id=primary_user_id, integration=integration)
                if setting == "true":
                    success = service.create_watch_req()
                else:
                    success = service.stop_watch()

            elif platform == "microsoft":
                manager = OutlookSubscriptionManager()

                if setting == "true":
                    cursor.execute(
                        "SELECT token, email FROM users WHERE user_id = %s", (uid,)
                    )
                    row = cursor.fetchone()
                    if not row or not row[0]:
                        # continue
                        cursor.execute(
                            "SELECT access_token, email FROM integrations WHERE primary_user_id_fk = %s",
                            (primary_user_id,),
                        )
                        row = cursor.fetchone()
                    access_token = row[0]
                    email = row[1]
                    success = manager.create_subscription_async(access_token, email)
                else:
                    success = manager.delete_subscription(uid)
            if success:
                users_to_update.append(primary_user_id)

        user_ids = [u["user_id"] for u in users_to_update]
        if user_ids:
            placeholders = ",".join(["%s"] * len(user_ids))

            cursor.execute(
                f"UPDATE users SET mailbox = %s WHERE user_id IN ({placeholders})",
                [mailbox_value, *user_ids],
            )

        connection.commit()
        return {"success": True}, 200

    except Exception as e:
        connection.rollback()
        logger.error(
            "Mailbox toggle failed for primary user %s: %s", primary_user_id, e
        )
        return {"error": f"Failed to update mailbox,{str(e)}"}, 500

    finally:
        cursor.close()
        connection.close()


@umail_bp.route("/send-mail", methods=["POST"])
@permission_required_body("taskbox.email.send")
async def send_mail_api():
    try:
        data = request.json
        user_id = data.get("user_id")
        logged_in_user_id, user_id = parse_composite_user_id(user_id)
        client_id = data.get("client_id")
        channel = data.get("channel")
        text = data.get("text")
        attachments = data.get("attachments", [])
        if not all([user_id, client_id, text]):
            return jsonify({"error": "Missing required fields"}), 400

        connection = connect_to_rds()
        if connection is None:
            return jsonify({"error": "Database connection failed"}), 500

        cursor = connection.cursor()

        # ---------------------------------
        # Get user email
        # ---------------------------------
        cursor.execute("SELECT email,social FROM users WHERE user_id = %s", (user_id,))
        bases = cursor.fetchone()
        # print("bases", bases)
        user_email = bases[0]
        # channel = bases[1]

        # ---------------------------------
        # Get client email
        # ---------------------------------
        cursor.execute(
            "SELECT email_id FROM users_clients WHERE users_clients_id = %s",
            (client_id,),
        )
        c_email = cursor.fetchone()
        client_email = c_email[0]

        if not client_email:
            return jsonify({"error": "Client email not found"}), 404

        # ---------------------------------
        # Generate subject (optional AI)
        # ---------------------------------
        subject = f"New Message from {user_email}"

        # ---------------------------------
        # Send email (NEW THREAD)
        # ---------------------------------
        sent_message_id = None
        sent_thread_id = None

        if channel == "gmail" or channel == "google":
            result = send_mail(
                user_id=user_id,
                to=client_email,
                subject=subject,
                body_text=text,
                attachments=attachments or [],
            )
            if result.get("status") != "success":
                return jsonify({"error": "Gmail send failed"}), 500
            print("result", result)

            sent_message_id = result.get("message_id")
            sent_thread_id = result.get("thread_id")

        elif channel == "outlook":
            sent = outlook_send_mail(
                user_id=user_id,
                to=client_email,
                subject=subject,
                body_text=text,
                attachments=attachments or [],
            )

            if not sent:
                return jsonify({"error": "Outlook send failed"}), 500
            sent_message_id = sent.get("message_id")
            sent_thread_id = sent.get("thread_id")

        # elif channel == "zoho":
        #     response_payload, status_code = send_zoho_email(
        #         user_id=user_id,
        #         to_email=client_email,
        #         subject=subject,
        #         body_text=text,
        #         from_user_email=user_email,
        #     )

        #     if status_code not in [200, 201]:
        #         return jsonify({"error": response_payload.get("error")}), status_code

        else:
            return jsonify({"error": "Unsupported channel"}), 400
        # ---------------------------------
        # Generate new conversation ID
        # ---------------------------------
        if sent_thread_id:
            conversation_id = f"{user_id}_{sent_thread_id}"
        else:
            conversation_id = str(uuid.uuid4())
        # ---------------------------------
        # Save conversation in DB
        # ---------------------------------
        now_utc = datetime.now(timezone.utc).isoformat()

        cursor.execute(
            """
            INSERT INTO threads (conversation_id, started_at, status, last_message_at,external_user_id )
            VALUES (%s, %s, %s, %s, %s)
            """,
            (conversation_id, now_utc, "Open", now_utc, user_id),
        )

        # ---------------------------------
        # Save message in DB
        # ---------------------------------
        if not sent_message_id:
            return {"error": "error storing"}, 402
        msg_id = sent_message_id

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
                update_at,
                sender_type
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                msg_id,
                conversation_id,
                client_id,
                f"{user_id}/messages/{client_id}/{conversation_id}.json",
                "outbound",
                subject,
                now_utc,
                now_utc,
                channel,
            ),
        )

        connection.commit()

        # ---------------------------------
        # Create S3 config.json entry
        # ---------------------------------
        s3_config_key = f"{user_id}/messages/{client_id}/config.json"
        config_data = read_json_from_s3(s3_config_key) or {"conversations": []}

        config_data["conversations"].append(
            {
                "conv_id": conversation_id,
                "subject": subject,
                "thread_id": sent_thread_id,
                "created_at": now_utc,
            }
        )

        local_config_path = f"/tmp/{client_id}_config.json"
        with open(local_config_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=2)

        upload_any_file(
            local_config_path,
            user_id,
            type="messages",
            s3_key_C=s3_config_key,
        )

        # ---------------------------------
        # Create conversation file in S3
        # ---------------------------------
        message = {
            "id": msg_id,
            "user_id": user_id,
            "client_id": client_id,
            "from": user_email,
            "to": client_email,
            "body": text,
            "timestamp": now_utc,
            "channel": channel,
            "direction": "outbound",
            "subject": subject,
            "thread_id": sent_thread_id,
            "conversation_id": conversation_id,
        }

        conversation_payload = {"input_data": [message]}

        local_folder = f"/tmp/{conversation_id}"
        os.makedirs(local_folder, exist_ok=True)

        local_conv_path = f"{local_folder}/{conversation_id}.json"
        with open(local_conv_path, "w", encoding="utf-8") as f:
            json.dump(conversation_payload, f, indent=2)

        s3_conv_key = f"{user_id}/messages/{client_id}/{conversation_id}.json"

        upload_any_file(
            local_conv_path,
            user_id,
            type="messages",
            s3_key_C=s3_conv_key,
        )

        # ---------------------------------
        # LanceDB Embed
        # ---------------------------------
        client = UmailLanceClient(user_id)
        # await client.embed_json_file_for_reply(
        #     conversation_payload["input_data"], user_id, client_id, conversation_id
        # )
        await client.embed_both_json_and_plain(folder_path=local_folder)

        return (
            jsonify(
                {
                    "status": "sent",
                    "conversation_id": conversation_id,
                    "message_id": msg_id,
                    "channel": channel,
                }
            ),
            200,
        )

    except Exception as e:
        print("ERROR", e)
        return jsonify({"error": "Internal server error"}), 500

    finally:
        if "connection" in locals():
            connection.close()
