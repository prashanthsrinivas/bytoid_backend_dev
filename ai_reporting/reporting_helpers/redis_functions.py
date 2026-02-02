# from glide import (
#     GlideClusterClient,
#     ClusterScanCursor,
# )
from services.redis_service import RedisService

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

    # Create Redis client
    # client = await GlideClusterClient.create(redis_config_glide)
    client = RedisService()

    try:
        # Get the stored data
        data = await client.get(key)

        if not data:
            # print(f"[Redis] No clarification state found for user {user_id}.")
            return None

        # Decode JSON string
        clarification_data = json.loads(data)

        # Delete the key after retrieving
        await client.delete(key)
        # print(f"[Redis] Retrieved and deleted clarification state for user {user_id}.")

        return clarification_data

    except Exception as e:
        # print(
        #     f"[Redis Error] Failed to retrieve clarification state for user {user_id}: {e}"
        # )
        return None

    finally:
        await client.close()


async def save_clarification_state_to_redis(user_id, state_data):
    """
    Save clarification state details in Redis for a given user (Flask version).

    Args:
        user_id (str): ID of the current user
        state_data (dict): Dictionary containing clarification state info
    """
    key = f"user:{user_id}:clarification_state"

    try:
        # Create Redis client (sync version)
        # client = await GlideClusterClient.create(redis_config_glide)
        client = RedisService()

        # Convert to JSON string
        json_data = json.dumps(state_data)

        # Replace if exists (overwrite)
        await client.set(key, json_data)
        await client.expire(key, 3600)

    # print(
    #     f"[Redis] Saved clarification state for user {user_id} (expires in 3600s)."
    # )

    except Exception as e:
        # print(f"[Redis Error] Failed to save clarification state: {e}")
        return None

    finally:
        await client.close()


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

    # Create the Redis client
    # client = await GlideClusterClient.create(redis_config_glide)
    client = RedisService()

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

    # print(f"[Redis] Saved ambiguous report for user {user_id} (expires in 3600s).")

    except Exception as e:
        # print(f"[Redis Error] Failed to save ambiguous report: {e}")
        return None

    finally:
        await client.close()


async def get_ambiguous_report_from_redis(user_id):
    """
    Retrieve ambiguous report details from Redis for a given user.

    Args:
        user_id (str): ID of the current user

    Returns:
        dict | None: The stored ambiguous report data or None if not found
    """
    key = f"user:{user_id}:ambiguous_report"
    # client = await GlideClusterClient.create(redis_config_glide)
    client = RedisService()

    try:
        json_data = await client.get(key)

        if json_data:
            data = json.loads(json_data)
            # print(f"[Redis] Retrieved ambiguous report for user {user_id}.")
            await client.delete(f"user:{user_id}:ambiguous_report")
            return data
        else:
            # print(f"[Redis] No ambiguous report found for user {user_id}.")
            return None

    except Exception as e:
        # print(f"[Redis Error] Failed to retrieve ambiguous report: {e}")
        return None

    finally:
        await client.close()
