
from apscheduler.schedulers.background import BackgroundScheduler

scheduler = BackgroundScheduler(timezone="Asia/Kolkata")
scheduler.start()

#print("Scheduler started globally")

# scheduler.add_job(
#     lambda: print("🔥 TEST JOB RUNNING"),
#     'interval',
#     seconds=10
# )