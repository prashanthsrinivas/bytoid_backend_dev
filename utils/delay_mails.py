import os, json, threading, time
from datetime import datetime, timezone
from db.rds_db import connect_to_rds
from db.db_checkers import get_userid
from utils.celery_base import web_umail_sync

# Single status file for all users
SYNC_STATUS_FILE = "data/internal_user_sync.json"
os.makedirs(os.path.dirname(SYNC_STATUS_FILE), exist_ok=True)

# Global file lock for safe JSON read/write
global_file_lock = threading.Lock()

# Per-user locks in memory
user_locks = {}


def get_user_lock(user_id):
    """Get or create a threading.Lock() for a specific user."""
    if user_id not in user_locks:
        user_locks[user_id] = threading.Lock()
    return user_locks[user_id]


def read_status_file():
    """Read the full status JSON safely."""
    with global_file_lock:
        if os.path.isfile(SYNC_STATUS_FILE):
            with open(SYNC_STATUS_FILE, "r") as f:
                # print("reading local file")
                return json.load(f)
        return {}


def write_status_file(status_data):
    """Write the full status JSON safely."""
    with global_file_lock:
        with open(SYNC_STATUS_FILE, "w") as f:
            # print("writing status file")
            json.dump(status_data, f, indent=2)


class DelayTrigger:
    def __init__(self, wait_seconds=30):
        self.wait_seconds = wait_seconds
        self.status_file = "data/internal_user_sync.json"

    def trigger(self, email, history_id, channel=None, integration=None):
        """Queue a per-user delayed trigger."""
        # print(
        #     f"inside trigger for email : {email} : integration - {integration} : channel : {channel}"
        # )
        user_id = None
        status_data = read_status_file()

        # Check if user exists in JSON
        for uid, info in status_data.items():
            if info.get("email") == email:
                user_id = uid
                break

        # If not found, query DB
        if not user_id:
            connection = connect_to_rds()
            user_id = get_userid(email, connection)
            connection.close()
            if not user_id:
                # print(f"[WARN] User not found: {email}")
                return

        # Update user status in JSON
        now_iso = datetime.now(timezone.utc).isoformat()
        user_lock = get_user_lock(user_id)
        with user_lock:
            status_data = read_status_file()
            user_info = status_data.get(user_id, {})
            user_info["email"] = email
            user_info["last_history"] = history_id
            user_info["timestamp"] = now_iso
            if user_info.get("status") not in ("started", "pending"):
                user_info["status"] = "pending"
            status_data[user_id] = user_info
            write_status_file(status_data)

        # Start background thread for delayed trigger
        threading.Thread(
            target=self._delayed_trigger,
            args=(user_id, channel, integration),
            daemon=True,
        ).start()

    def _delayed_trigger(self, user_id, channel, integration):
        user_lock = get_user_lock(user_id)
        MAX_STARTED_AGE = 60  # 1 minutes in seconds

        # print(f"_delayed_trigger for userid: {user_id} , channel : {channel}")

        while True:
            with user_lock:
                status_data = read_status_file()
                user_info = status_data.get(user_id)

                if not user_info:
                    # print("no user found for trigger")
                    return

                status = user_info.get("status")
                ts_str = user_info.get("timestamp")
                ts = (
                    datetime.fromisoformat(ts_str)
                    if ts_str
                    else datetime.now(timezone.utc)
                )

                # If already started, check age
                if status == "started":
                    age_seconds = (datetime.now(timezone.utc) - ts).total_seconds()

                    if age_seconds < MAX_STARTED_AGE:
                        # Started recently → do not re-trigger
                        # print(
                        #     f"Status 'started' but age {age_seconds}s < {MAX_STARTED_AGE}s → skipping."
                        # )
                        return
                    else:
                        # Started old → allow retry
                        # print(
                        #     f"Status 'started' but old ({age_seconds}s). Retrying trigger."
                        # )
                        # Treat as pending
                        status = "pending"

                # Normal pending wait logic
                elapsed = (datetime.now(timezone.utc) - ts).total_seconds()
                wait_time = max(0, self.wait_seconds - elapsed)

            # Sleep outside lock
            time.sleep(wait_time)

            # Trigger Celery task
            with user_lock:
                status_data = read_status_file()
                user_info = status_data.get(user_id)
                if not user_info:
                    return

                # Re-check status (protect from races)
                status = user_info.get("status")
                ts_str = user_info.get("timestamp")
                ts = (
                    datetime.fromisoformat(ts_str)
                    if ts_str
                    else datetime.now(timezone.utc)
                )

                # If started again recently, re-check after sleep
                if status == "started":
                    age_seconds = (datetime.now(timezone.utc) - ts).total_seconds()

                    if age_seconds < MAX_STARTED_AGE:
                        # print(
                        #     f"[ABORT] Second check: still started recently ({age_seconds}s)."
                        # )
                        return
                    # else:
                    # print(
                    #     f"[CONTINUE] Second check: started old ({age_seconds}s). Proceeding."
                    # )

                # Set started
                user_info["status"] = "started"
                user_info["timestamp"] = datetime.now(timezone.utc).isoformat()
                status_data[user_id] = user_info
                write_status_file(status_data)

            # print(
            #     f"[INFO] Triggering umail_sync for user {user_id} ({user_info.get('email')})"
            # )

            web_umail_sync.delay(user_id, channel=channel, integration=integration)

            # Mark as complete immediately (your logic)
            with user_lock:
                status_data = read_status_file()
                user_info = status_data.get(user_id)
                if user_info:
                    user_info["status"] = "complete"
                    user_info["timestamp"] = datetime.now(timezone.utc).isoformat()
                    status_data[user_id] = user_info
                    # print("complete made ok")
                    write_status_file(status_data)
            return
