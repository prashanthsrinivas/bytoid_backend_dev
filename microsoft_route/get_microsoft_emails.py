from db.rds_db import connect_to_rds
from cust_helpers import pathconfig
from utils.normal import ensure_dir
from email.utils import parseaddr
from umail_helper.helper import get_users_client_id, update_user_message_cache, set_user_sync_time, get_last_sync_time
from gmail_route.routes import add_lead_contact, add_customer_contact, safe_json_load
from utils.s3_utils import (
    read_json_from_s3,
    upload_any_file,
)
from datetime import datetime, timezone, timedelta, time
from flask import jsonify
import asyncio
from umail_helper.ticketalloc import TicketAllocator
import os
from .outlook_class import OutlookService
import shutil
from db.db_checkers import update_umail_json, update_umail_json_integration
from services.redis_service import RedisService
import json, threading

async def get_outlook_thread_count_dynamic(
    user_id, start_date, end_date, min_days=7, integration=None
):
    """
    Fetch Outlook conversation IDs dynamically: if large ranges fail, split into smaller chunks.
    Only fetches conversation IDs, not full messages.
    Returns dict: {"count": total_count, "conversations": all_conversations_list}
    """
    import random

    async def fetch_chunk(s_date, e_date):
        """
        Fetch a single chunk of Outlook conversations within s_date → e_date
        """
        outlook_service = OutlookService(user_id)
        while getattr(fetch_chunk, "service_running", False):
            await asyncio.sleep(0.5)
        fetch_chunk.service_running = True

        try:
            # Handle 'Z' in ISO string for Python
            s_dt = parse_iso_utc(s_date)
            e_dt = parse_iso_utc(e_date)

            # Ensure UTC ISO format with 'Z' for Graph API
            s_dt_str = s_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            e_dt_str = e_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

            conv_ids = []
            page_token = None
            while True:
                params = {
                    "top": 500,
                    "$select": "id,conversationId,receivedDateTime",
                    "$orderby": "receivedDateTime desc",
                    "$filter": f"receivedDateTime ge {s_dt_str} and receivedDateTime le {e_dt_str}",
                }
                if page_token:
                    params["$skiptoken"] = page_token

                try:
                    response = await outlook_service.list_messages(
                        params, integration=integration
                    )
                    messages = response.get("value", [])
                    for msg in messages:
                        conv_id = msg.get("conversationId")
                        if conv_id and conv_id not in conv_ids:
                            conv_ids.append(conv_id)

                    page_token = response.get("@odata.nextLink")
                    if not page_token:
                        break
                except Exception as e:
                    if hasattr(e, "resp") and e.resp.status == 429:
                        await asyncio.sleep(1 + random.random())
                        continue
                    else:
                        raise

            return {"count": len(conv_ids), "conversations": conv_ids}

        finally:
            fetch_chunk.service_running = False

    all_conversations = []
    total_count = 0
    stack = [(start_date, end_date)]

    while stack:
        s_date, e_date = stack.pop(0)
        try:
            result = await fetch_chunk(s_date, e_date)
            total_count += result["count"]
            all_conversations.extend(result["conversations"])
        except Exception as e:
            print(f"⚠️ Chunk {s_date} → {e_date} failed: {e}")
            # Split if more than min_days
            s_dt = parse_iso_utc(s_date)
            e_dt = parse_iso_utc(e_date)
            delta_days = (e_dt - s_dt).days
            if delta_days > min_days:
                mid_dt = s_dt + timedelta(days=delta_days // 2)
                stack.insert(0, (mid_dt.strftime("%Y-%m-%d"), e_date))
                stack.insert(0, (s_date, mid_dt.strftime("%Y-%m-%d")))
            else:
                print(f"❌ Skipping unresponsive chunk {s_date} → {e_date}")

    print(f"all_conversations : {all_conversations}")

    return {
        "conversationsTotal": {
            "count": total_count,
            "conversations": all_conversations,
        },
        "start_date": start_date,
        "end_date": end_date,
    }


async def get_outlook_conversation_ids_dynamic(
    user_id, start_date=None, end_date=None, integration=None
):
    """
    Fetch conversation IDs from Outlook dynamically.
    - Optionally use start_date and end_date to limit range.
    Returns dict: {"conversation_ids": [list_of_ids]}
    """
    print(f"inside get_outlook_conversation_ids_dynamic")

    params = {}
    if start_date:
        params["startDateTime"] = start_date  # or proper ISO string
    if end_date:
        params["endDateTime"] = end_date

    outlook_service = OutlookService(user_id)
    messages_response = await outlook_service.list_messages(
        params, integration=integration
    )
    messages = messages_response.get("value", [])  # Graph API returns 'value' list

    conv_ids = list(
        {msg.get("conversationId") for msg in messages if msg.get("conversationId")}
    )
    return {"conversation_ids": conv_ids}


async def v2fetch_outlook_messages_batch(
    user_id, conv_batch, my_email, batch_count, connection, integration=None, primary_user_id = None
):
    """
    Fetch a single batch of Outlook messages dynamically using conversation IDs.
    - user_id: external user id
    - my_email: user's primary email (for direction/conversation assignment)
    - batch_count: logging/debug counter
    - connection: optional DB connection; if None, function will create & close one
    Returns:
      - status
      - new_messages
      - next_page_token (always None)
      - grouped_messages
    """
    from collections import defaultdict
    import os
    import json
    import uuid
    from datetime import datetime, timezone
    from email.utils import parsedate_to_datetime

    new_connection = None
    try:
        # Ensure DB connection
        if connection is None:
            new_connection = connect_to_rds()
            connection = new_connection

        cursor = connection.cursor()
        print(f"🚀 Starting Outlook batch {batch_count} fetch for user {user_id}")

        print(f"integration : {integration}")
        print(f"primary_user_id : {primary_user_id}")

        # --- STEP 1: fetch conversation IDs dynamically ---
        # conv_result = await get_outlook_conversation_ids_dynamic(user_id, integration =integration)
        # conv_ids = conv_result.get("conversation_ids", [])

        conv_ids = conv_batch

        print(f"conv_ids: {conv_ids}")
        if not conv_ids:
            print("⚠️ No conversations found for user")
            return {
                "status": "success",
                "new_messages": 0,
                "next_page_token": None,
                "grouped_messages": {},
            }

        outlook_service = OutlookService(user_id)

        print(
            f"\n📧 [OUTLOOK SYNC] Calling process_conversations_batch with {len(conv_ids)} conversations"
        )
        results = await outlook_service.process_conversations_batch(
            conv_ids, my_email, batch_count, integration = integration
        )
        if not results:
            return {
                "status": "success",
                "new_messages": 0,
                "next_page_token": None,
                "grouped_messages": {},
            }

        # print(f"results: {results}")
        count_new = 0
        grouped_messages = defaultdict(list)

        # File setup (same pattern as Gmail)
        timestamp = datetime.now(timezone.utc)
        date_str = timestamp.strftime("%Y-%m-%d")
        filename = f"{date_str}.json"

        user_folder = os.path.join(pathconfig.basepath, "messages", user_id)
        ensure_dir(user_folder)
        filepath = os.path.join(user_folder, filename)

        # Load existing data for dedup / reprocess checks
        existing_ids_local = set()
        input_data_local = {}
        if os.path.exists(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    existing_data_local = json.load(f)
                    input_data_local = existing_data_local.get("input_data", {})
                # Extract existing message IDs
                for client_data in input_data_local.values():
                    if isinstance(client_data, dict):
                        for client_channels in client_data.values():
                            if isinstance(client_channels, dict):
                                for channel_msgs in client_channels.values():
                                    if isinstance(channel_msgs, list):
                                        for msg in channel_msgs:
                                            if isinstance(msg, dict) and "id" in msg:
                                                existing_ids_local.add(msg["id"])
            except Exception as e:
                print(f"⚠️ Error loading existing data: {e}")

        print(f"************* before db pre checks")

        # DB pre-checks
        email_to_client_id = {}
        configs_created = set()
        first_time_user = True

        # if integration:
        #     cursor.execute(
        #         "SELECT m.message_id, th.conversation_id FROM messages m JOIN threads th ON m.conversation_id_fk = th.conversation_id WHERE th.external_user_id = %s",
        #         (primary_user_id,),
        #     )
        # else:
        cursor.execute(
                "SELECT m.message_id, th.conversation_id FROM messages m JOIN threads th ON m.conversation_id_fk = th.conversation_id WHERE th.external_user_id = %s",
                (user_id,),
            )

        rows = cursor.fetchall()
        if rows:
            first_time_user = False

        # --- STEP 2: process each conversation ---
        for conv_id in conv_ids:
            conv_res = results.get(conv_id)
            if not conv_res:
                print(f"⚠️ No response for conversation {conv_id}")
                continue

            conv_messages, err = conv_res
            if err or not conv_messages:
                if err:
                    print(f"⚠️ Conversation {conv_id} error: {err}")
                continue

            thread_id = conv_id


            for msg in conv_messages:
                message_id = msg.get("messageId") or msg.get("id") or str(uuid.uuid4())
                # if integration:
                #     row_id = f"{primary_user_id}_{message_id}"
                # else:
                row_id = f"{user_id}_{message_id}"

                # Skip existing in DB check (still allow reprocessing if old)

                cursor.execute(
                    """
                        SELECT m.conversation_id_fk, m.sender_id, t.external_user_id
                        FROM messages m
                        JOIN threads t ON m.conversation_id_fk = t.conversation_id
                        WHERE m.message_id = %s
                        """,
                    (row_id,),
                )

                row = cursor.fetchone()
                if row and row[2] == (user_id):
                    print(f"message {message_id} already exists in DB for user")

                # Check local cache for recent messages to skip
                if row_id in existing_ids_local:
                    try:
                        msg_time = parsedate_to_datetime(msg.get("date"))
                        if (
                            datetime.now(timezone.utc) - msg_time
                        ).total_seconds() < 3600:
                            print(f"skipping recent message {message_id}")
                            continue
                        existing_ids_local.discard(row_id)
                    except Exception:
                        pass

                # Normalize fields
                try:
                    dt = parsedate_to_datetime(msg.get("date"))
                except Exception:
                    dt = datetime.now(timezone.utc)
                timestamp_iso = dt.isoformat()
                direction = msg.get("direction", "inbound")
                body_content = msg.get("body", "")
                plain_text = msg.get("plain_text", "")
                subject = msg.get("subject", "")

                from_email = msg.get("from", "")
                from_name = msg.get("from_name", "")
                to_emails = msg.get("toRecipients", [])
                to_names = msg.get("to_names", [])

                conversation_id = f"{user_id}_{thread_id}"


                participant = from_email if direction == "inbound" else (to_emails[0] if to_emails else None)
                participant_name = from_name if direction == "inbound" else (to_names[0] if to_names else "")

                # Get or create client
                if participant in email_to_client_id:
                    client_id, client_type = email_to_client_id[participant]
                else:
                    result = get_users_client_id(
                        participant,
                        user_id,
                        cursor,
                    )
                    if isinstance(result, tuple) and len(result) == 2:
                        client_id, client_type = result
                    else:
                        client_id = result if result else None
                        client_type = None

                    if not client_id:
                        if not first_time_user:
                            client_id = add_lead_contact(
                                user_id,
                                cursor,
                                participant,
                                participant_name,
                            )

                            client_type = "Lead"
                        else:
                            client_id = add_customer_contact(
                                user_id,
                                cursor,
                                participant,
                                participant_name,
                            )

                            client_type = "Customer"
                    email_to_client_id[participant] = (client_id, client_type)

                # Build message dict
                outlook_attachments = msg.get("attachments", [])
                message = {
                    "id": row_id,
                    "from": from_email,
                    "to": to_emails[0],
                    "cc": msg.get("cc", ""),
                    "bcc": msg.get("bcc", ""),
                    "body": body_content,
                    "plain_text": plain_text,
                    "subject": subject,
                    "timestamp": timestamp_iso,
                    "source": "outlook",
                    "direction": direction,
                    "user_id": user_id,
                    "thread_id": thread_id,
                    "conversation_id": conversation_id,
                    "type": client_type,
                    "attachments": outlook_attachments,
                }

                print("----------------------------")
                print(f"in vefetch_outlook_messages_batch : {conversation_id}")
                print("----------------------------")

                grouped_messages.setdefault(client_id, {}).setdefault(
                    "outlook", []
                ).append(message)
                count_new += 1

                # Create per-client config files if needed
                if client_id not in configs_created:
                    config_folder = os.path.join(
                        pathconfig.basepath,
                        "messages",
                        user_id,
                        client_id,
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
                        print(f"s3_config_key : {s3_config_key}")

                        s3_data = read_json_from_s3(s3_config_key)
                        if s3_data is None:
                            upload_any_file(
                                config_filepath,
                                user_id,
                                type="messages",
                                s3_key_C=s3_config_key,
                            )

                    configs_created.add(client_id)

        # Merge with existing data and save
        existing_data = safe_json_load(filepath)
        print(f"filepath : {filepath}")
        merged_messages = existing_data.get("input_data", {})
        for client_id, channels in grouped_messages.items():
            for channel, messages in channels.items():
                merged_messages.setdefault(client_id, {}).setdefault(
                    channel, []
                ).extend(messages)

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(
                {"filename": filename, "input_data": merged_messages}, f, indent=2
            )

        print(f"✅ Outlook batch complete: {count_new} new messages processed")
        return {
            "status": "success",
            "new_messages": count_new,
            "next_page_token": None,
            "grouped_messages": dict(grouped_messages),
        }

    except Exception as e:
        print(f"[ERROR] → v2fetch_outlook_messages_batch failed: {e}")
        return {
            "error": str(e),
            "status": "failed",
            "next_page_token": None,
            "grouped_messages": {},
        }
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        if new_connection:
            new_connection.close()


# @microsoft_bp.route("/microsoft/v2all_continuous_outlook", methods=["POST"])
async def v2all_continuous_outlook(user_id, integration=None, min_days=2):
    """
    Run Outlook fetch + processing in parallel batches.
    Each batch also runs heavy embedding processes in parallel.
    """
    print(f"inside v2all_continuous_outlook with user id : {user_id} ")
    import time
    from umail_helper.asyn_functions import v2process_batch_with_embedding

   
# also, give a code block that would call the function to get the time value for a user id. if there is no entry found for the user id, set start time as the current day starting time. else the time fetched form the fucntion is start. end is always the current time. 
    
    # get start date
    start = get_last_sync_time(user_id)

    print("start =", start)
    print(f"--------------")

    # If start is a string, convert it to datetime
    if isinstance(start, str):
        start = parse_iso_utc(start)

    if start is None:
        # start of today's date in UTC
        start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)


    # convert to ISO8601 with Z
    start_date = start.isoformat().replace("+00:00", "Z")

    # end_date is now
    end_dt = datetime.utcnow().replace(tzinfo=timezone.utc)
    end_date = end_dt.isoformat().replace("+00:00", "Z")

    print("start =", start_date)
    print("end   =", end_date)

    print(f"--------------")
    print(f"integration : {integration}")
    print(f"----------------")

    connection = connect_to_rds()
    cursor = connection.cursor()

    my_email = ""
    primary_user_id = ""
    if integration:
        cursor.execute(
            """
                SELECT  email,primary_user_id_fk
                FROM integrations
                WHERE user_id = %s
            """,
            (str(user_id),),
        )
        row = cursor.fetchone()
        my_email, primary_user_id = row
        print(f"{my_email} | {primary_user_id}")

    else:
        cursor.execute(
            """
                    SELECT  email
                    FROM users
                    WHERE user_id = %s
                """,
            (str(user_id),),
        )

        row = cursor.fetchone()

        if not row:
            jsonify({"error": "Microsoft user not found"}), 404

        my_email = row[0]

    any_new_messages = False
    all_results = []
    complete_results = 0
    embedding_futures = []
    start_time = time.perf_counter()

    try:
        # Get total conversation info without fetching messages (optional)
        total_threads = await get_outlook_thread_count_dynamic(
            user_id=user_id,
            start_date=start_date,
            end_date=end_date,
            min_days=min_days,
            integration=integration,
        )

        if not total_threads:
            return {"error": "Cannot fetch thread info", "status": "failed"}

        threads_max = total_threads["conversationsTotal"]["count"]
        threads = total_threads["conversationsTotal"]["conversations"]

        print("====== OUTLOOK THREAD SUMMARY ======")
        print(f"Total threads: {threads_max}")
        print(f"First thread raw object: {threads[0] if threads else 'No threads'}")
        print(f"my_email : {my_email}")
        print("====================================")

        # startdate = total_convos["start_date"]
        # enddate = total_convos["end_date"]

        print(f"🚀 Starting continuous Outlook batch processing for user {user_id}")
        print(f"total conversations: {threads_max}")

        semaphore = asyncio.Semaphore(5)

        async def process_with_semaphore(
            conv_batch, batch_count, ticket_allocator, integration=None
        ):
            nonlocal complete_results, any_new_messages
            async with semaphore:
                try:
                    outlook_result = await v2fetch_outlook_messages_batch(
                        user_id,
                        conv_batch,
                        my_email,
                        batch_count,
                        connection,
                        integration=integration,
                        primary_user_id = primary_user_id
                    )
                except Exception as e:
                    print(f"❌ Error fetching Outlook batch {batch_count}: {e}")
                    import traceback

                    traceback.print_exc()
                    return None

                if outlook_result.get("status") != "success":
                    print(
                        f"❌ Outlook batch {batch_count} failed: {outlook_result.get('error')}"
                    )
                    return None

                # print("====================================")
                # print(f"outlook_result : {outlook_result}")
                # print("====================================")

                new_messages = outlook_result.get("new_messages", 0)
                if new_messages > 0:
                    any_new_messages = True
                    print(f"📬 Batch {batch_count}: {new_messages} new messages")
                else:
                    print(f"📭 Batch {batch_count}: no new messages")

                complete_results += new_messages

                current_batch_messages = outlook_result.get("grouped_messages", {})
                if new_messages > 0 and current_batch_messages:
                    lance_folder = os.path.join(
                        pathconfig.basepath,
                        "messages",
                        user_id,
                        f"lance_folder:{batch_count}",
                    )
                    os.makedirs(lance_folder, exist_ok=True)

                    # print(f"current_batch_messages : {current_batch_messages}")
                    task = asyncio.to_thread(
                        v2process_batch_with_embedding,
                        user_id,
                        current_batch_messages,
                        batch_count,
                        lance_folder,
                        ticket_allocator,
                        None,
                        integration=integration,
                    )
                    embedding_futures.append(task)
                return outlook_result

        # Split into batches
        max_batchval = len(threads)
        batch_size = min(1000, max(100, len(threads) // 2 or 1))
        batches = [
            threads[i : i + batch_size] for i in range(0, max_batchval, batch_size)
        ]
        ticket_allocator = await TicketAllocator.create(user_id)

        async def process_batch(batch_index, batch, integration=None):
            batch_start_time = time.perf_counter()
            print(
                f"\n⚡ Starting batch {batch_index+1}/{len(batches)} with {len(batch)} conversations..."
            )
            results = await asyncio.gather(
                process_with_semaphore(
                    batch, batch_index + 1, ticket_allocator, integration=integration
                ),
                return_exceptions=True,
            )
            batch_runtime = time.perf_counter() - batch_start_time
            print(f"✅ Finished batch {batch_index+1} in {batch_runtime:.2f} seconds")
            return results

        all_batch_results = await asyncio.gather(
            *[
                process_batch(i, batch, integration=integration)
                for i, batch in enumerate(batches)
            ],
            return_exceptions=True,
        )

        all_results = [
            item
            for batch_results in all_batch_results
            for item in batch_results
            if item
        ]

        # Update Redis cache and wait for embeddings


        # print("------------------------")
        # print(f"saved to redis: {all_results}")
        # print("------------------------")

        redis_service = RedisService()
        await update_user_message_cache(
            redis_service, user_id, all_results, newly_creation=True
        )

        if embedding_futures:
            await asyncio.gather(*embedding_futures)

        total_runtime = time.perf_counter() - start_time
        print(
            f"\n🎯 Completed processing {threads_max} conversations in {total_runtime:.2f} seconds, total messages: {complete_results}"
        )

        # ✅ Only update umail_json + finalize if any batch had new messages
        if any_new_messages:
            if integration:
                update_umail_json_integration(
                    user_id=user_id, new_count=threads_max, connection=connection
                )
                await ticket_allocator.finalize()
                folder_path = os.path.join(pathconfig.basepath, "messages",user_id)
                if os.path.exists(folder_path):
                    shutil.rmtree(folder_path)
                    print(f"🗑️ Deleted folder and contents: {folder_path}")
                else:
                    print(f"⚠️ Folder not found: {folder_path}")
            else:
                update_umail_json(
                    user_id=user_id, new_count=threads_max, connection=connection
                )
                await ticket_allocator.finalize()
                folder_path = os.path.join(pathconfig.basepath, "messages",user_id)
                if os.path.exists(folder_path):
                    shutil.rmtree(folder_path)
                    print(f"🗑️ Deleted folder and contents: {folder_path}")
                else:
                    print(f"⚠️ Folder not found: {folder_path}")

        else:
            print(
                "ℹ️ No new messages in any batch → skipping umail_json update/finalize"
            )

        # update the outlook_sync file
        # Get current UTC time
        current_utc = datetime.utcnow().replace(tzinfo=timezone.utc)

        # Convert to ISO8601 "Z" format
        current_time_str = current_utc.isoformat().replace("+00:00", "Z")

        # Call the function to set/update the user's sync time
        set_user_sync_time(user_id, current_time_str)

        return {
            "user":  user_id,
            "total_conversations": threads_max,
            "batches": len(batches),
            "runtime_seconds": total_runtime,
            "results": all_results,
        }

    except Exception as e:
        print(f"[ERROR] v2all_continuous_outlook failed: {e}")
        import traceback

        traceback.print_exc()
        return {"error": str(e), "status": "failed"}

    finally:
        try:
            if cursor:
                cursor.close()
            if connection:
                connection.close()
        except Exception:
            pass


def parse_iso_utc(value):
    if isinstance(value, datetime):
        return value

    if not value:
        raise ValueError("Timestamp is missing")

    if value.endswith("Z"):
        value = value.replace("Z", "+00:00")

    return datetime.fromisoformat(value)