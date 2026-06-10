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
from db.rds_db import connect_to_rds
import pymysql
import json

logger = get_logger(__name__)
# Source allowed origins from the single canonical list so this (currently
# disabled) middleware can't drift from app.py and silently drop e.g.
# demo.bytoid.ai if it is ever re-enabled.
from utils.app_configs import ALLOWED_ORIGINS

BASE_ORGINS = list(ALLOWED_ORIGINS)

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
    # VRA OSINT collector Lambda posts findings here; authenticated by its own
    # HMAC signature + nonce, not the user session.
    "/vra/osint/callback",
    # SG-audit collector Lambda posts posture snapshots here; authenticated by its
    # own HMAC signature + nonce, not the user session.
    "/sg-audit/callback",
]


def register_session_check(app):
    CORS(app, supports_credentials=True, resources={r"/*": {"origins": BASE_ORGINS}})

    @app.before_request
    def session_check():
        print("PATH:", request.path)
        print("SESSION ID:", request.cookies.get("session_id"))
        print("ACCESS TOKEN:", request.cookies.get("access_token"))
        if request.method == "OPTIONS" or any(request.path.startswith(p) for p in EXEMPT_PATHS):
            return None  # allow exempt requests

        session_hash = request.cookies.get("session_id")
        access_token = request.cookies.get("access_token") 

        if not access_token:
            auth_header = request.headers.get("Authorization")
            if auth_header and auth_header.startswith("Bearer "):
                access_token = auth_header.split(" ")[1]


        if not session_hash or not access_token:
            logger.warning("Missing session or token")
            return (
                jsonify({"error": "Session expired", "redirect": "/login"}),
                401,
            )
        session, key_str = asyncio.run(get_session(session_hash, request))
        if not session:
            logger.warning("Invalid session")
            asyncio.run(delete_all_session_cookies(key_str))
            return (
                jsonify({"error": "Session expired", "redirect": "/login"}),
                401,
            )
        forwarded_for = request.headers.get("X-Forwarded-For")
        client_ip = forwarded_for.split(",")[0].strip() if forwarded_for else request.remote_addr
        client_ua = request.headers.get("User-Agent")

        if session("user_agent") != client_ua:
            logger.warning("User agent mismatch")
            asyncio.run(delete_all_session_cookies(key_str))
            return (
                jsonify({"error": "Session expired", "redirect": "/login"}),
                401,
            )
        now = datetime.now(timezone.utc)
        expiry_str = session.get("session_expiry")
        if not expiry_str:
            return jsonify({"error": "Session expired", "redirect": "/login"}), 401
        expiry = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))

        if expiry < now:
             logger.warning("session expired")
             asyncio.run(delete_all_session_cookies(key_str))
             return jsonify({"error": "Session expired", "redirect": "/login"}), 401
        is_valid, new_tokens = asyncio.run(
            validate_and_refresh_tokens(session_hash, session, access_token, key_str)
        )
        if not is_valid:
            logger.warning("Token validation failed")
            return (
                jsonify({"error": "Session expired", "redirect": "/login"}),
                401,
            )
        # store context for later
        g.user_id = session["user_id"]
        conn = None
        try:
            conn = connect_to_rds()
            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute(
                    "SELECT user_id, user_type, permissions, email FROM users WHERE user_id=%s",
                    (session["user_id"],),
                )
                user_row = cursor.fetchone()
                if user_row:
                    try:
                         user_row["permissions"] = json.loads(user_row["permissions"] or "{}")
                    except Exception:
                        user_row["permissions"] = {}
                    g.user = user_row
                else:
                    g.user = None
        except Exception as e:
            logger.error(f"User fetch failed: {str(e)}")
            g.user = None
        finally:
            if conn:
                conn.close()
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
