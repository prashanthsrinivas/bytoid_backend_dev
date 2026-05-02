from datetime import datetime
import os
from random import uniform
import shutil
import time
import traceback
from apiConnector.helpers import is_schedule_app_active
from cust_helpers import pathconfig
from dotenv import load_dotenv
from celery import Celery
from celery.utils.log import get_task_logger
import asyncio
from runbook.helper import (
    analyze_questions_with_references,
    trigger_runbooks_for_api_response,
    trigger_scheduled_api_runbook,
    trigger_scheduled_playbook_runbook,
)
from services.redis_service import get_redis

from umail_helper.asyn_functions import fetchnextmonthmails, v2all_continuous
import json
from umail_helper.mails_process import check_mailbox
from umail_lance.umail_lance_agent import UmailLanceClient
from utils.app_configs import IS_DEV
from utils.async_check import run_async
from microsoft_route.get_microsoft_emails import v2all_continuous_outlook
from db.rds_db import connect_to_rds
from request_context import current_user_id
from playbook.helperzz import returnconfigandpath
from utils.s3_utils import read_json_from_s3, upload_any_file
from zoho_routes.routes import v2all_continuous_zoho

# from umail_helper.auto_rep import autoReplyhelper

logger = get_task_logger(__name__)
load_dotenv()

dev_val = os.getenv("DEV", "")
base_ip = os.getenv("CELERY_BROKER_URL")
# lock_client = redis.StrictRedis.from_url(base_ip)  # or your broker Redis
lock_client = get_redis()

LOCK_TTL = 600  # 10 minutes

QUEUE_PREFIX = "user_embed_queue:"


def acquire_user_lock(user_id):
    # returns True if we got the lock, False otherwise
    return run_async(lock_client.set(f"umail_lock:{user_id}", "1", ex=LOCK_TTL, nx=True))


def release_user_lock(user_id):
    run_async(lock_client.delete(f"umail_lock:{user_id}"))


def acquire_scrape_lock(user_id, url):
    # returns True if we got the lock, False otherwise
    return run_async(lock_client.set(f"scrape_lock:{user_id}", str(url), ex=LOCK_TTL, nx=True))


async def get_scrape_lock(user_id):
    return await lock_client.get(f"scrape_lock:{user_id}")


def release_scrape_lock(user_id):
    run_async(lock_client.delete(f"scrape_lock:{user_id}"))


def make_celery(app_name=__name__):
    # print("having celery instanced")
    # print("base IP", base_ip)
    celery = Celery(
        app_name,
        broker=base_ip,
        backend=base_ip,
    )
    if IS_DEV or dev_val == "true":
        print("connecting to Dev Redis for celery")
        celery.conf.update(
            task_serializer="json",
            result_serializer="json",
            accept_content=["json"],
            task_acks_late=True,
            task_reject_on_worker_lost=True,
            worker_prefetch_multiplier=1,
            broker_transport_options={"visibility_timeout": 3600},
            broker_use_ssl={
                "ssl_cert_reqs": "none"
            },  # required for AWS ElastiCache TLS
            redis_backend_use_ssl={"ssl_cert_reqs": "none"},
            worker_hijack_root_logger=False,
        )
    else:
        print("connecting to Prod Redis for celery")
        celery.conf.update(
            task_serializer="json",
            result_serializer="json",
            accept_content=["json"],
            task_acks_late=True,
            task_reject_on_worker_lost=True,
            worker_prefetch_multiplier=1,
            broker_transport_options={"visibility_timeout": 3600},
            broker_use_ssl={
                "ssl_ca_certs": "/home/ec2-user/bytoid_python/awsredis.pem",  # 👈 ADD HERE
                "ssl_cert_reqs": "required",
            },
            redis_backend_use_ssl={
                "ssl_ca_certs": "/home/ec2-user/bytoid_python/awsredis.pem",  # 👈 ADD HERE
                "ssl_cert_reqs": "required",
            },
            worker_hijack_root_logger=False,
        )

    return celery


new_celery = make_celery()
celery = new_celery  # <— important for CLI


def backoff(retries):
    return min(2**retries, 300)


@new_celery.task(bind=True, name="tasks.umailSync")
def umail_sync(self, user_id):

    connection = None
    cursor = None
    _release_lock = True  # hold lock during retries; release only on final success/failure

    try:
        connection = connect_to_rds()

        if connection is None:
            raise Exception("Database connection failed (connect_to_rds returned None)")

        cursor = connection.cursor()

        integration = None
        social = None

        cursor.execute(
            "SELECT social FROM users WHERE user_id = %s",
            (user_id,),
        )
        row = cursor.fetchone()

        if row:
            social = row[0]
        else:
            cursor.execute(
                "SELECT platform FROM integrations WHERE user_id = %s",
                (user_id,),
            )
            row = cursor.fetchone()
            if row:
                social = row[0]
                integration = True

        mailbox_setting = check_mailbox(user_id)

        if not mailbox_setting:
            return {"status": "skipped", "user_id": user_id, "reason": "mailbox_disabled"}

        if social == "google":
            result = asyncio.run(v2all_continuous(user_id, integration=integration))
        elif social == "outlook":
            result = asyncio.run(
                v2all_continuous_outlook(user_id, integration=integration)
            )
        else:
            result = asyncio.run(
                v2all_continuous_zoho(user_id, integration=integration)
            )

        return {"status": "completed", "user_id": user_id, "result": result}

    except Exception as exc:
        if self.request.retries < 5:
            _release_lock = False  # keep lock held so a concurrent sync cannot start
            raise self.retry(exc=exc, countdown=backoff(self.request.retries), max_retries=5)
        logger.error("umail_sync permanently failed for user %s: %s", user_id, exc)
        raise

    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()
        if _release_lock:
            release_user_lock(user_id)


@new_celery.task(bind=True, name="webhook.umailSync")
def web_umail_sync(self, user_id, channel=None, integration=None):

    _release_lock = True  # hold lock during retries; release only on final success/failure

    try:
        mailbox_setting = check_mailbox(user_id)
        if not mailbox_setting:
            return {"status": "skipped", "user_id": user_id, "reason": "mailbox_disabled"}

        if channel == "google":
            result = asyncio.run(v2all_continuous(user_id, integration=integration))
        elif channel == "microsoft":
            result = asyncio.run(
                v2all_continuous_outlook(user_id, integration=integration)
            )
        else:
            result = asyncio.run(
                v2all_continuous_zoho(user_id, integration=integration)
            )

        return {"status": "completed", "user_id": user_id, "result": result}

    except Exception as exc:
        if self.request.retries < 5:
            _release_lock = False  # keep lock held so a concurrent sync cannot start
            raise self.retry(exc=exc, countdown=backoff(self.request.retries), max_retries=5)
        logger.error("web_umail_sync permanently failed for user %s: %s", user_id, exc)
        raise

    finally:
        if _release_lock:
            release_user_lock(user_id)


@new_celery.task(bind=True, name="umail_helper.delayed_trigger")
def delayed_trigger(self, user_email, history_id, channel=None, integration=None):
    import time
    from utils.delay_mails import DelayTrigger

    # print(f"inside delayed_trigger, cahnnel : {channel}")

    lock_key = f"umail_delayed_lock:{user_email}"
    # Try to acquire lock for 10 minutes
    acquired = run_async(lock_client.set(lock_key, "1", ex=LOCK_TTL, nx=True))
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
        trigger.trigger(
            user_email, history_id, channel=channel, integration=integration
        )
        return {"status": "completed", "user_email": user_email}
    except Exception as exc:
        countdown = backoff(self.request.retries)
        raise self.retry(exc=exc, countdown=countdown, max_retries=5)
    finally:
        # Always release lock at the end so new task can start
        asyncio.run(lock_client.delete(lock_key))


# ------------ REPLY EMBEDDING TASK FOR AI ASSISTANT --------------#


async def enqueue_user_task(user_id, payload):
    """
    Add a new embedding task for a user to their Redis queue.
    """
    key = f"{QUEUE_PREFIX}{user_id}"
    await lock_client.rpush(key, json.dumps(payload))
    # logger.info(
    #     f"📩 Queued embedding for user {user_id} (queue size: {lock_client.llen(key)})"
    # )

    # Trigger worker to process if not already running
    process_user_queue.delay(user_id)


def exponential_backoff(retries, base=2, cap=300):
    """Return exponential backoff in seconds (min delay 1s, max 300s)."""
    return min(base**retries + uniform(0, 1), cap)


@new_celery.task(bind=True, name="embedding.process_user_queue")
async def process_user_queue(self, user_id):
    """
    Process all embedding tasks for a user sequentially (FIFO).
    Automatically retries failed tasks and keeps strict ordering.
    """
    key = f"{QUEUE_PREFIX}{user_id}"
    lock_key = f"{key}:lock"

    # Acquire a distributed lock per user (avoid two workers running the same queue)
    got_lock = await lock_client.set(lock_key, "1", ex=LOCK_TTL)
    if not got_lock:
        logger.info(f"⏸ Queue for user {user_id} already being processed.")
        return

    try:
        while True:
            raw_task = await lock_client.lpop(key)
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
                await client.embed_json_file_for_reply(
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
                    await lock_client.lpush(key, json.dumps(payload))
                else:
                    logger.error(
                        f"💀 Max retries reached for conv_id: {conversation_id}"
                    )
    finally:
        await lock_client.delete(lock_key)
        logger.info(f"🔓 Released lock for user {user_id}")


@celery.task(bind=True, name="tasks.send_bulk_emails")
async def send_bulk_emails(self, user_id: str, email_count: int, receiver_email: str):
    """
    Send multiple AI-generated emails for a specific user.
    """
    from services.gmail_service import GmailService
    from services.automate_service import AutoMateService
    import random
    from utils.normal import EMAIL_TITLES, extract_subject_from_html

    # Lock user to avoid multiple parallel bulk sends
    if not acquire_user_lock(user_id):
        return {
            "status": "locked",
            "message": f"Bulk email task already running for user {user_id}",
        }

    try:
        ai = AutoMateService(userid=user_id)
        gmail = GmailService(user_id=user_id)

        sent = 0
        failed = 0

        for i in range(email_count):

            # Pick random title
            rand_title = random.choice(EMAIL_TITLES)

            # Generate email body (HTML)
            email_body_html = await ai.create_custom_email_body(
                user_input=f"Write a short memo/news update about {rand_title} with 200 - 300 words and it must have a title included in <title> tag ",
            )
            # print("emmail_body_html", type(email_body_html))

            # Extract subject from HTML (or fallback)
            subject = extract_subject_from_html(
                email_body_html, fallback=f"News Update: {rand_title}"
            )

            # Send the email
            try:
                gmail.send_email(
                    receipent_emails=receiver_email,
                    subject=subject,
                    body_text=email_body_html["email_body_html"],
                )
                sent += 1
            except Exception as send_err:
                failed += 1
                # print(f"Email send failed ({i}):", send_err)

        return {
            "status": "completed",
            "user_id": user_id,
            "total_requested": email_count,
            "sent": sent,
            "failed": failed,
        }

    except Exception as exc:
        countdown = min(2**self.request.retries, 300)
        raise self.retry(exc=exc, countdown=countdown, max_retries=5)

    finally:
        release_user_lock(user_id)


def generate_execution_id():
    return "exec_" + datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")


def is_workflow_running(userid, filename):
    playbook_id, config_path, subagent_id = returnconfigandpath(userid)
    config = read_json_from_s3(config_path) or {}

    for pb in config.get(userid, {}).get("playbooklist", []):
        if pb["name"] == filename:
            return pb.get("runtime", {}).get("is_running", False)
    return False


def try_acquire_workflow_lock(userid, filename, execution_id):
    playbook_id, config_path, subagent_id = returnconfigandpath(userid)
    config = read_json_from_s3(config_path) or {}

    if userid not in config:
        return False

    for pb in config[userid].get("playbooklist", []):
        if pb.get("name") == filename:
            runtime = pb.setdefault("runtime", {})

            if runtime.get("is_running"):
                return False

            runtime.update(
                {
                    "is_running": True,
                    "current_execution_id": execution_id,
                    "last_run_at": datetime.utcnow().isoformat(),
                }
            )
            break
    else:
        return False

    local_path = f"/tmp/{userid}_playbooksconfig.json"
    with open(local_path, "w") as f:
        json.dump(config, f, indent=2)

    upload_any_file(
        file_path=local_path,
        user_id=userid,
        file_name=config_path,
        type="workflow",
    )
    os.remove(local_path)
    return True


# def update_playbook_runtime(userid, filename, runtime_updates):
#     playbook_id, config_path, subagent_id = returnconfigandpath(userid)
#     config = read_json_from_s3(config_path) or {}

#     if userid not in config:
#         return False

#     for pb in config[userid].get("playbooklist", []):
#         if pb.get("name") == filename:
#             pb.setdefault("runtime", {}).update(runtime_updates)
#             pb.setdefault("status", runtime_updates["last_execution_status"])
#             break
#     else:
#         return False

#     local_path = f"/tmp/{userid}_playbooksconfig.json"
#     with open(local_path, "w") as f:
#         json.dump(config, f, indent=2)

#     upload_any_file(
#         file_path=local_path,
#         user_id=userid,
#         file_name=config_path,
#         type="workflow",
#     )
#     os.remove(local_path)
#     return True


def update_playbook_runtime(userid, filename, runtime_updates):
    playbook_id, config_path, subagent_id = returnconfigandpath(userid)
    config = read_json_from_s3(config_path) or {}

    if userid not in config:
        return False

    playbooks = config.get(userid, {}).get("playbooklist", [])
    found = False

    for pb in playbooks:
        if pb.get("name") == filename:
            found = True

            # Ensure runtime exists
            pb.setdefault("runtime", {})

            # Update runtime fields
            for k, v in runtime_updates.items():
                if k != "status":
                    pb["runtime"][k] = v

            # Update status ONLY if explicitly provided
            if "status" in runtime_updates:
                pb["status"] = runtime_updates["status"]

            break

    if not found:
        return False

    local_path = f"/tmp/{userid}_playbooksconfig.json"
    with open(local_path, "w") as f:
        json.dump(config, f, indent=2)

    upload_any_file(
        file_path=local_path,
        user_id=userid,
        file_name=config_path,
        type="workflow",
    )

    os.remove(local_path)
    return True


@celery.task(bind=True, max_retries=3, name="tasks.workflow_scheduler")
def run_scheduled_job(self, userid, filename, contacts, uniquekey):
    from services.workflow_service import WorkflowRunnerV2
    import asyncio

    execution_id = generate_execution_id()

    # 🔒 Lock check
    if not try_acquire_workflow_lock(userid, filename, execution_id):
        update_playbook_runtime(
            userid,
            filename,
            {"status": "skipped"},
        )
        return {"status": "skipped", "reason": "workflow_already_running"}

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        # 🔹 Mark as running
        update_playbook_runtime(
            userid,
            filename,
            {
                "status": "running",
                "current_execution_id": execution_id,
                "is_running": True,
            },
        )

        runner = WorkflowRunnerV2(
            userid=userid,
            filename=filename,
            contacts=contacts,
            testing=False,
            execution_id=execution_id,
            execution_unique_key=uniquekey,
        )

        result = loop.run_until_complete(runner.execute())

        # ✅ Success
        update_playbook_runtime(
            userid,
            filename,
            {
                "status": "success",
                "last_execution_id": execution_id,
            },
        )

        return {
            "status": "completed",
            "execution_id": execution_id,
            "result": result,
        }

    except Exception as e:
        # ❌ Failure
        update_playbook_runtime(
            userid,
            filename,
            {
                "status": "failed",
                "last_execution_id": execution_id,
                "last_error": str(e),
            },
        )
        raise

    finally:
        # 🧹 Cleanup only (NO status change here)
        update_playbook_runtime(
            userid,
            filename,
            {
                "is_running": False,
                "current_execution_id": None,
            },
        )
        loop.close()


@celery.task(bind=True, max_retries=3, name="tasks.workflow_schedule_single")
def run_scheduled_step_job(self, userid, filename, stepid):
    from services.workflow_service import WorkflowRunnerV2

    try:
        runner = WorkflowRunnerV2(userid=userid, filename=filename, testing=False)

        result = asyncio.run(
            runner.execute_from_text_input(
                step_id=stepid, user_input=f"execute the step {stepid}"
            )
        )

        return result

    except Exception as e:
        # print("error", e)
        raise


@celery.task(bind=True, max_retries=3, name="tasks.lance_embedding")
async def run_lance_embedding(self, user_id, batch_count, lance_folder):
    try:

        client = UmailLanceClient(user_id)
        token = current_user_id.set(user_id)

        try:
            await client.embed_both_json_and_plain(lance_folder)
            folder_path = os.path.join(pathconfig.basepath, "messages", user_id)
            if os.path.exists(folder_path):
                shutil.rmtree(folder_path)
                # print(f"🗑️ Deleted folder and contents: {folder_path}")
            # else:
            # print(f"⚠️ Folder not found: {folder_path}")
            return {"status": "done", "batch": batch_count, "folder": lance_folder}

        finally:
            current_user_id.reset(token)

    except Exception as exc:
        countdown = 2**self.request.retries
        raise self.retry(exc=exc, countdown=countdown)


@new_celery.task(bind=True, name="tasks.next_month_emails")
def next_monthemails(self, user_id, lastmsgdate):

    try:
        if not acquire_user_lock(user_id):
            # Lock exists → task is running or within TTL
            logger.info(
                "get_all_messages Task already running currently for  %s", user_id
            )
            return {
                "message": "Task already running or recently triggered",
                "user_id": user_id,
            }  # Too Many Requests

        result = asyncio.run(fetchnextmonthmails(user_id, startDate=lastmsgdate))
        return {"status": "completed", "user_id": user_id, "result": result}
    except Exception as exc:
        countdown = backoff(self.request.retries)
        raise self.retry(exc=exc, countdown=countdown, max_retries=5)
    finally:
        # always release lock at end so new task can start
        release_user_lock(user_id)


@new_celery.task(bind=True, name="tasks.run_scrape_links")
def run_back_scrape(self, url, user_id, level):
    from training.scrape.fast_multilevel_scraper import run_scrapper_links

    try:
        if not acquire_scrape_lock(user_id, url):
            return {
                "status": "already_running",
                "user_id": user_id,
                "url": url,
            }

        result = run_async(run_scrapper_links(url=url, user_id=user_id, level=level))

        # MUST be JSON serializable
        return result

    except Exception as exc:
        raise self.retry(
            exc=exc,
            countdown=backoff(self.request.retries),
            max_retries=5,
        )

    finally:
        release_scrape_lock(user_id)


@celery.task(bind=True, max_retries=3, name="tasks.schedule_app")
def run_schedule_app(self, userid, app_id):
    from apiConnector.helpers import _execute_app_internal, is_schedule_active

    # 🛑 HARD STOP if user disabled it
    if not is_schedule_app_active(app_id):
        return "SKIPPED — schedule disabled by user"

    try:
        return _execute_app_internal(app_id, userid)
    except Exception as e:
        self.retry(exc=e, countdown=5)


@celery.task(bind=True, max_retries=3, name="tasks.schedule_app_endpoint")
def run_schedule_app_endpoint(self, userid, endpoint_id, context=None):
    from apiConnector.helpers import (
        _execute_endpoint_internal,
        is_schedule_active,
        completed_endpoint_schedule,
    )

    # 🛑 HARD STOP if user disabled it
    if not is_schedule_active(endpoint_id):
        return "SKIPPED — schedule disabled by user"

    try:
        result = _execute_endpoint_internal(endpoint_id, userid, context)
        if result:
            completed_endpoint_schedule(endpoint_id=endpoint_id)
        return result
    except Exception as e:
        self.retry(exc=e, countdown=5)


@celery.task(bind=True, max_retries=3, name="tasks.run_endpoint_interval")
def run_endpoint_interval(self, userid, endpoint_id, interval_seconds, stop_key=None):
    """
    Executes endpoint and reschedules itself.
    stop_key: optional unique key to check if this interval should stop
    """
    from services.scheduler_service import APIConnectorScheduler
    from apiConnector.helpers import _execute_endpoint_internal

    # Check if user disabled this schedule
    if stop_key and asyncio.run(APIConnectorScheduler.is_schedule_disabled(stop_key)):
        return {"stopped": True}

    try:
        # Execute the endpoint
        _execute_endpoint_internal(endpoint_id, userid)
    except Exception as e:
        self.retry(exc=e, countdown=5)

    # Reschedule self
    self.apply_async(
        args=[userid, endpoint_id, interval_seconds, stop_key],
        countdown=interval_seconds,
    )


@celery.task(bind=True, max_retries=3, name="tasks.trigger_runbooks_api")
def trigger_runbooks_api_task(self, user_id, app_id, endpoint_id, record):
    import asyncio

    print("🔥 TASK STARTED")
    lock_key = f"runbook_trigger_lock:{user_id}:{endpoint_id}"

    try:
        # 🔒 Acquire lock (avoid duplicate execution)
        acquired = asyncio.run(lock_client.set(lock_key, "1", ex=LOCK_TTL))
        if not acquired:
            return {"status": "skipped", "reason": "already_running"}
        print("🚀 running trigger_runbooks_for_api_response from task ")
        # 🚀 Run your async method
        result = run_async(
            trigger_runbooks_for_api_response(
                user_id=user_id,
                app_id=app_id,
                endpoint_id=endpoint_id,
                record=record,
            )
        )

        return {
            "status": "completed",
            "user_id": user_id,
            "endpoint_id": endpoint_id,
            "result": result,
        }

    except Exception as e:
        # 🔁 Retry with exponential backoff
        countdown = min(2**self.request.retries, 300)
        raise self.retry(exc=e, countdown=countdown)

    finally:
        # 🔓 Always release lock
        asyncio.run(lock_client.delete(lock_key))


@celery.task(bind=True, max_retries=3, name="tasks.create_playbook_runbook_task")
def create_playbook_runbook_task(self, user_id, playbook_id, rb_pb_id, session_id=None):
    import asyncio
    from runbook.helper import trigger_runbook_from_playbook
    from websockets_custom.ws_instance import ws_service

    print(f"🔥 PLAYBOOK RUNBOOK TASK STARTED {playbook_id} amd {rb_pb_id}")

    lock_key = f"playbook_runbook_lock:{user_id}:{playbook_id}"

    try:
        # 🔒 Acquire lock
        acquired = asyncio.run(lock_client.set(lock_key, "1", ex=LOCK_TTL))
        if not acquired:
            return {"status": "skipped", "reason": "already_running"}

        # 🚀 SEND START MESSAGE (only if session_id exists)
        if session_id:
            asyncio.run(
                ws_service.emit(
                    user_id=user_id,
                    message="📊 Report generation started...",
                    scope="session",
                    session_id=session_id,
                    msg_type="info",
                    stage="start",
                    progress=0,
                    feature="runbook_execution",
                )
            )

        print("🚀 Running trigger_runbook_from_playbook inside Celery task")

        # 🚀 Execute async workflow
        result = run_async(
            trigger_runbook_from_playbook(
                playbook_id=playbook_id,
                user_id=user_id,
                runbook_id=rb_pb_id,
            )
        )

        # ✅ SEND COMPLETION MESSAGE
        if session_id:
            asyncio.run(
                ws_service.emit(
                    user_id=user_id,
                    message="✅ Report generation completed",
                    scope="session",
                    session_id=session_id,
                    msg_type="success",
                    stage="completed",
                    progress=100,
                    feature="runbook_execution",
                )
            )

        return {
            "status": "completed",
            "user_id": user_id,
            "playbook_id": playbook_id,
            "result": result,
        }

    except Exception as e:
        countdown = min(2**self.request.retries, 300)
        raise self.retry(exc=e, countdown=countdown)

    finally:
        # 🔓 Release lock
        asyncio.run(lock_client.delete(lock_key))


@celery.task(bind=True, max_retries=3, name="tasks.trigger_scheduled_playbook_runbook")
def trigger_scheduled_playbook_runbook_task(self, user_id, runbook_id):
    import asyncio

    print("🔥 PLAYBOOK TASK STARTED")
    lock_key = f"scheduled_playbook_lock:{user_id}:{runbook_id}"

    try:
        # 🔒 Acquire lock
        acquired = asyncio.run(lock_client.set(lock_key, "1", ex=LOCK_TTL))
        if not acquired:
            return {"status": "skipped", "reason": "already_running"}

        print("🚀 Running trigger_scheduled_playbook_runbook")

        # 🚀 Run async function
        result = run_async(
            trigger_scheduled_playbook_runbook(
                user_id=user_id,
                runbook_id=runbook_id,
            )
        )

        return {
            "status": "completed",
            "type": "playbook",
            "user_id": user_id,
            "runbook_id": runbook_id,
            "result": result,
        }

    except Exception as e:
        countdown = min(2**self.request.retries, 300)
        raise self.retry(exc=e, countdown=countdown)

    finally:
        # 🔓 Release lock
        asyncio.run(lock_client.delete(lock_key))


@celery.task(bind=True, max_retries=3, name="tasks.trigger_scheduled_api_runbook")
def trigger_scheduled_api_runbook_task(self, user_id, runbook_id):
    import asyncio

    print("🔥 API TASK STARTED")
    lock_key = f"scheduled_api_lock:{user_id}:{runbook_id}"

    try:
        # 🔒 Acquire lock
        acquired = asyncio.run(lock_client.set(lock_key, "1", ex=LOCK_TTL))
        if not acquired:
            return {"status": "skipped", "reason": "already_running"}

        print("🚀 Running trigger_scheduled_api_runbook")

        # 🚀 Run async function
        result = run_async(
            trigger_scheduled_api_runbook(
                user_id=user_id,
                runbook_id=runbook_id,
            )
        )

        return {
            "status": "completed",
            "type": "api",
            "user_id": user_id,
            "runbook_id": runbook_id,
            "result": result,
        }

    except Exception as e:
        countdown = min(2**self.request.retries, 300)
        raise self.retry(exc=e, countdown=countdown)

    finally:
        # 🔓 Release lock
        asyncio.run(lock_client.delete(lock_key))


@celery.task(bind=True, max_retries=3, name="tasks.analyze_runbook_questions")
def analyze_runbook_questions_task(
    self,
    user_id,
    questions,
    reference_source,
    reference_main_source,
    runbook,
):
    import asyncio

    print("🔥 ANALYZE TASK STARTED")

    lock_key = f"analyze_lock:{user_id}:{runbook.get('runbook_id')}"

    try:
        # 🔒 Acquire lock (prevent duplicate execution)
        acquired = asyncio.run(lock_client.set(lock_key, "1", ex=LOCK_TTL))
        if not acquired:
            return {"status": "skipped", "reason": "already_running"}

        print("🚀 Running analyze_questions_with_references")

        # 🚀 Run async function
        result = run_async(
            analyze_questions_with_references(
                questions,
                reference_source,
                reference_main_source,
                user_id,
                runbook,
            )
        )

        if not result:
            print("⚠️ No analysis results generated")

        return {
            "status": "completed",
            "type": "analysis",
            "user_id": user_id,
            "runbook_id": runbook.get("runbook_id"),
            "total_questions": len(questions),
            "result": result,
        }

    except Exception as e:
        print(f"❌ Error in analyze task: {e}")

        countdown = min(2**self.request.retries, 300)
        raise self.retry(exc=e, countdown=countdown)

    finally:
        # 🔓 Always release lock
        asyncio.run(lock_client.delete(lock_key))
