from dotenv import load_dotenv
import uuid
import hashlib
import secrets
from datetime import datetime, timezone, timedelta
import os
from glide import (
    GlideClusterClient,
    ClusterScanCursor,
)
from services.redis_service import RedisService
from utils.base_logger import get_logger


load_dotenv()

logger = get_logger(__name__)


# addresses = [
#     NodeAddress("bytoidcache-w2ofwh.serverless.cac1.cache.amazonaws.com", 6379)
# ]

# config = GlideClusterClientConfiguration(addresses=addresses, use_tls=True)


def hash_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def verify_sha256(text: str, given_hash: str) -> bool:
    return hash_sha256(text) == given_hash.lower()


async def session_login_redis(user_id, request_data):  # during login

    # print("inside session_login_redis")
    # 1. Generate session_id
    session_id = str(uuid.uuid4())

    # 2. Hash the session_id (SHA-256)
    session_hash = hash_sha256(session_id)

    # 3. Generate tokens
    access_token = secrets.token_urlsafe(32)  # random string, short-lived
    refresh_token = secrets.token_urlsafe(64)  # longer token

    # Expiry times
    session_expiry = datetime.now(timezone.utc) + timedelta(minutes=30)
    access_expiry = datetime.now(timezone.utc) + timedelta(minutes=15)
    refresh_expiry = datetime.now(timezone.utc) + timedelta(hours=1)

    # client = await GlideClusterClient.create(redis_config_glide)
    client = RedisService()

    await client.hset(
        f"session:{session_id}",
        {
            "user_id": user_id,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "session_expiry": session_expiry.isoformat(),
            "access_expiry": access_expiry.isoformat(),
            "refresh_expiry": refresh_expiry.isoformat(),
            "ip": request_data.get("remote_addr"),
            "user_agent": request_data.get("user_agent"),
            "active": "1",
        },
    )

    await client.close()

    return {
        "session_id": session_hash,
        "access_token": access_token,
        "refresh_token": refresh_token,
    }


async def get_session(session_hash, request):

    # client = await GlideClusterClient.create(redis_config_glide)
    client = RedisService()

    session_keys = []
    try:
        # Use scan_iter which handles cursor management automatically
        async for key in client.scan_iter(match="session:*", count=100):
            session_keys.append(key)
    except AttributeError:
        # Fallback to manual cursor handling if scan_iter is not available
        cursor = ClusterScanCursor()  # Use proper cursor object

        while True:
            result = await client.scan(cursor, match="session:*", count=100)

            if isinstance(result, (list, tuple)) and len(result) == 2:
                cursor, keys = result
            else:
                logger.error(f"Unexpected scan result format: {result}")
                break

            if keys:
                session_keys.extend(keys)

            # Check if scan is complete
            if cursor.is_finished():
                break

    for key in session_keys:
        key_str = key.decode() if isinstance(key, bytes) else key

        if key_str.startswith("session:"):
            session_uuid = key_str[8:]  # Remove "session:" prefix

            if verify_sha256(session_uuid, session_hash):
                session_data = await client.hgetall(key_str)

                # hgetall might return bytes, decode if needed
                if session_data:
                    session_data = {
                        k.decode() if isinstance(k, bytes) else k: (
                            v.decode() if isinstance(v, bytes) else v
                        )
                        for k, v in session_data.items()
                    }

                    await client.close()
                    return session_data, key_str

    await client.close()
    return None, None


async def update_session_tokens(
    client, session_hash, new_access_token, new_access_expiry, key_str, previous_token
):
    """Update session with new access token"""
    try:

        session_data = await client.hgetall(key_str)
        if session_data:
            await client.hset(
                key_str,
                {
                    "access_token": new_access_token,
                    "access_expiry": new_access_expiry.isoformat(),
                    "previous_access_token": previous_token,
                    "previous_token_expiry": (
                        datetime.now(timezone.utc) + timedelta(minutes=2)
                    ).isoformat(),  # Grace period
                },
            )
            return True

    except Exception as e:
        logger.error(f"Could not find session to update: {session_hash}")
        raise


async def validate_and_refresh_tokens(session_hash, session, access_token, key_str):

    # client = await GlideClusterClient.create(redis_config_glide)
    client = RedisService()

    previous_token_expiry_str = session.get("previous_token_expiry")
    if previous_token_expiry_str:
        previous_token_expiry = datetime.fromisoformat(previous_token_expiry_str)
        if datetime.now(timezone.utc) > previous_token_expiry:
            await client.hdel(key_str, ["previous_access_token"])
            await client.hdel(key_str, ["previous_token_expiry"])

    access_expiry = datetime.fromisoformat(session["access_expiry"])
    refresh_expiry = datetime.fromisoformat(session["refresh_expiry"])
    current_time = datetime.now(timezone.utc)

    # Check if provided access token matches stored one
    stored_access_token = session.get("access_token")
    if access_token != stored_access_token:
        # Check if it matches the previous token (grace period for race conditions)
        previous_token = session.get("previous_access_token")
        if previous_token and access_token == previous_token:
            logger.info("Using previous access token (grace period)")
            # Continue with validation using the previous token
        else:
            logger.info(
                f"Access token mismatch. Provided: {access_token[:20]}..., Stored: {stored_access_token[:20] if stored_access_token else 'None'}..."
            )
            return False, None

    if current_time <= access_expiry:
        # Access token is still valid
        return True, None

    if current_time > refresh_expiry:
        # Refresh token also expired
        await delete_all_session_cookies(key_str)
        return False, None

    # Refresh token is valid, generate new access token

    new_access_token = secrets.token_urlsafe(32)
    new_access_expiry = current_time + timedelta(minutes=15)

    await update_session_tokens(
        client,
        session_hash,
        new_access_token,
        new_access_expiry,
        key_str,
        stored_access_token,
    )

    new_tokens = {
        "access_token": new_access_token,
        "access_expiry": new_access_expiry.isoformat(),
    }

    await client.close()
    return True, new_tokens


async def delete_all_session_cookies(key_str):
    """Delete session from Redis"""
    # client = await GlideClusterClient.create(redis_config_glide)
    client = RedisService()
    try:
        session_data = await client.hgetall(key_str)
        if session_data:
            await client.delete([key_str])

    except:
        logger.warning(f"Could not find session to delete: {key_str}")

    await client.close()
    return True
