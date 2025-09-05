from datetime import datetime, timezone
import json
from create_db import connect_to_rds
from db.db_checkers import get_existing_umail_json, update_umail_json
from db.rds_db import get_cursor
from gmail_route.gmail_service import GmailService
from gmail_route.routes import fetch_gmail_messages_batch, v2fetch_gmail_messages_batch
from umail_helper.mails_process import (
    analyze_and_collect_messages_for_batch,
    vtooanalyze_and_collect_messages_for_batch,
)
import shutil
import asyncio
import os
import time
from cust_helpers import pathconfig
from umail_lance.umail_lance_agent import UmailLanceClient
from datetime import datetime, timezone
from dateutil.relativedelta import relativedelta
from utils.base_logger import get_logger

from glide import (
    GlideClusterClient,
    GlideClusterClientConfiguration,
    NodeAddress,
)

logger = get_logger(__name__)


from concurrent.futures import ThreadPoolExecutor

from utils.base_logger import get_logger

# Create global executor
executor = ThreadPoolExecutor(max_workers=8)  # CPU heavy embedding jobs


logger = get_logger(__name__)

TTL_90_DAYS = 90 * 24 * 60 * 60

addresses = [
    NodeAddress("bytoidcache-w2ofwh.serverless.cac1.cache.amazonaws.com", 6379)
]

config = GlideClusterClientConfiguration(addresses=addresses, use_tls=True)


async def getall_continuous(user_id):
    """
    Continuously fetch and process batches of 100 - 200 emails (async + threading for CPU tasks)
    """
    start_time = time.perf_counter()
    timestamp = datetime.now(timezone.utc)
    date_str = timestamp.strftime("%Y-%m-%d")
    file_loc = f"cust_helpers/messages/{user_id}/{date_str}"

    total_processed = 0
    batch_count = 0
    next_page_token = None
    batch_size = 200
    serv = GmailService(user_id)
    total_messages = serv.get_inbox_stats()

    print(
        f"🚀 Starting continuous batch processing for user {user_id}, "
        f"total primary inbox messages: {total_messages}"
    )

    loop = asyncio.get_event_loop()

    while True:
        batch_count += 1
        print(f"\n📦 Processing batch {batch_count} (batch size: {batch_size})")

        # Fetch Gmail batch (async I/O)
        gmail_result = await fetch_gmail_messages_batch(
            user_id, page_token=next_page_token, batch_size=batch_size
        )

        if gmail_result.get("status") != "success":
            print(f"❌ Gmail batch {batch_count} failed: {gmail_result.get('error')}")
            break

        new_messages = gmail_result.get("new_messages", 0)
        next_page_token = gmail_result.get("next_page_token")
        current_batch_messages = gmail_result.get("grouped_messages", {})

        if new_messages == 0:
            print(f"📭 No new messages in batch {batch_count}")
            if not next_page_token:
                print("🏁 No more emails to fetch")
                break
        else:
            print(f"📬 Batch {batch_count}: {new_messages} new messages")
            total_processed += new_messages

            print(f"🔍 Analyzing + embedding batch {batch_count}...")

            lance_folder = os.path.join(
                pathconfig.basepath, "messages", user_id, f"lance_folder:{batch_count}"
            )

            # Run heavy work in threads
            await loop.run_in_executor(
                executor,
                process_batch_with_embedding,
                user_id,
                current_batch_messages,
                batch_count,
                lance_folder,
            )

            print(f"✅ Batch {batch_count} processing complete")

        if not next_page_token:
            print("🏁 Reached end of available emails")
            break

        print(
            f"📊 Progress: {total_processed}/{total_messages} emails processed "
            f"across {batch_count} batches"
        )

        await asyncio.sleep(1)

    end_time = time.perf_counter()
    print(
        f"✅ All batches complete! Total: {total_processed}/{total_messages} emails "
        f"in {batch_count} batches | Took {end_time - start_time:.2f}s"
    )
    return f"OK - Processed {total_processed} emails in {batch_count} batches"


async def get_datewise_info_base(userid, endDate=None, startDate=None, months=3):
    try:
        # Get end_date from query params or default to today (UTC)
        enddate_str = endDate or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        enddate = datetime.fromisoformat(enddate_str)

        # Get start_date from query params or default to N months before end_date
        startdate_str = startDate or (enddate - relativedelta(months=months)).strftime(
            "%Y-%m-%d"
        )

        gmail_service = GmailService(userid)
        inbox_count = await gmail_service.get_inbox_date_wise_stats_dynamic(
            start_date=startdate_str, end_date=enddate_str
        )

        return inbox_count

    except Exception as e:
        logger.error(
            f"[get_datewise_info_base] Failed for user {userid}: {str(e)}",
            exc_info=True,
        )
        return {"status": "failed", "error": str(e)}


async def v2all_continuous(user_id, startdate=None, enddate=None):
    """
    Run Gmail fetch + processing in parallel batches.
    Each batch also runs 4 heavy processes in parallel.
    """
    existing_json = get_existing_umail_json(user_id)
    today = datetime.now(timezone.utc).date()

    if existing_json and existing_json.get("history"):
        total_messages = await get_datewise_info_base(userid=user_id, months=3)
        newly_creation = False
    else:
        # First time → 3 months back
        total_messages = await get_datewise_info_base(userid=user_id, months=3)
        newly_creation = True
    start_time = time.perf_counter()
    # Database connection
    connection = connect_to_rds()
    if connection is None:
        return {"error": "Database connection failed", "status": "failed"}

    # max_emails = total_messages["final_msg"]
    threads_max = total_messages["threadsTotal"]["count"]
    threads = total_messages["threadsTotal"]["threads"]
    my_email = total_messages["email"]

    print(
        f"🚀 Starting continuous batch processing for user {user_id}, "
        f" total threads: {threads_max}  with creation {newly_creation}"
    )

    semaphore = asyncio.Semaphore(5)
    complete_results = 0
    loop = asyncio.get_running_loop()
    embedding_futures = []  # keep track globally inside v2all_continuous

    async def process_with_semaphore(threads, batch_count, max_batchval):
        nonlocal complete_results  # allow updating outer var
        async with semaphore:
            with get_cursor(connection) as cursor:
                gmail_result = await v2fetch_gmail_messages_batch(
                    user_id, threads, my_email, batch_count, cursor
                )

            if gmail_result.get("status") != "success":
                print(
                    f"❌ Gmail batch {batch_count} failed: {gmail_result.get('error')}"
                )
                return None
            complete_results += len(gmail_result)

            new_messages = gmail_result.get("new_messages", 0)
            current_batch_messages = gmail_result.get("grouped_messages", {})

            if new_messages == 0:
                print(f"📭 No new messages in batch {batch_count}")
                return None

            print(f"📬 Batch {batch_count}: {new_messages} new messages")

            # Run 4 heavy processes in parallel threads
            lance_folder = os.path.join(
                pathconfig.basepath, "messages", user_id, f"lance_folder:{batch_count}"
            )

            # Pass only data, not cursor
            task = asyncio.to_thread(
                v2process_batch_with_embedding,
                user_id,
                current_batch_messages,
                batch_count,
                lance_folder,
                None,
            )
            embedding_futures.append(task)  # collect it
            return gmail_result

    # Split into chunks
    max_batchval = len(threads)
    batch_size = min(1000, max(100, len(threads) // 2 or 1))

    batches = [threads[i : i + batch_size] for i in range(0, max_batchval, batch_size)]
    client = await GlideClusterClient.create(config)

    async def process_batch(batch_index, batch):
        batch_start_time = time.perf_counter()
        print(
            f"\n⚡ Starting batch {batch_index+1}/{max_batchval} with {len(batch)} threads..."
        )

        # tasks = [process_with_semaphore(thread, batch_index + 1) for thread in batch]
        tasks = [process_with_semaphore(batch, batch_index + 1, max_batchval)]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        batch_runtime = time.perf_counter() - batch_start_time
        print(f"✅ Finished batch {batch_index+1} in {batch_runtime:.2f} seconds")

        return results

    # 🔑 Run ALL batches in parallel
    all_batch_results = await asyncio.gather(
        *[process_batch(i, batch) for i, batch in enumerate(batches)],
        return_exceptions=True,
    )
    # Flatten results
    all_results = [
        item for batch_results in all_batch_results for item in batch_results if item
    ]
    if newly_creation:
        await client.set(
            f"{user_id}", json.dumps(all_results, default=str), TTL_90_DAYS
        )

    # Now wait for embeddings to finish
    if embedding_futures:
        await asyncio.gather(*embedding_futures)

    total_runtime = time.perf_counter() - start_time
    print(
        f"\n🎯 Completed processing {threads_max} threads in {total_runtime:.2f} seconds",
        f"\n {complete_results}",
    )
    # if max_batchval == batch_count:
    print("FINISHED ALL PROCESS")
    folder_path = os.path.join(pathconfig.basepath, "messages", user_id)
    today = datetime.now(timezone.utc)
    new_entry = {
        "date_start": startdate,  # from v2all_continuous
        "date_end": enddate,  # from v2all_continuous
        "processed_threads": threads_max,
        "timestamp": today.isoformat(),
        "newly_creation": newly_creation,
    }
    update_umail_json(user_id, new_entry)

    if os.path.exists(folder_path):
        shutil.rmtree(folder_path)  # ✅ removes all files + subfolders
        print(f"🗑️ Deleted folder and contents: {folder_path}")
    else:
        print(f"⚠️ Folder not found: {folder_path}")

    connection.close()

    return {
        "user": user_id,
        "total_threads": threads_max,
        "batches": len(batches),
        "runtime_seconds": total_runtime,
        "results": all_results,
    }


def v2process_batch_with_embedding(
    user_id,
    current_batch_messages,
    batch_count,
    lance_folder,
    cursor=None,
):
    async def _inner(cursor):
        start_time = time.perf_counter()
        print("||||||||||| Start time Lance |||||||||", start_time)
        if cursor is None:
            connection = connect_to_rds()
            with get_cursor(connection) as cursor:
                messages = await vtooanalyze_and_collect_messages_for_batch(
                    user_id, current_batch_messages, batch_count, cursor
                )
            connection.close()
        else:
            messages = await vtooanalyze_and_collect_messages_for_batch(
                user_id, current_batch_messages, batch_count, cursor
            )

        print(f"🧩 batch {batch_count} messages analyzed: {len(messages)}")
        client = UmailLanceClient(user_id)
        client.embed_json_files(lance_folder)
        total_runtime = time.perf_counter() - start_time
        print("************ Total Time Lance *******", total_runtime)

    asyncio.run(_inner(cursor))


def process_batch_with_embedding(
    user_id, current_batch_messages, batch_count, lance_folder
):
    """
    Run CPU-heavy tasks (analysis + embedding) inside a worker thread
    """
    messages = analyze_and_collect_messages_for_batch(
        user_id, current_batch_messages, batch_count
    )
    print("batch messages", len(messages))
    client = UmailLanceClient(user_id)
    client.embed_json_files(lance_folder)


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
