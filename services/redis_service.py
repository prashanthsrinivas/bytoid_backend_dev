import os
import json
import redis
from redis.cluster import RedisCluster
import asyncio
from typing import Any, Optional
from utils.app_configs import IS_DEV
from utils.base_logger import get_logger

logger = get_logger(__name__, log_level="DEBUG" if IS_DEV else "INFO")

base_ip = os.getenv("CELERY_BROKER_URL")
dev_val = os.getenv("DEV", "")


class RedisService:
    """
    Proper async wrapper for synchronous redis-py client.
    Ensures all Redis calls run in background threads.
    """

    def __init__(self):
        self.redis_host = os.getenv("REDIS_HOST_DEV")
        if not self.redis_host:
            raise ValueError("Missing REDIS_HOST_DEV")
        if IS_DEV or dev_val == "true":
            logger.info("connecting to Dev Redis")

            self.client = redis.Redis(
                host=self.redis_host,  # ElastiCache primary endpoint
                port=6379,  # or 6380 if configured
                ssl=True,
                ssl_cert_reqs=None,  # ✅ important
                decode_responses=True,
                socket_connect_timeout=5,
            )
        else:
            logger.info("connecting to Prod Redis")
            self.client = redis.Redis(
                host=self.redis_host,
                port=6379,
                ssl=True,
                ssl_ca_certs="/home/ec2-user/bytoid_python/awsredis.pem",  # 👈 CA cert here
                ssl_cert_reqs="required",  # 👈 enforce validation
                decode_responses=True,
                socket_connect_timeout=5,
            )

    def _reconnect(self) -> None:
        logger.warning("Redis reconnecting...")
        if IS_DEV or dev_val == "true":
            self.client = redis.Redis(
                host=self.redis_host,
                port=6379,
                ssl=True,
                ssl_cert_reqs=None,
                decode_responses=True,
                socket_connect_timeout=5,
            )
        else:
            self.client = redis.Redis(
                host=self.redis_host,
                port=6379,
                ssl=True,
                ssl_ca_certs="/home/ec2-user/bytoid_python/awsredis.pem",
                ssl_cert_reqs="required",
                decode_responses=True,
                socket_connect_timeout=5,
            )

    async def _run(self, func, *args, **kwargs):
        """Run any blocking Redis command in a background thread, with one reconnect retry."""
        try:
            return await asyncio.to_thread(func, *args, **kwargs)
        except (redis.ConnectionError, redis.TimeoutError) as exc:
            logger.warning("Redis error (%s), reconnecting and retrying once...", exc)
            self._reconnect()
            return await asyncio.to_thread(func, *args, **kwargs)

    # --------------------- BASIC CRUD ----------------------

    async def checker(self):
        try:
            logger.debug("Trying to PING Redis...")
            pong = await self._run(self.client.ping)
            logger.debug("Redis response: %s", pong)
            return True
        except Exception as e:
            logger.error("Redis error: %s", e, exc_info=IS_DEV)
            return False

    async def set(self, key: str, value: Any, ex: Optional[int] = None) -> bool:
        if isinstance(value, (dict, list)):
            value = json.dumps(value)
        return bool(await self._run(self.client.set, key, value, ex))

    async def get(self, key: str) -> Optional[Any]:
        value = await self._run(self.client.get, key)
        if value is None:
            return None
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value

    async def delete(self, key: str) -> int:
        return await self._run(self.client.delete, key)

    async def exists(self, key: str) -> bool:
        return bool(await self._run(self.client.exists, key))

    # --------------------- SCAN ITERATOR ----------------------

    async def scan_iter(self, match=None, count=100):
        cursor = 0
        while True:
            cursor, keys = await self._run(
                self.client.scan, cursor=cursor, match=match, count=count
            )
            for k in keys:
                yield k
            if cursor == 0:
                break

    # --------------------- HASH CRUD ----------------------

    async def hset(self, name: str, key: Any, value: Any = None):
        if isinstance(key, dict) and value is None:
            mapping = {
                k: json.dumps(v) if isinstance(v, (dict, list)) else v
                for k, v in key.items()
            }
            return await self._run(self.client.hset, name, mapping=mapping)

        if isinstance(value, (dict, list)):
            value = json.dumps(value)

        return await self._run(self.client.hset, name, key, value)

    async def hget(self, name: str, key: str):
        value = await self._run(self.client.hget, name, key)
        if value is None:
            return None

        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value

    async def hgetall(self, name: str):
        data = await self._run(self.client.hgetall, name)
        new = {}
        for k, v in data.items():
            try:
                new[k] = json.loads(v)
            except json.JSONDecodeError:
                new[k] = v
        return new

    async def hdel(self, name: str, key: str) -> int:
        return await self._run(self.client.hdel, name, key)

    # --------------------- UTILITY ----------------------

    async def incr(self, key: str, amount: int = 1) -> int:
        return await self._run(self.client.incr, key, amount)

    async def hincrby(self, name: str, key: str, amount: int = 1) -> int:
        return await self._run(self.client.hincrby, name, key, amount)

    async def expire(self, key: str, seconds: int) -> bool:
        return bool(await self._run(self.client.expire, key, seconds))

    async def ttl(self, key: str) -> int:
        return await self._run(self.client.ttl, key)

    async def rpush(self, key, value):
        return await self._run(self.client.rpush, key, value)

    async def close(self):
        if self.client:
            await self._run(self.client.close)


_instance: Optional[RedisService] = None


def get_redis() -> RedisService:
    """Return the shared RedisService singleton, creating it on first call."""
    global _instance
    if _instance is None:
        _instance = RedisService()
    return _instance
