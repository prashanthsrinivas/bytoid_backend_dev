import pytz
import json
from datetime import datetime, date, time
from celery.schedules import crontab


class RunbookSchedulerService:

    # ========================
    # Timezone Helpers
    # ========================
    @staticmethod
    def to_utc(dt: datetime, timezone: str):
        tz = pytz.timezone(timezone)
        return tz.localize(dt).astimezone(pytz.UTC)

    @staticmethod
    def local_time_to_utc(hour, minute, timezone):
        local_dt = datetime.combine(date.today(), time(hour, minute))
        utc_dt = RunbookSchedulerService.to_utc(local_dt, timezone)
        return utc_dt.hour, utc_dt.minute

    # ========================
    # TASK SELECTOR (IMPORTANT 🔥)
    # ========================
    @staticmethod
    def get_task_and_args(runbook: dict):
        user_id = runbook["user_id"]
        runbook_id = runbook["runbook_id"]

        # 🔥 PLAYBOOK
        if runbook.get("playbook_id"):
            return (
                "tasks.trigger_runbook_from_playbook_task",
                [user_id, runbook["playbook_id"], runbook_id],
            )

        # 🔥 API
        elif runbook.get("api_endpoint"):
            return (
                "tasks.trigger_runbook_from_api_task",
                [user_id, runbook["app_id"], runbook["api_endpoint"], {}],
            )

        # 🔥 LOG SOURCE (optional future)
        elif runbook.get("log_source"):
            return (
                "tasks.trigger_runbook_from_log_task",
                [user_id, runbook["log_source"], runbook_id],
            )

        else:
            raise Exception("No valid runbook trigger source found")

    # ========================
    # ONE-TIME
    # ========================
    @staticmethod
    async def schedule_once(runbook: dict):
        from utils.celery_base import celery

        schedule = json.loads(runbook["schedule"])
        timezone = schedule["timezone"]
        dt = datetime.fromisoformat(schedule["data"]["datetime"])

        dt_utc = RunbookSchedulerService.to_utc(dt, timezone)

        task_name, args = RunbookSchedulerService.get_task_and_args(runbook)

        task = celery.send_task(task_name, args=args, eta=dt_utc)

        return {
            "task_id": task.id,
            "run_at_utc": dt_utc.isoformat()
        }

    # ========================
    # DAILY
    # ========================
    @staticmethod
    async def schedule_daily(runbook: dict):
        from utils.celery_base import celery

        schedule = json.loads(runbook["schedule"])
        timezone = schedule["timezone"]

        hour, minute = map(int, schedule["data"]["startTime"].split(":"))

        utc_hour, utc_minute = RunbookSchedulerService.local_time_to_utc(
            hour, minute, timezone
        )

        task_name, args = RunbookSchedulerService.get_task_and_args(runbook)

        key = f"runbook_daily_{runbook['user_id']}_{runbook['runbook_id']}"

        celery.conf.beat_schedule[key] = {
            "task": task_name,
            "schedule": crontab(hour=utc_hour, minute=utc_minute),
            "args": args,
        }

        return {"entry_name": key}

    # ========================
    # WEEKLY
    # ========================
    @staticmethod
    async def schedule_weekly(runbook: dict):
        from utils.celery_base import celery

        schedule = json.loads(runbook["schedule"])
        timezone = schedule["timezone"]

        hour, minute = map(int, schedule["data"]["startTime"].split(":"))
        weekday = schedule["data"]["weekday"]

        utc_hour, utc_minute = RunbookSchedulerService.local_time_to_utc(
            hour, minute, timezone
        )

        task_name, args = RunbookSchedulerService.get_task_and_args(runbook)

        key = f"runbook_weekly_{runbook['user_id']}_{runbook['runbook_id']}_{weekday}"

        celery.conf.beat_schedule[key] = {
            "task": task_name,
            "schedule": crontab(day_of_week=weekday, hour=utc_hour, minute=utc_minute),
            "args": args,
        }

        return {"entry_name": key}

    # ========================
    # MONTHLY
    # ========================
    @staticmethod
    async def schedule_monthly(runbook: dict):
        from utils.celery_base import celery

        schedule = json.loads(runbook["schedule"])
        timezone = schedule["timezone"]

        hour, minute = map(int, schedule["data"]["startTime"].split(":"))
        day = schedule["data"]["day"]

        utc_hour, utc_minute = RunbookSchedulerService.local_time_to_utc(
            hour, minute, timezone
        )

        task_name, args = RunbookSchedulerService.get_task_and_args(runbook)

        key = f"runbook_monthly_{runbook['user_id']}_{runbook['runbook_id']}_{day}"

        celery.conf.beat_schedule[key] = {
            "task": task_name,
            "schedule": crontab(day_of_month=day, hour=utc_hour, minute=utc_minute),
            "args": args,
        }

        return {"entry_name": key}

    # ========================
    # INTERVAL
    # ========================
    @staticmethod
    async def schedule_interval(runbook: dict, interval_seconds: int):
        from utils.celery_base import celery

        task_name, args = RunbookSchedulerService.get_task_and_args(runbook)

        stop_key = f"runbook:{runbook['runbook_id']}:{runbook['user_id']}:interval"

        task = celery.send_task(
            "tasks.run_runbook_interval",
            args=[task_name, args, interval_seconds, stop_key],
        )

        return {"task_id": task.id, "stop_key": stop_key}

    # ========================
    # MAIN ENTRY (🔥 USE THIS)
    # ========================
    @staticmethod
    async def activate_schedule(runbook: dict):
        schedule = json.loads(runbook["schedule"])
        schedule_type = schedule["type"]

        if schedule_type == "daily":
            return await RunbookSchedulerService.schedule_daily(runbook)

        elif schedule_type == "weekly":
            return await RunbookSchedulerService.schedule_weekly(runbook)

        elif schedule_type == "monthly":
            return await RunbookSchedulerService.schedule_monthly(runbook)

        elif schedule_type == "one_time":
            return await RunbookSchedulerService.schedule_once(runbook)

        elif schedule_type == "interval":
            return await RunbookSchedulerService.schedule_interval(
                runbook, schedule["data"]["interval"]
            )

        else:
            raise Exception("Unsupported schedule type")