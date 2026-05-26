import asyncio
import json
import re
from datetime import datetime

import pymysql
from flask import Blueprint, g, jsonify, request, session

from apiConnector.helpers import (
    expand_custom_dates,
    resolve_schedule_from_activation,
)
from gcp_integration.helpers import (
    _admin_only_check,
    _execute_gcp_app_internal,
    _execute_gcp_endpoint_internal,
    _fetch_service_account_token,
    _get_gcp_config,
    _resolve_gcp_auth,
)
from db.rds_db import connect_to_rds
from services.apiconnectors import APIConnector
from services.audit_log_service import (
    GCP_CONNECTED,
    GCP_DISCONNECTED,
    log_audit_event,
)
from services.scheduler_service import GCPAPIConnectorScheduler
from utils.s3_utils import get_filedata_endp, getallendpointdetails

gcp_integration_bp = Blueprint("gcp_integration", __name__, url_prefix="/gcp")


# ─────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────

def _save_gcp_endpoint_schedule(endpoint_id, schedule_payload):
    conn = connect_to_rds()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE gcp_external_app_endpoints
        SET schedules = %s, updated_at = NOW()
        WHERE id = %s
        """,
        (json.dumps(schedule_payload), endpoint_id),
    )
    conn.commit()
    cur.close()
    conn.close()


def _get_gcp_schedule_endpointdetails(endpoint_id):
    conn = connect_to_rds()
    cur = conn.cursor()
    cur.execute(
        "SELECT schedules FROM gcp_external_app_endpoints WHERE id = %s",
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
# GCP Configuration (per-admin)
# ─────────────────────────────────────────────────────────────


@gcp_integration_bp.route("/config", methods=["POST"])
def gcp_save_config():
    conn = None
    try:
        data = request.get_json(force=True) or {}
        user_id = data.get("user_id") or _extract_user_id()

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        project_id = (data.get("project_id") or "").strip()
        service_account_email = (data.get("service_account_email") or "").strip()
        service_account_key = (data.get("service_account_key") or "").strip()
        default_scope = (
            data.get("default_scope") or "https://www.googleapis.com/auth/cloud-platform"
        ).strip()
        gcp_region = (data.get("gcp_region") or "us-central1").strip()

        if not project_id:
            return jsonify({"success": False, "error": "project_id required"}), 400
        if not service_account_email:
            return jsonify({"success": False, "error": "service_account_email required"}), 400

        existing = _get_gcp_config(user_id)
        if not service_account_key:
            if not existing:
                return jsonify({"success": False, "error": "service_account_key required"}), 400
            service_account_key = existing["service_account_key"]

        # Validate the key is valid JSON if provided
        if service_account_key:
            try:
                json.loads(service_account_key)
            except (json.JSONDecodeError, ValueError):
                return jsonify({"success": False, "error": "service_account_key must be valid JSON"}), 400

        conn = connect_to_rds()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO gcp_configs
                    (user_id, project_id, service_account_email,
                     service_account_key, default_scope, gcp_region)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    project_id            = VALUES(project_id),
                    service_account_email = VALUES(service_account_email),
                    service_account_key   = VALUES(service_account_key),
                    default_scope         = VALUES(default_scope),
                    gcp_region            = VALUES(gcp_region),
                    updated_at            = NOW()
                """,
                (
                    user_id,
                    project_id,
                    service_account_email,
                    service_account_key,
                    default_scope,
                    gcp_region,
                ),
            )
        conn.commit()

        log_audit_event(
            action=GCP_CONNECTED,
            endpoint="/gcp/config",
            ip=request.remote_addr,
            status="success",
            actor_user_id=user_id,
            metadata={"project_id": project_id},
        )

        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@gcp_integration_bp.route("/config", methods=["GET"])
def gcp_get_config():
    user_id = request.args.get("user_id") or _extract_user_id()

    ok, err = _admin_only_check(user_id)
    if not ok:
        return err

    cfg = _get_gcp_config(user_id)
    if cfg:
        return jsonify(
            {
                "configured": True,
                "project_id": cfg["project_id"],
                "service_account_email": cfg["service_account_email"],
                "default_scope": cfg.get("default_scope"),
                "gcp_region": cfg.get("gcp_region", "us-central1"),
            }
        )
    return jsonify({"configured": False})


@gcp_integration_bp.route("/config", methods=["PUT"])
def gcp_update_config():
    return gcp_save_config()


@gcp_integration_bp.route("/config", methods=["DELETE"])
def gcp_delete_config():
    conn = None
    try:
        data = request.get_json(force=True) or {}
        user_id = data.get("user_id") or _extract_user_id()

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        conn = connect_to_rds()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM gcp_configs WHERE user_id=%s", (user_id,))
        conn.commit()

        log_audit_event(
            action=GCP_DISCONNECTED,
            endpoint="/gcp/config",
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


@gcp_integration_bp.route("/config/status", methods=["GET"])
def gcp_config_status():
    """Try to fetch a token — returns connected: true/false."""
    user_id = request.args.get("user_id") or _extract_user_id()

    ok, err = _admin_only_check(user_id)
    if not ok:
        return err

    cfg = _get_gcp_config(user_id)
    if not cfg:
        return jsonify({"connected": False, "error": "GCP not configured"})

    token, token_err = _fetch_service_account_token(cfg)
    if token_err or not token:
        return jsonify({"connected": False, "error": token_err or "Token fetch failed"})

    return jsonify(
        {
            "connected": True,
            "project_id": cfg["project_id"],
            "service_account_email": cfg["service_account_email"],
            "gcp_region": cfg.get("gcp_region", "us-central1"),
        }
    )


# ─────────────────────────────────────────────────────────────
# Connector — Ad-hoc test
# ─────────────────────────────────────────────────────────────


@gcp_integration_bp.route("/connector/test", methods=["POST"])
def gcp_connector_test():
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

        auth_type = auth.get("type", "gcp_oauth")
        if auth_type == "gcp_oauth" and not auth.get("access_token"):
            auth = _resolve_gcp_auth({}, user_id)

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


@gcp_integration_bp.route("/connector/apps", methods=["POST"])
def gcp_create_app():
    conn = None
    try:
        data = request.get_json(force=True) or {}
        user_id = data.get("user_id") or _extract_user_id()

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        app_name = (data.get("app_name") or "").strip()
        base_url = (data.get("base_url") or "").strip()
        auth_type = data.get("auth_type", "gcp_oauth")
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
                "SELECT id FROM gcp_external_apps WHERE user_id=%s AND app_name=%s LIMIT 1",
                (user_id, app_name),
            )
            existing = cur.fetchone()

            if existing:
                existing_id = existing["id"]
                cur.execute(
                    """
                    SELECT id, app_name, base_url, auth_type, status,
                           last_test_status, last_tested_at, created_at, updated_at
                    FROM gcp_external_apps WHERE id=%s
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
                        "message": f"'{app_name}' is already in your app list.",
                    }
                )

            cur.execute(
                """
                INSERT INTO gcp_external_apps
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


@gcp_integration_bp.route("/connector/apps/<user_id>", methods=["GET"])
def gcp_list_apps(user_id):
    ok, err = _admin_only_check(user_id)
    if not ok:
        return err

    conn = None
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                """
                SELECT id, app_name, base_url, auth_type, status,
                       last_test_status, last_tested_at, created_at, updated_at
                FROM gcp_external_apps
                WHERE user_id=%s
                ORDER BY created_at DESC
                """,
                (user_id,),
            )
            apps = cur.fetchall()

        for app in apps:
            for field in ("last_tested_at", "created_at", "updated_at"):
                if app.get(field):
                    app[field] = str(app[field])

        return jsonify({"success": True, "apps": apps})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@gcp_integration_bp.route("/connector/apps/<int:app_id>", methods=["PUT"])
def gcp_update_app(app_id):
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
                "SELECT user_id FROM gcp_external_apps WHERE id=%s", (app_id,)
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
                f"UPDATE gcp_external_apps SET {set_clause}, updated_at=NOW() WHERE id=%s AND user_id=%s",
                values,
            )
        conn.commit()
        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@gcp_integration_bp.route("/connector/apps/<int:app_id>", methods=["DELETE"])
def gcp_delete_app(app_id):
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
                "DELETE FROM gcp_external_apps WHERE id=%s AND user_id=%s",
                (app_id, user_id),
            )
        conn.commit()
        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@gcp_integration_bp.route("/connector/apps/<int:app_id>/test", methods=["POST"])
def gcp_test_app(app_id):
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
                "SELECT * FROM gcp_external_apps WHERE id=%s AND user_id=%s",
                (app_id, user_id),
            )
            app = cur.fetchone()

        if not app:
            return jsonify({"success": False, "error": "App not found"}), 404

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
            "retry": {"count": 1, "backoff": 1},
        }

        result = APIConnector(userid=user_id, config=config).execute()
        status = "success" if result.get("success") else "failed"

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE gcp_external_apps
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


@gcp_integration_bp.route("/connector/apps/<int:app_id>/execute", methods=["POST"])
def gcp_execute_app(app_id):
    try:
        data = request.get_json(force=True) or {}
        user_id = data.get("user_id") or _extract_user_id()

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        result = asyncio.run(_execute_gcp_app_internal(app_id, user_id))
        return jsonify({"success": True, "result": result})

    except ValueError as ve:
        return jsonify({"success": False, "error": str(ve)}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ─────────────────────────────────────────────────────────────
# Connector — Endpoints CRUD
# ─────────────────────────────────────────────────────────────


@gcp_integration_bp.route("/connector/apps/<int:app_id>/endpoints", methods=["POST"])
def gcp_create_endpoint(app_id):
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
                "SELECT id FROM gcp_external_apps WHERE id=%s AND user_id=%s AND status='active'",
                (app_id, user_id),
            )
            if not cur.fetchone():
                return jsonify({"success": False, "error": "App not found"}), 404

            cur.execute(
                """
                INSERT INTO gcp_external_app_endpoints
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


@gcp_integration_bp.route("/connector/apps/<int:app_id>/endpoints", methods=["GET"])
def gcp_list_endpoints(app_id):
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
                FROM gcp_external_app_endpoints
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
            for json_field in ("headers", "query_params", "path_params", "body_template"):
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


@gcp_integration_bp.route("/connector/endpoints/<int:endpoint_id>", methods=["PUT"])
def gcp_update_endpoint(endpoint_id):
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
                f"UPDATE gcp_external_app_endpoints SET {set_clause}, updated_at=NOW() WHERE id=%s AND user_id=%s",
                values,
            )
        conn.commit()
        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@gcp_integration_bp.route("/connector/endpoints/<int:endpoint_id>", methods=["DELETE"])
def gcp_delete_endpoint(endpoint_id):
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
                "DELETE FROM gcp_external_app_endpoints WHERE id=%s AND user_id=%s",
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


@gcp_integration_bp.route("/connector/endpoints/<int:endpoint_id>/test", methods=["POST"])
def gcp_test_endpoint(endpoint_id):
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
                FROM gcp_external_app_endpoints e
                JOIN gcp_external_apps a ON a.id = e.app_id
                WHERE e.id=%s AND e.is_active=1
                """,
                (endpoint_id,),
            )
            row = cur.fetchone()

        if not row:
            return jsonify({"success": False, "error": "Endpoint not found"}), 404

        raw_auth = json.loads(row["auth_config"] or "{}")
        auth_config = _resolve_gcp_auth(raw_auth, user_id)

        runtime_params = {
            "headers": data.get("headers", {}),
            "query_params": data.get("query_params", {}),
            "path_params": data.get("path_params", {}),
            "body": data.get("body"),
        }

        path = row["path"]
        final_path_params = {
            **json.loads(row["path_params"] or "{}"),
            **runtime_params["path_params"],
        }
        for var in re.findall(r"\{(.*?)\}", path):
            if var not in final_path_params:
                return (
                    jsonify({"success": False, "error": f"Missing path parameter: {var}"}),
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
                UPDATE gcp_external_app_endpoints
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


@gcp_integration_bp.route("/connector/endpoints/<int:endpoint_id>/execute", methods=["POST"])
def gcp_execute_endpoint(endpoint_id):
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
            _execute_gcp_endpoint_internal(endpoint_id, user_id, runtime_params)
        )
        return jsonify({"success": True, "result": result})

    except ValueError as ve:
        return jsonify({"success": False, "error": str(ve)}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ─────────────────────────────────────────────────────────────
# Connector — Run history (S3)
# ─────────────────────────────────────────────────────────────


@gcp_integration_bp.route("/connector/endpoints/<int:endpoint_id>/runs", methods=["GET"])
def gcp_list_endpoint_runs(endpoint_id):
    try:
        user_id = request.args.get("user_id") or _extract_user_id()

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT app_id FROM gcp_external_app_endpoints WHERE id=%s",
                (endpoint_id,),
            )
            row = cur.fetchone()
        conn.close()

        if not row:
            return jsonify({"success": False, "error": "Endpoint not found"}), 404

        app_id = row["app_id"]
        prefix = f"{user_id}/gcp_connector/{app_id}/{endpoint_id}/"
        files = getallendpointdetails(prefix)
        return jsonify({"success": True, "runs": files})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@gcp_integration_bp.route(
    "/connector/endpoints/<int:endpoint_id>/runs/<path:filename>", methods=["GET"]
)
def gcp_get_endpoint_run(endpoint_id, filename):
    try:
        user_id = request.args.get("user_id") or _extract_user_id()

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT app_id FROM gcp_external_app_endpoints WHERE id=%s",
                (endpoint_id,),
            )
            row = cur.fetchone()
        conn.close()

        if not row:
            return jsonify({"success": False, "error": "Endpoint not found"}), 404

        app_id = row["app_id"]
        key = f"{user_id}/gcp_connector/{app_id}/{endpoint_id}/{filename}"
        data = get_filedata_endp(key)
        return jsonify({"success": True, "data": data})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ─────────────────────────────────────────────────────────────
# Connector — Scheduling
# ─────────────────────────────────────────────────────────────


@gcp_integration_bp.route("/connector/endpoints/<int:endpoint_id>/schedule", methods=["POST"])
def gcp_schedule_endpoint(endpoint_id):
    try:
        body = request.get_json(force=True) or {}
        user_id = body.get("user_id") or _extract_user_id()

        ok, err = _admin_only_check(user_id)
        if not ok:
            return err

        activation = body.get("scheduledActivation")
        schedule_type, data = resolve_schedule_from_activation(activation)
        timezone = data.get("timezone", "UTC")

        existing = _get_gcp_schedule_endpointdetails(endpoint_id)
        if existing:
            celery_type = existing.get("celery_type", "")
            celery_id = existing.get("celery_task_id", "")
            celery_entry = existing.get("celery_entry", "")
            celery_task_ids = existing.get("celery_task_ids", [])

            if celery_type == "task" and celery_id:
                GCPAPIConnectorScheduler.revoke_task(celery_id)
            elif celery_type == "beat" and celery_entry:
                GCPAPIConnectorScheduler.disable_celery_entry(celery_entry)
            elif celery_type == "tasks" and celery_task_ids:
                for tid in celery_task_ids:
                    GCPAPIConnectorScheduler.revoke_task(tid)
            elif celery_id:
                GCPAPIConnectorScheduler.revoke_task(celery_id)

        if schedule_type == "one_time":
            dt = datetime.fromisoformat(data["datetime"])
            result = asyncio.run(
                GCPAPIConnectorScheduler.schedule_endpoint_once(
                    user_id, endpoint_id, dt, timezone
                )
            )
        elif schedule_type == "daily":
            hour, minute = map(int, data["startTime"].split(":"))
            result = asyncio.run(
                GCPAPIConnectorScheduler.schedule_endpoint_daily(
                    user_id, endpoint_id, hour, minute, timezone
                )
            )
        elif schedule_type == "weekly":
            hour, minute = map(int, data["startTime"].split(":"))
            result = asyncio.run(
                GCPAPIConnectorScheduler.schedule_endpoint_weekly(
                    user_id, endpoint_id, data["weekday"], hour, minute, timezone
                )
            )
        elif schedule_type == "monthly":
            hour, minute = map(int, data["startTime"].split(":"))
            result = asyncio.run(
                GCPAPIConnectorScheduler.schedule_endpoint_monthly(
                    user_id, endpoint_id, data["day"], hour, minute, timezone
                )
            )
        elif schedule_type == "interval":
            result = asyncio.run(
                GCPAPIConnectorScheduler.schedule_endpoint_interval(
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
                GCPAPIConnectorScheduler.schedule_endpoint_custom_dates(
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
            f"gcp_endpoint:{endpoint_id}:{user_id}:{schedule_type}"
        )

        _save_gcp_endpoint_schedule(endpoint_id, schedule_record)
        return jsonify({"success": True, "schedule": schedule_record})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ─────────────────────────────────────────────────────────────
# Connector — Cleanup inactive records
# ─────────────────────────────────────────────────────────────


@gcp_integration_bp.route("/connector/cleanup-inactive", methods=["DELETE"])
def gcp_cleanup_inactive():
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
                "DELETE FROM gcp_external_app_endpoints WHERE user_id=%s AND is_active=0",
                (user_id,),
            )
            deleted_endpoints = cur.rowcount

            cur.execute(
                "DELETE FROM gcp_external_apps WHERE user_id=%s AND status='inactive'",
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
