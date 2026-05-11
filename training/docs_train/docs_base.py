import asyncio
from datetime import datetime
import json
import os
import threading
import zipfile
from agent_route.Drive_downloader import (
    GetEmailandDriveService,
    Mediatorservice,
    get_main_service,
)
from agent_route.ag_helperzz import deletefilebasedData, process_and_update_yaml
from agent_route.lance_agent import LanceClient
from credits_route.route import Credits
from db.db_checkers import check_userid_valid
from db.rds_db import connect_to_rds
from flask import Blueprint, request, jsonify
from google_route.routes import get_token
from integrations.google_integration import get_integration_access_token
from utils.permission_required import permission_required_body
from playbook.background_worker import JobManager
from utils.base_logger import get_logger
from utils.normal import ensure_dir
from utils.s3_utils import load_yaml_from_s3, save_yaml_to_s3
from websockets_custom.ws_instance import ws_service, msg_builder_main
from werkzeug.utils import secure_filename
import uuid


logger = get_logger(__name__)
docs_agent_bps = Blueprint("agent_docs", __name__)
msg_builder = msg_builder_main


# ─────────────────────────────────────────────
# ZIP / ARCHIVE EXTRACTION HELPER
# ─────────────────────────────────────────────


def extract_archive_to_folder(file_path: str, dest_folder: str) -> list:
    """
    If file_path is a zip archive (detected by magic bytes), extract all contained
    files into dest_folder and delete the archive.
    Returns a list of extracted file paths.
    For non-archive files, returns [file_path] unchanged.
    """
    if zipfile.is_zipfile(file_path):
        extracted = []
        with zipfile.ZipFile(file_path, "r") as z:
            for name in z.namelist():
                if name.endswith("/"):
                    continue
                out_name = os.path.basename(name)
                if not out_name:
                    continue
                out_path = os.path.join(dest_folder, out_name)
                with z.open(name) as src, open(out_path, "wb") as dst:
                    dst.write(src.read())
                extracted.append(out_path)
        os.remove(file_path)
        return extracted
    return [file_path]


# ─────────────────────────────────────────────
# EXECUTE: process-drive
# ─────────────────────────────────────────────


async def execute_process_drive(data, job_id=None, session_id=None):
    from runbook.utils import send

    user_id = data.get("user_id")
    primary_provider = data.get("primary_provider")
    should_emit = bool(job_id and session_id)

    async def emit(msg):
        if should_emit:
            await send(ws_service, msg, user_id)

    db = connect_to_rds()
    credits = Credits(db=db)

    try:
        await emit(
            msg_builder.job_progress(
                job_id, session_id, "init", "Starting Drive processing...", 5
            )
        )

        if primary_provider:
            access_token = get_token(user_id, value=True, in_connection=db)
            actual_userid = user_id
        else:
            access_token, actual_userid = get_integration_access_token(
                user_id, "google"
            )

        if not access_token:
            await emit(
                msg_builder.job_error(
                    job_id, session_id, "Unable to fetch access token"
                )
            )
            return None

        user_service = GetEmailandDriveService(access_token)
        if not user_service:
            await emit(
                msg_builder.job_error(job_id, session_id, "Cannot access Google Drive")
            )
            return None

        await emit(
            msg_builder.job_progress(
                job_id,
                session_id,
                "download",
                "Downloading files from Google Drive...",
                20,
            )
        )

        all_paths, is_downloaded = Mediatorservice(data, actual_userid, user_service)

        if not is_downloaded or not all_paths:
            await emit(
                msg_builder.job_error(job_id, session_id, "Failed to download files")
            )
            return None

        await emit(
            msg_builder.job_progress(
                job_id,
                session_id,
                "download",
                f"Downloaded {len(all_paths)} file(s), queuing embedding...",
                50,
            )
        )

        folderpath = os.path.commonpath(all_paths)

        # Background thread handles embedding; passes job_id/session_id so it can
        # emit per-file progress via its own event loop.
        threading.Thread(
            target=run_processing_in_background,
            args=(all_paths, actual_userid, "google", folderpath),
            kwargs={"job_id": job_id, "session_id": session_id},
            daemon=True,
        ).start()

        db.commit()

        await emit(
            msg_builder.job_success(
                job_id,
                session_id,
                f"Files downloaded and processing started ({len(all_paths)} file(s))",
            )
        )

        return {
            "message": "Successfully processed files",
            "files": "started processing",
        }

    except Exception as e:
        db.rollback()
        await emit(msg_builder.job_error(job_id, session_id, str(e)))
        raise

    finally:
        db.close()


@permission_required_body("kb.doc.upload")
@docs_agent_bps.route("/process-drive", methods=["POST"])
async def download_files_stream():
    data = request.json

    if not get_main_service():
        return jsonify({"error": "Google Drive service not initialized."}), 500

    if not data or "files" not in data or not isinstance(data["files"], list):
        return (
            jsonify(
                {
                    "error": "Invalid request payload. Expected JSON with a 'files' array."
                }
            ),
            400,
        )

    if not data["files"]:
        return jsonify({"error": "No files picked"}), 400

    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400

    try:
        ensure_dir("data")
    except Exception as e:
        return jsonify({"error": f"Failed to create download directory: {e}"}), 500

    session_id = data.get("session_id") or None

    job_id = await JobManager.submit_job(
        execute_process_drive, data, session_id=session_id
    )

    return jsonify({"success": True, "job_id": job_id, "status": "queued"})


# ─────────────────────────────────────────────
# EXECUTE: process-local
# ─────────────────────────────────────────────


async def execute_process_local(data, job_id=None, session_id=None):
    from runbook.utils import send

    user_id = data.get("user_id")
    file_paths = data.get("file_paths", [])
    upload_folder = data.get("upload_folder")
    should_emit = bool(job_id and session_id)

    async def emit(msg):
        if should_emit:
            await send(ws_service, msg, user_id)

    # progress_emit is what gets passed to process_and_update_yaml.
    # It wraps msg_builder.job_progress so ag_helperzz.py stays free of
    # websocket imports — it only calls await emit(message, progress).
    async def progress_emit(message: str, progress: int):
        await emit(
            msg_builder.job_progress(
                job_id, session_id, "processing", message, progress
            )
        )

    try:
        await emit(
            msg_builder.job_progress(
                job_id, session_id, "init", "Starting file processing...", 5
            )
        )
        await emit(
            msg_builder.job_progress(
                job_id,
                session_id,
                "processing",
                f"Preparing {len(file_paths)} file(s) for embedding...",
                15,
            )
        )

        result = await process_and_update_yaml(
            all_downloaded_paths=file_paths,
            userid=user_id,
            provider="local",
            db=None,
            folderpath=upload_folder,
            emit=progress_emit,  # per-file progress, uses msg_builder internally
        )

        if isinstance(result, dict) and result.get("error") == "INSUFFICIENT_CREDITS":
            await emit(
                msg_builder.job_error(
                    job_id, session_id, "Insufficient credits to process file(s)"
                )
            )
            return None

        await emit(
            msg_builder.job_progress(
                job_id, session_id, "done", "All files processed successfully", 98
            )
        )
        await emit(
            msg_builder.job_success(
                job_id, session_id, "File(s) processed successfully"
            )
        )

        return {"message": "Done", "files": result}

    except Exception as e:
        await emit(msg_builder.job_error(job_id, session_id, str(e)))
        raise


@permission_required_body("kb.doc.upload")
@docs_agent_bps.route("/process-local", methods=["POST"])
async def process_local():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files uploaded"}), 400

    user_id = request.form.get("user_id")
    api_key = request.form.get("api_key")
    session_id = request.form.get("session_id") or None

    if not user_id or not api_key:
        return jsonify({"error": "Missing user_id or api_key"}), 400

    UPLOAD_FOLDER = f"uploads/uploads_{uuid.uuid4()}"
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)

    all_file_paths = []
    for file in files:
        if not file or not file.filename:
            continue
        filename = secure_filename(file.filename)
        saved_path = os.path.join(UPLOAD_FOLDER, filename)
        file.save(saved_path)
        # extract_archive_to_folder handles zip files (magic-byte detection);
        # non-archives are returned as-is
        extracted = extract_archive_to_folder(saved_path, UPLOAD_FOLDER)
        all_file_paths.extend(extracted)

    if not all_file_paths:
        return jsonify({"error": "No valid files to process"}), 400

    data = {
        "user_id": user_id,
        "api_key": api_key,
        "file_paths": all_file_paths,
        "upload_folder": UPLOAD_FOLDER,
        "session_id": session_id,
    }

    job_id = await JobManager.submit_job(
        execute_process_local, data, session_id=session_id
    )

    return jsonify({"success": True, "job_id": job_id, "status": "queued"})


# ─────────────────────────────────────────────
# BACKGROUND THREAD HELPER (Drive processing)
# ─────────────────────────────────────────────


def run_processing_in_background(
    all_paths, actual_userid, provider, folderpath, job_id=None, session_id=None
):
    """
    Runs process_and_update_yaml in a new event loop (background thread).
    Reconstructs the emit callback here using msg_builder so that per-file
    socket progress messages are sent from within the thread's event loop.
    """
    from runbook.utils import send
    from websockets_custom.ws_instance import (
        ws_service,
        msg_builder_main as _msg_builder,
    )

    should_emit = bool(job_id and session_id)

    async def _run():
        db = connect_to_rds()
        credits = Credits(db=db)

        async def progress_emit(message: str, progress: int):
            if should_emit:
                await send(
                    ws_service,
                    _msg_builder.job_progress(
                        job_id, session_id, "processing", message, progress
                    ),
                    actual_userid,
                )

        try:
            result = await process_and_update_yaml(
                all_downloaded_paths=all_paths,
                userid=actual_userid,
                provider=provider,
                folderpath=folderpath,
                db=db,
                credits=credits,
                emit=progress_emit,
            )
            db.commit()

            if should_emit:
                if (
                    isinstance(result, dict)
                    and result.get("error") == "INSUFFICIENT_CREDITS"
                ):
                    await send(
                        ws_service,
                        _msg_builder.job_error(
                            job_id, session_id, "Insufficient credits"
                        ),
                        actual_userid,
                    )
                else:
                    await send(
                        ws_service,
                        _msg_builder.job_success(
                            job_id, session_id, "File processing completed"
                        ),
                        actual_userid,
                    )

        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    asyncio.run(_run())


# ─────────────────────────────────────────────
# GET USER DOCS
# ─────────────────────────────────────────────


@permission_required_body("kb.doc.view")
@docs_agent_bps.route("/get-usersDocs", methods=["Get"])
def getUsersDocs():
    userid = request.args.get("userid")
    if not userid:
        return jsonify({"error": "User ID is required"}), 400
    if not check_userid_valid(userid):
        return jsonify({"error": "Invalid access"}), 404
    yaml_path = f"{userid}/yaml/users_fileData.yaml"

    all_file_data = load_yaml_from_s3(yaml_path) or {}

    return jsonify(all_file_data), 200


# ─────────────────────────────────────────────
# DELETE FILE
# ─────────────────────────────────────────────


@permission_required_body("kb.doc.delete")
@docs_agent_bps.route("/delete_file", methods=["DELETE"])
async def delete_file():
    userid = request.json.get("userid")
    filename = request.json.get("filename")
    source = request.json.get("source")

    if not userid or not filename or not source:
        return jsonify({"error": "User ID, filename, and source are required"}), 400
    if not check_userid_valid(userid):
        return jsonify({"error": "Invalid access"}), 404

    yaml_path = f"{userid}/yaml/users_fileData.yaml"

    all_file_data = load_yaml_from_s3(yaml_path) or {}

    if source not in all_file_data or not isinstance(all_file_data[source], list):
        return jsonify({"error": f"No entries found for source '{source}'"}), 404

    credits = Credits()
    lance_agent = LanceClient(user_id=userid, credits=credits)
    delete_result = await lance_agent.delete_file_Data(foldername=filename)
    if delete_result.get("status") != "success":
        return jsonify({"error": delete_result.get("message", "Unknown error")}), 500

    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    file_found = False

    for entry in all_file_data[source]:
        if isinstance(entry, dict) and entry.get("filename") == filename:
            entry["FileStatus"] = "Deleted"
            entry["updated_date"] = current_time
            file_found = True
            break

    if not file_found:
        return jsonify({"error": "Filename not found in specified source"}), 404

    save_yaml_to_s3(all_file_data, userid, "users_fileData.yaml")

    success = deletefilebasedData(filename, userid)
    if not success:
        return (
            jsonify(
                {
                    "message": f"Failed to delete question entries for user {userid}, file {filename}"
                }
            ),
            400,
        )

    return (
        jsonify(
            {
                "message": "File deleted and related question entries removed successfully",
                "data": all_file_data,
            }
        ),
        200,
    )
