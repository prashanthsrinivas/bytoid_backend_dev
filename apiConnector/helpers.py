import asyncio
from datetime import datetime, timedelta
import json
from pkg_resources import normalize_path
import pymysql
import re
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


def _get_effective_auth_config(cur, app_id, userid, app_auth_config):
    """
    Prefer user-scoped auth from external_app_user_config, then legacy table,
    then fallback to app-level auth.
    """
    # 1) New per-user config table
    try:
        cur.execute(
            """
            SELECT auth_config
            FROM external_app_user_config
            WHERE app_id = %s AND user_id = %s
            LIMIT 1
            """,
            (app_id, userid),
        )
        row = cur.fetchone()
        if row and row.get("auth_config"):
            return json.loads(row["auth_config"])
    except Exception:
        pass

    # 2) Legacy per-user auth table (if present)
    try:
        cur.execute(
            """
            SELECT auth_config
            FROM external_app_user_auth
            WHERE app_id = %s AND user_id = %s
            LIMIT 1
            """,
            (app_id, userid),
        )
        row = cur.fetchone()
        if row and row.get("auth_config"):
            return json.loads(row["auth_config"])
    except Exception:
        pass

    # 3) Global fallback
    return json.loads(app_auth_config or "{}")


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
    path_params = json.loads(app["path_params"] or "{}")

    config = {
        "auth": _get_effective_auth_config(cur, app_id, userid, app["auth_config"]),
        "request": {
            "url": app["base_url"].rstrip("/"),
            "method": "GET",
            "headers": headers,
            "query_params": query_params,
            "path_params": path_params,
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


async def _execute_endpoint_internal(
    endpoint_id,
    userid,
    context=None,
    runtime_params=None,
):
    """
    runtime_params can contain:
        {
            "headers": {},
            "query_params": {},
            "path_params": {},
            "body": {},
            "timeout": 20
        }
    """

    runtime_params = runtime_params or {}

    conn = connect_to_rds()
    cur = conn.cursor(pymysql.cursors.DictCursor)

    # ---------------------------
    # 1️⃣ Load endpoint + app
    # ---------------------------
    cur.execute(
        """
        SELECT
            e.*,
            a.base_url,
            a.auth_config,
            a.timeout_seconds as app_timeout,
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
        cur.close()
        conn.close()
        raise ValueError("App endpoint not found")

    # ---------------------------
    # 2️⃣ Parse stored JSON
    # ---------------------------
    try:
        db_headers = json.loads(row["headers"] or "{}")
        db_query_params = json.loads(row["query_params"] or "{}")
        db_path_params = json.loads(row["path_params"] or "{}")
        db_body = json.loads(row["body_template"] or "null")

        auth_config = _get_effective_auth_config(
            cur, row["app_id"], userid, row["auth_config"]
        )

    except json.JSONDecodeError as e:
        cur.close()
        conn.close()
        raise ValueError(f"Invalid JSON in DB: {e}")

    # ---------------------------
    # 3️⃣ Merge runtime overrides
    # ---------------------------
    final_headers = {**db_headers, **runtime_params.get("headers", {})}
    final_query_params = {**db_query_params, **runtime_params.get("query_params", {})}
    final_path_params = {**db_path_params, **runtime_params.get("path_params", {})}

    # Body merging (deep merge optional — simple override here)
    final_body = runtime_params.get("body", db_body)

    # ---------------------------
    # 4️⃣ Replace path variables
    # ---------------------------
    base_url = row["base_url"].rstrip("/")
    path = row["path"]

    matches = re.findall(r"\{(.*?)\}", path)

    for var in matches:
        if var not in final_path_params:
            cur.close()
            conn.close()
            raise ValueError(f"Missing path parameter: {var}")

        path = path.replace(f"{{{var}}}", str(final_path_params[var]))

    full_url = f"{base_url}{path}"

    # ---------------------------
    # 5️⃣ Build final config
    # ---------------------------
    timeout_value = (
        runtime_params.get("timeout")
        or row.get("timeout_seconds")
        or row.get("app_timeout")
        or 10
    )

    config = {
        "auth": auth_config,
        "request": {
            "url": full_url,
            "method": row["method"],
            "headers": final_headers,
            "query_params": final_query_params,
            "body": final_body,
        },
        "timeout": timeout_value,
        "retry": {
            "count": row.get("retry_count") or 1,
            "backoff": row.get("retry_backoff_seconds") or 1,
        },
    }
    # print("congfigs", config)

    # ---------------------------
    # 6️⃣ Execute
    # ---------------------------
    connector = APIConnector(userid=userid, config=config, context=context)
    result = connector.execute()
    # print("result", result)

    # ---------------------------
    # 7️⃣ Log execution
    # ---------------------------
    await save_app_run_to_s3(
        db=conn,
        user_id=userid,
        app_id=row["app_id"],
        endpoint_id=endpoint_id,
        request_cfg=config["request"],
        result=result,
        trigger="manual",
    )

    cur.close()
    conn.close()

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


def extract_user_id(payload=None):
    payload = payload or {}
    return (
        payload.get("user_id")
        or payload.get("userid")
        or request.args.get("user_id")
        or request.args.get("userid")
    )


def normalize_role(value):
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = value.strip().lower()
    return value or None


def get_onboarding_role(cursor, user_id):
    cursor.execute(
        """
        SELECT LineOfBusiness
        FROM business_info
        WHERE user_id_fk = %s
        LIMIT 1
        """,
        (user_id,),
    )
    row = cursor.fetchone() or {}
    return normalize_role(row.get("LineOfBusiness"))


def ensure_dict(value):
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return None
    return None


def merge_json(app_value, endpoint_value):
    app_value = ensure_dict(app_value)
    endpoint_value = ensure_dict(endpoint_value)

    if app_value is None and endpoint_value is None:
        return None
    if app_value is None:
        return endpoint_value
    if endpoint_value is None:
        return app_value

    return {**app_value, **endpoint_value}


def normalize_row_dynamic(row: dict):
    """
    Dynamically normalizes any JSON-like string values in a DB row.
    Does NOT assume field names.
    Does NOT invent values.
    """
    if not isinstance(row, dict):
        return row

    normalized = {}

    for key, value in row.items():
        # Already valid JSON
        if isinstance(value, dict) or isinstance(value, list):
            normalized[key] = value
            continue

        # Try parsing JSON strings
        if isinstance(value, str):
            value_str = value.strip()
            if (value_str.startswith("{") and value_str.endswith("}")) or (
                value_str.startswith("[") and value_str.endswith("]")
            ):
                try:
                    normalized[key] = json.loads(value_str)
                    continue
                except Exception:
                    pass  # fall through safely

        # Leave everything else untouched
        normalized[key] = value

    return normalized


# ==========================================================
# 🔹 Helper: Build URL With Optional Path Params
# ==========================================================
def build_full_url(base_url, path, path_params=None):
    base_url = base_url.rstrip("/")
    path = normalize_path(path)
    path_params = path_params or {}

    # Support {id}
    curly_matches = re.findall(r"\{(.*?)\}", path)
    for var in curly_matches:
        if var not in path_params:
            raise ValueError(f"Missing path parameter: {var}")
        path = path.replace(f"{{{var}}}", str(path_params[var]))

    # Support :id
    colon_matches = re.findall(r":(\w+)", path)
    for var in colon_matches:
        if var not in path_params:
            raise ValueError(f"Missing path parameter: {var}")
        path = path.replace(f":{var}", str(path_params[var]))

    if path_params and not (curly_matches or colon_matches):
        raise ValueError("Path parameters provided but no placeholders found in path")

    return f"{base_url}{path}"
