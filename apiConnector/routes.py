from datetime import datetime
import json
from urllib.parse import urlencode
import pymysql
from db.rds_db import connect_to_rds
from flask import request, jsonify, Blueprint
from utils.normal import parse_composite_user_id
from services.apiconnectors import APIConnector
from services.scheduler_service import APIConnectorScheduler
from utils.s3_utils import get_filedata_endp, getallendpointdetails
from apiConnector.helpers import (
    _execute_app_internal,
    _execute_endpoint_internal,
    _get_effective_auth_config,
    build_full_url,
    expand_custom_dates,
    extract_user_id,
    get_onboarding_role,
    get_schedule_endpointdetails,
    merge_json,
    normalize_role,
    normalize_row_dynamic,
    resolve_schedule_from_activation,
    save_endpoint_schedule,
)
from utils.app_configs import ACCESSIBLE_IDS
from utils.permission_required import permission_required_body

apiconnector_bp = Blueprint("apiconnector", __name__, url_prefix="/apiconnector/apps")

GLOBAL_APP_CREATOR_EMAIL = "beta@bytoid.ai"


# ==========================================================
# 🔹 1️⃣ TEST ENDPOINT
# ==========================================================
@apiconnector_bp.route("/test", methods=["POST"])
@permission_required_body("apps.endpoint.test")
def test_online_external_link():
    try:
        data = request.get_json(force=True)

        user_id = data.get("user_id")
        if not user_id:
            return jsonify({"success": False, "error": "user_id required"}), 400
        logged_in_user_id, user_id = parse_composite_user_id(user_id)

        base_url = data.get("base_url")
        if not base_url:
            return jsonify({"success": False, "error": "base_url required"}), 400

        auth = data.get("auth", {})

        # ✅ Support both formats (flat OR test_request)
        test_req = data.get("test_request", data)

        path = test_req.get("path", "/")
        path_params = test_req.get("path_params", {})
        method = test_req.get("method", "GET").upper()
        headers = test_req.get("headers", {})
        query_params = test_req.get("query_params", {})
        body = test_req.get("body")

        # ✅ Build full URL
        full_url = build_full_url(base_url, path, path_params)
        print("Full URL:", full_url)

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
        print("config", config)

        connector = APIConnector(userid=user_id, config=config)
        result = connector.execute()

        # print("Result:", result)

        return jsonify(
            {
                "success": True,
                "request": {"url": full_url, "method": method},
                "response": result,
            }
        )

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@apiconnector_bp.route("", methods=["POST"])
@permission_required_body("apps.create")
def create_external_app():
    conn = None
    cur = None

    try:
        data = request.get_json(force=True)

        # ✅ Validate user
        user_id = extract_user_id(data, request)
        if not user_id:
            return jsonify({"success": False, "error": "user_id required"}), 400
        logged_in_user_id, user_id = parse_composite_user_id(user_id)

        app_name = data["app_name"]
        provider = data.get("provider", "custom")
        base_url = data["base_url"].strip().rstrip("/")
        auth = data.get("auth") or {"type": "none"}

        # ✅ Accept both flat & nested
        test_req = data.get("test_request", data)

        incoming_path = (test_req.get("path") or "").strip()
        method = (test_req.get("method") or "GET").upper()
        headers = test_req.get("headers") or {}
        query_params = test_req.get("query_params") or {}
        path_params = test_req.get("path_params") or {}
        body = test_req.get("body")

        # Normalize path
        if incoming_path:
            path_template = (
                incoming_path if incoming_path.startswith("/") else f"/{incoming_path}"
            )
        else:
            path_template = "/"

        while "//" in path_template:
            path_template = path_template.replace("//", "/")

        # ✅ Build final URL using central function
        full_url = build_full_url(base_url, path_template, path_params)

        # If query params exist, append properly
        if query_params:
            full_url = f"{full_url}?{urlencode(query_params)}"

        # ✅ Connect DB
        conn = connect_to_rds()
        cur = conn.cursor(pymysql.cursors.DictCursor)

        # ✅ Duplicate app name check
        cur.execute(
            "SELECT id FROM external_apps WHERE user_id=%s AND app_name=%s",
            (user_id, app_name),
        )
        if cur.fetchone():
            return jsonify({"success": False, "error": "App name already exists"}), 409

        # ✅ Test connection BEFORE saving
        connector = APIConnector(
            userid=user_id,
            config={
                "auth": auth,
                "request": {
                    "url": full_url,
                    "method": method,
                    "headers": headers,
                    "body": body,
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

        # ✅ Insert external_apps (store TEMPLATE path, not resolved path)
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
                path_params,
                auth_type,
                auth_config
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                user_id,
                app_name,
                provider,
                base_url,
                json.dumps(headers),
                method,
                json.dumps(query_params),
                json.dumps(path_params),
                auth.get("type", "none"),
                json.dumps(auth),
            ),
        )

        app_id = cur.lastrowid

        # ✅ Auto create endpoint
        if incoming_path:
            cur.execute(
                """
                INSERT INTO external_app_endpoints
                (
                    app_id,
                    user_id,
                    name,
                    path,
                    method,
                    headers,
                    query_params,
                    path_params,
                    body_template,
                    timeout_seconds
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    app_id,
                    user_id,
                    f"{app_name}_default_endpoint",
                    path_template,  # store template
                    method,
                    json.dumps(headers),
                    json.dumps(query_params),
                    json.dumps(path_params),
                    json.dumps(body) if body else None,
                    data.get("timeout_seconds", 10),
                ),
            )

        conn.commit()

        return jsonify(
            {
                "success": True,
                "message": "App registered successfully!",
                "tested_endpoint": {
                    "url": full_url,
                    "method": method,
                    "headers": headers,
                    "query_params": query_params,
                    "path_params": path_params,
                    "body": body,
                },
                "endpoint_created": bool(incoming_path),
            }
        )

    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 500

    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@apiconnector_bp.route("/<int:app_id>", methods=["PUT"])
@permission_required_body("apps.edit")
def update_external_app(app_id):
    conn = None
    cur = None

    try:
        data = request.get_json(force=True)
        user_id = extract_user_id(data)
        if not user_id:
            return jsonify({"success": False, "error": "user_id required"}), 400
        logged_in_user_id, user_id = parse_composite_user_id(user_id)

        conn = connect_to_rds()
        cur = conn.cursor(pymysql.cursors.DictCursor)

        # ---------- Load App ----------
        cur.execute("SELECT * FROM external_apps WHERE id=%s", (app_id,))
        app = cur.fetchone()
        if not app:
            return jsonify({"success": False, "error": "App not found"}), 404

        is_owner = app["user_id"] == user_id

        # ---------- Accept Flat or Nested ----------
        test_req = data.get("test_request", data)

        method = (test_req.get("method") or app.get("method") or "GET").upper()
        headers = test_req.get("headers")
        query_params = test_req.get("query_params")
        path_params = test_req.get("path_params")
        body = test_req.get("body")

        # Fallback to stored values
        headers = (
            headers if headers is not None else json.loads(app.get("headers") or "{}")
        )
        query_params = (
            query_params
            if query_params is not None
            else json.loads(app.get("query_params") or "{}")
        )
        path_params = (
            path_params
            if path_params is not None
            else json.loads(app.get("path_params") or "{}")
        )

        base_url = data.get("base_url", app["base_url"]).rstrip("/")

        # ---------- Normalize Path (template) ----------
        incoming_path = test_req.get("path")
        if incoming_path is None:
            path_template = "/"
        else:
            path_template = incoming_path.strip()
            if not path_template.startswith("/"):
                path_template = f"/{path_template}"

        while "//" in path_template:
            path_template = path_template.replace("//", "/")

        # ---------- Build Final URL ----------
        full_url = build_full_url(base_url, path_template, path_params)

        if query_params:
            from urllib.parse import urlencode

            full_url = f"{full_url}?{urlencode(query_params)}"

        # ---------- Resolve Auth ----------
        auth_for_test = data.get("auth")
        if auth_for_test is None:
            auth_for_test = _get_effective_auth_config(
                cur, app_id, user_id, app.get("auth_config")
            )

        # ---------- Test Connection ----------
        connector = APIConnector(
            userid=user_id,
            config={
                "auth": auth_for_test,
                "request": {
                    "url": full_url,
                    "method": method,
                    "headers": headers,
                    "body": body,
                },
                "timeout": data.get(
                    "timeout_seconds", app.get("timeout_seconds") or 10
                ),
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

        # ---------- Owner Global Update ----------
        if is_owner:
            update_fields = []
            params = []

            if "app_name" in data:
                update_fields.append("app_name=%s")
                params.append(data["app_name"])

            if "base_url" in data:
                update_fields.append("base_url=%s")
                params.append(base_url)

            if "headers" in test_req:
                update_fields.append("headers=%s")
                params.append(json.dumps(headers))

            if "query_params" in test_req:
                update_fields.append("query_params=%s")
                params.append(json.dumps(query_params))

            if "path_params" in test_req:
                update_fields.append("path_params=%s")
                params.append(json.dumps(path_params))

            if "method" in test_req:
                update_fields.append("method=%s")
                params.append(method)

            if update_fields:
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

        # ---------- Per User Config ----------
        auth_local = data.get("auth")
        timeout_local = data.get("timeout_seconds")
        retry_count_local = data.get("retry_count")
        retry_backoff_local = data.get("retry_backoff_seconds")

        if any(
            v is not None
            for v in [
                auth_local,
                headers,
                query_params,
                path_params,
                timeout_local,
                retry_count_local,
                retry_backoff_local,
            ]
        ):
            cur.execute(
                """
                INSERT INTO external_app_user_config
                (app_id, user_id, auth_type, auth_config,
                 method, headers, query_params, path_params,
                 timeout_seconds, retry_count, retry_backoff_seconds)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                    auth_type=COALESCE(VALUES(auth_type), auth_type),
                    auth_config=COALESCE(VALUES(auth_config), auth_config),
                    method=COALESCE(VALUES(method), method),
                    headers=COALESCE(VALUES(headers), headers),
                    query_params=COALESCE(VALUES(query_params), query_params),
                    path_params=COALESCE(VALUES(path_params), path_params),
                    timeout_seconds=COALESCE(VALUES(timeout_seconds), timeout_seconds),
                    retry_count=COALESCE(VALUES(retry_count), retry_count),
                    retry_backoff_seconds=COALESCE(VALUES(retry_backoff_seconds), retry_backoff_seconds),
                    updated_at=NOW()
                """,
                (
                    app_id,
                    user_id,
                    (auth_local or {}).get("type", "none") if auth_local else None,
                    json.dumps(auth_local) if auth_local else None,
                    method,
                    json.dumps(headers),
                    json.dumps(query_params),
                    json.dumps(path_params),
                    timeout_local,
                    retry_count_local,
                    retry_backoff_local,
                ),
            )

        conn.commit()

        return jsonify(
            {
                "success": True,
                "message": "App updated and verified",
                "tested_endpoint": {
                    "url": full_url,
                    "method": method,
                    "headers": headers,
                    "query_params": query_params,
                    "path_params": path_params,
                    "body": body,
                },
            }
        )

    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 500

    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@apiconnector_bp.route("/<int:app_id>", methods=["DELETE"])
@permission_required_body("apps.delete")
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
@permission_required_body("apps.delete")
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
@permission_required_body("apps.endpoint.view")
def list_external_apps(user_id):
    conn = connect_to_rds()
    logged_in_user_id, user_id = parse_composite_user_id(user_id)
    cur = conn.cursor(pymysql.cursors.DictCursor)
    try:
        onboarding_role = None
        is_admin = False
        try:
            cur.execute(
                """
                SELECT LineOfBusiness
                FROM business_info
                WHERE user_id_fk = %s
                LIMIT 1
                """,
                (user_id,),
            )
            row = cur.fetchone()
            onboarding_role = (
                (row or {}).get("LineOfBusiness") or ""
            ).strip().lower() or None
        except Exception:
            onboarding_role = None

        try:

            if onboarding_role:
                cur.execute(
                    """
                    SELECT
                        a.id,
                        a.user_id,
                        a.app_name,
                        a.provider,
                        a.base_url,
                        COALESCE(uac.auth_type, a.auth_type) AS auth_type,
                        COALESCE(uac.auth_config, a.auth_config) AS auth_config,
                        COALESCE(uac.headers, a.headers) AS headers,
                        COALESCE(uac.method, a.method) AS method,
                        COALESCE(uac.query_params, a.query_params) AS query_params,
                        COALESCE(uac.timeout_seconds, a.timeout_seconds) AS timeout_seconds,
                        COALESCE(uac.retry_count, a.retry_count) AS retry_count,
                        COALESCE(uac.retry_backoff_seconds, a.retry_backoff_seconds) AS retry_backoff_seconds,
                        a.status,
                        a.last_test_status,
                        a.last_error,
                        a.last_tested_at,
                        a.schedules,
                        a.created_at,
                        a.updated_at,
                        a.is_universal,
                        a.target_onboarding_role
                    FROM external_apps a
                    LEFT JOIN external_app_user_config uac
                      ON uac.app_id = a.id AND uac.user_id = %s
                    WHERE a.user_id = %s
                       OR (a.is_universal = 1 AND LOWER(TRIM(a.target_onboarding_role)) = %s)
                    ORDER BY a.updated_at DESC

                    """,
                    (user_id, user_id, onboarding_role),
                )

            else:
                cur.execute(
                    """
                    SELECT *
                    FROM external_apps
                    WHERE user_id = %s
                    ORDER BY updated_at DESC
                    """,
                    (user_id,),
                )
        except Exception:
            # backward-compatible fallback
            cur.execute(
                """
                SELECT *
                FROM external_apps
                WHERE user_id = %s
                ORDER BY updated_at DESC
                """,
                (user_id,),
            )

        apps = cur.fetchall()
        app = [normalize_row_dynamic(a) for a in apps]
        if user_id in ACCESSIBLE_IDS:
            is_admin = True
        return jsonify({"success": True, "apps": app, "is_admin": is_admin})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        cur.close()
        conn.close()


@apiconnector_bp.route("/<int:app_id>/auth", methods=["PUT"])
@permission_required_body("apps.edit")
def upsert_user_app_auth(app_id):
    conn = None
    cur = None
    try:
        data = request.get_json(force=True) or {}
        user_id = extract_user_id(data)
        if not user_id:
            return jsonify({"success": False, "error": "user_id required"}), 400
        logged_in_user_id, user_id = parse_composite_user_id(user_id)

        auth = data.get("auth") or {"type": "none"}
        auth_type = auth.get("type", "none")

        if not user_id:
            return jsonify({"success": False, "error": "user_id required"}), 400

        conn = connect_to_rds()
        cur = conn.cursor(pymysql.cursors.DictCursor)

        cur.execute(
            """
            SELECT user_id, is_universal, target_onboarding_role
            FROM external_apps
            WHERE id = %s
            """,
            (app_id,),
        )
        app = cur.fetchone()
        if not app:
            return jsonify({"success": False, "error": "App not found"}), 404

        is_owner = app["user_id"] == user_id
        can_access = is_owner

        if not can_access and int(app.get("is_universal") or 0) == 1:
            onboarding_role = get_onboarding_role(cur, user_id)
            target_role = normalize_role(app.get("target_onboarding_role"))
            can_access = bool(
                onboarding_role and target_role and onboarding_role == target_role
            )

        if not can_access:
            return jsonify({"success": False, "error": "Access denied"}), 403

        cur.execute(
            """
            INSERT INTO external_app_user_auth (app_id, user_id, auth_type, auth_config)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                auth_type = VALUES(auth_type),
                auth_config = VALUES(auth_config),
                updated_at = NOW()
            """,
            (app_id, user_id, auth_type, json.dumps(auth)),
        )
        conn.commit()

        return jsonify({"success": True, "message": "User auth saved"})
    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@apiconnector_bp.route("/<int:app_id>/endpoints", methods=["POST"])
@permission_required_body("apps.endpoint.add")
def create_endpoint(app_id):
    data = request.get_json(force=True)

    user_id = extract_user_id(data)
    if not user_id:
        return jsonify({"success": False, "error": "user_id required"}), 400
    logged_in_user_id, user_id = parse_composite_user_id(user_id)

    conn = connect_to_rds()
    cur = conn.cursor(pymysql.cursors.DictCursor)

    try:
        # ---------- Load App ----------
        cur.execute(
            """
            SELECT * FROM external_apps
            WHERE id=%s AND status='active'
        """,
            (app_id,),
        )
        app = cur.fetchone()

        if not app:
            return jsonify({"success": False, "error": "App not found"}), 404

        # ---------- Access Control ----------
        is_owner = str(app["user_id"]) == str(user_id)
        if not is_owner:
            return jsonify({"success": False, "error": "Access denied"}), 403

        name = data.get("name")
        path_template = (data.get("path") or "").strip()

        if not name:
            return jsonify({"success": False, "error": "Endpoint name required"}), 400

        if not path_template:
            return jsonify({"success": False, "error": "Path required"}), 400

        if not path_template.startswith("/"):
            path_template = f"/{path_template}"

        while "//" in path_template:
            path_template = path_template.replace("//", "/")

        method = data.get("method", "GET").upper()
        headers = merge_json(app["headers"], data.get("headers"))
        query_params = merge_json(app["query_params"], data.get("query_params"))
        path_params = data.get("path_params") or {}
        body_template = data.get("body_template")

        timeout_seconds = (
            data.get("timeout_seconds") or app.get("timeout_seconds") or 10
        )
        base_url = app["base_url"].rstrip("/")

        # ---------- Build Final URL ----------
        full_url = build_full_url(base_url, path_template, path_params)

        if query_params:
            from urllib.parse import urlencode

            full_url = f"{full_url}?{urlencode(query_params)}"

        # ---------- Resolve Auth ----------
        auth_config = _get_effective_auth_config(
            cur, app_id, user_id, app.get("auth_config")
        )

        # ---------- Test ----------
        connector = APIConnector(
            userid=user_id,
            config={
                "auth": auth_config,
                "request": {
                    "url": full_url,
                    "method": method,
                    "headers": headers,
                    "body": (
                        body_template if method in ["POST", "PUT", "PATCH"] else None
                    ),
                },
                "timeout": timeout_seconds,
                "retry": {"count": 1, "backoff": 1},
            },
        )

        result = connector.execute()

        if not result.get("success"):
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "Endpoint test failed",
                        "details": result,
                    }
                ),
                400,
            )

        # ---------- Duplicate Check ----------
        cur.execute(
            """
            SELECT id FROM external_app_endpoints
            WHERE app_id=%s AND path=%s AND method=%s
        """,
            (app_id, path_template, method),
        )

        if cur.fetchone():
            return jsonify({"success": False, "error": "Endpoint already exists"}), 409

        # ---------- Insert ----------
        cur.execute(
            """
            INSERT INTO external_app_endpoints
            (app_id, user_id, name, path, method,
             headers, query_params, path_params,
             body_template, timeout_seconds,
             last_tested_at, last_test_status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),'success')
        """,
            (
                app_id,
                app["user_id"],
                name,
                path_template,
                method,
                json.dumps(headers),
                json.dumps(query_params),
                json.dumps(path_params),
                json.dumps(body_template) if body_template else None,
                timeout_seconds,
            ),
        )

        conn.commit()

        return jsonify(
            {
                "success": True,
                "message": "Endpoint created and tested successfully",
                "tested_url": full_url,
            }
        )

    except Exception as e:
        conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        cur.close()
        conn.close()


@apiconnector_bp.route("/endpoints/<int:endpoint_id>", methods=["PUT"])
@permission_required_body("apps.endpoint.edit")
def update_endpoint(endpoint_id):
    data = request.get_json(force=True)

    user_id = extract_user_id(data)
    if not user_id:
        return jsonify({"success": False, "error": "user_id required"}), 400
    logged_in_user_id, user_id = parse_composite_user_id(user_id)

    conn = connect_to_rds()
    cur = conn.cursor(pymysql.cursors.DictCursor)

    try:
        # ---------- Load Endpoint ----------
        cur.execute(
            """
            SELECT e.*, a.base_url, a.auth_config
            FROM external_app_endpoints e
            JOIN external_apps a ON a.id = e.app_id
            WHERE e.id=%s
        """,
            (endpoint_id,),
        )
        endpoint = cur.fetchone()

        if not endpoint:
            return jsonify({"success": False, "error": "Endpoint not found"}), 404

        if str(endpoint["user_id"]) != str(user_id):
            return jsonify({"success": False, "error": "Access denied"}), 403

        # ---------- Merge Updates ----------
        path_template = data.get("path", endpoint["path"])
        method = data.get("method", endpoint["method"]).upper()
        headers = data.get("headers", json.loads(endpoint["headers"] or "{}"))
        query_params = data.get(
            "query_params", json.loads(endpoint["query_params"] or "{}")
        )
        path_params = data.get(
            "path_params", json.loads(endpoint["path_params"] or "{}")
        )
        body_template = data.get(
            "body_template", json.loads(endpoint["body_template"] or "null")
        )
        timeout_seconds = data.get("timeout_seconds", endpoint["timeout_seconds"])

        if not path_template.startswith("/"):
            path_template = f"/{path_template}"

        base_url = endpoint["base_url"].rstrip("/")

        # ---------- Build URL ----------
        full_url = build_full_url(base_url, path_template, path_params)

        if query_params:
            from urllib.parse import urlencode

            full_url = f"{full_url}?{urlencode(query_params)}"

        # ---------- Test ----------
        connector = APIConnector(
            userid=user_id,
            config={
                "auth": json.loads(endpoint["auth_config"] or "{}"),
                "request": {
                    "url": full_url,
                    "method": method,
                    "headers": headers,
                    "body": (
                        body_template if method in ["POST", "PUT", "PATCH"] else None
                    ),
                },
                "timeout": timeout_seconds,
                "retry": {"count": 1, "backoff": 1},
            },
        )

        result = connector.execute()

        if not result.get("success"):
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "Endpoint test failed",
                        "details": result,
                    }
                ),
                400,
            )

        # ---------- Update ----------
        cur.execute(
            """
            UPDATE external_app_endpoints
            SET name=%s,
                path=%s,
                method=%s,
                headers=%s,
                query_params=%s,
                path_params=%s,
                body_template=%s,
                timeout_seconds=%s,
                last_tested_at=NOW(),
                last_test_status='success',
                last_error=NULL
            WHERE id=%s
        """,
            (
                data.get("name", endpoint["name"]),
                path_template,
                method,
                json.dumps(headers),
                json.dumps(query_params),
                json.dumps(path_params),
                json.dumps(body_template) if body_template else None,
                timeout_seconds,
                endpoint_id,
            ),
        )

        conn.commit()

        return jsonify(
            {
                "success": True,
                "message": "Endpoint updated and verified",
                "tested_url": full_url,
            }
        )

    except Exception as e:
        conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        cur.close()
        conn.close()


@apiconnector_bp.route("/<int:app_id>/endpoints", methods=["GET"])
@permission_required_body("apps.endpoint.view")
def list_endpoints(app_id):
    conn = connect_to_rds()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    try:
        user_id = request.args.get("user_id") or request.args.get("userid")
        is_admin = False
        # backward-compatible fallback
        if not user_id:
            cur.execute("SELECT user_id FROM external_apps WHERE id=%s", (app_id,))
            row = cur.fetchone() or {}
            user_id = row.get("user_id")

        if not user_id:
            return jsonify({"success": False, "error": "user_id required"}), 400
        logged_in_user_id, user_id = parse_composite_user_id(user_id)

        cur.execute(
            """
            SELECT *
            FROM external_app_endpoints
            WHERE app_id=%s AND user_id=%s
            """,
            (app_id, user_id),
        )

        endpoints = cur.fetchall()
        app = [normalize_row_dynamic(a) for a in endpoints]
        if user_id in ACCESSIBLE_IDS:
            is_admin = True
        return jsonify({"success": True, "endpoints": app, "is_admin": is_admin})
    finally:
        cur.close()
        conn.close()


@apiconnector_bp.route("/endpoints/<int:endpoint_id>/test", methods=["POST"])
@permission_required_body("apps.endpoint.test")
def test_endpoint(endpoint_id, userid=None):
    conn = None
    cur = None

    try:
        data = request.get_json(force=True) or {}
        userid = userid or data.get("user_id")

        if not userid:
            return jsonify({"success": False, "error": "user_id required"}), 400
        logged_in_user_id, userid = parse_composite_user_id(userid)

        conn = connect_to_rds()
        cur = conn.cursor(pymysql.cursors.DictCursor)

        # --------------------------------
        # 1️⃣ Load endpoint + app
        # --------------------------------
        cur.execute(
            """
            SELECT e.*, a.base_url, a.auth_config
            FROM external_app_endpoints e
            JOIN external_apps a ON a.id = e.app_id
            WHERE e.id = %s
            """,
            (endpoint_id,),
        )

        row = cur.fetchone()
        if not row:
            return jsonify({"success": False, "error": "Endpoint not found"}), 404

        # --------------------------------
        # 2️⃣ Merge Stored + Runtime Overrides
        # --------------------------------
        method = data.get("method", row["method"]).upper()

        headers = {
            **json.loads(row["headers"] or "{}"),
            **(data.get("headers") or {}),
        }

        query_params = {
            **json.loads(row["query_params"] or "{}"),
            **(data.get("query_params") or {}),
        }

        path_params = {
            **json.loads(row["path_params"] or "{}"),
            **(data.get("path_params") or {}),
        }

        body = data.get("body") or json.loads(row["body_template"] or "{}")

        timeout_seconds = (
            data.get("timeout_seconds") or row.get("timeout_seconds") or 10
        )

        retry_count = data.get("retry_count", 1)
        retry_backoff = data.get("retry_backoff_seconds", 1)

        # --------------------------------
        # 3️⃣ Build Final URL Properly
        # --------------------------------
        base_url = row["base_url"].rstrip("/")
        path_template = row["path"]

        full_url = build_full_url(base_url, path_template, path_params)

        if query_params:
            from urllib.parse import urlencode

            full_url = f"{full_url}?{urlencode(query_params)}"

        # --------------------------------
        # 4️⃣ Resolve Auth
        # --------------------------------
        auth_config = _get_effective_auth_config(
            cur, row["app_id"], userid, row["auth_config"]
        )

        # --------------------------------
        # 5️⃣ Build Connector Config
        # --------------------------------
        config = {
            "auth": auth_config,
            "request": {
                "url": full_url,
                "method": method,
                "headers": headers,
                "body": body,
            },
            "timeout": timeout_seconds,
            "retry": {
                "count": retry_count,
                "backoff": retry_backoff,
            },
        }

        connector = APIConnector(userid=userid, config=config)
        result = connector.execute()

        # --------------------------------
        # 6️⃣ Store Test Result
        # --------------------------------
        cur.execute(
            """
            UPDATE external_app_endpoints
            SET last_tested_at = NOW(),
                last_test_status = %s,
                last_error = %s
            WHERE id = %s
            """,
            (
                "success" if result.get("success") else "failed",
                None if result.get("success") else json.dumps(result),
                endpoint_id,
            ),
        )

        conn.commit()

        return jsonify(
            {
                "success": result.get("success"),
                "tested_endpoint": {
                    "url": full_url,
                    "method": method,
                    "headers": headers,
                    "query_params": query_params,
                    "path_params": path_params,
                    "body": body,
                },
                "response": result,
            }
        )

    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 500

    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@apiconnector_bp.route("/<int:app_id>/test", methods=["POST"])
@permission_required_body("apps.endpoint.test")
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
        if not userid:
            return jsonify({"success": False, "error": "user_id required"}), 400
        logged_in_user_id, userid = parse_composite_user_id(userid)
        method = test_config.get("method", "GET")
        headers = test_config.get("headers", {})
        query_params = test_config.get("query_params", {})
        body = test_config.get("body")

        base_url = app["base_url"].rstrip("/")
        full_url = f"{base_url}{path}"

        config = {
            "auth": _get_effective_auth_config(
                cur, app["id"], userid, app["auth_config"]
            ),
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
@permission_required_body("apps.endpoint.execute")
def execute_app(app_id):
    payload = request.get_json(force=True) or {}
    userid = payload.get("user_id")

    if not userid:
        return jsonify({"success": False, "error": "user_id required"}), 400
    logged_in_user_id, userid = parse_composite_user_id(userid)

    try:
        result = _execute_app_internal(app_id, userid)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@apiconnector_bp.route("/endpoints/<int:endpoint_id>/execute", methods=["POST"])
@permission_required_body("apps.endpoint.execute")
async def execute_endpoint(endpoint_id, userid=None):

    payload = request.get_json() or {}
    # print("payload for execution", payload)
    userid = payload.get("user_id")
    context = payload.get("context", None)
    runtime_params = payload.get("runtime_params", None)
    logged_in_user_id, userid = parse_composite_user_id(userid)
    try:
        result = await _execute_endpoint_internal(
            endpoint_id, userid, context, runtime_params
        )
        return jsonify(result)
    except Exception as e:
        # print("error on executing endpoint", e)
        return jsonify({"success": False, "error": str(e)}), 500


@apiconnector_bp.route("/endpoints/<int:endpoint_id>", methods=["DELETE"])
@permission_required_body("apps.endpoint.delete")
def delete_endpoint(endpoint_id):
    conn = None
    cur = None

    try:

        data = request.get_json(silent=True) or {}

        user_id = request.args.get("user_id") or data.get("user_id")

        if not user_id:
            return jsonify({"success": False, "error": "user_id required"}), 400
        logged_in_user_id, user_id = parse_composite_user_id(user_id)
        conn = connect_to_rds()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT id
            FROM external_app_endpoints
            WHERE id = %s AND user_id = %s
            """,
            (endpoint_id, user_id),
        )
        if not cur.fetchone():
            return jsonify({"success": False, "error": "Endpoint not found"}), 404

        cur.execute(
            """
            DELETE FROM external_app_endpoints
            WHERE id = %s AND user_id = %s
            """,
            (endpoint_id, user_id),
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
@permission_required_body("apps.endpoint.schedule")
async def schedule_app(app_id):
    body = request.json or {}
    userid = body.get("user_id")
    activation = body.get("scheduledActivation")

    if not userid:
        return jsonify({"error": "user_id missing"}), 400
    logged_in_user_id, userid = parse_composite_user_id(userid)

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
            start_date=data["startDate"],
            end_date=data["endDate"],
            start_time=data["startTime"],
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
@permission_required_body("apps.endpoint.schedule")
async def schedule_endpoint(endpoint_id):
    body = request.json or {}
    # print("body received for schedule ", body)
    userid = body.get("user_id")
    activation = body.get("scheduledActivation")

    if not userid:
        return jsonify({"error": "user_id missing"}), 400
    logged_in_user_id, userid = parse_composite_user_id(userid)

    schedule_type, data = resolve_schedule_from_activation(activation)
    timezone = data.get("timezone", "UTC")
    if values := get_schedule_endpointdetails(endpoint_id):
        # print("values", values, type(values))

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
            start_date=data["startDate"],
            end_date=data["endDate"],
            start_time=data["startTime"],
            # data["intervalMinutes"],
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
@permission_required_body("apps.endpoint.schedule")
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
        # print(f'type of sch.get("execution_key") = {type(sch_key)}, value = {sch_key}')
        # print(f"type of execution_key = {type(execution_key)}, value = {execution_key}")
        if sch.get("execution_key") == execution_key:
            execution_key = sch.get("execution_key")
            sch["status"] = "inactive"

            # Stop celery beat/interval
            if sch.get("celery_entry"):
                # print("stopping a beat")
                await APIConnectorScheduler.disable_celery_entry(sch["celery_entry"])

            # Stop one-time task
            if sch.get("celery_task_id"):
                # print("stopping a task")
                await APIConnectorScheduler.revoke_task(sch["celery_task_id"])
            if sch.get("celery_task_ids"):
                # print("stopping multiple tasks", len(sch["celery_task_ids"]))
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
@permission_required_body("apps.endpoint.view")
def list_endpoint_runs(endpoint_id):
    userid = request.args.get("user_id")

    conn = connect_to_rds()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    logged_in_user_id, userid = parse_composite_user_id(userid)

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
@permission_required_body("apps.endpoint.view")
def get_endpoint_run(endpoint_id, filename):
    userid = request.args.get("user_id")

    conn = connect_to_rds()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    logged_in_user_id, userid = parse_composite_user_id(userid)

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


@apiconnector_bp.route("/admin/pushapp", methods=["POST"])
@permission_required_body("apps.endpoint.push")
def push_global_app():

    body = request.json or {}
    print("body", body)

    user_id = body.get("user_id")
    admin_external_app_id = body.get("app_id")  # external_apps.id
    if not admin_external_app_id:
        return jsonify({"error": "required fields needed"}), 400

    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    logged_in_user_id, user_id = parse_composite_user_id(user_id)
    if user_id not in ACCESSIBLE_IDS:
        return jsonify({"error": "Unauthorized"}), 403

    app_name = body.get("app_name")
    base_url = body.get("base_url")

    if not app_name or not base_url:
        return jsonify({"error": "app_name and base_url required"}), 400

    provider = body.get("provider", "global")
    auth_type = body.get("auth_type", "none")
    auth_config = body.get("auth_config")
    headers = body.get("headers")
    method = body.get("method", "GET")
    query_params = body.get("query_params")
    path_params = body.get("path_params")
    timeout_seconds = body.get("timeout_seconds", 10)
    is_universal = body.get("is_universal", True)
    status = body.get("status", "development")
    notes = body.get("notes")
    required_config_schema = body.get("required_config_schema")

    connection = connect_to_rds()
    cursor = connection.cursor()

    try:
        # Convert dicts to JSON
        auth_config = json.dumps(auth_config) if auth_config is not None else None
        headers = json.dumps(headers) if headers is not None else None
        query_params = json.dumps(query_params) if query_params is not None else None
        path_params = json.dumps(path_params) if path_params is not None else None
        required_config_schema = (
            json.dumps(required_config_schema)
            if required_config_schema is not None
            else None
        )

        # 1️⃣ Insert into global_apps
        cursor.execute(
            """
            INSERT INTO global_apps (
                app_name,
                provider,
                base_url,
                auth_type,
                auth_config,
                headers,
                method,
                query_params,
                path_params,
                timeout_seconds,
                is_universal,
                status,
                notes,
                required_config_schema
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                app_name,
                provider,
                base_url,
                auth_type,
                auth_config,
                headers,
                method,
                query_params,
                path_params,
                timeout_seconds,
                is_universal,
                status,
                notes,
                required_config_schema,
            ),
        )

        new_global_app_id = cursor.lastrowid

        # 2️⃣ Update admin external_apps table
        if admin_external_app_id:
            cursor.execute(
                """
                UPDATE external_apps
                SET 
                    is_universal = 1,
                    source_global_app_id = %s,
                    updated_at = NOW()
                WHERE id = %s AND user_id = %s
                """,
                (new_global_app_id, admin_external_app_id, user_id),
            )

        connection.commit()

        return jsonify(
            {
                "success": True,
                "message": "Global app created and admin app linked successfully",
                "global_app_id": new_global_app_id,
            }
        )

    except Exception as e:
        print("error", e)
        connection.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        cursor.close()
        connection.close()


@apiconnector_bp.route("/admin/pushapp_endpoint", methods=["POST"])
@permission_required_body("apps.endpoint.push")
def push_global_app_endpoint():

    body = request.json or {}

    user_id = body.get("user_id")
    admin_external_endpoint_id = body.get("endpoint_id")
    admin_app_id = body.get("app_id")  # external_apps.id

    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    logged_in_user_id, user_id = parse_composite_user_id(user_id)

    if user_id not in ACCESSIBLE_IDS:
        return jsonify({"error": "Unauthorized"}), 403

    if not admin_app_id:
        return jsonify({"error": "app_id required"}), 400

    name = body.get("name")
    path = body.get("path")

    if not name or not path:
        return jsonify({"error": "name and path required"}), 400

    connection = connect_to_rds()
    cursor = connection.cursor(pymysql.cursors.DictCursor)

    try:
        # 🔹 Step 1: Get source_global_app_id
        cursor.execute(
            """
            SELECT source_global_app_id
            FROM external_apps
            WHERE id = %s AND user_id = %s
            """,
            (admin_app_id, user_id),
        )

        app_row = cursor.fetchone()

        if not app_row or not app_row["source_global_app_id"]:
            return (
                jsonify(
                    {"error": "App is not global. Please push app to global first."}
                ),
                400,
            )

        global_app_id = app_row["source_global_app_id"]
        print("global app id", global_app_id, type(global_app_id))

        # 🔹 Optional fields
        method = body.get("method", "GET")
        headers = body.get("headers")
        query_params = body.get("query_params")
        path_params = body.get("path_params")
        body_template = body.get("body_template")
        timeout_seconds = body.get("timeout_seconds")
        status = body.get("status", "development")
        notes = body.get("notes")
        required_config_schema = body.get("required_config_schema")
        is_active = body.get("is_active", True)

        # Convert dicts to JSON
        headers = json.dumps(headers) if headers is not None else None
        query_params = json.dumps(query_params) if query_params is not None else None
        path_params = json.dumps(path_params) if path_params is not None else None
        body_template = json.dumps(body_template) if body_template is not None else None
        required_config_schema = (
            json.dumps(required_config_schema)
            if required_config_schema is not None
            else None
        )

        # 🔹 Step 2: Insert into global_app_endpoints
        cursor.execute(
            """
            INSERT INTO global_app_endpoints (
                app_id,
                name,
                path,
                method,
                headers,
                query_params,
                path_params,
                body_template,
                timeout_seconds,
                status,
                notes,
                required_config_schema,
                is_active
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                global_app_id,  # ✅ Correct mapping here
                name,
                path,
                method,
                headers,
                query_params,
                path_params,
                body_template,
                timeout_seconds,
                status,
                notes,
                required_config_schema,
                is_active,
            ),
        )

        new_global_endpoint_id = cursor.lastrowid

        # 🔹 Step 3: Update admin endpoint if provided
        if admin_external_endpoint_id:
            cursor.execute(
                """
                UPDATE external_app_endpoints
                SET
                    is_universal = 1,
                    source_global_endpoint_id = %s,
                    updated_at = NOW()
                WHERE id = %s AND user_id = %s
                """,
                (
                    new_global_endpoint_id,
                    admin_external_endpoint_id,
                    user_id,
                ),
            )

        connection.commit()

        return jsonify(
            {
                "success": True,
                "message": "Global endpoint created and linked successfully",
                "global_endpoint_id": new_global_endpoint_id,
            }
        )

    except Exception as e:
        connection.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        cursor.close()
        connection.close()


@apiconnector_bp.route("/global/apps/<user_id>", methods=["GET"])
@permission_required_body("apps.view")
def list_global_apps(user_id):

    connection = connect_to_rds()
    cursor = connection.cursor(pymysql.cursors.DictCursor)

    try:
        logged_in_user_id, user_id = parse_composite_user_id(user_id)
        is_admin = user_id in ACCESSIBLE_IDS

        if is_admin:
            cursor.execute(
                """
                SELECT 
                    g.*,
                    ea.id AS external_app_id,
                    ea.created_at AS installed_on
                FROM global_apps g
                LEFT JOIN external_apps ea
                    ON ea.source_global_app_id = g.id
                    AND ea.user_id = %s
            """,
                (user_id,),
            )
        else:
            cursor.execute(
                """
                SELECT 
                    g.*,
                    ea.id AS external_app_id,
                    ea.created_at AS installed_on
                FROM global_apps g
                LEFT JOIN external_apps ea
                    ON ea.source_global_app_id = g.id
                    AND ea.user_id = %s
                WHERE g.status = 'ready'
            """,
                (user_id,),
            )

        rows = cursor.fetchall()

        apps = []
        for row in rows:
            app = dict(row)

            if row.get("external_app_id"):
                app["installed"] = True
                app["installed_on"] = row.get("installed_on")
            else:
                app["installed"] = False
                app["installed_on"] = None

            # remove helper fields
            app.pop("external_app_id", None)

            apps.append(app)

        return jsonify({"success": True, "apps": apps, "is_admin": is_admin})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        cursor.close()
        connection.close()


@apiconnector_bp.route(
    "/global/app_endpoints/<string:user_id>/<int:app_id>", methods=["GET"]
)
@permission_required_body("apps.view")
def list_global_app_endpoints(user_id, app_id):
    connection = None
    cursor = None
    logged_in_user_id, user_id = parse_composite_user_id(user_id)

    try:
        connection = connect_to_rds()
        cursor = connection.cursor(pymysql.cursors.DictCursor)

        # --------------------------------
        # 1️⃣ Check if app exists
        # --------------------------------
        cursor.execute(
            "SELECT id FROM global_apps WHERE id = %s",
            (app_id,),
        )
        app = cursor.fetchone()

        if not app:
            return jsonify({"success": False, "error": "Global app not found"}), 404

        # --------------------------------
        # 2️⃣ Admin Check
        # --------------------------------
        is_admin = user_id in ACCESSIBLE_IDS

        # --------------------------------
        # 3️⃣ Fetch Endpoints with Install Status
        # --------------------------------
        if is_admin:
            cursor.execute(
                """
                SELECT 
                    g.*,
                    e.id AS external_endpoint_id,
                    e.created_at AS installed_on
                FROM global_app_endpoints g
                LEFT JOIN external_app_endpoints e
                    ON e.source_global_endpoint_id = g.id
                    AND e.user_id = %s
                WHERE g.app_id = %s
                ORDER BY g.created_at DESC
                """,
                (user_id, app_id),
            )
        else:
            cursor.execute(
                """
                SELECT 
                    g.*,
                    e.id AS external_endpoint_id,
                    e.created_at AS installed_on
                FROM global_app_endpoints g
                LEFT JOIN external_app_endpoints e
                    ON e.source_global_endpoint_id = g.id
                    AND e.user_id = %s
                WHERE g.app_id = %s
                  AND g.status = 'ready'
                ORDER BY g.created_at DESC
                """,
                (user_id, app_id),
            )

        rows = cursor.fetchall()

        endpoints = []
        for row in rows:
            ep = dict(row)

            if row.get("external_endpoint_id"):
                ep["installed"] = True
                ep["installed_on"] = row.get("installed_on")
            else:
                ep["installed"] = False
                ep["installed_on"] = None

            # Remove helper column
            ep.pop("external_endpoint_id", None)

            endpoints.append(ep)

        return jsonify(
            {
                "success": True,
                "endpoints": endpoints,
                "is_admin": is_admin,
                "total": len(endpoints),
            }
        )

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@apiconnector_bp.route("/global/apps/change", methods=["POST"])
@permission_required_body("apps.endpoint.push")
def change_global_app():
    body = request.json or {}
    user_id = body.get("user_id")
    app_id = body.get("app_id")

    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    logged_in_user_id, user_id = parse_composite_user_id(user_id)

    if user_id not in ACCESSIBLE_IDS:
        return jsonify({"error": "Unauthorized"}), 403

    if not app_id:
        return jsonify({"error": "app_id required"}), 400

    connection = connect_to_rds()
    cursor = connection.cursor()

    try:
        # Check if app exists
        cursor.execute("SELECT id FROM global_apps WHERE id = %s", (app_id,))
        if not cursor.fetchone():
            return jsonify({"error": "App not found"}), 404

        # Allowed fields
        updatable_fields = [
            "app_name",
            "provider",
            "base_url",
            "auth_type",
            "auth_config",
            "headers",
            "method",
            "query_params",
            "path_params",
            "timeout_seconds",
            "is_universal",
            "status",
            "notes",
            "required_config_schema",
        ]

        update_clauses = []
        values = []

        for field in updatable_fields:
            if field in body:
                value = body[field]

                # Convert dict fields to JSON string
                if field in [
                    "auth_config",
                    "headers",
                    "query_params",
                    "path_params",
                    "required_config_schema",
                ]:
                    value = json.dumps(value) if value is not None else None

                update_clauses.append(f"{field} = %s")
                values.append(value)

        if not update_clauses:
            return jsonify({"error": "No fields provided to update"}), 400

        # Add updated_at
        update_clauses.append("updated_at = NOW()")

        query = f"""
            UPDATE global_apps
            SET {', '.join(update_clauses)}
            WHERE id = %s
        """

        values.append(app_id)

        cursor.execute(query, tuple(values))
        connection.commit()

        return jsonify({"success": True, "message": "Global app updated successfully"})

    except Exception as e:
        connection.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        cursor.close()
        connection.close()


@apiconnector_bp.route("/global/app_endpoint/change", methods=["POST"])
@permission_required_body("apps.endpoint.push")
def change_global_app_endpoint():

    body = request.json or {}
    user_id = body.get("user_id")
    endpoint_id = body.get("endpoint_id")

    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    logged_in_user_id, user_id = parse_composite_user_id(user_id)

    if user_id not in ACCESSIBLE_IDS:
        return jsonify({"error": "Unauthorized"}), 403

    if not endpoint_id:
        return jsonify({"error": "endpoint_id required"}), 400

    connection = connect_to_rds()
    cursor = connection.cursor(pymysql.cursors.DictCursor)

    try:
        # ✅ Correct table name
        cursor.execute(
            "SELECT id FROM global_app_endpoints WHERE id = %s", (endpoint_id,)
        )

        if not cursor.fetchone():
            return jsonify({"error": "Endpoint not found"}), 404

        # ✅ Only fields that exist in global_app_endpoints
        updatable_fields = [
            "name",
            "path",
            "method",
            "headers",
            "query_params",
            "path_params",
            "body_template",
            "timeout_seconds",
            "status",
            "notes",
            "required_config_schema",
            "is_active",
        ]

        update_clauses = []
        values = []

        for field in updatable_fields:
            if field in body:
                value = body[field]

                # Convert JSON fields
                if field in [
                    "headers",
                    "query_params",
                    "path_params",
                    "body_template",
                    "required_config_schema",
                ]:
                    value = json.dumps(value) if value is not None else None

                update_clauses.append(f"{field} = %s")
                values.append(value)

        if not update_clauses:
            return jsonify({"error": "No fields provided to update"}), 400

        update_clauses.append("updated_at = NOW()")

        query = f"""
            UPDATE global_app_endpoints
            SET {', '.join(update_clauses)}
            WHERE id = %s
        """

        values.append(endpoint_id)

        cursor.execute(query, tuple(values))
        connection.commit()

        return jsonify(
            {"success": True, "message": "Global endpoint updated successfully"}
        )

    except Exception as e:
        connection.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        cursor.close()
        connection.close()


@apiconnector_bp.route(
    "/global_endpoints/<int:app_id>/<int:endpoint_id>/test",
    methods=["POST"],
)
@permission_required_body("apps.endpoint.test")
def global_test_endpoint(app_id, endpoint_id):

    conn = connect_to_rds()
    cur = conn.cursor(pymysql.cursors.DictCursor)

    try:
        data = request.get_json(force=True) or {}

        # ----------------------------------
        # ✅ Validate user_id
        # ----------------------------------
        user_id = data.get("user_id")
        if not user_id:
            return jsonify({"success": False, "error": "user_id required"}), 400
        logged_in_user_id, user_id = parse_composite_user_id(user_id)

        # ----------------------------------
        # ✅ Check endpoint exists
        # ----------------------------------
        cur.execute(
            "SELECT id FROM global_app_endpoints WHERE id = %s AND app_id = %s",
            (endpoint_id, app_id),
        )
        exists = cur.fetchone()

        if not exists:
            return jsonify({"success": False, "error": "Endpoint not found"}), 404

        # ----------------------------------
        # ✅ Check if user installed this app
        # ----------------------------------
        cur.execute(
            """
            SELECT *
            FROM external_apps
            WHERE user_id = %s
              AND source_global_app_id = %s
              AND status = "active"
            """,
            (user_id, app_id),
        )
        installed_app = cur.fetchone()

        # ----------------------------------
        # ✅ Frontend data (always accepted)
        # ----------------------------------
        frontend_base_url = data.get("base_url")
        path = data.get("path")
        method = data.get("method", "GET")
        frontend_headers = data.get("headers", {})
        query_params = data.get("query_params", {})
        path_params = data.get("path_params", {})
        request_body = data.get("body", {})
        timeout = data.get("timeout", 30)
        frontend_auth = data.get("config", {})

        if not path:
            return jsonify({"success": False, "error": "path is required"}), 400

        # ----------------------------------
        # ✅ FINAL BASE URL
        # ----------------------------------
        if installed_app:
            base_url = installed_app.get("base_url") or frontend_base_url
        else:
            base_url = frontend_base_url

        if not base_url:
            return jsonify({"success": False, "error": "base_url required"}), 400

        # ----------------------------------
        # ✅ Merge Headers (installed first, frontend overrides)
        # ----------------------------------
        final_headers = {}

        if installed_app and installed_app.get("headers"):
            try:
                final_headers.update(json.loads(installed_app.get("headers") or "{}"))
            except Exception:
                pass

        final_headers.update(frontend_headers or {})

        # ----------------------------------
        # ✅ Merge Auth
        # ----------------------------------
        if installed_app and installed_app.get("auth_config"):
            try:
                final_auth = json.loads(installed_app.get("auth_config") or "{}")
            except Exception:
                final_auth = {}
        else:
            final_auth = {}

        # frontend overrides auth if provided
        if frontend_auth:
            final_auth.update(frontend_auth)

        # ----------------------------------
        # ✅ Build final URL
        # ----------------------------------
        try:
            final_url = build_full_url(
                base_url=base_url,
                path=path,
                path_params=path_params,
            )
        except ValueError as e:
            return jsonify({"success": False, "error": str(e)}), 400

        # ----------------------------------
        # ✅ Build execution config
        # ----------------------------------
        config = {
            "auth": final_auth,
            "request": {
                "url": final_url,
                "method": method,
                "headers": final_headers,
                "query_params": query_params,
                "body": request_body,
                "timeout": timeout,
            },
        }

        # ----------------------------------
        # ✅ Execute API
        # ----------------------------------
        connector = APIConnector(userid=user_id, config=config)
        result = connector.execute()

        return jsonify(result)

    except Exception as e:
        print("error:", e)
        return jsonify({"success": False, "error": str(e)}), 500

    finally:
        cur.close()
        conn.close()


@apiconnector_bp.route("/user/global-app/instantiate", methods=["POST"])
@permission_required_body("apps.install")
def instantiate_global_app_for_user():

    conn = connect_to_rds()
    cur = conn.cursor(pymysql.cursors.DictCursor)

    try:
        data = request.get_json(force=True) or {}

        user_id = data.get("user_id")
        global_app_id = data.get("app_id")
        user_config = data.get("config", {})

        if not user_id or not global_app_id:
            return jsonify({"error": "user_id and global_app_id required"}), 400
        logged_in_user_id, user_id = parse_composite_user_id(user_id)

        # ==========================================================
        # 1️⃣ Fetch Global App
        # ==========================================================
        cur.execute("SELECT * FROM global_apps WHERE id = %s", (global_app_id,))
        app = cur.fetchone()

        if not app:
            return jsonify({"error": "Global app not found"}), 404

        required_schema = app.get("required_config_schema") or {}

        if isinstance(required_schema, str):
            required_schema = json.loads(required_schema)

        # ==========================================================
        # 2️⃣ Validate App-Level Required Config
        # ==========================================================
        for field, meta in required_schema.items():
            if meta.get("required") and field not in user_config:
                return jsonify({"error": f"{field} is required"}), 400

        # ==========================================================
        # 3️⃣ Build Auth Config
        # ==========================================================
        auth_type = app["auth_type"]

        if auth_type == "bearer":
            auth_config = {"type": "bearer", "token": user_config.get("bearer_token")}
        elif auth_type == "api_key":
            auth_config = {"type": "api_key", "key": user_config.get("api_key")}
        elif auth_type == "basic":
            auth_config = {
                "type": "basic",
                "username": user_config.get("username"),
                "password": user_config.get("password"),
            }
        else:
            auth_config = {"type": "none"}

        # ==========================================================
        # 4️⃣ Merge Headers
        # ==========================================================
        global_headers = app.get("headers") or {}
        if isinstance(global_headers, str):
            global_headers = json.loads(global_headers)

        user_headers = user_config.get("headers", {})

        final_headers = {}
        final_headers.update(global_headers)
        final_headers.update(user_headers)

        # ==========================================================
        # 5️⃣ Insert external_apps
        # ==========================================================
        cur.execute(
            """
            INSERT INTO external_apps (
                user_id,
                app_name,
                provider,
                base_url,
                auth_type,
                auth_config,
                headers,
                method,
                is_universal,
                source_global_app_id,
                query_params,
                path_params,
                timeout_seconds,
                retry_count,
                retry_backoff_seconds,
                status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
            (
                user_id,
                app["app_name"],
                app["provider"],
                app["base_url"],
                auth_type,
                json.dumps(auth_config),
                json.dumps(final_headers),
                app["method"],
                0,
                app["id"],
                json.dumps(user_config.get("query_params", {})),
                json.dumps(user_config.get("path_params", {})),
                app.get("timeout_seconds", 10),
                0,
                0,
                "active",
            ),
        )

        external_app_id = cur.lastrowid

        # ==========================================================
        # 6️⃣ AUTO-INSTALL ELIGIBLE ENDPOINTS
        # ==========================================================
        cur.execute(
            """
            SELECT * FROM global_app_endpoints
            WHERE app_id = %s
              AND is_active = 1
              AND status = 'ready'
        """,
            (global_app_id,),
        )

        endpoints = cur.fetchall()

        def parse_json(val):
            if not val:
                return {}
            if isinstance(val, str):
                return json.loads(val)
            return val

        def requires_user_config(schema):
            if not schema or not isinstance(schema, dict):
                return False

            for section in schema.values():

                # Case 1: section is already boolean
                if isinstance(section, bool):
                    if section is True:
                        return True
                    continue

                # Case 2: section is dict
                if isinstance(section, dict):
                    for meta in section.values():

                        # If meta is boolean
                        if isinstance(meta, bool):
                            if meta is True:
                                return True

                        # If meta is proper schema dict
                        elif isinstance(meta, dict):
                            if meta.get("required"):
                                return True

            return False

        installed_count = 0

        for ep in endpoints:
            ep_schema = parse_json(ep.get("required_config_schema"))

            # Skip endpoints that require config
            if requires_user_config(ep_schema):
                continue

            endpoint_headers = parse_json(ep.get("headers"))

            merged_headers = {}
            merged_headers.update(final_headers)
            merged_headers.update(endpoint_headers)

            cur.execute(
                """
                INSERT INTO external_app_endpoints (
                    app_id,
                    name,
                    user_id,
                    path,
                    method,
                    headers,
                    query_params,
                    path_params,
                    body_template,
                    timeout_seconds,
                    is_universal,
                    source_global_endpoint_id,
                    is_active
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
                (
                    external_app_id,
                    ep["name"],
                    user_id,
                    ep["path"],
                    ep["method"],
                    json.dumps(merged_headers),
                    json.dumps(parse_json(ep.get("query_params"))),
                    json.dumps(parse_json(ep.get("path_params"))),
                    json.dumps(parse_json(ep.get("body_template"))),
                    ep.get("timeout_seconds") or app.get("timeout_seconds"),
                    0,
                    ep["id"],
                    1,
                ),
            )

            installed_count += 1

        conn.commit()
        return jsonify(
            {
                "success": True,
                "message": "App installed successfully",
                "external_app_id": external_app_id,
                "auto_installed_endpoints": installed_count,
            }
        )

    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        cur.close()
        conn.close()


@apiconnector_bp.route("/user/global-endpoint/instantiate", methods=["POST"])
@permission_required_body("apps.install")
def instantiate_global_endpoint():
    conn = connect_to_rds()
    cur = conn.cursor(pymysql.cursors.DictCursor)

    try:
        data = request.get_json(force=True) or {}

        user_id = data.get("user_id")
        external_app_id = data.get("external_app_id")
        global_endpoint_id = data.get("global_endpoint_id")
        user_config = data.get("config", {}) or {}

        if not user_id or not external_app_id or not global_endpoint_id:
            return jsonify({"error": "Missing required identifiers"}), 400
        logged_in_user_id, user_id = parse_composite_user_id(user_id)

        # ==========================================================
        # 1️⃣ Validate External App Belongs To User
        # ==========================================================
        cur.execute(
            """
            SELECT *
            FROM external_apps
            WHERE user_id = %s
              AND source_global_app_id = %s
              AND status = "active"
            """,
            (user_id, external_app_id),
        )

        external_app = cur.fetchone()
        if not external_app:
            return jsonify({"error": "External app not found"}), 404

        # ==========================================================
        # 2️⃣ Fetch Global Endpoint Template
        # ==========================================================
        cur.execute(
            """
            SELECT * FROM global_app_endpoints
            WHERE id = %s AND is_active = 1
            """,
            (global_endpoint_id,),
        )

        endpoint = cur.fetchone()
        if not endpoint:
            return jsonify({"error": "Global endpoint not found"}), 404

        # ----------------------------------------------------------
        # JSON Safe Parser
        # ----------------------------------------------------------
        def parse_json(val):
            if not val:
                return {}
            if isinstance(val, str):
                try:
                    return json.loads(val)
                except Exception:
                    return {}
            return val

        endpoint_headers = parse_json(endpoint.get("headers"))
        required_schema = parse_json(endpoint.get("required_config_schema"))
        body_template = parse_json(endpoint.get("body_template"))
        app_headers = parse_json(external_app.get("headers"))

        # ==========================================================
        # 3️⃣ Normalize Required Schema (Flat + Structured Support)
        # ==========================================================
        def normalize_required_schema(schema):
            structured = {
                "path_params": {},
                "query_params": {},
                "headers": {},
                "body": {},
            }

            if not schema:
                return structured

            # Already structured
            if any(k in schema for k in structured.keys()):
                for section in structured:
                    structured[section] = schema.get(section, {}) or {}
                return structured

            # Flat schema handling
            for key, meta in schema.items():
                if key.startswith("path_"):
                    structured["path_params"][key.replace("path_", "")] = meta
                elif key.startswith("query_"):
                    structured["query_params"][key.replace("query_", "")] = meta
                elif key.startswith("header_"):
                    structured["headers"][key.replace("header_", "")] = meta
                elif key.startswith("body_"):
                    structured["body"][key.replace("body_", "")] = meta

            return structured

        normalized_schema = normalize_required_schema(required_schema)

        # ==========================================================
        # 4️⃣ Universal Validator (Flat + Structured Frontend Support)
        # ==========================================================
        def validate_and_extract(section_name, schema_section):
            validated = {}

            structured_input = user_config.get(section_name, {}) or {}
            root_structured_input = data.get(section_name, {}) or {}

            prefix_map = {
                "path_params": "path",
                "query_params": "query",
                "headers": "header",
                "body": "body",
            }

            for key, meta in schema_section.items():

                flat_key = f"{prefix_map[section_name]}_{key}"

                value = None

                # 1️⃣ Structured inside config
                if key in structured_input:
                    value = structured_input[key]

                # 2️⃣ Flat inside config
                elif flat_key in user_config:
                    value = user_config[flat_key]

                # 3️⃣ Structured at root level
                elif key in root_structured_input:
                    value = root_structured_input[key]

                # 4️⃣ Flat at root level
                elif flat_key in data:
                    value = data[flat_key]

                if meta.get("required") and value is None:
                    raise ValueError(f"{section_name}.{key} is required")

                if value is not None:
                    validated[key] = value

            return validated

        validated_path = validate_and_extract(
            "path_params", normalized_schema["path_params"]
        )
        validated_query = validate_and_extract(
            "query_params", normalized_schema["query_params"]
        )
        validated_headers = validate_and_extract(
            "headers", normalized_schema["headers"]
        )
        validated_body = validate_and_extract("body", normalized_schema["body"])

        # ==========================================================
        # 5️⃣ Merge Headers (App + Endpoint + User)
        # ==========================================================
        final_headers = {}
        final_headers.update(app_headers)
        final_headers.update(endpoint_headers)
        final_headers.update(validated_headers)

        # ==========================================================
        # 6️⃣ Merge Body With Template
        # ==========================================================
        final_body = {}
        if body_template:
            final_body.update(body_template)

        final_body.update(validated_body)

        # ==========================================================
        # 7️⃣ Save Into external_app_endpoints
        # ==========================================================
        cur.execute(
            """
            INSERT INTO external_app_endpoints (
                app_id,
                name,
                user_id,
                path,
                method,
                headers,
                query_params,
                path_params,
                body_template,
                timeout_seconds,
                is_universal,
                source_global_endpoint_id,
                is_active
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                external_app["id"],
                endpoint["name"],
                user_id,
                endpoint["path"],
                endpoint["method"],
                json.dumps(final_headers),
                json.dumps(validated_query),
                json.dumps(validated_path),
                json.dumps(final_body),
                endpoint.get("timeout_seconds") or external_app.get("timeout_seconds"),
                0,
                endpoint["id"],
                1,
            ),
        )

        conn.commit()

        return jsonify(
            {
                "success": True,
                "message": "External endpoint instantiated successfully",
            }
        )

    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400

    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        cur.close()
        conn.close()
