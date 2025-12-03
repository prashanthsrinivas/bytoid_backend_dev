"""
Sync Manager for handling message sync timing and caching
Ensures that email syncing doesn't trigger too frequently
"""

import asyncio
import time
import json
from datetime import datetime, timedelta, timezone
from glide import GlideClusterClient
from utils.base_logger import get_logger
from utils.redis_config import redis_config_glide

logger = get_logger(__name__)

# Sync interval in seconds (30 minutes for development)
SYNC_INTERVAL = 30 * 60  # 1800 seconds


class SyncManager:
    """
    Manages email sync timing per user.
    Uses Redis to cache the last sync time and prevent frequent syncs.
    """

    @staticmethod
    async def get_last_sync_time(user_id: str) -> dict:
        """
        Get the last sync time for a user from Redis cache.
        
        Returns:
            dict with keys:
            - 'last_sync': ISO timestamp of last sync (or None)
            - 'next_allowed_sync': ISO timestamp when next sync is allowed
            - 'should_sync': bool indicating if sync should happen now
            - 'time_until_next_sync': seconds until next sync allowed
        """
        try:
            client = await GlideClusterClient.create(redis_config_glide)
            cache_key = f"sync_time:{user_id}"
            cached_data = await client.get(cache_key)
            
            now = datetime.now(timezone.utc)
            
            if cached_data:
                sync_data = json.loads(cached_data)
                last_sync_str = sync_data.get("last_sync")
                last_sync = datetime.fromisoformat(last_sync_str)
                next_allowed = last_sync + timedelta(seconds=SYNC_INTERVAL)
                
                should_sync = now >= next_allowed
                time_diff = (next_allowed - now).total_seconds()
                
                return {
                    "last_sync": last_sync.isoformat(),
                    "next_allowed_sync": next_allowed.isoformat(),
                    "should_sync": should_sync,
                    "time_until_next_sync": max(0, int(time_diff)),
                }
            else:
                # No sync record exists, allow sync
                next_allowed = now + timedelta(seconds=SYNC_INTERVAL)
                return {
                    "last_sync": None,
                    "next_allowed_sync": next_allowed.isoformat(),
                    "should_sync": True,
                    "time_until_next_sync": 0,
                }
        except Exception as e:
            logger.error(f"Error getting last sync time for {user_id}: {e}")
            # On error, allow sync to proceed
            return {
                "last_sync": None,
                "next_allowed_sync": None,
                "should_sync": True,
                "time_until_next_sync": 0,
            }

    @staticmethod
    async def record_sync_time(user_id: str) -> bool:
        """
        Record the current time as the last sync for this user.
        Sets TTL to 2x the sync interval to clean up old entries.
        
        Returns:
            bool indicating success
        """
        try:
            client = await GlideClusterClient.create(redis_config_glide)
            cache_key = f"sync_time:{user_id}"
            now = datetime.now(timezone.utc)
            
            sync_data = {
                "last_sync": now.isoformat(),
                "timestamp": int(now.timestamp()),
            }
            
            # Set with TTL of 2x sync interval
            ttl = SYNC_INTERVAL * 2
            await client.set(cache_key, json.dumps(sync_data), {"EX": ttl})
            
            logger.info(f"Recorded sync time for {user_id}")
            return True
        except Exception as e:
            logger.error(f"Error recording sync time for {user_id}: {e}")
            return False

    @staticmethod
    async def should_sync_on_login(user_id: str) -> dict:
        """
        Check if a user should trigger a sync on login.
        ⭐ CHANGED: Login syncs now trigger IMMEDIATELY (no 30-minute interval)
        
        Returns:
            dict with:
            - 'should_sync': always True (immediate trigger)
            - 'reason': explanation of action
            - 'context': 'login' to indicate this is a login trigger
        """
        try:
            sync_info = await SyncManager.get_last_sync_time(user_id)
            
            # ⭐ NEW: Always allow login sync (no 30-min check)
            return {
                "should_sync": True,
                "last_sync": sync_info.get("last_sync"),
                "next_allowed_sync": sync_info["next_allowed_sync"],
                "time_until_next_sync": 0,
                "reason": "Login sync triggered immediately",
                "context": "login",
            }
        except Exception as e:
            logger.error(f"Error in should_sync_on_login: {e}")
            # On error, allow sync as fallback
            return {
                "should_sync": True,
                "last_sync": None,
                "next_allowed_sync": None,
                "time_until_next_sync": 0,
                "reason": "Login sync triggered immediately (error fallback)",
                "context": "login_error_fallback",
            }

    @staticmethod
    async def should_sync_on_manual_action(user_id: str) -> dict:
        """
        Check if a user can manually trigger a sync (from button click or refresh).
        ⭐ CHANGED: Manual syncs now trigger IMMEDIATELY (no 30-minute interval)
        
        Returns:
            dict with:
            - 'should_sync': always True for manual (immediate trigger)
            - 'reason': string explaining action
            - 'context': 'manual_action'
        """
        try:
            # ⭐ NEW: Always allow manual sync (no 30-min check)
            return {
                "should_sync": True,
                "reason": "Manual sync triggered immediately",
                "next_allowed_sync": None,
                "time_until_next_sync": 0,
                "context": "manual_action",
            }
        except Exception as e:
            logger.error(f"Error in should_sync_on_manual_action: {e}")
            # On error, allow sync to be safe
            return {
                "should_sync": True,
                "reason": f"Manual sync triggered immediately (error fallback)",
                "next_allowed_sync": None,
                "time_until_next_sync": 0,
                "context": "manual_action_error",
            }

    @staticmethod
    async def clear_sync_timer(user_id: str) -> bool:
        """
        Clear the sync timer for a user (useful for testing or forced resets).
        
        Returns:
            bool indicating success
        """
        try:
            client = await GlideClusterClient.create(redis_config_glide)
            cache_key = f"sync_time:{user_id}"
            result = await client.delete([cache_key])
            logger.info(f"Cleared sync timer for {user_id}")
            return result >= 1
        except Exception as e:
            logger.error(f"Error clearing sync timer for {user_id}: {e}")
            return False


def get_sync_manager():
    """Helper to get SyncManager class"""
    return SyncManager
