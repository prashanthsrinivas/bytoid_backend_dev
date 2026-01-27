import asyncio
from datetime import datetime
import json
import os
from agent_route.Drive_downloader import (
    GetEmailandDriveService,
    Mediatorservice,
    get_main_service,
)
from agent_route.ag_helperzz import deletefilebasedData, process_and_update_yaml
from agent_route.lance_agent import LanceClient
from agent_route.routes import agent_bps
from credits_route.route import Credits
from db.db_checkers import check_userid_valid
from db.rds_db import connect_to_rds
from flask import (
    Blueprint,
    request,
    jsonify,
    session,
    Response,
    stream_with_context,
)
from google_route.routes import get_token
from integrations.google_integration import get_integration_access_token
from utils.base_logger import get_logger
from utils.normal import ensure_dir
from utils.s3_utils import load_yaml_from_s3, save_yaml_to_s3
from request_context import current_user_id
from werkzeug.utils import secure_filename
import uuid


logger = get_logger(__name__)

docs_agent_bps = Blueprint("agent_docs", __name__)


@docs_agent_bps.route("/process-drive", methods=["POST"])
def download_files_stream():
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

    apikey = data.get("api_key")
    user_id = data.get("user_id")
    primary_provider = data.get("primary_provider")

    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400

    try:
        ensure_dir("data")
    except Exception as e:
        return jsonify({"error": f"Failed to create download directory: {e}"}), 500

    # --------------------------------------------------
    # DB + Credits (shared for entire stream)
    # --------------------------------------------------
    db = connect_to_rds()
    credits = Credits(db=db)

    try:
        # --------------------------------------------------
        # Resolve access token + actual userid
        # --------------------------------------------------
        if primary_provider:
            access_token = get_token(user_id, value=True, in_connection=db)
            actual_userid = user_id
        else:
            access_token, actual_userid = get_integration_access_token(
                user_id, "google"
            )

        if not access_token:
            db.close()
            return jsonify({"error": "Unable to fetch access token"}), 401

        # --------------------------------------------------
        # Streaming generator
        # --------------------------------------------------
        def event_stream():
            try:
                yield "event: start\ndata: Starting processing...\n\n"

                user_service = GetEmailandDriveService(access_token)
                if not user_service:
                    yield "event: error\ndata: Cannot access Google Drive\n\n"
                    return

                # -------------------------------
                # Step 1: Download files
                # -------------------------------
                all_paths, is_downloaded = Mediatorservice(
                    data, actual_userid, user_service
                )

                if not is_downloaded or not all_paths:
                    yield "event: error\ndata: Failed to download files\n\n"
                    return

                for i, path in enumerate(all_paths, 1):
                    yield (
                        f"event: progress\ndata: Downloaded {i}/{len(all_paths)}: {path}\n\n"
                    )

                # -------------------------------
                # Step 2: Process files (embeddings)
                # Credits are deducted INSIDE
                # -------------------------------
                folderpath = os.path.commonpath(all_paths)

                processed_data = asyncio.run(
                    process_and_update_yaml(
                        all_downloaded_paths=all_paths,
                        userid=actual_userid,
                        provider="google",
                        folderpath=folderpath,
                        db=db,
                        credits=credits,  # 🔐 atomic credit usage inside
                    )
                )

                # -------------------------------
                # Commit only if everything succeeded
                # -------------------------------
                db.commit()

                yield (
                    f"event: complete\ndata: {json.dumps({'message': 'Successfully processed files', 'files': processed_data})}\n\n"
                )

            except Exception as e:
                db.rollback()
                yield f"event: error\ndata: {str(e)}\n\n"

            finally:
                db.close()

        return Response(
            stream_with_context(event_stream()),
            mimetype="text/event-stream",
        )

    except Exception as e:
        db.rollback()
        db.close()
        return jsonify({"error": str(e)}), 500


# @docs_agent_bps.route("/process-local", methods=["POST"])
# def process_local():

#     # 🔹 Get file
#     if "files" not in request.files:
#         return jsonify({"error": "No file uploaded"}), 400

#     file = request.files["files"]
#     print(f"file name : {file.filename}")

#     if file.filename == "":
#         return jsonify({"error": "No file selected"}), 400

#     # 🔹 Get form fields
#     user_id = request.form.get("user_id")
#     api_key = request.form.get("api_key")
#     source = request.form.get("source")

#     if not user_id or not api_key:
#         return jsonify({"error": "Missing user_id or api_key"}), 400

#     UPLOAD_FOLDER = f"uploads_{uuid.uuid4()}"
#     os.makedirs(UPLOAD_FOLDER, exist_ok=True)

#     # 🔹 Save file safely
#     filename = secure_filename(file.filename)
#     file_path = os.path.join(UPLOAD_FOLDER, filename)
#     file.save(file_path)


#     def event_stream():
#         yield "event: start\ndata: Starting processing...\n\n"

#         try:
#             all_file_data = asyncio.run(
#                 process_and_update_yaml(
#                     all_downloaded_paths=[file_path],
#                     userid=user_id,
#                     provider="local",
#                     folderpath=UPLOAD_FOLDER,
#                     credits=credits
#                 )
#             )

#             yield (
#                 "event: complete\n"
#                 f"data: {json.dumps({'message': 'Successfully processed files', 'files': all_file_data})}\n\n"
#             )

#         except Exception as e:
#             yield ("event: error\n" f"data: {json.dumps({'error': str(e)})}\n\n")

#     return Response(stream_with_context(event_stream()), mimetype="text/event-stream")


@docs_agent_bps.route("/get-usersDocs", methods=["Get"])
def getUsersDocs():
    """
    It retrieves the user's documents from the YAML file and returns them as a JSON response.
    If the user_id is not provided, it returns an error message.
    """
    userid = request.args.get("userid")
    if not userid:
        return jsonify({"error": "User ID is required"}), 400
    if not check_userid_valid(userid):
        return jsonify({"error": "Invalid access"}), 404
    yaml_path = f"{userid}/yaml/users_fileData.yaml"
    # if not os.path.exists(yaml_path):
    #     return jsonify({"error": "No documents found for this user"}), 404

    all_file_data = load_yaml_from_s3(yaml_path) or {}

    return jsonify(all_file_data), 200


@docs_agent_bps.route("/delete_file", methods=["DELETE"])
async def delete_file():
    """
    Deletes vector data from LanceDB via LanceClient and updates the YAML metadata:
    - Sets 'FileStatus' to 'Deleted'
    - Sets 'updated_date' to current datetime
    - Removes entries from passed_ques.yaml and failed_ques.yaml with matching filename
    - Deletes passed/failed YAML files if they become empty
    """
    userid = request.json.get("userid")
    filename = request.json.get("filename")
    source = request.json.get("source")  # e.g., "outlook" or "google"

    if not userid or not filename or not source:
        return jsonify({"error": "User ID, filename, and source are required"}), 400
    if not check_userid_valid(userid):
        return jsonify({"error": "Invalid access"}), 404

    yaml_path = f"{userid}/yaml/users_fileData.yaml"
    # if not os.path.exists(yaml_path):
    #     return jsonify({"error": "No documents found for this user"}), 404

    # Load main file metadata YAML
    all_file_data = load_yaml_from_s3(yaml_path) or {}

    if source not in all_file_data or not isinstance(all_file_data[source], list):
        return jsonify({"error": f"No entries found for source '{source}'"}), 404

    # Step 1: Delete vectors from LanceDB
    lance_agent = LanceClient(user_id=userid)
    delete_result = await lance_agent.delete_file_Data(foldername=filename)
    if delete_result.get("status") != "success":
        return jsonify({"error": delete_result.get("message", "Unknown error")}), 500

    # Step 2: Update YAML entry
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    file_found = False

    for entry in all_file_data[source]:
        if isinstance(entry, dict) and entry.get("filename") == filename:
            if entry.get("FileStatus", "").lower() != "deleted":
                entry["FileStatus"] = "Deleted"
                entry["updated_date"] = current_time
                file_found = True
            else:
                return jsonify({"error": "File is already marked as deleted"}), 400
            break

    if not file_found:
        return jsonify({"error": "Filename not found in specified source"}), 404

    # Step 3: Save updated YAML
    # with open(yaml_path, "w") as f:
    #     yaml.safe_dump(all_file_data, f, sort_keys=False)
    save_yaml_to_s3(all_file_data, userid, "users_fileData.yaml")

    # Step 4: Delete related passed/failed Q&A entries
    success = deletefilebasedData(filename, userid)
    if not success:
        logger.warning(
            f"Failed to delete question entries for user {userid}, file {filename}"
        )

    # # Reload for returning updated data
    # all_file_data

    return (
        jsonify(
            {
                "message": "File deleted and related question entries removed successfully",
                "data": all_file_data,
            }
        ),
        200,
    )
