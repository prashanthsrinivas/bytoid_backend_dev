import asyncio
import json
import os
import re
from datetime import datetime

import pymysql
from onelogin.saml2.auth import OneLogin_Saml2_Auth

from agent_route.lance_agent import LanceClient
from apiConnector.helpers import build_full_url
from credits_route.route import Credits
from db.rds_db import connect_to_rds
from flask import jsonify
from services.apiconnectors import APIConnector
from utils.s3_utils import save_app_runbase_S3


# ──────────────────────────────────────────────
# Admin gate
# ──────────────────────────────────────────────

def _admin_only_check(user_id):
    """
    Returns (True, None) when user_id belongs to an admin.
    Returns (False, (response, status_code)) otherwise.
    """
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
# AWS session helpers
# ──────────────────────────────────────────────

def _get_active_aws_session(user_id):
    """
    Returns the non-expired aws_saml_sessions row for user_id, or None.
    """
    conn = None
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                """
                SELECT aws_access_key_id, aws_secret_access_key, aws_session_token,
                       aws_region, aws_account_id, aws_role_arn, expires_at
                FROM aws_saml_sessions
                WHERE user_id=%s AND expires_at > NOW()
                LIMIT 1
                """,
                (user_id,),
            )
            return cur.fetchone()
    except Exception:
        return None
    finally:
        if conn:
            conn.close()


def _build_sigv4_auth_from_session(session_row, service="execute-api"):
    """
    Converts an aws_saml_sessions row into an auth_config dict
    accepted by APIConnector when auth_type='aws_sigv4'.
    """
    return {
        "type": "aws_sigv4",
        "access_key_id": session_row["aws_access_key_id"],
        "secret_access_key": session_row["aws_secret_access_key"],
        "session_token": session_row.get("aws_session_token"),
        "region": session_row.get("aws_region", "us-east-1"),
        "service": service,
    }


def _resolve_aws_auth(auth_config_raw, user_id, service="execute-api"):
    """
    Returns a resolved auth_config dict.
    If auth_config_raw is empty / missing credentials, falls back to
    the stored SAML session.  Raises ValueError if no session is available.
    """
    if auth_config_raw and auth_config_raw.get("access_key_id"):
        return auth_config_raw

    session_row = _get_active_aws_session(user_id)
    if not session_row:
        raise ValueError(
            "AWS credentials not found. Authenticate first via /aws/saml/login."
        )
    return _build_sigv4_auth_from_session(session_row, service=service)


# ──────────────────────────────────────────────
# AWS IdP config (per-admin)
# ──────────────────────────────────────────────

def _get_aws_idp_config(user_id):
    """Returns the aws_idp_configs row for user_id, or None."""
    conn = None
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                """
                SELECT entity_id, sso_url, x509_cert, aws_region
                FROM aws_idp_configs
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
# SAML request helpers (mirror of sso_by/routes.py)
# ──────────────────────────────────────────────

def prepare_flask_request_aws(request):
    proto = request.headers.get("X-Forwarded-Proto", "https")
    host = request.headers.get("X-Forwarded-Host", request.host)
    return {
        "https": "on" if proto == "https" else "off",
        "http_host": host,
        "server_port": None,
        "script_name": request.path,
        "get_data": request.args.copy(),
        "post_data": request.form.copy(),
    }


def _init_saml_auth_aws(req, user_id):
    """
    Builds OneLogin_Saml2_Auth using the static SP config from saml/aws/settings.json
    overlaid with the per-user IdP config fetched from aws_idp_configs.
    """
    settings_path = os.path.join(os.getcwd(), "saml", "aws", "settings.json")
    with open(settings_path) as f:
        settings = json.load(f)

    idp = _get_aws_idp_config(user_id)
    if idp:
        settings["idp"]["entityId"] = idp["entity_id"]
        settings["idp"]["singleSignOnService"]["url"] = idp["sso_url"]
        settings["idp"]["x509cert"] = idp["x509_cert"]

    return OneLogin_Saml2_Auth(req, old_settings=settings)


# ──────────────────────────────────────────────
# S3 + LanceDB dual logging
# ──────────────────────────────────────────────

async def save_aws_run_to_s3(
    *, db, user_id, app_id, endpoint_id, request_cfg, result, trigger
):
    """
    Identical to apiConnector/helpers.py:save_app_run_to_s3 but uses the
    aws_connector S3 prefix and LanceDB table naming.
    """
    now = datetime.utcnow()
    minute_bucket = now.strftime("%Y-%m-%d-%H-%M")

    if endpoint_id:
        key = f"{user_id}/aws_connector/{app_id}/{endpoint_id}/{minute_bucket}.json"
    else:
        key = f"{user_id}/aws_connector/{app_id}/{minute_bucket}.json"

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

async def _execute_aws_endpoint_internal(endpoint_id, user_id, runtime_params=None):
    """
    Mirrors apiConnector/helpers.py:_execute_endpoint_internal but targets
    aws_external_app_endpoints / aws_external_apps and uses save_aws_run_to_s3.
    """
    runtime_params = runtime_params or {}

    conn = connect_to_rds()
    cur = conn.cursor(pymysql.cursors.DictCursor)

    # 1. Load endpoint + parent app
    cur.execute(
        """
        SELECT
            e.*,
            a.base_url,
            a.auth_config,
            a.timeout_seconds AS app_timeout,
            a.retry_count,
            a.retry_backoff_seconds
        FROM aws_external_app_endpoints e
        JOIN aws_external_apps a ON a.id = e.app_id
        WHERE e.id = %s AND e.is_active = 1
        """,
        (endpoint_id,),
    )
    row = cur.fetchone()

    if not row:
        cur.close()
        conn.close()
        raise ValueError("AWS endpoint not found")

    # 2. Parse stored JSON
    try:
        db_headers = json.loads(row["headers"] or "{}")
        db_query_params = json.loads(row["query_params"] or "{}")
        db_path_params = json.loads(row["path_params"] or "{}")
        db_body = json.loads(row["body_template"] or "null")
        raw_auth = json.loads(row["auth_config"] or "{}")
        auth_config = _resolve_aws_auth(raw_auth, user_id)
    except json.JSONDecodeError as e:
        cur.close()
        conn.close()
        raise ValueError(f"Invalid JSON in DB: {e}")

    # 3. Merge runtime overrides
    final_headers = {**db_headers, **runtime_params.get("headers", {})}
    final_query_params = {**db_query_params, **runtime_params.get("query_params", {})}
    final_path_params = {**db_path_params, **runtime_params.get("path_params", {})}
    final_body = runtime_params.get("body", db_body)

    # 4. Substitute path variables
    base_url = row["base_url"].rstrip("/")
    path = row["path"]

    for var in re.findall(r"\{(.*?)\}", path):
        if var not in final_path_params:
            cur.close()
            conn.close()
            raise ValueError(f"Missing path parameter: {var}")
        path = path.replace(f"{{{var}}}", str(final_path_params[var]))

    full_url = f"{base_url}{path}"

    # 5. Build config
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

    # 6. Execute
    connector = APIConnector(userid=user_id, config=config)
    result = connector.execute()

    # 7. Dual log to S3 + LanceDB
    await save_aws_run_to_s3(
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


async def _execute_aws_app_internal(app_id, user_id):
    """App-level execution (no endpoint, uses base_url directly)."""
    conn = connect_to_rds()
    cur = conn.cursor(pymysql.cursors.DictCursor)

    cur.execute("SELECT * FROM aws_external_apps WHERE id=%s", (app_id,))
    app = cur.fetchone()
    if not app:
        cur.close()
        conn.close()
        raise ValueError("AWS app not found")

    raw_auth = json.loads(app["auth_config"] or "{}")
    auth_config = _resolve_aws_auth(raw_auth, user_id)

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

    await save_aws_run_to_s3(
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
