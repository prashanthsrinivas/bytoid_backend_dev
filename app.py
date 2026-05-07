from datetime import datetime
import json
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
from radar.routes import radar_bp
from runbook.routes import runbook_bp
from config_evidences.routes import config_evidences_bp
from sso_by.routes import sso_bp
from tab_tracker.routes import tracker_bp
from tab_tracker.tab_ai_tracker.routes import tracker_ai_bp
from websockets_custom.routes import ws_bp
from policy_hub.routes import policy_hub_bp
import os
from dotenv import load_dotenv
from flask_cors import CORS
import tempfile
from utils.app_configs import ALLOWED_ORIGINS, IS_DEV
from werkzeug.middleware.proxy_fix import ProxyFix

# from session_middleware import register_session_check

load_dotenv("/home/ec2-user/bytoid_python/.env")
app = Flask(__name__)
# print("🚀 Starting watcher during app init")
# start_api_watcher()
Compress(app)
app.secret_key = os.getenv("SECRETKEY")
# set a secret key as an enviornmental variable later
# app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=False)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

app.config.update(SESSION_COOKIE_SAMESITE="None", SESSION_COOKIE_SECURE=True)

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
def after_request(response):
    origin = request.headers.get("Origin")

    if is_origin_allowed(origin):
        # If no origin (mobile app), use *
        # If has origin, echo it back
        if origin:
            response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Methods"] = (
            "GET, POST, PUT, DELETE, OPTIONS"
        )
        response.headers["Access-Control-Allow-Headers"] = (
            "Content-Type, Authorization, X-Requested-With"
        )
        response.headers["Access-Control-Max-Age"] = "3600"

    return response


# Handle preflight OPTIONS requests
@app.route("/<path:path>", methods=["OPTIONS"])
def handle_options(path):
    return "", 204


blueprints = [
    google_bp,
    facebook_bp,
    agent_bps,
    gmail_bp,
    playbook_bp,
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
    apiconnector_bp,
    radar_bp,
    runbook_bp,
    config_evidences_bp,
    sso_bp,
    ws_bp,
    tracker_bp,
    tracker_ai_bp,
    policy_hub_bp,
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
import time, uuid


@app.before_request
def before_request():
    g.start_time = time.time()
    g.request_id = str(uuid.uuid4())[:8]


@app.before_request
def audit_before_request():
    """Stamp audit context on the request."""
    g.audit_logged = False  # Duplicate-guard flag (set True by direct calls)
    g.session_user_id = session.get("user_id")  # Partial identity; may be None


@app.after_request
def after_request(response):
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
    "/health", "/ping", "/favicon.ico",
    "/browser_url", "/user/alive",
    "/ws/", "/socket.io/",
    "/notifications",
    # Routes with explicit instrumentation — prevent any fallback
    "/user_login",        # → LOGIN_SUCCESS / LOGIN_FAILED
    "/delete_session",    # → USER_LOGGED_OUT
    # Known read-only POST endpoints (CQRS-style)
    "/users/get_group", "/users/get_all_groups",
    "/get_onboarding", "/email_exist",
    "/check_integrations", "/get_all_integrations",
    "/check_pb_output", "/autocheck-workflow", "/schedule-workflow-checker",
    "/bytoidpro/get_a_chat", "/bytoidpro/chat_history", "/bytoidpro/think/status",
    "/list_all_draft_reports",
    "/get_google_client_id", "/get_microsoft_client_id",
    "/admin/audit-logs",   # prevent recursive logging of the audit viewer
}

# Pattern indicators for read-only operations (CQRS-style)
_AUDIT_READ_INDICATORS = (
    "/get_",    # /get_active_customers, /get_conversation_notes, /get_user_notes ...
    "/check_",  # /check_runbook_exists, /check_pb_output ...
    "/search",  # /search_users_for_sharing, /search-emails
    "/fetch_",  # /fetch_all_emails, /microsoft/fetch_all_emails
    "/list_",   # /list_all_draft_reports, /list_chat_config
)

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

        # Layer 1: Skip explicitly exempted paths (system endpoints and fully-instrumented routes)
        if any(path.startswith(p) for p in _AUDIT_EXEMPT_PATHS):
            return response

        # Layer 2: Skip read-only patterns (CQRS-style data queries)
        if any(indicator in path for indicator in _AUDIT_READ_INDICATORS):
            return response

        # Log all other mutation routes (denylist-based fallback coverage)
        actor_user_id = getattr(g, "session_user_id", None)

        from services.audit_log_service import log_audit_event
        from db.db_checkers import get_email_by_id

        actor_email = get_email_by_id(actor_user_id) if actor_user_id else None

        # Detect delegation from request body (cross-check session vs. body user_id)
        acting_on_behalf_of_user_id = None
        acting_on_behalf_of_email = None
        try:
            body_uid = (request.get_json(silent=True) or {}).get("user_id")
            if body_uid and actor_user_id and str(body_uid) != str(actor_user_id):
                acting_on_behalf_of_user_id = body_uid
                acting_on_behalf_of_email = get_email_by_id(body_uid)
        except Exception:
            pass

        log_audit_event(
            action="API_MUTATION",
            endpoint=path,
            ip=request.remote_addr,
            status="success" if response.status_code < 400 else "failure",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=acting_on_behalf_of_user_id or getattr(g, "acting_on_behalf_of_user_id", None),
            acting_on_behalf_of_email=acting_on_behalf_of_email,
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

    app.run(host=args.host, port=args.port, debug=True)
