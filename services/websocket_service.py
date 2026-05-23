import json
import time
import boto3
from services.redis_service import get_redis
import os
from utils.app_configs import IS_DEV
from utils.base_logger import get_logger

logger = get_logger(__name__, log_level="DEBUG" if IS_DEV else "INFO")


class WebSocketService:
    def __init__(self):
        self.redis = get_redis()
        self.ws_endpoint = os.getenv("WEBSOCKETURL")

        self.client = boto3.client(
            "apigatewaymanagementapi",
            endpoint_url=self.ws_endpoint,
            region_name=os.getenv("S3_REGION"),  # 👈 add this
        )

    # -------------------------------
    # 🔑 Redis Keys
    # -------------------------------

    def _user_key(self, user_id):
        return f"ws:user:{user_id}"

    def _conn_key(self, connection_id):
        return f"ws:conn:{connection_id}"

    # -------------------------------
    # 📌 Store Connection
    # -------------------------------

    async def store_connection(self, user_id, session_id, connection_id):
        key = self._user_key(user_id)

        data = await self.redis.get(key) or {}
        data[session_id] = connection_id

        await self.redis.set(key, data)

        # reverse mapping
        await self.redis.set(
            self._conn_key(connection_id),
            {"user_id": user_id, "session_id": session_id},
        )

    # -------------------------------
    # ❌ Delete Connection
    # -------------------------------

    async def delete_connection(self, connection_id):
        mapping = await self.redis.get(self._conn_key(connection_id))

        if not mapping:
            return

        user_id = mapping.get("user_id")
        session_id = mapping.get("session_id")

        user_key = self._user_key(user_id)
        data = await self.redis.get(user_key) or {}

        if session_id in data:
            del data[session_id]
            await self.redis.set(user_key, data)

        await self.redis.delete(self._conn_key(connection_id))

    # -------------------------------
    # 📤 Send Message
    # -------------------------------

    async def emit(
        self,
        user_id,
        message,
        scope="global",  # 👈 NEW
        session_id=None,
        job_id=None,
        msg_type="info",
        stage=None,
        progress=None,
        feature=None,
    ):
        import json, time, asyncio
        from botocore.exceptions import ClientError

        key = self._user_key(user_id)

        connections = await self.redis.get(key) or "{}"

        if isinstance(connections, str):
            connections = json.loads(connections)

        payload = {
            "scope": scope,  # 👈 IMPORTANT
            "type": msg_type,
            "message": message,
            "user_id": user_id,
            "session_id": session_id,
            "job_id": job_id,
            "stage": stage,
            "progress": progress,
            "feature": feature,
            "timestamp": time.time(),
        }

        for sess_id, conn_id in connections.items():

            # 🎯 SESSION FILTER
            if scope in ["session", "job"] and session_id and sess_id != session_id:
                continue

            try:
                await asyncio.to_thread(
                    self.client.post_to_connection,
                    ConnectionId=conn_id,
                    Data=json.dumps(payload).encode("utf-8"),
                )

            except ClientError as e:
                code = e.response.get("Error", {}).get("Code")

                if code == "GoneException":
                    await self.delete_connection(conn_id)
                    continue

                logger.warning("WS error: %s", e)

            except Exception as e:
                logger.error("WS unexpected error: %s", e, exc_info=IS_DEV)
                await self.delete_connection(conn_id)


class WSMessageBuilder:

    @staticmethod
    def global_msg(message):
        return {
            "scope": "global",
            "message": message,
            "type": "notification",
        }

    @staticmethod
    def global_session_msg(session_id, message):
        return {
            "scope": "global",
            "session_id": session_id,
            "message": message,
            "type": "notification",
        }

    @staticmethod
    def session_progress(session_id, message, progress):
        return {
            "scope": "session",
            "session_id": session_id,
            "type": "progress",
            "message": message,
            "progress": progress,
        }

    @staticmethod
    def session_success(session_id, message, progress):
        return {
            "scope": "session",
            "session_id": session_id,
            "type": "success",
            "message": message,
            "progress": 100,
        }

    @staticmethod
    def job_progress(job_id, session_id, stage, message, progress):
        return {
            "scope": "job",
            "job_id": job_id,
            "session_id": session_id,
            "type": "progress",
            "stage": stage,
            "message": message,
            "progress": progress,
        }

    @staticmethod
    def job_success(job_id, session_id, message):
        return {
            "scope": "job",
            "job_id": job_id,
            "session_id": session_id,
            "type": "success",
            "message": message,
            "progress": 100,
        }

    @staticmethod
    def job_error(job_id, session_id, message):
        return {
            "scope": "job",
            "job_id": job_id,
            "session_id": session_id,
            "type": "error",
            "message": message,
        }

    @staticmethod
    def report_toast(message, report_name=None, status="creating"):
        """Global floating-bar notification for report generation lifecycle.

        status: "creating" | "done" | "error"
        """
        return {
            "scope": "global",
            "type": "report_toast",
            "message": message,
            "report_name": report_name,
            "status": status,
        }
