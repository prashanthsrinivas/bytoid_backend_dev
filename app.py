from datetime import datetime
import json
from flask import Flask, request, jsonify
from flask_compress import Compress
from google_route.routes import google_bp
from facebook_route.routes import facebook_bp
from agent_route.routes import agent_bps
from gmail_route.routes import gmail_bp
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
import os
from dotenv import load_dotenv
from flask_cors import CORS
import tempfile
from utils.app_configs import ALLOWED_ORIGINS, IS_DEV

# from session_middleware import register_session_check

load_dotenv("/home/ec2-user/bytoid_python/.env")
app = Flask(__name__)
Compress(app)
app.secret_key = os.getenv(
    "SECRETKEY"
)  # set a secret key as an enviornmental variable later
app.config.update(SESSION_COOKIE_SAMESITE="None", SESSION_COOKIE_SECURE=True)

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
        response.headers["Access-Control-Allow-Origin"] = origin if origin else "*"
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
]

for print in blueprints:
    CORS(
        print,
        supports_credentials=True,
        origins=ALLOWED_ORIGINS,
    )
app.config["SESSION_FILE_DIR"] = os.path.join(tempfile.gettempdir(), "flask_sessions")
os.makedirs(app.config["SESSION_FILE_DIR"], exist_ok=True)

# Create data directory if not exists
os.makedirs("data", exist_ok=True)

for prints in blueprints:
    app.register_blueprint(prints)

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


# save_routes_to_json()
import argparse

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=3000)
    args = parser.parse_args()

    app.run(host=args.host, port=args.port, debug=True)
