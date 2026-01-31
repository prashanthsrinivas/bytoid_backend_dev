from datetime import datetime
import json
import pymysql

from apiConnector.helpers import (
    _execute_app_internal,
    _execute_endpoint_internal,
    expand_custom_dates,
    get_schedule_endpointdetails,
    resolve_schedule_from_activation,
    save_endpoint_schedule,
)
from db.rds_db import connect_to_rds
from flask import request, jsonify, Blueprint
from services.apiconnectors import APIConnector
from services.scheduler_service import APIConnectorScheduler
from utils.s3_utils import get_filedata_endp, getallendpointdetails

apiconnector_bp = Blueprint("apiconnector", __name__, url_prefix="/apiconnector/apps")


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


@apiconnector_bp.route("/test", methods=["POST"])
def test_online_external_link():
    try:
        data = request.get_json(force=True)

        base_url = data["base_url"].rstrip("/")
        userid = data["user_id"]
        if not userid:
            return jsonify({"message": "userid required"}), 400

        auth = data.get("auth", {})

        test_req = data.get("test_request", {})
        path = test_req.get("path", "/")
        method = test_req.get("method", "GET")
        headers = test_req.get("headers", {})
        query_params = test_req.get("query_params", {})
        body = test_req.get("body")

        full_url = f"{base_url}{path}"

        config = {
            "auth": auth,
            "request": {
                "url": full_url,
                "method": method,
                "headers": headers,
                "query_params": query_params,
                "body": body,
            },
            "retry": {"count": 1, "backoff": 1},
            "timeout": 10,
        }

        connector = APIConnector(userid=userid, config=config)
        result = connector.execute()

        return jsonify(result)

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@apiconnector_bp.route("", methods=["POST"])
def create_external_app():
    conn = None
    cur = None

    try:
        data = request.get_json(force=True)
       #print("data from frontend", data)

        # ------------------------
        # Required fields
        # ------------------------
        user_id = data["user_id"]
        app_name = data["app_name"]
        provider = data.get("provider", "custom")
        base_url = data["base_url"].rstrip("/")
        auth = data.get("auth") or {"type": "none"}

        # ------------------------
        # Universal request normalizer
        # Supports BOTH:
        #  - flat payload
        #  - test_request payload
        # ------------------------
        test_req = data.get("test_request") or {
            "path": data.get("path", "/"),
            "method": data.get("method", "GET"),
            "headers": data.get("headers"),
            "query_params": data.get("query_params"),
        }

        path = test_req.get("path") or "/"
        method = test_req.get("method") or "GET"
        headers = test_req.get("headers") or {}
        query_params = test_req.get("query_params") or {}

        # ------------------------
        # DB connection
        # ------------------------
        conn = connect_to_rds()
        cur = conn.cursor(pymysql.cursors.DictCursor)

        # ------------------------
        # Duplicate app check
        # ------------------------
        cur.execute(
            """
            SELECT id FROM external_apps
            WHERE user_id=%s AND app_name=%s
            """,
            (user_id, app_name),
        )

        if cur.fetchone():
            return jsonify({"success": False, "error": "App name already exists"}), 409

        # ------------------------
        # Test API Connection
        # ------------------------
        connector = APIConnector(
            userid=user_id,
            config={
                "auth": auth,
                "request": {
                    "url": f"{base_url}{path}",
                    "method": method,
                    "headers": headers,
                    "query_params": query_params,
                },
                "timeout": data.get("timeout_seconds", 10),
                "retry": {"count": 2, "backoff": 1},
            },
        )

        result = connector.execute()

        if not result.get("success"):
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "Connection test failed",
                        "details": result,
                    }
                ),
                400,
            )

        # ------------------------
        # Insert App
        # ------------------------
        cur.execute(
            """
            INSERT INTO external_apps
            (
                user_id,
                app_name,
                provider,
                base_url,
                headers,
                method,
                query_params,
                auth_type,
                auth_config
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                user_id,
                app_name,
                provider,
                base_url,
                json.dumps(headers),  # stored as JSON
                method,
                json.dumps(query_params),  # stored as JSON
                auth.get("type", "none"),
                json.dumps(auth),
            ),
        )

        conn.commit()

        return jsonify(
            {
                "success": True,
                "message": "App registered successfully!",
                "tested_endpoint": {
                    "url": f"{base_url}{path}",
                    "method": method,
                    "headers": headers,
                    "query_params": query_params,
                },
            }
        )

    except Exception as e:
       #print("error", e)
        if conn:
            conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 500

    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@apiconnector_bp.route("/<int:app_id>", methods=["PUT"])
def update_external_app(app_id):
    conn = None
    cur = None

    try:
        data = request.get_json(force=True)
       #print("update payload", data)

        user_id = data["user_id"]

        # ----------------------------
        # DB connect
        # ----------------------------
        conn = connect_to_rds()
        cur = conn.cursor(pymysql.cursors.DictCursor)

        # ----------------------------
        # Ownership check
        # ----------------------------
        cur.execute(
            "SELECT * FROM external_apps WHERE id=%s AND user_id=%s",
            (app_id, user_id),
        )
        app = cur.fetchone()
        if not app:
            return jsonify({"success": False, "error": "App not found"}), 404

        # ----------------------------
        # Duplicate name check
        # ----------------------------
        if "app_name" in data:
            cur.execute(
                """
                SELECT id FROM external_apps
                WHERE user_id=%s AND app_name=%s AND id!=%s
                """,
                (user_id, data["app_name"], app_id),
            )
            if cur.fetchone():
                return (
                    jsonify({"success": False, "error": "App name already exists"}),
                    409,
                )

        # ----------------------------
        # Universal request normalizer
        # ----------------------------
        test_req = data.get("test_request") or {
            "path": data.get("path"),
            "method": data.get("method"),
            "headers": data.get("headers"),
            "query_params": data.get("query_params"),
        }

        path = test_req.get("path") or "/"
        method = test_req.get("method") or "GET"
        headers = test_req.get("headers") or {}
        query_params = test_req.get("query_params") or {}

        # Use existing base_url if not updated
        base_url = data.get("base_url", app["base_url"]).rstrip("/")

        # Use existing auth if not updated
        auth = data.get("auth") or json.loads(app["auth_config"])

        # ----------------------------
        # Test updated connection
        # ----------------------------
        connector = APIConnector(
            userid=user_id,
            config={
                "auth": auth,
                "request": {
                    "url": f"{base_url}{path}",
                    "method": method,
                    "headers": headers,
                    "query_params": query_params,
                },
                "timeout": data.get("timeout_seconds", 10),
                "retry": {"count": 2, "backoff": 1},
            },
        )

        result = connector.execute()

        if not result.get("success"):
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "Connection test failed",
                        "details": result,
                    }
                ),
                400,
            )

        # ----------------------------
        # Build UPDATE
        # ----------------------------
        update_fields = []
        params = []

        if "app_name" in data:
            update_fields.append("app_name=%s")
            params.append(data["app_name"])

        if "method" in data:
            update_fields.append("method=%s")
            params.append(method)

        if "provider" in data:
            update_fields.append("provider=%s")
            params.append(data["provider"])

        if "base_url" in data:
            update_fields.append("base_url=%s")
            params.append(base_url)

        if "auth" in data:
            update_fields.append("auth_type=%s")
            update_fields.append("auth_config=%s")
            params.append(auth.get("type", "none"))
            params.append(json.dumps(auth))

        if "headers" in data or "test_request" in data:
            update_fields.append("headers=%s")
            params.append(json.dumps(headers))

        if "query_params" in data or "test_request" in data:
            update_fields.append("query_params=%s")
            params.append(json.dumps(query_params))

        if not update_fields:
            return jsonify({"success": True, "message": "Nothing to update"})

        update_fields.append("updated_at=NOW()")
        params.append(app_id)

        cur.execute(
            f"""
            UPDATE external_apps
            SET {', '.join(update_fields)}
            WHERE id=%s
            """,
            tuple(params),
        )

        conn.commit()

        return jsonify(
            {
                "success": True,
                "message": "App updated and verified",
                "tested_endpoint": {
                    "url": f"{base_url}{path}",
                    "method": method,
                    "headers": headers,
                    "query_params": query_params,
                },
            }
        )

    except Exception as e:
       #print("update error", e)
        if conn:
            conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 500

    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@apiconnector_bp.route("/<int:app_id>", methods=["DELETE"])
def delete_external_app(app_id):
    conn = connect_to_rds()
    cur = conn.cursor()

    try:
        # # Disable app
        # cur.execute(
        #     """
        #     UPDATE external_apps
        #     SET status = 'inactive'
        #     WHERE id = %s
        # """,
        #     (app_id,),
        # )

        # # Disable all endpoints
        # cur.execute(
        #     """
        #     UPDATE external_app_endpoints
        #     SET is_active = 0
        #     WHERE app_id = %s
        # """,
        #     (app_id,),
        # )
        cur.execute(
            """
            DELETE FROM external_apps
            WHERE id = %s
        """,
            (app_id,),
        )

        conn.commit()

        return jsonify(
            {"success": True, "message": "App and all related data permanently deleted"}
        )

    except Exception as e:
        conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 500

    finally:
        cur.close()
        conn.close()


@apiconnector_bp.route("/<int:app_id>/hard-delete", methods=["DELETE"])
def hard_delete_external_app(app_id):
    conn = connect_to_rds()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            DELETE FROM external_apps
            WHERE id = %s
        """,
            (app_id,),
        )

        conn.commit()

        return jsonify(
            {"success": True, "message": "App and all related data permanently deleted"}
        )

    except Exception as e:
        conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 500

    finally:
        cur.close()
        conn.close()


@apiconnector_bp.route("/<user_id>", methods=["GET"])
def list_external_apps(user_id):
    conn = connect_to_rds()
    cur = conn.cursor(pymysql.cursors.DictCursor)

    cur.execute(
        """
        SELECT *
        FROM external_apps
        WHERE user_id = %s
    """,
        (user_id,),
    )

    apps = cur.fetchall()
    app = [normalize_row_dynamic(app) for app in apps]

    # print("all apps", app)
    cur.close()
    conn.close()

    return jsonify({"success": True, "apps": app})


@apiconnector_bp.route("/<int:app_id>/endpoints", methods=["POST"])
def create_endpoint(app_id):
    data = request.get_json(force=True)

    conn = connect_to_rds()
    cur = conn.cursor(pymysql.cursors.DictCursor)

    try:
        # 1️⃣ Load parent app
        cur.execute(
            """
            SELECT headers, query_params, timeout_seconds
            FROM external_apps
            WHERE id=%s AND status='active'
            """,
            (app_id,),
        )
        app = cur.fetchone()
        if not app:
            return jsonify({"success": False, "error": "App not found"}), 404

        # 2️⃣ Merge defaults
        headers = merge_json(app["headers"], data.get("headers"))

        query_params = merge_json(app["query_params"], data.get("query_params"))

        timeout_seconds = data.get("timeout_seconds") or app["timeout_seconds"]

        method = data.get("method", "GET").upper()

        # 3️⃣ Prevent duplicates
        cur.execute(
            """
            SELECT id FROM external_app_endpoints
            WHERE app_id=%s AND path=%s AND method=%s
            """,
            (app_id, data["path"], method),
        )
        if cur.fetchone():
            return jsonify({"success": False, "error": "Endpoint already exists"}), 409

        # 4️⃣ Insert endpoint
        cur.execute(
            """
            INSERT INTO external_app_endpoints
            (
                app_id,
                name,
                path,
                method,
                headers,
                query_params,
                body_template,
                timeout_seconds
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                app_id,
                data["name"],
                data["path"],
                method,
                json.dumps(headers),
                json.dumps(query_params),
                json.dumps(data.get("body_template")),
                timeout_seconds,
            ),
        )

        conn.commit()

        return jsonify(
            {
                "success": True,
                "message": "Endpoint created",
            }
        )

    except Exception as e:
       #print("err", e)
        conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 500

    finally:
        cur.close()
        conn.close()


@apiconnector_bp.route("/endpoints/<int:endpoint_id>", methods=["PUT"])
def update_endpoint(endpoint_id):
    data = request.get_json(force=True)
    conn = connect_to_rds()
    cur = conn.cursor()

    try:
        update_fields = []
        params = []

        for field in ["name", "path", "method", "timeout_seconds"]:
            if field in data:
                update_fields.append(f"{field}=%s")
                params.append(data[field])

        for field in ["headers", "query_params", "body_template"]:
            if field in data:
                update_fields.append(f"{field}=%s")
                params.append(json.dumps(data[field]))

        if not update_fields:
            return jsonify({"success": True, "message": "Nothing to update"})

        params.append(endpoint_id)
        cur.execute(
            f"UPDATE external_app_endpoints SET {', '.join(update_fields)} WHERE id=%s",
            tuple(params),
        )

        conn.commit()
        return jsonify({"success": True, "message": "Endpoint updated"})

    except Exception as e:
        conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        cur.close()
        conn.close()


@apiconnector_bp.route("/<int:app_id>/endpoints", methods=["GET"])
def list_endpoints(app_id):
    conn = connect_to_rds()
    cur = conn.cursor(pymysql.cursors.DictCursor)

    cur.execute(
        """
        SELECT *
        FROM external_app_endpoints
        WHERE app_id=%s
        """,
        (app_id,),
    )

    endpoints = cur.fetchall()
    app = [normalize_row_dynamic(app) for app in endpoints]
    cur.close()
    conn.close()

    return jsonify({"success": True, "endpoints": app})


@apiconnector_bp.route("/endpoints/<int:endpoint_id>/test", methods=["POST"])
def test_endpoint(endpoint_id, userid=None):
    conn = connect_to_rds()
    cur = conn.cursor(pymysql.cursors.DictCursor)

    cur.execute(
        """
        SELECT e.*, a.base_url, a.auth_config
        FROM external_app_endpoints e
        JOIN external_apps a ON a.id = e.app_id
        WHERE e.id = %s
    """,
        (endpoint_id,),
    )
    if not userid:
        data = request.get_json(force=True)
        userid = data.get("user_id")
    row = cur.fetchone()
    if not row:
        return jsonify({"success": False, "error": "Endpoint not found"}), 404

    config = {
        "auth": json.loads(row["auth_config"]),
        "request": {
            "url": row["base_url"] + row["path"],
            "method": row["method"],
            "headers": json.loads(row["headers"] or "{}"),
            "query_params": json.loads(row["query_params"] or "{}"),
            "body": json.loads(row["body_template"] or "{}"),
        },
    }
   #print("coonfig from endpoint test", config)
    connector = APIConnector(userid=userid, config=config)
    result = connector.execute()
    # print("result", result)

    cur.execute(
        """
        UPDATE external_app_endpoints
        SET last_tested_at = NOW(),
            last_test_status = %s,
            last_error = %s
        WHERE id = %s
    """,
        (
            "success" if result["success"] else "failed",
            None if result["success"] else json.dumps(result),
            endpoint_id,
        ),
    )

    conn.commit()
    cur.close()
    conn.close()

    return jsonify(result)


@apiconnector_bp.route("/<int:app_id>/test", methods=["POST"])
def test_external_app(app_id):
    conn = None
    cur = None

    try:
        test_config = request.get_json(force=True) or {}

        conn = connect_to_rds()
        cur = conn.cursor(pymysql.cursors.DictCursor)

        # -----------------------------------
        # LOAD APP
        # -----------------------------------
        cur.execute(
            """
            SELECT id, base_url, auth_config
            FROM external_apps
            WHERE id = %s
            """,
            (app_id,),
        )

        app = cur.fetchone()
        if not app:
            return jsonify({"success": False, "error": "App not found"}), 404

        auth = json.loads(app["auth_config"])

        # -----------------------------------
        # BUILD REQUEST
        # -----------------------------------
        path = test_config.get("path", "/")
        userid = test_config.get("user_id")
        method = test_config.get("method", "GET")
        headers = test_config.get("headers", {})
        query_params = test_config.get("query_params", {})
        body = test_config.get("body")

        base_url = app["base_url"].rstrip("/")
        full_url = f"{base_url}{path}"

        config = {
            "auth": auth,
            "request": {
                "url": full_url,
                "method": method,
                "headers": headers,
                "query_params": query_params,
                "body": body,
            },
            "retry": {"count": 2, "backoff": 1},
            "timeout": 10,
        }

        # -----------------------------------
        # EXECUTE TEST
        # -----------------------------------
        connector = APIConnector(userid=userid, config=config)
        result = connector.execute()

        # -----------------------------------
        # STORE TEST RESULT
        # -----------------------------------
        cur.execute(
            """
    UPDATE external_apps
    SET status = %s,
        updated_at = NOW()
    WHERE id = %s
    """,
            (
                "active" if result["success"] else "error",
                app_id,
            ),
        )

        conn.commit()

        return jsonify(result)

    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 500

    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@apiconnector_bp.route("/<int:app_id>/execute", methods=["POST"])
def execute_app(app_id):
    payload = request.get_json(force=True) or {}
    userid = payload.get("user_id")

    if not userid:
        return jsonify({"success": False, "error": "user_id required"}), 400

    try:
        result = _execute_app_internal(app_id, userid)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@apiconnector_bp.route("/endpoints/<int:endpoint_id>/execute", methods=["POST"])
async def execute_endpoint(endpoint_id, userid=None):

    payload = request.get_json() or {}
    userid = payload.get("user_id")
    context = payload.get("context", {})
    try:
        result = await _execute_endpoint_internal(endpoint_id, userid, context)
        return jsonify(result)
    except Exception as e:
       #print("error on executing endpoint", e)
        return jsonify({"success": False, "error": str(e)}), 500


@apiconnector_bp.route("/endpoints/<int:endpoint_id>", methods=["DELETE"])
def delete_endpoint(endpoint_id):
    conn = None
    cur = None

    try:
        conn = connect_to_rds()
        cur = conn.cursor()

        # -----------------------------------
        # CHECK EXISTS
        # -----------------------------------
        cur.execute(
            """
            SELECT id
            FROM external_app_endpoints
            WHERE id = %s
            """,
            (endpoint_id,),
        )

        if not cur.fetchone():
            return jsonify({"success": False, "error": "Endpoint not found"}), 404

        # -----------------------------------
        # DELETE
        # -----------------------------------
        cur.execute(
            """
            DELETE FROM external_app_endpoints
            WHERE id = %s
            """,
            (endpoint_id,),
        )

        conn.commit()

        return jsonify({"success": True, "message": "Endpoint deleted permanently"})

    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 500

    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@apiconnector_bp.route("/<int:app_id>/schedule", methods=["POST"])
async def schedule_app(app_id):
    body = request.json or {}
    userid = body.get("user_id")
    activation = body.get("scheduledActivation")

    if not userid:
        return jsonify({"error": "user_id missing"}), 400

    schedule_type, data = resolve_schedule_from_activation(activation)
    timezone = data.get("timezone", "UTC")

    # ---------- ONE TIME ----------
    if schedule_type == "one_time":
        dt = datetime.fromisoformat(data["datetime"])
        result = await APIConnectorScheduler.schedule_app_once(
            userid, app_id, dt, timezone
        )

    # ---------- DAILY ----------
    elif schedule_type == "daily":
        hour, minute = map(int, data["startTime"].split(":"))
        result = await APIConnectorScheduler.schedule_app_daily(
            userid, app_id, hour, minute, timezone
        )

    # ---------- WEEKLY ----------
    elif schedule_type == "weekly":
        hour, minute = map(int, data["startTime"].split(":"))
        result = await APIConnectorScheduler.schedule_app_weekly(
            userid, app_id, data["weekday"], hour, minute, timezone
        )

    # ---------- MONTHLY ----------
    elif schedule_type == "monthly":
        hour, minute = map(int, data["startTime"].split(":"))
        result = await APIConnectorScheduler.schedule_app_monthly(
            userid, app_id, data["day"], hour, minute, timezone
        )

    # ---------- INTERVAL ----------
    elif schedule_type == "interval":
        result = await APIConnectorScheduler.schedule_app_interval(
            userid, app_id, data["seconds"]
        )

    # ---------- CUSTOM RANGE ----------
    elif schedule_type == "custom":
        dates = expand_custom_dates(
            data["startDate"],
            data["endDate"],
            data["startTime"],
        )
        result = await APIConnectorScheduler.schedule_app_custom_dates(
            userid, app_id, dates, timezone
        )

    else:
        return jsonify({"error": "Unsupported schedule type"}), 400
    schedule_record = {
        "frequency": schedule_type,
        "config": data,
        "timezone": timezone,
        "status": "active",
        "created_at": datetime.utcnow().isoformat(),
    }
    # attach celery ids
    if "task_id" in result:
        schedule_record["celery_task_id"] = result["task_id"]
    if "task_ids" in result:
        schedule_record["celery_task_ids"] = result["task_ids"]
    if "entry_name" in result:
        schedule_record["celery_entry"] = result["entry_name"]

    schedule_record["execution_key"] = f"endpoint:{app_id}:{userid}:{schedule_type}"

    save_endpoint_schedule(app_id, schedule_record)
    return jsonify({"success": True, "schedule": result})


@apiconnector_bp.route("/endpoints/<int:endpoint_id>/schedule", methods=["POST"])
async def schedule_endpoint(endpoint_id):
    body = request.json or {}
   #print("body received for schedule ", body)
    userid = body.get("user_id")
    activation = body.get("scheduledActivation")

    if not userid:
        return jsonify({"error": "user_id missing"}), 400

    schedule_type, data = resolve_schedule_from_activation(activation)
    timezone = data.get("timezone", "UTC")
    if values := get_schedule_endpointdetails(endpoint_id):
       #print("values", values, type(values))

        celery_type = values.get("celery_type", "")
        celery_id = values.get("celery_task_id", "")
        celery_entry_name = values.get("celery_entry", "")
        celery_task_ids = values.get("celery_task_ids", "")

        if celery_type == "task" and celery_id:
            APIConnectorScheduler.revoke_task(celery_id)

        elif celery_type == "beat" and celery_entry_name:
            APIConnectorScheduler.disable_celery_entry(celery_entry_name)
        elif celery_type == "tasks" and celery_task_ids:
            for tid in celery_task_ids:
                APIConnectorScheduler.revoke_task(tid)

        # fallback – if type missing but task_id exists
        elif celery_id:
            APIConnectorScheduler.revoke_task(celery_id)

    if schedule_type == "one_time":
        dt = datetime.fromisoformat(data["datetime"])
        result = await APIConnectorScheduler.schedule_endpoint_once(
            userid, endpoint_id, dt, timezone
        )

    elif schedule_type == "daily":
        hour, minute = map(int, data["startTime"].split(":"))
        result = await APIConnectorScheduler.schedule_endpoint_daily(
            userid, endpoint_id, hour, minute, timezone
        )

    elif schedule_type == "weekly":
        hour, minute = map(int, data["startTime"].split(":"))
        result = await APIConnectorScheduler.schedule_endpoint_weekly(
            userid, endpoint_id, data["weekday"], hour, minute, timezone
        )

    elif schedule_type == "monthly":
        hour, minute = map(int, data["startTime"].split(":"))
        result = await APIConnectorScheduler.schedule_endpoint_monthly(
            userid, endpoint_id, data["day"], hour, minute, timezone
        )

    elif schedule_type == "interval":
        result = await APIConnectorScheduler.schedule_endpoint_interval(
            userid, endpoint_id, data["seconds"]
        )

    elif schedule_type == "custom":
        dates = expand_custom_dates(
            data["startDate"],
            data["endDate"],
            data["startTime"],
            data["intervalMinutes"],
        )
        result = await APIConnectorScheduler.schedule_endpoint_custom_dates(
            userid, endpoint_id, dates, timezone
        )

    else:
        return jsonify({"error": "Unsupported schedule type"}), 400
    schedule_record = {
        "frequency": schedule_type,
        "config": data,
        "timezone": timezone,
        "status": "active",
        "created_at": datetime.utcnow().isoformat(),
    }

    # attach celery ids
    if "task_id" in result:
        schedule_record["celery_type"] = "task"
        schedule_record["celery_task_id"] = result["task_id"]
    if "task_ids" in result:
        schedule_record["celery_task_ids"] = result["task_ids"]
        schedule_record["celery_type"] = "tasks"

    if "entry_name" in result:
        schedule_record["celery_type"] = "beat"
        schedule_record["celery_entry"] = result["entry_name"]

    schedule_record["execution_key"] = (
        f"endpoint:{endpoint_id}:{userid}:{schedule_type}"
    )

    save_endpoint_schedule(endpoint_id, schedule_record)
    return jsonify({"success": True, "schedule": result})


@apiconnector_bp.route("/endpoints/<int:endpoint_id>/schedules/stop", methods=["POST"])
async def stop_schedule(endpoint_id):
    """
    Stop a schedule for a given endpoint.
    Body JSON:
    {
        "execution_key": "endpoint:3:109161866299858012556:interval"
    }
    """
    body = request.json or {}
    execution_key = body.get("execution_key")
    if not execution_key:
        return jsonify({"success": False, "error": "execution_key missing"}), 400

    conn = connect_to_rds()
    cur = conn.cursor(pymysql.cursors.DictCursor)

    # Fetch existing schedules
    cur.execute(
        "SELECT schedules FROM external_app_endpoints WHERE id=%s", (endpoint_id,)
    )
    row = cur.fetchone()
    if not row:
        return jsonify({"success": False, "error": "Endpoint not found"}), 404

    # Ensure schedules is always a list
    schedules_raw = json.loads(row["schedules"] or "[]")
    if isinstance(schedules_raw, dict):
        schedules = [schedules_raw]  # wrap single dict in a list
    else:
        schedules = schedules_raw

    updated = False
    # print("schedules", schedules)

    # Find and stop the schedule
    for sch in schedules:
        sch_key = sch.get("execution_key")
       #print(f'type of sch.get("execution_key") = {type(sch_key)}, value = {sch_key}')
       #print(f"type of execution_key = {type(execution_key)}, value = {execution_key}")
        if sch.get("execution_key") == execution_key:
            execution_key = sch.get("execution_key")
            sch["status"] = "inactive"

            # Stop celery beat/interval
            if sch.get("celery_entry"):
               #print("stopping a beat")
                await APIConnectorScheduler.disable_celery_entry(sch["celery_entry"])

            # Stop one-time task
            if sch.get("celery_task_id"):
               #print("stopping a task")
                await APIConnectorScheduler.revoke_task(sch["celery_task_id"])
            if sch.get("celery_task_ids"):
               #print("stopping multiple tasks", len(sch["celery_task_ids"]))
                for tid in sch["celery_task_ids"]:
                    APIConnectorScheduler.revoke_task(tid)

            updated = True
            break

    # if not updated:
    #     return jsonify({"success": False, "error": "Schedule not found"}), 404

    # Update DB
    cur.execute(
        "UPDATE external_app_endpoints SET schedules=%s WHERE id=%s",
        (json.dumps(schedules if len(schedules) > 1 else schedules[0]), endpoint_id),
    )
    conn.commit()
    conn.close()
    await APIConnectorScheduler.make_schedule_disabled(stop_key=execution_key)
    return jsonify({"success": True, "message": "Schedule stopped"})


@apiconnector_bp.route("/endpoints/<int:endpoint_id>/runs", methods=["GET"])
def list_endpoint_runs(endpoint_id):
    userid = request.args.get("user_id")

    conn = connect_to_rds()
    cur = conn.cursor(pymysql.cursors.DictCursor)

    try:
        cur.execute(
            "SELECT app_id FROM external_app_endpoints WHERE id = %s",
            (endpoint_id,),
        )
        row = cur.fetchone()

        if not row:
            return jsonify({"success": False, "error": "Endpoint not found"}), 404

        app_id = row["app_id"]

        prefix = f"{userid}/apiconnectors/{app_id}/{endpoint_id}/"
        files = getallendpointdetails(prefix)

        return jsonify({"success": True, "runs": files})

    finally:
        cur.close()
        conn.close()


@apiconnector_bp.route("/endpoints/<int:endpoint_id>/runs/<filename>", methods=["GET"])
def get_endpoint_run(endpoint_id, filename):
    userid = request.args.get("user_id")

    conn = connect_to_rds()
    cur = conn.cursor(pymysql.cursors.DictCursor)

    try:
        cur.execute(
            "SELECT app_id FROM external_app_endpoints WHERE id = %s",
            (endpoint_id,),
        )
        row = cur.fetchone()

        if not row:
            return jsonify({"success": False, "error": "Endpoint not found"}), 404

        app_id = row["app_id"]

        key = f"{userid}/apiconnectors/{app_id}/{endpoint_id}/{filename}"

        try:
            data = get_filedata_endp(key)
            return jsonify({"success": True, "data": data})
        except Exception:
            return jsonify({"success": False, "error": "Run not found"}), 404

    finally:
        cur.close()
        conn.close()
