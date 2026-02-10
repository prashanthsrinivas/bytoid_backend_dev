import os
import sys
import uuid
import json
import hashlib
import traceback
import asyncio
import threading
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from agent_route.ag_helperzz import (
    deletefilebasedData,
    process_and_update_yaml,
    remove_https_prefix,
)
from db.db_checkers import ensure_starter_credits_for_user
from .microsoft_helpers import retrieve_auth_state_from_redis
from integrations.integrations_helpers import get_all_integrations
from umail_helper.helper import store_integrations_in_redis
from google_route.google_helpers import check_google_token_expiry, update_user_alive
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as g_request
from services.gmail_service import GmailService
import base64
from google_route.routes import refresh_expired_microsoft_tokens_for_integrations
from request_context import current_user_id
from umail_helper.mails_process import check_mailbox
from services.credit_system import CreditManager


# Third-party imports
from flask import (
    Blueprint,
    request,
    jsonify,
    session,
    redirect,
    make_response,
    stream_with_context,
    Response,
)
from msal import ConfidentialClientApplication
import requests
import pymysql
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# from glide import GlideClusterClient
from services.redis_service import RedisService
from integrations.google_integration import get_integration_access_token
from umail_helper.ticketalloc import TicketAllocator

import asyncio
import aiohttp
from .microsoft_helpers import (
    check_microsoft_token_expiry,
    refresh_expired_microsoft_tokens,
    check_microsoft_token_expiry_normal,
)
from .microsoft_helpers import OutlookSubscriptionManager

# import html2text
import re
from bs4 import BeautifulSoup
from utils.celery_base import delayed_trigger, lock_client
from utils.delay_mails import read_status_file, write_status_file


# Load environment variables
load_dotenv()
frontend_url = os.getenv("BASE_FRNT_URL")
# Add project root to path for imports
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

# Local imports with error handling
try:
    from utils.base_logger import get_logger
except ImportError:
    import logging

    def get_logger(name):
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                "%(asctime)s - %(levelname)s - [%(name)s] - %(message)s"
            )
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        return logger


try:
    from db.rds_db import connect_to_rds
except ImportError:
    # Fallback database connection
    def connect_to_rds():
        import boto3
        import json

        def get_secret():
            secret_name = "rds!db-9db402d8-3595-4048-bf23-979d5e5985e4"
            region_name = "ca-central-1"
            client = boto3.client("secretsmanager", region_name=region_name)
            response = client.get_secret_value(SecretId=secret_name)

            if "SecretString" in response:
                return json.loads(response["SecretString"])
            else:
                import base64

                return json.loads(base64.b64decode(response["SecretBinary"]))

        creds = get_secret()
        rds_host = "bytoiddb.c9ek8228ux41.ca-central-1.rds.amazonaws.com"

        return pymysql.connect(
            host=rds_host,
            user=creds["username"],
            password=creds["password"],
            database="bytoid_support_agent",
            port=3306,
            charset="utf8mb4",
            connect_timeout=10,
        )


try:
    from db.db_checkers import (
        fetch_userid_from_launch,
        check_onboarding_user,
        fetch_apikey_from_launch,
    )
except ImportError:
    # Fallback implementations
    def fetch_userid_from_launch(apikey):
        try:
            conn = connect_to_rds()
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM users WHERE api_key = %s", (apikey,))
            result = cursor.fetchone()
            cursor.close()
            conn.close()
            return result[0] if result else None
        except:
            return None

    def check_onboarding_user(user_id):
        try:
            conn = connect_to_rds()
            cursor = conn.cursor()
            cursor.execute("SELECT user_type FROM users WHERE user_id = %s", (user_id,))
            result = cursor.fetchone()
            cursor.close()
            conn.close()
            return result[0] == "user" if result else True
        except:
            return True

    def fetch_apikey_from_launch(user_id):
        try:
            conn = connect_to_rds()
            cursor = conn.cursor()
            cursor.execute("SELECT api_key FROM users WHERE user_id = %s", (user_id,))
            result = cursor.fetchone()
            cursor.close()
            conn.close()
            return result[0] if result else None
        except:
            return None


try:
    from session_manager_route.routes import session_login
except ImportError:
    # Fallback session login
    def session_login(user_id):
        session_id = str(uuid.uuid4())
        access_token = str(uuid.uuid4())
        refresh_token = str(uuid.uuid4())
        return session_id, access_token, refresh_token


try:
    from utils.s3_utils import upload_any_file, s3bucket
except ImportError:
    # Mock S3 utils
    def upload_any_file(*args, **kwargs):
        return True

    def s3bucket():
        class MockS3:
            def upload_fileobj(self, *args, **kwargs):
                pass

        return MockS3()


try:
    from data import MESSAGES
except ImportError:
    # Initialize MESSAGES if not available
    MESSAGES = {}

# try:
#
# except ImportError:
#     redis_config_glide = None

# Initialize logger
logger = get_logger(__name__)


# Redis helper functions for Microsoft OAuth flow persistence
async def store_auth_state_in_redis(
    state_key: str, code_verifier: str, ttl: int = 600
) -> bool:
    """
    Store minimal OAuth state in Redis (only PKCE code_verifier needed).
    Uses state parameter as the key to associate login with callback.
    """
    try:
        # client = await GlideClusterClient.create(redis_config_glide)
        client = RedisService()
        key = f"microsoft_auth_state:{state_key}"

        # Store only the essential PKCE verifier as JSON
        state_data = {
            "code_verifier": code_verifier,
            "client_id": CLIENT_ID,
            "state": state_key,
        }

        await client.set(key, json.dumps(state_data))
        await client.expire(key, ttl)  # Set expiration separately (Glide API)
        logger.info(f"✅ Stored auth state in Redis for state: {state_key[:20]}...")
        await client.close()
        return True
    except Exception as e:
        logger.warning(f"⚠️ Failed to store auth state in Redis: {str(e)}")
        return False


# Create Blueprint
microsoft_bp = Blueprint("microsoft", __name__)

# Microsoft OAuth Configuration
CLIENT_ID = os.environ.get("MICROSOFT_CLIENT_ID")
CLIENT_SECRET = os.environ.get("MICROSOFT_CLIENT_SECRET")
TENANT_ID = os.environ.get("MICROSOFT_TENANT_ID")
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
# Dynamic redirect URI - will be set based on current request (like Gmail)
REDIRECT_URI = None  # Will be dynamically set
# SCOPES = ["User.Read", "Mail.Send", "Mail.ReadWrite"]
SCOPES = [
    "User.Read",
    "Mail.Send",
    "Mail.ReadWrite",
    "Calendars.ReadWrite",
    "OnlineMeetings.ReadWrite",
    "Chat.ReadWrite",
    "Files.Read.All",
]


def get_microsoft_redirect_uri(request):
    """Generate dynamic redirect URI based on current request (always HTTPS for Microsoft)"""
    if request:
        # Use the current host from the request
        host = request.headers.get("Host")
        if host:
            # Always use HTTPS for Microsoft OAuth (required by Azure)
            return f"https://{host}/microsoft/callback"

    # Fallback: try to use BASE_API_URL if available
    base_api = os.getenv("BASE_API_URL")
    if base_api:
        return f"{base_api}/microsoft/callback"

    # Final fallback: use current working endpoint
    return "https://v0eoj1kl71.execute-api.us-east-1.amazonaws.com/microsoft/callback"


def is_microsoft_allowed_origin(origin):
    """Check if origin is allowed for Microsoft OAuth CORS"""
    if not origin:
        return False

    # Allowed origins (expandable for global access)
    allowed_patterns = [
        "https://app.bytoid.ai",
        "https://bytoid.ai",
        "https://www.bytoid.ai",
        "http://localhost:3000",
        "http://localhost:4173",
        "http://172.31.12.212",
    ]

    # Check exact matches first
    if origin in allowed_patterns:
        return True

    # Allow any bytoid.ai subdomain for global access
    if origin.endswith(".bytoid.ai") and origin.startswith("https://"):
        return True

    # Allow localhost for development (any port)
    if origin.startswith("http://localhost:") or origin.startswith(
        "https://localhost:"
    ):
        return True

    return False


# Validate required environment variables
# if not all([CLIENT_ID, CLIENT_SECRET, TENANT_ID]):
#     logger.error("❌ Missing required Microsoft OAuth environment variables")
#     logger.error(f"CLIENT_ID: {'✓' if CLIENT_ID else '✗'}")
#     logger.error(f"CLIENT_SECRET: {'✓' if CLIENT_SECRET else '✗'}")
#     logger.error(f"TENANT_ID: {'✓' if TENANT_ID else '✗'}")

# try:
#     msal_app = ConfidentialClientApplication(
#         client_id=CLIENT_ID,
#         client_credential=CLIENT_SECRET,
#         authority=AUTHORITY,
#         # Explicitly set token_cache to None for stateless operation
#         token_cache=None,
#     )
#     logger.info("✅ MSAL app initialized successfully")
# except Exception as e:
#     logger.error(f"❌ Failed to initialize MSAL app: {str(e)}")
#     msal_app = None


# Outlook Service Fallback (simplified version)


# ------ login related ------------#


@microsoft_bp.route("/microsoft/login", methods=["GET", "OPTIONS"])
def microsoft_login():
    """Simple Microsoft login with global access like Gmail"""

    # Handle CORS preflight for global access
    if request.method == "OPTIONS":
        response = make_response()
        origin = request.headers.get("Origin")
        if is_microsoft_allowed_origin(origin):
            response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Headers"] = (
            "Content-Type, Authorization, X-Requested-With"
        )
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return response

    logger.info("🚀 Starting Microsoft OAuth login (global access)")

    msal_app = initialize_msal()
    # Validate MSAL app is available
    if not msal_app:
        logger.error("❌ MSAL app not available")
        return jsonify({"error": "Microsoft OAuth not properly configured"}), 500

    try:
        # Clear any existing auth state from session
        session.pop("auth_flow", None)
        session.pop("microsoft_state", None)

        # Get dynamic redirect URI based on current request (like Gmail does)
        # redirect_uri = get_microsoft_redirect_uri(request)
        redirect_uri = redirect_uri = (
            f"{os.getenv('BASE_FRNT_URL')}/auth/microsoft/callback"
        )

        logger.info(f"🌍 Using  redirect URI: {redirect_uri}")

        # Create auth flow with dynamic redirect URI (like Google's approach)
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
        response = jsonify({"auth_url": flow["auth_uri"], "status": "success"})

        # Add CORS headers for global frontend access
        origin = request.headers.get("Origin")
        if is_microsoft_allowed_origin(origin):
            response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Headers"] = (
            "Content-Type, Authorization, X-Requested-With"
        )
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"

        return response

    except Exception as e:
        logger.error(f"❌ Error in Microsoft login: {str(e)}")
        return jsonify({"error": f"Login initiation failed: {str(e)}"}), 500


@microsoft_bp.route("/auth/microsoft/callback", methods=["GET"])
async def microsoft_callback():
    """Simple Microsoft callback - like Google oauth2callback"""
    # print(f"********* microsoft_callback ************")

    try:
        # Get parameters
        auth_code = request.args.get("code")
        error = request.args.get("error")
        state = request.args.get("state")
        newuser = None
        user_type = None

        if error:
            logger.error(f"❌ OAuth error: {error}")

            return redirect(f"{frontend_url}/login?error={error}")

        if not auth_code:
            logger.error("❌ No authorization code")

            return redirect(f"{frontend_url}/login?error=missing_code")

        if not state:
            logger.error("❌ No state parameter in callback")

            return redirect(f"{frontend_url}/login?error=missing_state")

        logger.info(f"✅ Callback received with state: {state[:20]}...")

        # ✅ Retrieve the stored PKCE verifier from Redis using state
        stored_state = await retrieve_auth_state_from_redis(state)

        if not stored_state:
            logger.error(f"❌ No auth state found in Redis for state: {state[:20]}...")

            return redirect(f"{frontend_url}/login?error=no_flow")

        code_verifier = stored_state.get("code_verifier")

        if not code_verifier:
            logger.error(f"❌ No code_verifier found in stored state")

            return redirect(f"{frontend_url}/login?error=no_verifier")

        # Use direct HTTP call to Microsoft token endpoint with PKCE code_verifier
        # redirect_uri = get_microsoft_redirect_uri(request)
        redirect_uri = redirect_uri = (
            f"{os.getenv('BASE_FRNT_URL')}/auth/microsoft/callback"
        )
        token_url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"

        try:
            token_data = {
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "code": auth_code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,  # ✅ Send PKCE verifier directly
                "scope": " ".join(SCOPES),
            }

            token_response = requests.post(token_url, data=token_data, timeout=10)

            if token_response.status_code != 200:
                logger.error(
                    f"❌ Token exchange failed: {token_response.status_code} - {token_response.text}"
                )

                return redirect(f"{frontend_url}/login?error=token_failed")

            result = token_response.json()

        except Exception as e:
            logger.error(f"❌ Token acquisition failed: {str(e)}")

            return redirect(f"{frontend_url}/login?error=token_failed")

        if not result or "access_token" not in result:
            logger.error(f"❌ No access token in result: {result}")

            return redirect(f"{frontend_url}/login?error=no_token")

        # Get user info (like Google does)
        access_token = result["access_token"]
        headers = {"Authorization": f"Bearer {access_token}"}

        userinfo_response = requests.get(
            "https://graph.microsoft.com/v1.0/me", headers=headers
        )

        if userinfo_response.status_code != 200:
            logger.error(f"❌ Failed to get user info: {userinfo_response.status_code}")

            return redirect(f"{frontend_url}/login?error=userinfo_failed")

        userinfo = userinfo_response.json()
        email = userinfo.get("mail") or userinfo.get("userPrincipalName")
        given_name = userinfo.get("givenName", "")
        family_name = userinfo.get("surname", "")
        user_id = userinfo.get("id")

        # Store user in session (like Google does)
        session["user"] = {
            "id": user_id,
            "name": f"{given_name} {family_name}".strip(),
            "email": email,
        }

        # Save to database (simplified like Google)
        try:
            conn = connect_to_rds()
            cursor = conn.cursor(pymysql.cursors.DictCursor)

            cursor.execute(
                "SELECT user_id, user_type FROM users WHERE email = %s", (email,)
            )
            existing_user = cursor.fetchone()
            # print("exising valus", existing_user)

            if existing_user:
                # print(f"***** existing user")
                # Update existing user
                user_type = existing_user["user_type"]
                # print("usertype", user_type)
                cursor.execute(
                    """
                    UPDATE users SET 
                        user_id = %s, first_name = %s, last_name = %s,
                        token = %s, refresh_token = %s, social = %s,
                        logged_in_at = NOW(), updated_in = NOW()
                    WHERE email = %s
                """,
                    (
                        user_id,
                        given_name,
                        family_name,
                        access_token,
                        result.get("refresh_token", ""),
                        "microsoft",
                        email,
                    ),
                )
                ensure_starter_credits_for_user(user_id, conn)
            else:
                # Create new user - EXACTLY like Google does
                cursor.execute(
                    """INSERT INTO users (user_id, user_type, launch_id_fk, first_name, last_name, email,phone, client_id,
                    client_secret, token, refresh_token, expiry, password_hash, profile_pic, location, social,
                    created_in, updated_in, logged_in_at, logged_out_at,sociallinks,subscribe_id,roles_creation,permissions,special_access )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW(), NOW(), %s,%s,%s,%s,%s,%s)
                """,
                    (
                        user_id,
                        "admin",
                        "",
                        given_name,
                        family_name,
                        email,
                        "",
                        CLIENT_ID,
                        CLIENT_SECRET,
                        access_token,
                        result.get("refresh_token", ""),
                        None,
                        "",
                        "",
                        "",
                        "microsoft",
                        None,
                        None,
                        None,
                        None,
                        None,
                        True,
                    ),
                )
                # print(f"******** created new user")

                # Auto-generate API key for new Microsoft users (like we do in users/generate-website-api-key)
                new_api_key = str(uuid.uuid4())
                new_launch_id = str(uuid.uuid4())
                new_sub_agent_id = str(uuid.uuid4())

                # Create default subagent
                cursor.execute(
                    """
                    INSERT INTO subagents (
                        sub_agent_id, launch_id_fk, name, description, voice_type,
                        documentation_link, model_version, created_at, updated_at
                    ) VALUES (%s, %s, %s, NULL, NULL, NULL, NULL, NULL, NULL)
                """,
                    (new_sub_agent_id, None, "Default Agent"),
                )

                # Create launch with API key

                cursor.execute(
                    """
                    INSERT INTO launch (launch_id, sub_agent_id_fk, user_id_fk, api_id, website_name)
                    VALUES (%s, %s, %s, %s, NULL)
                """,
                    (new_launch_id, new_sub_agent_id, user_id, new_api_key),
                )

                # Update subagent with correct launch_id
                cursor.execute(
                    """
                    UPDATE subagents SET launch_id_fk = %s WHERE sub_agent_id = %s
                """,
                    (new_launch_id, new_sub_agent_id),
                )

                logger.info(
                    f"✅ Auto-generated API key {str(new_api_key)[:20]}... for new Microsoft user {email}"
                )
                # 3️⃣ Fetch STARTER plan
                cursor.execute(
                    """
                    SELECT plan_code, monthly_token_limit
                    FROM plans
                    WHERE plan_code = 'STARTER'
                    AND is_active = 1
                    LIMIT 1
                    """
                )
                starter_plan = cursor.fetchone()

                if not starter_plan:
                    raise Exception("STARTER plan not found")

                # 4️⃣ Create FREE subscription (internal, no Stripe)
                cursor.execute(
                    """
                    INSERT INTO subscriptions (
                        user_id,
                        stripe_subscription_id,
                        stripe_customer_id,
                        stripe_price_id,
                        status,
                        current_period_start,
                        current_period_end,
                        created_at
                    ) VALUES (
                        %s,
                        %s,
                        NULL,
                        NULL,
                        'active',
                        NOW(),
                        NULL,
                        NOW()
                    )
                    """,
                    (
                        user_id,
                        "STARTER",  # internal reference
                    ),
                )

                # 5️⃣ Create credit bucket (250,000 credits)
                cursor.execute(
                    """
                    INSERT INTO credit_buckets (
                        bucket_id,
                        user_id,
                        source_type,
                        source_ref,
                        credits_total,
                        credits_used,
                        expires_at,
                        is_expired,
                        created_at
                    ) VALUES (
                        UUID(),
                        %s,
                        'SUBSCRIPTION',
                        %s,
                        %s,
                        0,
                        DATE_ADD(NOW(), INTERVAL 60 DAY),
                        0,
                        NOW()
                    )
                    """,
                    (
                        user_id,
                        "STARTER",
                        starter_plan["monthly_token_limit"],
                    ),
                )

            conn.commit()
            cursor.close()
            conn.close()

        except Exception as db_error:
            logger.error(f"❌ Database error: {str(db_error)}")
            # Continue anyway - don't fail login for DB issues

        # Check if user needs onboarding
        new_user = False
        if user_type == "user":
            newuser = True
        else:
            newuser = check_onboarding_user(user_id)

        apikey = fetch_apikey_from_launch(user_id)
        # service = OutlookService(user_id=user_id)
        # service.create_subscription(access_token, email)

        connection = connect_to_rds()

        mailbox_setting = check_mailbox(user_id)
        if mailbox_setting:
            manager = OutlookSubscriptionManager()
            future = manager.create_subscription_async(access_token, email)

        # # get all integrations for this user and store it in redis
        integrations_data, status_code = get_all_integrations(user_id)
        integrations = integrations_data.get("integrations")

        # print(f"integrations_data : {integrations_data}")
        if integrations_data:
            redis_response = await store_integrations_in_redis(
                user_id, integrations_data
            )
            # if redis_response:
            #     #print(f"integrations stored in redis")
            # else:
            #     #print(f"integrations not stored in redis")

            exists = any(
                item["platform"] == "google"
                for item in integrations_data.get("integrations", [])
            )
            if exists:
                # print(f"goole in integratiosn")
                google_user_id = refresh_expired_google_tokens_for_integrations(
                    user_id, connection
                )

                # print(f"calling watch servoce using : {google_user_id}")
                service = GmailService(user_id=google_user_id, integration=True)
                service.create_watch_req()

        # ---- SESSION LOGIN ----
        session_id, access_token_session, refresh_token_session = await session_login(
            user_id
        )

        # check if credits are available
        credits = CreditManager(connection)
        avail_credits = credits.check_if_remaining(user_id=user_id)
        credit_status = avail_credits.get("status")
        message = avail_credits.get("message")

        cursor.close()
        connection.close()

        response = make_response(
            jsonify(
                {
                    "status": "success",
                    "userid": user_id,
                    "user_onboarded": newuser,
                    "api_key": apikey or "",
                    "service": "microsoft",
                    "credit_status": credit_status,
                    "message": message,
                }
            )
        )
        redis = RedisService()
        await update_user_alive(redis, user_id, True)

        # Set secure cookies
        response.set_cookie(
            "session_id",
            session_id,
            max_age=30 * 24 * 60 * 60,
            httponly=True,
            secure=True,
            samesite="None",
        )
        response.set_cookie(
            "access_token",
            access_token_session,
            max_age=30 * 24 * 60 * 60,
            httponly=True,
            secure=True,
            samesite="None",
        )
        response.set_cookie(
            "refresh_token",
            refresh_token_session,
            max_age=30 * 24 * 60 * 60,
            httponly=True,
            secure=True,
            samesite="None",
        )

        return response

    except Exception as e:
        logger.error(f"❌ Microsoft callback error: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")

        return redirect(f"{frontend_url}/login?error=callback_failed")


@microsoft_bp.route("/microsoft/session-debug", methods=["GET"])
def session_debug():
    """Debug endpoint to check session status"""
    try:
        return jsonify(
            {
                "session_id": session.get("_id"),
                "session_keys": list(session.keys()),
                "has_auth_flow": "auth_flow" in session,
                "auth_flow_keys": (
                    list(session.get("auth_flow", {}).keys())
                    if session.get("auth_flow")
                    else []
                ),
                "user": session.get("user"),
                "permanent": session.permanent,
                "msal_available": msal_app is not None,
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@microsoft_bp.route("/microsoft/test-cors", methods=["GET", "OPTIONS"])
def test_cors():
    """Simple endpoint to test CORS configuration"""
    if request.method == "OPTIONS":
        response = make_response()
        origin = request.headers.get("Origin")
        logger.info(f"🔍 CORS Preflight - Origin: {origin}")
        if is_microsoft_allowed_origin(origin):
            response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        return response

    logger.info(f"🔍 CORS Test - Origin: {request.headers.get('Origin')}")
    logger.info(f"🔍 CORS Test - Headers: {dict(request.headers)}")

    response = make_response(
        jsonify(
            {
                "status": "success",
                "message": "CORS test successful",
                "origin": request.headers.get("Origin"),
                "headers": dict(request.headers),
            }
        )
    )

    origin = request.headers.get("Origin")
    if is_microsoft_allowed_origin(origin):
        response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Credentials"] = "true"

    return response


def initialize_msal():
    if not all([CLIENT_ID, CLIENT_SECRET, TENANT_ID]):
        logger.error("❌ Missing required Microsoft OAuth environment variables")
        logger.error(f"CLIENT_ID: {'✓' if CLIENT_ID else '✗'}")
        logger.error(f"CLIENT_SECRET: {'✓' if CLIENT_SECRET else '✗'}")
        logger.error(f"TENANT_ID: {'✓' if TENANT_ID else '✗'}")

    try:
        msal_app = ConfidentialClientApplication(
            client_id=CLIENT_ID,
            client_credential=CLIENT_SECRET,
            authority=AUTHORITY,
            # Explicitly set token_cache to None for stateless operation
            token_cache=None,
        )
        logger.info("✅ MSAL app initialized successfully")
        return msal_app
    except Exception as e:
        logger.error(f"❌ Failed to initialize MSAL app: {str(e)}")
        return None


# --------- related to fetching to emails ----------------#
async def fetch_outlook_emails_batch(
    user_id, page_token=None, batch_size=100, months=12
):  # ✅ Changed default from 3 to 12 months
    """
    Fetch a batch of Outlook emails with pagination support (like Gmail)
    Uses Microsoft Graph API with skipToken for pagination
    """
    try:
        logger.info(
            f"🚀 Starting Outlook batch fetch for user: {user_id}, batch_size: {batch_size}"
        )

        # Get user token
        conn = connect_to_rds()
        cursor = conn.cursor()
        cursor.execute("SELECT token, email FROM users WHERE user_id = %s", (user_id,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if not row:
            logger.error(f"❌ User not found: {user_id}")
            return {"status": "error", "error": "User not found", "new_messages": 0}

        access_token, user_email = row
        headers = {"Authorization": f"Bearer {access_token}"}

        # Calculate date filter (last N months)
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=months * 30)
        start_date_str = start_date.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        # Build request parameters with pagination
        params = {
            "$filter": f"receivedDateTime ge {start_date_str}",
            "$orderby": "receivedDateTime desc",
            "$top": min(batch_size, 100),  # API limit is 100
            "$select": "id,subject,body,from,toRecipients,receivedDateTime,sentDateTime,sender,internetMessageId,conversationId,hasAttachments,importance,categories",
        }

        # Use skipToken for pagination (continuation from where we left off)
        if page_token:
            params["$skiptoken"] = page_token
            logger.info(f"📄 Using skip token for pagination")

        # Make API call
        logger.info(f"📨 Calling Microsoft Graph API with params: {params}")
        response = requests.get(
            "https://graph.microsoft.com/v1.0/me/messages",
            headers=headers,
            params=params,
            timeout=30,
        )

        if response.status_code != 200:
            logger.error(
                f"❌ Microsoft Graph API error: {response.status_code} - {response.text}"
            )
            return {
                "status": "error",
                "error": f"API error: {response.status_code}",
                "new_messages": 0,
            }

        data = response.json()
        messages = data.get("value", [])
        next_page_token = data.get("@odata.nextLink")  # Get continuation link

        logger.info(f"✅ Fetched {len(messages)} messages from Outlook")

        # Process messages
        processed_messages = []
        for msg in messages:
            try:
                email_id = msg.get("id")
                from_data = msg.get("from", {}).get("emailAddress", {})
                from_address = from_data.get("address", "")
                from_name = from_data.get("name", "")

                to_recipients = msg.get("toRecipients", [])
                to_email = ""
                to_name = ""
                if to_recipients and len(to_recipients) > 0:
                    to_email = (
                        to_recipients[0].get("emailAddress", {}).get("address", "")
                    )
                    to_name = to_recipients[0].get("emailAddress", {}).get("name", "")

                # Extract body
                body_content = msg.get("body", {}).get("content", "")
                soup = BeautifulSoup(body_content, "html.parser")
                plain_text = soup.get_text(separator="\n").strip()

                # Determine direction
                direction = (
                    "inbound"
                    if from_address.lower() != user_email.lower()
                    else "outbound"
                )

                # Create processed message
                processed_msg = {
                    "id": email_id,
                    "email_id": email_id,  # Duplicate for consistency
                    "from_email": from_address,
                    "from_name": from_name,
                    "from": from_name or from_address,
                    "to_email": to_email,
                    "to_name": to_name,
                    "to": to_name or to_email,
                    "body": plain_text,
                    "subject": msg.get("subject", ""),
                    "timestamp": msg.get("receivedDateTime") or msg.get("sentDateTime"),
                    "received_time": msg.get("receivedDateTime"),
                    "sent_time": msg.get("sentDateTime"),
                    "direction": direction,
                    "conversation_id": msg.get("conversationId"),
                    "internet_message_id": msg.get("internetMessageId"),
                    "has_attachments": msg.get("hasAttachments", False),
                    "importance": msg.get("importance", "normal"),
                    "source": "outlook",
                    "user_id": user_id,
                    "categories": msg.get("categories", []),
                }
                processed_messages.append(processed_msg)
                logger.debug(f"✅ Processed message: {email_id} from {from_address}")

            except Exception as e:
                logger.error(
                    f"❌ Error processing message {msg.get('id', 'unknown')}: {str(e)}"
                )
                continue

        # Extract next skip token from continuation link if present
        next_skip_token = None
        if next_page_token:
            try:
                import urllib.parse as urlparse

                parsed_url = urlparse.urlparse(next_page_token)
                query_params = urlparse.parse_qs(parsed_url.query)
                if "$skiptoken" in query_params:
                    next_skip_token = query_params["$skiptoken"][0]
                    logger.info(f"📄 Next skip token available for pagination")
            except Exception as e:
                logger.warning(f"⚠️ Could not extract skip token: {str(e)}")

        return {
            "status": "success",
            "new_messages": len(processed_messages),
            "messages": processed_messages,
            "next_page_token": next_skip_token,
            "has_more": bool(next_skip_token),
            "batch_size": len(processed_messages),
        }

    except requests.Timeout:
        logger.error(f"❌ Timeout fetching emails for user {user_id}")
        return {"status": "error", "error": "Request timeout", "new_messages": 0}
    except Exception as e:
        logger.error(f"❌ Batch fetch error: {str(e)}")
        return {"status": "error", "error": str(e), "new_messages": 0}


@microsoft_bp.route("/microsoft/get_emails_infinite", methods=["POST"])
def microsoft_get_emails_infinite():
    """Infinite scroll email fetching - loads emails in chunks as user scrolls"""
    try:
        data = request.get_json() or {}

        # Get pagination parameters
        page_size = data.get("page_size", 100)  # ✅ Increased default from 20 to 100
        skip_token = data.get("skip_token")  # For continuing from where we left off
        cursor = data.get("cursor")  # Alternative cursor-based pagination
        last_timestamp = data.get("last_timestamp")  # For timestamp-based continuation
        months = data.get("months", 12)  # ✅ Increased from 3 to 12 months (1 year)

        # Get user info
        email = session.get("user", {}).get("email")
        user_id = data.get("user_id")

        if not email and not user_id:
            return jsonify({"error": "User authentication required"}), 401

        conn = connect_to_rds()
        cursor_db = conn.cursor()

        if email and not user_id:
            cursor_db.execute(
                "SELECT token, user_id FROM users WHERE email = %s", (email,)
            )
            row = cursor_db.fetchone()
            if not row:
                cursor_db.close()
                conn.close()
                return jsonify({"error": "User not found"}), 404
            access_token, user_id = row
        else:
            cursor_db.execute(
                "SELECT token, email FROM users WHERE user_id = %s", (user_id,)
            )
            row = cursor_db.fetchone()
            if not row:
                cursor_db.close()
                conn.close()
                return jsonify({"error": "User not found"}), 404
            access_token, email = row

        cursor_db.close()
        conn.close()

        headers = {"Authorization": f"Bearer {access_token}"}
        base_url = "https://graph.microsoft.com/v1.0/me/messages"

        # Calculate date filter
        from datetime import datetime, timedelta, timezone

        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=months * 30)
        start_date_str = start_date.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        # Build request parameters
        params = {
            "$orderby": "receivedDateTime desc",
            "$top": page_size,
            "$select": "id,subject,body,from,toRecipients,receivedDateTime,sentDateTime,sender,internetMessageId,conversationId,hasAttachments,importance",
        }

        # Add date filter
        filter_conditions = [f"receivedDateTime ge {start_date_str}"]

        # Add timestamp filter for infinite scroll continuation
        if last_timestamp:
            filter_conditions.append(f"receivedDateTime lt {last_timestamp}")

        params["$filter"] = " and ".join(filter_conditions)

        # Use skip token if provided (Microsoft Graph pagination)
        if skip_token:
            params["$skiptoken"] = skip_token

        logger.info(
            f"🔄 Infinite scroll request: page_size={page_size}, skip_token={skip_token is not None}"
        )

        # Make the request
        response = requests.get(base_url, headers=headers, params=params)

        if response.status_code != 200:
            logger.error(
                f"❌ Failed to fetch emails: {response.status_code} - {response.text}"
            )
            return (
                jsonify({"error": f"Failed to fetch emails: {response.text}"}),
                response.status_code,
            )

        response_data = response.json()
        emails = response_data.get("value", [])
        next_link = response_data.get("@odata.nextLink")

        logger.info(f"📧 Fetched {len(emails)} emails for infinite scroll")

        # Process emails for frontend
        processed_emails = []
        new_skip_token = None
        new_last_timestamp = None

        for email_data in emails:
            try:
                # Extract email details
                email_id = email_data.get("id")
                from_name = (
                    email_data.get("sender", {}).get("emailAddress", {}).get("name", "")
                )
                from_address = (
                    email_data.get("from", {})
                    .get("emailAddress", {})
                    .get("address", "")
                )

                to_recipients = email_data.get("toRecipients", [])
                to_email = ""
                to_name = ""
                if to_recipients:
                    to_email = to_recipients[0]["emailAddress"]["address"]
                    to_name = to_recipients[0]["emailAddress"]["name"]

                body_content = email_data.get("body", {}).get("content", "")
                soup = BeautifulSoup(body_content, "html.parser")
                plain_text = soup.get_text().strip()

                # Truncate body for infinite scroll (show preview only)
                preview_text = (
                    plain_text[:200] + "..." if len(plain_text) > 200 else plain_text
                )

                subject = email_data.get("subject", "")
                received_time = email_data.get("receivedDateTime")
                sent_time = email_data.get("sentDateTime")

                direction = (
                    "inbound" if from_address.lower() != email.lower() else "outbound"
                )

                # Format for frontend
                processed_email = {
                    "id": email_id,
                    "subject": subject,
                    "from_name": from_name,
                    "from_email": from_address,
                    "to_name": to_name,
                    "to_email": to_email,
                    "preview": preview_text,
                    "timestamp": received_time or sent_time,
                    "received_time": received_time,
                    "direction": direction,
                    "has_attachments": email_data.get("hasAttachments", False),
                    "importance": email_data.get("importance", "normal"),
                    "conversation_id": email_data.get("conversationId"),
                }

                processed_emails.append(processed_email)

                # Update last timestamp for next request
                if received_time:
                    new_last_timestamp = received_time

            except Exception as e:
                logger.error(
                    f"❌ Error processing email {email_data.get('id', 'unknown')}: {str(e)}"
                )
                continue

        # Extract skip token from next_link if available
        if next_link:
            import urllib.parse as urlparse

            parsed_url = urlparse.urlparse(next_link)
            query_params = urlparse.parse_qs(parsed_url.query)
            if "$skiptoken" in query_params:
                new_skip_token = query_params["$skiptoken"][0]

        # Determine if there are more emails
        has_more = len(emails) == page_size and (next_link is not None)

        logger.info(
            f"✅ Infinite scroll response: {len(processed_emails)} emails, has_more={has_more}"
        )

        return jsonify(
            {
                "status": "success",
                "emails": processed_emails,
                "has_more": has_more,
                "skip_token": new_skip_token,
                "last_timestamp": new_last_timestamp,
                "page_size": page_size,
                "total_returned": len(processed_emails),
            }
        )

    except Exception as e:
        logger.error(f"❌ Error in infinite scroll fetch: {str(e)}")
        logger.error(f"❌ Traceback: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


@microsoft_bp.route("/microsoft/get_email_detail", methods=["POST"])
def microsoft_get_email_detail():
    """Get full email content when user clicks on an email"""
    try:
        data = request.get_json() or {}
        email_id = data.get("email_id")

        if not email_id:
            return jsonify({"error": "Email ID is required"}), 400

        # Get user info
        email = session.get("user", {}).get("email")
        user_id = data.get("user_id")

        if not email and not user_id:
            return jsonify({"error": "User authentication required"}), 401

        conn = connect_to_rds()
        cursor = conn.cursor()

        if email and not user_id:
            cursor.execute(
                "SELECT token, user_id FROM users WHERE email = %s", (email,)
            )
            row = cursor.fetchone()
            if not row:
                cursor.close()
                conn.close()
                return jsonify({"error": "User not found"}), 404
            access_token, user_id = row
        else:
            cursor.execute("SELECT token FROM users WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
            if not row:
                cursor.close()
                conn.close()
                return jsonify({"error": "User not found"}), 404
            access_token = row[0]

        cursor.close()
        conn.close()

        headers = {"Authorization": f"Bearer {access_token}"}
        url = f"https://graph.microsoft.com/v1.0/me/messages/{email_id}"

        # Get full email details
        params = {
            "$select": "id,subject,body,from,toRecipients,ccRecipients,bccRecipients,receivedDateTime,sentDateTime,sender,internetMessageId,conversationId,hasAttachments,importance,flag,categories,attachments"
        }

        response = requests.get(url, headers=headers, params=params)

        if response.status_code != 200:
            logger.error(
                f"❌ Failed to fetch email detail: {response.status_code} - {response.text}"
            )
            return (
                jsonify({"error": f"Failed to fetch email: {response.text}"}),
                response.status_code,
            )

        email_data = response.json()

        # Process full email data
        body_content = email_data.get("body", {}).get("content", "")
        body_type = email_data.get("body", {}).get("contentType", "html")

        # Parse HTML to plain text for plain text version
        soup = BeautifulSoup(body_content, "html.parser")
        plain_text = soup.get_text().strip()

        # Get attachments if any
        attachments = []
        if email_data.get("hasAttachments", False):
            attachments_url = (
                f"https://graph.microsoft.com/v1.0/me/messages/{email_id}/attachments"
            )
            att_response = requests.get(attachments_url, headers=headers)
            if att_response.status_code == 200:
                att_data = att_response.json()
                for attachment in att_data.get("value", []):
                    attachments.append(
                        {
                            "id": attachment.get("id"),
                            "name": attachment.get("name"),
                            "contentType": attachment.get("contentType"),
                            "size": attachment.get("size"),
                            "isInline": attachment.get("isInline", False),
                        }
                    )

        # Format detailed response
        detailed_email = {
            "id": email_data.get("id"),
            "subject": email_data.get("subject", ""),
            "from": {
                "name": email_data.get("from", {})
                .get("emailAddress", {})
                .get("name", ""),
                "email": email_data.get("from", {})
                .get("emailAddress", {})
                .get("address", ""),
            },
            "to": [
                {
                    "name": recipient.get("emailAddress", {}).get("name", ""),
                    "email": recipient.get("emailAddress", {}).get("address", ""),
                }
                for recipient in email_data.get("toRecipients", [])
            ],
            "cc": [
                {
                    "name": recipient.get("emailAddress", {}).get("name", ""),
                    "email": recipient.get("emailAddress", {}).get("address", ""),
                }
                for recipient in email_data.get("ccRecipients", [])
            ],
            "bcc": [
                {
                    "name": recipient.get("emailAddress", {}).get("name", ""),
                    "email": recipient.get("emailAddress", {}).get("address", ""),
                }
                for recipient in email_data.get("bccRecipients", [])
            ],
            "body": {
                "html": body_content if body_type.lower() == "html" else None,
                "text": plain_text,
                "contentType": body_type,
            },
            "received_time": email_data.get("receivedDateTime"),
            "sent_time": email_data.get("sentDateTime"),
            "importance": email_data.get("importance", "normal"),
            "has_attachments": email_data.get("hasAttachments", False),
            "attachments": attachments,
            "conversation_id": email_data.get("conversationId"),
            "internet_message_id": email_data.get("internetMessageId"),
            "categories": email_data.get("categories", []),
            "flag": email_data.get("flag", {}),
        }

        logger.info(f"✅ Retrieved full email detail for {email_id}")

        return jsonify({"status": "success", "email": detailed_email})

    except Exception as e:
        logger.error(f"❌ Error getting email detail: {str(e)}")
        return jsonify({"error": str(e)}), 500


@microsoft_bp.route("/microsoft/get_email")
def microsoft_get_email():
    """Fetch Outlook emails with proper pagination and date filtering"""
    try:
        conn = connect_to_rds()
        cursor = conn.cursor()

        email = session.get("user", {}).get("email")
        # print(f"Fetching emails for: {email}")

        if not email:
            return redirect(f"{os.getenv('BASE_FRNT_URL')}/login")

        cursor.execute("SELECT token, user_id FROM users WHERE email = %s", (email,))
        row = cursor.fetchone()

        if not row:
            return jsonify({"error": "Access token not found for user"}), 404

        access_token, user_id = row

        headers = {"Authorization": f"Bearer {access_token}"}

        # Calculate date filter for last 12 months (1 year of history)
        from datetime import datetime, timedelta, timezone

        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(
            days=365
        )  # ✅ Changed from 90 to 365 days (1 year)
        start_date_str = start_date.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        # Use proper Microsoft Graph API with filtering and pagination
        base_url = "https://graph.microsoft.com/v1.0/me/messages"

        all_emails = []
        page_size = 100  # ✅ Increased from 50 to 100 for better performance
        total_fetched = 0
        max_emails = 2000  # ✅ Increased limit from 1000 to 2000

        # Initial request with filters
        params = {
            "$filter": f"receivedDateTime ge {start_date_str}",
            "$orderby": "receivedDateTime desc",
            "$top": page_size,
            "$select": "id,subject,body,from,toRecipients,receivedDateTime,sentDateTime,sender,internetMessageId,conversationId,hasAttachments",
        }

        logger.info(f"🔄 Starting to fetch emails with date filter: {start_date_str}")

        # Fetch first page
        response = requests.get(base_url, headers=headers, params=params)

        if response.status_code != 200:
            logger.error(
                f"❌ Failed to fetch emails: {response.status_code} - {response.text}"
            )
            cursor.close()
            conn.close()
            return (
                jsonify({"error": f"Failed to fetch emails: {response.text}"}),
                response.status_code,
            )

        response_data = response.json()
        emails = response_data.get("value", [])
        next_link = response_data.get("@odata.nextLink")

        logger.info(f"📧 First page: {len(emails)} emails")
        all_emails.extend(emails)
        total_fetched += len(emails)

        # Continue fetching pages using nextLink
        while next_link and total_fetched < max_emails:
            logger.info(f"🔄 Fetching next page... (total so far: {total_fetched})")

            response = requests.get(next_link, headers=headers)

            if response.status_code != 200:
                logger.warning(f"⚠️ Failed to fetch page: {response.status_code}")
                break

            response_data = response.json()
            emails = response_data.get("value", [])
            next_link = response_data.get("@odata.nextLink")

            if not emails:
                logger.info("📭 No more emails found")
                break

            all_emails.extend(emails)
            total_fetched += len(emails)

            logger.info(
                f"📧 Fetched {len(emails)} emails this page (total: {total_fetched})"
            )

            # Add small delay to avoid rate limiting
            import time

            time.sleep(0.1)

        logger.info(f"✅ Total emails fetched: {len(all_emails)}")
        cursor.close()
        conn.close()

        if all_emails:
            # Process and save emails to database (same pattern as Gmail)
            processed_count = 0

            # Reconnect to database for processing
            conn = connect_to_rds()
            cursor = conn.cursor()

            logger.info(f"🔄 Processing {len(all_emails)} emails...")

            for i, email_data in enumerate(all_emails):
                try:
                    if i % 100 == 0:  # Log progress every 100 emails
                        logger.info(f"📧 Processing email {i+1}/{len(all_emails)}")

                    # Process email data same as before
                    email_id = email_data.get("id")
                    from_name = (
                        email_data.get("sender", {})
                        .get("emailAddress", {})
                        .get("name", "")
                    )
                    to_recipients = email_data.get("toRecipients", [])

                    body_content = email_data.get("body", {}).get("content", "")
                    internet_message_id = email_data.get("internetMessageId")
                    soup = BeautifulSoup(body_content, "html.parser")
                    plain_text = soup.get_text().strip()
                    subject = email_data.get("subject", "")
                    sent_time = email_data.get("sentDateTime")
                    received_time = email_data.get("receivedDateTime")

                    from_address = (
                        email_data.get("from", {})
                        .get("emailAddress", {})
                        .get("address", "")
                    )
                    direction = (
                        "inbound"
                        if from_address.lower() != email.lower()
                        else "outbound"
                    )

                    # Get to_email safely
                    to_email = ""
                    to_name = ""
                    if to_recipients:
                        to_email = to_recipients[0]["emailAddress"]["address"]
                        to_name = to_recipients[0]["emailAddress"]["name"]

                    conversation_id = email_data.get("conversationId") or (
                        from_address if direction == "inbound" else to_email
                    )
                    message_id = str(uuid.uuid4())

                    # Check if message already exists in MESSAGES to avoid duplicates
                    if email_id not in MESSAGES:
                        # Save to MESSAGES for backward compatibility
                        MESSAGES[email_id] = {
                            "id": message_id,
                            "from": from_name,
                            "to": to_name,
                            "to_email": to_email,
                            "from_email": from_address,
                            "body": plain_text,
                            "subject": subject,
                            "timestamp": sent_time or received_time,
                            "received_time": received_time,
                            "sent_time": sent_time,
                            "status": "received" if direction == "inbound" else "sent",
                            "source": "outlook",
                            "direction": direction,
                            "conversation_id": conversation_id,
                            "internet_message_id": internet_message_id,
                            "has_attachments": email_data.get("hasAttachments", False),
                        }

                        # Now save to database exactly like Gmail does
                        save_outlook_message_to_db(
                            cursor,
                            user_id,
                            message_id,
                            from_address,
                            plain_text,
                            subject,
                            sent_time or received_time,
                            direction,
                            conversation_id,
                        )
                        processed_count += 1
                    else:
                        logger.debug(f"📧 Email {email_id} already exists, skipping")

                except Exception as e:
                    logger.error(
                        f"❌ Error processing Outlook email {email_data.get('id', 'unknown')}: {str(e)}"
                    )
                    continue

            conn.commit()
            cursor.close()
            conn.close()

            logger.info(
                f"✅ Processed {processed_count} new Outlook emails for user {email}"
            )

            return jsonify(
                {
                    "status": "success",
                    "total_fetched": len(all_emails),
                    "processed_count": processed_count,
                    "stored_messages": len(
                        [
                            k
                            for k in MESSAGES.keys()
                            if MESSAGES[k].get("source") == "outlook"
                        ]
                    ),
                    "message": f"Successfully fetched {len(all_emails)} emails and processed {processed_count} new ones",
                }
            )
        else:
            logger.info("📭 No emails found in the specified date range")
            return jsonify(
                {
                    "status": "success",
                    "total_fetched": 0,
                    "processed_count": 0,
                    "stored_messages": 0,
                    "message": "No emails found in the last 3 months",
                }
            )

    except Exception as e:
        logger.error(f"❌ Error in microsoft_get_email: {str(e)}")
        logger.error(f"❌ Traceback: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


@microsoft_bp.route("/microsoft/get_emails_batch", methods=["POST"])
def microsoft_get_emails_batch():
    """Fetch Outlook emails in batches with pagination support"""
    try:
        data = request.get_json() or {}
        user_id = data.get("user_id")
        page_token = data.get("page_token")  # For pagination continuation
        batch_size = data.get("batch_size", 100)  # Default 100 per batch
        months = data.get("months", 3)  # Default to 12 months (1 year of history)

        # Try to get user_id from session if not provided
        if not user_id:
            user_info = session.get("user", {})
            user_id = user_info.get("id")

        if not user_id:
            return jsonify({"error": "User ID is required"}), 400

        logger.info(
            f"🚀 Starting batch email fetch for user: {user_id} (batch_size: {batch_size}, months: {months}, page_token: {page_token is not None})"
        )

        # Fetch emails using the new batch service with pagination
        result = asyncio.run(
            fetch_outlook_emails_batch(
                user_id=user_id,
                page_token=page_token,
                batch_size=batch_size,
                months=months,
            )
        )

        if result["status"] == "error":
            logger.error(f"❌ Batch fetch failed: {result['error']}")
            return jsonify({"error": result["error"]}), 500

        # Store the fetched emails in the database
        stored_result = {}
        if result.get("messages"):
            try:
                logger.info(
                    f"🔄 About to store {len(result['messages'])} messages in database"
                )
                stored_result = store_outlook_emails_in_db(user_id, result["messages"])
                logger.info(f"✅ Storage result: {stored_result}")
            except Exception as storage_error:
                logger.error(f"❌ Error storing messages: {str(storage_error)}")
                logger.error(f"❌ Storage traceback: {traceback.format_exc()}")
                stored_result = {
                    "status": "error",
                    "stored_count": 0,
                    "error": str(storage_error),
                }

        response_data = {
            "status": "success",
            "user_id": user_id,
            "batch_size": batch_size,
            "months": months,
            "new_messages": result.get("new_messages", 0),
            "has_more": result.get("has_more", False),
            "next_page_token": result.get("next_page_token"),
            "stored_count": stored_result.get("stored_count", 0),
            "skipped_count": stored_result.get("skipped_count", 0),
            "message": f"Fetched {result.get('new_messages', 0)} emails, stored {stored_result.get('stored_count', 0)}",
        }

        # Only include full message list if specifically requested
        if data.get("include_messages", False):
            response_data["messages"] = result.get("messages", [])

        logger.info(f"✅ Batch fetch completed: {response_data}")

        return jsonify(response_data), 200

    except Exception as e:
        logger.error(f"❌ Error in batch email fetch: {str(e)}")
        logger.error(f"❌ Traceback: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


@microsoft_bp.route("/microsoft/get_emails_count", methods=["POST"])
async def microsoft_get_emails_count():
    """Get total count of emails for the last N months"""
    try:
        data = request.get_json() or {}
        user_id = data.get("user_id")
        months = data.get("months", 3)

        # Try to get user_id from session if not provided
        if not user_id:
            user_info = session.get("user", {})
            user_id = user_info.get("id")

        if not user_id:
            return jsonify({"error": "User ID is required"}), 400

        logger.info(f"📊 Getting email count for user: {user_id}")

        # Initialize Outlook service and get count
        service = OutlookService(user_id)
        total_count = await service.get_total_message_count(months=months)

        return (
            jsonify(
                {
                    "user_id": user_id,
                    "months": months,
                    "total_messages": total_count,
                    "status": "success",
                }
            ),
            200,
        )

    except Exception as e:
        logger.error(f"❌ Error getting email count: {str(e)}")
        return jsonify({"error": str(e)}), 500


@microsoft_bp.route("/microsoft/fetch_all_emails", methods=["POST"])
def microsoft_fetch_all_emails():
    """Enhanced endpoint to fetch all emails with better pagination and filtering"""
    try:
        data = request.get_json() or {}
        user_id = data.get("user_id")
        months = data.get("months", 6)  # Default to 6 months for comprehensive fetch
        max_emails = data.get("max_emails", 5000)  # Higher default limit

        # Try to get user_id from session if not provided
        if not user_id:
            user_info = session.get("user", {})
            user_id = user_info.get("id")
            email = session.get("user", {}).get("email")

            if email and not user_id:
                # Get user_id from database
                conn = connect_to_rds()
                cursor = conn.cursor()
                cursor.execute("SELECT user_id FROM users WHERE email = %s", (email,))
                row = cursor.fetchone()
                if row:
                    user_id = row[0]
                cursor.close()
                conn.close()

        if not user_id:
            return jsonify({"error": "User ID is required"}), 400

        logger.info(f"🚀 Starting comprehensive email fetch for user: {user_id}")
        logger.info(f"📊 Parameters: {months} months, max {max_emails} emails")

        # Get user credentials
        conn = connect_to_rds()
        cursor = conn.cursor()
        cursor.execute("SELECT token, email FROM users WHERE user_id = %s", (user_id,))
        row = cursor.fetchone()

        if not row:
            cursor.close()
            conn.close()
            return jsonify({"error": "User not found"}), 404

        access_token, user_email = row
        cursor.close()
        conn.close()

        headers = {"Authorization": f"Bearer {access_token}"}

        # Calculate comprehensive date filter
        from datetime import datetime, timedelta, timezone

        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(
            days=months * 30
        )  # More precise month calculation
        start_date_str = start_date.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        logger.info(
            f"📅 Fetching emails from {start_date_str} to {end_date.strftime('%Y-%m-%dT%H:%M:%S.%fZ')}"
        )

        # Use Microsoft Graph API with comprehensive filtering
        base_url = "https://graph.microsoft.com/v1.0/me/messages"

        all_emails = []
        page_size = 100  # Larger page size for efficiency
        total_fetched = 0
        page_count = 0

        # Enhanced initial request with more comprehensive selection
        params = {
            "$filter": f"receivedDateTime ge {start_date_str}",
            "$orderby": "receivedDateTime desc",
            "$top": page_size,
            "$select": "id,subject,body,from,toRecipients,receivedDateTime,sentDateTime,sender,internetMessageId,conversationId,hasAttachments,importance,flag,categories,isDraft",
        }

        logger.info(f"🔄 Starting comprehensive fetch...")

        # First request
        response = requests.get(base_url, headers=headers, params=params)

        if response.status_code != 200:
            logger.error(
                f"❌ Failed to fetch emails: {response.status_code} - {response.text}"
            )
            return (
                jsonify({"error": f"Failed to fetch emails: {response.text}"}),
                response.status_code,
            )

        response_data = response.json()
        emails = response_data.get("value", [])
        next_link = response_data.get("@odata.nextLink")

        logger.info(f"📧 First page: {len(emails)} emails")
        all_emails.extend(emails)
        total_fetched += len(emails)
        page_count += 1

        # Continue fetching all pages
        while next_link and total_fetched < max_emails:
            page_count += 1
            logger.info(
                f"🔄 Fetching page {page_count}... (total so far: {total_fetched})"
            )

            response = requests.get(next_link, headers=headers)

            if response.status_code != 200:
                logger.warning(
                    f"⚠️ Failed to fetch page {page_count}: {response.status_code}"
                )
                break

            response_data = response.json()
            emails = response_data.get("value", [])
            next_link = response_data.get("@odata.nextLink")

            if not emails:
                logger.info("📭 No more emails found")
                break

            all_emails.extend(emails)
            total_fetched += len(emails)

            logger.info(
                f"📧 Page {page_count}: {len(emails)} emails (total: {total_fetched})"
            )

            # Add delay to be respectful to the API
            import time

            time.sleep(0.05)  # 50ms delay

        logger.info(
            f"✅ Fetch complete: {len(all_emails)} emails from {page_count} pages"
        )

        # Process and store emails
        if all_emails:
            conn = connect_to_rds()
            cursor = conn.cursor()

            processed_count = 0
            duplicate_count = 0
            error_count = 0

            logger.info(f"🔄 Processing {len(all_emails)} emails...")

            for i, email_data in enumerate(all_emails):
                try:
                    if i % 200 == 0:  # Log progress every 200 emails
                        logger.info(
                            f"📧 Processing email {i+1}/{len(all_emails)} ({processed_count} processed, {duplicate_count} duplicates)"
                        )

                    email_id = email_data.get("id")

                    # Check if already exists
                    if email_id in MESSAGES:
                        duplicate_count += 1
                        continue

                    # Extract email details
                    from_name = (
                        email_data.get("sender", {})
                        .get("emailAddress", {})
                        .get("name", "")
                    )
                    to_recipients = email_data.get("toRecipients", [])

                    body_content = email_data.get("body", {}).get("content", "")
                    internet_message_id = email_data.get("internetMessageId")

                    # Parse HTML to plain text
                    soup = BeautifulSoup(body_content, "html.parser")
                    plain_text = soup.get_text().strip()

                    subject = email_data.get("subject", "")
                    sent_time = email_data.get("sentDateTime")
                    received_time = email_data.get("receivedDateTime")

                    from_address = (
                        email_data.get("from", {})
                        .get("emailAddress", {})
                        .get("address", "")
                    )
                    direction = (
                        "inbound"
                        if from_address.lower() != user_email.lower()
                        else "outbound"
                    )

                    to_email = ""
                    to_name = ""
                    if to_recipients:
                        to_email = to_recipients[0]["emailAddress"]["address"]
                        to_name = to_recipients[0]["emailAddress"]["name"]

                    conversation_id = email_data.get("conversationId") or (
                        from_address if direction == "inbound" else to_email
                    )
                    message_id = str(uuid.uuid4())

                    # Store in MESSAGES
                    MESSAGES[email_id] = {
                        "id": message_id,
                        "from": from_name,
                        "to": to_name,
                        "to_email": to_email,
                        "from_email": from_address,
                        "body": plain_text,
                        "subject": subject,
                        "timestamp": sent_time or received_time,
                        "received_time": received_time,
                        "sent_time": sent_time,
                        "status": "received" if direction == "inbound" else "sent",
                        "source": "outlook",
                        "direction": direction,
                        "conversation_id": conversation_id,
                        "internet_message_id": internet_message_id,
                        "has_attachments": email_data.get("hasAttachments", False),
                        "importance": email_data.get("importance", "normal"),
                        "is_draft": email_data.get("isDraft", False),
                    }

                    # Save to database
                    save_outlook_message_to_db(
                        cursor,
                        user_id,
                        message_id,
                        from_address,
                        plain_text,
                        subject,
                        sent_time or received_time,
                        direction,
                        conversation_id,
                    )
                    processed_count += 1

                except Exception as e:
                    error_count += 1
                    logger.error(
                        f"❌ Error processing email {email_data.get('id', 'unknown')}: {str(e)}"
                    )
                    continue

            conn.commit()
            cursor.close()
            conn.close()

            logger.info(
                f"✅ Processing complete: {processed_count} new, {duplicate_count} duplicates, {error_count} errors"
            )

            return jsonify(
                {
                    "status": "success",
                    "user_id": user_id,
                    "months": months,
                    "pages_fetched": page_count,
                    "total_fetched": len(all_emails),
                    "processed_count": processed_count,
                    "duplicate_count": duplicate_count,
                    "error_count": error_count,
                    "total_stored": len(
                        [
                            k
                            for k in MESSAGES.keys()
                            if MESSAGES[k].get("source") == "outlook"
                        ]
                    ),
                    "message": f"Successfully fetched {len(all_emails)} emails across {page_count} pages. Processed {processed_count} new emails.",
                }
            )
        else:
            logger.info("📭 No emails found in the specified date range")
            return jsonify(
                {
                    "status": "success",
                    "total_fetched": 0,
                    "processed_count": 0,
                    "message": f"No emails found in the last {months} months",
                }
            )

    except Exception as e:
        logger.error(f"❌ Error in microsoft_fetch_all_emails: {str(e)}")
        logger.error(f"❌ Traceback: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


@microsoft_bp.route("/microsoft/trigger_email_fetch", methods=["POST"])
async def trigger_email_fetch():
    """Manually trigger email fetching for a user"""
    try:
        data = request.get_json() or {}
        user_id = data.get("user_id")
        months = data.get("months", 3)
        max_messages = data.get("max_messages", 1000)

        # Try to get user_id from session if not provided
        if not user_id:
            user_info = session.get("user", {})
            user_id = user_info.get("id")

        if not user_id:
            return jsonify({"error": "User ID is required"}), 400

        logger.info(f"🔄 Manual trigger for email fetch: {user_id}")

        # Start background fetch
        result = await fetch_outlook_emails_batch(
            user_id=user_id, months=months, max_messages=max_messages
        )

        return (
            jsonify(
                {
                    "status": "success" if result["status"] == "success" else "error",
                    "message": "Email fetch completed",
                    "user_id": user_id,
                    "result": result,
                }
            ),
            200,
        )

    except Exception as e:
        logger.error(f"❌ Error in manual trigger: {str(e)}")
        return jsonify({"error": str(e)}), 500


# async def list_messages(self, start_date=None, end_date=None):
#     """
#     Fetch messages for the user, optionally filtered by date range.
#     Returns a list of message dicts from Outlook API.
#     """

#     headers = {"Authorization": f"Bearer {self.access_token}"}
#     params = {"$top": 100}  # max messages per call

#     if start_date or end_date:
#         filters = []
#         if start_date:
#             filters.append(f"receivedDateTime ge {start_date}")
#         if end_date:
#             filters.append(f"receivedDateTime le {end_date}")
#         params["$filter"] = " and ".join(filters)

#     all_messages = []
#     url = self.base_url

#     while url:
#         response = requests.get(url, headers=headers, params=params)
#         if response.status_code != 200:
#             raise Exception(
#                 f"Outlook API error: {response.status_code} {response.text}"
#             )
#         data = response.json()
#         all_messages.extend(data.get("value", []))
#         url = data.get("@odata.nextLink")  # pagination
#         params = {}  # nextLink already has query params

#     return all_messages


def clean_plain_text(text):
    # remove zero-width characters, soft hyphens, etc.
    raw = re.sub(r"[\u200b\u200c\u200d\u2060\ufeff\u00ad]", "", text)

    # remove markdown links: [text](url)
    raw = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", raw)

    # remove HTML links
    raw = re.sub(r"<a\s+[^>]*>(.*?)<\/a>", r"\1", raw, flags=re.DOTALL)

    # remove bare URLs
    raw = re.sub(r"https?://\S+", "", raw)

    # collapse multiple spaces/newlines
    raw = re.sub(r"\s+\n", "\n", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)

    # remove bare URLs
    raw = re.sub(r"https?://\S+", "", raw)

    # collapse crazy spacing
    raw = re.sub(r"[ \t]{2,}", " ", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)

    return raw.strip()


# -------------- related to saving outlook messages ----------#


def store_outlook_emails_in_db(user_id: str, messages: list) -> dict:
    """
    Store Outlook emails in database with duplicate checking
    Returns: {"status": "success"/"error", "stored_count": N, "skipped_count": N, "error": "..."}
    """
    logger.info(
        f"🔄 Starting to store {len(messages)} Outlook messages for user {user_id}"
    )

    if not messages:
        logger.warning("⚠️ No messages to store")
        return {"status": "success", "stored_count": 0, "skipped_count": 0}

    stored_count = 0
    skipped_count = 0
    error_count = 0

    try:
        conn = connect_to_rds()
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        for message in messages:
            try:
                message_id = message.get("id")
                internet_message_id = message.get("internet_message_id", "")

                # Check if message already exists
                cursor.execute(
                    "SELECT 1 FROM messages WHERE message_id = %s OR internet_message_id = %s",
                    (message_id, internet_message_id),
                )
                if cursor.fetchone():
                    logger.debug(f"⏭️  Skipping duplicate message: {message_id}")
                    skipped_count += 1
                    continue

                # Extract message data
                conversation_id = message.get("conversation_id", str(uuid.uuid4()))
                from_email = message.get("from_email", "")
                to_email = message.get("to_email", "")
                from_name = message.get("from_name", message.get("from", ""))
                to_name = message.get("to_name", message.get("to", ""))
                subject = message.get("subject", "")
                body = message.get("body", "")
                timestamp = message.get(
                    "timestamp", datetime.now(timezone.utc).isoformat()
                )
                direction = message.get("direction", "inbound")
                has_attachments = message.get("has_attachments", False)
                importance = message.get("importance", "normal")

                # Determine participant
                if direction == "inbound":
                    participant_email = from_email
                    participant_name = from_name
                else:
                    participant_email = to_email
                    participant_name = to_name

                # Get or create user client
                client_id = None
                try:
                    cursor.execute(
                        "SELECT users_clients_id FROM users_clients WHERE email_id = %s AND user_id_fk = %s",
                        (participant_email, user_id),
                    )
                    client_row = cursor.fetchone()
                    if client_row:
                        client_id = client_row["users_clients_id"]
                    else:
                        # Create new contact if not exists
                        client_id = str(uuid.uuid4())
                        cursor.execute(
                            """
                            INSERT INTO users_clients (users_clients_id, user_id_fk, first_name, last_name, email_id, created_in)
                            VALUES (%s, %s, %s, %s, %s, NOW())
                        """,
                            (
                                client_id,
                                user_id,
                                participant_name or "",
                                "",
                                participant_email,
                            ),
                        )
                        logger.debug(f"✅ Created new contact: {participant_email}")
                except Exception as e:
                    logger.warning(
                        f"⚠️ Could not get/create client for {participant_email}: {str(e)}"
                    )
                    client_id = str(uuid.uuid4())

                # Insert message
                cursor.execute(
                    """
                    INSERT INTO messages (
                        message_id, user_id_fk, client_id_fk, from_address, to_address,
                        from_name, to_name, subject, body, timestamp, direction,
                        source, conversation_id, internet_message_id, has_attachments,
                        importance, created_in
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                """,
                    (
                        message_id,
                        user_id,
                        client_id,
                        from_email,
                        to_email,
                        from_name,
                        to_name,
                        subject,
                        body,
                        timestamp,
                        direction,
                        "outlook",
                        conversation_id,
                        internet_message_id,
                        has_attachments,
                        importance,
                    ),
                )

                stored_count += 1
                logger.debug(f"✅ Stored message: {message_id}")

            except Exception as e:
                logger.error(
                    f"❌ Error storing message {message.get('id', 'unknown')}: {str(e)}"
                )
                error_count += 1
                continue

        conn.commit()
        conn.close()

        logger.info(
            f"✅ Storage complete - Stored: {stored_count}, Skipped: {skipped_count}, Errors: {error_count}"
        )
        return {
            "status": "success",
            "stored_count": stored_count,
            "skipped_count": skipped_count,
            "error_count": error_count,
        }

    except Exception as e:
        logger.error(f"❌ Database error in store_outlook_emails_in_db: {str(e)}")
        return {"status": "error", "error": str(e), "stored_count": 0}


def save_outlook_message_to_db(
    cursor,
    user_id,
    message_id,
    from_address,
    message_content,
    subject,
    timestamp,
    direction,
    conversation_id,
):
    """Save Outlook message to database in same format as Gmail"""
    try:
        from datetime import datetime, timezone
        from utils.s3_utils import upload_any_file
        import json
        import os

        # Create message data in same format as Gmail
        message_data = {
            "id": message_id,
            "from_email": from_address,
            "content": message_content,
            "subject": subject,
            "timestamp": timestamp,
            "direction": direction,
            "source": "outlook",
            "conversation_id": conversation_id,
        }

        # Create S3 content reference (same pattern as Gmail)
        timestamp_obj = datetime.now(timezone.utc)

        # For now, store basic info - can be enhanced later to match Gmail's full S3 storage
        content_ref = f"outlook/{message_id}.json"

        # Insert into messages table (same as Gmail does)
        cursor.execute(
            """
            INSERT INTO messages (
                message_id, conversation_id_fk, sender_id, content_ref,
                message_type, is_summary, created_at, update_at, sender_type
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE update_at = VALUES(update_at)
        """,
            (
                message_id,
                conversation_id,
                from_address,  # Use email as sender_id for now
                content_ref,
                direction,
                subject,
                timestamp_obj,
                timestamp_obj,
                "outlook",
            ),
        )

        logger.info(f"💾 Saved Outlook message {message_id} to database")

    except Exception as e:
        logger.error(f"❌ Error saving Outlook message to DB: {str(e)}")


# --------------- related to sending outlook mails ----------#


def send_outlook_email(to_email, subject, body_text, from_user_email, conversation_id):

    # print("Sending Outlook email...")
    # print(f"To: {to_email}, Subject: {subject}, Body: {body_text}")

    # email = session.get("user", {}).get("email")  # remove after session is working
    email = from_user_email
    if not email:
        raise Exception("No user email found in session.")

    # Fetch token
    conn = connect_to_rds()
    cursor = conn.cursor()
    cursor.execute("SELECT token FROM users WHERE email = %s", (email,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if not row:
        raise Exception("Access token not found for user.")

    access_token = row[0]

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body_text},
            "toRecipients": [{"emailAddress": {"address": to_email}}],
        },
        "saveToSentItems": "true",
    }

    if from_user_email:
        url = f"https://graph.microsoft.com/v1.0/users/{from_user_email}/sendMail"
        # print(f"Using from_user_email: {from_user_email}")
    else:
        url = "https://graph.microsoft.com/v1.0/me/sendMail"
    # print("Using current user's email for sending.")

    # Send mail
    response = requests.post(url, headers=headers, json=payload)

    if not response.ok:
        # print("Graph API returned an error!")
        # print("Status:", response.status_code)
        # print("Response text:", response.text)
        response.raise_for_status()
        response.raise_for_status()

    # Fetch last sent message to get conversationId
    if from_user_email:
        sent_items_url = f"https://graph.microsoft.com/v1.0/users/{from_user_email}/mailFolders/sentitems/messages?$orderby=sentDateTime desc&$top=1"
    else:
        sent_items_url = "https://graph.microsoft.com/v1.0/me/mailFolders/sentitems/messages?$orderby=sentDateTime desc&$top=1"
    sent_response = requests.get(sent_items_url, headers=headers)
    sent_response.raise_for_status()

    sent_data = sent_response.json()
    if sent_data.get("value"):
        last_msg = sent_data["value"][0]

        # conversation_id = last_msg.get("conversationId")
        real_message_id = last_msg.get("id")
        timestamp = last_msg.get("sentDateTime")
        subject_saved = last_msg.get("subject", subject)
        body_saved = last_msg.get("body", {}).get("content", body_text)
        message_id = str(uuid.uuid4())

        # Save the message to MESSAGES
        MESSAGES[real_message_id] = {
            "id": message_id,
            "from": email,
            "to": to_email,
            "body": body_saved,
            "subject": subject_saved,
            "timestamp": timestamp,
            "status": "sent",
            "source": "outlook",
            "direction": "outbound",
            "user_id": email,
            "conversation_id": conversation_id,
        }

        # print(
        #     f"✅ Saved Outlook message. Conversation ID: {conversation_id} messages:{MESSAGES[real_message_id]}"
        # )

        return {"id": message_id, "conversation_id": conversation_id}

    else:
        # fallback if no message found
        fallback_id = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        MESSAGES[fallback_id] = {
            "id": fallback_id,
            "from": email,
            "to": to_email,
            "body": body_text,
            "subject": subject,
            "timestamp": timestamp,
            "status": "sent",
            "source": "outlook",
            "direction": "outbound",
            "user_id": email,
            "conversation_id": None,
            "message_id": fallback_id,
        }

        # print(
        #     f"⚠️ Could not find sent mail. Saved fallback message with no conversation ID."
        # )
        return {"id": fallback_id}


@microsoft_bp.route("/microsoft/send_mail", methods=["POST"])
def send_mail_microsoft():

    user = session.get("user", {})
    email = user.get("email")

    if not email:
        return redirect(f"{os.getenv('BASE_FRNT_URL')}/login")

    #################################
    # data = request.get_json()
    # to = data.get("to")
    # subject = data.get("subject")
    # body = data.get("body")
    #################################

    to = "test@example.com"
    subject = "subject"
    body = "body"

    if not to or not subject or not body:
        return jsonify({"error": "Missing 'to', 'subject', or 'body'"}), 400

    try:
        conn = connect_to_rds()
        cursor = conn.cursor()
        cursor.execute("SELECT token FROM users WHERE email = %s", (email,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if not row:
            return jsonify({"error": "Access token not found"}), 404

        access_token = row[0]

        send_payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": body},
                "toRecipients": [{"emailAddress": {"address": to}}],
            },
            "saveToSentItems": True,
        }

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        response = requests.post(
            "https://graph.microsoft.com/v1.0/me/sendMail",
            headers=headers,
            json=send_payload,
        )

        if response.status_code == 202:
            return jsonify({"message": "Email sent"}), 200
        else:
            return (
                jsonify({"error": "Failed to send email", "details": response.text}),
                response.status_code,
            )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


def outlook_send_mail(
    user_id,
    to,
    subject,
    body_text,
    thread_id=None,
    in_reply_to=None,
    attachments=None,
    cc=None,
    bcc=None,
    integration=None,
    outlook_integration=None,
):
    """
    Sends an Outlook email using Microsoft Graph.
    Matches gmail_reply() signature and behavior.
    """

    # ---------------------------
    # 1. Fetch OAuth Tokens From DB
    # ---------------------------
    conn = connect_to_rds()
    cursor = conn.cursor()

    if integration:
        if outlook_integration:
            cursor.execute(
                """
                SELECT user_id,access_token, refresh_token, email
                FROM integrations
                WHERE primary_user_id_fk = %s
                AND platform = 'microsoft'
                AND status = 'active'
                LIMIT 1;
            """,
                (user_id,),
            )
            row = cursor.fetchone()
            if not row:
                raise Exception("No active Microsoft integration found.")

        else:
            cursor.execute(
                """
                SELECT user_id,access_token, refresh_token, email
                FROM integrations
                WHERE user_id = %s
                AND platform = 'microsoft'
                AND status = 'active'
                LIMIT 1;
            """,
                (user_id,),
            )
            row = cursor.fetchone()
            if not row:
                raise Exception("No active Microsoft integration found.")

        integration_user_id, access_token, refresh_token, mail = row

    else:
        cursor.execute(
            """
            SELECT token, refresh_token, email
            FROM users
            WHERE user_id = %s;
        """,
            (user_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise Exception("No active Microsoft user found.")
        access_token, refresh_token, mail = row

    # sender_email = row["email"]  # Outlook email ID

    # ---------------------------
    # 2. Build Message Body
    # ---------------------------
    message = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body_text},
            "toRecipients": [{"emailAddress": {"address": to}}],
        },
        "saveToSentItems": "true",
    }

    # CC
    if cc:
        message["message"]["ccRecipients"] = [
            {"emailAddress": {"address": c}} for c in cc
        ]

    # BCC
    if bcc:
        message["message"]["bccRecipients"] = [
            {"emailAddress": {"address": b}} for b in bcc
        ]

    # Threading / Reply
    if in_reply_to:
        message["message"]["internetMessageHeaders"] = [
            {"name": "In-Reply-To", "value": in_reply_to},
            {"name": "References", "value": in_reply_to},
        ]

    # Conversation (thread_id)
    if thread_id:
        message["message"]["conversationId"] = thread_id

    # ---------------------------
    # 3. Attachments
    # ---------------------------
    if attachments:
        formatted = []
        for file in attachments:
            formatted.append(
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": file["filename"],
                    "contentBytes": base64.b64encode(file["content"]).decode("utf-8"),
                }
            )
        message["message"]["attachments"] = formatted

    # ---------------------------
    # 4. Hit Microsoft Send API
    # ---------------------------

    # check if tokens are expired. If expired, refresh them
    if integration:
        expired = check_microsoft_token_expiry(cursor, user_id)
        if expired:
            resp = refresh_expired_microsoft_tokens_for_integrations(
                refresh_token, cursor, conn, None, integration_user_id
            )
            data = resp.get_json()
            access_token = data["token"]

    else:
        expired, access_token = check_microsoft_token_expiry_normal(
            cursor, conn, user_id
        )

    cursor.close()
    conn.close()

    url = "https://graph.microsoft.com/v1.0/me/sendMail"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    # print(f"message at line 1729 :{message}")

    res = requests.post(url, json=message, headers=headers)

    if res.status_code not in [200, 202]:
        # print("Outlook send error:", res.text)
        raise Exception("Failed to send Outlook message.")

    # Outlook does NOT return message ID.
    # You must fetch latest sent messages if you want ID.
    # print(f"----success :{res}")
    return {"status": "sent", "conversation_id": thread_id}


# ---------------- related to onedrive ----------------------- #


def GetOutlookDriveService(access_token):
    import requests

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {access_token}"})

    return session


def download_onedrive_file(session, file_id, local_path):
    url = f"https://graph.microsoft.com/v1.0/me/drive/items/{file_id}/content"

    response = session.get(url, stream=True)
    # print(f"reponse code from url :{response.status_code}")

    if response.status_code != 200:
        return False

    with open(local_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=2048):
            if chunk:
                f.write(chunk)
    return True


@microsoft_bp.route("/process-outlook", methods=["POST"])
def process_outlook():
    try:
        data = request.json
        if not data or "files" not in data or not isinstance(data["files"], list):
            return (
                jsonify(
                    {"error": "Invalid payload. Expected JSON with 'files' array."}
                ),
                400,
            )

        if len(data["files"]) == 0:
            return jsonify({"error": "No files picked"}), 400

        apikey = data.get("api_key")
        id = data.get("user_id")
        primary_provider = data.get("primary_provider")

        # print("----------------------------")
        # print(f"primary_provider:{primary_provider}")
        # print(f"id: {id}")
        # print("----------------------------")

        if primary_provider:
            access_token = get_outlook_token(id)
            userid = id
        else:
            # fetch from integrations table
            access_token, userid = get_integration_access_token(id, "microsoft")

        # if user signed in through microsoft and selected file from onedrive, userid = id
        # if user signed in through google and selected file from onedrive, then -
        #  - userid is microsoft userid,  id is google user id. (id of the primary user is "id")

        # print("--------------------------")
        # print(f"userid:{userid}")
        # print(f"id:{id}")
        # print("--------------------------")

        def event_stream():
            yield "event: start\ndata: Starting Outlook file processing...\n\n"

            graph_client = GetOutlookDriveService(access_token)
            if not graph_client:
                yield "event: error\ndata: Unable to initialize Microsoft Graph service\n\n"
                return

            # Step 1 — Download OneDrive/SharePoint files
            downloaded_paths = []
            total = len(data["files"])

            for index, file in enumerate(data["files"], start=1):
                try:
                    file_id = file["id"]
                    file_name = file.get("name", f"file_{index}")
                    local_path = os.path.join("data", file_name)
                    # print(f"file_id:{file_id}")
                    # print(f"file_name:{file_name}")
                    # print(f"local_path:{local_path}")

                    success = download_onedrive_file(graph_client, file_id, local_path)
                    if not success:
                        yield f"event: error\ndata: Failed to download {file_name}\n\n"
                        return

                    downloaded_paths.append(local_path)
                    yield f"event: progress\ndata: Downloaded {index}/{total}: {file_name}\n\n"

                except Exception as e:
                    yield f"event: error\ndata: Error downloading file: {str(e)}\n\n"
                    return

            if not downloaded_paths:
                yield "event: error\ndata: No files downloaded\n\n"
                return

            # Step 2 — Process files (your existing pipeline)
            folderpath = os.path.commonpath(downloaded_paths)
            try:
                all_file_data = asyncio.run(
                    process_and_update_yaml(
                        all_downloaded_paths=downloaded_paths,
                        userid=id,
                        provider="microsoft",
                        folderpath=folderpath,
                    )
                )
            except Exception as e:
                print(f"error in process_outlook:{e} ")

            yield (
                "event: complete\ndata: "
                + json.dumps(
                    {
                        "message": "Successfully processed OneDrive files",
                        "files": all_file_data,
                    }
                )
                + "\n\n"
            )

        return Response(
            stream_with_context(event_stream()), mimetype="text/event-stream"
        )

    except Exception as e:
        return jsonify({"error": f"Unexpected Outlook processing error: {str(e)}"}), 500


# ---------------- logout related -------------------------- #


@microsoft_bp.route("/logout")
def microsoft_logout():
    user = session.get("user")

    if not user:
        return jsonify({"error": "No user is currently logged in"}), 400

    user_id = user.get("id")
    session.pop("user", None)
    session.pop("user_id", None)

    return jsonify({"status": "User logged out", "user_id": user_id}), 200


# ------------------- others -------------------------------- #


@microsoft_bp.route("/microsoft/sent_items", methods=["GET"])
def microsoft_sent_items():
    try:
        email = session.get("user", {}).get("email")
        if not email:
            return redirect(f"{os.getenv('BASE_FRNT_URL')}/login")

        conn = connect_to_rds()
        cursor = conn.cursor()
        cursor.execute("SELECT token FROM users WHERE email = %s", (email,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if not row:
            return jsonify({"error": "Access token not found"}), 404

        access_token = row[0]
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        drafts_url = (
            "https://graph.microsoft.com/v1.0/me/mailFolders/SentItems/messages"
        )
        response = requests.get(drafts_url, headers=headers)

        if response.status_code == 200:
            emails = response.json().get("value", [])
            for email_data in emails:
                email_id = email_data.get("id")
                from_name = (
                    email_data.get("sender", {}).get("emailAddress", {}).get("name")
                )
                to_recipients = email_data.get("toRecipients", [])
                to_name = (
                    to_recipients[0]["emailAddress"]["name"] if to_recipients else None
                )
                body_content = email_data.get("body", {}).get("content")
                subject = email_data.get("subject")
                sent_time = email_data.get("sentDateTime")

                message_id = str(uuid.uuid4())
                MESSAGES[email_id] = {
                    "id": message_id,
                    "from": from_name,
                    "to": to_name,
                    "body": body_content,
                    "subject": subject,
                    "timestamp": sent_time,
                    "status": "sent",
                    "source": "outlook",
                    "direction": "outbound",
                }
            return jsonify({"sent_items": emails}), 200
        else:
            return (
                jsonify(
                    {
                        "error": "Failed to fetch sent items",
                        "details": response.text,
                    }
                ),
                response.status_code,
            )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@microsoft_bp.route("/microsoft/drafts", methods=["GET"])
def microsoft_list_drafts():
    try:
        email = session.get("user", {}).get("email")
        if not email:
            return redirect(f"{os.getenv('BASE_FRNT_URL')}/login")

        conn = connect_to_rds()
        cursor = conn.cursor()
        cursor.execute("SELECT token FROM users WHERE email = %s", (email,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if not row:
            return jsonify({"error": "Access token not found"}), 404

        access_token = row[0]
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        drafts_url = "https://graph.microsoft.com/v1.0/me/mailFolders/drafts/messages"
        response = requests.get(drafts_url, headers=headers)

        if response.status_code == 200:
            return jsonify({"drafts": response.json().get("value", [])}), 200
        else:
            return (
                jsonify({"error": "Failed to fetch drafts", "details": response.text}),
                response.status_code,
            )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@microsoft_bp.route("/microsoft/spam", methods=["GET"])
def microsoft_list_spam():
    try:
        email = session.get("user", {}).get("email")
        if not email:
            return redirect(f"{os.getenv('BASE_FRNT_URL')}/login")

        conn = connect_to_rds()
        cursor = conn.cursor()
        cursor.execute("SELECT token FROM users WHERE email = %s", (email,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if not row:
            return jsonify({"error": "Access token not found"}), 404

        access_token = row[0]
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        drafts_url = (
            "https://graph.microsoft.com/v1.0/me/mailFolders/JunkEmail/messages"
        )
        response = requests.get(drafts_url, headers=headers)

        if response.status_code == 200:
            return jsonify({"spam": response.json().get("value", [])}), 200
        else:
            return (
                jsonify({"error": "Failed to fetch spam", "details": response.text}),
                response.status_code,
            )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@microsoft_bp.route("/microsoft/trash", methods=["GET"])
def microsoft_list_trash():
    try:
        email = session.get("user", {}).get("email")
        if not email:
            return redirect(f"{os.getenv('BASE_FRNT_URL')}/login")

        conn = connect_to_rds()
        cursor = conn.cursor()
        cursor.execute("SELECT token FROM users WHERE email = %s", (email,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if not row:
            return jsonify({"error": "Access token not found"}), 404

        access_token = row[0]
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        drafts_url = (
            "https://graph.microsoft.com/v1.0/me/mailFolders/DeletedItems/messages"
        )
        response = requests.get(drafts_url, headers=headers)

        if response.status_code == 200:
            return jsonify({"trash": response.json().get("value", [])}), 200
        else:
            return (
                jsonify({"error": "Failed to fetch trash", "details": response.text}),
                response.status_code,
            )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@microsoft_bp.route("/auth/microsoft/token", methods=["POST"])
def get_outlook_token(inuser=None, value=None, in_connection=None):
    """
    Microsoft token fetch & refresh — mirrors Gmail get_token() behavior
    """
    if inuser:
        user_id = inuser
    else:
        data = request.json
        user_id = (
            session.get("user_id")
            or session.get("userState_id")
            or inuser
            or data.get("userid")
        )
    if not in_connection:
        connection = connect_to_rds()
    else:
        connection = in_connection

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT client_id, client_secret, token, refresh_token, expiry
                FROM users
                WHERE user_id = %s AND social = 'microsoft'
            """,
                (str(user_id),),
            )
            row = cursor.fetchone()

            if not row:
                return jsonify({"error": "Microsoft user not found"}), 404

            client_id, client_secret, token, refresh_token, expiry = row

            # Convert expiry from string if needed
            if isinstance(expiry, str):
                expiry = datetime.fromisoformat(expiry)

            time_to_expiry = expiry - datetime.now()

            # Refresh if expiring soon (same 10 min rule as Google)
            if expiry <= datetime.now() or time_to_expiry <= timedelta(minutes=10):
                # print(f"** expired**")
                # new_token = refresh_expired_microsoft_tokens(
                #     client_id,
                #     client_secret,
                #     refresh_token,
                #     cursor,
                #     value,
                #     user_id,
                # )
                new_token = refresh_expired_microsoft_tokens(
                    refresh_token,
                    cursor,
                    value,
                    user_id,
                )
                return new_token

            return token

    except Exception as e:
        # print(f"Microsoft token error: {e}")
        return jsonify({"error": "Internal server error"}), 500

    finally:
        if not in_connection and connection:
            connection.close()


@microsoft_bp.route("/check-microsoft-user", methods=["POST"])
def check_microsoft_user():
    """Check if Microsoft user exists - similar to Gmail's check-user"""
    try:
        data = request.get_json()
        user_id = data.get("user_id")

        if not user_id:
            return jsonify({"error": "User ID is required"}), 400

        conn = connect_to_rds()
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        cursor.execute(
            "SELECT user_id, email, first_name, last_name FROM users WHERE user_id = %s AND social = 'microsoft'",
            (user_id,),
        )
        user = cursor.fetchone()

        cursor.close()
        conn.close()

        if user:
            return jsonify({"exists": True, "user": user}), 200
        else:
            return jsonify({"exists": False}), 200

    except Exception as e:
        # print(f"Error in check_microsoft_user: {str(e)}")
        return jsonify({"error": str(e)}), 500


# Add routes that match Gmail's pattern
@microsoft_bp.route("/get_microsoft_client_id", methods=["POST"])
def get_microsoft_client_id():
    """Get Microsoft client ID - similar to Gmail's get_google_client_id"""
    try:
        data = request.get_json()
        secretkey = data.get("secretkey", "")

        microsoft_client_id = os.getenv("MICROSOFT_CLIENT_ID")
        microsoft_tenantid = os.getenv("MICROSOFT_TENANT_ID")

        def xor_encrypt(data, key):
            """XOR encryption function"""
            return "".join(
                chr(ord(c) ^ ord(k))
                for c, k in zip(data, key * (len(data) // len(key) + 1))
            )

        if not microsoft_client_id:
            return jsonify({"error": "Missing MICROSOFT_CLIENT_ID"}), 500

        response_data = {
            "status": "success",
            "data": {
                "value": xor_encrypt(microsoft_client_id, secretkey),
                "tenant_id": microsoft_tenantid,
            },
        }

        return jsonify(response_data), 200

    except Exception as e:
        # print(f"Error in get_microsoft_client_id: {str(e)}")
        return jsonify({"error": str(e)}), 500


# ------------------  webhook --------------------------------------#


def check_and_refresh_token(user_id, cursor, conn):
    # print(f"[INFO] Checking Microsoft token for user_id: {user_id}")

    cursor.execute(
        """
        SELECT expiry
        FROM integrations
        WHERE user_id = %s AND platform = 'microsoft'
        """,
        (str(user_id),),
    )
    row = cursor.fetchone()
    # print(f"[DEBUG] Fetched expiry row: {row}")

    if not row:
        # print(f"[ERROR] Microsoft user not found for user_id: {user_id}")
        return jsonify({"error": "Microsoft user not found"}), 404

    expiry = row[0]
    # print(f"[DEBUG] Current expiry: {expiry}")

    # Convert expiry from string if needed
    if isinstance(expiry, str):
        try:
            expiry = datetime.fromisoformat(expiry)
            # print(f"[DEBUG] Converted expiry to datetime: {expiry}")
        except Exception as e:
            # print(f"[ERROR] Failed to convert expiry string to datetime: {e}")
            return False

    time_to_expiry = expiry - datetime.now()
    # print(f"[DEBUG] Time to expiry: {time_to_expiry}")

    # Refresh if expiring soon (10 min rule)
    if expiry <= datetime.now() or time_to_expiry <= timedelta(minutes=10):
        # print(f"[INFO] Token expired or about to expire, refreshing...")

        # get refresh token
        cursor.execute(
            """
            SELECT refresh_token
            FROM integrations
            WHERE user_id = %s AND platform = 'microsoft'
            """,
            (str(user_id),),
        )
        row = cursor.fetchone()
        # print(f"[DEBUG] Fetched refresh token row: {row}")

        refresh_token = row[0]
        # print(f"[DEBUG] Current refresh token: {refresh_token}")

        client_id = os.environ.get("MICROSOFT_CLIENT_ID")
        client_secret = os.environ.get("MICROSOFT_CLIENT_SECRET")
        SCOPES = [
            "User.Read",
            "Mail.Send",
            "Mail.ReadWrite",
            "Calendars.ReadWrite",
            "OnlineMeetings.ReadWrite",
            "Chat.ReadWrite",
            "Files.Read.All",
        ]

        try:
            token_url = "https://login.microsoftonline.com/common/oauth2/v2.0/token"

            payload = {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
                "scope": " ".join(SCOPES + ["offline_access"]),
            }

            # print(f"[DEBUG] Sending refresh request with payload: {payload}")
            response = requests.post(token_url, data=payload)
            # print(f"[DEBUG] Refresh response status: {response.status_code}")

            if response.status_code != 200:
                # print(f"[ERROR] Refresh failed: {response.text}")
                return redirect(f"{os.getenv('BASE_FRNT_URL')}/login")

            new_data = response.json()
            # print(f"[DEBUG] Refresh response JSON: {new_data}")

            new_token = new_data.get("access_token")
            new_refresh = new_data.get("refresh_token", refresh_token)
            expires_in = new_data.get("expires_in", 3600)
            new_expiry = datetime.now() + timedelta(seconds=expires_in)

            # print(f"[INFO] New access_token: {new_token}")
            # print(f"[INFO] New refresh_token: {new_refresh}")
            # print(f"[INFO] New expiry: {new_expiry}")

            # Store updated token
            cursor.execute(
                """
                UPDATE integrations
                SET access_token = %s, refresh_token = %s, expiry = %s
                WHERE user_id = %s
                """,
                (new_token, new_refresh, new_expiry.isoformat(), user_id),
            )
            conn.commit()
            # print("[INFO] Successfully refreshed Microsoft token")
            return True

        except Exception as e:
            # print(f"[ERROR] Microsoft token refresh failed: {e}")
            return False

    else:
        # print("[INFO] Microsoft token is valid, no refresh needed")
        return True


@microsoft_bp.route("/outlook/webhook", methods=["POST", "GET"])
async def outlook_webhook():
    validation_token = request.args.get("validationToken")
    if validation_token:
        return Response(validation_token, status=200, mimetype="text/plain")

    # Handle notifications (POST)
    notification = request.json
    for change in notification.get("value", []):
        if change.get("clientState") != "secretClientValue123":
            # print("Invalid clientState, ignoring notification")
            continue

    resource = change.get("resource", "")
    user_id = resource.split("Users/")[1].split("/Messages")[0]
    message_id = change.get("resourceData", {}).get("id")
    received_at = datetime.now(timezone.utc).isoformat()
    redis = RedisService()

    val = await redis.exists(f"user_alive:{user_id}")
    if not val:
        return "user skipped not alive", 200

    # -------------------------
    # 1. Extract historyId from Outlook webhook (equivalent to Gmail historyId)
    # -------------------------
    # Outlook doesn't send historyId, so we emulate it using message_id + etag
    history_id = change.get("resourceData", {}).get("@odata.etag")

    if not history_id:
        # fallback so dedupe still works
        history_id = f"{user_id}:{message_id}"

    # -------------------------
    # 2. DEDUP using Redis (lock_client)
    # -------------------------
    dedup_key = f"webhook_dedup:{user_id}:{history_id}"

    recent = await lock_client.get(dedup_key)
    if recent:
        logger.info(f"Duplicate webhook skipped for {user_id}, historyId={history_id}")
        return "Duplicate webhook skipped", 200

    await lock_client.set(dedup_key, "1", ex=300)  # 5-minute dedupe window

    logger.info(f"Processing webhook for {user_id}, historyId={history_id}")

    # -------------------------
    # 3. Trigger Celery Worker
    # -------------------------
    conn = connect_to_rds()
    cursor = conn.cursor()

    integration = False
    email = ""
    cursor.execute("SELECT email FROM integrations WHERE user_id=%s", (user_id,))
    row = cursor.fetchone()
    if row:
        email = row[0]
        integration = True
    else:
        cursor.execute("SELECT email FROM users WHERE user_id=%s", (user_id,))
        row = cursor.fetchone()
        if row:
            email = row[0]
        else:
            return ("User not found", 404)

    try:
        # check if it is expired and refresh if expired
        if integration:
            check_result = check_and_refresh_token(user_id, cursor, conn)  # TODO ....
            # print(f"check_result : {check_result}")
            if not check_result:
                # print("cannot refresh token")
                return ("Token refresh failed", 400)

        else:
            check_result, token = check_microsoft_token_expiry_normal(
                cursor, conn, user_id
            )
            if not check_result:
                # print("cannot refresh token")
                return ("Could not refresh token ", 401)

        delayed_trigger.delay(
            email, history_id, channel="microsoft", integration=integration
        )

    except Exception as e:
        logger.error(f"Failed to trigger delayed task for {email}: {e}")
        cursor.close()
        conn.close()

        status_data = read_status_file()
        user_info = status_data.get(user_id)
        if user_info:
            user_info["status"] = "complete"
            user_info["timestamp"] = datetime.now(timezone.utc).isoformat()
            status_data[user_id] = user_info
            # print("complete made ok")
            write_status_file(status_data)

        return ("Internal Server Error", 500)

    # -------------------------
    # 4. Append to local JSON log (your original logic)
    # -------------------------
    WEBHOOK_LOG_DIR = "data/test"
    WEBHOOK_LOG_FILE = os.path.join(WEBHOOK_LOG_DIR, "outlook_webhook_log.json")
    os.makedirs(WEBHOOK_LOG_DIR, exist_ok=True)

    # Load old logs
    if os.path.isfile(WEBHOOK_LOG_FILE):
        try:
            with open(WEBHOOK_LOG_FILE, "r") as f:
                log_data = json.load(f)
            if not isinstance(log_data, dict):
                log_data = {}
        except json.JSONDecodeError:
            log_data = {}
    else:
        log_data = {}

    # Append new entry
    new_entry = {"timestamp": received_at, "message_id": message_id}
    log_data.setdefault(email, []).append(new_entry)

    # Clean old entries > 2 days
    cutoff = datetime.now(timezone.utc) - timedelta(days=2)
    cleaned_log = {}

    for email, entries in log_data.items():
        valid_entries = []
        for e in entries:
            try:
                ts = datetime.fromisoformat(e["timestamp"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except Exception:
                continue

            if ts > cutoff:
                valid_entries.append(e)

        if valid_entries:
            cleaned_log[email] = valid_entries

    with open(WEBHOOK_LOG_FILE, "w") as f:
        json.dump(cleaned_log, f, indent=2)

    return "OK", 200


def refresh_expired_google_tokens_for_integrations(user_id, connection):
    try:
        with connection.cursor() as cursor:
            # print(
            #     f"user_id inside refresh_expired_google_tokens_for_integrations : {user_id}"
            # )
            cursor.execute(
                """
                SELECT user_id, client_id, client_secret, access_token, refresh_token, expiry
                FROM integrations
                WHERE primary_user_id_fk = %s
            """,
                (str(user_id),),
            )
            row = cursor.fetchone()

            if not row:
                return jsonify({"error": "User not found"}), 404

            google_user_id, client_id, client_secret, token, refresh_token, expiry = row

            # print("google_user_id:", google_user_id)
            # print("client_id:", client_id)
            # print("client_secret:", client_secret)
            # print("token:", token)
            # print("refresh_token:", refresh_token)
            # print("expiry:", expiry)

            # Ensure expiry is a datetime object
            if isinstance(expiry, str):
                expiry = datetime.fromisoformat(expiry)

            time_to_expiry = expiry - datetime.now()
            ##print("expiry from db",expiry)
            ##print("current time", datetime.now())

            # Refresh only if token is close to expiring
            if expiry <= datetime.now() or time_to_expiry <= timedelta(minutes=10):
                ##print("token time expired")
                try:
                    creds = Credentials(
                        token=token,
                        refresh_token=refresh_token,
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=client_id,
                        client_secret=client_secret,
                        # scopes=[
                        #     "https://www.googleapis.com/auth/userinfo.profile",
                        #     "https://www.googleapis.com/auth/userinfo.email",
                        #     "https://www.googleapis.com/auth/gmail.readonly",
                        #     "https://www.googleapis.com/auth/gmail.send",
                        #     "https://www.googleapis.com/auth/gmail.modify",
                        #     "https://www.googleapis.com/auth/gmail.compose",
                        #     "https://www.googleapis.com/auth/drive.metadata.readonly",
                        #     "https://www.googleapis.com/auth/drive",
                        #     "https://www.googleapis.com/auth/calendar",
                        #     "https://www.googleapis.com/auth/contacts",
                        #     "openid",
                        # ],
                        scopes=(
                            # Identity
                            "openid",
                            "https://www.googleapis.com/auth/userinfo.profile",
                            "https://www.googleapis.com/auth/userinfo.email",
                            # Gmail – FULL access
                            "https://www.googleapis.com/auth/gmail.readonly",
                            "https://www.googleapis.com/auth/gmail.send",
                            "https://www.googleapis.com/auth/gmail.modify",
                            "https://www.googleapis.com/auth/gmail.compose",
                            # Drive – READ ONLY
                            "https://www.googleapis.com/auth/drive",
                            "https://www.googleapis.com/auth/drive.metadata.readonly",
                            # Calendar – READ ONLY
                            "https://www.googleapis.com/auth/calendar",
                            # Contacts – READ ONLY
                            "https://www.googleapis.com/auth/contacts.readonly",
                        ),
                    )

                    creds.refresh(g_request())
                    # print("refresh started")

                    # Save refreshed token and new expiry time
                    cursor.execute(
                        """
                        UPDATE integrations SET access_token = %s, expiry = %s WHERE primary_user_id_fk = %s
                    """,
                        (creds.token, creds.expiry.isoformat(), user_id),
                    )
                    connection.commit()
                    # print(f"expired and refreshed")
                    return google_user_id

                except Exception as e:
                    # print(f"Token refresh failed: {e}")
                    return redirect(f"{os.getenv('BASE_FRNT_URL')}/login")

            # Return existing token if not refreshed
            cursor.execute(
                "SELECT user_id FROM integrations WHERE primary_user_id_fk = %s",
                (user_id,),
            )
            user_row = cursor.fetchone()
            # print(f"google not expired")

            if user_row is None:
                return jsonify({"error": "Token missing after fallback"}), 400
            ##print("returning token", user_row[0])

            return user_row[0]

    except Exception as e:
        # print(f"Error occurred: {e}")
        return jsonify({"error": "Internal server error"}), 500
