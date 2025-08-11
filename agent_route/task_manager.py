import threading
import uuid
from collections import defaultdict

# Stores task status: running/completed/failed
task_status = defaultdict(dict)
task_threads = {}


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
