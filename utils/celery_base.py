import os
from dotenv import load_dotenv

# Must run before any module-level imports that call load_dotenv() + os.getenv()
# (e.g. db/lance_db_service.py, utils/app_configs.py, db/rds_db.py)
load_dotenv("/home/ec2-user/bytoid_python/.env")
load_dotenv()  # fallback for local dev

from datetime import datetime
from random import uniform
import shutil
import time
import traceback
from apiConnector.helpers import is_schedule_app_active
from cust_helpers import pathconfig
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
from microsoft_route.get_microsoft_emails import v2all_continuous_outlook, v2all_continuous_teams
from db.rds_db import connect_to_rds
from request_context import current_user_id
from playbook.helperzz import returnconfigandpath
from utils.s3_utils import read_json_from_s3, upload_any_file
from zoho_routes.routes import v2all_continuous_zoho

# from umail_helper.auto_rep import autoReplyhelper

logger = get_task_logger(__name__)

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
            # Bound broker connect so a synchronous send_task() from the web tier
            # (e.g. interactive step scheduling) fails fast instead of hanging the
            # request when the broker is unreachable.
            broker_connection_timeout=5,
            broker_use_ssl={
                "ssl_cert_reqs": "none"
            },  # required for AWS ElastiCache TLS
            redis_backend_use_ssl={"ssl_cert_reqs": "none"},
            worker_hijack_root_logger=False,
            include=["utils.celery_base"],
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
            # Bound broker connect so a synchronous send_task() from the web tier
            # (e.g. interactive step scheduling) fails fast instead of hanging the
            # request when the broker is unreachable.
            broker_connection_timeout=5,
            broker_use_ssl={
                "ssl_ca_certs": "/home/ec2-user/bytoid_python/awsredis.pem",  # 👈 ADD HERE
                "ssl_cert_reqs": "required",
            },
            redis_backend_use_ssl={
                "ssl_ca_certs": "/home/ec2-user/bytoid_python/awsredis.pem",  # 👈 ADD HERE
                "ssl_cert_reqs": "required",
            },
            worker_hijack_root_logger=False,
            include=["utils.celery_base"],
        )

    return celery


new_celery = make_celery()
celery = new_celery  # <— important for CLI

# Nightly heal of the statement↔tracker reverse-lookup graph (02:00 UTC).
# Harmless if no beat process is running; the task is also callable on demand
# via `celery -A utils.celery_base call tasks.reconcile_statement_tracker_refs`.
try:
    from celery.schedules import crontab

    new_celery.conf.beat_schedule = {
        **(getattr(new_celery.conf, "beat_schedule", None) or {}),
        "reconcile-statement-tracker-refs-nightly": {
            "task": "tasks.reconcile_statement_tracker_refs",
            "schedule": crontab(hour=2, minute=0),
        },
        # Heal the policy_hub_documents metadata index against S3 nightly.
        # Offset 30 minutes after the tracker reconcile to spread DB load.
        "reconcile-policy-hub-documents-nightly": {
            "task": "tasks.reconcile_policy_hub_documents",
            "schedule": crontab(hour=2, minute=30),
        },
    }
    # Platform-wide AI governance scan — opt-in (heavy: drives Bedrock per user).
    # Enable by setting AI_GOVSCAN_BEAT_ENABLED=true on the beat host so it never
    # fires in dev by accident.  Sunday 03:00 UTC, off-peak.
    if os.getenv("AI_GOVSCAN_BEAT_ENABLED", "").lower() == "true":
        new_celery.conf.beat_schedule["ai-governance-platform-scan-weekly"] = {
            "task": "tasks.ai_governance.scan_platform",
            "schedule": crontab(hour=3, minute=0, day_of_week=0),
            "kwargs": {
                "modes": ["tabular", "prompt", "raget", "guardrail"],
                "sample_size": 200,
                "max_questions": 10,
                "run_id": None,
                "user_limit": None,
                "started_by": "system",
            },
        }
except Exception as _beat_exc:  # pragma: no cover - beat config is best-effort
    logger.warning("could not register reconcile beat schedule: %s", _beat_exc)


def backoff(retries):
    return min(2**retries, 300)


@new_celery.task(bind=True, name="tasks.reconcile_statement_tracker_refs")
def reconcile_statement_tracker_refs(self):
    """Rebuild the statement↔tracker RDS graph from S3 tracker blobs."""
    from tab_tracker.reconcile import reconcile_all

    return reconcile_all()


@new_celery.task(bind=True, name="tasks.reconcile_policy_hub_documents")
def reconcile_policy_hub_documents(self):
    """Heal policy_hub_documents against S3 (upsert missing, delete orphaned)."""
    from policy_hub.reconcile_doc_index import reconcile_all

    return reconcile_all()


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
        elif channel == "teams":
            result = asyncio.run(
                v2all_continuous_teams(user_id, integration=integration)
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
            except Exception:
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
def run_scheduled_step_job(self, userid, filename, stepid, uniquekey=None):
    # uniquekey is passed by SchedulerService.schedule_single_step (4 args). It
    # was missing from this signature, so every scheduled single step raised
    # "takes 4 positional arguments but 5 were given" in the worker and never
    # actually executed. Accept it (optional for backwards compatibility).
    from services.workflow_service import WorkflowRunnerV2

    try:
        runner = WorkflowRunnerV2(userid=userid, filename=filename, testing=False)

        result = asyncio.run(
            runner.execute_from_text_input(
                step_id=stepid, user_input=f"execute the step {stepid}"
            )
        )

        return result

    except Exception:
        # print("error", e)
        raise


@celery.task(bind=True, max_retries=0, name="tasks.playground_step")
def run_playground_step_job(
    self, owner_user_id, filename, userinput, testing, ws_user_id, session_id, job_id
):
    """Run the interactive playground option/step pipeline off the request path.

    The browser reaches the API through REST API Gateway, which caps a
    synchronous request at ~29s and cannot stream. The option-selection pipeline
    (check_input_tone → intent → trigger → route → execute) makes several
    sequential LLM calls, so a full-execution step like "Generate Meeting
    Invitation Body" routinely blows past that and the request 503s. So the work
    runs here and the result is pushed to the browser over the WebSocket
    (ws_service), keyed by job_id, instead of streamed back over HTTP.
    """
    from services.workflow_service import WorkflowRunnerV2
    from credits_route.route import Credits
    from websockets_custom.ws_instance import ws_service

    def _emit(result):
        msg_type = "error" if (result or {}).get("log_status") == "error" else "success"
        try:
            asyncio.run(
                ws_service.emit(
                    user_id=ws_user_id,
                    message=(result or {}).get("response_message", ""),
                    # scope="job" is delivered only to this session's connection
                    # and is NOT toasted/added to the notification bell by the
                    # frontend — it is purely indexed by job_id for the awaiter.
                    scope="job",
                    session_id=session_id,
                    job_id=job_id,
                    msg_type=msg_type,
                    stage="done",
                    feature="playground_step",
                    extra={"result": result},
                )
            )
        except Exception as emit_exc:
            logger.error("playground_step emit failed job=%s: %s", job_id, emit_exc)

    db = connect_to_rds()
    try:
        credits = Credits(db)
        with WorkflowRunnerV2(
            userid=owner_user_id,
            filename=filename,
            testing=testing,
            db=db,
            credits=credits,
        ) as runner:
            result = asyncio.run(runner.check_input_tone(user_input=userinput))

        if result is None:
            result = {
                "response_message": "Error processing option.",
                "wf_single_runner": False,
                "log_status": "error",
            }
        _emit(result)

    except Exception as e:
        logger.error(
            "playground_step failed job=%s user=%s: %s",
            job_id,
            owner_user_id,
            e,
            exc_info=True,
        )
        _emit(
            {
                "response_message": f"Error processing option: {type(e).__name__}: {e}",
                "wf_single_runner": False,
                "log_status": "error",
            }
        )
    finally:
        try:
            db.close()
        except Exception:
            pass


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
    from apiConnector.helpers import _execute_app_internal

    # 🛑 HARD STOP if user disabled it
    if not is_schedule_app_active(app_id):
        return "SKIPPED — schedule disabled by user"

    try:
        # _execute_app_internal is async — must be awaited or it no-ops.
        return asyncio.run(_execute_app_internal(app_id, userid))
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
        # _execute_endpoint_internal is async — must be awaited or it no-ops.
        result = asyncio.run(_execute_endpoint_internal(endpoint_id, userid, context))
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

    # Without a stop_key there is no way to ever disable this interval, so it
    # would self-reschedule forever. Refuse to start an ungovernable loop.
    if not stop_key:
        logger.warning(
            "run_endpoint_interval called without stop_key (endpoint %s) — "
            "not scheduling an unstoppable interval",
            endpoint_id,
        )
        return {"stopped": True, "reason": "missing_stop_key"}

    # Check if user disabled this schedule
    if asyncio.run(APIConnectorScheduler.is_schedule_disabled(stop_key)):
        return {"stopped": True}

    try:
        # Execute the endpoint (async — must be awaited or it silently no-ops)
        asyncio.run(_execute_endpoint_internal(endpoint_id, userid))
    except Exception as e:
        # retry() raises, so the reschedule below is skipped on failure; that is
        # intentional — Celery re-runs this task and will reschedule on success.
        self.retry(exc=e, countdown=5)

    # Reschedule self
    self.apply_async(
        args=[userid, endpoint_id, interval_seconds, stop_key],
        countdown=interval_seconds,
    )


@celery.task(bind=True, max_retries=3, name="tasks.schedule_azure_app")
def run_schedule_azure_app(self, userid, app_id):
    from azure_integration.helpers import _execute_azure_app_internal

    try:
        return asyncio.run(_execute_azure_app_internal(app_id, userid))
    except Exception as e:
        self.retry(exc=e, countdown=5)


@celery.task(bind=True, max_retries=3, name="tasks.schedule_azure_app_endpoint")
def run_schedule_azure_app_endpoint(self, userid, endpoint_id, context=None):
    from azure_integration.helpers import _execute_azure_endpoint_internal

    try:
        return asyncio.run(_execute_azure_endpoint_internal(endpoint_id, userid))
    except Exception as e:
        self.retry(exc=e, countdown=5)


@celery.task(bind=True, max_retries=3, name="tasks.schedule_gcp_app")
def run_schedule_gcp_app(self, userid, app_id):
    from gcp_integration.helpers import _execute_gcp_app_internal

    try:
        return asyncio.run(_execute_gcp_app_internal(app_id, userid))
    except Exception as e:
        self.retry(exc=e, countdown=5)


@celery.task(bind=True, max_retries=3, name="tasks.schedule_gcp_app_endpoint")
def run_schedule_gcp_app_endpoint(self, userid, endpoint_id, context=None):
    from gcp_integration.helpers import _execute_gcp_endpoint_internal

    try:
        return asyncio.run(_execute_gcp_endpoint_internal(endpoint_id, userid))
    except Exception as e:
        self.retry(exc=e, countdown=5)


@celery.task(bind=True, max_retries=3, name="tasks.run_gcp_endpoint_interval")
def run_gcp_endpoint_interval(self, userid, endpoint_id, interval_seconds, stop_key=None):
    from services.scheduler_service import GCPAPIConnectorScheduler
    from gcp_integration.helpers import _execute_gcp_endpoint_internal

    if stop_key and asyncio.run(GCPAPIConnectorScheduler.is_schedule_disabled(stop_key)):
        return {"stopped": True}

    try:
        asyncio.run(_execute_gcp_endpoint_internal(endpoint_id, userid))
    except Exception as e:
        self.retry(exc=e, countdown=5)

    self.apply_async(
        args=[userid, endpoint_id, interval_seconds, stop_key],
        countdown=interval_seconds,
    )


@celery.task(bind=True, max_retries=3, name="tasks.run_azure_endpoint_interval")
def run_azure_endpoint_interval(self, userid, endpoint_id, interval_seconds, stop_key=None):
    """
    Executes an Azure endpoint and reschedules itself.
    stop_key: optional unique key to check if this interval should stop.
    """
    from services.scheduler_service import AzureAPIConnectorScheduler
    from azure_integration.helpers import _execute_azure_endpoint_internal

    if stop_key and asyncio.run(AzureAPIConnectorScheduler.is_schedule_disabled(stop_key)):
        return {"stopped": True}

    try:
        asyncio.run(_execute_azure_endpoint_internal(endpoint_id, userid))
    except Exception as e:
        self.retry(exc=e, countdown=5)

    self.apply_async(
        args=[userid, endpoint_id, interval_seconds, stop_key],
        countdown=interval_seconds,
    )


@celery.task(bind=True, max_retries=3, name="tasks.schedule_aws_app_endpoint")
def run_schedule_aws_app_endpoint(self, userid, endpoint_id, context=None):
    from aws_integration.helpers import _execute_aws_endpoint_internal

    try:
        return asyncio.run(_execute_aws_endpoint_internal(endpoint_id, userid))
    except Exception as e:
        self.retry(exc=e, countdown=5)


@celery.task(bind=True, max_retries=3, name="tasks.run_aws_endpoint_interval")
def run_aws_endpoint_interval(self, userid, endpoint_id, interval_seconds, stop_key=None):
    """
    Executes an AWS endpoint and reschedules itself.
    stop_key: optional unique key to check if this interval should stop.
    """
    from services.scheduler_service import AWSAPIConnectorScheduler
    from aws_integration.helpers import _execute_aws_endpoint_internal

    if stop_key and asyncio.run(AWSAPIConnectorScheduler.is_schedule_disabled(stop_key)):
        return {"stopped": True}

    try:
        asyncio.run(_execute_aws_endpoint_internal(endpoint_id, userid))
    except Exception as e:
        self.retry(exc=e, countdown=5)

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


# ── Workflow email task ───────────────────────────────────────────────────────


@celery.task(
    bind=True,
    name="tasks.send_workflow_email",
    autoretry_for=(Exception,),
    retry_kwargs={"max_retries": 6},
    retry_backoff=True,
    retry_backoff_max=900,
    retry_jitter=True,
)
def send_workflow_email(
    self,
    workflow_id: str,
    event_type: str,
    recipient_email: str,
    recipient_user_id: str,
    template_name: str,
    context: dict,
):
    """Send a workflow notification email via Graph API raw-MIME path.

    Retries up to 6 times with exponential backoff (max 900s). On terminal
    failure, writes to workflow_email_dlq and posts an in-app notification.
    """
    import os
    import requests as _requests
    from services.workflow_notifications_service import render_email, build_multipart_mime

    from_email = os.getenv("WORKFLOW_SENDER_EMAIL", "noreply@bytoid.ai")
    sender_user_id = os.getenv("WORKFLOW_SENDER_GRAPH_USER_ID", "")

    try:
        subject, html_body, text_body = render_email(template_name, context)
        raw_mime = build_multipart_mime(from_email, recipient_email, subject, html_body, text_body)

        # Graph API raw-MIME sendMail
        access_token = _get_graph_access_token()
        url = f"https://graph.microsoft.com/v1.0/users/{sender_user_id}/sendMail"
        resp = _requests.post(
            url,
            data=raw_mime,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "text/plain",
            },
            timeout=30,
        )
        if resp.status_code not in (200, 202):
            raise Exception(f"Graph sendMail returned {resp.status_code}: {resp.text[:500]}")

    except Exception as exc:
        if self.request.retries >= self.max_retries:
            # Terminal failure — write DLQ and post in-app notification
            _workflow_email_terminal_failure(
                workflow_id, recipient_user_id, recipient_email, template_name, context, str(exc)
            )
        raise self.retry(exc=exc)


def _get_graph_access_token() -> str:
    """Obtain a Microsoft Graph access token for the sender account."""
    import os
    import requests as _requests

    tenant_id = os.getenv("MICROSOFT_TENANT_ID", "")
    client_id = os.getenv("MICROSOFT_CLIENT_ID", "")
    client_secret = os.getenv("MICROSOFT_CLIENT_SECRET", "")

    resp = _requests.post(
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _workflow_email_terminal_failure(
    workflow_id: str,
    recipient_user_id: str,
    recipient_email: str,
    template_name: str,
    context: dict,
    error: str,
):
    """On terminal DLQ failure: update DLQ row, post in-app notification."""
    from db.rds_db import connect_to_rds as _rds
    conn = _rds()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE workflow_email_dlq SET status='permanent_failure', last_error=%s "
                "WHERE workflow_id=%s AND recipient=%s AND template_name=%s AND status='pending' "
                "ORDER BY created_at DESC LIMIT 1",
                (error[:2000], workflow_id, recipient_email, template_name),
            )
        conn.commit()
    except Exception as exc:
        print(f"[workflow] DLQ update failed: {exc}")
    finally:
        conn.close()

    # In-app notification to recipient
    from services.workflow_notifications_service import _insert_raw_in_app_notification
    _insert_raw_in_app_notification(
        user_id=recipient_user_id,
        workflow_id=workflow_id,
        workflow_state=None,
        doc_type=context.get("doc_type"),
        doc_id=context.get("doc_title"),
        message="Email delivery failed for a workflow notification. Please check your workflow inbox.",
        action_required=False,
    )


# ── Legacy policy migration tasks ────────────────────────────────────────────


@celery.task(
    bind=True,
    name="tasks.migrate_legacy_policy",
    max_retries=5,
    default_retry_delay=30,
)
def migrate_legacy_policy(self, key: str, data: dict, dry_run: bool = False):
    """Celery worker: migrate one legacy policy YAML to V2 structured schema."""
    import asyncio as _asyncio
    from policy_hub.migrate_legacy_policies import _migrate_one_policy

    try:
        loop = _asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(_migrate_one_policy(key, data, dry_run=dry_run))
        finally:
            loop.close()
        if result.get("status") == "migration_failed":
            logger.error("migrate_legacy_policy: %s", result)
        else:
            logger.info("migrate_legacy_policy: %s", result)
        return result
    except Exception as exc:
        countdown = min(2 ** self.request.retries, 600)
        logger.warning(
            "migrate_legacy_policy retrying (attempt %d): %s — retry in %ds",
            self.request.retries,
            exc,
            countdown,
        )
        raise self.retry(exc=exc, countdown=countdown)


@celery.task(
    bind=True,
    name="tasks.replicate_template_to_org",
    max_retries=3,
)
def replicate_template_to_org(self, user_id: str, doc_type: str = "all", dry_run: bool = False):
    """Sync all V2 policies for user_id to the current template definition.

    doc_type: "all" applies each type's template to that type's documents.
    Returns: { processed, updated, skipped, errors, dry_run, doc_type }
    """
    import uuid as _uuid
    from datetime import datetime as _datetime, timezone as _timezone

    from policy_hub.replicate import _replicate_sections
    from policy_hub.templates import validate as _validate
    from policy_hub.migrate_legacy_policies import _render_sections_to_html
    from policy_hub.routes import _write_yaml_to_s3, _sync_statements
    from utils.s3_utils import s3bucket, load_yaml_from_s3, S3_BUCKET  # S3_BUCKET used for list_objects only

    types_to_process = (
        ["policy", "procedure", "standard"] if doc_type == "all" else [doc_type]
    )

    prefix = f"{user_id}/policies/"
    try:
        response = s3bucket().list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix)
    except Exception as e:
        logger.error("replicate_template: S3 list failed for %s: %s", user_id, e)
        return {"error": str(e), "dry_run": dry_run, "doc_type": doc_type}

    keys = [
        obj["Key"]
        for obj in response.get("Contents", [])
        if obj["Key"].endswith(".yaml")
        and "/raw/" not in obj["Key"]
        and "/jobs/" not in obj["Key"]
    ]

    processed, updated, skipped, errors = 0, 0, 0, []

    for key in keys:
        try:
            data = load_yaml_from_s3(key)
            if not data:
                skipped += 1
                continue

            policy_type = data.get("type")
            if policy_type not in types_to_process:
                skipped += 1
                continue

            existing_sections = data.get("sections", [])
            new_sections = _replicate_sections(existing_sections, policy_type, user_id=user_id)

            existing_ids = [s["id"] for s in existing_sections]
            new_ids = [s["id"] for s in new_sections]
            if existing_ids == new_ids:
                processed += 1
                skipped += 1
                continue

            processed += 1

            rendered_html = _render_sections_to_html(new_sections)
            vr = _validate(rendered_html, policy_type, user_id=user_id)
            new_validation_status = "ok" if vr.ok else "needs_review"

            if not dry_run:
                data["sections"] = new_sections
                data["validation_status"] = new_validation_status
                data["etag"] = str(_uuid.uuid4())
                data["updated_at"] = _datetime.now(_timezone.utc).isoformat()
                _write_yaml_to_s3(key, data)

                try:
                    import asyncio as _asyncio
                    loop = _asyncio.new_event_loop()
                    _sync_statements(data, user_id, policy_type, loop)
                    loop.close()
                except Exception as se:
                    logger.warning(
                        "replicate_template: LanceDB sync failed for %s: %s", key, se
                    )

            updated += 1

        except Exception as e:
            logger.error("replicate_template: failed for key %s: %s", key, e)
            errors.append({"key": key, "error": str(e)})

    result = {
        "processed": processed,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "dry_run": dry_run,
        "doc_type": doc_type,
    }
    logger.info("replicate_template completed: %s", result)
    return result


@celery.task(
    bind=True,
    name="tasks.apply_template_to_org",
    max_retries=2,
)
def apply_template_to_org(self, user_id: str, doc_type: str, dry_run: bool = False):
    """AI-driven recategorisation of every policy of *doc_type* into the user's custom template.

    For each policy YAML:
      - Build prompt from existing sections/content + new template.
      - Call Fireworks; parse JSON; validate.
      - Apply (or just count, when dry_run) and sync statements to LanceDB.
    Returns counts: { processed, updated, skipped, errors, dry_run, doc_type }.
    """
    import asyncio as _asyncio
    import uuid as _uuid
    from datetime import datetime as _datetime, timezone as _timezone

    from policy_hub.template_storage import load_custom_template
    from policy_hub.templates import validate as _validate
    from policy_hub.routes import (
        _apply_template_prompt,
        _parse_llm_json,
        _render_upload_sections_to_html,
        _sync_statements,
        _write_yaml_to_s3,
    )
    from utils.fireworkzz import get_fireworks_response2
    from utils.s3_utils import S3_BUCKET, load_yaml_from_s3, s3bucket

    custom_sections = load_custom_template(user_id, doc_type)
    if not custom_sections:
        return {
            "error": "No custom template saved. Edit and save the template first.",
            "dry_run": dry_run,
            "doc_type": doc_type,
        }

    prefix = f"{user_id}/policies/"
    try:
        response = s3bucket().list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix)
    except Exception as e:
        logger.error("apply_template: S3 list failed for %s: %s", user_id, e)
        return {"error": str(e), "dry_run": dry_run, "doc_type": doc_type}

    keys = [
        obj["Key"]
        for obj in response.get("Contents", [])
        if obj["Key"].endswith(".yaml")
        and "/raw/" not in obj["Key"]
        and "/jobs/" not in obj["Key"]
    ]

    processed, updated, skipped, errors = 0, 0, 0, []

    for key in keys:
        try:
            data = load_yaml_from_s3(key)
            if not data:
                skipped += 1
                continue

            if data.get("type") != doc_type:
                skipped += 1
                continue

            has_sections = bool(data.get("sections"))
            has_content = bool(data.get("content"))
            if not has_sections and not has_content:
                skipped += 1
                continue

            processed += 1

            prompt = _apply_template_prompt(data, custom_sections)
            loop = _asyncio.new_event_loop()
            try:
                raw = loop.run_until_complete(
                    get_fireworks_response2(
                        user_id=user_id,
                        user_message=prompt,
                        role="user",
                        credits=None,
                        temp=0.0,
                    )
                )
            except Exception as exc:
                loop.close()
                logger.warning("apply_template: LLM call failed for %s: %s", key, exc)
                errors.append({"key": key, "error": f"LLM call failed: {exc}"})
                continue

            if raw == "INSUFFICIENT":
                loop.close()
                errors.append({"key": key, "error": "Insufficient credits"})
                # Hard stop on credits exhaustion — do not burn remaining policies.
                break

            structured = _parse_llm_json(raw) if raw else None
            if not (isinstance(structured, dict) and isinstance(structured.get("sections"), list)):
                loop.close()
                logger.warning(
                    "apply_template: LLM returned unparseable JSON for %s — skipping",
                    key,
                )
                errors.append({"key": key, "error": "LLM unparseable JSON"})
                continue

            new_sections = structured["sections"]
            rendered_html = _render_upload_sections_to_html(new_sections)
            vr = _validate(rendered_html, doc_type, user_id=user_id)
            new_validation_status = "ok" if vr.ok else "needs_review"

            if dry_run:
                loop.close()
                updated += 1
                continue

            data["template_version"] = 1
            data["sections"] = new_sections
            if isinstance(structured.get("metadata"), dict) and structured["metadata"]:
                data["metadata"] = structured["metadata"]
            data["validation_status"] = new_validation_status
            data["etag"] = str(_uuid.uuid4())
            data["updated_at"] = _datetime.now(_timezone.utc).isoformat()

            _write_yaml_to_s3(key, data)
            try:
                _sync_statements(data, user_id, doc_type, loop)
            except Exception as se:
                logger.warning("apply_template: LanceDB sync failed for %s: %s", key, se)
            finally:
                loop.close()

            updated += 1
        except Exception as exc:
            logger.error("apply_template: failed for %s: %s", key, exc)
            errors.append({"key": key, "error": str(exc)})

    result = {
        "processed": processed,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "dry_run": dry_run,
        "doc_type": doc_type,
    }
    logger.info("apply_template completed: %s", result)
    return result


@celery.task(
    bind=True,
    name="tasks.migrate_legacy_policies_org",
    max_retries=3,
)
def migrate_legacy_policies_org(self, user_id: str, dry_run: bool = False, policy_id: str | None = None):
    """Orchestrator: list legacy policies for an org and dispatch in chunks.

    Uses a Redis lock (migration_lock:{user_id}) so two concurrent invocations
    for the same org never overlap.
    """
    from utils.app_configs import MIGRATION_CHUNK_SIZE
    from policy_hub.migrate_legacy_policies import list_legacy_policy_keys
    from utils.s3_utils import load_yaml_from_s3

    lock_key = f"migration_lock:{user_id}"
    lock_ttl = 600  # 10 minutes; refreshed each chunk

    # Acquire per-org migration lock
    acquired = run_async(lock_client.set(lock_key, "1", ex=lock_ttl, nx=True))
    if not acquired:
        logger.warning("migrate_legacy_policies_org: lock held for org %s — skipping", user_id)
        return {"status": "locked", "user_id": user_id}

    try:
        if policy_id:
            key = f"{user_id}/policies/{policy_id}.yaml"
            data = load_yaml_from_s3(key)
            if not data:
                return {"status": "not_found", "policy_id": policy_id}
            keys = [(key, data)]
        else:
            raw_keys = list_legacy_policy_keys(user_id)
            keys = []
            for k in raw_keys:
                d = load_yaml_from_s3(k)
                if d:
                    keys.append((k, d))

        total = len(keys)
        logger.info("migrate_legacy_policies_org: %d policies to migrate for %s", total, user_id)

        results = []
        for chunk_start in range(0, total, MIGRATION_CHUNK_SIZE):
            chunk = keys[chunk_start: chunk_start + MIGRATION_CHUNK_SIZE]

            # Dispatch chunk synchronously (blocking) using a chord-like pattern:
            # run each task and wait before the next chunk.
            chunk_results = []
            for k, d in chunk:
                res = migrate_legacy_policy.apply(args=[k, d, dry_run])
                chunk_results.append(res.get(timeout=300))

            results.extend(chunk_results)

            # Refresh lock TTL after each chunk
            run_async(lock_client.expire(lock_key, lock_ttl))

        ok = sum(1 for r in results if isinstance(r, dict) and r.get("status") == "ok")
        failed = sum(1 for r in results if isinstance(r, dict) and r.get("status") == "migration_failed")
        dry = sum(1 for r in results if isinstance(r, dict) and r.get("status") == "dry_run")
        logger.info(
            "migrate_legacy_policies_org done: total=%d ok=%d failed=%d dry=%d",
            total, ok, failed, dry,
        )
        return {"status": "done", "total": total, "ok": ok, "failed": failed, "dry_run": dry}

    except Exception as exc:
        logger.error("migrate_legacy_policies_org error for %s: %s", user_id, exc)
        raise
    finally:
        run_async(lock_client.delete(lock_key))


# ─────────────────────────────────────────────────────────────
# Unit Test Results — backend test runner Celery tasks.
# Each task delegates to tests_routes/runners.py, which handles the
# subprocess invocation, output normalization, and persistence.
# ─────────────────────────────────────────────────────────────


@new_celery.task(bind=True, name="tasks.tests.run_backend_unit")
def run_backend_unit(self, run_id):
    from tests_routes.runners import run_pytest_category

    return run_pytest_category(
        "backend_unit", run_id, pytest_targets=["tests/unit/"], timeout_seconds=600
    )


@new_celery.task(bind=True, name="tasks.tests.run_backend_integration")
def run_backend_integration(self, run_id):
    from tests_routes.runners import run_pytest_category

    return run_pytest_category(
        "backend_integration",
        run_id,
        pytest_targets=["testing/"],
        timeout_seconds=900,
    )


@new_celery.task(bind=True, name="tasks.tests.run_backend_regression")
def run_backend_regression(self, run_id):
    from tests_routes.runners import run_pytest_category

    return run_pytest_category(
        "backend_regression",
        run_id,
        pytest_targets=["tests/regression/", "testing/"],
        timeout_seconds=1800,
    )


@new_celery.task(bind=True, name="tasks.tests.run_backend_crypto")
def run_backend_crypto(self, run_id):
    from tests_routes.runners import run_pytest_category

    return run_pytest_category(
        "backend_crypto",
        run_id,
        pytest_targets=["tests/test_encryption.py"],
        timeout_seconds=300,
    )


@new_celery.task(bind=True, name="tasks.tests.run_backend_load")
def run_backend_load(self, run_id, target_url, users, spawn_rate, run_time):
    from tests_routes.runners import run_locust_category

    return run_locust_category(
        "backend_load",
        run_id,
        scenario="steady",
        target_url=target_url,
        users=users,
        spawn_rate=spawn_rate,
        run_time=run_time,
    )


@new_celery.task(bind=True, name="tasks.tests.run_backend_stress")
def run_backend_stress(self, run_id, target_url, max_users, spawn_rate, run_time):
    from tests_routes.runners import run_locust_category

    return run_locust_category(
        "backend_stress",
        run_id,
        scenario="stress",
        target_url=target_url,
        users=max_users,
        spawn_rate=spawn_rate,
        run_time=run_time,
    )


@new_celery.task(bind=True, name="tasks.tests.run_backend_performance")
def run_backend_performance(self, run_id, target_url, run_time):
    from tests_routes.runners import run_locust_category

    # Performance probe: low concurrency, short duration, captures p95/p99.
    return run_locust_category(
        "backend_performance",
        run_id,
        scenario="performance",
        target_url=target_url,
        users=10,
        spawn_rate=2,
        run_time=run_time,
    )


@celery.task(bind=True, max_retries=2, name="tasks.lazy_reencrypt_runbook_rows")
def lazy_reencrypt_runbook_rows_task(self, user_id: str, rows: list, fields: list):
    """Re-encrypt plaintext runbook fields in LanceDB as a background Celery task.

    Replaces the asyncio.create_task() pattern that caused 'Task destroyed but
    pending' errors when the per-request event loop closed before the work finished.
    """
    from db.lance_db_service import LanceDBServer

    async def _run():
        server = LanceDBServer()
        await server._lazy_reencrypt_runbook_rows(user_id, rows, tuple(fields))

    try:
        asyncio.run(_run())
    except Exception as exc:
        logger.warning("lazy_reencrypt_runbook_rows_task failed user=%s: %s", user_id, exc)


# Discover ai_governance Celery tasks so workers register them on startup.
from ai_governance import tasks as _ai_gov_tasks  # noqa: E402, F401
