import asyncio
from db.lance_db_service import LanceDBServer
from db.rds_db import get_cursor, safe_execute
from flask import Blueprint, current_app, request, jsonify, session, send_file
from services.gmail_service import GmailService
from umail_helper.ticketalloc import TicketAllocator

# from utils.delay_mails import DelayTrigger

import uuid
from bs4 import BeautifulSoup
from email.utils import parsedate_to_datetime, parseaddr
import base64
from datetime import datetime, timezone
import json
from cust_helpers import pathconfig
import os
from utils.normal import can_reply_to_email, ensure_dir
from utils.s3_utils import (
    delete_folder_from_s3,
    upload_any_file,
    read_json_from_s3,
)
from db.rds_db import connect_to_rds
from umail_helper.helper import get_users_client_id, extract_reply_content
from collections import defaultdict
import traceback
import re
import pymysql


gmail_bp = Blueprint("gmail", __name__)


# @gmail_bp.route("/gmail/fetch")


async def fetch_gmail_messages_batch(user_id, page_token=None, batch_size=100):
    """
    Fetch a single batch of Gmail messages
    """
    try:
        # print(f"🚀 Starting Gmail batch fetch for user {user_id}")
        gmail_service = GmailService(user_id)
        user_email = gmail_service.user_email

        # Fetch one batch of messages
        threads, next_page_token = await gmail_service.get_threads_async(
            "INBOX", max_results=batch_size, start_page_token=page_token
        )
        # print("threads fetched are", len(threads))
        if not threads:
            # print("📭 No threads found in this batch")
            return {"status": "success", "new_messages": 0, "next_page_token": None}

        count_new = 0
        grouped_messages = defaultdict(list)

        # Database connection
        connection = connect_to_rds()
        if connection is None:
            return {"error": "Database connection failed", "status": "failed"}

        cursor = connection.cursor()

        # File setup
        timestamp = datetime.now(timezone.utc)
        date_str = timestamp.strftime("%Y-%m-%d")
        filename = f"{date_str}.json"

        user_folder = os.path.join(pathconfig.basepath, "messages", user_id)
        ensure_dir(user_folder)
        filepath = os.path.join(user_folder, filename)

        # Load existing data
        existing_ids_local = set()
        input_data_local = {}

        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                existing_data_local = json.load(f)
                input_data_local = existing_data_local.get("input_data", {})
            if len(input_data_local) > 0:
                try:
                    # Extract existing message IDs
                    for client_data in input_data_local.values():
                        if isinstance(client_data, dict):
                            for client_channels in client_data.values():
                                if isinstance(client_channels, dict):
                                    for channel_msgs in client_channels.values():
                                        if isinstance(channel_msgs, list):
                                            for msg in channel_msgs:
                                                if isinstance(msg, dict):
                                                    msg_id = msg.get("id")
                                                    if msg_id:
                                                        existing_ids_local.add(msg_id)
                except Exception as e:
                    # print(f"⚠️ Error loading existing data: {e}")
                    return e

        # Process messages (your existing logic)
        email_to_client_id = {}
        configs_created = set()

        first_time_user = True
        cursor.execute(
            "SELECT 1 FROM messages m JOIN threads th  ON m.conversation_id_fk = th.conversation_id WHERE th.external_user_id = %s",
            (user_id,),
        )

        if cursor.fetchone():
            first_time_user = False

        for msg in threads:
            message_id = msg["messageId"]

            # Skip if already exists in database
            cursor.execute(
                "SELECT 1 FROM messages WHERE message_id = %s", (message_id,)
            )
            if cursor.fetchone():
                continue

            # Skip if already exists locally
            if message_id in existing_ids_local:
                continue

            # Your existing message processing logic...
            thread_id = msg["thread_id"]
            dt = parsedate_to_datetime(msg["date"])
            timestamp_iso = dt.isoformat()
            direction = "inbound" if msg["from"] != user_email else "outbound"
            subject = msg["subject"]
            body_content = msg.get("body", "")
            # ✅ FIXED: Keep HTML body as-is with images and formatting
            # Don't convert to plain text - that destroys all embedded images and HTML structure
            extracted_body = body_content  # Keep original HTML/text format

            from_name, from_email = parseaddr(msg["from"])
            to_name, to_email = parseaddr(msg.get("to", ""))

            if direction == "inbound":
                participant = from_email
                participant_name = from_name
            else:
                participant = to_email
                participant_name = to_name

            # Get or create client
            if participant in email_to_client_id:
                client_id, type = email_to_client_id[participant]
            else:
                client_id, type = get_users_client_id(participant, user_id, cursor)
                if can_reply_to_email(participant):
                    if not client_id:
                        if not first_time_user:
                            client_id = add_lead_contact(
                                user_id, cursor, participant, participant_name
                            )
                            type = "Lead"
                        if first_time_user:
                            client_id = add_customer_contact(
                                user_id, cursor, participant, participant_name
                            )
                            type = "Customer"
                    email_to_client_id[participant] = (client_id, type)

            # Create message object
            message = {
                "id": message_id,
                "from": from_email,
                "to": user_email,
                "body": extracted_body,
                "subject": subject,
                "timestamp": timestamp_iso,
                "source": "gmail",
                "direction": direction,
                "user_id": user_id,
                "thread_id": thread_id,
                "conversation_id": from_email if direction == "inbound" else user_email,
                "type": type,
            }

            grouped_messages.setdefault(client_id, {}).setdefault("gmail", []).append(
                message
            )
            count_new += 1

            if client_id not in configs_created:

                # Create config files if needed (your existing logic)
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
                        f.flush()
                        os.fsync(f.fileno())

                    s3_config_key = f"{user_id}/messages/{client_id}/config.json"
                    s3_data = read_json_from_s3(s3_config_key)
                    if s3_data is None:

                        upload_any_file(
                            config_filepath,
                            user_id,
                            type="messages",
                            s3_key_C=s3_config_key,
                        )
                    # print(f"uploaded config for client_id: {client_id}")

                configs_created.add(client_id)

        # Merge with existing data and save
        existing_data = {}
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                existing_data = json.load(f)

        merged_messages = existing_data.get("input_data", {})

        for client_id, channels in grouped_messages.items():
            for channel, messages in channels.items():
                merged_messages.setdefault(client_id, {}).setdefault(
                    "gmail", []
                ).extend(messages)

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(
                {"filename": filename, "input_data": merged_messages}, f, indent=2
            )

        cursor.close()
        connection.close()

        # print(f"✅ Batch complete: {count_new} new messages processed")
        return {
            "status": "success",
            "new_messages": count_new,
            "next_page_token": next_page_token,
            "grouped_messages": dict(grouped_messages),  # Return current batch data
        }

    except Exception as e:
        # print(f"[ERROR] → fetch_gmail_messages_batch failed: {e}")
        return {
            "error": str(e),
            "status": "failed",
            "next_page_token": None,
            "grouped_messages": {},
        }


def safe_json_load(filepath):
    if not os.path.exists(filepath):
        return {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return {}
            return json.loads(content)
    except json.JSONDecodeError:
        # print(f"⚠️ Corrupted JSON at {filepath}, resetting.")
        return {}


async def v2fetch_gmail_messages_batch_og(
    user_id, threads, my_email, batch_count, connection
):
    """
    Fetch a single batch of Gmail messages
    """
    try:
        if connection is None:
            new_connection = connect_to_rds()
            connection = new_connection
        # Use cursor context for all DB operations
        cursor = connection.cursor()
        # print(f"🚀 Starting Gmail batch {batch_count} fetch for user {user_id}")
        gmail_service = GmailService(user_id, connection)
        # user_email = gmail_service.user_email

        # Fetch one batch of messages - THIS IS WHERE MIME EXTRACTION HAPPENS
        # print(
        #     f"\n📧 [GMAIL SYNC] Calling process_threads_batch with {len(threads)} threads"
        # )
        results = await gmail_service.process_threads_batch(
            threads, my_email, batch_count
        )
        # print(
        #     f"GOT the data from results {batch_count} in v2 fetch gmail", len(results)
        # )
        # all_messages = []
        # for idx, thread in enumerate(threads):
        #     thread_id = thread["id"]
        #     res = results.get(thread_id)

        #     if not res:
        #         print(f"⚠️ No response for thread {thread_id}")
        #         continue

        #     thread_data, err = res  # ✅ unpack tuple

        #     if err:
        #         print(f"⚠️ Thread {thread_id} error: {err}")
        #         continue

        #     if thread_data:
        #         all_messages.append(thread_data)
        # print("ALL mesages complete")

        if not results:
            # print("📭 No messages found in this batch")
            return {"status": "success", "new_messages": 0, "next_page_token": None}
        count_new = 0
        grouped_messages = defaultdict(list)
        # cursor = connection.cursor()

        # File setup
        timestamp = datetime.now(timezone.utc)
        date_str = timestamp.strftime("%Y-%m-%d")
        filename = f"{date_str}.json"

        user_folder = os.path.join(pathconfig.basepath, "messages", user_id)
        ensure_dir(user_folder)
        filepath = os.path.join(user_folder, filename)

        # Load existing data
        existing_ids_local = set()
        input_data_local = {}

        if os.path.exists(filepath):
            # print("filepath exists")
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    existing_data_local = json.load(f)
                    input_data_local = existing_data_local.get("input_data", {})
                # print("file loaded")

                # Extract existing message IDs
                for client_data in input_data_local.values():
                    if isinstance(client_data, dict):
                        for client_channels in client_data.values():
                            if isinstance(client_channels, dict):
                                for channel_msgs in client_channels.values():
                                    if isinstance(channel_msgs, list):
                                        for msg in channel_msgs:
                                            if isinstance(msg, dict):
                                                msg_id = msg.get("id")
                                                if msg_id:
                                                    idms = f"{user_id}_{msg_id}"
                                                    existing_ids_local.add(idms)
            except Exception as e:
                # print(f"⚠️ Error loading existing data: {e}")
                return e

        # Process messages (your existing logic)
        email_to_client_id = {}
        client_id = ""
        client_type = ""
        configs_created = set()

        first_time_user = True
        ##print("starting checks for messages in db")
        cursor.execute(
            "SELECT m.message_id,th.conversation_id FROM messages m JOIN threads th  ON m.conversation_id_fk = th.conversation_id WHERE th.external_user_id = %s",
            (user_id,),
        )
        # print("got a result")
        rows = cursor.fetchall()
        if rows:
            # cursor.execute(
            #     """
            # SELECT COUNT(*)
            # FROM messages m
            # JOIN threads th ON m.conversation_id_fk = th.conversation_id
            # WHERE th.external_user_id = %s
            # """,
            #     (user_id,),
            # )
            ##print("Count of rows:", cursor.fetchone())

            ##print("GOT CURSOR RESULTS Line 382 v2 fetch", rows)
            first_time_user = False
        else:
            # print("ERROR dont got any result")
            return
        ##print("goting into for loop of all messages")
        for idx, thread in enumerate(threads):
            thread_id = thread["id"]
            res = results.get(thread_id)

            if not res:
                # print(f"⚠️ No response for thread {thread_id}")
                continue

            thread_data, err = res  # ✅ unpack tuple

            if err:
                # print(f"⚠️ Thread {thread_id} error: {err}")
                continue

            if not thread_data:
                continue

            # directly iterate messages here
            for msg in thread_data:
                message_id = msg["messageId"]
                row_id = f"{user_id}_{message_id}"
                # print({"message_id": row_id, "timestamp": msg["date"]})
                # Skip if already exists in database
                sql = """
                SELECT m.conversation_id_fk,
                    m.sender_id,
                    t.external_user_id
                FROM messages m
                JOIN threads t
                ON m.conversation_id_fk = t.conversation_id     -- join condition
                WHERE m.message_id = %s
                """
                cursor.execute(sql, (row_id,))
                row = cursor.fetchone()

                if row:
                    external_user_id = row[2]
                    ##print("GOT CURSOR RESULTS Line 389 v2 fetch", row)

                    if external_user_id == user_id:
                        continue
                    # print("message added already")
                    # skip/continue whatever you need

                # ⭐ FIXED: Do NOT skip existing messages!
                # Old messages need to be reprocessed to extract HTML with images
                # The MIME extraction now preserves embedded images, so we need to update old messages
                # ONLY skip if it's VERY recent (last 1 hour) to avoid reprocessing duplicates
                if message_id in existing_ids_local:
                    # Check if this is a very recent message (last hour)
                    # If so, skip. If older, reprocess with MIME extraction
                    msg_time = parsedate_to_datetime(msg["date"])
                    time_since_msg = datetime.now(timezone.utc) - msg_time
                    if time_since_msg.total_seconds() < 3600:  # Less than 1 hour old
                        # print(
                        #     f"skipping recent message in v2fetch gmail (already processed): {message_id}"
                        # )
                        continue
                    else:
                        # print(
                        #     f"⭐ REPROCESSING old message with MIME extraction: {message_id} (age: {time_since_msg.total_seconds()/3600:.1f}h)"
                        # )
                        # Remove from local cache to force reprocessing
                        existing_ids_local.discard(message_id)

                # Your existing message processing logic...
                thread_id = msg["thread_id"]
                dt = parsedate_to_datetime(msg["date"])
                timestamp_iso = dt.isoformat()
                direction = msg["direction"]
                subject = msg["subject"]
                body_content = msg.get("body", "")
                # ✅ FIXED: Keep HTML body as-is with images and formatting
                # Don't convert to plain text - that destroys all embedded images and HTML structure
                # The backend already extracted clean HTML with 54 Base64 images embedded
                extracted_body = body_content  # Keep original HTML/text format

                from_name, from_email = parseaddr(msg["from"])
                to_name, to_email = parseaddr(msg.get("to", ""))

                if direction == "inbound":
                    participant = from_email
                    participant_name = from_name
                else:
                    participant = to_email
                    participant_name = to_name

                # Get or create client
                if participant in email_to_client_id:
                    client_id, client_type = email_to_client_id[participant]
                else:
                    result = get_users_client_id(participant, user_id, cursor)

                    if isinstance(result, tuple) and len(result) == 2:
                        client_id, client_type = result
                    else:
                        # assume function returned only client_id
                        client_id = result if result else None
                        client_type = None

                    if not client_id:
                        if not first_time_user:
                            # print(f"first_time_user : {first_time_user}")
                            client_id = add_lead_contact(
                                user_id, cursor, participant, participant_name
                            )
                            client_type = "Lead"
                        if first_time_user:
                            # print(f"first_time_user : {first_time_user}")
                            client_id = add_customer_contact(
                                user_id, cursor, participant, participant_name
                            )
                            client_type = "Customer"

                    email_to_client_id[participant] = (client_id, client_type)

                # Create message object
                # 📎 DEBUG: Show attachments BEFORE message dict creation
                gmail_attachments = msg.get("attachments", [])
                # print(
                #     f"📎 [GMAIL_ROUTE] Before message dict - msg has {len(gmail_attachments)} attachments"
                # )
                # for att in gmail_attachments[:2]:
                #     print(f"   - {att.get('filename', '?')}: {att.get('status', '?')}")

                message = {
                    "id": row_id,
                    "from": from_email,
                    "to": to_email,
                    "cc": msg.get("cc", ""),
                    "bcc": msg.get("bcc", ""),
                    "body": extracted_body,
                    "subject": subject,
                    "timestamp": timestamp_iso,
                    "source": "gmail",
                    "direction": direction,
                    "user_id": user_id,
                    "thread_id": thread_id,
                    "conversation_id": (
                        from_email if direction == "inbound" else my_email
                    ),
                    "type": client_type,
                    "attachments": gmail_attachments,
                }

                # 📎 DEBUG: Show attachments AFTER message dict creation
                # print(
                #     f"📧 [GMAIL_ROUTE] After message dict - message has {len(message.get('attachments', []))} attachments"
                # )
                # if message.get("attachments"):
                #     for att in message["attachments"][:2]:
                #     #print(
                #             f"     ✅ {att.get('filename', '?')}: {att.get('status', '?')}"
                #         )
                # else:
                #     print(f"     ⚠️ NO attachments in message dict!")

                grouped_messages.setdefault(client_id, {}).setdefault(
                    "gmail", []
                ).append(message)
                count_new += 1

                if client_id not in configs_created:

                    # Create config files if needed (your existing logic)
                    config_folder = os.path.join(
                        pathconfig.basepath, "messages", user_id, client_id
                    )
                    ensure_dir(config_folder)
                    config_filepath = os.path.join(config_folder, "config.json")
                    if not os.path.exists(config_filepath):
                        dummy_config = {
                            "userclients_id": client_id,
                            "conversations": [],
                        }
                        with open(config_filepath, "w", encoding="utf-8") as f:
                            json.dump(dummy_config, f, indent=2)
                            f.flush()
                            os.fsync(f.fileno())

                        s3_config_key = f"{user_id}/messages/{client_id}/config.json"
                        s3_data = read_json_from_s3(s3_config_key)
                        if s3_data is None:

                            upload_any_file(
                                config_filepath,
                                user_id,
                                type="messages",
                                s3_key_C=s3_config_key,
                            )
                            # print(f"uploaded config for client_id: {client_id}")

                    configs_created.add(client_id)

        # Merge with existing data and save
        existing_data = safe_json_load(filepath)

        merged_messages = existing_data.get("input_data", {})

        for client_id, channels in grouped_messages.items():
            for channel, messages in channels.items():
                merged_messages.setdefault(client_id, {}).setdefault(
                    "gmail", []
                ).extend(messages)

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(
                {"filename": filename, "input_data": merged_messages}, f, indent=2
            )

        # cursor.close()
        # connection.close()

        # print(f"✅ Batch complete: {count_new} new messages processed")
        return {
            "status": "success",
            "new_messages": count_new,
            "next_page_token": None,
            "grouped_messages": dict(grouped_messages),  # Return current batch data
        }

    except Exception as e:
        # print(f"[ERROR] → v2fetch_gmail_messages_batch failed: {e}")
        return {
            "error": str(e),
            "status": "failed",
            "next_page_token": None,
            "grouped_messages": {},
        }
    finally:
        if connection is None and new_connection:
            new_connection.close()


async def v2fetch_gmail_messages_batch(
    user_id, threads, my_email, batch_count, connection, integration=None
):
    """
    Fetch a single batch of Gmail messages
    """
    try:
        if connection is None:
            new_connection = connect_to_rds()
            connection = new_connection
        # Use cursor context for all DB operations
        cursor = connection.cursor()
        # print(f"🚀 Starting Gmail batch {batch_count} fetch for user {user_id}")
        # print(f"integration inside v2fetch_gmail_messages_batch : {integration} ")
        gmail_service = GmailService(user_id, connection, integration=integration)
        # user_email = gmail_service.user_email

        # Fetch one batch of messages - THIS IS WHERE MIME EXTRACTION HAPPENS
        # print(
        #     f"\n📧 [GMAIL SYNC] Calling process_threads_batch with {len(threads)} threads"
        # )
        results = await gmail_service.process_threads_batch(
            threads, my_email, batch_count
        )
        if not results:
            # print("📭 No messages found in this batch")
            return {"status": "success", "new_messages": 0, "next_page_token": None}
        count_new = 0
        grouped_messages = defaultdict(list)
        # cursor = connection.cursor()

        # File setup
        timestamp = datetime.now(timezone.utc)
        date_str = timestamp.strftime("%Y-%m-%d")
        filename = f"{date_str}.json"

        user_folder = os.path.join(pathconfig.basepath, "messages", user_id)
        ensure_dir(user_folder)
        filepath = os.path.join(user_folder, filename)

        # Load existing data
        existing_ids_local = set()
        input_data_local = {}

        if os.path.exists(filepath):
            # print("filepath exists")
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    existing_data_local = json.load(f)
                    input_data_local = existing_data_local.get("input_data", {})
                # print("file loaded")

                # Extract existing message IDs
                for client_data in input_data_local.values():
                    if isinstance(client_data, dict):
                        for client_channels in client_data.values():
                            if isinstance(client_channels, dict):
                                for channel_msgs in client_channels.values():
                                    if isinstance(channel_msgs, list):
                                        for msg in channel_msgs:
                                            if isinstance(msg, dict):
                                                msg_id = msg.get("id")
                                                if msg_id:
                                                    idms = f"{user_id}_{msg_id}"
                                                    existing_ids_local.add(idms)
            except Exception as e:
                # print(f"⚠️ Error loading existing data: {e}")
                return e

        # Process messages (your existing logic)
        email_to_client_id = {}
        client_id = ""
        client_type = ""
        configs_created = set()

        first_time_user = True
        ##print("starting checks for messages in db")
        cursor.execute(
            "SELECT m.message_id,th.conversation_id FROM messages m JOIN threads th  ON m.conversation_id_fk = th.conversation_id WHERE th.external_user_id = %s",
            (user_id,),
        )
        # print("got a result")
        rows = cursor.fetchall()
        if rows:
            # cursor.execute(
            #     """
            # SELECT COUNT(*)
            # FROM messages m
            # JOIN threads th ON m.conversation_id_fk = th.conversation_id
            # WHERE th.external_user_id = %s
            # """,
            #     (user_id,),
            # )
            ##print("Count of rows:", cursor.fetchone())

            ##print("GOT CURSOR RESULTS Line 382 v2 fetch", rows)
            first_time_user = False
        # else:
        #     print("ERROR dont got any result")
        ##print("goting into for loop of all messages")
        for idx, thread in enumerate(threads):
            thread_id = thread["id"]
            res = results.get(thread_id)

            if not res:
                # print(f"⚠️ No response for thread {thread_id}")
                continue

            thread_data, err = res  # ✅ unpack tuple

            if err:
                # print(f"⚠️ Thread {thread_id} error: {err}")
                continue

            if not thread_data:
                continue
            # print(f"thread_data :")
            # print(f"{thread_data}")
            # directly iterate messages here
            for msg in thread_data:

                message_id = msg["messageId"]
                row_id = f"{user_id}_{message_id}"
                # print(f"row_id : {row_id}")
                # print({"message_id": row_id, "timestamp": msg["date"]})
                # Skip if already exists in database
                sql = """
                SELECT m.conversation_id_fk,
                    m.sender_id,
                    t.external_user_id
                FROM messages m
                JOIN threads t
                ON m.conversation_id_fk = t.conversation_id     -- join condition
                WHERE m.message_id = %s
                """
                cursor.execute(sql, (row_id,))
                row = cursor.fetchone()

                if row:
                    external_user_id = row[2]
                    ##print("GOT CURSOR RESULTS Line 389 v2 fetch", row)
                    # print(f"external_user_id : {external_user_id} | user_id: {user_id}")
                    if external_user_id == user_id:
                        # print("message added already")
                        # skip/continue whatever you need
                        continue

                # ⭐ FIXED: Do NOT skip existing messages!
                # Old messages need to be reprocessed to extract HTML with images
                # The MIME extraction now preserves embedded images, so we need to update old messages
                # ONLY skip if it's VERY recent (last 1 hour) to avoid reprocessing duplicates
                if message_id in existing_ids_local:
                    # Check if this is a very recent message (last hour)
                    # If so, skip. If older, reprocess with MIME extraction
                    msg_time = parsedate_to_datetime(msg["date"])
                    time_since_msg = datetime.now(timezone.utc) - msg_time
                    if time_since_msg.total_seconds() < 3600:  # Less than 1 hour old
                        # print(
                        #     f"skipping recent message in v2fetch gmail (already processed): {message_id}"
                        # )
                        continue
                    else:
                        # print(
                        #     f"⭐ REPROCESSING old message with MIME extraction: {message_id} (age: {time_since_msg.total_seconds()/3600:.1f}h)"
                        # )
                        # Remove from local cache to force reprocessing
                        existing_ids_local.discard(message_id)

                # Your existing message processing logic...
                thread_id = msg["thread_id"]
                dt = parsedate_to_datetime(msg["date"])
                timestamp_iso = dt.isoformat()
                direction = msg["direction"]
                subject = msg["subject"]
                body_content = msg.get("body", "")
                plain_text = msg.get("plain_text", "")
                # ✅ FIXED: Keep HTML body as-is with images and formatting
                # Don't convert to plain text - that destroys all embedded images and HTML structure
                # The backend already extracted clean HTML with 54 Base64 images embedded
                extracted_body = body_content  # Keep original HTML/text format

                from_name, from_email = parseaddr(msg["from"])
                to_name, to_email = parseaddr(msg.get("to", ""))

                if direction == "inbound":
                    participant = from_email
                    participant_name = from_name
                else:
                    participant = to_email
                    participant_name = to_name

                # Get or create client
                if participant in email_to_client_id:
                    client_id, client_type = email_to_client_id[participant]
                else:
                    result = get_users_client_id(participant, user_id, cursor)

                    if isinstance(result, tuple) and len(result) == 2:
                        client_id, client_type = result
                    else:
                        # assume function returned only client_id
                        client_id = result if result else None
                        client_type = None

                    if not client_id:
                        if not first_time_user:
                            # print(f"first_time_user : {first_time_user}")
                            client_id = add_lead_contact(
                                user_id, cursor, participant, participant_name
                            )
                            client_type = "Lead"
                        if first_time_user:
                            # print(f"first_time_user : {first_time_user}")
                            client_id = add_customer_contact(
                                user_id, cursor, participant, participant_name
                            )
                            client_type = "Customer"

                    email_to_client_id[participant] = (client_id, client_type)

                # Create message object
                # 📎 DEBUG: Show attachments BEFORE message dict creation
                gmail_attachments = msg.get("attachments", [])
                # print(
                #     f"📎 [GMAIL_ROUTE] Before message dict - msg has {len(gmail_attachments)} attachments"
                # )
                # for att in gmail_attachments[:2]:
                #     print(f"   - {att.get('filename', '?')}: {att.get('status', '?')}")

                message = {
                    "id": row_id,
                    "from": from_email,
                    "to": to_email,
                    "cc": msg.get("cc", ""),
                    "bcc": msg.get("bcc", ""),
                    "body": extracted_body,
                    "plain_text": plain_text,
                    "subject": subject,
                    "timestamp": timestamp_iso,
                    "source": "gmail",
                    "direction": direction,
                    "user_id": user_id,
                    "thread_id": thread_id,
                    "conversation_id": (
                        from_email if direction == "inbound" else my_email
                    ),
                    "type": client_type,
                    "attachments": gmail_attachments,
                }

                # 📎 DEBUG: Show attachments AFTER message dict creation
                # print(
                #     f"📧 [GMAIL_ROUTE] After message dict - message has {len(message.get('attachments', []))} attachments"
                # )
                # if message.get("attachments"):
                #     for att in message["attachments"][:2]:
                #         print(
                #             f"     ✅ {att.get('filename', '?')}: {att.get('status', '?')}"
                #         )
                # else:
                #     print(f"     ⚠️ NO attachments in message dict!")

                grouped_messages.setdefault(client_id, {}).setdefault(
                    "gmail", []
                ).append(message)
                count_new += 1

                if client_id not in configs_created:

                    # Create config files if needed (your existing logic)
                    config_folder = os.path.join(
                        pathconfig.basepath, "messages", user_id, client_id
                    )
                    ensure_dir(config_folder)
                    config_filepath = os.path.join(config_folder, "config.json")
                    if not os.path.exists(config_filepath):
                        dummy_config = {
                            "userclients_id": client_id,
                            "conversations": [],
                        }
                        with open(config_filepath, "w", encoding="utf-8") as f:
                            json.dump(dummy_config, f, indent=2)
                            f.flush()
                            os.fsync(f.fileno())

                        s3_config_key = f"{user_id}/messages/{client_id}/config.json"
                        s3_data = read_json_from_s3(s3_config_key)
                        if s3_data is None:

                            upload_any_file(
                                config_filepath,
                                user_id,
                                type="messages",
                                s3_key_C=s3_config_key,
                            )
                            # print(f"uploaded config for client_id: {client_id}")

                    configs_created.add(client_id)

        # Merge with existing data and save
        existing_data = safe_json_load(filepath)

        merged_messages = existing_data.get("input_data", {})

        for client_id, channels in grouped_messages.items():
            for channel, messages in channels.items():
                merged_messages.setdefault(client_id, {}).setdefault(
                    "gmail", []
                ).extend(messages)

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(
                {"filename": filename, "input_data": merged_messages}, f, indent=2
            )

        # cursor.close()
        # connection.close()

        # print(f"✅ Batch complete: {count_new} new messages processed")
        return {
            "status": "success",
            "new_messages": count_new,
            "next_page_token": None,
            "grouped_messages": dict(grouped_messages),  # Return current batch data
        }

    except Exception as e:
        # print(f"[ERROR] → v2fetch_gmail_messages_batch failed: {e}")
        return {
            "error": str(e),
            "status": "failed",
            "next_page_token": None,
            "grouped_messages": {},
        }
    finally:
        if connection is None and new_connection:
            new_connection.close()


# @gmail_bp.route("/gmail/sync_gmail_contacts/<user_id>")
def sync_gmail_contacts(user_id):
    # print(f"🚀 Starting sync_gmail_contacts for user_id: {user_id}")

    try:
        # Initialize Gmail service
        # print("📧 Initializing Gmail service...")
        gmail_service = GmailService(user_id)

        # Get contacts
        # print("🔍 Fetching contacts from Gmail...")
        messages = gmail_service.get_contacts()
        # print(f"📬 Retrieved {len(messages)} contact entries")

        if not messages:
            # print("⚠️ No messages retrieved from Gmail")
            response = jsonify(
                {
                    "success": True,
                    "message": "No contacts found",
                    "results": [],
                    "count": 0,
                }
            )
            # print(f"📤 Returning response: {response.get_json()}")
            return response

        # Database connection
        # print("🗄️ Connecting to database...")
        connection = connect_to_rds()
        if connection is None:
            # print("❌ Database connection failed")
            error_response = jsonify(
                {"success": False, "error": "Database connection failed", "results": []}
            )
            # print(f"📤 Returning error response: {error_response.get_json()}")
            return error_response, 500

        # print("✅ Database connection successful")
        cursor = connection.cursor()
        results = []
        processed_count = 0
        skipped_count = 0

        for i, item in enumerate(messages):
            try:
                # print(f"🔄 Processing item {i+1}/{len(messages)}: {item}")
                processed_count += 1

                # Decode Unicode escape sequences if needed
                if "\\u003C" in item or "\\u003E" in item:
                    decoded_item = item.encode().decode("unicode-escape")
                # print(f"🔤 Decoded: {decoded_item}")
                else:
                    decoded_item = item

                # Parse email and name
                match = re.search(r"<([^<>]+)>", decoded_item)
                if match:
                    email = match.group(1).strip()
                    name_part = decoded_item.split("<")[0].strip()
                # print(f"📧 Found email in brackets: {email}, name: '{name_part}'")
                else:
                    email = decoded_item.strip()
                    name_part = ""
                # print(f"📧 Plain email format: {email}")

                # Validate email
                if not email or "@" not in email:
                    # print(f"❌ Invalid email format: {email}")
                    skipped_count += 1
                    continue

                # Check if email exists in database
                # print(f"🔍 Checking if email exists in database: {email}")
                cursor.execute(
                    "SELECT 1 FROM users_clients WHERE email_id = %s", (email,)
                )
                existing = cursor.fetchone()

                if existing:
                    # print(f"⏭️ Email already exists, skipping: {email}")
                    skipped_count += 1
                    continue

                # Process name
                if name_part:
                    name_part = name_part.strip('"').strip("'").strip()
                    name_tokens = name_part.split()
                    first_name = name_tokens[0] if name_tokens else ""
                    last_name = (
                        " ".join(name_tokens[1:]) if len(name_tokens) > 1 else ""
                    )
                # print(
                #     f"👤 Parsed name - First: '{first_name}', Last: '{last_name}'"
                # )
                else:
                    # Extract name from email
                    email_prefix = email.split("@")[0]
                    email_name = re.sub(r"[._-]", " ", email_prefix)
                    name_tokens = email_name.split()
                    first_name = name_tokens[0].title() if name_tokens else ""
                    last_name = (
                        " ".join(token.title() for token in name_tokens[1:])
                        if len(name_tokens) > 1
                        else ""
                    )
                # print(
                #     f"👤 Generated name from email - First: '{first_name}', Last: '{last_name}'"
                # )

                contact_data = {
                    "email": email,
                    "first_name": first_name,
                    "last_name": last_name,
                }
                results.append(contact_data)
            # print(f"✅ Added contact: {contact_data}")

            # Uncomment when ready to save
            # users_clients_id = add_synced_contact(user_id, cursor, email, first_name, last_name)

            except Exception as item_error:
                # print(f"❌ Error processing item '{item}': {item_error}")
                # print(f"📋 Traceback: {traceback.format_exc()}")
                skipped_count += 1
                continue

        # Close database connection
        # print("🔒 Closing database connection...")
        cursor.close()
        connection.close()

        # Prepare final response
        final_response = {
            "success": True,
            "message": f"Successfully processed {len(results)} new contacts",
            "results": results,
            "stats": {
                "total_processed": processed_count,
                "new_contacts": len(results),
                "skipped": skipped_count,
                "total_retrieved": len(messages),
            },
        }

        # print(f"🎉 Final response prepared: {final_response}")
        # print(
        #     f"📊 Stats - Total: {len(messages)}, New: {len(results)}, Skipped: {skipped_count}"
        # )

        response = jsonify(final_response)
        # print(f"📤 Returning JSON response with status 200")
        return response

    except Exception as e:
        error_msg = f"Unexpected error in sync_gmail_contacts: {str(e)}"
        # print(f"💥 {error_msg}")
        # print(f"📋 Full traceback: {traceback.format_exc()}")

        error_response = {
            "success": False,
            "error": error_msg,
            "results": [],
            "traceback": traceback.format_exc() if current_app.debug else None,
        }

        # print(f"📤 Returning error response: {error_response}")
        return jsonify(error_response), 500


def gmail_reply(
    user_id,
    to,
    subject,
    thread_id,
    body_text,
    in_reply_to,
    connection=None,
    attachments=None,
    cc=None,
    bcc=None,
):
    try:
        gmail_service = (
            GmailService(user_id, connection) if connection else GmailService(user_id)
        )
        user_email = gmail_service.user_email

        # Normalize recipient fields
        def normalize_address(val):
            if not val:
                return None
            if isinstance(val, str):
                return val.strip()
            if isinstance(val, dict):
                return val.get("email") or val.get("address")
            if isinstance(val, list):
                return [normalize_address(v) for v in val]
            raise ValueError(f"Invalid email field: {val}")

        to = normalize_address(to)
        cc = normalize_address(cc)
        bcc = normalize_address(bcc)

        # Validate
        if not to or not subject or not thread_id or not in_reply_to:
            missing = [
                k
                for k, v in {
                    "to": to,
                    "subject": subject,
                    "thread_id": thread_id,
                    "in_reply_to": in_reply_to,
                }.items()
                if not v
            ]
            return {"error": f"Missing required fields: {', '.join(missing)}"}

        sent = gmail_service.send_reply(
            receipent_emails=to,
            subject=subject,
            thread_id=thread_id,
            in_reply_to=in_reply_to,
            body_text=body_text,
            attachments=attachments,
            cc=cc,
            bcc=bcc,
        )

        if not isinstance(sent, dict):
            return {"error": "Unexpected Gmail response format"}

        message_api_id = sent.get("id")
        if not message_api_id:
            return None

        header_id = get_message_id(gmail_service.service, "me", message_api_id)
        message_id = header_id or message_api_id

        return f"{user_id}_{message_id}"

    except Exception as e:
        # print(f"❌ Gmail reply failed: {e}")
        return {"error": str(e)}


def get_message_id(service, user_id, gmail_id):
    msg = (
        service.users()
        .messages()
        .get(userId=user_id, id=gmail_id, format="metadata")
        .execute()
    )
    headers = msg.get("payload", {}).get("headers", [])
    return next(
        (h["value"] for h in headers if h["name"].lower() == "message-id"), None
    )


def send_mail(user_id, to, subject, body_text, attachments=None):
    message_id = None
    thread_id = None

    try:
        # print(f"User ID from session: {user_id}")
        gmail_service = GmailService(user_id)
        user_email = gmail_service.user_email

        # Log attachment info if provided
        # if attachments:
        # print(f"📎 [DEBUG] Sending email with {len(attachments)} attachment(s)")

        sent = gmail_service.send_email(
            receipent_emails=to,
            subject=subject,
            body_text=body_text,
            attachments=attachments,
        )

        if not sent or "id" not in sent:
            raise ValueError("No message ID returned from Gmail API")
        sendid = sent["id"]
        message_id = f"{user_id}_{sendid}"
        thread_id = sent.get("threadId") or sent.get("thread_id")
        return {"status": "success", "message_id": message_id, "thread_id": thread_id}

    except Exception as e:
        # print(f"[ERROR] → send_mail failed: {e}")
        return {"error": str(e), "status": "failed"}


def add_lead_contact(user_id, cursor, participant, participant_name):
    """
    Create a new Lead contact safely with deadlock retry.
    """
    try:
        # print("Creating new lead")
        communication_id = str(uuid.uuid4())
        users_clients_id = str(uuid.uuid4())

        dt_utc = datetime.now(timezone.utc)
        created_date = dt_utc.strftime("%Y-%m-%d %H:%M:%S")
        updated_date = dt_utc.isoformat()

        # Step 1: Insert into communication
        safe_execute(
            cursor,
            """
            INSERT INTO communication (communication_id, user_id_fk, users_clients_id_fk)
            VALUES (%s, %s, NULL)
            """,
            (communication_id, user_id),
        )

        # Step 2: Insert into users_clients
        safe_execute(
            cursor,
            """
            INSERT INTO users_clients (
                users_clients_id, communication_id_fk, first_name, last_name,
                phone_number, whatsapp_number, email_id, facebook_id, instagram_id,
                slack_id, slack_workspace, type, created_in, updated_in, snooze
            ) VALUES (%s, %s, %s, %s, NULL, NULL, %s, NULL, NULL, NULL, NULL, %s, %s, %s, %s)
            """,
            (
                users_clients_id,
                communication_id,
                participant_name,
                "",
                participant,
                "Lead",
                created_date,
                updated_date,
                False,
            ),
        )

        # Step 3: Link communication → user client
        safe_execute(
            cursor,
            """
            UPDATE communication
            SET users_clients_id_fk = %s
            WHERE communication_id = %s
            """,
            (users_clients_id, communication_id),
        )

        # Commit quickly to release locks
        cursor.connection.commit()
        return users_clients_id

    except Exception as e:
        cursor.connection.rollback()
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


def add_customer_contact(user_id, cursor, participant, participant_name):
    """
    Create a new Customer contact safely with deadlock retry.
    """
    try:
        # print("Creating new customer")
        communication_id = str(uuid.uuid4())
        users_clients_id = str(uuid.uuid4())

        dt_utc = datetime.now(timezone.utc)
        created_date = dt_utc.strftime("%Y-%m-%d %H:%M:%S")
        updated_date = dt_utc.isoformat()

        # Step 1: Insert into communication
        safe_execute(
            cursor,
            """
            INSERT INTO communication (communication_id, user_id_fk, users_clients_id_fk)
            VALUES (%s, %s, NULL)
            """,
            (communication_id, user_id),
        )

        # Step 2: Insert into users_clients
        safe_execute(
            cursor,
            """
            INSERT INTO users_clients (
                users_clients_id, communication_id_fk, first_name, last_name,
                phone_number, whatsapp_number, email_id, facebook_id, instagram_id,
                slack_id, slack_workspace, type, created_in, updated_in, snooze
            ) VALUES (%s, %s, %s, %s, NULL, NULL, %s, NULL, NULL, NULL, NULL, %s, %s, %s, %s)
            """,
            (
                users_clients_id,
                communication_id,
                participant_name,
                "",
                participant,
                "Customer",
                created_date,
                updated_date,
                False,
            ),
        )

        # Step 3: Link communication → user client
        safe_execute(
            cursor,
            """
            UPDATE communication
            SET users_clients_id_fk = %s
            WHERE communication_id = %s
            """,
            (users_clients_id, communication_id),
        )

        # Commit quickly to release locks
        cursor.connection.commit()
        return users_clients_id

    except Exception as e:
        cursor.connection.rollback()
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


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
        # print("userID", user_id)
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


@gmail_bp.route("/gmail/forward", methods=["POST"])
def forward_email():
    """
    Forward an email to one or more recipients with CC/BCC support

    Request body:
    {
        "to": "recipient@example.com" or ["email1@example.com", "email2@example.com"],
        "cc": "cc@example.com" (optional, string or array),
        "bcc": "bcc@example.com" (optional, string or array),
        "subject": "Original Subject",
        "body": "Email body (HTML or plain text) with original message",
        "message_id": "gmail_message_id" (optional, for reference)
    }

    Returns:
    {
        "status": "success",
        "message": "Email forwarded successfully",
        "result": {...}
    }
    """
    try:
        user_id = session.get("user_id")
        if not user_id:
            return jsonify({"error": "User not authenticated"}), 401

        gmail_service = GmailService(user_id)
        data = request.get_json()

        to = data.get("to")
        cc = data.get("cc")
        bcc = data.get("bcc")
        subject = data.get("subject")
        body = data.get("body")
        attachments = data.get("attachments", [])

        # Validate required fields
        if not all([to, subject, body]):
            return (
                jsonify(
                    {
                        "error": "Missing required fields",
                        "required": ["to", "subject", "body"],
                    }
                ),
                400,
            )

        # Handle multiple recipients (convert to comma-separated string if list)
        if isinstance(to, list):
            to = ", ".join(to)
        if isinstance(cc, list):
            cc = ", ".join(cc) if cc else None
        if isinstance(bcc, list):
            bcc = ", ".join(bcc) if bcc else None

        # print(f"📧 Forwarding email to: {to}")
        # if cc:
        #     print(f"📧 CC: {cc}")
        # if bcc:
        #     print(f"📧 BCC: {bcc}")
        # print(f"📝 Subject: {subject}")
        # if attachments:
        #     print(f"📎 Attachments: {len(attachments)}")

        # Send forward using the service method with CC/BCC support and attachments
        result = gmail_service.send_forward(
            to, subject, body, cc=cc, bcc=bcc, attachments=attachments or []
        )

        if result:
            # print(f"✅ Email forwarded successfully")
            return (
                jsonify(
                    {
                        "status": "success",
                        "message": "Email forwarded successfully",
                        "result": result,
                    }
                ),
                200,
            )
        else:
            return jsonify({"status": "error", "error": "Failed to forward email"}), 500

    except Exception as e:
        # print(f"❌ Error forwarding email: {e}")
        # print(f"📋 Traceback: {traceback.format_exc()}")
        return (
            jsonify(
                {
                    "status": "error",
                    "error": str(e),
                    "message": "Failed to forward email",
                }
            ),
            500,
        )


@gmail_bp.route("/gmail/inbox_info/<userid>", methods=["GET"])
def get_inbox_info(userid):
    try:
        max_emails = 1000
        base_days = int(request.args.get("days", 1))  # default 30 days
        gmail_service = GmailService(userid)

        total_messages = 0
        final_days_used = base_days

        # print(base_days)

        total_messages = gmail_service.get_inbox_stats(base_days)  # returns int
        # while True:
        #     total_messages = inbox_count
        #     final_days_used = base_days

        #     if total_messages >= max_emails:
        #         # if total_messages > (max_emails + 30):
        #         #     base_days -= 2
        #         # else:
        #         #     break
        #         break
        #     else:
        #         base_days += 10  # widen search window

        return (
            jsonify(
                {
                    "result": {
                        "total_messages": total_messages,
                        "days_covered": final_days_used,
                    }
                }
            ),
            200,
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


from datetime import datetime, timezone
from dateutil.relativedelta import relativedelta


@gmail_bp.route("/gmail/datewise/<userid>", methods=["GET"])
def get_datewise_info(userid):
    try:
        Enddate_str = request.args.get(
            "end_date", datetime.now(timezone.utc).strftime("%Y-%m-%d")
        )
        Enddate = datetime.fromisoformat(Enddate_str)

        startDate_str = request.args.get(
            "start_date", (Enddate - relativedelta(months=3)).strftime("%Y-%m-%d")
        )

        gmail_service = GmailService(userid)

        inbox_count = asyncio.run(
            gmail_service.get_inbox_date_wise_stats_dynamic(
                start_date=startDate_str, end_date=Enddate_str
            )
        )

        return jsonify({"result": inbox_count}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


from threading import Thread


@gmail_bp.route("/deletedb/<user_id>", methods=["GET"])
def delete_user_ticket_data(user_id):
    try:
        connection = connect_to_rds()
        with connection.cursor() as cursor:
            # 1. Get ticket IDs assigned to the user
            cursor.execute(
                "SELECT ticket_id_fk FROM assigned WHERE user_id_fk = %s",
                (user_id,),
            )
            ticket_ids = [row[0] for row in cursor.fetchall() if row[0]]

            # 2. Get conversation IDs from those tickets
            conversation_ids = []
            if ticket_ids:
                format_strings = ",".join(["%s"] * len(ticket_ids))
                cursor.execute(
                    f"SELECT conversation_id_fk FROM tickets WHERE tickets_id IN ({format_strings})",
                    tuple(ticket_ids),
                )
                conversation_ids = [row[0] for row in cursor.fetchall() if row[0]]

            # 3. Delete messages based on ticket conversation IDs
            if conversation_ids:
                format_strings = ",".join(["%s"] * len(conversation_ids))
                cursor.execute(
                    f"DELETE FROM messages WHERE conversation_id_fk IN ({format_strings})",
                    tuple(conversation_ids),
                )

            # 4. Delete assigned
            if ticket_ids:
                format_strings = ",".join(["%s"] * len(ticket_ids))
                cursor.execute(
                    f"DELETE FROM assigned WHERE ticket_id_fk IN ({format_strings})",
                    tuple(ticket_ids),
                )

            # 5. Delete tickets
            if ticket_ids:
                format_strings = ",".join(["%s"] * len(ticket_ids))
                cursor.execute(
                    f"DELETE FROM tickets WHERE tickets_id IN ({format_strings})",
                    tuple(ticket_ids),
                )

            # 6. Delete threads (based on conversation IDs)
            if conversation_ids:
                format_strings = ",".join(["%s"] * len(conversation_ids))
                cursor.execute(
                    f"DELETE FROM threads WHERE conversation_id IN ({format_strings})",
                    tuple(conversation_ids),
                )

            # 7. Delete communication & users_clients (based on user_id)
            cursor.execute(
                "SELECT communication_id FROM communication WHERE user_id_fk = %s",
                (user_id,),
            )
            comm_ids = [row[0] for row in cursor.fetchall() if row[0]]

            if comm_ids:
                format_strings = ",".join(["%s"] * len(comm_ids))
                cursor.execute(
                    f"DELETE FROM users_clients WHERE communication_id_fk IN ({format_strings})",
                    tuple(comm_ids),
                )
                cursor.execute(
                    f"DELETE FROM communication WHERE communication_id IN ({format_strings})",
                    tuple(comm_ids),
                )

            # 8. Cleanup orphaned threads/messages (not tied to tickets)
            cursor.execute(
                "SELECT conversation_id FROM threads WHERE external_user_id = %s",
                (user_id,),
            )
            orphan_convs = [row[0] for row in cursor.fetchall()]
            if orphan_convs:
                format_strings = ",".join(["%s"] * len(orphan_convs))
                cursor.execute(
                    f"DELETE FROM messages WHERE conversation_id_fk IN ({format_strings})",
                    tuple(orphan_convs),
                )
                cursor.execute(
                    f"DELETE FROM threads WHERE conversation_id IN ({format_strings})",
                    tuple(orphan_convs),
                )

            # 9. Null umail_json on users
            cursor.execute(
                "UPDATE users SET umail_json = NULL WHERE user_id = %s",
                (user_id,),
            )

            # 10. Commit all changes once
            connection.commit()

        # Outside the cursor context: delete S3 folder + update ticket allocator
        folder_path = f"{user_id}/messages"
        Thread(target=delete_folder_from_s3, args=(folder_path,)).start()
        client_ticket = TicketAllocator(user_id)
        client_ticket.update_ticket(value=0)
        ls = LanceDBServer()
        asyncio.run(ls.delete_user_table(user_id=user_id))

        # single return at the end
        return {
            "status": "success",
            "message": "User-related ticket, communication, users_clients and orphaned threads/messages data deleted successfully",
        }

    except Exception as e:
        if connection:
            connection.rollback()
        return {"status": "failed", "error": str(e)}
    finally:
        if connection:
            connection.close()


@gmail_bp.route("/delete_user_cache/<primary_user_id>", methods=["GET"])
def delete_user_cache(primary_user_id):

    if not primary_user_id:
        return False

    connection = connect_to_rds()
    cursor = connection.cursor(pymysql.cursors.DictCursor)

    try:
        # fetch integration user_ids
        query = """
            SELECT user_id
            FROM integrations
            WHERE primary_user_id_fk = %s
              AND status = 'active'
        """
        cursor.execute(query, (primary_user_id,))
        rows = cursor.fetchall()

        # collect all user_ids (primary + integrations)
        user_ids = {primary_user_id}
        if rows:
            user_ids.update(row["user_id"] for row in rows)

        async def _inner():
            results = []
            for uid in user_ids:
                # print(f"deleting cache for user_id: {uid}")
                results.append(await _delete_cache_async(uid))
            return results

        # run async safely
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(_inner())
        else:
            return loop.run_until_complete(_inner())

    except Exception as e:
        # print(f"Cache delete error: {e}")
        return False

    finally:
        cursor.close()
        connection.close()


async def _delete_cache_async(user_id):
    from services.redis_service import RedisService

    client = RedisService()
    return await client.delete(f"umail_{user_id}")


def delete_all_user_data(user_id):
    """
    Delete all data related to a user across all tables.
    - If the user is invited (not admin/owner), clean up owner permissions.
    - If the user is an owner/admin who invited others, remove their references from invited users.
    """
    connection = None
    try:
        connection = connect_to_rds()

        with connection.cursor(pymysql.cursors.DictCursor) as cursor:
            # Check the user type first
            cursor.execute(
                "SELECT permissions, user_type, email FROM users WHERE user_id = %s",
                (user_id,),
            )
            user_row = cursor.fetchone()
            if not user_row:
                return {"status": "failed", "error": "User not found"}

            user_type = user_row["user_type"]
            user_email = user_row["email"]
            user_permissions = (
                json.loads(user_row["permissions"])
                if user_row.get("permissions")
                else {}
            )

        with connection.cursor() as cursor:
            # 1. Delete tickets/messages/threads/communication/users_clients
            ticket_cleanup = delete_user_ticket_data(user_id)

            # 2. Delete from session
            cursor.execute("DELETE FROM session WHERE user_id_fk = %s", (user_id,))
            # print("DONE 1")

            # 3. Delete business_info
            cursor.execute(
                "DELETE FROM business_info WHERE user_id_fk = %s", (user_id,)
            )
            # print("DONE 2")

            # 4. Delete launch entries (get launch_id for subagents cleanup)
            cursor.execute(
                "SELECT launch_id FROM launch WHERE user_id_fk = %s", (user_id,)
            )
            launch_ids = [row[0] for row in cursor.fetchall() if row[0]]
            # print("DONE 3")

            # 5. Delete subagents and linked playbooks/instructions/integrations/connect
            if launch_ids:
                fmt = ",".join(["%s"] * len(launch_ids))
                cursor.execute(
                    f"SELECT sub_agent_id FROM subagents WHERE launch_id_fk IN ({fmt})",
                    tuple(launch_ids),
                )
                subagent_ids = [row[0] for row in cursor.fetchall() if row[0]]
                # print("DONE 4")
                if subagent_ids:
                    fmt = ",".join(["%s"] * len(subagent_ids))
                    cursor.execute(
                        f"DELETE FROM playbook WHERE sub_agent_id IN ({fmt})",
                        tuple(subagent_ids),
                    )
                    # print("DONE 5")
                    cursor.execute(
                        f"DELETE FROM instructions WHERE sub_agent_id_fk IN ({fmt})",
                        tuple(subagent_ids),
                    )
                    # print("DONE 6")
                    cursor.execute(
                        f"DELETE FROM integrations WHERE sub_agent_id_fk IN ({fmt})",
                        tuple(subagent_ids),
                    )
                    # print("DONE 7")
                    cursor.execute(
                        f"DELETE FROM connect WHERE sub_agent_id_fk IN ({fmt})",
                        tuple(subagent_ids),
                    )
                    # print("DONE 8")
                    cursor.execute(
                        f"DELETE FROM subagents WHERE sub_agent_id IN ({fmt})",
                        tuple(subagent_ids),
                    )
                # print("DONE 9")

            # 6. Delete launch rows
            if launch_ids:
                fmt = ",".join(["%s"] * len(launch_ids))
                cursor.execute(
                    f"DELETE FROM launch WHERE launch_id IN ({fmt})", tuple(launch_ids)
                )
            # print("DONE 10")

            # === Handle invited-user/owner relationships ===
            with connection.cursor(pymysql.cursors.DictCursor) as cursor2:
                if user_type == "user":
                    # invited user: remove from owner's permissions
                    invited_by_email = user_permissions.get("invited_by")
                    if invited_by_email:
                        cursor2.execute(
                            "SELECT user_id, permissions FROM users WHERE email = %s",
                            (invited_by_email,),
                        )
                        owner_row = cursor2.fetchone()
                        if owner_row:
                            owner_perms = (
                                json.loads(owner_row["permissions"])
                                if owner_row["permissions"]
                                else {}
                            )

                            # Remove from shared
                            owner_perms["shared"] = [
                                s
                                for s in owner_perms.get("shared", [])
                                if s.get("email") != user_email
                            ]
                            # Remove from invites if present
                            owner_perms["invites"] = [
                                i
                                for i in owner_perms.get("invites", [])
                                if i.get("email") != user_email
                            ]

                            # Update owner's permissions JSON
                            cursor2.execute(
                                "UPDATE users SET permissions=%s WHERE user_id=%s",
                                (json.dumps(owner_perms), owner_row["user_id"]),
                            )
                        # print("Owner permissions updated for invited user removal")

            # 7. Delete the user itself
            cursor.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
            # print("DONE 12")

            connection.commit()

        # 8. Delete any S3 folders
        Thread(target=delete_folder_from_s3, args=(f"{user_id}/",)).start()

        return {
            "status": "success",
            "message": f"All data for user {user_id} deleted successfully",
            "ticket_cleanup": ticket_cleanup,
        }

    except Exception as e:
        if connection:
            connection.rollback()
        return {"status": "failed", "error": str(e)}

    finally:
        if connection:
            connection.close()


@gmail_bp.route("/delete_user/<user_id>", methods=["DELETE"])
def delete_user(user_id):
    result = delete_all_user_data(user_id)
    return jsonify(result)


@gmail_bp.route("/gmail/start_watch/<userid>", methods=["GET"])
def start_gmail_watch(userid):
    serv = GmailService(user_id=userid)
    res = serv.create_watch_req()
    return jsonify(res)


@gmail_bp.route("/start_gmail_watches", methods=["GET"])
def start_gmail_watches():
    conn = connect_to_rds()
    try:
        with conn.cursor() as cursor:
            # join your social table if needed
            cursor.execute(
                """
               select user_id,social from users
            """
            )
            rows = cursor.fetchall()
    finally:
        conn.close()
    results = []
    for row in rows:
        user_id = row[0]
        service = GmailService(user_id)
        res = service.create_watch_req(user_id)
        results.append({"user_id": user_id, "result": res})


#     return jsonify(results)
@gmail_bp.route("/gmail/history_check/<userid>/<hisid>", methods=["GET"])
def histcheckmail(userid, hisid):
    serv = GmailService(user_id=userid)
    res = serv.check_hisdata(hisid)
    return jsonify(res)


# ============ SECURE ATTACHMENT DOWNLOAD ENDPOINT ============


@gmail_bp.route("/gmail/attachment/download", methods=["POST"])
def download_attachment():
    """
    Direct attachment download endpoint

    """
    try:
        user_id = session.get("user_id")
        if not user_id:
            # print("❌ No user_id in session")
            return jsonify({"error": "Unauthorized: No active session"}), 401

        data = request.get_json() or {}
        s3_key = data.get("s3_key", "").strip()
        filename = data.get("filename", "").strip()
        message_id = data.get("message_id", "").strip()
        thread_id = data.get("thread_id", "").strip()

        # Verify all fields present
        if not all([s3_key, filename, message_id, thread_id]):
            return (
                jsonify(
                    {
                        "error": "Missing required fields: s3_key, filename, message_id, thread_id"
                    }
                ),
                400,
            )

        # Verify user owns the S3 path
        if not s3_key.startswith(f"{user_id}/"):
            # print(f"❌ User {user_id} attempting to download: {s3_key}")
            return jsonify({"error": "Forbidden: You do not own this attachment"}), 403

        # Verify S3 file exists
        from utils.s3_utils import s3bucket

        s3 = s3bucket()
        bucket_name = os.getenv("S3_BUCKET")

        try:
            # print(f"☁️ Checking S3 file: {s3_key}")
            s3.head_object(Bucket=bucket_name, Key=s3_key)
        # print(f"✅ S3 file verified")

        except s3.exceptions.NoSuchKey:
            # print(f"❌ File not found in S3: {s3_key}")
            return jsonify({"error": "File not found"}), 404

        except Exception as e:
            # print(f"❌ S3 error: {e}")
            return jsonify({"error": f"S3 access error: {str(e)}"}), 500

        # Generate presigned URL
        try:
            # print(f"🔐 Generating presigned URL for {filename}")
            presigned_url = s3.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": bucket_name,
                    "Key": s3_key,
                    "ResponseContentDisposition": f'attachment; filename="{filename}"',
                },
                ExpiresIn=3600,  # URL valid for 1 hour
            )

            # print(f"✅ Presigned URL generated, valid for 1 hour")
            # print(f"📋 AUDIT: User {user_id} downloading {filename} from {s3_key}")

            return (
                jsonify(
                    {
                        "status": "success",
                        "download_url": presigned_url,
                        "filename": filename,
                        "expires_in_seconds": 3600,
                        "message": "Download ready",
                    }
                ),
                200,
            )

        except Exception as e:
            # print(f"❌ Presigned URL generation failed: {e}")
            return (
                jsonify({"error": f"Failed to generate download link: {str(e)}"}),
                500,
            )

    except Exception as e:
        # print(f"💥 Unexpected error in download_attachment: {e}")
        # print(f"📋 Traceback: {traceback.format_exc()}")
        return jsonify({"error": f"Internal server error: {str(e)}"}), 500


@gmail_bp.route("/gmail/download_attachment", methods=["POST"])
def download_attachment_on_demand():
    """
    ⚠️ NEW ENDPOINT: On-demand attachment download

    Purpose:
    - User clicks "Download" button on attachment in frontend
    - Frontend sends attachment_id + message_id to this endpoint
    - Backend retrieves file from Gmail API
    - Uploads to S3 temporarily
    - Returns CloudFront URL for direct download

    Request body:
    {
        "user_id": "user123",
        "message_id": "gmail_message_id",
        "attachment_id": "gmail_attachment_id",
        "filename": "document.pdf"
    }

    Returns:
    {
        "status": "success",
        "filename": "document.pdf",
        "download_url": "https://cloudfront.../file.pdf",
        "s3_key": "user123/messages/attachments/..."
    }
    """
    try:
        data = request.get_json()
        user_id = data.get("user_id")
        message_id = data.get("message_id")
        attachment_id = data.get("attachment_id")
        filename = data.get("filename", "attachment")

        if not all([user_id, message_id, attachment_id]):
            return (
                jsonify(
                    {
                        "status": "error",
                        "error": "Missing required fields: user_id, message_id, attachment_id",
                    }
                ),
                400,
            )

        # print(f"\n📥 [DOWNLOAD] Attachment download requested:")
        # print(f"   User: {user_id}")
        # print(f"   Message: {message_id}")
        # print(f"   Attachment: {attachment_id}")
        # print(f"   Filename: {filename}")

        # Initialize Gmail service
        gmail_service = GmailService(user_id)

        # Retrieve attachment from Gmail API
        # print(f"☁️ [DOWNLOAD] Fetching from Gmail API...")
        attachment_data = (
            gmail_service.service.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=message_id, id=attachment_id)
            .execute()
        )

        # Decode the attachment data
        import base64
        import tempfile
        import os

        file_data = base64.urlsafe_b64decode(attachment_data.get("data", b""))

        if not file_data:
            return jsonify({"status": "error", "error": "Attachment has no data"}), 400

        # print(f"✅ [DOWNLOAD] Retrieved {len(file_data)} bytes from Gmail API")

        # Create temporary file
        with tempfile.NamedTemporaryFile(
            delete=False, suffix=os.path.splitext(filename)[1]
        ) as tmp_file:
            tmp_file.write(file_data)
            tmp_path = tmp_file.name

        # Upload to S3
        from utils.s3_utils import upload_any_file, attach_CLDFRNT_url

        filename_safe = filename.replace("/", "_").replace("\\", "_")
        s3_key = (
            f"{user_id}/messages/attachments/downloads/{message_id}/{filename_safe}"
        )

        # print(f"☁️ [DOWNLOAD] Uploading to S3: {s3_key}")
        result = upload_any_file(tmp_path, user_id, type="messages", s3_key_C=s3_key)

        # Clean up temp file
        # try:
        #     os.remove(tmp_path)
        # except Exception as e:
        # #print(f"⚠️ Could not delete temp file: {e}")
        os.remove(tmp_path)

        # Check upload result
        if result.get("status") != "success":
            return (
                jsonify(
                    {
                        "status": "error",
                        "error": result.get("message", "S3 upload failed"),
                    }
                ),
                500,
            )

        # Generate CloudFront URL
        download_url = attach_CLDFRNT_url(s3_key)

        # print(f"✅ [DOWNLOAD] Success! URL: {download_url}")

        return (
            jsonify(
                {
                    "status": "success",
                    "filename": filename,
                    "download_url": download_url,
                    "s3_key": s3_key,
                    "size": len(file_data),
                }
            ),
            200,
        )

    except Exception as e:
        # print(f"❌ [DOWNLOAD] Error: {e}")
        # print(f"📋 [DOWNLOAD] Traceback: {traceback.format_exc()}")
        return (
            jsonify(
                {
                    "status": "error",
                    "error": str(e),
                    "message": "Failed to download attachment",
                }
            ),
            500,
        )
