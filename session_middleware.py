from datetime import datetime, timezone
import asyncio
from flask import request, redirect, make_response, g
from session_manager_route.session_redis import  get_session, delete_all_session_cookies, validate_and_refresh_tokens
from utils.base_logger import get_logger
from functools import wraps

logger = get_logger(__name__)


EXEMPT_PATHS = [
    "/generate_session",
    "/get_google_client_id",
    "/google_login",
    "/login",
    "/oauth2callback",
    "/browser_url",
    "/check-user",
    "/get_user_permissions",
]


def debug_request_cookies():
    """Debug function to log all request information"""
    logger.info("=== REQUEST DEBUG INFO ===")
    logger.info("Request URL: %s", request.url)
    logger.info("Request Host: %s", request.host)
    logger.info("Request Path: %s", request.path)
    logger.info("Request Method: %s", request.method)
    logger.info("Request Headers: %s", dict(request.headers))
    logger.info("All Cookies: %s", dict(request.cookies))
    logger.info("Cookie Names: %s", list(request.cookies.keys()))
    
    # Check specific cookies
    session_id = request.cookies.get("session_id")
    access_token = request.cookies.get("access_token")
    refresh_token = request.cookies.get("refresh_token")
    
    logger.info("session_id cookie: %s", session_id)
    logger.info("access_token cookie: %s", access_token)
    logger.info("refresh_token cookie: %s", refresh_token)
    logger.info("=== END DEBUG INFO ===")


# def register_session_check(app):
#     @app.before_request
#     def session_check():

def session_check(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if request.method == "OPTIONS" or request.path in EXEMPT_PATHS:
            return f(*args, **kwargs)
            # return None
        
        logger.info("inside check_session" )

        session_hash = request.cookies.get("session_id")
        access_token = request.cookies.get("access_token") or request.headers.get("Authorization")

        if not session_hash or not access_token:
            logger.warning("NO session_hash or access_token " )
            return redirect("https://www.bytoid.ai/login")

        session, key_str = asyncio.run(get_session(session_hash, request))
        if not session:
            logger.warning("NO session " )
            asyncio.run(delete_all_session_cookies(key_str))
            return redirect("https://www.bytoid.ai/login")

        client_ip = request.remote_addr
        client_ua = request.headers.get("User-Agent")

        if session["ip"] != client_ip or session["user_agent"] != client_ua:
            logger.warning("client_ip or ua not same" )
            asyncio.run(delete_all_session_cookies(key_str))
            return redirect("https://www.bytoid.ai/login")

        now = datetime.now(timezone.utc)        
        expiry_str = session["session_expiry"]
        expiry = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))

        if expiry < now:
            logger.warning("session expired " )
            asyncio.run(delete_all_session_cookies(key_str))
            return redirect("https://www.bytoid.ai/login")

        is_valid, new_tokens = asyncio.run(validate_and_refresh_tokens(session_hash, session, access_token, key_str))
        if not is_valid:
            logger.warning("Token validation failed")
            return redirect("https://www.bytoid.ai/login")
        
        # request.user_id = session["user_id"]
        # g.user_id = session["user_id"]
        # g.session_data = session
        # g.new_tokens = new_tokens

        logger.info("Session validation successful" )
        response = f(*args, **kwargs)
        
        # return None

    # @app.after_request
    # def after_request(response):
    #     # Handle token refresh after the main request is processed
    #     if hasattr(g, 'new_tokens') and g.new_tokens:
    #         logger.info("Setting new access token in cookie")
    #         response.set_cookie(
    #             "access_token",
    #             g.new_tokens['access_token'],
    #             httponly=True,
    #             secure=True,
    #             samesite='None',
    #             max_age=60*60*24*7
    #         )
    #     return response
        # If new tokens were generated, update cookies
        if new_tokens:
            logger.info("Setting new access token in cookie")
            if hasattr(response, 'set_cookie'):
                # Response is a Flask Response object
                response.set_cookie(
                    "access_token", 
                    new_tokens['access_token'],
                    httponly=True,
                    secure=True,
                    samesite='None',
                    max_age=60*60*24*7
                )
            else:
                # Convert to Response object and set cookie
                response = make_response(response)
                response.set_cookie(
                    "access_token", 
                    new_tokens['access_token'],
                    httponly=True,
                    secure=True,
                    samesite='None',
                    max_age=60*60*24*7
                )
        
        return response
    
    return wrapper


