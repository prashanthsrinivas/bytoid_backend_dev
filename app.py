from flask import Flask, request
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
import os
from dotenv import load_dotenv
from flask_cors import CORS
import tempfile

# from session_middleware import register_session_check

load_dotenv()
app = Flask(__name__)
Compress(app)
app.secret_key = os.getenv(
    "SECRETKEY"
)  # set a secret key as an enviornmental variable later
app.config.update(SESSION_COOKIE_SAMESITE="None", SESSION_COOKIE_SECURE=True)
BASE_ORGINS = [
    # "http://localhost:4173/",
    "http://172.31.12.212",
    "https://www.bytoid.ai",
    "https://bytoid.ai",
    "https://dev.bytoid.ai",
    "http://localhost:8081",
    'bytoid://',
    'user-app://',
    'http://localhost:19000',
    'http://localhost:19006',
    'https://auth.expo.io',
]
# register_session_check(app)

ALLOWED_ORIGINS = {o.rstrip("/") for o in BASE_ORGINS}

ALLOWED_SCHEMES = ['bytoid', 'user-app', 'exp']

def is_origin_allowed(origin):
    """Check if origin is allowed"""
    # No origin header = native mobile app making API request
    if not origin:
        return True
    
    # Check exact matches (web/development)
    if origin in ALLOWED_ORIGINS:
        return True
    
    # Check if origin starts with allowed scheme (mobile OAuth)
    for scheme in ALLOWED_SCHEMES:
        if origin.startswith(f'{scheme}://'):
            return True
    
    # Allow any localhost port (development)
    if origin.startswith('http://localhost:'):
        return True
    
    return False

@app.after_request
def after_request(response):
    origin = request.headers.get('Origin')

    if is_origin_allowed(origin):
        # If no origin (mobile app), use *
        # If has origin, echo it back
        response.headers['Access-Control-Allow-Origin'] = origin if origin else '*'
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Requested-With'
        response.headers['Access-Control-Max-Age'] = '3600'

    return response

# Handle preflight OPTIONS requests
@app.route('/<path:path>', methods=['OPTIONS'])
def handle_options(path):
    return '', 204


CORS(
    app,
    supports_credentials=True,
    origins=BASE_ORGINS,
)
CORS(
    google_bp,
    supports_credentials=True,
    origins=BASE_ORGINS,
)
CORS(
    facebook_bp,
    supports_credentials=True,
    origins=BASE_ORGINS,
)
CORS(
    agent_bps,
    supports_credentials=True,
    origins=BASE_ORGINS,
)
CORS(
    playbook_bp,
    supports_credentials=True,
    origins=BASE_ORGINS,
)
CORS(
    gmail_bp,
    supports_credentials=True,
    origins=BASE_ORGINS,
)
CORS(
    session_bp,
    supports_credentials=True,
    origins=BASE_ORGINS,
)
CORS(
    microsoft_bp,
    supports_credentials=True,
    origins=BASE_ORGINS,
)
CORS(
    twilio_bp,
    supports_credentials=True,
    origins=BASE_ORGINS,
)
CORS(
    users_bp,
    supports_credentials=True,
    origins=BASE_ORGINS,
)
CORS(
    contacts_bp,
    supports_credentials=True,
    origins=BASE_ORGINS,
)
CORS(
    zoho_bp,
    supports_credentials=True,
    origins=BASE_ORGINS,
)
CORS(
    umail_bp,
    supports_credentials=True,
    origins=BASE_ORGINS,
)
CORS(
    tickets_bp,
    supports_credentials=True,
    origins=BASE_ORGINS,
)
CORS(
    inv_users_bp,
    supports_credentials=True,
    origins=BASE_ORGINS,
)

CORS(
    agent_hub_bp,
    supports_credentials=True,
    origins=BASE_ORGINS,
)
CORS(
    search_bp,
    supports_credentials=True,
    origins=BASE_ORGINS,
)
CORS(
    assist_suggest_bp,
    supports_credentials=True,
    origins=BASE_ORGINS,
)
CORS(
    unified_bp,
    supports_credentials=True,
    origins=BASE_ORGINS,
)
CORS(
    ai_assistant_chat_bp,
    supports_credentials=True,
    origins=BASE_ORGINS,
)
CORS(
    onboarding_bps,
    supports_credentials=True,
    origins=BASE_ORGINS,
)
CORS(
    ai_reporting_bp,
    supports_credentials=True,
    origins=BASE_ORGINS,
)
CORS(
    forwarding_bp,
    supports_credentials=True,
    origins=BASE_ORGINS,
)
CORS(
    calenders_bp,
    supports_credentials=True,
    origins=BASE_ORGINS,
)

CORS(
    integrations_bp,
    supports_credentials=True,
    origins=BASE_ORGINS,
)
CORS(
    audio_agent_bps,
    supports_credentials=True,
    origins=BASE_ORGINS,
)
CORS(
    docs_agent_bps,
    supports_credentials=True,
    origins=BASE_ORGINS,
)
CORS(
    scrape_agent_bps,
    supports_credentials=True,
    origins=BASE_ORGINS,
)

app.config["SESSION_FILE_DIR"] = os.path.join(tempfile.gettempdir(), "flask_sessions")
os.makedirs(app.config["SESSION_FILE_DIR"], exist_ok=True)

# Create data directory if not exists
os.makedirs("data", exist_ok=True)

# Register Blueprints
app.register_blueprint(google_bp)
app.register_blueprint(facebook_bp)
app.register_blueprint(agent_bps)
app.register_blueprint(gmail_bp)
app.register_blueprint(session_bp)
app.register_blueprint(microsoft_bp)
app.register_blueprint(twilio_bp)
app.register_blueprint(users_bp)
app.register_blueprint(contacts_bp)
app.register_blueprint(playbook_bp)
app.register_blueprint(zoho_bp)
app.register_blueprint(credits_bp)
app.register_blueprint(umail_bp)
app.register_blueprint(tickets_bp)
app.register_blueprint(inv_users_bp)
app.register_blueprint(agent_hub_bp)
app.register_blueprint(search_bp)
app.register_blueprint(assist_suggest_bp)
app.register_blueprint(unified_bp)
app.register_blueprint(ai_assistant_chat_bp)
app.register_blueprint(onboarding_bps)
app.register_blueprint(ai_reporting_bp)
app.register_blueprint(forwarding_bp)
app.register_blueprint(calenders_bp)
app.register_blueprint(integrations_bp)
app.register_blueprint(docs_agent_bps)
app.register_blueprint(scrape_agent_bps)
app.register_blueprint(audio_agent_bps)

import argparse

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=3000)
    args = parser.parse_args()

    app.run(host=args.host, port=args.port, debug=True)
