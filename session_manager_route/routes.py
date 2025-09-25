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





