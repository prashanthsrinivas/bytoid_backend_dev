import os
import json
import time
import uuid
import threading
import asyncio
from typing import Optional, Dict, Any, List, Tuple

from credits_route.route import Credits
from db.rds_db import connect_to_rds
from flask import request, jsonify, Blueprint
from utils.fireworkzz import (
    get_coder_fire_response,
    get_think_fire_response_image,
    get_fireworks_response2,
    get_think_fire_response_og,
)
from utils.s3_utils import upload_any_file_and_get_url, upload_think_image_and_get_url
from io import BytesIO
import base64
from io import BytesIO
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

bytoid_dev_pro_bp = Blueprint("bytoid_dev_pro", __name__, url_prefix="/bytoid-pro-dev")

FIREWORKS_URL = "https://api.fireworks.ai/inference/v1/chat/completions"
MODEL_QWEN3_VL = "accounts/fireworks/models/qwen3-vl-235b-a22b-thinking"

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

    user_id = request.form.get("user_id")
    if not user_id:
        return jsonify({"ok": False, "error": "user_id is required"}), 400

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
async def fireworks_think_og():
    db = connect_to_rds()
    credits = Credits(db)

    # Safely read from JSON or multipart
    json_body = request.get_json(silent=True) or {}
    user_id = json_body.get("user_id") or request.form.get("user_id")
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
def handle_audio_fallback():
    try:
        json_body = request.get_json(silent=True) or {}
        audio_file = request.files.get("audio")
        user_id = json_body.get("user_id") or request.form.get("user_id")

        if not audio_file or not user_id:
            return jsonify({"error": "Missing audio or user_id"}), 400

        # print("audio received")
        transcript = handle_audio(audio_file, user_id)  # returns string

        return {"transcript": transcript} if transcript else {"transcript": ""}

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bytoid_dev_pro_bp.route("/bytoidpro/think", methods=["POST"])
async def fireworks_think():
    db = connect_to_rds()
    credits = Credits(db)

    try:
        # 1️⃣ Read request JSON or form
        json_body = request.get_json(silent=True) or {}
        user_id = json_body.get("user_id") or request.form.get("user_id")
        message = json_body.get("message") or request.form.get("message")
        chat_id = json_body.get("chat_id", "")

        if not user_id or not message:
            return jsonify({"error": "user_id and message required"}), 400

        if not chat_id:
            chat_id = str(uuid.uuid4())

        db.begin()  # start transaction
        total_input_chars = 8000

        if not await credits.has_ai_credits(
            total_chars=total_input_chars, user_id=user_id
        ):
            db.rollback()
            return "INSUFFICIENT", 402

        uploaded_image_urls = []

        # 2️⃣ Handle file uploads if any
        files = request.files.getlist("file") or request.files.getlist("image")
        for file in files:
            uploaded_image_urls.append(
                upload_think_image_and_get_url(
                    user_id=user_id,
                    file_obj=file,
                    filename=file.filename,
                    content_type=file.content_type,
                )
            )

        # 3️⃣ Handle base64 images from JSON/form
        image_urls_payload = json_body.get("image_urls") or []
        if not image_urls_payload and request.form.getlist("image_urls"):
            image_urls_payload = request.form.getlist("image_urls")

        for image_data in image_urls_payload:
            if image_data.startswith("data:"):
                try:
                    header, encoded = image_data.split(",", 1)
                    content_type = header.split(";")[0].replace("data:", "")
                    ext = content_type.split("/")[-1]
                    binary = base64.b64decode(encoded)
                    file_obj = BytesIO(binary)

                    uploaded_image_urls.append(
                        upload_think_image_and_get_url(
                            user_id=user_id,
                            file_obj=file_obj,
                            filename=f"upload.{ext}",
                            content_type=content_type,
                        )
                    )
                except Exception as e:
                    db.rollback()
                    return (
                        jsonify({"error": f"Failed to process base64 image: {str(e)}"}),
                        400,
                    )
            elif image_data.startswith("http://") or image_data.startswith("https://"):
                uploaded_image_urls.append(image_data)  # valid URL

        # check for files
        uploaded_file_urls = []

        file_urls_payload = json_body.get("file_urls") or []
        if not file_urls_payload and request.form.getlist("file_urls"):
            file_urls_payload = request.form.getlist("file_urls")

        for file_data in file_urls_payload:
            if file_data.startswith("data:"):
                try:
                    header, encoded = file_data.split(",", 1)
                    content_type = header.split(";")[0].replace("data:", "")
                    ext = content_type.split("/")[-1]
                    if (
                        ext
                        == "vnd.openxmlformats-officedocument.wordprocessingml.document"
                    ):
                        ext = "docx"
                    elif (
                        ext
                        == "vnd.openxmlformats-officedocument.presentationml.presentation"
                    ):
                        ext = "pptx"
                    elif ext == "vnd.openxmlformats-officedocument.spreadsheetml.sheet":
                        ext = "xlsx"
                    binary = base64.b64decode(encoded)
                    file_obj = BytesIO(binary)

                    uploaded_file_urls.append(
                        upload_any_file_and_get_url(
                            user_id=user_id,
                            file_obj=file_obj,
                            filename=f"upload.{ext}",
                            content_type=content_type,
                        )
                    )
                except Exception as e:
                    db.rollback()
                    return (
                        jsonify({"error": f"Failed to process base64 file: {str(e)}"}),
                        400,
                    )
            elif file_data.startswith("http://") or file_data.startswith("https://"):
                uploaded_file_urls.append(file_data)  # valid URL

        has_images = bool(uploaded_image_urls)
        has_files = bool(uploaded_file_urls)

        # ---- Create job ID and job entry ----
        job_id = str(uuid.uuid4())
        jobs = load_jobs()
        jobs[job_id] = {
            "user_id": user_id,
            "status": "PENDING",
            "progress": 0,
            "result": None,
        }
        save_jobs(jobs)

        # ---- Background task function (non-async version) ----

        def task_wrapper():

            # Create new DB connection for background task
            task_db = connect_to_rds()
            task_credits = Credits(task_db)

            # print(f"uploaded_file_urls : {uploaded_file_urls}")
            # print(f"uploaded_image_urls : {uploaded_image_urls}")

            loop = asyncio.new_event_loop()  # create one loop for everything
            asyncio.set_event_loop(loop)

            try:
                jobs = load_jobs()
                jobs[job_id]["status"] = "PROCESSING"
                save_jobs(jobs)

                task_db.begin()

                # create context
                lance = Bytoid_pro_lance(user_id)
                context = loop.run_until_complete(lance.get_context(message, chat_id))

                # Call AI model

                if has_images and has_files:
                    response = loop.run_until_complete(
                        mixed_response(
                            user_message=message,
                            role="system",
                            user_id=user_id,
                            file_url=uploaded_file_urls,
                            image_url=uploaded_image_urls,
                            credits=task_credits,
                            context=context,
                        )
                    )

                elif has_images:
                    response = loop.run_until_complete(
                        get_think_fire_response_image(
                            user_message=message,
                            role="system",
                            user_id=user_id,
                            image_url=uploaded_image_urls,
                            credits=task_credits,
                            context=context,
                        )
                    )
                elif has_files:
                    response = loop.run_until_complete(
                        process_large_book(
                            user_message=message,
                            role="system",
                            user_id=user_id,
                            file_url=uploaded_file_urls,
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
                            credits=task_credits,
                            file_url=[],
                            context=context,
                        )
                    )

                if response == "INSUFFICIENT":
                    task_db.rollback()
                    jobs = load_jobs()
                    jobs[job_id]["status"] = "FAILED"
                    jobs[job_id]["error"] = "Insufficient credits"
                    save_jobs(jobs)
                    return

                task_db.commit()

                # --- save to lance ----- #

                chat = build_chat(
                    chat_id=chat_id,
                    user_message=message,
                    assistant_message=response,
                    user_files=uploaded_file_urls,
                    user_images=uploaded_image_urls,
                )

                s3_response = save_conversation_to_json(user_id, chat_id, chat)
                lance_response = loop.run_until_complete(lance.insert_to_lance(chat))
                # print(f"lance_reponse : {lance_response}")

                # --- save to jobs_file ---- #
                jobs = load_jobs()
                jobs[job_id]["status"] = "COMPLETED"
                jobs[job_id]["progress"] = 100
                jobs[job_id]["result"] = response
                jobs[job_id]["chat_id"] = chat_id
                save_jobs(jobs)

            except Exception as e:
                task_db.rollback()
                jobs = load_jobs()
                jobs[job_id]["status"] = "FAILED"
                jobs[job_id]["error"] = str(e)
                jobs[job_id]["chat_id"] = chat_id
                save_jobs(jobs)
            # print(f"Job {job_id} failed: {str(e)}")

            finally:
                loop.close()  # close loop only here
                task_db.close()

        # ✅ Start background thread
        thread = threading.Thread(target=task_wrapper, daemon=True)
        thread.start()

        # ---- Immediately return job ID ----
        return jsonify({"status": "PROCESSING", "job_id": job_id}), 202

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        db.close()


@bytoid_dev_pro_bp.route("/bytoidpro/think/status", methods=["POST"])
def check_job_status():
    json_body = request.get_json()
    job_id = json_body.get("job_id")
    if not job_id:
        return jsonify({"error": "job_id is required"}), 400

    jobs = load_jobs()
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    # Return relevant fields
    response = {
        "job_id": job_id,
        "status": job.get("status"),
        "progress": job.get("progress"),
        "result": job.get("result"),
        "error": job.get("error"),
        "user_id": job.get("user_id"),
        "chat_id": job.get("chat_id"),
    }
    return jsonify(response)


@bytoid_dev_pro_bp.route("/bytoidpro/chat_history", methods=["POST"])
def chat_history():
    json_body = request.get_json()
    user_id = json_body.get("user_id")
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


@bytoid_dev_pro_bp.route("/bytoidpro/get_a_chat", methods=["POST"])
def get_a_chat():
    json_body = request.get_json()
    user_id = json_body.get("user_id")
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
def delete_table():

    body = request.json or {}
    user_id = body.get("user_id")

    lance = Bytoid_pro_lance(user_id)
    reponse = lance.delete_table()
    return jsonify(reponse)


@bytoid_dev_pro_bp.route("/bytoid/coder", methods=["POST"])
async def fireworks_coder():
    db = connect_to_rds()
    credits = Credits(db)

    body = request.json or {}
    user_id = body.get("user_id")
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
