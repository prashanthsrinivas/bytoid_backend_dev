import json
import re
from datetime import datetime

import pymysql
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from agent_route.lance_agent import LanceClient
from credits_route.route import Credits
from db.rds_db import connect_to_rds
from flask import jsonify
from services.apiconnectors import APIConnector
from utils.s3_utils import save_app_runbase_S3


# ──────────────────────────────────────────────
# Admin gate
# ──────────────────────────────────────────────

def _admin_only_check(user_id):
    if not user_id:
        return False, (jsonify({"error": "ADMIN_ONLY", "detail": "user_id required"}), 403)

    conn = None
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT user_type FROM users WHERE user_id=%s", (user_id,))
            row = cur.fetchone()

        if not row or row.get("user_type") != "admin":
            return False, (jsonify({"error": "ADMIN_ONLY"}), 403)

        return True, None

    except Exception as e:
        return False, (jsonify({"error": "ADMIN_ONLY", "detail": str(e)}), 403)

    finally:
        if conn:
            conn.close()


# ──────────────────────────────────────────────
# GCP config helpers
# ──────────────────────────────────────────────

def _get_gcp_config(user_id):
    conn = None
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                """
                SELECT project_id, service_account_email, service_account_key,
                       default_scope, gcp_region
                FROM gcp_configs
                WHERE user_id=%s LIMIT 1
                """,
                (user_id,),
            )
            return cur.fetchone()
    except Exception:
        return None
    finally:
        if conn:
            conn.close()


# ──────────────────────────────────────────────
# Token generation (service account JWT)
# ──────────────────────────────────────────────

def _fetch_service_account_token(config_row):
    """
    Use stored service account JSON key to fetch a short-lived GCP OAuth token.
    Returns (token_dict, None) on success, (None, error_string) on failure.
    """
    try:
        sa_key = json.loads(config_row["service_account_key"])
        scope = config_row.get("default_scope") or "https://www.googleapis.com/auth/cloud-platform"
        credentials = service_account.Credentials.from_service_account_info(
            sa_key, scopes=[scope]
        )
        credentials.refresh(Request())
        expires_in = int((credentials.expiry - datetime.utcnow()).total_seconds())
        return {"access_token": credentials.token, "expires_in": max(expires_in, 0)}, None
    except Exception as e:
        return None, str(e)


def _resolve_gcp_auth(auth_config_raw, user_id):
    """
    Returns a resolved auth_config dict for APIConnector.
    If auth_config_raw carries an access_token, use it directly.
    Otherwise fetch a fresh token from the stored service account key.
    Raises ValueError if no config is found.
    """
    if auth_config_raw and auth_config_raw.get("access_token"):
        result = dict(auth_config_raw)
        result["type"] = "gcp_oauth"
        return result

    config_row = _get_gcp_config(user_id)
    if not config_row:
        raise ValueError("GCP credentials not configured. Save your service account key first via /gcp/config.")

    token, err = _fetch_service_account_token(config_row)
    if err or not token:
        raise ValueError(f"Failed to obtain GCP token: {err}")

    return {"type": "gcp_oauth", "access_token": token["access_token"]}


# ──────────────────────────────────────────────
# S3 + LanceDB dual logging
# ──────────────────────────────────────────────

async def save_gcp_run_to_s3(
    *, db, user_id, app_id, endpoint_id, request_cfg, result, trigger
):
    now = datetime.utcnow()
    minute_bucket = now.strftime("%Y-%m-%d-%H-%M")

    if endpoint_id:
        key = f"{user_id}/gcp_connector/{app_id}/{endpoint_id}/{minute_bucket}.json"
    else:
        key = f"{user_id}/gcp_connector/{app_id}/{minute_bucket}.json"

    record = {
        "ts": now.isoformat() + "Z",
        "trigger": trigger,
        "request": request_cfg,
        "response": result,
    }

    val = save_app_runbase_S3(record=record, key=key)
    if val:
        credits = Credits(db=db)
        lance = LanceClient(user_id=user_id, credits=credits)
        await lance.save_app_run(
            user_id=user_id,
            app_id=app_id,
            endpoint_id=endpoint_id,
            request_cfg=request_cfg,
            result=result,
            trigger=trigger,
            minute_bucket=minute_bucket,
        )

    return val


# ──────────────────────────────────────────────
# Endpoint execution
# ──────────────────────────────────────────────

async def _execute_gcp_endpoint_internal(endpoint_id, user_id, runtime_params=None):
    runtime_params = runtime_params or {}

    conn = connect_to_rds()
    cur = conn.cursor(pymysql.cursors.DictCursor)

    cur.execute(
        """
        SELECT
            e.*,
            a.base_url,
            a.auth_config,
            a.timeout_seconds AS app_timeout,
            a.retry_count,
            a.retry_backoff_seconds
        FROM gcp_external_app_endpoints e
        JOIN gcp_external_apps a ON a.id = e.app_id
        WHERE e.id = %s AND e.is_active = 1
        """,
        (endpoint_id,),
    )
    row = cur.fetchone()

    if not row:
        cur.close()
        conn.close()
        raise ValueError("GCP endpoint not found")

    try:
        db_headers = json.loads(row["headers"] or "{}")
        db_query_params = json.loads(row["query_params"] or "{}")
        db_path_params = json.loads(row["path_params"] or "{}")
        db_body = json.loads(row["body_template"] or "null")
        raw_auth = json.loads(row["auth_config"] or "{}")
        auth_config = _resolve_gcp_auth(raw_auth, user_id)
    except json.JSONDecodeError as e:
        cur.close()
        conn.close()
        raise ValueError(f"Invalid JSON in DB: {e}")

    final_headers = {**db_headers, **runtime_params.get("headers", {})}
    final_query_params = {**db_query_params, **runtime_params.get("query_params", {})}
    final_path_params = {**db_path_params, **runtime_params.get("path_params", {})}
    final_body = runtime_params.get("body", db_body)

    base_url = row["base_url"].rstrip("/")
    path = row["path"]

    for var in re.findall(r"\{(.*?)\}", path):
        if var not in final_path_params:
            cur.close()
            conn.close()
            raise ValueError(f"Missing path parameter: {var}")
        path = path.replace(f"{{{var}}}", str(final_path_params[var]))

    full_url = f"{base_url}{path}"

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

    connector = APIConnector(userid=user_id, config=config)
    result = connector.execute()

    await save_gcp_run_to_s3(
        db=conn,
        user_id=user_id,
        app_id=row["app_id"],
        endpoint_id=endpoint_id,
        request_cfg=config["request"],
        result=result,
        trigger="manual",
    )

    cur.close()
    conn.close()

    return result


async def _execute_gcp_app_internal(app_id, user_id):
    conn = connect_to_rds()
    cur = conn.cursor(pymysql.cursors.DictCursor)

    cur.execute("SELECT * FROM gcp_external_apps WHERE id=%s", (app_id,))
    app = cur.fetchone()
    if not app:
        cur.close()
        conn.close()
        raise ValueError("GCP app not found")

    raw_auth = json.loads(app["auth_config"] or "{}")
    auth_config = _resolve_gcp_auth(raw_auth, user_id)

    config = {
        "auth": auth_config,
        "request": {
            "url": app["base_url"].rstrip("/"),
            "method": "GET",
            "headers": json.loads(app["headers"] or "{}"),
            "query_params": json.loads(app["query_params"] or "{}"),
            "body": None,
        },
        "timeout": app.get("timeout_seconds") or 10,
        "retry": {
            "count": app.get("retry_count") or 1,
            "backoff": app.get("retry_backoff_seconds") or 1,
        },
    }

    connector = APIConnector(userid=user_id, config=config)
    result = connector.execute()

    await save_gcp_run_to_s3(
        db=conn,
        user_id=user_id,
        app_id=app_id,
        endpoint_id=None,
        request_cfg=config["request"],
        result=result,
        trigger="schedule",
    )

    cur.close()
    conn.close()
    return result
