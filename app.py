from datetime import datetime
import json
from services.audit_log_service import build_audit_actor
from utils.base_logger import get_logger
from flask import Flask, request, g, session
from flask_compress import Compress
from google_route.routes import google_bp
from facebook_route.routes import facebook_bp
from agent_route.routes import agent_bps
from gmail_route.routes import gmail_bp

# from runbook.api_watcher import start_api_watcher
from session_manager_route.routes import session_bp
from microsoft_route.routes import microsoft_bp
from users_routes.routes import users_bp
from webhooks.routes import twilio_bp
from contacts_route.route import contacts_bp
from playbook.routes import playbook_bp
from playbook.drive_import import drive_import_bp
from zoho_routes.routes import zoho_bp
from credits_route.route import credits_bp
from umail.routes import umail_bp
from tickets.routes import tickets_bp
from invited_users.routes import inv_users_bp
from agents_hub_route.routes import agent_hub_bp
from search_email.routes import search_bp
from suggest_assist.route import assist_suggest_bp
from unified_mailbox.routes import unified_bp
from ai_assistant_chat.routes import ai_assistant_chat_bp
from onboarding.routes import onboarding_bps
from ai_reporting.routes import ai_reporting_bp
from umail_helper.forwarding_rules_route import forwarding_bp
from calenders.routes import calenders_bp
from integrations.routes import integrations_bp
from training.docs_train.docs_base import docs_agent_bps

from training.scrape.scrape_base import scrape_agent_bps
from training.voice.audio_base import audio_agent_bps
from payments.payments import payments_bp
from plans.routes import plans_bp
from bytoid_pro_dev.routes import bytoid_dev_pro_bp
from apiConnector.routes import apiconnector_bp
from aws_integration.routes import aws_integration_bp
from azure_integration.routes import azure_integration_bp
from gcp_integration.routes import gcp_integration_bp
from radar.routes import radar_bp
from runbook.routes import runbook_bp
from risk_config.routes import risk_config_bp
from config_evidences.routes import config_evidences_bp
from sso_by.routes import sso_bp
from tab_tracker.routes import tracker_bp
from tab_tracker.tab_ai_tracker.routes import tracker_ai_bp
from websockets_custom.routes import ws_bp
from policy_hub.routes import policy_hub_bp
from trust_center.routes import trust_center_bp
from workflow_route.routes import workflow_bp
from tests_routes.routes import tests_bp
from ai_governance.routes import ai_governance_bp
from assessment_chat.routes import assessment_chat_bp
from vra.routes import vra_bp
from sg_audit.routes import sg_audit_bp
from azure_audit.routes import azure_audit_bp
from gcp_audit.routes import gcp_audit_bp
from strategy.routes import strategy_bp
import os
from dotenv import load_dotenv
from flask_cors import CORS
import tempfile
from utils.app_configs import ALLOWED_ORIGINS, IS_DEV
from werkzeug.middleware.proxy_fix import ProxyFix

# from session_middleware import register_session_check

load_dotenv("/home/ec2-user/bytoid_python/.env")

# Handle numpy/pyarrow types returned by LanceDB that aren't JSON-serializable by default
from flask.json.provider import DefaultJSONProvider


class _NumpyJSONProvider(DefaultJSONProvider):
    def default(self, obj):
        try:
            import numpy as np

            if isinstance(obj, np.integer):
                return int(obj)
            if isinstance(obj, np.floating):
                return float(obj)
            if isinstance(obj, np.ndarray):
                return obj.tolist()
        except ImportError:
            pass
        return super().default(obj)


app = Flask(__name__)
app.json_provider_class = _NumpyJSONProvider
app.json = _NumpyJSONProvider(app)

# print("🚀 Starting watcher during app init")
# start_api_watcher()
Compress(app)
app.secret_key = os.getenv("SECRETKEY")
# set a secret key as an enviornmental variable later
# app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=False)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)


class BearerSessionMiddleware:
    """Let cross-site clients authenticate with the signed session via header.

    Safari (ITP) won't send our third-party session cookie, so clients send the
    same signed value as `Authorization: Bearer <token>` instead. Here we copy it
    into the request's Cookie before Flask opens the session — so all existing
    cookie/session code works unchanged. A real session cookie (same-origin /
    non-Safari) always wins; we only fill in when it's absent.
    """

    def __init__(self, wsgi_app, cookie_name: str):
        self.wsgi_app = wsgi_app
        self.cookie_name = cookie_name

    def __call__(self, environ, start_response):
        auth = environ.get("HTTP_AUTHORIZATION", "")
        if auth[:7].lower() == "bearer ":
            token = auth[7:].strip()
            existing = environ.get("HTTP_COOKIE", "")
            if token and f"{self.cookie_name}=" not in existing:
                piece = f"{self.cookie_name}={token}"
                environ["HTTP_COOKIE"] = f"{existing}; {piece}" if existing else piece
        return self.wsgi_app(environ, start_response)


app.wsgi_app = BearerSessionMiddleware(
    app.wsgi_app, app.config.get("SESSION_COOKIE_NAME", "session")
)

app.config.update(SESSION_COOKIE_SAMESITE="None", SESSION_COOKIE_SECURE=True)

# Cross-site cookie reliability (esp. Safari ITP): the session cookie is only
# sent on cross-subdomain requests when the app and API share a registrable
# domain (e.g. app.bytoid.ai -> api.bytoid.ai). Scope the cookie to the parent
# domain so it rides on every *.bytoid.ai subdomain.
#
# IMPORTANT: a server can only set a Domain for its OWN registrable domain. While
# the API is served from the raw API Gateway host (*.amazonaws.com), setting this
# to ".bytoid.ai" would make the browser REJECT the cookie entirely. So it is
# env-gated and unset by default: once the API is fronted by api.bytoid.ai, set
# SESSION_COOKIE_DOMAIN=.bytoid.ai in the environment to activate it — no code
# change needed.
_session_cookie_domain = os.getenv("SESSION_COOKIE_DOMAIN")
if _session_cookie_domain:
    app.config["SESSION_COOKIE_DOMAIN"] = _session_cookie_domain

ALLOWED_SCHEMES = ["bytoid", "user-app", "exp"]


def is_origin_allowed(origin: str | None) -> bool:
    # Native mobile app (no Origin header)
    if not origin:
        return True

    origin = origin.rstrip("/")

    # Exact match only
    if origin in ALLOWED_ORIGINS:
        return True

    # Mobile app schemes (OAuth / deep links)
    for scheme in ALLOWED_SCHEMES:
        if origin.startswith(f"{scheme}://"):
            return True

    # DEV only: allow any localhost port
    if IS_DEV and origin.startswith("http://localhost:"):
        return True

    return False


@app.after_request
def cors_after_request(response):
    origin = request.headers.get("Origin")

    if is_origin_allowed(origin):
        if origin:
            response.headers["Access-Control-Allow-Origin"] = origin
            # ACAO varies by request Origin — tell caches so one origin's
            # response is never reused for another (append; don't clobber
            # an existing Vary such as Accept-Encoding from Compress).
            existing_vary = response.headers.get("Vary")
            response.headers["Vary"] = f"{existing_vary}, Origin" if existing_vary and "Origin" not in existing_vary else (existing_vary or "Origin")
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Methods"] = (
            "GET, POST, PUT, DELETE, OPTIONS"
        )
        response.headers["Access-Control-Allow-Headers"] = (
            "Content-Type, Authorization, X-Requested-With"
        )
        response.headers["Access-Control-Max-Age"] = "3600"

        # Hand the signed session back as a bearer token so cross-site clients
        # (Safari ITP drops the third-party session cookie) can re-send it as
        # `Authorization: Bearer <token>`. Refreshed on every authenticated
        # response so it stays current as the session changes (workspace switch,
        # 2FA clear, etc.). Must be exposed for JS to read it cross-origin.
        try:
            if session.get("user_id"):
                from utils.session_token import current_session_token

                token = current_session_token()
                if token:
                    response.headers["X-Session-Token"] = token
                    expose = response.headers.get("Access-Control-Expose-Headers")
                    response.headers["Access-Control-Expose-Headers"] = (
                        f"{expose}, X-Session-Token"
                        if expose and "X-Session-Token" not in expose
                        else (expose or "X-Session-Token")
                    )
        except Exception:
            pass

    return response


# Handle preflight OPTIONS requests
@app.route("/<path:path>", methods=["OPTIONS"])
def handle_options(path):
    from flask import make_response
    origin = request.headers.get("Origin", "")
    resp = make_response("", 204)
    if is_origin_allowed(origin):
        if origin:
            resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = (
            "Content-Type, Authorization, X-Requested-With"
        )
        resp.headers["Access-Control-Max-Age"] = "3600"
    return resp


blueprints = [
    google_bp,
    facebook_bp,
    agent_bps,
    gmail_bp,
    playbook_bp,
    drive_import_bp,
    session_bp,
    microsoft_bp,
    twilio_bp,
    users_bp,
    contacts_bp,
    zoho_bp,
    umail_bp,
    tickets_bp,
    inv_users_bp,
    agent_hub_bp,
    search_bp,
    assist_suggest_bp,
    unified_bp,
    ai_assistant_chat_bp,
    onboarding_bps,
    ai_reporting_bp,
    forwarding_bp,
    calenders_bp,
    integrations_bp,
    audio_agent_bps,
    docs_agent_bps,
    scrape_agent_bps,
    credits_bp,
    plans_bp,
    payments_bp,
    bytoid_dev_pro_bp,
    aws_integration_bp,
    azure_integration_bp,
    gcp_integration_bp,
    apiconnector_bp,
    radar_bp,
    runbook_bp,
    risk_config_bp,
    config_evidences_bp,
    sso_bp,
    ws_bp,
    tracker_bp,
    tracker_ai_bp,
    policy_hub_bp,
    trust_center_bp,
    workflow_bp,
    tests_bp,
    ai_governance_bp,
    assessment_chat_bp,
    vra_bp,
    sg_audit_bp,
    azure_audit_bp,
    gcp_audit_bp,
    strategy_bp,
]

for bp in blueprints:
    CORS(
        bp,
        supports_credentials=True,
        origins=ALLOWED_ORIGINS,
    )
app.config["SESSION_FILE_DIR"] = os.path.join(tempfile.gettempdir(), "flask_sessions")
os.makedirs(app.config["SESSION_FILE_DIR"], exist_ok=True)

# Create data directory if not exists
os.makedirs("data", exist_ok=True)

for bp in blueprints:
    app.register_blueprint(bp)

# Register Prometheus /metrics scrape endpoint
from services.metrics_service import register_metrics_endpoint
register_metrics_endpoint(app)

from collections import defaultdict


def list_routes_by_blueprint(app):
    grouped = defaultdict(list)

    for rule in app.url_map.iter_rules():
        endpoint = rule.endpoint  # e.g. payments_bp.paymenttopup

        if "." in endpoint:
            blueprint, func_name = endpoint.split(".", 1)
        else:
            blueprint = "app"
            func_name = endpoint

        grouped[blueprint].append(
            {
                "endpoint": endpoint,
                "function": func_name,
                "methods": list(rule.methods),
                "path": str(rule),
            }
        )

    return grouped


def save_routes_to_json(file_path="all_apis.json"):
    routes = list_routes_by_blueprint(app)

    data = {
        "generated_at": datetime.utcnow().isoformat(),
        "total_blueprints": len(routes),
        "apis": routes,
    }

    os.makedirs(os.path.dirname(file_path), exist_ok=True) if "/" in file_path else None

    with open(file_path, "w") as f:
        json.dump(data, f, indent=2)

    return file_path


# logger = get_logger("api")
import time
import uuid


@app.before_request
def before_request():
    g.start_time = time.time()
    g.request_id = str(uuid.uuid4())[:8]
    g.user_id = session.get("user_id")  # Fallback: session middleware is commented out


@app.before_request
def audit_before_request():
    """Stamp audit context on the request."""
    g.audit_logged = False  # Duplicate-guard flag (set True by direct calls)
    g.workspace_access_logged = (
        False  # Prevents duplicate WORKSPACE_ACCESS_ENTERED per request
    )
    # Prefer g.user_id set by session_middleware (Redis-backed); fall back to Flask signed-cookie session.
    g.session_user_id = getattr(g, "user_id", None) or session.get("user_id")

    # If there's an active workspace delegation in the session, pre-stamp the delegation context
    active_workspace_id = session.get("active_workspace_id")
    if (
        active_workspace_id
        and g.session_user_id
        and g.session_user_id != active_workspace_id
    ):
        from db.db_checkers import get_email_by_id

        g.acting_on_behalf_of_user_id = active_workspace_id
        g.acting_on_behalf_of_email = get_email_by_id(active_workspace_id)

    # Fallback: detect delegation from request body
    # (for routes that don't explicitly call build_audit_actor)
    if not getattr(g, "acting_on_behalf_of_user_id", None) and request.is_json:
        try:
            body = request.get_json(silent=True) or {}
            body_uid = (body.get("user_id") or "").strip()

            if body_uid:
                (
                    actor_user_id,
                    actor_email,
                    acting_on_behalf_of_user_id,
                    acting_on_behalf_of_email,
                ) = build_audit_actor(body_uid)

                # Stamp parsed values into request context
                g.session_user_id = actor_user_id

                if acting_on_behalf_of_user_id:
                    g.acting_on_behalf_of_user_id = acting_on_behalf_of_user_id
                    g.acting_on_behalf_of_email = acting_on_behalf_of_email

        except Exception:
            pass


@app.after_request
def log_after_request(response):
    duration = round((time.time() - g.start_time) * 1000, 2)

    blueprint_name = request.blueprint or "root"
    logger = get_logger(blueprint_name)

    logger.info(
        f"[{g.request_id}] {request.method} {request.path} "
        f"{response.status_code} {duration}ms "
        f"IP={request.remote_addr}"
    )

    return response


# Paths that should NOT be audited (system and read-only endpoints)
_AUDIT_EXEMPT_PATHS = {
    # System endpoints
    "/health",
    "/ping",
    "/favicon.ico",
    "/browser_url",
    "/user/alive",
    "/ws/",
    "/socket.io/",
    "/notifications",
    # Read-only POST endpoints (CQRS-style data queries)
    "/users/get_group",
    "/users/get_all_groups",
    "/get_onboarding",
    "/email_exist",
}

# Mutating methods
_AUDIT_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@app.after_request
def audit_after_request(response):
    """Audit hook for middleware fallback coverage (denylist-based: log all mutations except exempted paths)."""
    try:
        # Skip if already logged by direct call instrumentation
        if getattr(g, "audit_logged", False):
            return response

        method = request.method
        path = request.path

        # Only log mutating methods
        if method not in _AUDIT_METHODS:
            return response

        # Skip explicitly exempted paths (system endpoints and read-only POST operations)
        if any(path.startswith(p) for p in _AUDIT_EXEMPT_PATHS):
            return response

        # Log all other mutation routes (denylist-based fallback coverage)
        actor_user_id = getattr(g, "session_user_id", None)

        from services.audit_log_service import log_audit_event
        from db.db_checkers import get_email_by_id

        actor_email = get_email_by_id(actor_user_id) if actor_user_id else None
        log_audit_event(
            action="API_MUTATION",
            endpoint=path,
            ip=request.remote_addr,
            status="success" if response.status_code < 400 else "failure",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=getattr(g, "acting_on_behalf_of_user_id", None),
            acting_on_behalf_of_email=getattr(g, "acting_on_behalf_of_email", None),
            metadata={
                "method": method,
                "status_code": response.status_code,
                "blueprint": request.blueprint,
                "source": "middleware_fallback",
                "route_rule": request.url_rule.rule if request.url_rule else None,
            },
        )
    except Exception:
        pass

    return response


# save_routes_to_json()
import argparse

if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=3000)
    args = parser.parse_args()

    # Prod runs under gunicorn (see gunicorn.conf.py on_starting); mirror that
    # self-heal here so local `python app.py` also keeps the bucket CORS current.
    try:
        from utils.s3_utils import ensure_bucket_cors

        ensure_bucket_cors()
    except Exception as e:
        get_logger("api").warning("ensure_bucket_cors failed at dev startup: %s", e)

    app.run(host=args.host, port=args.port, debug=True)
