from datetime import date, datetime, timezone
import json
from create_db import connect_to_rds
from db.db_checkers import (
    get_existing_autopilot_json,
    get_existing_umail_json,
    update_umail_json,
)
from db.rds_db import get_cursor
from gmail_route.gmail_service import GmailService
from gmail_route.routes import v2fetch_gmail_messages_batch
from umail_helper.auto_rep import autoReplyhelper
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
from utils.redis_config import redis_config_glide
from glide import (
    GlideClusterClient,
)

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
    userid, connection, endDate=None, startDate=None, months=3
):

    try:
        today = datetime.now(timezone.utc)

        enddate = to_datetime_safe(endDate, default=today)
        startdate = to_datetime_safe(
            startDate, default=enddate - relativedelta(months=months)
        )

        enddate_str = enddate.strftime("%Y-%m-%d")
        startdate_str = startdate.strftime("%Y-%m-%d")

        gmail_service = GmailService(userid, connection)
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
    print(threads_max, last_processed)
    return threads_max != last_processed


async def v2all_continuous(user_id):
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

    try:
        existing_json = get_existing_umail_json(user_id, connection)
        today = datetime.now(timezone.utc).date()

        if existing_json and existing_json.get("history"):
            relevant_date = get_relevant_processed_date(existing_json)
            logger.info("last fetched date %s", relevant_date)
            total_messages = await get_datewise_info_base(
                userid=user_id, connection=connection, startDate=relevant_date
            )
            newly_creation = False
        else:
            total_messages = await get_datewise_info_base(
                userid=user_id, connection=connection, months=3
            )
            newly_creation = True

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
                    print(f"❌ Error fetching Gmail batch {batch_count}: {e}")
                    import traceback

                    traceback.print_exc()
                    return None

                if gmail_result.get("status") != "success":
                    print(
                        f"❌ Gmail batch {batch_count} failed: {gmail_result.get('error')}"
                    )
                    return None

                complete_results += len(gmail_result)

                new_messages = gmail_result.get("new_messages", 0)
                if new_messages > 0:
                    any_new_messages = True  # mark that we did get something
                    print(f"📬 Batch {batch_count}: {new_messages} new messages")
                else:
                    print(f"📭 Batch {batch_count}: no new messages")

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
                    print(f"📭 Batch {batch_count}: no new messages to process")
                    return None
                return gmail_result

        max_batchval = len(threads)
        batch_size = min(1000, max(100, len(threads) // 2 or 1))
        batches = [
            threads[i : i + batch_size] for i in range(0, max_batchval, batch_size)
        ]
        client = await GlideClusterClient.create(redis_config_glide)
        ticket_allocator = await TicketAllocator.create(user_id)

        async def process_batch(batch_index, batch):
            batch_start_time = time.perf_counter()
            print(
                f"\n⚡ Starting batch {batch_index+1}/{max_batchval} with {len(batch)} threads..."
            )
            tasks = [
                process_with_semaphore(
                    batch, batch_index + 1, max_batchval, ticket_allocator
                )
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            batch_runtime = time.perf_counter() - batch_start_time
            print(f"✅ Finished batch {batch_index+1} in {batch_runtime:.2f} seconds")
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
        # print("all_results", all_results)

        if newly_creation:
            print("NEW CREATION attaching to valkey", len(all_results))
            await client.set(
                f"{user_id}", json.dumps(all_results, default=str), TTL_90_DAYS
            )

        # wait for embeddings to finish
        if embedding_futures:
            await asyncio.gather(*embedding_futures)

        total_runtime = time.perf_counter() - start_time
        print(
            f"\n🎯 Completed processing {threads_max} threads in {total_runtime:.2f} seconds",
            f"\n total gmail results counted: {complete_results}",
        )

        # ✅ Only update umail_json + finalize if any batch had new messages
        if any_new_messages:
            folder_path = os.path.join(pathconfig.basepath, "messages", user_id)
            today_ts = datetime.now(timezone.utc)
            # new_entry = {
            #     "date_start": startdate,
            #     "date_end": enddate,
            #     "processed_threads": threads_max,
            #     "timestamp": today_ts.isoformat(),
            #     "newly_creation": newly_creation,
            # }
            update_umail_json(
                user_id=user_id, new_count=threads_max, connection=connection
            )
            await ticket_allocator.finalize()

            if os.path.exists(folder_path):
                shutil.rmtree(folder_path)
                print(f"🗑️ Deleted folder and contents: {folder_path}")
            else:
                print(f"⚠️ Folder not found: {folder_path}")
        else:
            print(
                "ℹ️ No new messages in any batch → skipping umail_json update/finalize"
            )
        if not newly_creation and any_new_messages:
            print("Triggering this api")
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
        print(f"[ERROR] v2all_continuous failed: {e}")
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
):
    async def _inner(cursor):
        start_time = time.perf_counter()
        print("||||||||||| Start time Lance |||||||||", start_time)

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

            print(f"🧩 batch {batch_count} messages analyzed: {len(messages)}")

            client = UmailLanceClient(user_id)
            # run CPU-bound embedding in a thread so we don’t block
            await asyncio.to_thread(client.embed_json_files, lance_folder)

            total_runtime = time.perf_counter() - start_time
            print("************ Total Time Lance *******", total_runtime)

        except Exception as e:
            # log/handle the error
            print(f"[ERROR] v2process_batch_with_embedding failed: {e}")
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
#     print("batch messages", len(messages))
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
