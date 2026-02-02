from datetime import date, datetime, timezone
import json
from db.rds_db import connect_to_rds
from db.db_checkers import (
    get_existing_autopilot_json,
    get_existing_umail_json,
    get_existing_umail_json_integration,
    update_umail_json,
    update_umail_json_integration,
)
from db.rds_db import get_cursor
from services.gmail_service import GmailService
from gmail_route.routes import v2fetch_gmail_messages_batch
from services.redis_service import RedisService
from umail_helper.auto_rep import autoReplyhelper
from umail_helper.helper import update_user_message_cache
from umail_helper.mails_process import (
    vtooanalyze_and_collect_messages_for_batch,
)
import shutil
import asyncio
import os
import time
from cust_helpers import pathconfig
from umail_helper.ticketalloc import TicketAllocator
from umail_lance.umail_lance_agent import UmailLanceClient
from datetime import datetime, timezone
from dateutil.relativedelta import relativedelta
from utils.base_logger import get_logger

from request_context import current_user_id

logger = get_logger(__name__)


from concurrent.futures import ThreadPoolExecutor

from utils.base_logger import get_logger

# Create global executor
executor = ThreadPoolExecutor(max_workers=8)  # CPU heavy embedding jobs


logger = get_logger(__name__)

TTL_90_DAYS = 90 * 24 * 60 * 60


def to_datetime_safe(val, default=None):
    if not val:
        return default
    if isinstance(val, datetime):
        return val.astimezone(timezone.utc)
    if isinstance(val, date):  # <-- handles datetime.date
        return datetime.combine(val, datetime.min.time(), tzinfo=timezone.utc)
    if isinstance(val, str):
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(val, fmt).replace(tzinfo=timezone.utc)
            except Exception:
                continue
        try:
            return datetime.fromisoformat(val).astimezone(timezone.utc)
        except Exception:
            return default
    return default


async def get_datewise_info_base(
    userid, connection, endDate=None, startDate=None, months=1, integration=None
):

    try:
        today = datetime.now(timezone.utc)

        enddate = to_datetime_safe(endDate, default=today)
        # startdate = to_datetime_safe(
        #     startDate, default=enddate - relativedelta(months=months)
        # )
        startdate = to_datetime_safe(startDate, default=enddate - relativedelta(days=7))

        enddate_str = enddate.strftime("%Y-%m-%d")
        startdate_str = startdate.strftime("%Y-%m-%d")
        gmail_service = GmailService(userid, connection, integration=integration)
        inbox_count = await gmail_service.get_inbox_date_wise_stats_dynamic(
            start_date=startdate_str, end_date=enddate_str
        )

        # protect against missing keys
        return inbox_count

    except Exception as e:
        logger.error(
            f"[get_datewise_info_base] Failed for user {userid}: {str(e)}",
            exc_info=True,
        )
        return {"status": "failed", "error": str(e)}


def get_relevant_processed_date(existing_json):
    """
    - If today's date exists in history -> return today's date.
    - Else return the most recent earlier processed date.
    - Returns a date (YYYY-MM-DD) or None.
    """
    if not existing_json or "history" not in existing_json:
        return None

    today = datetime.now(timezone.utc).date()
    dates = []

    for h in existing_json["history"]:
        ts = h.get("timestamp")
        if not ts:
            continue
        try:
            ts_date = datetime.fromisoformat(ts).date()
            dates.append(ts_date)
        except Exception:
            continue

    if not dates:
        return None

    # If today exists in history → return today
    if today in dates:
        return today

    # Otherwise return the latest date before today
    past_dates = [d for d in dates if d < today]
    return max(past_dates) if past_dates else None


def has_new_threads(existing_json, total_messages):
    """
    Check if today's threads have increased.
    - Look at history entries with today's date (from timestamp).
    - If no entry for today → return True (new run needed).
    - If today's entry exists → compare processed_threads with current total.
    """
    today = datetime.now(timezone.utc).date().isoformat()  # "YYYY-MM-DD"

    history = existing_json.get("history", []) if existing_json else []
    today_record = None

    for h in reversed(history):  # check from latest backward
        ts = h.get("timestamp")
        if not ts:
            continue
        try:
            ts_date = datetime.fromisoformat(ts).date().isoformat()
            if ts_date == today:
                today_record = h
                break
        except Exception:
            continue

    threads_max = total_messages["threadsTotal"]["count"]

    if not today_record:
        # no entry for today → treat as new threads exist
        return True

    last_processed = today_record.get("processed_threads", 0)
    # print(threads_max, last_processed)
    return threads_max != last_processed


async def v2all_continuous(user_id, integration=None):
    """
    Run Gmail fetch + processing in parallel batches.
    Each batch also runs heavy embedding processes in parallel.
    """
    connection = connect_to_rds()
    if connection is None:
        return {"error": "Database connection failed", "status": "failed"}

    # default flags
    any_new_messages = False
    all_results = []
    complete_results = 0
    embedding_futures = []
    start_time = time.perf_counter()
    # print(f"integration inside v2all_continuous: {integration}")
    try:
        if integration:
            existing_json = get_existing_umail_json_integration(user_id, connection)

        else:
            existing_json = get_existing_umail_json(user_id, connection)
        today = datetime.now(timezone.utc).date()

        if existing_json and existing_json.get("history"):
            relevant_date = get_relevant_processed_date(existing_json)
            logger.info("last fetched date %s", relevant_date)
            total_messages = await get_datewise_info_base(
                userid=user_id,
                connection=connection,
                startDate=relevant_date,
                integration=integration,
            )
            newly_creation = False
        else:
            total_messages = await get_datewise_info_base(
                userid=user_id, connection=connection, months=1, integration=integration
            )
            newly_creation = True
        if not total_messages:
            return "cant have the messages fetched."
        # total_messages = await get_datewise_info_base(
        #     userid=user_id, connection=connection, months=1, integration=integration
        # )
        # newly_creation = True

        # print(f"total messages:")
        # print(f"{total_messages}")
        threads_max = total_messages["threadsTotal"]["count"]
        threads = total_messages["threadsTotal"]["threads"]
        my_email = total_messages["email"]
        startdate = total_messages["start_date"]
        enddate = total_messages["end_date"]

        logger.info("🚀 Starting continuous batch processing for user %s ", user_id)
        logger.info("total threads: %s with creation=%s", threads_max, newly_creation)

        semaphore = asyncio.Semaphore(5)

        async def process_with_semaphore(
            threads, batch_count, max_batchval, ticket_allocator
        ):
            nonlocal complete_results, any_new_messages
            async with semaphore:
                try:
                    gmail_result = await v2fetch_gmail_messages_batch(
                        user_id,
                        threads,
                        my_email,
                        batch_count,
                        connection,
                        integration=integration,
                    )
                except Exception as e:
                    # print(f"❌ Error fetching Gmail batch {batch_count}: {e}")
                    import traceback

                    traceback.print_exc()
                    return None

                if gmail_result.get("status") != "success":
                    # print(
                    #     f"❌ Gmail batch {batch_count} failed: {gmail_result.get('error')}"
                    # )
                    return None

                complete_results += len(gmail_result)

                new_messages = gmail_result.get("new_messages", 0)
                if new_messages > 0:
                    any_new_messages = True  # mark that we did get something
                    # print(f"📬 Batch {batch_count}: {new_messages} new messages")
                # else:
                #     #print(f"📭 Batch {batch_count}: no new messages")

                current_batch_messages = gmail_result.get("grouped_messages", {})
                if new_messages > 0 and current_batch_messages:
                    lance_folder = os.path.join(
                        pathconfig.basepath,
                        "messages",
                        user_id,
                        f"lance_folder:{batch_count}",
                    )
                    os.makedirs(lance_folder, exist_ok=True)
                    task = asyncio.to_thread(
                        v2process_batch_with_embedding,
                        user_id,
                        current_batch_messages,
                        batch_count,
                        lance_folder,
                        ticket_allocator,
                        None,
                    )
                    embedding_futures.append(task)
                else:
                    # print(f"📭 Batch {batch_count}: no new messages to process")
                    return None
                return gmail_result

        max_batchval = len(threads)
        batch_size = min(1000, max(100, len(threads) // 2 or 1))
        batches = [
            threads[i : i + batch_size] for i in range(0, max_batchval, batch_size)
        ]
        ## client = await GlideClusterClient.create(redis_config_glide)
        ticket_allocator = await TicketAllocator.create(user_id)

        async def process_batch(batch_index, batch):
            batch_start_time = time.perf_counter()
            # print(
            #     f"\n⚡ Starting batch {batch_index+1}/{max_batchval} with {len(batch)} threads..."
            # )
            tasks = [
                process_with_semaphore(
                    batch, batch_index + 1, max_batchval, ticket_allocator
                )
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            batch_runtime = time.perf_counter() - batch_start_time
            # print(f"✅ Finished batch {batch_index+1} in {batch_runtime:.2f} seconds")
            return results

        all_batch_results = await asyncio.gather(
            *[process_batch(i, batch) for i, batch in enumerate(batches)],
            return_exceptions=True,
        )
        all_results = [
            item
            for batch_results in all_batch_results
            for item in batch_results
            if item
        ]
        ##print("all_results", all_results)

        # if newly_creation:
        # print("NEW CREATION attaching to valkey", len(all_results))
        # Merge all grouped_messages and collect next_page_token if any
        # merged_grouped = {}
        # next_page_token = None
        # total_new_messages = 0

        # for batch_result in all_results:
        #     if not isinstance(batch_result, dict):
        #         continue
        #     grouped = batch_result.get("grouped_messages", {})
        #     if isinstance(grouped, dict):
        #         merged_grouped.update(grouped)
        #     total_new_messages += batch_result.get("new_messages", 0)
        #     if batch_result.get("next_page_token"):
        #         next_page_token = batch_result["next_page_token"]

        # # Prepare cache payload
        # cache_payload = {
        #     "status": "success",
        #     "total_new_messages": total_new_messages,
        #     "next_page_token": next_page_token,
        #     "grouped_messages": merged_grouped,
        #     "updated_at": datetime.utcnow().isoformat(),
        # }

        # await client.set(
        #     f"{user_id}", json.dumps(all_results, default=str), TTL_90_DAYS
        # )
        # await client.set(
        #     f"{user_id}",
        #     json.dumps(cache_payload, default=str),
        #     TTL_90_DAYS,
        # )
        redis_service = RedisService()

        await update_user_message_cache(
            redis_service, user_id, all_results, newly_creation=newly_creation
        )

        # wait for embeddings to finish
        if embedding_futures:
            await asyncio.gather(*embedding_futures)

        total_runtime = time.perf_counter() - start_time
        # print(
        #     f"\n🎯 Completed processing {threads_max} threads in {total_runtime:.2f} seconds",
        #     f"\n total gmail results counted: {complete_results}",
        # )

        # ✅ Only update umail_json + finalize if any batch had new messages
        if any_new_messages:
            if integration:
                update_umail_json_integration(
                    user_id=user_id, new_count=threads_max, connection=connection
                )
                await ticket_allocator.finalize()
                folder_path = os.path.join(pathconfig.basepath, "messages", user_id)
                if os.path.exists(folder_path):
                    shutil.rmtree(folder_path)
                    # print(f"🗑️ Deleted folder and contents: {folder_path}")
                # else:
                #     #print(f"⚠️ Folder not found: {folder_path}")

            else:
                update_umail_json(
                    user_id=user_id, new_count=threads_max, connection=connection
                )
                await ticket_allocator.finalize()
                folder_path = os.path.join(pathconfig.basepath, "messages", user_id)
                if os.path.exists(folder_path):
                    shutil.rmtree(folder_path)
                    # print(f"🗑️ Deleted folder and contents: {folder_path}")
                # else:
                #     #print(f"⚠️ Folder not found: {folder_path}")
        # else:
        #     #print(
        #         "ℹ️ No new messages in any batch → skipping umail_json update/finalize"
        #     )
        if not newly_creation and any_new_messages:
            # print("Triggering this api autopilot check")
            pilotvalues = get_existing_autopilot_json(
                user_id=user_id, connection=connection
            )
            if pilotvalues is not None:
                autoReplyhelper(
                    all_results=all_results,
                    user_id=user_id,
                    my_email=my_email,
                    pilotvalues=pilotvalues,
                )

        return {
            "user": user_id,
            "total_threads": threads_max,
            "batches": len(batches),
            "runtime_seconds": total_runtime,
            "results": all_results,
        }

    except Exception as e:
        # print(f"[ERROR] v2all_continuous failed: {e}")
        import traceback

        traceback.print_exc()
        return {"error": str(e), "status": "failed"}

    finally:
        try:
            connection.close()
        except Exception:
            pass


async def fetchnextmonthmails(user_id, startDate):
    """
    Fetch next-month mails with correct date logic.
    startDate = boundary date (ISO string or datetime)
    bs_startdate = startDate minus 1 month
    """
    connection = connect_to_rds()
    if connection is None:
        return {"error": "Database connection failed", "status": "failed"}

    # Convert startDate safely
    if isinstance(startDate, str):
        startDate = to_datetime_safe(startDate, default=datetime.now(timezone.utc))
    elif isinstance(startDate, datetime):
        startDate = startDate.replace(tzinfo=timezone.utc)
    else:
        startDate = datetime.now(timezone.utc)
    any_new_messages = False
    all_results = []
    complete_results = 0
    embedding_futures = []
    start_time = time.perf_counter()

    # ---------------------------
    # Correct date computation
    # ---------------------------
    endDate = startDate  # end boundary
    bs_startdate = startDate - relativedelta(months=1)  # start boundary

    # Optional: strip to date() if backend expects date only
    # endDate = endDate.date()
    # bs_startdate = bs_startdate.date()

    # Debug logs
    # print("➡ startDate (end):", endDate)
    # print("➡ bs_startdate (start):", bs_startdate)

    try:
        newly_creation = False
        total_messages = await get_datewise_info_base(
            userid=user_id,
            connection=connection,
            endDate=endDate,
            startDate=bs_startdate,
        )
        if not total_messages:
            return "cant have the messages fetched."

        threads_max = total_messages["threadsTotal"]["count"]
        threads = total_messages["threadsTotal"]["threads"]
        my_email = total_messages["email"]
        startdate = total_messages["start_date"]
        enddate = total_messages["end_date"]

        logger.info("🚀 Starting continuous batch processing for user %s, ", user_id)
        logger.info("total threads: %s with creation=%s", threads_max, newly_creation)

        semaphore = asyncio.Semaphore(5)

        async def process_with_semaphore(
            threads, batch_count, max_batchval, ticket_allocator
        ):
            nonlocal complete_results, any_new_messages
            async with semaphore:
                try:
                    gmail_result = await v2fetch_gmail_messages_batch(
                        user_id, threads, my_email, batch_count, connection
                    )
                except Exception as e:
                    # print(f"❌ Error fetching Gmail batch {batch_count}: {e}")
                    import traceback

                    traceback.print_exc()
                    return None

                if gmail_result.get("status") != "success":
                    # print(
                    #     f"❌ Gmail batch {batch_count} failed: {gmail_result.get('error')}"
                    # )
                    return None

                complete_results += len(gmail_result)

                new_messages = gmail_result.get("new_messages", 0)
                if new_messages > 0:
                    any_new_messages = True  # mark that we did get something
                    # print(f"📬 Batch {batch_count}: {new_messages} new messages")
                # else:
                #     #print(f"📭 Batch {batch_count}: no new messages")

                current_batch_messages = gmail_result.get("grouped_messages", {})
                if new_messages > 0 and current_batch_messages:
                    lance_folder = os.path.join(
                        pathconfig.basepath,
                        "messages",
                        user_id,
                        f"lance_folder:{batch_count}",
                    )
                    os.makedirs(lance_folder, exist_ok=True)
                    task = asyncio.to_thread(
                        v2process_batch_with_embedding,
                        user_id,
                        current_batch_messages,
                        batch_count,
                        lance_folder,
                        ticket_allocator,
                        None,
                    )
                    embedding_futures.append(task)
                else:
                    # print(f"📭 Batch {batch_count}: no new messages to process")
                    return None
                return gmail_result

        max_batchval = len(threads)
        batch_size = min(1000, max(100, len(threads) // 2 or 1))
        batches = [
            threads[i : i + batch_size] for i in range(0, max_batchval, batch_size)
        ]
        ## client = await GlideClusterClient.create(redis_config_glide)
        ticket_allocator = await TicketAllocator.create(user_id)

        async def process_batch(batch_index, batch):
            batch_start_time = time.perf_counter()
            # print(
            #     f"\n⚡ Starting batch {batch_index+1}/{max_batchval} with {len(batch)} threads..."
            # )
            tasks = [
                process_with_semaphore(
                    batch, batch_index + 1, max_batchval, ticket_allocator
                )
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            batch_runtime = time.perf_counter() - batch_start_time
            # print(f"✅ Finished batch {batch_index+1} in {batch_runtime:.2f} seconds")
            return results

        all_batch_results = await asyncio.gather(
            *[process_batch(i, batch) for i, batch in enumerate(batches)],
            return_exceptions=True,
        )
        all_results = [
            item
            for batch_results in all_batch_results
            for item in batch_results
            if item
        ]
        ##print("all_results", all_results)

        # if newly_creation:
        #     # print("NEW CREATION attaching to valkey", len(all_results))
        #     # Merge all grouped_messages and collect next_page_token if any
        #     merged_grouped = {}
        #     next_page_token = None
        #     total_new_messages = 0

        #     for batch_result in all_results:
        #         if not isinstance(batch_result, dict):
        #             continue
        #         grouped = batch_result.get("grouped_messages", {})
        #         if isinstance(grouped, dict):
        #             merged_grouped.update(grouped)
        #         total_new_messages += batch_result.get("new_messages", 0)
        #         if batch_result.get("next_page_token"):
        #             next_page_token = batch_result["next_page_token"]

        #     # Prepare cache payload
        #     cache_payload = {
        #         "status": "success",
        #         "total_new_messages": total_new_messages,
        #         "next_page_token": next_page_token,
        #         "grouped_messages": merged_grouped,
        #         "updated_at": datetime.utcnow().isoformat(),
        #     }

        #     # await client.set(
        #     #     f"{user_id}", json.dumps(all_results, default=str), TTL_90_DAYS
        #     # )
        #     await client.set(
        #         f"{user_id}",
        #         json.dumps(cache_payload, default=str),
        #         TTL_90_DAYS,
        #     )
        redis_service = RedisService()

        await update_user_message_cache(
            redis_service, user_id, all_results, newly_creation=newly_creation
        )

        # wait for embeddings to finish
        if embedding_futures:
            await asyncio.gather(*embedding_futures)

        total_runtime = time.perf_counter() - start_time
        # print(
        #     f"\n🎯 Completed processing {threads_max} threads in {total_runtime:.2f} seconds",
        #     f"\n total gmail results counted: {complete_results}",
        # )

        # ✅ Only update umail_json + finalize if any batch had new messages
        if any_new_messages:
            update_umail_json(
                user_id=user_id, new_count=threads_max, connection=connection
            )
            await ticket_allocator.finalize()
            folder_path = os.path.join(pathconfig.basepath, "messages", user_id)
            if os.path.exists(folder_path):
                shutil.rmtree(folder_path)
                # print(f"🗑️ Deleted folder and contents: {folder_path}")
            # else:
            # print(f"⚠️ Folder not found: {folder_path}")
        # else:
        #     #print(
        #         "ℹ️ No new messages in any batch → skipping umail_json update/finalize"
        #     )
        # if not newly_creation and any_new_messages:
        #     #print("Triggering this api autopilot check")
        #     pilotvalues = get_existing_autopilot_json(
        #         user_id=user_id, connection=connection
        #     )
        #     if pilotvalues is not None:
        #         autoReplyhelper(
        #             all_results=all_results,
        #             user_id=user_id,
        #             my_email=my_email,
        #             pilotvalues=pilotvalues,
        #         )

        return {
            "user": user_id,
            "total_threads": threads_max,
            "batches": len(batches),
            "runtime_seconds": total_runtime,
            "results": all_results,
        }

    except Exception as e:
        # print(f"[ERROR] v2all_continuous failed: {e}")
        import traceback

        traceback.print_exc()
        return {"error": str(e), "status": "failed"}

    finally:
        try:
            connection.close()
        except Exception:
            pass


def v2process_batch_with_embedding(
    user_id,
    current_batch_messages,
    batch_count,
    lance_folder,
    ticket_allocator,
    cursor=None,
    integration=None,
):
    from utils.celery_base import run_lance_embedding

    async def _inner(cursor):
        start_time = time.perf_counter()
        # print("||||||||||| Start time Lance |||||||||", start_time)

        connection = None
        try:
            if cursor is None:
                # open our own connection
                connection = connect_to_rds()
                with get_cursor(connection) as cur:
                    messages = await vtooanalyze_and_collect_messages_for_batch(
                        user_id,
                        current_batch_messages,
                        batch_count,
                        cur,
                        ticket_allocator,
                        integration=integration,
                    )
            else:
                # using passed-in cursor
                messages = await vtooanalyze_and_collect_messages_for_batch(
                    user_id,
                    current_batch_messages,
                    batch_count,
                    cursor,
                    ticket_allocator,
                )

            # print(f"🧩 batch {batch_count} messages analyzed: {len(messages)}")

            client = UmailLanceClient(user_id)
            # run CPU-bound embedding in a thread so we don’t block
            # print(f"lance_folder : {lance_folder}")

            await client.embed_both_json_and_plain(lance_folder)
            # run_lance_embedding.delay(user_id, batch_count, lance_folder)

            total_runtime = time.perf_counter() - start_time
            # print("************ Total Time Lance *******", total_runtime)

        except Exception as e:
            # log/handle the error
            # print(f"[ERROR] v2process_batch_with_embedding failed: {e}")
            import traceback

            traceback.print_exc()

        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    # run the async inner
    asyncio.run(_inner(cursor))


# def process_batch_with_embedding(
#     user_id, current_batch_messages, batch_count, lance_folder
# ):
#     """
#     Run CPU-heavy tasks (analysis + embedding) inside a worker thread
#     """
#     messages = analyze_and_collect_messages_for_batch(
#         user_id, current_batch_messages, batch_count
#     )
#    #print("batch messages", len(messages))
#     client = UmailLanceClient(user_id)
#     client.embed_json_files(lance_folder)


# # actual function:
# @umail_bp.route("/get_all_messages/<user_id>", methods=["GET"])
# def getall(user_id):

#     timestamp = datetime.now(timezone.utc)
#     date_str = timestamp.strftime("%Y-%m-%d")
#     file_loc = f"cust_helpers/messages/{user_id}/{date_str}"
#     gmail = gmail_messages(user_id)
#     zoho = fetch_zoho_emails(user_id)

#     analyze_and_collect_messages(user_id)

#     return "OK"

dummy_batch_results = [
    {
        "grouped_messages": {
            "2025-12-04": {
                "gmail": [
                    {
                        "id": "msg_1",
                        "from": "alice@example.com",
                        "to": "mahender@example.com",
                        "subject": "Hello",
                        "timestamp": "2025-12-04T10:15:00Z",
                        "source": "gmail",
                        "direction": "inbound",
                    },
                    {
                        "id": "msg_2",
                        "from": "bob@example.com",
                        "to": "mahender@example.com",
                        "subject": "Status Update",
                        "timestamp": "2025-12-04T10:30:00Z",
                        "source": "gmail",
                        "direction": "inbound",
                    },
                ]
            }
        },
        "new_messages": 2,
        "next_page_token": "NEXT_TOKEN_123",
    },
    {
        "grouped_messages": {
            "2025-12-05": {
                "gmail": [
                    {
                        "id": "msg_3",
                        "from": "charlie@example.com",
                        "to": "mahender@example.com",
                        "subject": "Meeting Reminder",
                        "timestamp": "2025-12-05T08:00:00Z",
                        "source": "gmail",
                        "direction": "inbound",
                    }
                ]
            }
        },
        "new_messages": 1,
        "next_page_token": "NEXT_TOKEN_456",
    },
]


async def test_cache_insertion():
    redis_service = RedisService()

    # print("\nChecking Redis connection:")
    await redis_service.checker()

    # print("\nWriting dummy data...")
    data = await update_user_message_cache(
        redis_service,
        user_id="dummy_user",
        batch_results=dummy_batch_results,
        newly_creation=True,
    )

    # print("\nWritten payload:")
    # print(data)

    # print("\nReading back from Redis:")
    stored = await redis_service.get("umail_dummy_user")
    # print(stored)


# asyncio.run(test_cache_insertion())
