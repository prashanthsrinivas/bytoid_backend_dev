import asyncio
import json
import os
from datetime import datetime
from urllib.parse import urlparse

import boto3
import pymysql
from flask import Blueprint, g, jsonify, redirect, request, session

from apiConnector.helpers import (
    expand_custom_dates,
    resolve_schedule_from_activation,
)
from aws_integration.helpers import (
    _admin_only_check,
    _build_sigv4_auth_from_session,
    _execute_aws_app_internal,
    _execute_aws_endpoint_internal,
    _find_admin_by_saml_issuer,
    _get_active_aws_session,
    _get_aws_idp_config,
    _init_saml_auth_aws,
    _resolve_aws_auth,
    prepare_flask_request_aws,
    save_aws_run_to_s3,
)
from db.rds_db import connect_to_rds
from services.apiconnectors import APIConnector
from services.audit_log_service import (
    AWS_SAML_CONNECTED,
    AWS_SAML_DISCONNECTED,
    log_audit_event,
)
from services.scheduler_service import APIConnectorScheduler
from utils.app_configs import ALLOWED_ORIGINS
from utils.s3_utils import get_filedata_endp, getallendpointdetails

aws_integration_bp = Blueprint("aws_integration", __name__, url_prefix="/aws")


# ─────────────────────────────────────────────────────────────
# Internal helpers specific to AWS tables
# ─────────────────────────────────────────────────────────────

def _save_aws_endpoint_schedule(endpoint_id, schedule_payload):
    conn = connect_to_rds()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE aws_external_app_endpoints
        SET schedules = %s, updated_at = NOW()
        WHERE id = %s
        """,
        (json.dumps(schedule_payload), endpoint_id),
    )
    conn.commit()
    cur.close()
    conn.close()


def _get_aws_schedule_endpointdetails(endpoint_id):
    conn = connect_to_rds()
    cur = conn.cursor()
    cur.execute(
        "SELECT schedules FROM aws_external_app_endpoints WHERE id = %s",
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


# ─────────────────────────────────────────────────────────────
# IdP configuration (per-admin)
# ─────────────────────────────────────────────────────────────

@aws_integration_bp.route("/idp/config", methods=["POST"])
def aws_save_idp_config():
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
        aws_region = (data.get("aws_region") or "us-east-1").strip()

        if not entity_id:
            return jsonify({"success": False, "error": "entity_id required"}), 400
        if not sso_url:
            return jsonify({"success": False, "error": "sso_url required"}), 400

        existing = _get_aws_idp_config(user_id)
        if not x509_cert:
            if not existing:
                return jsonify({"success": False, "error": "x509_cert required"}), 400
            x509_cert = existing["x509_cert"]

        conn = connect_to_rds()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO aws_idp_configs
                    (user_id, entity_id, sso_url, x509_cert, aws_region)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    entity_id  = VALUES(entity_id),
                    sso_url    = VALUES(sso_url),
                    x509_cert  = VALUES(x509_cert),
                    aws_region = VALUES(aws_region),
                    updated_at = NOW()
                """,
                (user_id, entity_id, sso_url, x509_cert, aws_region),
            )
        conn.commit()
        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@aws_integration_bp.route("/idp/config", methods=["GET"])
def aws_get_idp_config():
    user_id = request.args.get("user_id") or _extract_user_id()

    ok, err = _admin_only_check(user_id)
    if not ok:
        return err

    idp = _get_aws_idp_config(user_id)
    if idp:
        return jsonify({
            "configured": True,
            "entity_id": idp["entity_id"],
            "sso_url": idp["sso_url"],
            "aws_region": idp.get("aws_region", "us-east-1"),
        })
    return jsonify({"configured": False})


@aws_integration_bp.route("/idp/config", methods=["DELETE"])
def aws_delete_idp_config():
    conn = None
    try:
        data = request.get_json(force=True) or {}
        user_id = data.get("user_id") or _extract_user_id()

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        conn = connect_to_rds()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM aws_idp_configs WHERE user_id=%s", (user_id,))
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

@aws_integration_bp.route("/saml/login", methods=["GET"])
def aws_saml_login():
    user_id = request.args.get("user_id") or _extract_user_id()

    ok, err = _admin_only_check(user_id)
    if not ok:
        return err

    if not _get_aws_idp_config(user_id):
        return jsonify({"error": "AWS IdP not configured. Save your Identity Center details first."}), 400

    redirect_url = request.args.get("redirect", "")
    parsed = urlparse(redirect_url)
    host_origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else ""
    if host_origin not in ALLOWED_ORIGINS:
        redirect_url = "https://app.bytoid.ai/aws-integration"

    # Store state in RelayState (echoed back by IdP in ACS POST) instead of
    # Flask session — session cookies are blocked on cross-site POST (SameSite=Lax).
    relay_state = json.dumps({"user_id": user_id, "redirect": redirect_url})

    req = prepare_flask_request_aws(request)
    try:
        auth = _init_saml_auth_aws(req, user_id)
        login_url = auth.login(return_to=relay_state, force_authn=True)
        return redirect(login_url)
    except Exception as e:
        return jsonify({"error": "AWS SAML not configured", "detail": str(e)}), 500


# ─────────────────────────────────────────────────────────────
# SAML — Assertion Consumer Service
# ─────────────────────────────────────────────────────────────

@aws_integration_bp.route("/saml/acs", methods=["POST"])
def aws_saml_acs():
    conn = None
    try:
        # Decode RelayState (set during /saml/login) — more reliable than Flask
        # session because SameSite=Lax blocks session cookies on cross-site POST.
        relay_state_raw = request.form.get("RelayState", "")
        relay_data = {}
        try:
            relay_data = json.loads(relay_state_raw) if relay_state_raw else {}
        except Exception:
            pass

        user_id = relay_data.get("user_id") or session.get("aws_saml_user_id")
        redirect_target = relay_data.get("redirect") or session.get("aws_saml_redirect") or "https://app.bytoid.ai/aws-integration"

        # Re-validate redirect URL from RelayState for security
        parsed_rd = urlparse(redirect_target)
        host_rd = f"{parsed_rd.scheme}://{parsed_rd.netloc}" if parsed_rd.netloc else ""
        if host_rd not in ALLOWED_ORIGINS:
            redirect_target = "https://app.bytoid.ai/aws-integration"

        # IdP-initiated SSO fallback: no prior /saml/login
        if not user_id:
            raw_saml = request.form.get("SAMLResponse", "")
            user_id = _find_admin_by_saml_issuer(raw_saml) if raw_saml else None

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        req = prepare_flask_request_aws(request)
        auth = _init_saml_auth_aws(req, user_id)

        auth.process_response(request_id=request_id)
        errors = auth.get_errors()
        if errors:
            return jsonify({"error": errors, "reason": auth.get_last_error_reason()}), 400
        if not auth.is_authenticated():
            return jsonify({"error": "Not authenticated"}), 401

        user_data = auth.get_attributes()

        # Extract AWS role attribute
        # Format: "arn:aws:iam::ACCOUNT:role/Name,arn:aws:iam::ACCOUNT:saml-provider/Name"
        role_attrs = user_data.get("https://aws.amazon.com/SAML/Attributes/Role", [])
        if not role_attrs:
            return jsonify({"error": "AWS Role attribute missing from SAML assertion"}), 400

        role_parts = role_attrs[0].split(",")
        if len(role_parts) != 2:
            return jsonify({"error": "Malformed AWS Role attribute"}), 400

        # AWS sends role,principal or principal,role depending on IdP config
        part_a, part_b = [p.strip() for p in role_parts]
        if ":role/" in part_a:
            role_arn, principal_arn = part_a, part_b
        else:
            role_arn, principal_arn = part_b, part_a

        saml_assertion = request.form.get("SAMLResponse", "")
        if not saml_assertion:
            return jsonify({"error": "SAMLResponse missing"}), 400

        # Extract region from RoleSessionName attribute if present
        aws_region = user_data.get(
            "https://aws.amazon.com/SAML/Attributes/awsRequestedRegion",
            ["us-east-1"],
        )[0]

        # Call STS AssumeRoleWithSAML
        sts = boto3.client("sts", region_name="us-east-1")
        sts_resp = sts.assume_role_with_saml(
            RoleArn=role_arn,
            PrincipalArn=principal_arn,
            SAMLAssertion=saml_assertion,
            DurationSeconds=3600,
        )

        creds = sts_resp["Credentials"]
        account_id = role_arn.split(":")[4]
        expires_at = creds["Expiration"].strftime("%Y-%m-%d %H:%M:%S")

        # Upsert session row
        conn = connect_to_rds()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO aws_saml_sessions
                    (user_id, aws_account_id, aws_role_arn,
                     aws_access_key_id, aws_secret_access_key,
                     aws_session_token, aws_region, expires_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                    aws_account_id        = VALUES(aws_account_id),
                    aws_role_arn          = VALUES(aws_role_arn),
                    aws_access_key_id     = VALUES(aws_access_key_id),
                    aws_secret_access_key = VALUES(aws_secret_access_key),
                    aws_session_token     = VALUES(aws_session_token),
                    aws_region            = VALUES(aws_region),
                    expires_at            = VALUES(expires_at),
                    updated_at            = NOW()
                """,
                (
                    user_id,
                    account_id,
                    role_arn,
                    creds["AccessKeyId"],
                    creds["SecretAccessKey"],
                    creds["SessionToken"],
                    aws_region,
                    expires_at,
                ),
            )
        conn.commit()

        log_audit_event(
            action=AWS_SAML_CONNECTED,
            endpoint="/aws/saml/acs",
            ip=request.remote_addr,
            status="success",
            actor_user_id=user_id,
            metadata={"aws_account_id": account_id, "role_arn": role_arn},
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

@aws_integration_bp.route("/saml/status", methods=["GET"])
def aws_saml_status():
    user_id = request.args.get("user_id") or _extract_user_id()

    ok, err = _admin_only_check(user_id)
    if not ok:
        return err

    session_row = _get_active_aws_session(user_id)
    if session_row:
        return jsonify({
            "connected": True,
            "aws_account_id": session_row.get("aws_account_id"),
            "aws_role_arn": session_row.get("aws_role_arn"),
            "aws_region": session_row.get("aws_region"),
            "expires_at": str(session_row.get("expires_at")),
        })
    return jsonify({"connected": False})


@aws_integration_bp.route("/saml/disconnect", methods=["POST"])
def aws_saml_disconnect():
    data = request.get_json(force=True) or {}
    user_id = data.get("user_id") or _extract_user_id()

    ok, err = _admin_only_check(user_id)
    if not ok:
        return err

    conn = None
    try:
        conn = connect_to_rds()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM aws_saml_sessions WHERE user_id=%s", (user_id,)
            )
        conn.commit()

        log_audit_event(
            action=AWS_SAML_DISCONNECTED,
            endpoint="/aws/saml/disconnect",
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

@aws_integration_bp.route("/connector/test", methods=["POST"])
def aws_connector_test():
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

        # Resolve auth — fall back to stored SAML session for sigv4
        auth_type = auth.get("type", "aws_sigv4")
        if auth_type == "aws_sigv4" and not auth.get("access_key_id"):
            auth = _resolve_aws_auth({}, user_id)

        # Build URL
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
        return jsonify({
            "success": True,
            "request": {"url": full_url, "method": method},
            "response": result,
        })

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ─────────────────────────────────────────────────────────────
# Connector — Apps CRUD
# ─────────────────────────────────────────────────────────────

@aws_integration_bp.route("/connector/apps", methods=["POST"])
def aws_create_app():
    conn = None
    try:
        data = request.get_json(force=True) or {}
        user_id = data.get("user_id") or _extract_user_id()

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        app_name = (data.get("app_name") or "").strip()
        base_url = (data.get("base_url") or "").strip()
        auth_type = data.get("auth_type", "aws_sigv4")
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

        # Resolve SigV4 credentials from session if not supplied
        if auth_type == "aws_sigv4" and not auth_config.get("access_key_id"):
            auth_config = _resolve_aws_auth({}, user_id)

        # Test connection before saving
        config = {
            "auth": auth_config,
            "request": {
                "url": base_url.rstrip("/"),
                "method": "GET",
                "headers": headers,
                "query_params": query_params,
                "body": None,
            },
            "timeout": timeout_seconds,
            "retry": {"count": 1, "backoff": 1},
        }
        test_result = APIConnector(userid=user_id, config=config).execute()

        conn = connect_to_rds()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO aws_external_apps
                    (user_id, app_name, base_url, auth_type, auth_config,
                     headers, query_params, path_params,
                     timeout_seconds, retry_count, retry_backoff_seconds,
                     last_test_status, last_tested_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                """,
                (
                    user_id, app_name, base_url, auth_type,
                    json.dumps(auth_config),
                    json.dumps(headers), json.dumps(query_params),
                    json.dumps(path_params),
                    timeout_seconds, retry_count, retry_backoff_seconds,
                    "success" if test_result.get("success") else "failed",
                ),
            )
            app_id = cur.lastrowid
        conn.commit()

        return jsonify({
            "success": True,
            "app_id": app_id,
            "app_name": app_name,
            "test_result": test_result,
        })

    except ValueError as ve:
        return jsonify({"success": False, "error": str(ve)}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@aws_integration_bp.route("/connector/apps/<user_id>", methods=["GET"])
def aws_list_apps(user_id):
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
                       last_test_status, last_tested_at, created_at, updated_at
                FROM aws_external_apps
                WHERE user_id=%s AND status='active'
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


@aws_integration_bp.route("/connector/apps/<int:app_id>", methods=["PUT"])
def aws_update_app(app_id):
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
                "SELECT user_id FROM aws_external_apps WHERE id=%s", (app_id,)
            )
            row = cur.fetchone()

        if not row or row["user_id"] != user_id:
            return jsonify({"success": False, "error": "App not found"}), 404

        fields = {}
        for field in ("app_name", "base_url", "auth_type", "status",
                      "timeout_seconds", "retry_count", "retry_backoff_seconds"):
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
                f"UPDATE aws_external_apps SET {set_clause}, updated_at=NOW() WHERE id=%s AND user_id=%s",
                values,
            )
        conn.commit()
        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@aws_integration_bp.route("/connector/apps/<int:app_id>", methods=["DELETE"])
def aws_delete_app(app_id):
    conn = None
    try:
        data = request.get_json(force=True) or {}
        user_id = data.get("user_id") or _extract_user_id() or request.args.get("user_id")

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        conn = connect_to_rds()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE aws_external_apps SET status='inactive', updated_at=NOW() WHERE id=%s AND user_id=%s",
                (app_id, user_id),
            )
        conn.commit()
        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@aws_integration_bp.route("/connector/apps/<int:app_id>/hard", methods=["DELETE"])
def aws_hard_delete_app(app_id):
    conn = None
    try:
        data = request.get_json(force=True) or {}
        user_id = data.get("user_id") or _extract_user_id() or request.args.get("user_id")

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        conn = connect_to_rds()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM aws_external_apps WHERE id=%s AND user_id=%s",
                (app_id, user_id),
            )
        conn.commit()
        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@aws_integration_bp.route("/connector/apps/<int:app_id>/test", methods=["POST"])
def aws_test_app(app_id):
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
                "SELECT * FROM aws_external_apps WHERE id=%s AND user_id=%s",
                (app_id, user_id),
            )
            app = cur.fetchone()

        if not app:
            return jsonify({"success": False, "error": "App not found"}), 404

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
            "retry": {"count": 1, "backoff": 1},
        }

        result = APIConnector(userid=user_id, config=config).execute()
        status = "success" if result.get("success") else "failed"

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE aws_external_apps
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


@aws_integration_bp.route("/connector/apps/<int:app_id>/execute", methods=["POST"])
def aws_execute_app(app_id):
    try:
        data = request.get_json(force=True) or {}
        user_id = data.get("user_id") or _extract_user_id()

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        result = asyncio.run(_execute_aws_app_internal(app_id, user_id))
        return jsonify({"success": True, "result": result})

    except ValueError as ve:
        return jsonify({"success": False, "error": str(ve)}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ─────────────────────────────────────────────────────────────
# Connector — Endpoints CRUD
# ─────────────────────────────────────────────────────────────

@aws_integration_bp.route("/connector/apps/<int:app_id>/endpoints", methods=["POST"])
def aws_create_endpoint(app_id):
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

        # Verify app ownership
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT id FROM aws_external_apps WHERE id=%s AND user_id=%s AND status='active'",
                (app_id, user_id),
            )
            if not cur.fetchone():
                return jsonify({"success": False, "error": "App not found"}), 404

            cur.execute(
                """
                INSERT INTO aws_external_app_endpoints
                    (app_id, user_id, name, path, method, headers,
                     query_params, path_params, body_template, timeout_seconds)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    app_id, user_id, name, path, method,
                    json.dumps(headers), json.dumps(query_params),
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


@aws_integration_bp.route("/connector/apps/<int:app_id>/endpoints", methods=["GET"])
def aws_list_endpoints(app_id):
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
                SELECT id, name, path, method, timeout_seconds, is_active,
                       last_test_status, last_tested_at, created_at, updated_at
                FROM aws_external_app_endpoints
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

        return jsonify({"success": True, "endpoints": endpoints})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@aws_integration_bp.route("/connector/endpoints/<int:endpoint_id>", methods=["PUT"])
def aws_update_endpoint(endpoint_id):
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
                f"UPDATE aws_external_app_endpoints SET {set_clause}, updated_at=NOW() WHERE id=%s AND user_id=%s",
                values,
            )
        conn.commit()
        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@aws_integration_bp.route("/connector/endpoints/<int:endpoint_id>", methods=["DELETE"])
def aws_delete_endpoint(endpoint_id):
    conn = None
    try:
        data = request.get_json(force=True) or {}
        user_id = data.get("user_id") or _extract_user_id() or request.args.get("user_id")

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        conn = connect_to_rds()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE aws_external_app_endpoints SET is_active=0, updated_at=NOW() WHERE id=%s AND user_id=%s",
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
# Connector — Endpoint test & execute
# ─────────────────────────────────────────────────────────────

@aws_integration_bp.route("/connector/endpoints/<int:endpoint_id>/test", methods=["POST"])
def aws_test_endpoint(endpoint_id):
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
                FROM aws_external_app_endpoints e
                JOIN aws_external_apps a ON a.id = e.app_id
                WHERE e.id=%s AND e.is_active=1
                """,
                (endpoint_id,),
            )
            row = cur.fetchone()

        if not row:
            return jsonify({"success": False, "error": "Endpoint not found"}), 404

        raw_auth = json.loads(row["auth_config"] or "{}")
        auth_config = _resolve_aws_auth(raw_auth, user_id)

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
                return jsonify({"success": False, "error": f"Missing path parameter: {var}"}), 400
            path = path.replace(f"{{{var}}}", str(final_path_params[var]))

        full_url = row["base_url"].rstrip("/") + path

        config = {
            "auth": auth_config,
            "request": {
                "url": full_url,
                "method": row["method"],
                "headers": {**json.loads(row["headers"] or "{}"), **runtime_params["headers"]},
                "query_params": {**json.loads(row["query_params"] or "{}"), **runtime_params["query_params"]},
                "body": runtime_params["body"] or json.loads(row["body_template"] or "null"),
            },
            "timeout": row.get("timeout_seconds") or row.get("app_timeout") or 10,
            "retry": {"count": 1, "backoff": 1},
        }

        result = APIConnector(userid=user_id, config=config).execute()
        status = "success" if result.get("success") else "failed"

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE aws_external_app_endpoints
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


@aws_integration_bp.route("/connector/endpoints/<int:endpoint_id>/execute", methods=["POST"])
def aws_execute_endpoint(endpoint_id):
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
            _execute_aws_endpoint_internal(endpoint_id, user_id, runtime_params)
        )
        return jsonify({"success": True, "result": result})

    except ValueError as ve:
        return jsonify({"success": False, "error": str(ve)}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ─────────────────────────────────────────────────────────────
# Connector — Run history (S3)
# ─────────────────────────────────────────────────────────────

@aws_integration_bp.route("/connector/endpoints/<int:endpoint_id>/runs", methods=["GET"])
def aws_list_endpoint_runs(endpoint_id):
    try:
        user_id = request.args.get("user_id") or _extract_user_id()

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        # We need the app_id to build the S3 prefix
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT app_id FROM aws_external_app_endpoints WHERE id=%s",
                (endpoint_id,),
            )
            row = cur.fetchone()
        conn.close()

        if not row:
            return jsonify({"success": False, "error": "Endpoint not found"}), 404

        app_id = row["app_id"]
        prefix = f"{user_id}/aws_connector/{app_id}/{endpoint_id}/"
        files = getallendpointdetails(prefix)
        return jsonify({"success": True, "runs": files})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@aws_integration_bp.route(
    "/connector/endpoints/<int:endpoint_id>/runs/<path:filename>", methods=["GET"]
)
def aws_get_endpoint_run(endpoint_id, filename):
    try:
        user_id = request.args.get("user_id") or _extract_user_id()

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT app_id FROM aws_external_app_endpoints WHERE id=%s",
                (endpoint_id,),
            )
            row = cur.fetchone()
        conn.close()

        if not row:
            return jsonify({"success": False, "error": "Endpoint not found"}), 404

        app_id = row["app_id"]
        key = f"{user_id}/aws_connector/{app_id}/{endpoint_id}/{filename}"
        data = get_filedata_endp(key)
        return jsonify({"success": True, "data": data})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ─────────────────────────────────────────────────────────────
# Connector — Scheduling
# ─────────────────────────────────────────────────────────────

@aws_integration_bp.route(
    "/connector/endpoints/<int:endpoint_id>/schedule", methods=["POST"]
)
def aws_schedule_endpoint(endpoint_id):
    try:
        body = request.get_json(force=True) or {}
        user_id = body.get("user_id") or _extract_user_id()

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        activation = body.get("scheduledActivation")
        schedule_type, data = resolve_schedule_from_activation(activation)
        timezone = data.get("timezone", "UTC")

        # Cancel any existing schedule
        existing = _get_aws_schedule_endpointdetails(endpoint_id)
        if existing:
            celery_type = existing.get("celery_type", "")
            celery_id = existing.get("celery_task_id", "")
            celery_entry = existing.get("celery_entry", "")
            celery_task_ids = existing.get("celery_task_ids", [])

            if celery_type == "task" and celery_id:
                APIConnectorScheduler.revoke_task(celery_id)
            elif celery_type == "beat" and celery_entry:
                APIConnectorScheduler.disable_celery_entry(celery_entry)
            elif celery_type == "tasks" and celery_task_ids:
                for tid in celery_task_ids:
                    APIConnectorScheduler.revoke_task(tid)
            elif celery_id:
                APIConnectorScheduler.revoke_task(celery_id)

        if schedule_type == "one_time":
            dt = datetime.fromisoformat(data["datetime"])
            result = asyncio.run(
                APIConnectorScheduler.schedule_endpoint_once(user_id, endpoint_id, dt, timezone)
            )
        elif schedule_type == "daily":
            hour, minute = map(int, data["startTime"].split(":"))
            result = asyncio.run(
                APIConnectorScheduler.schedule_endpoint_daily(user_id, endpoint_id, hour, minute, timezone)
            )
        elif schedule_type == "weekly":
            hour, minute = map(int, data["startTime"].split(":"))
            result = asyncio.run(
                APIConnectorScheduler.schedule_endpoint_weekly(
                    user_id, endpoint_id, data["weekday"], hour, minute, timezone
                )
            )
        elif schedule_type == "monthly":
            hour, minute = map(int, data["startTime"].split(":"))
            result = asyncio.run(
                APIConnectorScheduler.schedule_endpoint_monthly(
                    user_id, endpoint_id, data["day"], hour, minute, timezone
                )
            )
        elif schedule_type == "interval":
            result = asyncio.run(
                APIConnectorScheduler.schedule_endpoint_interval(user_id, endpoint_id, data["seconds"])
            )
        elif schedule_type == "custom":
            dates = expand_custom_dates(
                start_date=data["startDate"],
                end_date=data["endDate"],
                start_time=data["startTime"],
            )
            result = asyncio.run(
                APIConnectorScheduler.schedule_endpoint_custom_dates(user_id, endpoint_id, dates, timezone)
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
            f"aws_endpoint:{endpoint_id}:{user_id}:{schedule_type}"
        )

        _save_aws_endpoint_schedule(endpoint_id, schedule_record)
        return jsonify({"success": True, "schedule": schedule_record})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
