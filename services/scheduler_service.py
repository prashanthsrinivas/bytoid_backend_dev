from celery.schedules import crontab
from datetime import datetime
from utils.celery_base import celery
import pytz


class SchedulerService:

    @staticmethod
    def to_utc(dt: datetime, timezone: str):
        """Convert a naive user datetime + timezone → UTC datetime."""
        tz = pytz.timezone(timezone)
        local_dt = tz.localize(dt)
        return local_dt.astimezone(pytz.UTC)

    @staticmethod
    def schedule_one_time(dt, userid, filename, timezone):
        """Schedule a single run job at user's timezone."""
        dt_utc = SchedulerService.to_utc(dt, timezone)

        task = celery.send_task(
            "tasks.workflow_scheduler", args=[userid, filename], eta=dt_utc
        )
        return {"task_id": task.id, "run_at_utc": dt_utc.isoformat()}

    @staticmethod
    def schedule_daily(hour, minute, userid, filename, timezone):
        """Schedule daily jobs according to user timezone."""
        entry_name = f"daily_job_{userid}_{filename}"

        celery.conf.beat_schedule[entry_name] = {
            "task": "tasks.workflow_scheduler",
            "schedule": crontab(hour=hour, minute=minute, tz=timezone),
            "args": (userid, filename),
        }

        return {"status": "scheduled", "timezone": timezone}

    @staticmethod
    def schedule_weekly(weekday, hour, minute, userid, filename, timezone):
        """Schedule weekly jobs with timezone."""
        weekday_map = {
            "mon": 0,
            "tue": 1,
            "wed": 2,
            "thu": 3,
            "fri": 4,
            "sat": 5,
            "sun": 6,
        }

        if weekday not in weekday_map:
            raise ValueError("Invalid weekday")

        entry_name = f"weekly_job_{userid}_{filename}"

        celery.conf.beat_schedule[entry_name] = {
            "task": "tasks.workflow_scheduler",
            "schedule": crontab(
                weekday=weekday_map[weekday], hour=hour, minute=minute, tz=timezone
            ),
            "args": (userid, filename),
        }

        return {"status": "scheduled", "timezone": timezone}

    @staticmethod
    def delete_scheduled_task(task_id):
        try:
            celery.control.revoke(task_id, terminate=False)
            return True
        except Exception as e:
            # print("❌ Error revoking task:", e)
            return False
