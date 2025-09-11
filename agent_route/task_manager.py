import threading
import uuid
from collections import defaultdict
import traceback
from datetime import datetime, timedelta
# from gmail_route.routes import sync_gmail_contacts
import logging
from typing import Callable, Any, Optional
import asyncio


# Stores task status: running/completed/failed
task_status = defaultdict(dict)
task_threads = {}

gmail_sync_status = defaultdict(dict)
gmail_sync_threads = {}


def run_background_task(userid, industry, func, **kwargs):
    task_id = str(uuid.uuid4())  # unique ID for tracking

    if userid in task_threads and task_threads[userid].is_alive():
        print(f"[DEBUG] Skipping — task already running for user {userid}")
        return {"message": "Task already running for this user.", "task_id": task_id}

    def task_wrapper():
        try:
            task_status[userid] = {"status": "running", "task_id": task_id}
            print(
                f"[DEBUG] Task {task_id} started for user={userid}, industry={industry}, kwargs={kwargs}"
            )

            func(userid=userid, industry=industry, **kwargs)

            task_status[userid]["status"] = "completed"
            print(f"[DEBUG] Task {task_id} completed successfully.")
        except Exception as e:
            task_status[userid]["status"] = "failed"
            task_status[userid]["error"] = str(e)
            print(f"[ERROR] Task {task_id} failed: {e}")

    thread = threading.Thread(target=task_wrapper, daemon=True)
    task_threads[userid] = thread
    thread.start()

    return {"message": "Task started.", "task_id": task_id}


def run_fetch_gmail_in_background(fetch_function, user_id) -> dict:
    # ✅ Do not start a new thread if one is already alive
    existing_thread = task_threads.get(user_id)
    if existing_thread and existing_thread.is_alive():
        return {"message": "Background fetch already running.", "user_id": user_id}

    def background_task():
        try:
            print(f"Starting background Gmail fetch for user: {user_id}")
            asyncio.run(fetch_function(user_id))
        except Exception as e:
            error_msg = f"Background Gmail fetch failed for user {user_id}: {str(e)}"
            print(f"[ERROR] {error_msg}")
            logging.error(error_msg)
        finally:
            # Once done, remove from dict
            task_threads.pop(user_id, None)

    thread = threading.Thread(
        target=background_task,
        name=f"gmail_fetch_{user_id}",
        daemon=True,
    )
    thread.start()
    task_threads[user_id] = thread
    return {"message": "Fetch started.", "user_id": user_id}



# Example usage:
def handle_result(result, user_id):
    """Optional callback to handle the result"""
    if result.get("status") == "ok":
        print(
            f"Successfully fetched {result.get('new_messages', 0)} new messages for {user_id}"
        )
    else:
        print(
            f"Error fetching messages for {user_id}: {result.get('error', 'Unknown error')}"
        )


# @task_bp.route("/gmail/sync_gmail_contacts/<user_id>")
def run_gmail_sync_background(user_id, func=None, **kwargs):
    task_id = str(uuid.uuid4())  # Unique ID for tracking

    # Check if task is already running for this user
    if user_id in gmail_sync_threads and gmail_sync_threads[user_id].is_alive():
        print(f"[DEBUG] Skipping — Gmail sync already running for user {user_id}")
        return {
            "success": False,
            "message": "Gmail sync already running for this user.",
            "task_id": gmail_sync_status[user_id].get("task_id", "unknown"),
            "status": "already_running",
        }

    def sync_task_wrapper():
        """Internal wrapper that handles the Gmail sync execution."""
        try:
            # Initialize task status
            gmail_sync_status[user_id] = {
                "status": "running",
                "task_id": task_id,
                "start_time": datetime.now().isoformat(),
                "end_time": None,
                "error": None,
                "result": None,
            }

            print(f"[DEBUG] Gmail sync task {task_id} started for user {user_id}")
            print(f"[DEBUG] Additional kwargs: {kwargs}")

            # Use provided function or default to sync_gmail_contacts
            if func:
                result = func(user_id, **kwargs)
            else:
                # Import and call sync_gmail_contacts
                result = sync_gmail_contacts(user_id, **kwargs)

            # Task completed successfully
            gmail_sync_status[user_id].update(
                {
                    "status": "completed",
                    "end_time": datetime.now().isoformat(),
                    "result": result,
                }
            )

            print(
                f"[DEBUG] Gmail sync task {task_id} completed successfully for user {user_id}"
            )

        except Exception as e:
            # Task failed
            error_msg = str(e)
            error_traceback = traceback.format_exc()

            gmail_sync_status[user_id].update(
                {
                    "status": "failed",
                    "end_time": datetime.now().isoformat(),
                    "error": error_msg,
                    "traceback": error_traceback,
                }
            )

            print(
                f"[ERROR] Gmail sync task {task_id} failed for user {user_id}: {error_msg}"
            )
            print(f"[ERROR] Traceback: {error_traceback}")

    # Create and start the background thread
    thread = threading.Thread(target=sync_task_wrapper, daemon=True)
    thread.name = f"GmailSync-{user_id}-{task_id[:8]}"
    gmail_sync_threads[user_id] = thread
    thread.start()

    return {
        "success": True,
        "message": "Gmail sync started in background.",
        "task_id": task_id,
        "status": "started",
    }


def get_gmail_sync_status(user_id):
    if user_id not in gmail_sync_status:
        return {
            "status": "not_found",
            "message": "No Gmail sync task found for this user",
        }

    return gmail_sync_status[user_id]


def cancel_gmail_sync(user_id):
    if user_id in gmail_sync_threads and gmail_sync_threads[user_id].is_alive():
        gmail_sync_status[user_id]["status"] = "cancelled"
        gmail_sync_status[user_id]["end_time"] = datetime.now().isoformat()

        return {
            "success": True,
            "message": "Gmail sync marked as cancelled (thread may still be running)",
        }
    else:
        return {"success": False, "message": "No active Gmail sync found for this user"}


def get_all_gmail_sync_status():
    return dict(gmail_sync_status)


def cleanup_completed_gmail_syncs(max_age_hours=24):
    cutoff_time = datetime.now() - timedelta(hours=max_age_hours)
    users_to_remove = []

    for user_id, status_info in gmail_sync_status.items():
        if status_info.get("status") in ["completed", "failed", "cancelled"]:
            if "end_time" in status_info and status_info["end_time"]:
                try:
                    end_time = datetime.fromisoformat(status_info["end_time"])
                    if end_time < cutoff_time:
                        users_to_remove.append(user_id)
                except ValueError:
                    # If time parsing fails, remove old entries anyway
                    users_to_remove.append(user_id)

    # Remove old entries
    for user_id in users_to_remove:
        if user_id in gmail_sync_status:
            del gmail_sync_status[user_id]
        if user_id in gmail_sync_threads:
            del gmail_sync_threads[user_id]

    print(f"[DEBUG] Cleaned up {len(users_to_remove)} old Gmail sync tasks")
    return len(users_to_remove)


