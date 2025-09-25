import os, json, threading, time
from datetime import datetime, timezone
from db.rds_db import connect_to_rds
from db.db_checkers import get_userid
from utils.celery_base import umail_sync

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
                return json.load(f)
        return {}


def write_status_file(status_data):
    """Write the full status JSON safely."""
    with global_file_lock:
        with open(SYNC_STATUS_FILE, "w") as f:
            json.dump(status_data, f, indent=2)


class DelayTrigger:
    def __init__(self, wait_seconds=30):
        self.wait_seconds = wait_seconds

    def trigger(self, email, history_id):
        """Queue a per-user delayed trigger."""
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
                print(f"[WARN] User not found: {email}")
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
            target=self._delayed_trigger, args=(user_id,), daemon=True
        ).start()

    def _delayed_trigger(self, user_id):
        """Waits for the delay and triggers the Celery task."""
        user_lock = get_user_lock(user_id)

        while True:
            with user_lock:
                status_data = read_status_file()
                user_info = status_data.get(user_id)
                if not user_info:
                    return  # User removed?

                status = user_info.get("status")
                ts_str = user_info.get("timestamp")
                ts = (
                    datetime.fromisoformat(ts_str)
                    if ts_str
                    else datetime.now(timezone.utc)
                )

                # Already started? exit
                if status == "started":
                    return

                # Wait until wait_seconds since last timestamp
                elapsed = (datetime.now(timezone.utc) - ts).total_seconds()
                wait_time = max(0, self.wait_seconds - elapsed)

            # Sleep outside lock
            time.sleep(wait_time)

            # Trigger Celery task
            with user_lock:
                status_data = read_status_file()
                user_info = status_data.get(user_id)
                if not user_info or user_info.get("status") == "started":
                    return

                user_info["status"] = "started"
                user_info["timestamp"] = datetime.now(timezone.utc).isoformat()
                status_data[user_id] = user_info
                write_status_file(status_data)

            print(
                f"[INFO] Triggering umail_sync for user {user_id} ({user_info.get('email')})"
            )
            umail_sync.delay(user_id)

            # Mark complete
            with user_lock:
                status_data = read_status_file()
                user_info = status_data.get(user_id)
                if user_info:
                    user_info["status"] = "complete"
                    user_info["timestamp"] = datetime.now(timezone.utc).isoformat()
                    status_data[user_id] = user_info
                    write_status_file(status_data)
            return
