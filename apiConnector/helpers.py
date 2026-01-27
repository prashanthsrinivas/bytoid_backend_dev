import asyncio
from datetime import datetime, timedelta
import json
import pymysql

from db.rds_db import connect_to_rds
from services.apiconnectors import APIConnector
from utils.s3_utils import save_app_runbase_S3
from agent_route.lance_agent import LanceClient
from credits_route.route import Credits


async def save_app_run_to_s3(
    *, db, user_id, app_id, endpoint_id, request_cfg, result, trigger
):
    now = datetime.utcnow()
    minute_bucket = now.strftime("%Y-%m-%d-%H-%M")

    if endpoint_id:
        key = f"{user_id}/apiconnectors/{app_id}/{endpoint_id}/{minute_bucket}.json"
    else:
        key = f"{user_id}/apiconnectors/{app_id}/{minute_bucket}.json"

    record = {
        "ts": now.isoformat() + "Z",
        "trigger": trigger,
        "request": request_cfg,
        "response": result,
    }

    val = save_app_runbase_S3(record=record, key=key)
    if val:
        credits = Credits(db=db)
        lanceClient = LanceClient(user_id=user_id, credits=credits)
        await lanceClient.save_app_run(
            user_id=user_id,
            app_id=app_id,
            endpoint_id=endpoint_id,
            request_cfg=request_cfg,
            result=result,
            trigger="manual",
            minute_bucket=minute_bucket,
        )

    return val


def _execute_app_internal(app_id, userid):
    conn = connect_to_rds()
    cur = conn.cursor(pymysql.cursors.DictCursor)

    cur.execute(
        "SELECT * FROM external_apps WHERE id=%s",
        (app_id,),
    )

    app = cur.fetchone()
    if not app:
        raise ValueError("App not found")

    headers = json.loads(app["headers"] or "{}")
    query_params = json.loads(app["query_params"] or "{}")

    config = {
        "auth": json.loads(app["auth_config"]),
        "request": {
            "url": app["base_url"].rstrip("/"),
            "method": "GET",
            "headers": headers,
            "query_params": query_params,
            "body": None,
        },
        "timeout": app.get("timeout_seconds") or 10,
        "retry": {
            "count": app.get("retry_count") or 1,
            "backoff": app.get("retry_backoff_seconds") or 1,
        },
    }

    connector = APIConnector(userid=userid, config=config)
    result = connector.execute()

    save_app_run_to_s3(
        user_id=userid,
        app_id=app_id,
        endpoint_id=None,
        request_cfg=config,
        result=result,
        trigger="schedule",
    )

    cur.close()
    conn.close()

    return result


async def _execute_endpoint_internal(endpoint_id, userid, context=None):
    conn = connect_to_rds()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    # Load endpoint + app
    cur.execute(
        """
        SELECT
            e.*,
            a.base_url,
            a.auth_config,
            a.timeout_seconds,
            a.retry_count,
            a.retry_backoff_seconds
        FROM external_app_endpoints e
        JOIN external_apps a ON a.id = e.app_id
        WHERE e.id = %s AND e.is_active = 1
        """,
        (endpoint_id,),
    )

    row = cur.fetchone()
    if not row:
        return ValueError("App endpoint not Found")

    # Build request ONLY from DB
    base_url = row["base_url"].rstrip("/")
    full_url = f"{base_url}{row['path']}"

    try:
        headers = json.loads(row["headers"] or "{}")
        query_params = json.loads(row["query_params"] or "{}")
        body = json.loads(row["body_template"] or "null")
        auth_config = json.loads(row["auth_config"] or "{}")
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in DB: {e}")

    config = {
        "auth": auth_config,
        "request": {
            "url": full_url,
            "method": row["method"],
            "headers": headers,
            "query_params": query_params,
            "body": body,
        },
        "timeout": row.get("timeout_seconds") or 10,
        "retry": {
            "count": row.get("retry_count") or 1,
            "backoff": row.get("retry_backoff_seconds") or 1,
        },
    }

    connector = APIConnector(userid=userid, config=config, context=context)
    result = connector.execute()

    # Save execution log to S3 (minute-bucketed)
    await save_app_run_to_s3(
        db=conn,
        user_id=userid,
        app_id=row["app_id"],
        endpoint_id=endpoint_id,
        request_cfg=config["request"],  # Only request, not full config
        result=result,
        trigger="manual",
    )

    cur.close()
    conn.close()
    print("res done", result.get("status_code"))
    return result


def resolve_schedule_from_activation(scheduled):
    if not scheduled:
        raise ValueError("scheduledActivation missing")

    frequency = scheduled.get("frequency")
    if not frequency:
        raise ValueError("frequency missing")

    frequency = frequency.lower()
    timezone = scheduled.get("timezone", "UTC")

    # -------------------------
    # DAILY
    # -------------------------
    if frequency == "daily":
        return "daily", {
            "startTime": scheduled["startTime"],
            "timezone": timezone,
        }

    # -------------------------
    # WEEKLY
    # -------------------------
    if frequency == "weekly":
        return "weekly", {
            "weekday": scheduled["weeklyDay"],
            "startTime": scheduled["startTime"],
            "timezone": timezone,
        }

    # -------------------------
    # MONTHLY
    # -------------------------
    if frequency == "monthly":
        return "monthly", {
            "day": scheduled["dayOfMonth"],
            "startTime": scheduled["startTime"],
            "timezone": timezone,
        }

    # -------------------------
    # EVERY N SECONDS / MINUTES
    # -------------------------
    if frequency == "interval":
        if "intervalMinutes" in scheduled:
            minute_value = scheduled["intervalMinutes"]
            return "interval", {"seconds": int(minute_value) * 60}
        return "interval", {
            "seconds": int(scheduled["seconds"]),
        }

    # -------------------------
    # ONE-TIME / ONCE
    # -------------------------
    if frequency in ("one_time", "once"):
        start_date = scheduled["startDate"]
        start_time = scheduled["startTime"]

        return "one_time", {
            "datetime": f"{start_date}T{start_time}",
            "timezone": timezone,
        }

    # -------------------------
    # CUSTOM RANGE (date range)
    # -------------------------
    if frequency == "custom":
        return "custom", {
            "startDate": scheduled["startDate"],
            "endDate": scheduled["endDate"],
            "startTime": scheduled["startTime"],
            "intervalMinutes": int(scheduled.get("intervalMinutes", 60)),
            "timezone": timezone,
        }

    raise ValueError(f"Unsupported frequency: {frequency}")


def expand_custom_dates(start_date, end_date, start_time):
    """
    Custom schedule = run once per day at start_time
    between start_date and end_date (inclusive).
    intervalMinutes is intentionally ignored.
    """

    start_dt = datetime.fromisoformat(f"{start_date}T{start_time}")
    end_dt = datetime.fromisoformat(f"{end_date}T{start_time}")

    dates = []
    cur = start_dt

    while cur <= end_dt:
        dates.append(cur.isoformat())
        cur += timedelta(days=1)

    return dates


def save_endpoint_schedule(endpoint_id, schedule_payload):
    conn = connect_to_rds()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE external_app_endpoints
        SET schedules = %s,
            updated_at = NOW()
        WHERE id = %s
        """,
        (json.dumps(schedule_payload), endpoint_id),
    )

    conn.commit()
    cur.close()
    conn.close()


def save_app_schedule(endpoint_id, schedule_payload):
    conn = connect_to_rds()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE external_apps
        SET schedules = %s,
            updated_at = NOW()
        WHERE id = %s
        """,
        (json.dumps(schedule_payload), endpoint_id),
    )

    conn.commit()
    cur.close()
    conn.close()


def save_app_schedule(endpoint_id, schedule_payload):
    conn = connect_to_rds()
    cur = conn.cursor()

    cur.execute("SELECT schedules FROM external_apps WHERE id = %s", (endpoint_id,))
    row = cur.fetchone()

    existing = []
    if row and row[0]:
        existing = json.loads(row[0])

    existing.append(schedule_payload)

    cur.execute(
        """
        UPDATE external_app_endpoints
        SET schedules = %s, updated_at = NOW()
        WHERE id = %s
        """,
        (json.dumps(existing), endpoint_id),
    )

    conn.commit()


def get_schedule_endpointdetails(endpoint_id):
    conn = connect_to_rds()
    cur = conn.cursor()

    cur.execute(
        "SELECT schedules FROM external_app_endpoints WHERE id = %s", (endpoint_id,)
    )
    row = cur.fetchone()
    conn.close()
    if not row or not row[0]:
        return None

    # row[0] is JSON string
    return json.loads(row[0])


def completed_endpoint_schedule(endpoint_id):
    conn = connect_to_rds()
    cur = conn.cursor()

    cur.execute(
        "SELECT schedules FROM external_app_endpoints WHERE id = %s", (endpoint_id,)
    )
    row = cur.fetchone()

    existing = []
    if row and row[0]:
        existing = json.loads(row[0])

    if existing and row["frequency"] == "one_time":
        cur.execute(
            """
            UPDATE external_app_endpoints
            SET schedules = NULL;
            """
        )

        conn.commit()


def is_schedule_active(endpoint_id):
    conn = connect_to_rds()
    cur = conn.cursor()

    cur.execute(
        "SELECT schedules FROM external_app_endpoints WHERE id=%s",
        (endpoint_id,),
    )
    row = cur.fetchone()

    cur.close()
    conn.close()

    if not row or not row[0]:
        return False

    schedule = json.loads(row[0])
    return schedule.get("status") == "active"


def mark_schedules_inactive(endpoint_id):
    """
    Marks all schedules for a given endpoint as inactive.
    Returns True if any schedule was updated, False otherwise.
    """
    conn = connect_to_rds()
    cur = conn.cursor()

    cur.execute(
        "SELECT schedules FROM external_app_endpoints WHERE id=%s",
        (endpoint_id,),
    )
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return False

    schedules = json.loads(row[0] or "[]")
    updated = False

    for sch in schedules:
        if sch.get("status") != "inactive":
            sch["status"] = "inactive"
            updated = True

    if updated:
        cur.execute(
            "UPDATE external_app_endpoints SET schedules=%s, updated_at=NOW() WHERE id=%s",
            (json.dumps(schedules), endpoint_id),
        )
        conn.commit()

    cur.close()
    conn.close()
    return updated


def is_schedule_app_active(endpoint_id):
    conn = connect_to_rds()
    cur = conn.cursor()

    cur.execute(
        "SELECT schedules FROM external_apps WHERE id=%s",
        (endpoint_id,),
    )
    row = cur.fetchone()

    cur.close()
    conn.close()

    if not row or not row[0]:
        return False

    schedule = json.loads(row[0])
    return schedule.get("status") == "active"
