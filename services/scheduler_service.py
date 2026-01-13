from celery.schedules import crontab
from datetime import date, datetime, timedelta, time
from services.redis_service import RedisService
from utils.celery_base import celery
import pytz
import uuid


class SchedulerService:
    redis_service = RedisService()  # your existing async Redis wrapper

    @staticmethod
    def to_utc(dt: datetime, timezone: str):
        """Convert a naive user datetime + timezone → UTC datetime."""
        tz = pytz.timezone(timezone)
        local_dt = tz.localize(dt)
        return local_dt.astimezone(pytz.UTC)

    @staticmethod
    async def schedule_custom(
        *,
        start_date: str,
        start_time: str,
        userid: str,
        filename: str,
        timezone: str,
        contacts: list,
    ):
        local_dt = datetime.fromisoformat(f"{start_date}T{start_time}")
        dt_utc = SchedulerService.to_utc(local_dt, timezone)
        run_iso = dt_utc.isoformat()
        key = f"scheduled:{userid}:{filename}:all"
        hexid = uuid.uuid4()
        uniquekey = f"{filename}_{hexid}"
        print("making task for custom", dt_utc)
        # existing_task = await SchedulerService.redis_service.get(key)
        # if existing_task:
        #     return {
        #         "task_id": existing_task,
        #         "run_at_utc": run_iso,
        #         "status": "already_scheduled",
        #     }

        task = celery.send_task(
            "tasks.workflow_scheduler",
            args=[userid, filename, contacts, uniquekey],
            eta=dt_utc,
        )
        # await SchedulerService.redis_service.set(key, task.id, ex=86400)
        print("task made on custom", task)
        return {
            "task_id": task.id,
            "run_at_utc": run_iso,
            "status": "scheduled",
            "uniquekey": uniquekey,
        }

    @staticmethod
    async def schedule_one_time(dt, userid, filename, timezone, contacts):
        dt_utc = SchedulerService.to_utc(dt, timezone)
        run_iso = dt_utc.isoformat()
        key = f"scheduled:{userid}:{filename}:all"
        print("making a one time schedule", dt_utc)
        hexid = uuid.uuid4()
        uniquekey = f"{filename}_{hexid}"

        # existing_task = await SchedulerService.redis_service.get(key)
        # if existing_task:
        #     return {
        #         "task_id": existing_task,
        #         "run_at_utc": run_iso,
        #         "status": "already_scheduled",
        #     }

        task = celery.send_task(
            "tasks.workflow_scheduler",
            args=[userid, filename, contacts, uniquekey],
            eta=dt_utc,
        )
        # await SchedulerService.redis_service.set(key, task.id, ex=86400)
        print("task made for one time", task)
        return {
            "task_id": task.id,
            "run_at_utc": run_iso,
            "status": "scheduled",
            "uniquekey": uniquekey,
        }

    # ---------------------- SINGLE STEP ------------------------
    @staticmethod
    async def schedule_single_step(
        *,
        run_at: datetime,
        userid: str,
        filename: str,
        stepid: int,
        timezone: str,
    ):
        dt_utc = SchedulerService.to_utc(run_at, timezone)
        run_iso = dt_utc.isoformat()
        key = f"scheduled:{userid}:{filename}:{stepid}"
        print("making sinfle task schedule", dt_utc)
        hexid = uuid.uuid4()
        uniquekey = f"{filename}_{hexid}"

        # existing_task = await SchedulerService.redis_service.get(key)
        # if existing_task:
        #     return {
        #         "task_id": existing_task,
        #         "run_at_utc": run_iso,
        #         "status": "already_scheduled",
        #     }

        task = celery.send_task(
            "tasks.workflow_schedule_single",
            args=[userid, filename, stepid, uniquekey],
            eta=dt_utc,
        )
        # await SchedulerService.redis_service.set(key, task.id, ex=86400)
        print("task made for single", task)
        return {
            "task_id": task.id,
            "run_at_utc": run_iso,
            "status": "scheduled",
            "uniquekey": uniquekey,
        }

    # ---------------------- DAILY ------------------------
    @staticmethod
    async def schedule_daily(hour, minute, userid, filename, timezone, contacts):
        entry_name = f"daily_job_{userid}_{filename}"

        # Check Redis if already scheduled
        key = f"scheduled:{userid}:{filename}:all"
        # existing_task = await SchedulerService.redis_service.get(key)
        print("making task for daily")
        hexid = uuid.uuid4()
        uniquekey = f"{filename}_{hexid}"
        # if existing_task:
        #     return {
        #         "status": "already_scheduled",
        #         "entry_name": entry_name,
        #         "timezone": timezone,
        #     }

        task = celery.conf.beat_schedule[entry_name] = {
            "task": "tasks.workflow_scheduler",
            "schedule": crontab(hour=hour, minute=minute, tz=timezone),
            "args": (userid, filename, contacts, uniquekey),
        }

        # Mark in Redis (just a placeholder, TTL optional)
        # await SchedulerService.redis_service.set(key, entry_name, ex=86400 * 365)
        print("task made for daily", task)
        return {
            "status": "scheduled",
            "entry_name": entry_name,
            "timezone": timezone,
            "uniquekey": uniquekey,
        }

    # ---------------------- WEEKLY ------------------------
    @staticmethod
    async def schedule_weekly(
        weekday, hour, minute, userid, filename, timezone, contacts
    ):
        weekday_map = {
            "mon": 0,
            "monday": 0,
            "tue": 1,
            "tuesday": 1,
            "wed": 2,
            "wednesday": 2,
            "thu": 3,
            "thursday": 3,
            "fri": 4,
            "friday": 4,
            "sat": 5,
            "saturday": 5,
            "sun": 6,
            "sunday": 6,
        }

        weekday_lower = weekday.lower()
        if weekday_lower not in weekday_map:
            raise ValueError("Invalid weekday")

        entry_name = f"weekly_job_{userid}_{filename}"
        key = f"scheduled:{userid}:{filename}:all"
        hexid = uuid.uuid4()
        uniquekey = f"{filename}_{hexid}"

        # existing_task = await SchedulerService.redis_service.get(key)
        # if existing_task:
        #     return {
        #         "status": "already_scheduled",
        #         "entry_name": entry_name,
        #         "timezone": timezone,
        #     }

        celery.conf.beat_schedule[entry_name] = {
            "task": "tasks.workflow_scheduler",
            "schedule": crontab(
                weekday=weekday_map[weekday_lower],
                hour=hour,
                minute=minute,
                tz=timezone,
            ),
            "args": (userid, filename, contacts, uniquekey),
        }

        # await SchedulerService.redis_service.set(key, entry_name, ex=86400 * 365)
        return {
            "status": "scheduled",
            "entry_name": entry_name,
            "timezone": timezone,
            "uniquekey": uniquekey,
        }

    @staticmethod
    def delete_scheduled_task(task_id):
        try:
            celery.control.revoke(task_id, terminate=False)
            return True
        except Exception as e:
            # print("❌ Error revoking task:", e)
            return False

    @staticmethod
    def preview_next_custom_time(
        *,
        start_date: str,
        end_date: str,
        start_time: str,
        end_time: str,
        timezone: str,
    ):
        tz = pytz.timezone(timezone)
        now = datetime.now(tz)

        start_d = date.fromisoformat(start_date)
        end_d = date.fromisoformat(end_date)

        start_t = time.fromisoformat(start_time)
        end_t = time.fromisoformat(end_time)

        today = now.date()

        # ❌ Outside date window
        if today > end_d:
            return None

        # Case 1: today < startDate → schedule at startDate + startTime
        if today < start_d:
            return tz.localize(datetime.combine(start_d, start_t))

        # Case 2: today within date window
        today_start = tz.localize(datetime.combine(today, start_t))
        today_end = tz.localize(datetime.combine(today, end_t))

        # Now inside time window → run now
        if today_start <= now <= today_end:
            return now

        # Before today's window → schedule at today's startTime
        if now < today_start:
            return today_start

        # After today's window → move to next day
        next_day = today + timedelta(days=1)
        if next_day > end_d:
            return None

        return tz.localize(datetime.combine(next_day, start_t))

    @staticmethod
    def preview_next_daily_time(hour, minute, timezone):
        tz = pytz.timezone(timezone)
        now = datetime.now(tz)

        next_run = now.replace(
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0,
        )

        if next_run <= now:
            next_run += timedelta(days=1)

        return next_run.isoformat()

    @staticmethod
    def preview_next_weekly_time(weekday, hour, minute, timezone):
        weekday_map = {
            "mon": 0,
            "monday": 0,
            "tue": 1,
            "tuesday": 1,
            "wed": 2,
            "wednesday": 2,
            "thu": 3,
            "thursday": 3,
            "fri": 4,
            "friday": 4,
            "sat": 5,
            "saturday": 5,
            "sun": 6,
            "sunday": 6,
        }

        weekday = weekday.lower()
        if weekday not in weekday_map:
            raise ValueError("Invalid weekday")

        tz = pytz.timezone(timezone)
        now = datetime.now(tz)

        days_ahead = (weekday_map[weekday] - now.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7

        next_run = (now + timedelta(days=days_ahead)).replace(
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0,
        )

        return next_run.isoformat()


# sc = SchedulerService()
# print("task remoded",sc.delete_scheduled_task(task_id="f4439e13-47d4-4f9a-962a-dd99b44bfb91"))
# print("task remoded",sc.delete_scheduled_task(task_id="2c4dc220-bce4-4a31-93da-c3f8048b6528"))
