import threading
from collections import defaultdict

# Stores task status: running/completed/failed
task_status = defaultdict(dict)
task_threads = {}

def run_background_task(userid, industry, func):
    if userid in task_threads and task_threads[userid].is_alive():
        return {"message": "Task already running for this user."}

    def task_wrapper():
        try:
            task_status[userid]["status"] = "running"
            func(userid=userid, industry=industry)
            task_status[userid]["status"] = "completed"
        except Exception as e:
            task_status[userid]["status"] = "failed"
            task_status[userid]["error"] = str(e)

    thread = threading.Thread(target=task_wrapper, daemon=True)
    task_threads[userid] = thread
    thread.start()
    return {"message": "Task started."}
