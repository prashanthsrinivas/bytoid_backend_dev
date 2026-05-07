from flask import Blueprint, request, jsonify, make_response, session, redirect, g
from functools import wraps
from db.rds_db import connect_to_rds
from db.db_checkers import get_email_by_id
from datetime import datetime
import os
from dotenv import load_dotenv
from session_manager_route.session_redis import session_login_redis, get_session, delete_all_session_cookies
load_dotenv()
import asyncio
from utils.base_logger import get_logger
from services.audit_log_service import log_audit_event, USER_LOGGED_OUT

session_bp = Blueprint("session", __name__)

logger = get_logger(__name__)


async def session_login(user_id):

    request_data = {
        "remote_addr": request.remote_addr,
        "user_agent": request.headers.get("User-Agent"),
    }
    result = await session_login_redis(user_id, request_data )
    session_id = result.get("session_id")
    access_token = result.get("access_token")
    refresh_token = result.get("refresh_token")
    
    return session_id, access_token, refresh_token



@session_bp.route("/delete_session", methods=["POST"])
def logout():
    session_hash = request.cookies.get("session_id")
    session_data, key_str = asyncio.run(get_session(session_hash, request))

    # Extract user_id from session BEFORE it is deleted (for audit logging)
    actor_user_id = None
    if session_data and isinstance(session_data, dict):
        actor_user_id = session_data.get("user_id")

    delete_status = asyncio.run(delete_all_session_cookies(key_str))

    actor_email = get_email_by_id(actor_user_id) if actor_user_id else None
    log_audit_event(
        action=USER_LOGGED_OUT,
        endpoint="/delete_session",
        ip=request.remote_addr,
        status="success" if delete_status else "failure",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
    )
    g.audit_logged = True

    if delete_status:
        return jsonify({"message": "Logged out!"})
    return jsonify({"message": "Could not find session to logout"})





