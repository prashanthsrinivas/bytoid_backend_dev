import time
import threading
from typing import Dict, Any

from utils.normal import parse_composite_user_id
from utils.app_configs import IS_DEV
from credits_route.route import Credits
from db.rds_db import connect_to_rds
from flask import request, jsonify, Blueprint
from utils.permission_required import permission_required_body
import logging
from utils.fireworkzz import (
    get_coder_fire_response,
    get_think_fire_response_image,
    get_think_fire_response_og,
)
from utils.s3_utils import upload_any_file_and_get_url, upload_think_image_and_get_url
from .bytoid_pro_helpers import (
    load_jobs,
    save_jobs,
    process_large_book,
    mixed_response,
    get_think_fire_response_file,
    build_chat,
    save_conversation_to_json,
    handle_audio,
)
from .bytoid_pro_lance import Bytoid_pro_lance
from utils.permission_required import permission_required_body

bytoid_dev_pro_bp = Blueprint("bytoid_dev_pro", __name__, url_prefix="/bytoid-pro-dev")
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG if IS_DEV else logging.INFO)
FIREWORKS_URL = "https://api.fireworks.ai/inference/v1/chat/completions"
MODEL_QWEN3_VL = "moonshotai.kimi-k2.5"

# ---- In-memory, ephemeral image store (RAM only; no disk, no AWS) ----
# token -> {"bytes": b"...", "content_type": "image/jpeg", "exp": <epoch>, "hits": int}
_TMP_IMAGES: Dict[str, Dict[str, Any]] = {}
_TMP_LOCK = threading.Lock()

ALLOWED_IMAGE_MIME = {"image/png", "image/jpeg", "image/webp"}
MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # 8 MB
TMP_TTL_SECONDS = 120  # expire quickly (adjust 30-180)
DELETE_AFTER_FIRST_FETCH = False  # set True if you want one-time fetch behavior


def _now() -> float:
    return time.time()


@bytoid_dev_pro_bp.route("/upload", methods=["POST"])
@permission_required_body("intake.bytoid_pro")
def bytoid_pro_upload():
    """
    Universal upload (RAM-only → S3).
    Supports:
      - Any file type
      - Multiple files
      - Mixed uploads

    Request (multipart/form-data):
      - user_id (required)
      - files (one or many)

    Response:
      {
        ok: true,
        files: [
          {
            filename: "...",
            content_type: "...",
            size: 12345,
            url: "https://..."
          }
        ]
      }
    """

    base_user_id = request.form.get("user_id")
    if not base_user_id:
        return jsonify({"ok": False, "error": "user_id is required"}), 400
    logged_in_user_id, user_id = parse_composite_user_id(base_user_id)

    files = request.files.getlist("files")
    if not files:
        return jsonify({"ok": False, "error": "files are required"}), 400

    uploaded = []

    for file in files:
        if not file or not file.filename:
            continue

        url = upload_any_file_and_get_url(
            user_id=user_id,
            file_obj=file,
            filename=file.filename,
            content_type=file.mimetype or "application/octet-stream",
        )

        # print(f"url : {url}")

        uploaded.append(
            {
                "filename": file.filename,
                "content_type": file.mimetype,
                "size": file.content_length,
                "url": url,
            }
        )

    if not uploaded:
        return jsonify({"ok": False, "error": "no valid files uploaded"}), 400

    return jsonify({"ok": True, "files": uploaded}), 200


@bytoid_dev_pro_bp.route("/bytoidpro/think_og", methods=["POST"])
@permission_required_body("intake.bytoid_pro")
async def fireworks_think_og():
    db = connect_to_rds()
    credits = Credits(db)

    # Safely read from JSON or multipart
    json_body = request.get_json(silent=True) or {}
    base_user_id = json_body.get("user_id") or request.form.get("user_id")
    logged_in_user_id, user_id = parse_composite_user_id(base_user_id)
    message = json_body.get("message") or request.form.get("message")
    image_links = json_body.get("file_links") or request.form.get("file_links")

    # print(message)

    if not user_id or not message:
        db.close()
        return jsonify({"error": "user_id and message required"}), 400

    try:
        db.begin()  # 🔐 TRANSACTION START
        total_input_chars = 8000

        if not await credits.has_ai_credits(
            total_chars=total_input_chars,
            user_id=user_id,
        ):
            db.rollback()
            return "INSUFFICIENT", 402

        image_url = ""

        # Case 1: File upload
        file = request.files.get("file") or request.files.get("image_url")
        if file:
            image_url = upload_think_image_and_get_url(
                user_id=user_id,
                file_obj=file,
                filename=file.filename,
                content_type=file.content_type,
            )
        else:

            image_url = image_links or json_body.get("image_url", "")

        # Call model
        response = await get_think_fire_response_og(
            user_message=message,
            role="system",
            user_id=user_id,
            image_url=image_url,
            credits=credits,
        )

        # Handle credit failure
        if response == "INSUFFICIENT":
            db.rollback()
            return jsonify({"error": "Insufficient credits"}), 402

        # Commit on success
        db.commit()

        # Return response
        return jsonify(
            {
                "model": "think",
                "image_used": bool(image_url),
                "image_url": image_url,
                "response": response,
            }
        )

    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        db.close()


@bytoid_dev_pro_bp.route("/bytoidpro/handle_audio_fallback", methods=["POST"])
@permission_required_body("intake.bytoid_pro")
def handle_audio_fallback():
    try:
        json_body = request.get_json(silent=True) or {}
        audio_file = request.files.get("audio")
        base_user_id = json_body.get("user_id") or request.form.get("user_id")
        logged_in_user_id, user_id = parse_composite_user_id(base_user_id)

        if not audio_file or not user_id:
            return jsonify({"error": "Missing audio or user_id"}), 400

        # print("audio received")
        transcript = handle_audio(audio_file, user_id)  # returns string

        return {"transcript": transcript} if transcript else {"transcript": ""}

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bytoid_dev_pro_bp.route("/bytoidpro/think", methods=["POST"])
@permission_required_body("intake.bytoid_pro")
async def bytoidpro_think():
    import json, time, uuid, base64, asyncio, threading
    from websockets_custom.ws_instance import ws_service, msg_builder_main
    from runbook.utils import send

    db = connect_to_rds()
    credits = Credits(db)
    msg_builder = msg_builder_main

    # -------------------------------
    # ✅ MESSAGE CONFIG (NEW)
    # -------------------------------
    DEFAULT_MESSAGES = {
        "stage": "thinking",
        "start": "Starting your request...",
        "validating": "Checking inputs and preparing data...",
        "processing_files": "Processing uploaded files...",
        "fetching_context": "Understanding your request context...",
        "model_selection": "Choosing the best processing strategy...",
        "executing": "Working on your request...",
        "saving": "Saving results...",
        "completed": "Done! Your response is ready.",
        "failed": "Something went wrong while processing your request.",
    }

    try:
        # -------------------------------
        # 1️⃣ Read request
        # -------------------------------
        json_body = request.get_json(silent=True) or {}
        base_user_id = json_body.get("user_id") or request.form.get("user_id")
        logged_in_user_id, user_id = parse_composite_user_id(base_user_id)
        message = json_body.get("message") or request.form.get("message")
        chat_id = json_body.get("chat_id") or str(uuid.uuid4())
        session_id = json_body.get("session_id") or request.form.get("session_id")

        progress_messages = json_body.get("progress_messages") or DEFAULT_MESSAGES
        stage = progress_messages.get("stage", "thinking")

        job_id = str(uuid.uuid4())

        async def emit(msg):
            if job_id and session_id:
                await send(ws_service, msg, user_id)

        if not user_id or not message:
            return jsonify({"error": "user_id and message required"}), 400

        db.begin()

        # -------------------------------
        # Emit START
        # -------------------------------
        await emit(
            msg_builder.job_progress(
                job_id,
                session_id,
                stage,
                progress_messages["start"],
                5,
            )
        )

        if not await credits.has_ai_credits(total_chars=8000, user_id=user_id):
            db.rollback()
            return "INSUFFICIENT", 402

        inline_images = []
        inline_files = []

        # -------------------------------
        # Emit VALIDATION
        # -------------------------------
        await emit(
            msg_builder.job_progress(
                job_id,
                session_id,
                stage,
                progress_messages["validating"],
                10,
            )
        )

        # -------------------------------
        # 2️⃣ Handle IMAGE files
        # -------------------------------
        files = request.files.getlist("file") or request.files.getlist("image")

        for file in files:
            binary = file.read()
            encoded = base64.b64encode(binary).decode("utf-8")
            content_type = file.content_type or "image/png"

            if not content_type.startswith("image/"):
                return jsonify({"error": "Only image files allowed"}), 400

            inline_images.append(f"data:{content_type};base64,{encoded}")

        # -------------------------------
        # 3️⃣ Inline images from JSON
        # -------------------------------
        image_urls_payload = json_body.get("image_urls") or request.form.getlist(
            "image_urls"
        )

        for image_data in image_urls_payload:
            if not image_data.startswith("data:image/"):
                return jsonify({"error": "Invalid image input"}), 400
            inline_images.append(image_data)

        # -------------------------------
        # 4️⃣ Inline FILES
        # -------------------------------
        file_urls_payload = json_body.get("file_urls") or request.form.getlist(
            "file_urls"
        )

        for file_data in file_urls_payload:
            if not file_data.startswith("data:"):
                return jsonify({"error": "Invalid file input"}), 400
            inline_files.append(file_data)

        # -------------------------------
        # Emit FILE PROCESSING
        # -------------------------------
        await emit(
            msg_builder.job_progress(
                job_id,
                session_id,
                stage,
                progress_messages["processing_files"],
                20,
            )
        )

        # -------------------------------
        # 5️⃣ VALIDATION
        # -------------------------------
        for img in inline_images:
            if not img.startswith("data:image/"):
                raise ValueError("Only base64 images allowed")

        for f in inline_files:
            if not f.startswith("data:"):
                raise ValueError("Only base64 files allowed")

        # -------------------------------
        # 6️⃣ Create Job
        # -------------------------------
        jobs = load_jobs()
        jobs[job_id] = {
            "user_id": user_id,
            "status": "PENDING",
            "progress": 0,
            "result": None,
        }
        save_jobs(jobs)

        # -------------------------------
        # 7️⃣ Background Task
        # -------------------------------
        def task_wrapper():
            task_db = connect_to_rds()
            task_credits = Credits(task_db)
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            try:
                jobs = load_jobs()
                jobs[job_id]["status"] = "PROCESSING"
                save_jobs(jobs)

                task_db.begin()

                # -------------------------------
                # Emit CONTEXT
                # -------------------------------
                loop.run_until_complete(
                    emit(
                        msg_builder.job_progress(
                            job_id,
                            session_id,
                            stage,
                            progress_messages["fetching_context"],
                            35,
                        )
                    )
                )

                lance = Bytoid_pro_lance(user_id)
                context = loop.run_until_complete(lance.get_context(message, chat_id))

                has_images = bool(inline_images)
                has_files = bool(inline_files)

                # -------------------------------
                # Emit MODEL SELECTION
                # -------------------------------
                loop.run_until_complete(
                    emit(
                        msg_builder.job_progress(
                            job_id,
                            session_id,
                            stage,
                            progress_messages["model_selection"],
                            50,
                        )
                    )
                )

                # -------------------------------
                # Model execution
                # -------------------------------
                loop.run_until_complete(
                    emit(
                        msg_builder.job_progress(
                            job_id,
                            session_id,
                            stage,
                            progress_messages["executing"],
                            60,
                        )
                    )
                )

                if has_images and has_files:
                    loop.run_until_complete(
                        emit(
                            msg_builder.job_progress(
                                job_id,
                                session_id,
                                stage,
                                "analyzing images and files",
                                70,
                            )
                        )
                    )
                    response = loop.run_until_complete(
                        mixed_response(
                            user_message=message,
                            role="system",
                            user_id=user_id,
                            image_url=inline_images,
                            file_url=inline_files,
                            credits=task_credits,
                            context=context,
                        )
                    )

                elif has_images:
                    loop.run_until_complete(
                        emit(
                            msg_builder.job_progress(
                                job_id,
                                session_id,
                                stage,
                                "analyzing image content",
                                70,
                            )
                        )
                    )
                    response = loop.run_until_complete(
                        get_think_fire_response_image(
                            user_message=message,
                            role="system",
                            user_id=user_id,
                            image_url=inline_images,
                            credits=task_credits,
                            context=context,
                        )
                    )

                elif has_files:
                    loop.run_until_complete(
                        emit(
                            msg_builder.job_progress(
                                job_id,
                                session_id,
                                stage,
                                "analyzing files content",
                                70,
                            )
                        )
                    )
                    response = loop.run_until_complete(
                        process_large_book(
                            user_message=message,
                            role="system",
                            user_id=user_id,
                            file_url=inline_files,
                            credits=task_credits,
                            context=context,
                        )
                    )

                else:
                    response = loop.run_until_complete(
                        get_think_fire_response_file(
                            user_message=message,
                            role="system",
                            user_id=user_id,
                            file_url=[],
                            credits=task_credits,
                            context=context,
                        )
                    )

                if response == "INSUFFICIENT":
                    task_db.rollback()
                    jobs[job_id]["status"] = "FAILED"
                    jobs[job_id]["error"] = "Insufficient credits"
                    save_jobs(jobs)
                    return

                task_db.commit()

                # -------------------------------
                # Emit SAVING
                # -------------------------------
                loop.run_until_complete(
                    emit(
                        msg_builder.job_progress(
                            job_id,
                            session_id,
                            stage,
                            progress_messages["saving"],
                            90,
                        )
                    )
                )

                chat = build_chat(
                    chat_id=chat_id,
                    user_message=message,
                    assistant_message=response,
                    user_files=[],
                    user_images=inline_images,
                )

                save_conversation_to_json(user_id, chat_id, chat)
                loop.run_until_complete(lance.insert_to_lance(chat))

                jobs[job_id].update(
                    {
                        "status": "COMPLETED",
                        "progress": 100,
                        "result": response,
                        "chat_id": chat_id,
                    }
                )
                save_jobs(jobs)

                # -------------------------------
                # Emit COMPLETED
                # -------------------------------
                loop.run_until_complete(
                    emit(
                        msg_builder.job_success(
                            job_id,
                            session_id,
                            progress_messages["completed"],
                        )
                    )
                )

            except Exception as e:
                task_db.rollback()
                jobs[job_id]["status"] = "FAILED"
                jobs[job_id]["error"] = str(e)
                jobs[job_id]["chat_id"] = chat_id
                save_jobs(jobs)

                loop.run_until_complete(
                    emit(
                        msg_builder.job_error(
                            job_id,
                            session_id,
                            progress_messages["failed"],
                        )
                    )
                )

            finally:
                loop.close()
                task_db.close()

        threading.Thread(target=task_wrapper, daemon=True).start()

        return (
            jsonify(
                {
                    "status": "PROCESSING",
                    "job_id": job_id,
                    "chat_id": chat_id,
                }
            ),
            202,
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        db.close()


@bytoid_dev_pro_bp.route("/bytoidpro/think/status", methods=["POST"])
@permission_required_body("intake.bytoid_pro")
def check_job_status():
    json_body = request.get_json()
    job_id = json_body.get("job_id")

    if not job_id:
        return jsonify({"error": "job_id is required"}), 400

    jobs = load_jobs()
    job = jobs.get(job_id)

    if not job:
        return jsonify({"error": "Job not found"}), 404

    response = {
        "job_id": job_id,
        "status": job.get("status"),
        "progress": job.get("progress"),
        "result": job.get("result"),
        "error": job.get("error"),
        "user_id": job.get("user_id"),
        "chat_id": job.get("chat_id"),
    }

    # ✅ DELETE JOB AFTER FINAL STATE IS READ
    if job.get("status") in ["COMPLETED", "FAILED"]:
        jobs.pop(job_id, None)
        save_jobs(jobs)

    return jsonify(response)


@bytoid_dev_pro_bp.route("/bytoidpro/chat_history", methods=["POST"])
@permission_required_body("intake.bytoid_pro")
def chat_history():
    try:
        json_body = request.get_json()
        base_user_id = json_body.get("user_id")
        logged_in_user_id, user_id = parse_composite_user_id(base_user_id)
        last_timestamp = json_body.get("last_timestamp", "")  # pagination pending

        lance = Bytoid_pro_lance(user_id)
        rows = lance.get_history(last_timestamp)

        first_user_message = {}
        last_activity = {}

        for r in rows:
            chat_id = r["chat_id"]

            # capture FIRST user message only once
            if r["role"] == "user" and chat_id not in first_user_message:
                first_user_message[chat_id] = r["content"]

            # track last activity timestamp (always overwritten)
            last_activity[chat_id] = r["timestamp"]

        # 2️⃣ Build list of dictionaries
        history = [
            {
                "chat_id": chat_id,
                "preview": first_user_message[chat_id],
                "timestamp": last_activity[chat_id],
            }
            for chat_id in first_user_message
        ]

        # 3️⃣ Order sidebar by most recent activity
        history.sort(key=lambda x: x["timestamp"], reverse=True)

        return history
    except Exception as e:
        logger.info("error %s", e)


@bytoid_dev_pro_bp.route("/bytoidpro/get_a_chat", methods=["POST"])
@permission_required_body("intake.bytoid_pro")
def get_a_chat():
    json_body = request.get_json()
    base_user_id = json_body.get("user_id")
    logged_in_user_id, user_id = parse_composite_user_id(base_user_id)
    chat_id = json_body.get("chat_id")

    try:
        lance = Bytoid_pro_lance(user_id)
        rows = lance.get_chat(chat_id)

        # Convert to dict
        chat_messages = [
            {
                "id": row["id"],
                "chat_id": row["chat_id"],
                "role": row["role"],
                "content": row["content"],
                "timestamp": row["timestamp"],
                "images": row["images"],
                "files": row["files"],
            }
            for row in rows
        ]

        # Sort by timestamp ascending
        chat_messages.sort(key=lambda x: x["timestamp"])
        return chat_messages

    except Exception as e:
        # print(f"Error fetching chat: {str(e)}")
        return []


@bytoid_dev_pro_bp.route("/bytoidpro/delete", methods=["POST"])
@permission_required_body("intake.bytoid_pro")
def delete_table():

    body = request.json or {}
    base_user_id = body.get("user_id")
    logged_in_user_id, user_id = parse_composite_user_id(base_user_id)

    lance = Bytoid_pro_lance(user_id)
    reponse = lance.delete_table()
    return jsonify(reponse)


@bytoid_dev_pro_bp.route("/bytoid/coder", methods=["POST"])
@permission_required_body("intake.bytoid_pro")
async def fireworks_coder():
    db = connect_to_rds()
    credits = Credits(db)

    body = request.json or {}
    base_user_id = body.get("user_id")
    logged_in_user_id, user_id = parse_composite_user_id(base_user_id)
    message = body.get("message")

    if not user_id or not message:
        db.close()
        return jsonify({"error": "user_id and message required"}), 400

    try:
        db.begin()  # 🔐 TRANSACTION START
        # total_input_chars = 5000
        # if not await credits.has_ai_credits(
        #     total_chars=total_input_chars,
        #     user_id=user_id,
        # ):
        #     db.rollback()
        #     return "INSUFFICIENT", 402

        response = await get_coder_fire_response(
            user_message=message,
            role="system",
            user_id=user_id,
            credits=credits,  # ✅ PASS CREDITS
        )

        if response == "INSUFFICIENT":  # ✅ FIXED
            db.rollback()
            return jsonify({"error": "Insufficient credits"}), 402

        db.commit()  # ✅ COMMIT ON SUCCESS

        return jsonify({"response": response})

    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        db.close()
