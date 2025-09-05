from flask import Blueprint, request, jsonify, make_response, session, redirect
from functools import wraps
from create_db import connect_to_rds
from datetime import datetime
import os
from dotenv import load_dotenv
from session_manager_route.session_redis import session_login_redis, get_session, delete_all_session_cookies
load_dotenv()
import asyncio
from glide import GlideClusterClient, GlideClusterClientConfiguration, NodeAddress
from utils.base_logger import get_logger



session_bp = Blueprint("session", __name__)

logger = get_logger(__name__)

# @session_bp.route("/session_login/<user_id>", methods=["POST"])
def session_login(user_id):

    request_data = {
        "remote_addr": request.remote_addr,
        "user_agent": request.headers.get("User-Agent"),
    }
    result = asyncio.run(session_login_redis(user_id, request_data ))
    session_id = result.get("session_id")
    access_token = result.get("access_token")
    refresh_token = result.get("refresh_token")
    
    return session_id, access_token, refresh_token



@session_bp.route("/delete_session", methods=["POST"])
def logout():
    session_hash = request.cookies.get("session_id")
    session, key_str = asyncio.run(get_session(session_hash, request))

    delete_status = asyncio.run(delete_all_session_cookies(key_str))
    if delete_status:
        return jsonify({"message": "Logged out!"})
    return jsonify({"message": "Could not find session to logout"})

@session_bp.route('/test-redirect')
def test_redirect():
    logger.info("Test redirect triggered")
    return redirect("https://www.bytoid.ai/login")

# @session_bp.before_request
# def validate_cookie_session():
#     if request.endpoint in EXEMPT_PATHS or request.endpoint is None:
#         return
#     session_id = request.cookies.get("session_id")
#     print("session id", session_id)
#     if session_id:
#         if not session_manager.is_session_valid(session_id):
#             # Invalidate expired session
#             session_manager.delete_session(session_id)
#             response = make_response({"error": "Session expired"}, 440)
#             response.delete_cookie("session_id")
#             return response
#         else:
#             session_manager.update_session_timestamp(session_id)


# @session_bp.route("/session_exists", methods=["POST"])
# def session_exists():
#     print("Received request:", request.cookies)
#     session_id = request.cookies.get("session_id")
#     print("Session id:", session_id)

#     if not session_id:
#         return jsonify({"error": "session_id is required"}), 400

#     if session_manager.is_session_valid(session_id):
#         return jsonify({"session_exists": True}), 200
#     else:
#         return jsonify({"session_exists": False}), 200


# @session_bp.route("/delete_session", methods=["POST"])
# def delete_session():
#     session_id = request.cookies.get("session_id")

#     if not session_id:
#         return jsonify({"error": "Missing session_id cookie"}), 400

#     # Attempt to delete the session
#     deleted = session_manager.delete_session(session_id)

#     # Optionally clear the cookie from the client as well
#     response = make_response(jsonify({"deleted": deleted}))
#     response.set_cookie("session_id", "", expires=0)

#     return response


# # @session_bp.route("/generate_session", methods=["POST"])
# @session_bp.route("/generate_session")
# def generate_session():

#     try:
#         session_id = session_manager.create_session()

#         response = make_response(jsonify({"message": "Session created"}))
#         response.set_cookie(
#             "session_id",
#             value=session_id,
#             httponly=True,  # Prevents access via JavaScript
#             secure=True,  # Only send over HTTPS
#             samesite="None",  # Or "Strict"/"None" depending on your setup
#             path="/",
#             max_age=10800,
#         )
#         return response
#     except Exception as e:
#         return jsonify({"error": str(e)}), 500


# @session_bp.route("/debug_session")
# def debug_session():
#     return jsonify(dict(session))
