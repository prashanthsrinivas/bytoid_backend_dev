import os
from random import uniform
import time
import traceback
from dotenv import load_dotenv
from celery import Celery
from celery.utils.log import get_task_logger
import asyncio
from umail_helper.asyn_functions import v2all_continuous
import json
from umail_lance.umail_lance_agent import UmailLanceClient

# from umail_helper.auto_rep import autoReplyhelper

logger = get_task_logger(__name__)
load_dotenv()

import redis

base_ip = os.getenv("CELERY_BROKER_URL")

lock_client = redis.StrictRedis.from_url(base_ip)  # or your broker Redis

LOCK_TTL = 600  # 10 minutes

QUEUE_PREFIX = "user_embed_queue:"


def acquire_user_lock(user_id):
    # returns True if we got the lock, False otherwise
    return lock_client.set(f"umail_lock:{user_id}", "1", nx=True, ex=LOCK_TTL)


def release_user_lock(user_id):
    lock_client.delete(f"umail_lock:{user_id}")


def make_celery(app_name=__name__):
    print("having celery instanced")
    print("base IP", base_ip)
    celery = Celery(
        app_name,
        broker=base_ip,
        backend=base_ip,
    )

    celery.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        broker_transport_options={"visibility_timeout": 3600},
        worker_hijack_root_logger=False,
    )

    return celery


new_celery = make_celery()
celery = new_celery  # <— important for CLI


def backoff(retries):
    return min(2**retries, 300)


@new_celery.task(bind=True, name="tasks.umailSync")
def umail_sync(self, user_id):

    try:
        result = asyncio.run(v2all_continuous(user_id))
        return {"status": "completed", "user_id": user_id, "result": result}
    except Exception as exc:
        countdown = backoff(self.request.retries)
        raise self.retry(exc=exc, countdown=countdown, max_retries=5)
    finally:
        # always release lock at end so new task can start
        release_user_lock(user_id)


@new_celery.task(bind=True, name="webhook.umailSync")
def web_umail_sync(self, user_id):

    try:
        result = asyncio.run(v2all_continuous(user_id))
        return {"status": "completed", "user_id": user_id, "result": result}
    except Exception as exc:
        countdown = backoff(self.request.retries)
        raise self.retry(exc=exc, countdown=countdown, max_retries=5)
    finally:
        # always release lock at end so new task can start
        release_user_lock(user_id)


@new_celery.task(bind=True, name="umail_helper.delayed_trigger")
def delayed_trigger(self, user_email, history_id):
    import time
    from utils.delay_mails import DelayTrigger

    lock_key = f"umail_delayed_lock:{user_email}"
    # Try to acquire lock for 10 minutes
    acquired = lock_client.set(lock_key, "1", nx=True, ex=LOCK_TTL)
    if not acquired:
        # Another task is already running or recently completed
        logger.info(
            {
                "status": "skipped",
                "user_email": user_email,
                "reason": "task already running or locked",
            }
        )
        return {
            "status": "skipped",
            "user_email": user_email,
            "reason": "task already running or locked",
        }

    try:
        trigger = DelayTrigger(wait_seconds=30)
        trigger.trigger(user_email, history_id)
        return {"status": "completed", "user_email": user_email}
    except Exception as exc:
        countdown = backoff(self.request.retries)
        raise self.retry(exc=exc, countdown=countdown, max_retries=5)
    finally:
        # Always release lock at the end so new task can start
        lock_client.delete(lock_key)


# ------------ REPLY EMBEDDING TASK FOR AI ASSISTANT --------------#


def enqueue_user_task(user_id, payload):
    """
    Add a new embedding task for a user to their Redis queue.
    """
    key = f"{QUEUE_PREFIX}{user_id}"
    lock_client.rpush(key, json.dumps(payload))
    logger.info(
        f"📩 Queued embedding for user {user_id} (queue size: {lock_client.llen(key)})"
    )

    # Trigger worker to process if not already running
    process_user_queue.delay(user_id)


def exponential_backoff(retries, base=2, cap=300):
    """Return exponential backoff in seconds (min delay 1s, max 300s)."""
    return min(base**retries + uniform(0, 1), cap)


@new_celery.task(bind=True, name="embedding.process_user_queue")
def process_user_queue(self, user_id):
    """
    Process all embedding tasks for a user sequentially (FIFO).
    Automatically retries failed tasks and keeps strict ordering.
    """
    key = f"{QUEUE_PREFIX}{user_id}"
    lock_key = f"{key}:lock"

    # Acquire a distributed lock per user (avoid two workers running the same queue)
    got_lock = lock_client.set(lock_key, "1", nx=True, ex=LOCK_TTL)
    if not got_lock:
        logger.info(f"⏸ Queue for user {user_id} already being processed.")
        return

    try:
        while True:
            raw_task = lock_client.lpop(key)
            if not raw_task:
                logger.info(f"✅ Queue empty for user {user_id}. Done.")
                break

            payload = json.loads(raw_task)
            retries = payload.get("retries", 0)

            user_id = payload["user_id"]
            client_id = payload["client_id"]
            conversation_id = payload["conversation_id"]
            input_data = payload["input_data"]

            logger.info(
                f"🚀 Processing embedding for user {user_id}, conv_id: {conversation_id}, retry={retries}"
            )

            try:

                client = UmailLanceClient(user_id)
                client.embed_json_file_for_reply(
                    input_data, user_id, client_id, conversation_id
                )

                logger.info(f"✅ Completed embedding for conv_id: {conversation_id}")

            except Exception as e:
                logger.error(f"❌ Error embedding for conv_id {conversation_id}: {e}")
                traceback.print_exc()

                if retries < 5:  # Retry up to 5 times
                    delay = exponential_backoff(retries)
                    payload["retries"] = retries + 1
                    logger.warning(
                        f"🔁 Retrying task {conversation_id} in {delay:.1f}s (attempt {retries + 1}/5)"
                    )
                    time.sleep(delay)
                    # Requeue task to the *front* so it retries before the next new task
                    lock_client.lpush(key, json.dumps(payload))
                else:
                    logger.error(
                        f"💀 Max retries reached for conv_id: {conversation_id}"
                    )
    finally:
        lock_client.delete(lock_key)
        logger.info(f"🔓 Released lock for user {user_id}")


# @new_celery.task(bind=True, name="webhook.testautoreply")
# def testassistfeat(self, user_id, all_results, my_email):

#     lock_key = f"umail_autopilot:{my_email}"
#     acquired = lock_client.set(lock_key, "1", nx=True, ex=60)
#     if not acquired:
#         return {"status": "skipped", "user_email": my_email}

#     try:
#         result = asyncio.run(
#             autoReplyhelper(all_results=all_results, my_email=my_email, user_id=user_id)
#         )
#         return {"status": "completed", "user_id": user_id, "result": result}
#     except Exception as exc:
#         countdown = backoff(self.request.retries)
#         raise self.retry(exc=exc, countdown=countdown, max_retries=5)
#     finally:
#         release_user_lock(user_id)


@new_celery.task
def addbase(x, y):
    print("OKKKK")
    return x + y
