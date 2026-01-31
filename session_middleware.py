from datetime import datetime, timezone
import asyncio
from flask import request, redirect, make_response, g, jsonify
from session_manager_route.session_redis import (
    get_session,
    delete_all_session_cookies,
    validate_and_refresh_tokens,
)
from utils.base_logger import get_logger
from functools import wraps
from flask_cors import CORS

logger = get_logger(__name__)
BASE_ORGINS = [
    "http://172.31.12.212",
    "https://www.bytoid.ai",
    "https://bytoid.ai",
    "https://app.bytoid.ai",
]

EXEMPT_PATHS = [
    "/generate_session",
    "/get_google_client_id",
    "/google_login",
    "/login",
    "/oauth2callback",
    "/browser_url",
    "/check-user",
    "/get_user_permissions",
    "/get-training-settings",
    "/get-usersDocs",
    "/get_all_instructions",
    "/microsoft/login",
    "/microsoft/callback",
    "/microsoft/login/debug",
]


def register_session_check(app):
    CORS(app, supports_credentials=True, resources={r"/*": {"origins": BASE_ORGINS}})

    @app.before_request
    def session_check():
        if request.method == "OPTIONS" or request.path in EXEMPT_PATHS:
            return None  # allow exempt requests

        session_hash = request.cookies.get("session_id")
        access_token = request.cookies.get("access_token") or request.headers.get(
            "Authorization"
        )
        path = request.path

        if not session_hash or not access_token:
            logger.warning("NO session_hash or access_token")
            return (
                jsonify({"error": "Authentication required", "redirect": "/login"}),
                401,
            )
        session, key_str = asyncio.run(get_session(session_hash, request))
        if not session:
            logger.warning("NO session")
            asyncio.run(delete_all_session_cookies(key_str))
            return (
                jsonify({"error": "Authentication required", "redirect": "/login"}),
                401,
            )
        client_ip = request.remote_addr
        client_ua = request.headers.get("User-Agent")

        if session["ip"] != client_ip or session["user_agent"] != client_ua:
            logger.warning("client_ip or ua not same")
            asyncio.run(delete_all_session_cookies(key_str))
            return (
                jsonify({"error": "Authentication required", "redirect": "/login"}),
                401,
            )
        now = datetime.now(timezone.utc)
        expiry_str = session["session_expiry"]
        expiry = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))

        # if expiry < now:
        #     logger.warning("session expired")
        #     asyncio.run(delete_all_session_cookies(key_str))
        #     return jsonify({"error": "Authentication required", "redirect": "/login"}), 401
        is_valid, new_tokens = asyncio.run(
            validate_and_refresh_tokens(session_hash, session, access_token, key_str)
        )
        if not is_valid:
            logger.warning("Token validation failed")
            return (
                jsonify({"error": "Authentication required", "redirect": "/login"}),
                401,
            )
        # store context for later
        g.user_id = session["user_id"]
        g.session_data = session
        g.new_tokens = new_tokens
        g.current_access_token = (
            new_tokens["access_token"] if new_tokens else access_token
        )

        if new_tokens:
            logger.info(f"Token refreshed in request, updating session context")

        return None  # continue processing

    @app.after_request
    def after_request(response):
        # Handle token refresh after main request
        if hasattr(g, "new_tokens") and g.new_tokens:
            response.set_cookie(
                "access_token",
                g.new_tokens["access_token"],
                httponly=True,
                secure=True,
                samesite="None",
                max_age=60 * 60 * 24 * 7,
            )

        return response
