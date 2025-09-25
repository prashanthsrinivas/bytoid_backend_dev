import os
from dotenv import load_dotenv
from celery import Celery
from celery.utils.log import get_task_logger
import asyncio
from umail_helper.asyn_functions import v2all_continuous

logger = get_task_logger(__name__)
load_dotenv()

import redis

base_ip = os.getenv("CELERY_BROKER_URL")

lock_client = redis.StrictRedis.from_url(base_ip)  # or your broker Redis

LOCK_TTL = 600  # 10 minutes


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


@new_celery.task
def addbase(x, y):
    print("OKKKK")
    return x + y
