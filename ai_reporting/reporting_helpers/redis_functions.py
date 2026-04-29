# from glide import (
#     GlideClusterClient,
#     ClusterScanCursor,
# )
from services.redis_service import get_redis

import json


async def get_report_data(user_id: str):
    """
    Retrieve the saved clarification state for a user from Redis,
    then delete the key after successful retrieval.

    Args:
        user_id (str): ID of the current user.

    Returns:
        dict: Clarification state data previously saved in Redis, or None if not found.
    """
    key = f"user:{user_id}:clarification_state"

    client = get_redis()

    try:
        # Get the stored data
        data = await client.get(key)

        if not data:
            return None

        # Decode JSON string
        clarification_data = json.loads(data)

        # Delete the key after retrieving
        await client.delete(key)

        return clarification_data

    except Exception as e:
        return None


async def save_clarification_state_to_redis(user_id, state_data):
    """
    Save clarification state details in Redis for a given user (Flask version).

    Args:
        user_id (str): ID of the current user
        state_data (dict): Dictionary containing clarification state info
    """
    key = f"user:{user_id}:clarification_state"

    try:
        client = get_redis()

        # Convert to JSON string
        json_data = json.dumps(state_data)

        # Replace if exists (overwrite)
        await client.set(key, json_data)
        await client.expire(key, 3600)

    except Exception as e:
        return None


async def save_ambiguous_report_to_redis(
    user_id, results, ambiguous, query, special_access_status
):
    """
    Save ambiguous report details in Redis for a given user.

    Args:
        user_id (str): ID of the current user
        results (list/dict): ambiguous results to store
        query (str): original user query
        special_access_status (dict): access control info
    """

    key = f"user:{user_id}:ambiguous_report"

    client = get_redis()

    try:
        # Prepare the data as JSON
        data = {
            "results": results,
            "ambiguous": ambiguous,
            "query": query,
            "special_access_status": special_access_status,
        }

        # Convert to JSON string
        json_data = json.dumps(data)

        # Replace if exists (overwrite)
        await client.set(key, json_data)
        await client.expire(key, 3600)

    except Exception as e:
        return None


async def get_ambiguous_report_from_redis(user_id):
    """
    Retrieve ambiguous report details from Redis for a given user.

    Args:
        user_id (str): ID of the current user

    Returns:
        dict | None: The stored ambiguous report data or None if not found
    """
    key = f"user:{user_id}:ambiguous_report"
    client = get_redis()

    try:
        json_data = await client.get(key)

        if json_data:
            data = json.loads(json_data)
            await client.delete(f"user:{user_id}:ambiguous_report")
            return data
        else:
            return None

    except Exception as e:
        return None
