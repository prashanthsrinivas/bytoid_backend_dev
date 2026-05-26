import asyncio
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from urllib.parse import urlparse

import pymysql
from flask import Blueprint, g, jsonify, redirect, request, session

from apiConnector.helpers import (
    expand_custom_dates,
    resolve_schedule_from_activation,
)
from azure_integration.helpers import (
    _admin_only_check,
    _execute_azure_app_internal,
    _execute_azure_endpoint_internal,
    _find_admin_by_saml_issuer,
    _get_active_azure_session,
    _get_azure_idp_config,
    _init_saml_auth_azure,
    _resolve_azure_auth,
    exchange_saml_for_azure_token,
    prepare_flask_request_azure,
    save_azure_run_to_s3,
)
from db.rds_db import connect_to_rds
from services.apiconnectors import APIConnector
from services.audit_log_service import (
    AZURE_SAML_CONNECTED,
    AZURE_SAML_DISCONNECTED,
    log_audit_event,
)
from services.scheduler_service import AzureAPIConnectorScheduler
from utils.app_configs import ACCESSIBLE_IDS, ALLOWED_ORIGINS
from utils.s3_utils import get_filedata_endp, getallendpointdetails

azure_integration_bp = Blueprint("azure_integration", __name__, url_prefix="/azure")


# ─────────────────────────────────────────────────────────────
# Internal helpers specific to Azure tables
# ─────────────────────────────────────────────────────────────


def _save_azure_endpoint_schedule(endpoint_id, schedule_payload):
    conn = connect_to_rds()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE azure_external_app_endpoints
        SET schedules = %s, updated_at = NOW()
        WHERE id = %s
        """,
        (json.dumps(schedule_payload), endpoint_id),
    )
    conn.commit()
    cur.close()
    conn.close()


def _get_azure_schedule_endpointdetails(endpoint_id):
    conn = connect_to_rds()
    cur = conn.cursor()
    cur.execute(
        "SELECT schedules FROM azure_external_app_endpoints WHERE id = %s",
        (endpoint_id,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row and row[0]:
        try:
            return json.loads(row[0])
        except Exception:
            return None
    return None


def _extract_user_id():
    """Pull user_id from g, session, or request body/args."""
    uid = getattr(g, "user_id", None)
    if uid:
        return uid
    uid = session.get("user_id")
    if uid:
        return uid
    body = request.get_json(silent=True) or {}
    return body.get("user_id") or request.args.get("user_id")


def _parse_json_field(val):
    if val is None or val == "":
        return None
    if isinstance(val, (dict, list)):
        return val
    try:
        return json.loads(val)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# IdP configuration (per-admin)
# ─────────────────────────────────────────────────────────────


@azure_integration_bp.route("/idp/config", methods=["POST"])
def azure_save_idp_config():
    conn = None
    try:
        data = request.get_json(force=True) or {}
        user_id = data.get("user_id") or _extract_user_id()

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        entity_id = (data.get("entity_id") or "").strip()
        sso_url = (data.get("sso_url") or "").strip()
        x509_cert = (data.get("x509_cert") or "").strip()
        azure_region = (data.get("azure_region") or "eastus").strip()
        tenant_id = (data.get("tenant_id") or "").strip()
        client_id = (data.get("client_id") or "").strip()
        client_secret = (data.get("client_secret") or "").strip()
        default_scope = (
            data.get("default_scope") or "https://graph.microsoft.com/.default"
        ).strip()

        if not entity_id:
            return jsonify({"success": False, "error": "entity_id required"}), 400
        if not sso_url:
            return jsonify({"success": False, "error": "sso_url required"}), 400
        if not tenant_id:
            return jsonify({"success": False, "error": "tenant_id required"}), 400
        if not client_id:
            return jsonify({"success": False, "error": "client_id required"}), 400

        existing = _get_azure_idp_config(user_id)
        if not x509_cert:
            if not existing:
                return jsonify({"success": False, "error": "x509_cert required"}), 400
            x509_cert = existing["x509_cert"]
        if not client_secret:
            if not existing:
                return (
                    jsonify({"success": False, "error": "client_secret required"}),
                    400,
                )
            client_secret = existing["client_secret"]

        conn = connect_to_rds()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO azure_idp_configs
                    (user_id, entity_id, sso_url, x509_cert, azure_region,
                     tenant_id, client_id, client_secret, default_scope)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    entity_id     = VALUES(entity_id),
                    sso_url       = VALUES(sso_url),
                    x509_cert     = VALUES(x509_cert),
                    azure_region  = VALUES(azure_region),
                    tenant_id     = VALUES(tenant_id),
                    client_id     = VALUES(client_id),
                    client_secret = VALUES(client_secret),
                    default_scope = VALUES(default_scope),
                    updated_at    = NOW()
                """,
                (
                    user_id,
                    entity_id,
                    sso_url,
                    x509_cert,
                    azure_region,
                    tenant_id,
                    client_id,
                    client_secret,
                    default_scope,
                ),
            )
        conn.commit()
        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@azure_integration_bp.route("/idp/config", methods=["GET"])
def azure_get_idp_config():
    user_id = request.args.get("user_id") or _extract_user_id()

    ok, err = _admin_only_check(user_id)
    if not ok:
        return err

    idp = _get_azure_idp_config(user_id)
    if idp:
        return jsonify(
            {
                "configured": True,
                "entity_id": idp["entity_id"],
                "sso_url": idp["sso_url"],
                "azure_region": idp.get("azure_region", "eastus"),
                "tenant_id": idp.get("tenant_id"),
                "client_id": idp.get("client_id"),
                "default_scope": idp.get("default_scope"),
            }
        )
    return jsonify({"configured": False})


@azure_integration_bp.route("/idp/config", methods=["DELETE"])
def azure_delete_idp_config():
    conn = None
    try:
        data = request.get_json(force=True) or {}
        user_id = data.get("user_id") or _extract_user_id()

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        conn = connect_to_rds()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM azure_idp_configs WHERE user_id=%s", (user_id,))
        conn.commit()
        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


# ─────────────────────────────────────────────────────────────
# SAML — Login initiation
# ─────────────────────────────────────────────────────────────


@azure_integration_bp.route("/saml/login", methods=["GET"])
def azure_saml_login():
    user_id = request.args.get("user_id") or _extract_user_id()

    ok, err = _admin_only_check(user_id)
    if not ok:
        return err

    if not _get_azure_idp_config(user_id):
        return (
            jsonify(
                {"error": "Azure IdP not configured. Save your Entra ID details first."}
            ),
            400,
        )

    redirect_url = request.args.get("redirect", "")
    parsed = urlparse(redirect_url)
    host_origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else ""
    if host_origin not in ALLOWED_ORIGINS:
        # Infer the caller's origin from headers so dev instances redirect back
        # to themselves (localhost, dev.bytoid.ai, etc.) rather than prod.
        req_origin = request.headers.get("Origin") or ""
        if not req_origin:
            referer = request.headers.get("Referer") or ""
            p = urlparse(referer)
            req_origin = f"{p.scheme}://{p.netloc}" if p.netloc else ""
        if req_origin in ALLOWED_ORIGINS:
            redirect_url = f"{req_origin}/azure-integration"
        else:
            redirect_url = "https://app.bytoid.ai/azure-integration"

    # Store state in RelayState (echoed back by IdP in ACS POST) instead of
    # Flask session — session cookies are blocked on cross-site POST.
    relay_state = json.dumps({"user_id": user_id, "redirect": redirect_url})

    req = prepare_flask_request_azure(request)
    try:
        auth = _init_saml_auth_azure(req, user_id)
        login_url = auth.login(return_to=relay_state, force_authn=True)
        return redirect(login_url)
    except Exception as e:
        return jsonify({"error": "Azure SAML not configured", "detail": str(e)}), 500


# ─────────────────────────────────────────────────────────────
# SAML — Assertion Consumer Service
# ─────────────────────────────────────────────────────────────


@azure_integration_bp.route("/saml/acs", methods=["POST"])
def azure_saml_acs():
    conn = None
    try:
        relay_state_raw = request.form.get("RelayState", "")
        relay_data = {}
        try:
            relay_data = json.loads(relay_state_raw) if relay_state_raw else {}
        except Exception:
            pass

        user_id = relay_data.get("user_id") or session.get("azure_saml_user_id")
        redirect_target = (
            relay_data.get("redirect")
            or session.get("azure_saml_redirect")
            or "https://app.bytoid.ai/azure-integration"
        )

        parsed_rd = urlparse(redirect_target)
        host_rd = f"{parsed_rd.scheme}://{parsed_rd.netloc}" if parsed_rd.netloc else ""
        if host_rd not in ALLOWED_ORIGINS:
            redirect_target = "https://app.bytoid.ai/azure-integration"

        # IdP-initiated SSO fallback: no prior /saml/login
        if not user_id:
            raw_saml = request.form.get("SAMLResponse", "")
            user_id = _find_admin_by_saml_issuer(raw_saml) if raw_saml else None

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        req = prepare_flask_request_azure(request)
        auth = _init_saml_auth_azure(req, user_id)

        auth.process_response(request_id=None)
        errors = auth.get_errors()
        if errors:
            return (
                jsonify({"error": errors, "reason": auth.get_last_error_reason()}),
                400,
            )
        if not auth.is_authenticated():
            return jsonify({"error": "Not authenticated"}), 401

        idp_cfg = _get_azure_idp_config(user_id)
        if not idp_cfg:
            return jsonify({"error": "Azure IdP not configured"}), 400

        saml_response_b64 = request.form.get("SAMLResponse", "")
        if not saml_response_b64:
            return jsonify({"error": "SAMLResponse missing"}), 400

        token, exch_err = exchange_saml_for_azure_token(idp_cfg, saml_response_b64)
        if exch_err or not token:
            return jsonify({"error": "Token exchange failed", "detail": exch_err}), 400

        attrs = auth.get_attributes() or {}
        oid_list = (
            attrs.get("http://schemas.microsoft.com/identity/claims/objectidentifier")
            or []
        )
        azure_oid = oid_list[0] if oid_list else None
        upn_list = (
            attrs.get("http://schemas.xmlsoap.org/ws/2005/05/identity/claims/upn") or []
        )
        azure_upn = (
            upn_list[0]
            if upn_list
            else (auth.get_nameid() if hasattr(auth, "get_nameid") else None)
        )

        expires_at = (
            datetime.utcnow() + timedelta(seconds=int(token.get("expires_in", 3600)))
        ).strftime("%Y-%m-%d %H:%M:%S")
        scope = (
            token.get("scope")
            or idp_cfg.get("default_scope")
            or "https://graph.microsoft.com/.default"
        )
        azure_region = idp_cfg.get("azure_region") or "eastus"

        conn = connect_to_rds()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO azure_saml_sessions
                    (user_id, azure_tenant_id, azure_object_id, azure_upn,
                     access_token, refresh_token, scope, azure_region, expires_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                    azure_tenant_id = VALUES(azure_tenant_id),
                    azure_object_id = VALUES(azure_object_id),
                    azure_upn       = VALUES(azure_upn),
                    access_token    = VALUES(access_token),
                    refresh_token   = COALESCE(VALUES(refresh_token), refresh_token),
                    scope           = VALUES(scope),
                    azure_region    = VALUES(azure_region),
                    expires_at      = VALUES(expires_at),
                    updated_at      = NOW()
                """,
                (
                    user_id,
                    idp_cfg.get("tenant_id"),
                    azure_oid,
                    azure_upn,
                    token["access_token"],
                    token.get("refresh_token"),
                    scope,
                    azure_region,
                    expires_at,
                ),
            )
        conn.commit()

        log_audit_event(
            action=AZURE_SAML_CONNECTED,
            endpoint="/azure/saml/acs",
            ip=request.remote_addr,
            status="success",
            actor_user_id=user_id,
            metadata={
                "tenant_id": idp_cfg.get("tenant_id"),
                "azure_upn": azure_upn,
            },
        )

        sep = "&" if "?" in redirect_target else "?"
        return redirect(f"{redirect_target}{sep}status=success&userid={user_id}")

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        if conn:
            conn.close()


# ─────────────────────────────────────────────────────────────
# SAML — Status & Disconnect
# ─────────────────────────────────────────────────────────────


@azure_integration_bp.route("/saml/status", methods=["GET"])
def azure_saml_status():
    user_id = request.args.get("user_id") or _extract_user_id()

    ok, err = _admin_only_check(user_id)
    if not ok:
        return err

    session_row = _get_active_azure_session(user_id)
    if session_row:
        return jsonify(
            {
                "connected": True,
                "azure_tenant_id": session_row.get("azure_tenant_id"),
                "azure_upn": session_row.get("azure_upn"),
                "azure_region": session_row.get("azure_region"),
                "scope": session_row.get("scope"),
                "expires_at": str(session_row.get("expires_at")),
            }
        )
    return jsonify({"connected": False})


@azure_integration_bp.route("/saml/disconnect", methods=["POST"])
def azure_saml_disconnect():
    data = request.get_json(force=True) or {}
    user_id = data.get("user_id") or _extract_user_id()

    ok, err = _admin_only_check(user_id)
    if not ok:
        return err

    conn = None
    try:
        conn = connect_to_rds()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM azure_saml_sessions WHERE user_id=%s", (user_id,))
        conn.commit()

        log_audit_event(
            action=AZURE_SAML_DISCONNECTED,
            endpoint="/azure/saml/disconnect",
            ip=request.remote_addr,
            status="success",
            actor_user_id=user_id,
        )
        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

    finally:
        if conn:
            conn.close()


# ─────────────────────────────────────────────────────────────
# Connector — Test (ad-hoc, no DB record required)
# ─────────────────────────────────────────────────────────────


@azure_integration_bp.route("/connector/test", methods=["POST"])
def azure_connector_test():
    try:
        data = request.get_json(force=True) or {}
        user_id = data.get("user_id") or _extract_user_id()

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        base_url = data.get("base_url", "")
        if not base_url:
            return jsonify({"success": False, "error": "base_url required"}), 400

        auth = data.get("auth", {})
        test_req = data.get("test_request", data)

        path = test_req.get("path", "/")
        path_params = test_req.get("path_params", {})
        method = test_req.get("method", "GET").upper()
        headers = test_req.get("headers", {})
        query_params = test_req.get("query_params", {})
        body = test_req.get("body")

        # Resolve auth — fall back to stored SAML session for azure_oauth
        auth_type = auth.get("type", "azure_oauth")
        if auth_type == "azure_oauth" and not auth.get("access_token"):
            auth = _resolve_azure_auth({}, user_id)

        full_url = base_url.rstrip("/") + path
        for k, v in path_params.items():
            full_url = full_url.replace(f"{{{k}}}", str(v))

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

        result = APIConnector(userid=user_id, config=config).execute()
        return jsonify(
            {
                "success": True,
                "request": {"url": full_url, "method": method},
                "response": result,
            }
        )

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ─────────────────────────────────────────────────────────────
# Connector — Apps CRUD
# ─────────────────────────────────────────────────────────────


@azure_integration_bp.route("/connector/apps", methods=["POST"])
def azure_create_app():
    conn = None
    try:
        data = request.get_json(force=True) or {}
        user_id = data.get("user_id") or _extract_user_id()

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        app_name = (data.get("app_name") or "").strip()
        base_url = (data.get("base_url") or "").strip()
        auth_type = data.get("auth_type", "azure_oauth")
        auth_config = data.get("auth_config") or {}
        headers = data.get("headers") or {}
        query_params = data.get("query_params") or {}
        path_params = data.get("path_params") or {}
        timeout_seconds = int(data.get("timeout_seconds") or 10)
        retry_count = int(data.get("retry_count") or 0)
        retry_backoff_seconds = int(data.get("retry_backoff_seconds") or 0)

        if not app_name:
            return jsonify({"success": False, "error": "app_name required"}), 400
        if not base_url:
            return jsonify({"success": False, "error": "base_url required"}), 400

        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT id FROM azure_external_apps WHERE user_id=%s AND app_name=%s LIMIT 1",
                (user_id, app_name),
            )
            existing = cur.fetchone()

            if existing:
                existing_id = existing["id"]
                cur.execute(
                    """
                    SELECT id, app_name, provider, base_url, auth_type, status,
                           last_test_status, last_tested_at, created_at, updated_at
                    FROM azure_external_apps WHERE id=%s
                    """,
                    (existing_id,),
                )
                existing_app = cur.fetchone()
                for field in ("last_tested_at", "created_at", "updated_at"):
                    if existing_app.get(field):
                        existing_app[field] = str(existing_app[field])
                conn.close()
                return jsonify(
                    {
                        "success": True,
                        "already_exists": True,
                        "app": existing_app,
                        "message": f"'{app_name}' is already in your app list. You can manage it from there.",
                    }
                )

            cur.execute(
                """
                INSERT INTO azure_external_apps
                    (user_id, app_name, base_url, auth_type, auth_config,
                     headers, query_params, path_params,
                     timeout_seconds, retry_count, retry_backoff_seconds, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active')
                """,
                (
                    user_id,
                    app_name,
                    base_url,
                    auth_type,
                    json.dumps(auth_config),
                    json.dumps(headers),
                    json.dumps(query_params),
                    json.dumps(path_params),
                    timeout_seconds,
                    retry_count,
                    retry_backoff_seconds,
                ),
            )
            app_id = cur.lastrowid
        conn.commit()

        return jsonify(
            {
                "success": True,
                "already_exists": False,
                "app_id": app_id,
                "app_name": app_name,
                "message": "App created. Use the Test button to verify connectivity.",
            }
        )

    except ValueError as ve:
        return jsonify({"success": False, "error": str(ve)}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@azure_integration_bp.route("/connector/apps/<user_id>", methods=["GET"])
def azure_list_apps(user_id):
    ok, err = _admin_only_check(user_id)
    if not ok:
        return err

    conn = None
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                """
                SELECT id, app_name, provider, base_url, auth_type, status,
                       is_universal, source_global_azure_app_id,
                       last_test_status, last_tested_at, created_at, updated_at
                FROM azure_external_apps
                WHERE user_id=%s
                ORDER BY created_at DESC
                """,
                (user_id,),
            )
            apps = cur.fetchall()

        for app in apps:
            if app.get("last_tested_at"):
                app["last_tested_at"] = str(app["last_tested_at"])
            if app.get("created_at"):
                app["created_at"] = str(app["created_at"])
            if app.get("updated_at"):
                app["updated_at"] = str(app["updated_at"])

        return jsonify({"success": True, "apps": apps})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@azure_integration_bp.route("/connector/apps/<int:app_id>", methods=["PUT"])
def azure_update_app(app_id):
    conn = None
    try:
        data = request.get_json(force=True) or {}
        user_id = data.get("user_id") or _extract_user_id()

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT user_id FROM azure_external_apps WHERE id=%s", (app_id,)
            )
            row = cur.fetchone()

        if not row or row["user_id"] != user_id:
            return jsonify({"success": False, "error": "App not found"}), 404

        fields = {}
        for field in (
            "app_name",
            "base_url",
            "auth_type",
            "status",
            "timeout_seconds",
            "retry_count",
            "retry_backoff_seconds",
        ):
            if field in data:
                fields[field] = data[field]
        for json_field in ("auth_config", "headers", "query_params", "path_params"):
            if json_field in data:
                fields[json_field] = json.dumps(data[json_field])

        if not fields:
            return jsonify({"success": False, "error": "No fields to update"}), 400

        set_clause = ", ".join(f"{k}=%s" for k in fields)
        values = list(fields.values()) + [app_id, user_id]

        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE azure_external_apps SET {set_clause}, updated_at=NOW() WHERE id=%s AND user_id=%s",
                values,
            )
        conn.commit()
        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@azure_integration_bp.route("/connector/apps/<int:app_id>", methods=["DELETE"])
def azure_delete_app(app_id):
    conn = None
    try:
        data = request.get_json(force=True) or {}
        user_id = (
            data.get("user_id") or _extract_user_id() or request.args.get("user_id")
        )

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        conn = connect_to_rds()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM azure_external_apps WHERE id=%s AND user_id=%s",
                (app_id, user_id),
            )
        conn.commit()
        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@azure_integration_bp.route("/connector/apps/<int:app_id>/hard", methods=["DELETE"])
def azure_hard_delete_app(app_id):
    conn = None
    try:
        data = request.get_json(force=True) or {}
        user_id = (
            data.get("user_id") or _extract_user_id() or request.args.get("user_id")
        )

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        conn = connect_to_rds()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM azure_external_apps WHERE id=%s AND user_id=%s",
                (app_id, user_id),
            )
        conn.commit()
        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@azure_integration_bp.route("/connector/apps/<int:app_id>/test", methods=["POST"])
def azure_test_app(app_id):
    conn = None
    try:
        data = request.get_json(force=True) or {}
        user_id = data.get("user_id") or _extract_user_id()

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM azure_external_apps WHERE id=%s AND user_id=%s",
                (app_id, user_id),
            )
            app = cur.fetchone()

        if not app:
            return jsonify({"success": False, "error": "App not found"}), 404

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
            "retry": {"count": 1, "backoff": 1},
        }

        result = APIConnector(userid=user_id, config=config).execute()
        status = "success" if result.get("success") else "failed"

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE azure_external_apps
                SET last_test_status=%s, last_tested_at=NOW(),
                    last_error=%s, updated_at=NOW()
                WHERE id=%s
                """,
                (
                    status,
                    json.dumps(result) if not result.get("success") else None,
                    app_id,
                ),
            )
        conn.commit()

        return jsonify({"success": True, "test_result": result})

    except ValueError as ve:
        return jsonify({"success": False, "error": str(ve)}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@azure_integration_bp.route("/connector/apps/<int:app_id>/execute", methods=["POST"])
def azure_execute_app(app_id):
    try:
        data = request.get_json(force=True) or {}
        user_id = data.get("user_id") or _extract_user_id()

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        result = asyncio.run(_execute_azure_app_internal(app_id, user_id))
        return jsonify({"success": True, "result": result})

    except ValueError as ve:
        return jsonify({"success": False, "error": str(ve)}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ─────────────────────────────────────────────────────────────
# Connector — Endpoints CRUD
# ─────────────────────────────────────────────────────────────


@azure_integration_bp.route("/connector/apps/<int:app_id>/endpoints", methods=["POST"])
def azure_create_endpoint(app_id):
    conn = None
    try:
        data = request.get_json(force=True) or {}
        user_id = data.get("user_id") or _extract_user_id()

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        name = (data.get("name") or "").strip()
        path = (data.get("path") or "").strip()
        method = data.get("method", "GET").upper()
        headers = data.get("headers") or {}
        query_params = data.get("query_params") or {}
        path_params = data.get("path_params") or {}
        body_template = data.get("body_template")
        timeout_seconds = data.get("timeout_seconds")

        if not name:
            return jsonify({"success": False, "error": "name required"}), 400
        if not path:
            return jsonify({"success": False, "error": "path required"}), 400

        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT id FROM azure_external_apps WHERE id=%s AND user_id=%s AND status='active'",
                (app_id, user_id),
            )
            if not cur.fetchone():
                return jsonify({"success": False, "error": "App not found"}), 404

            cur.execute(
                """
                INSERT INTO azure_external_app_endpoints
                    (app_id, user_id, name, path, method, headers,
                     query_params, path_params, body_template, timeout_seconds)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    app_id,
                    user_id,
                    name,
                    path,
                    method,
                    json.dumps(headers),
                    json.dumps(query_params),
                    json.dumps(path_params),
                    json.dumps(body_template) if body_template else None,
                    timeout_seconds,
                ),
            )
            endpoint_id = cur.lastrowid
        conn.commit()

        return jsonify({"success": True, "endpoint_id": endpoint_id, "name": name})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@azure_integration_bp.route("/connector/apps/<int:app_id>/endpoints", methods=["GET"])
def azure_list_endpoints(app_id):
    user_id = request.args.get("user_id") or _extract_user_id()

    ok, err = _admin_only_check(user_id)
    if not ok:
        return err

    conn = None
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                """
                SELECT id, name, path, method, headers, query_params,
                       path_params, body_template, timeout_seconds, is_active,
                       last_test_status, last_tested_at, created_at, updated_at
                FROM azure_external_app_endpoints
                WHERE app_id=%s AND is_active=1
                ORDER BY created_at ASC
                """,
                (app_id,),
            )
            endpoints = cur.fetchall()

        for ep in endpoints:
            for dt_field in ("last_tested_at", "created_at", "updated_at"):
                if ep.get(dt_field):
                    ep[dt_field] = str(ep[dt_field])
            for json_field in (
                "headers",
                "query_params",
                "path_params",
                "body_template",
            ):
                if ep.get(json_field):
                    try:
                        ep[json_field] = json.loads(ep[json_field])
                    except Exception:
                        pass

        return jsonify({"success": True, "endpoints": endpoints})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@azure_integration_bp.route("/connector/endpoints/<int:endpoint_id>", methods=["PUT"])
def azure_update_endpoint(endpoint_id):
    conn = None
    try:
        data = request.get_json(force=True) or {}
        user_id = data.get("user_id") or _extract_user_id()

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        fields = {}
        for field in ("name", "path", "method", "timeout_seconds", "is_active"):
            if field in data:
                fields[field] = data[field]
        for json_field in ("headers", "query_params", "path_params", "body_template"):
            if json_field in data:
                fields[json_field] = json.dumps(data[json_field])

        if not fields:
            return jsonify({"success": False, "error": "No fields to update"}), 400

        set_clause = ", ".join(f"{k}=%s" for k in fields)
        values = list(fields.values()) + [endpoint_id, user_id]

        conn = connect_to_rds()
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE azure_external_app_endpoints SET {set_clause}, updated_at=NOW() WHERE id=%s AND user_id=%s",
                values,
            )
        conn.commit()
        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@azure_integration_bp.route(
    "/connector/endpoints/<int:endpoint_id>", methods=["DELETE"]
)
def azure_delete_endpoint(endpoint_id):
    conn = None
    try:
        data = request.get_json(force=True) or {}
        user_id = (
            data.get("user_id") or _extract_user_id() or request.args.get("user_id")
        )

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        conn = connect_to_rds()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM azure_external_app_endpoints WHERE id=%s AND user_id=%s",
                (endpoint_id, user_id),
            )
        conn.commit()
        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


# ─────────────────────────────────────────────────────────────
# Connector — Cleanup inactive / duplicate records
# ─────────────────────────────────────────────────────────────


@azure_integration_bp.route("/connector/cleanup-inactive", methods=["DELETE"])
def azure_cleanup_inactive():
    """Hard-deletes inactive apps and deactivated endpoints for the calling admin."""
    conn = None
    try:
        data = request.get_json(force=True) or {}
        user_id = (
            data.get("user_id") or _extract_user_id() or request.args.get("user_id")
        )

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        conn = connect_to_rds()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM azure_external_app_endpoints WHERE user_id=%s AND is_active=0",
                (user_id,),
            )
            deleted_endpoints = cur.rowcount

            cur.execute(
                "DELETE FROM azure_external_apps WHERE user_id=%s AND status='inactive'",
                (user_id,),
            )
            deleted_apps = cur.rowcount

        conn.commit()
        return jsonify(
            {
                "success": True,
                "deleted_apps": deleted_apps,
                "deleted_endpoints": deleted_endpoints,
            }
        )

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


# ─────────────────────────────────────────────────────────────
# Connector — Endpoint test & execute
# ─────────────────────────────────────────────────────────────


@azure_integration_bp.route(
    "/connector/endpoints/<int:endpoint_id>/test", methods=["POST"]
)
def azure_test_endpoint(endpoint_id):
    conn = None
    try:
        data = request.get_json(force=True) or {}
        user_id = data.get("user_id") or _extract_user_id()

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                """
                SELECT e.*, a.base_url, a.auth_config,
                       a.timeout_seconds AS app_timeout,
                       a.retry_count, a.retry_backoff_seconds
                FROM azure_external_app_endpoints e
                JOIN azure_external_apps a ON a.id = e.app_id
                WHERE e.id=%s AND e.is_active=1
                """,
                (endpoint_id,),
            )
            row = cur.fetchone()

        if not row:
            return jsonify({"success": False, "error": "Endpoint not found"}), 404

        raw_auth = json.loads(row["auth_config"] or "{}")
        auth_config = _resolve_azure_auth(raw_auth, user_id)

        runtime_params = {
            "headers": data.get("headers", {}),
            "query_params": data.get("query_params", {}),
            "path_params": data.get("path_params", {}),
            "body": data.get("body"),
        }

        import re

        path = row["path"]
        final_path_params = {
            **json.loads(row["path_params"] or "{}"),
            **runtime_params["path_params"],
        }
        for var in re.findall(r"\{(.*?)\}", path):
            if var not in final_path_params:
                return (
                    jsonify(
                        {"success": False, "error": f"Missing path parameter: {var}"}
                    ),
                    400,
                )
            path = path.replace(f"{{{var}}}", str(final_path_params[var]))

        full_url = row["base_url"].rstrip("/") + path

        config = {
            "auth": auth_config,
            "request": {
                "url": full_url,
                "method": row["method"],
                "headers": {
                    **json.loads(row["headers"] or "{}"),
                    **runtime_params["headers"],
                },
                "query_params": {
                    **json.loads(row["query_params"] or "{}"),
                    **runtime_params["query_params"],
                },
                "body": runtime_params["body"]
                or json.loads(row["body_template"] or "null"),
            },
            "timeout": row.get("timeout_seconds") or row.get("app_timeout") or 10,
            "retry": {"count": 1, "backoff": 1},
        }

        result = APIConnector(userid=user_id, config=config).execute()
        status = "success" if result.get("success") else "failed"

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE azure_external_app_endpoints
                SET last_test_status=%s, last_tested_at=NOW(),
                    last_error=%s, updated_at=NOW()
                WHERE id=%s
                """,
                (
                    status,
                    json.dumps(result) if not result.get("success") else None,
                    endpoint_id,
                ),
            )
        conn.commit()

        return jsonify({"success": True, "test_result": result})

    except ValueError as ve:
        return jsonify({"success": False, "error": str(ve)}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@azure_integration_bp.route(
    "/connector/endpoints/<int:endpoint_id>/execute", methods=["POST"]
)
def azure_execute_endpoint(endpoint_id):
    try:
        data = request.get_json(force=True) or {}
        user_id = data.get("user_id") or _extract_user_id()

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        runtime_params = {
            "headers": data.get("headers", {}),
            "query_params": data.get("query_params", {}),
            "path_params": data.get("path_params", {}),
            "body": data.get("body"),
            "timeout": data.get("timeout"),
        }

        result = asyncio.run(
            _execute_azure_endpoint_internal(endpoint_id, user_id, runtime_params)
        )
        return jsonify({"success": True, "result": result})

    except ValueError as ve:
        return jsonify({"success": False, "error": str(ve)}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ─────────────────────────────────────────────────────────────
# Connector — Run history (S3)
# ─────────────────────────────────────────────────────────────


@azure_integration_bp.route(
    "/connector/endpoints/<int:endpoint_id>/runs", methods=["GET"]
)
def azure_list_endpoint_runs(endpoint_id):
    try:
        user_id = request.args.get("user_id") or _extract_user_id()

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT app_id FROM azure_external_app_endpoints WHERE id=%s",
                (endpoint_id,),
            )
            row = cur.fetchone()
        conn.close()

        if not row:
            return jsonify({"success": False, "error": "Endpoint not found"}), 404

        app_id = row["app_id"]
        prefix = f"{user_id}/azure_connector/{app_id}/{endpoint_id}/"
        files = getallendpointdetails(prefix)
        return jsonify({"success": True, "runs": files})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@azure_integration_bp.route(
    "/connector/endpoints/<int:endpoint_id>/runs/<path:filename>", methods=["GET"]
)
def azure_get_endpoint_run(endpoint_id, filename):
    try:
        user_id = request.args.get("user_id") or _extract_user_id()

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT app_id FROM azure_external_app_endpoints WHERE id=%s",
                (endpoint_id,),
            )
            row = cur.fetchone()
        conn.close()

        if not row:
            return jsonify({"success": False, "error": "Endpoint not found"}), 404

        app_id = row["app_id"]
        key = f"{user_id}/azure_connector/{app_id}/{endpoint_id}/{filename}"
        data = get_filedata_endp(key)
        return jsonify({"success": True, "data": data})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ─────────────────────────────────────────────────────────────
# Connector — Scheduling (uses AzureAPIConnectorScheduler)
# ─────────────────────────────────────────────────────────────


@azure_integration_bp.route(
    "/connector/endpoints/<int:endpoint_id>/schedule", methods=["POST"]
)
def azure_schedule_endpoint(endpoint_id):
    try:
        body = request.get_json(force=True) or {}
        user_id = body.get("user_id") or _extract_user_id()

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        activation = body.get("scheduledActivation")
        schedule_type, data = resolve_schedule_from_activation(activation)
        timezone = data.get("timezone", "UTC")

        existing = _get_azure_schedule_endpointdetails(endpoint_id)
        if existing:
            celery_type = existing.get("celery_type", "")
            celery_id = existing.get("celery_task_id", "")
            celery_entry = existing.get("celery_entry", "")
            celery_task_ids = existing.get("celery_task_ids", [])

            if celery_type == "task" and celery_id:
                AzureAPIConnectorScheduler.revoke_task(celery_id)
            elif celery_type == "beat" and celery_entry:
                AzureAPIConnectorScheduler.disable_celery_entry(celery_entry)
            elif celery_type == "tasks" and celery_task_ids:
                for tid in celery_task_ids:
                    AzureAPIConnectorScheduler.revoke_task(tid)
            elif celery_id:
                AzureAPIConnectorScheduler.revoke_task(celery_id)

        if schedule_type == "one_time":
            dt = datetime.fromisoformat(data["datetime"])
            result = asyncio.run(
                AzureAPIConnectorScheduler.schedule_endpoint_once(
                    user_id, endpoint_id, dt, timezone
                )
            )
        elif schedule_type == "daily":
            hour, minute = map(int, data["startTime"].split(":"))
            result = asyncio.run(
                AzureAPIConnectorScheduler.schedule_endpoint_daily(
                    user_id, endpoint_id, hour, minute, timezone
                )
            )
        elif schedule_type == "weekly":
            hour, minute = map(int, data["startTime"].split(":"))
            result = asyncio.run(
                AzureAPIConnectorScheduler.schedule_endpoint_weekly(
                    user_id, endpoint_id, data["weekday"], hour, minute, timezone
                )
            )
        elif schedule_type == "monthly":
            hour, minute = map(int, data["startTime"].split(":"))
            result = asyncio.run(
                AzureAPIConnectorScheduler.schedule_endpoint_monthly(
                    user_id, endpoint_id, data["day"], hour, minute, timezone
                )
            )
        elif schedule_type == "interval":
            result = asyncio.run(
                AzureAPIConnectorScheduler.schedule_endpoint_interval(
                    user_id, endpoint_id, data["seconds"]
                )
            )
        elif schedule_type == "custom":
            dates = expand_custom_dates(
                start_date=data["startDate"],
                end_date=data["endDate"],
                start_time=data["startTime"],
            )
            result = asyncio.run(
                AzureAPIConnectorScheduler.schedule_endpoint_custom_dates(
                    user_id, endpoint_id, dates, timezone
                )
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
            f"azure_endpoint:{endpoint_id}:{user_id}:{schedule_type}"
        )

        _save_azure_endpoint_schedule(endpoint_id, schedule_record)
        return jsonify({"success": True, "schedule": schedule_record})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ─────────────────────────────────────────────────────────────
# Global Azure Apps (templates curated by service@bytoid.ca)
# ─────────────────────────────────────────────────────────────


@azure_integration_bp.route("/admin/pushapp", methods=["POST"])
def azure_push_global_app():
    """Promote a local azure_external_apps row to a global_azure_apps template.
    Restricted to ACCESSIBLE_IDS."""
    body = request.json or {}
    user_id = body.get("user_id") or _extract_user_id()
    admin_app_id = body.get("app_id")

    if not user_id:
        return jsonify({"success": False, "error": "user_id required"}), 400
    if not admin_app_id:
        return jsonify({"success": False, "error": "app_id required"}), 400

    ok, err = _admin_only_check(user_id)
    if not ok:
        return err
    if user_id not in ACCESSIBLE_IDS:
        return (
            jsonify(
                {
                    "success": False,
                    "error": "Only service@bytoid.ca can push global Azure apps.",
                }
            ),
            403,
        )

    conn = None
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM azure_external_apps WHERE id=%s AND user_id=%s",
                (admin_app_id, user_id),
            )
            local = cur.fetchone()
            if not local:
                return (
                    jsonify({"success": False, "error": "Local Azure app not found"}),
                    404,
                )

            app_name = (body.get("app_name") or local["app_name"]).strip()
            base_url = (body.get("base_url") or local["base_url"]).strip()
            status = body.get("status", "development")
            notes = body.get("notes")
            required_config_schema = body.get("required_config_schema")

            cur.execute(
                """
                INSERT INTO global_azure_apps (
                    app_name, provider, base_url, auth_type, auth_config,
                    headers, method, query_params, path_params,
                    timeout_seconds, is_universal, status, notes, required_config_schema
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    app_name,
                    local.get("provider", "azure"),
                    base_url,
                    local.get("auth_type", "azure_oauth"),
                    json.dumps({}),  # never bake credentials
                    json.dumps(_parse_json_field(local.get("headers")) or {}),
                    local.get("method", "GET"),
                    json.dumps(_parse_json_field(local.get("query_params")) or {}),
                    json.dumps(_parse_json_field(local.get("path_params")) or {}),
                    local.get("timeout_seconds") or 10,
                    True,
                    status,
                    notes,
                    (
                        json.dumps(required_config_schema)
                        if required_config_schema is not None
                        else None
                    ),
                ),
            )
            new_global_app_id = cur.lastrowid

            cur.execute(
                """
                UPDATE azure_external_apps
                SET is_universal=1, source_global_azure_app_id=%s, updated_at=NOW()
                WHERE id=%s AND user_id=%s
                """,
                (new_global_app_id, admin_app_id, user_id),
            )

            cur.execute(
                "SELECT * FROM azure_external_app_endpoints WHERE app_id=%s AND user_id=%s",
                (admin_app_id, user_id),
            )
            local_endpoints = cur.fetchall()
            endpoints_pushed = 0
            for ep in local_endpoints:
                try:
                    cur.execute(
                        """
                        INSERT INTO global_azure_app_endpoints (
                            app_id, name, path, method,
                            headers, query_params, path_params, body_template,
                            timeout_seconds, is_active, status, notes
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            new_global_app_id,
                            ep["name"],
                            ep["path"],
                            ep.get("method", "GET"),
                            json.dumps(_parse_json_field(ep.get("headers")) or {}),
                            json.dumps(_parse_json_field(ep.get("query_params")) or {}),
                            json.dumps(_parse_json_field(ep.get("path_params")) or {}),
                            json.dumps(
                                _parse_json_field(ep.get("body_template")) or {}
                            ),
                            ep.get("timeout_seconds"),
                            bool(ep.get("is_active", True)),
                            status,
                            None,
                        ),
                    )
                    endpoints_pushed += 1
                except pymysql.err.IntegrityError:
                    continue

        conn.commit()
        return jsonify(
            {
                "success": True,
                "global_app_id": new_global_app_id,
                "endpoints_pushed": endpoints_pushed,
                "message": (
                    f"Azure app pushed to global with {endpoints_pushed} endpoint(s)."
                    if endpoints_pushed
                    else "Azure app pushed to global successfully."
                ),
            }
        )

    except pymysql.err.IntegrityError as ie:
        if conn:
            conn.rollback()
        msg = str(ie)
        if "uq_global_azure_app_name" in msg or "Duplicate entry" in msg:
            return (
                jsonify(
                    {
                        "success": False,
                        "error": f"A global Azure app named '{app_name}' already exists.",
                    }
                ),
                409,
            )
        return jsonify({"success": False, "error": msg}), 500
    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@azure_integration_bp.route("/admin/unpushapp", methods=["POST"])
def azure_unpush_global_app():
    """Downgrade a global Azure app back to local-only.
    Restricted to ACCESSIBLE_IDS."""
    body = request.json or {}
    user_id = body.get("user_id") or _extract_user_id()
    admin_app_id = body.get("app_id")

    if not user_id:
        return jsonify({"success": False, "error": "user_id required"}), 400
    if not admin_app_id:
        return jsonify({"success": False, "error": "app_id required"}), 400

    ok, err = _admin_only_check(user_id)
    if not ok:
        return err
    if user_id not in ACCESSIBLE_IDS:
        return (
            jsonify(
                {
                    "success": False,
                    "error": "Only service@bytoid.ca can downgrade global Azure apps.",
                }
            ),
            403,
        )

    conn = None
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT source_global_azure_app_id FROM azure_external_apps WHERE id=%s AND user_id=%s",
                (admin_app_id, user_id),
            )
            row = cur.fetchone()
            if not row:
                return (
                    jsonify({"success": False, "error": "Local Azure app not found"}),
                    404,
                )
            global_app_id = row.get("source_global_azure_app_id")
            if not global_app_id:
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": "This app is not linked to a Global template.",
                        }
                    ),
                    400,
                )

            cur.execute(
                "SELECT COUNT(*) AS cnt FROM azure_external_apps WHERE source_global_azure_app_id=%s AND user_id<>%s",
                (global_app_id, user_id),
            )
            other_installs = cur.fetchone()["cnt"]

            cur.execute(
                """
                UPDATE azure_external_apps
                SET is_universal=0, source_global_azure_app_id=NULL, updated_at=NOW()
                WHERE id=%s AND user_id=%s
                """,
                (admin_app_id, user_id),
            )
            cur.execute(
                """
                UPDATE azure_external_apps
                SET is_universal=0, source_global_azure_app_id=NULL, updated_at=NOW()
                WHERE source_global_azure_app_id=%s
                """,
                (global_app_id,),
            )
            cur.execute("DELETE FROM global_azure_apps WHERE id=%s", (global_app_id,))

        conn.commit()
        return jsonify(
            {
                "success": True,
                "global_app_id": global_app_id,
                "other_installs_detached": other_installs,
                "message": (
                    f"Downgraded to local. {other_installs} other admin installation(s) detached."
                    if other_installs
                    else "Downgraded to local."
                ),
            }
        )

    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@azure_integration_bp.route("/admin/pushapp_endpoint", methods=["POST"])
def azure_push_global_app_endpoint():
    """Promote a local azure_external_app_endpoints row to global_azure_app_endpoints."""
    body = request.json or {}
    user_id = body.get("user_id") or _extract_user_id()
    admin_app_id = body.get("app_id")
    admin_endpoint_id = body.get("endpoint_id")

    if not user_id:
        return jsonify({"success": False, "error": "user_id required"}), 400
    if not admin_app_id:
        return jsonify({"success": False, "error": "app_id required"}), 400

    ok, err = _admin_only_check(user_id)
    if not ok:
        return err
    if user_id not in ACCESSIBLE_IDS:
        return (
            jsonify(
                {
                    "success": False,
                    "error": "Only service@bytoid.ca can push global Azure endpoints.",
                }
            ),
            403,
        )

    conn = None
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                """
                SELECT source_global_azure_app_id
                FROM azure_external_apps
                WHERE id=%s AND user_id=%s
                """,
                (admin_app_id, user_id),
            )
            row = cur.fetchone()
            if not row or not row["source_global_azure_app_id"]:
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": "App is not global. Push the app to global first.",
                        }
                    ),
                    400,
                )
            global_app_id = row["source_global_azure_app_id"]

            local_ep = None
            if admin_endpoint_id:
                cur.execute(
                    "SELECT * FROM azure_external_app_endpoints WHERE id=%s AND app_id=%s AND user_id=%s",
                    (admin_endpoint_id, admin_app_id, user_id),
                )
                local_ep = cur.fetchone()
                if not local_ep:
                    return (
                        jsonify(
                            {"success": False, "error": "Local endpoint not found"}
                        ),
                        404,
                    )

            def pick(field, default=None):
                if body.get(field) is not None:
                    return body[field]
                if local_ep is not None:
                    return local_ep.get(field, default)
                return default

            name = (pick("name") or "").strip() if pick("name") else None
            path = pick("path")
            if not name or not path:
                return (
                    jsonify({"success": False, "error": "name and path required"}),
                    400,
                )

            cur.execute(
                """
                INSERT INTO global_azure_app_endpoints (
                    app_id, name, path, method,
                    headers, query_params, path_params, body_template,
                    timeout_seconds, is_active, status, notes, required_config_schema
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    global_app_id,
                    name,
                    path,
                    pick("method", "GET"),
                    json.dumps(_parse_json_field(pick("headers")) or {}),
                    json.dumps(_parse_json_field(pick("query_params")) or {}),
                    json.dumps(_parse_json_field(pick("path_params")) or {}),
                    json.dumps(_parse_json_field(pick("body_template")) or {}),
                    pick("timeout_seconds"),
                    bool(pick("is_active", True)),
                    body.get("status", "development"),
                    body.get("notes"),
                    (
                        json.dumps(body["required_config_schema"])
                        if body.get("required_config_schema") is not None
                        else None
                    ),
                ),
            )
            new_global_endpoint_id = cur.lastrowid
        conn.commit()
        return jsonify(
            {
                "success": True,
                "global_endpoint_id": new_global_endpoint_id,
                "global_app_id": global_app_id,
            }
        )

    except pymysql.err.IntegrityError as ie:
        if conn:
            conn.rollback()
        msg = str(ie)
        if "Duplicate entry" in msg:
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "An endpoint with the same name or (path, method) already exists in the global app.",
                    }
                ),
                409,
            )
        return jsonify({"success": False, "error": msg}), 500
    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@azure_integration_bp.route("/global/apps/<user_id>", methods=["GET"])
def azure_list_global_apps(user_id):
    """List global Azure apps with installed-state for the given user.
    Non-admins see only status='ready' rows. service@ sees all statuses."""
    ok, err = _admin_only_check(user_id)
    if not ok:
        return err

    is_service_admin = user_id in ACCESSIBLE_IDS
    conn = None
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            if is_service_admin:
                cur.execute(
                    """
                    SELECT g.*,
                           ea.id        AS external_app_id,
                           ea.created_at AS installed_on
                    FROM global_azure_apps g
                    LEFT JOIN azure_external_apps ea
                      ON ea.source_global_azure_app_id = g.id
                     AND ea.user_id = %s
                    ORDER BY g.created_at DESC
                    """,
                    (user_id,),
                )
            else:
                cur.execute(
                    """
                    SELECT g.*,
                           ea.id        AS external_app_id,
                           ea.created_at AS installed_on
                    FROM global_azure_apps g
                    LEFT JOIN azure_external_apps ea
                      ON ea.source_global_azure_app_id = g.id
                     AND ea.user_id = %s
                    WHERE g.status = 'ready'
                    ORDER BY g.created_at DESC
                    """,
                    (user_id,),
                )
            rows = cur.fetchall()

        apps = []
        for r in rows:
            app = dict(r)
            app["installed"] = bool(r.get("external_app_id"))
            app["installed_local_app_id"] = r.get("external_app_id")
            app["installed_on"] = r.get("installed_on")
            app.pop("external_app_id", None)
            apps.append(app)

        return jsonify({"success": True, "apps": apps, "is_admin": is_service_admin})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@azure_integration_bp.route(
    "/global/app_endpoints/<string:user_id>/<int:app_id>", methods=["GET"]
)
def azure_list_global_app_endpoints(user_id, app_id):
    """List endpoints for a single global Azure app.
    Non-admins see only status='ready' endpoints."""
    ok, err = _admin_only_check(user_id)
    if not ok:
        return err

    is_service_admin = user_id in ACCESSIBLE_IDS
    conn = None
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT id FROM global_azure_apps WHERE id=%s", (app_id,))
            if not cur.fetchone():
                return jsonify({"success": False, "error": "Global app not found"}), 404

            if is_service_admin:
                cur.execute(
                    "SELECT * FROM global_azure_app_endpoints WHERE app_id=%s ORDER BY created_at DESC",
                    (app_id,),
                )
            else:
                cur.execute(
                    """
                    SELECT * FROM global_azure_app_endpoints
                    WHERE app_id=%s AND status='ready' AND is_active=1
                    ORDER BY created_at DESC
                    """,
                    (app_id,),
                )
            endpoints = cur.fetchall()

            cur.execute(
                """
                SELECT e.path, e.method, e.id
                FROM azure_external_app_endpoints e
                JOIN azure_external_apps a ON a.id = e.app_id
                WHERE a.user_id=%s AND a.source_global_azure_app_id=%s
                """,
                (user_id, app_id),
            )
            installed_map = {(r["path"], r["method"]): r["id"] for r in cur.fetchall()}

        for ep in endpoints:
            local_id = installed_map.get((ep.get("path"), ep.get("method")))
            ep["installed"] = local_id is not None
            ep["installed_local_endpoint_id"] = local_id

        return jsonify(
            {"success": True, "endpoints": endpoints, "is_admin": is_service_admin}
        )
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@azure_integration_bp.route("/user/global-app/instantiate", methods=["POST"])
def azure_instantiate_global_app():
    """Clone a global_azure_apps row into azure_external_apps for the calling user.
    auth_config is always stored as {} — OAuth token is resolved from the live SAML
    session at execute-time."""
    body = request.json or {}
    user_id = body.get("user_id") or _extract_user_id()
    global_app_id = body.get("app_id")

    if not user_id or not global_app_id:
        return jsonify({"success": False, "error": "user_id and app_id required"}), 400

    ok, err = _admin_only_check(user_id)
    if not ok:
        return err

    conn = None
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT * FROM global_azure_apps WHERE id=%s", (global_app_id,))
            g = cur.fetchone()
            if not g:
                return jsonify({"success": False, "error": "Global app not found"}), 404

            cur.execute(
                "SELECT id FROM azure_external_apps WHERE user_id=%s AND source_global_azure_app_id=%s LIMIT 1",
                (user_id, global_app_id),
            )
            existing = cur.fetchone()
            if existing:
                return jsonify(
                    {
                        "success": True,
                        "already_installed": True,
                        "app_id": existing["id"],
                        "message": "Already installed.",
                    }
                )

            base_name = g["app_name"]
            app_name = base_name
            suffix = 2
            while True:
                cur.execute(
                    "SELECT id FROM azure_external_apps WHERE user_id=%s AND app_name=%s",
                    (user_id, app_name),
                )
                if not cur.fetchone():
                    break
                app_name = f"{base_name} ({suffix})"
                suffix += 1

            cur.execute(
                """
                INSERT INTO azure_external_apps (
                    user_id, app_name, provider, base_url, auth_type, auth_config,
                    headers, method, query_params, path_params,
                    timeout_seconds, retry_count, retry_backoff_seconds,
                    is_universal, source_global_azure_app_id, status
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    user_id,
                    app_name,
                    g.get("provider", "azure"),
                    g["base_url"],
                    g.get("auth_type", "azure_oauth"),
                    json.dumps({}),
                    json.dumps(_parse_json_field(g.get("headers")) or {}),
                    g.get("method", "GET"),
                    json.dumps(_parse_json_field(g.get("query_params")) or {}),
                    json.dumps(_parse_json_field(g.get("path_params")) or {}),
                    g.get("timeout_seconds") or 10,
                    0,
                    0,
                    1,
                    global_app_id,
                    "active",
                ),
            )
            new_app_id = cur.lastrowid

            cur.execute(
                """
                SELECT * FROM global_azure_app_endpoints
                WHERE app_id=%s AND is_active=1 AND status='ready'
                """,
                (global_app_id,),
            )
            endpoints = cur.fetchall()
            installed_count = 0

            for ep in endpoints:
                schema = _parse_json_field(ep.get("required_config_schema"))
                if schema:
                    requires = False
                    for k, v in schema.items():
                        if isinstance(v, dict) and v.get("required"):
                            requires = True
                            break
                    if requires:
                        continue

                cur.execute(
                    """
                    INSERT INTO azure_external_app_endpoints (
                        app_id, user_id, name, path, method,
                        headers, query_params, path_params, body_template,
                        timeout_seconds, is_active
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        new_app_id,
                        user_id,
                        ep["name"],
                        ep["path"],
                        ep.get("method", "GET"),
                        json.dumps(_parse_json_field(ep.get("headers")) or {}),
                        json.dumps(_parse_json_field(ep.get("query_params")) or {}),
                        json.dumps(_parse_json_field(ep.get("path_params")) or {}),
                        json.dumps(_parse_json_field(ep.get("body_template")) or {}),
                        ep.get("timeout_seconds"),
                        1,
                    ),
                )
                installed_count += 1

        conn.commit()
        return jsonify(
            {
                "success": True,
                "app_id": new_app_id,
                "app_name": app_name,
                "auto_installed_endpoints": installed_count,
                "message": "Global Azure app installed.",
            }
        )
    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@azure_integration_bp.route("/user/global-apps/install-all", methods=["POST"])
def azure_install_all_global_apps():
    """Install every ready global Azure app that the user has not yet installed."""
    body = request.json or {}
    user_id = body.get("user_id") or _extract_user_id()

    if not user_id:
        return jsonify({"success": False, "error": "user_id required"}), 400

    ok, err = _admin_only_check(user_id)
    if not ok:
        return err

    conn = None
    try:
        conn = connect_to_rds()
        apps_installed = 0
        apps_skipped = 0
        endpoints_installed = 0

        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM global_azure_apps WHERE status='ready' ORDER BY id",
            )
            global_apps = cur.fetchall()

            for g in global_apps:
                global_app_id = g["id"]

                cur.execute(
                    "SELECT id FROM azure_external_apps WHERE user_id=%s AND source_global_azure_app_id=%s LIMIT 1",
                    (user_id, global_app_id),
                )
                if cur.fetchone():
                    apps_skipped += 1
                    continue

                base_name = g["app_name"]
                app_name = base_name
                suffix = 2
                while True:
                    cur.execute(
                        "SELECT id FROM azure_external_apps WHERE user_id=%s AND app_name=%s",
                        (user_id, app_name),
                    )
                    if not cur.fetchone():
                        break
                    app_name = f"{base_name} ({suffix})"
                    suffix += 1

                cur.execute(
                    """
                    INSERT INTO azure_external_apps (
                        user_id, app_name, provider, base_url, auth_type, auth_config,
                        headers, method, query_params, path_params,
                        timeout_seconds, retry_count, retry_backoff_seconds,
                        is_universal, source_global_azure_app_id, status
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        user_id,
                        app_name,
                        g.get("provider", "azure"),
                        g["base_url"],
                        g.get("auth_type", "azure_oauth"),
                        json.dumps({}),
                        json.dumps(_parse_json_field(g.get("headers")) or {}),
                        g.get("method", "GET"),
                        json.dumps(_parse_json_field(g.get("query_params")) or {}),
                        json.dumps(_parse_json_field(g.get("path_params")) or {}),
                        g.get("timeout_seconds") or 10,
                        0,
                        0,
                        1,
                        global_app_id,
                        "active",
                    ),
                )
                new_app_id = cur.lastrowid
                apps_installed += 1

                cur.execute(
                    "SELECT * FROM global_azure_app_endpoints WHERE app_id=%s AND is_active=1 AND status='ready'",
                    (global_app_id,),
                )
                for ep in cur.fetchall():
                    schema = _parse_json_field(ep.get("required_config_schema"))
                    if schema and any(
                        isinstance(v, dict) and v.get("required")
                        for v in schema.values()
                    ):
                        continue
                    cur.execute(
                        """
                        INSERT INTO azure_external_app_endpoints (
                            app_id, user_id, name, path, method,
                            headers, query_params, path_params, body_template,
                            timeout_seconds, is_active
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            new_app_id,
                            user_id,
                            ep["name"],
                            ep["path"],
                            ep.get("method", "GET"),
                            json.dumps(_parse_json_field(ep.get("headers")) or {}),
                            json.dumps(_parse_json_field(ep.get("query_params")) or {}),
                            json.dumps(_parse_json_field(ep.get("path_params")) or {}),
                            json.dumps(
                                _parse_json_field(ep.get("body_template")) or {}
                            ),
                            ep.get("timeout_seconds"),
                            1,
                        ),
                    )
                    endpoints_installed += 1

        conn.commit()
        return jsonify(
            {
                "success": True,
                "apps_installed": apps_installed,
                "apps_skipped": apps_skipped,
                "endpoints_installed": endpoints_installed,
                "message": f"Installed {apps_installed} app(s), skipped {apps_skipped} already-installed.",
            }
        )
    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@azure_integration_bp.route("/user/global-endpoint/instantiate", methods=["POST"])
def azure_instantiate_global_endpoint():
    """Clone a single global_azure_app_endpoints row into the user's local app."""
    body = request.json or {}
    user_id = body.get("user_id") or _extract_user_id()
    global_endpoint_id = body.get("global_endpoint_id") or body.get("endpoint_id")

    if not user_id or not global_endpoint_id:
        return (
            jsonify(
                {"success": False, "error": "user_id and global_endpoint_id required"}
            ),
            400,
        )

    ok, err = _admin_only_check(user_id)
    if not ok:
        return err

    conn = None
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM global_azure_app_endpoints WHERE id=%s AND is_active=1",
                (global_endpoint_id,),
            )
            ge = cur.fetchone()
            if not ge:
                return (
                    jsonify({"success": False, "error": "Global endpoint not found"}),
                    404,
                )

            cur.execute(
                """
                SELECT id FROM azure_external_apps
                WHERE user_id=%s AND source_global_azure_app_id=%s
                LIMIT 1
                """,
                (user_id, ge["app_id"]),
            )
            parent = cur.fetchone()
            if not parent:
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": "Parent global app is not installed for this user. Install the app first.",
                        }
                    ),
                    400,
                )
            local_app_id = parent["id"]

            cur.execute(
                """
                SELECT id FROM azure_external_app_endpoints
                WHERE app_id=%s AND ((path=%s AND method=%s) OR name=%s)
                LIMIT 1
                """,
                (local_app_id, ge["path"], ge.get("method", "GET"), ge["name"]),
            )
            existing = cur.fetchone()
            if existing:
                return jsonify(
                    {
                        "success": True,
                        "already_installed": True,
                        "endpoint_id": existing["id"],
                    }
                )

            cur.execute(
                """
                INSERT INTO azure_external_app_endpoints (
                    app_id, user_id, name, path, method,
                    headers, query_params, path_params, body_template,
                    timeout_seconds, is_active
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    local_app_id,
                    user_id,
                    ge["name"],
                    ge["path"],
                    ge.get("method", "GET"),
                    json.dumps(_parse_json_field(ge.get("headers")) or {}),
                    json.dumps(_parse_json_field(ge.get("query_params")) or {}),
                    json.dumps(_parse_json_field(ge.get("path_params")) or {}),
                    json.dumps(_parse_json_field(ge.get("body_template")) or {}),
                    ge.get("timeout_seconds"),
                    1,
                ),
            )
            new_endpoint_id = cur.lastrowid
        conn.commit()
        return jsonify(
            {
                "success": True,
                "endpoint_id": new_endpoint_id,
                "local_app_id": local_app_id,
            }
        )
    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


# ─────────────────────────────────────────────────────────────
# Unit Test Results API (service@bytoid.ca only)
# ─────────────────────────────────────────────────────────────

_RESULTS_FILE = os.path.join(
    os.path.dirname(__file__), "..", "testing", "results", "latest.json"
)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read_test_results():
    """Load and normalise the pytest-json-report output file."""
    path = os.path.abspath(_RESULTS_FILE)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)

    summary = raw.get("summary", {})
    tests = [
        {
            "nodeid": t.get("nodeid"),
            "outcome": t.get("outcome"),
            "duration": round(t.get("duration", 0), 4),
            "longrepr": (t.get("call") or {}).get("longrepr"),
        }
        for t in raw.get("tests", [])
    ]
    return {
        "summary": {
            "total": summary.get("total", 0),
            "passed": summary.get("passed", 0),
            "failed": summary.get("failed", 0),
            "error": summary.get("error", 0),
            "duration": round(raw.get("duration", 0), 3),
            "created": raw.get("created"),
        },
        "tests": tests,
    }


@azure_integration_bp.route("/test-results", methods=["GET"])
def azure_get_test_results():
    user_id = request.args.get("user_id") or _extract_user_id()
    if not user_id or user_id not in ACCESSIBLE_IDS:
        return (
            jsonify(
                {"success": False, "error": "Access restricted to service@bytoid.ca"}
            ),
            403,
        )

    results = _read_test_results()
    if results is None:
        return (
            jsonify(
                {"success": False, "error": "No test results found. Run tests first."}
            ),
            404,
        )

    return jsonify({"success": True, **results})


@azure_integration_bp.route("/run-tests", methods=["POST"])
def azure_run_tests():
    data = request.get_json(force=True) or {}
    user_id = data.get("user_id") or _extract_user_id()
    if not user_id or user_id not in ACCESSIBLE_IDS:
        return (
            jsonify(
                {"success": False, "error": "Access restricted to service@bytoid.ca"}
            ),
            403,
        )

    results_dir = os.path.join(_PROJECT_ROOT, "testing", "results")
    os.makedirs(results_dir, exist_ok=True)

    pytest_bin = shutil.which("pytest") or shutil.which("pytest3")
    if pytest_bin:
        cmd = [pytest_bin]
    else:
        cmd = [sys.executable, "-m", "pytest"]

    env = os.environ.copy()
    env["PYTHONPATH"] = _PROJECT_ROOT

    try:
        proc = subprocess.run(
            cmd
            + [
                "testing/",
                "-v",
                "--tb=short",
                "--json-report",
                "--json-report-file=testing/results/latest.json",
            ],
            capture_output=True,
            text=True,
            cwd=_PROJECT_ROOT,
            env=env,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return (
            jsonify({"success": False, "error": "Test run timed out after 120s"}),
            504,
        )
    except Exception as exc:
        return (
            jsonify(
                {"success": False, "error": f"Failed to launch test runner: {exc}"}
            ),
            500,
        )

    results = _read_test_results() or {}
    response = {
        "success": proc.returncode == 0,
        "exit_code": proc.returncode,
        **results,
    }
    if not results:
        response["stdout"] = proc.stdout[-3000:] if proc.stdout else ""
        response["stderr"] = proc.stderr[-3000:] if proc.stderr else ""
    return jsonify(response)
