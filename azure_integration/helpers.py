import base64
import json
import os
import re
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET

import pymysql
import requests
from onelogin.saml2.auth import OneLogin_Saml2_Auth

from agent_route.lance_agent import LanceClient
from credits_route.route import Credits
from db.rds_db import connect_to_rds
from flask import jsonify
from services.apiconnectors import APIConnector
from utils.s3_utils import save_app_runbase_S3
from utils.key_rotation_manager import SecureKMSService as _AzureRunKMSService
_azure_run_kms = _AzureRunKMSService()


def _enc_run(user_id, v):
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    enc = _azure_run_kms.encrypt(user_id, s)
    return {"ciphertext": enc["ciphertext"], "iv": enc["iv"], "encrypted_key": enc["encrypted_key"]}


def _dec_run(user_id, v):
    if isinstance(v, dict) and "encrypted_key" in v:
        raw = _azure_run_kms.decrypt(user_id, v["encrypted_key"], v["iv"], v["ciphertext"])
        try:
            return json.loads(raw)
        except Exception:
            return raw
    return v


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
# Azure session helpers
# ──────────────────────────────────────────────

def _get_active_azure_session(user_id):
    """
    Returns the non-expired azure_saml_sessions row for user_id, or None.
    """
    conn = None
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                """
                SELECT access_token, refresh_token, scope, azure_region,
                       azure_tenant_id, azure_object_id, azure_upn, expires_at
                FROM azure_saml_sessions
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


def _get_any_azure_session(user_id):
    """Same as _get_active_azure_session but ignores expires_at — used for refresh."""
    conn = None
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                """
                SELECT access_token, refresh_token, scope, azure_region,
                       azure_tenant_id, azure_object_id, azure_upn, expires_at
                FROM azure_saml_sessions
                WHERE user_id=%s
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


def _build_oauth_auth_from_session(session_row):
    """
    Converts an azure_saml_sessions row into an auth_config dict accepted by
    APIConnector when auth_type='azure_oauth'.
    """
    return {
        "type": "azure_oauth",
        "access_token": session_row["access_token"],
    }


def _refresh_azure_token(user_id):
    """
    Try to refresh the user's Azure access token using the stored refresh_token.
    UPSERTs the new credentials into azure_saml_sessions and returns the new row,
    or None if the refresh fails / no refresh_token exists / no IdP config exists.
    """
    idp_cfg = _get_azure_idp_config(user_id)
    if not idp_cfg:
        return None

    session_row = _get_any_azure_session(user_id)
    if not session_row or not session_row.get("refresh_token"):
        # client_credentials tokens have no refresh_token — just re-issue.
        token, err = _fetch_client_credentials_token(idp_cfg)
        if err or not token:
            return None
        expires_at = (
            datetime.utcnow() + timedelta(seconds=int(token.get("expires_in", 3600)))
        ).strftime("%Y-%m-%d %H:%M:%S")
        conn = None
        try:
            conn = connect_to_rds()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE azure_saml_sessions
                    SET access_token=%s, scope=%s, expires_at=%s, updated_at=NOW()
                    WHERE user_id=%s
                    """,
                    (
                        token["access_token"],
                        token.get("scope") or idp_cfg.get("default_scope"),
                        expires_at,
                        user_id,
                    ),
                )
            conn.commit()
        except Exception:
            return None
        finally:
            if conn:
                conn.close()
        return _get_active_azure_session(user_id)

    token_url = f"https://login.microsoftonline.com/{idp_cfg['tenant_id']}/oauth2/v2.0/token"
    try:
        resp = requests.post(
            token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": session_row["refresh_token"],
                "client_id": idp_cfg["client_id"],
                "client_secret": idp_cfg["client_secret"],
                "scope": session_row.get("scope") or idp_cfg.get("default_scope")
                or "https://graph.microsoft.com/.default",
            },
            timeout=15,
        )
        if not resp.ok:
            return None
        token = resp.json()
    except Exception:
        return None

    expires_at = (
        datetime.utcnow() + timedelta(seconds=int(token.get("expires_in", 3600)))
    ).strftime("%Y-%m-%d %H:%M:%S")

    conn = None
    try:
        conn = connect_to_rds()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE azure_saml_sessions
                SET access_token=%s,
                    refresh_token=COALESCE(%s, refresh_token),
                    scope=%s,
                    expires_at=%s,
                    updated_at=NOW()
                WHERE user_id=%s
                """,
                (
                    token["access_token"],
                    token.get("refresh_token"),
                    token.get("scope") or session_row.get("scope"),
                    expires_at,
                    user_id,
                ),
            )
        conn.commit()
    except Exception:
        return None
    finally:
        if conn:
            conn.close()

    return _get_active_azure_session(user_id)


def _resolve_azure_auth(auth_config_raw, user_id):
    """
    Returns a resolved auth_config dict for APIConnector.
    If auth_config_raw already carries a static access_token, return it.
    Otherwise fall back to the stored SAML session, refreshing via refresh_token
    if the session is expired.
    Raises ValueError if no session is available.
    """
    if auth_config_raw and auth_config_raw.get("access_token"):
        result = dict(auth_config_raw)
        result["type"] = "azure_oauth"
        return result

    session_row = _get_active_azure_session(user_id)
    if not session_row:
        refreshed = _refresh_azure_token(user_id)
        if not refreshed:
            raise ValueError(
                "Azure credentials not found. Authenticate first via /azure/saml/login."
            )
        session_row = refreshed

    return _build_oauth_auth_from_session(session_row)


# ──────────────────────────────────────────────
# Azure IdP config (per-admin)
# ──────────────────────────────────────────────

def _get_azure_idp_config(user_id):
    """Returns the azure_idp_configs row for user_id, or None."""
    conn = None
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                """
                SELECT entity_id, sso_url, x509_cert, azure_region,
                       tenant_id, client_id, client_secret, default_scope
                FROM azure_idp_configs
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
# IdP-initiated SSO: issuer → user lookup
# ──────────────────────────────────────────────

def _find_admin_by_saml_issuer(saml_response_b64):
    """
    For IdP-initiated SSO: decode the SAMLResponse, extract the <Issuer>, and
    return the user_id of the admin whose azure_idp_configs.entity_id matches.
    Returns None if the issuer can't be extracted or no admin matches.
    """
    try:
        decoded = base64.b64decode(saml_response_b64)
        root = ET.fromstring(decoded)
        ns = {"saml": "urn:oasis:names:tc:SAML:2.0:assertion"}
        issuer_el = root.find("saml:Issuer", ns)
        if issuer_el is None:
            issuer_el = root.find("Issuer")
        if issuer_el is None:
            return None
        issuer = issuer_el.text.strip() if issuer_el.text else None
        if not issuer:
            return None
    except Exception:
        return None

    conn = None
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT user_id FROM azure_idp_configs WHERE entity_id=%s LIMIT 1",
                (issuer,),
            )
            row = cur.fetchone()
        return row["user_id"] if row else None
    except Exception:
        return None
    finally:
        if conn:
            conn.close()


# ──────────────────────────────────────────────
# SAML request helpers
# ──────────────────────────────────────────────

def prepare_flask_request_azure(request):
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


def _init_saml_auth_azure(req, user_id):
    """
    Builds OneLogin_Saml2_Auth using the static SP config from saml/azure/settings.json
    overlaid with the per-user IdP config fetched from azure_idp_configs.
    """
    settings_path = os.path.join(os.getcwd(), "saml", "azure", "settings.json")
    with open(settings_path) as f:
        settings = json.load(f)

    idp = _get_azure_idp_config(user_id)
    if idp:
        settings["idp"]["entityId"] = idp["entity_id"]
        settings["idp"]["singleSignOnService"]["url"] = idp["sso_url"]
        settings["idp"]["x509cert"] = idp["x509_cert"]

    return OneLogin_Saml2_Auth(req, old_settings=settings)


# ──────────────────────────────────────────────
# Entra SAML → OAuth token exchange (SAML 2.0 Bearer Assertion grant)
# ──────────────────────────────────────────────

def exchange_saml_for_azure_token(idp_cfg, saml_response_b64):
    """
    Obtains an Azure access token after a successful SAML authentication.

    Azure Entra issues the SAML assertion itself, so the SAML2 bearer assertion
    grant (RFC 7522) is not applicable — Entra rejects its own assertions with
    AADSTS50107/AADSTS7000013. The SAML flow proved identity; we get the token
    for Graph API calls via client credentials (app-level grant).

    Returns (token_dict, None) on success, (None, error_string) on failure.
    """
    return _fetch_client_credentials_token(idp_cfg)


def _fetch_client_credentials_token(idp_cfg):
    """Issues a client_credentials grant and returns (token_dict, error)."""
    token_url = (
        f"https://login.microsoftonline.com/{idp_cfg['tenant_id']}/oauth2/v2.0/token"
    )
    scope = idp_cfg.get("default_scope") or "https://graph.microsoft.com/.default"
    try:
        resp = requests.post(
            token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": idp_cfg["client_id"],
                "client_secret": idp_cfg["client_secret"],
                "scope": scope,
            },
            timeout=15,
        )
    except Exception as e:
        return None, f"Token endpoint unreachable: {e}"

    if not resp.ok:
        return None, resp.text

    return resp.json(), None


# ──────────────────────────────────────────────
# S3 + LanceDB dual logging
# ──────────────────────────────────────────────

async def save_azure_run_to_s3(
    *, db, user_id, app_id, endpoint_id, request_cfg, result, trigger
):
    """Mirror of save_aws_run_to_s3 with an azure_connector prefix."""
    now = datetime.utcnow()
    minute_bucket = now.strftime("%Y-%m-%d-%H-%M")

    if endpoint_id:
        key = f"{user_id}/azure_connector/{app_id}/{endpoint_id}/{minute_bucket}.json"
    else:
        key = f"{user_id}/azure_connector/{app_id}/{minute_bucket}.json"

    record = {
        "ts": now.isoformat() + "Z",
        "trigger": trigger,
        "request": _enc_run(user_id, request_cfg),
        "response": _enc_run(user_id, result),
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
            provider="azure",
        )

    return val


# ──────────────────────────────────────────────
# Endpoint execution
# ──────────────────────────────────────────────

async def _execute_azure_endpoint_internal(endpoint_id, user_id, runtime_params=None):
    """
    Mirrors _execute_aws_endpoint_internal but targets
    azure_external_app_endpoints / azure_external_apps and uses azure_oauth auth.
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
        FROM azure_external_app_endpoints e
        JOIN azure_external_apps a ON a.id = e.app_id
        WHERE e.id = %s AND e.is_active = 1
        """,
        (endpoint_id,),
    )
    row = cur.fetchone()

    if not row:
        cur.close()
        conn.close()
        raise ValueError("Azure endpoint not found")

    # 2. Parse stored JSON
    try:
        db_headers = json.loads(row["headers"] or "{}")
        db_query_params = json.loads(row["query_params"] or "{}")
        db_path_params = json.loads(row["path_params"] or "{}")
        db_body = json.loads(row["body_template"] or "null")
        raw_auth = json.loads(row["auth_config"] or "{}")
        auth_config = _resolve_azure_auth(raw_auth, user_id)
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
    await save_azure_run_to_s3(
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


async def _execute_azure_app_internal(app_id, user_id):
    """App-level execution (no endpoint, uses base_url directly)."""
    conn = connect_to_rds()
    cur = conn.cursor(pymysql.cursors.DictCursor)

    cur.execute("SELECT * FROM azure_external_apps WHERE id=%s", (app_id,))
    app = cur.fetchone()
    if not app:
        cur.close()
        conn.close()
        raise ValueError("Azure app not found")

    raw_auth = json.loads(app["auth_config"] or "{}")
    auth_config = _resolve_azure_auth(raw_auth, user_id)

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

    await save_azure_run_to_s3(
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
