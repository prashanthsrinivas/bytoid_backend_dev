from flask import Blueprint, request, jsonify, session, redirect
import requests
import os
from datetime import datetime, timedelta
import json
from db.rds_db import connect_to_rds
import pymysql
from microsoft_route.routes import initialize_msal, store_auth_state_in_redis
from utils.base_logger import get_logger
import asyncio
from dotenv import load_dotenv

logger = get_logger(__name__)

load_dotenv()
dev_val = os.getenv("BASE_FRNT_URL")


def microsoft_integration_login():
    """Simple Microsoft login with global access like Gmail"""

    # # Handle CORS preflight for global access
    # if request.method == "OPTIONS":
    #     response = make_response()
    #     origin = request.headers.get("Origin")
    #     if is_microsoft_allowed_origin(origin):
    #         response.headers["Access-Control-Allow-Origin"] = origin
    #     response.headers["Access-Control-Allow-Credentials"] = "true"
    #     response.headers["Access-Control-Allow-Headers"] = (
    #         "Content-Type, Authorization, X-Requested-With"
    #     )
    #     response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    #     return response

    # Validate MSAL app is available

    msal_app = initialize_msal()
    if not msal_app:
        logger.error("❌ MSAL app not available")
        return jsonify({"error": "Microsoft OAuth not properly configured"}), 500

    try:

        redirect_uri = f"{dev_val}/integration/microsoft/callback"

        logger.info(f"🌍 Using  redirect URI: {redirect_uri}")

        # Create auth flow with dynamic redirect URI (like Google's approach)
        SCOPES = [
            "User.Read",
            "Mail.Send",
            "Mail.ReadWrite",
            "Calendars.ReadWrite",
            "OnlineMeetings.ReadWrite",
            "Chat.ReadWrite",
            "Files.Read.All",
        ]
        flow = msal_app.initiate_auth_code_flow(
            scopes=SCOPES, redirect_uri=redirect_uri
        )

        if not flow.get("auth_uri"):
            logger.error("❌ Failed to generate auth URI")
            return jsonify({"error": "Failed to generate auth URI"}), 500

        # Extract state and code_verifier from flow for Redis storage
        state_key = flow.get("state")
        code_verifier = flow.get("code_verifier")

        if not state_key:
            logger.error("❌ No state parameter in auth flow")
            return jsonify({"error": "Failed to generate state"}), 500

        if not code_verifier:
            logger.error("❌ No code_verifier (PKCE) in auth flow")
            return jsonify({"error": "Failed to generate PKCE verifier"}), 500

        # ✅ Store only the minimal OAuth state in Redis (code_verifier for PKCE)
        # This ensures it's available regardless of which server handles the callback
        asyncio.run(store_auth_state_in_redis(state_key, code_verifier, ttl=600))

        logger.info("✅ Microsoft auth flow created and state stored in Redis")

        # Create response with CORS headers for global access
        response = jsonify({"auth_url": flow["auth_uri"]})

        # Add CORS headers for global frontend access
        # origin = request.headers.get("Origin")
        # if is_microsoft_allowed_origin(origin):
        #     response.headers["Access-Control-Allow-Origin"] = origin
        # response.headers["Access-Control-Allow-Credentials"] = "true"
        # response.headers["Access-Control-Allow-Headers"] = (
        #     "Content-Type, Authorization, X-Requested-With"
        # )
        # response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"

        return response

    except Exception as e:
        logger.error(f"❌ Error in Microsoft login: {str(e)}")
        return jsonify({"error": f"Login initiation failed: {str(e)}"}), 500
