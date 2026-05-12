import asyncio
from flask import g, session, request
from session_manager_route.session_redis import get_session, validate_and_refresh_tokens
from utils.base_logger import get_logger

logger = get_logger(__name__)


def get_user_from_request() -> str | None:
    """
    Resolve the current user_id from g, Flask session, or Redis session cookies/headers.
    Returns the user_id string or None if unauthenticated.
    """
    # Already resolved by middleware
    if getattr(g, "user_id", None):
        return g.user_id

    # Flask session fallback
    if session.get("user_id"):
        return session.get("user_id")

    # Query param / body fallback (legacy)
    req_user_id = request.args.get("user_id") or (request.get_json(silent=True) or {}).get("user_id")
    if req_user_id:
        return req_user_id

    # Validate session cookies against Redis
    session_hash = request.cookies.get("session_id")
    access_token = request.cookies.get("access_token")
    if not access_token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            access_token = auth_header.split(" ")[1]

    if not session_hash or not access_token:
        return None

    try:
        redis_session, key_str = asyncio.run(get_session(session_hash, request))
        if not redis_session:
            return None

        is_valid, _ = asyncio.run(
            validate_and_refresh_tokens(session_hash, redis_session, access_token, key_str)
        )
        if not is_valid:
            return None

        user_id = redis_session.get("user_id")
        if user_id:
            g.user_id = user_id
        return user_id
    except Exception as e:
        logger.warning("auth_resolver: Redis session validation failed: %s", e)
        return None
