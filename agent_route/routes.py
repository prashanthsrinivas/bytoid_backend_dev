import json
import os
from agent_route.Drive_downloader import (
    GetEmailandDriveService,
    Main_service,
    Mediatorservice,
)
from agent_route.ag_helperzz import (
    deletefilebasedData,
    process_and_update_yaml,
    remove_https_prefix,
    scrape_links,
)
from agent_route.doc_clarity import (
    QueryInput,
    clarific_transcriptions,
    preProcessDocWithUsecases,
    remove_transcript_clarifications,
)
from agent_route.lance_agent import LanceClient
from agent_route.s_t_s import Speech2TextService
from agent_route.utils import extract_filename, extract_transcript_filename
from cust_helpers import pathconfig
from flask import Blueprint, request, jsonify, session, redirect
from google_route.routes import get_token
from utils.base_logger import get_logger
from utils.chatopenzz import check_lancedb
from utils.fireworkzz import (
    evaluate_transcript,
    evaluator_llama,
    get_fireworks_response,
)
from utils.normal import ensure_dir, load_yaml_file
import uuid
import asyncio
import traceback
from db.rds_db import connect_to_rds
import re
from datetime import datetime
import yaml
from werkzeug.utils import secure_filename
from utils.s3_utils import (
    attach_CLDFRNT_url,
    delete_file_from_s3,
    generate_presigned_url,
    read_json_from_s3,
    upload_any_file,
)
from .task_manager import run_background_task, task_status
from db.db_checkers import (
    create_ticket_Communication_assigned,
    fetch_document_link,
    fetch_userid_from_launch,
    check_userid_valid,
    get_line_of_business,
    get_user_agent_id,
    update_agent_document_link,
)
import pymysql

# from app import qa_chain

agent_bp = Blueprint("agent", __name__)
logger = get_logger(__name__)


@agent_bp.route("/save-training-settings", methods=["POST"])
def save_training_settings():
    """
    Create or update launch + subagent for a user.
    Returns: api_key, assistant_name, sync_website, voice_type
    """
    try:
        data = request.get_json()
        print("dasdsa", data)
        assistant_name = data.get("assistant_name")
        voice_type = data.get("voice_type", "").capitalize()
        sync_website = (
            remove_https_prefix(data.get("sync_website"))
            if data.get("sync_website")
            else None
        )
        user_id = session.get("user_id") or data.get("user_id")

        # Validate required fields
        if not user_id:
            return jsonify({"error": "User not logged in"}), 400
        if voice_type not in ["Man", "Woman"]:
            return jsonify({"error": "Invalid voice type"}), 400
        if not assistant_name:
            return jsonify({"error": "Assistant name is required"}), 400
        if not sync_website:
            return jsonify({"error": "Website is required"}), 400
        if not check_userid_valid(user_id):
            return jsonify({"error": "Invalid access"}), 404

        connection = connect_to_rds()
        base_updated = False
        with connection.cursor() as cursor:
            # Check if launch exists
            cursor.execute(
                "SELECT launch_id, api_id FROM launch WHERE user_id_fk = %s LIMIT 1",
                (user_id,),
            )
            launch = cursor.fetchone()

            if not launch:
                print("Creating new launch and subagent")

                launch_id = str(uuid.uuid4())
                sub_agent_id = str(uuid.uuid4())
                api_key = str(uuid.uuid4())

                # Insert subagent
                cursor.execute(
                    """
                    INSERT INTO subagents (
                        sub_agent_id, launch_id_fk, name, description, voice_type,
                        documentation_link, model_version, created_at, updated_at
                    ) VALUES (%s, NULL, %s, 'Registered', %s, NULL, NULL, NOW(), NOW())
                """,
                    (sub_agent_id, assistant_name, voice_type),
                )

                # Insert launch
                cursor.execute(
                    """
                    INSERT INTO launch (
                        launch_id, sub_agent_id_fk, user_id_fk, api_id, website_name
                    ) VALUES (%s, %s, %s, %s, %s)
                """,
                    (launch_id, sub_agent_id, user_id, api_key, sync_website),
                )

                # Link subagent
                cursor.execute(
                    """
                    UPDATE subagents
                    SET launch_id_fk = %s
                    WHERE sub_agent_id = %s
                """,
                    (launch_id, sub_agent_id),
                )

            else:
                print("Updating existing launch and subagent")

                launch_id, api_key = launch

                # Update launch
                cursor.execute(
                    """
                    UPDATE launch
                    SET website_name = %s
                    WHERE launch_id = %s
                """,
                    (sync_website, launch_id),
                )

                # Update subagent
                cursor.execute(
                    """
                    UPDATE subagents
                    SET name = %s,
                        voice_type = %s,
                        updated_at = NOW()
                    WHERE launch_id_fk = %s
                """,
                    (assistant_name, voice_type, launch_id),
                )
                base_updated = True

            connection.commit()
            # 🔹 Extra step: update invited_by's agents_hub if current user is "user"
        if base_updated:
            with connection.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute(
                    "SELECT permissions, user_type FROM users WHERE user_id = %s",
                    (user_id,),
                )
                row = cursor.fetchone()
                if row and row["user_type"] == "user":
                    # permissions of this invited user
                    invited_user_permissions = json.loads(row["permissions"])
                    invited_by_email = invited_user_permissions.get("invited_by")

                    if invited_by_email:
                        # get the invited_by owner
                        cursor.execute(
                            "SELECT permissions FROM users WHERE email = %s",
                            (invited_by_email,),
                        )
                        owner_row = cursor.fetchone()
                        if owner_row and owner_row["permissions"]:
                            owner_permissions = json.loads(owner_row["permissions"])

                            agents_hub = owner_permissions.get("agents_hub", [])
                            updated = False
                            for agent in agents_hub:
                                if agent.get("launch_id") == launch_id:
                                    agent["name"] = assistant_name
                                    agent["website_name"] = sync_website
                                    agent["voice_type"] = voice_type
                                    updated = True
                                    break

                            if updated:
                                cursor.execute(
                                    "UPDATE users SET permissions = %s WHERE email = %s",
                                    (json.dumps(owner_permissions), invited_by_email),
                                )
                                connection.commit()
                else:
                    if row and row["permissions"]:
                        owner_permissions = json.loads(row["permissions"])

                        agents_hub = owner_permissions.get("agents_hub", [])
                        updated = False
                        for agent in agents_hub:
                            if agent.get("launch_id") == launch_id:
                                agent["name"] = assistant_name
                                agent["website_name"] = sync_website
                                agent["voice_type"] = voice_type
                                updated = True
                                break

                        if updated:
                            cursor.execute(
                                "UPDATE users SET permissions = %s WHERE user_id = %s",
                                (json.dumps(owner_permissions), user_id),
                            )
                            connection.commit()

        # Return consistent response
        return jsonify(
            {
                "api_key": api_key,
                "assistant_name": assistant_name,
                "sync_website": sync_website,
                "voice_type": voice_type,
            }
        )

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        connection.close()


@agent_bp.route("/get-training-settings", methods=["POST"])
def get_training_settings():
    """
    It takes the user_id from the session or request,
    retrieves the launch_id and api_id for that user,
    and returns the subagent settings including assistant name, voice type, sync website, and api key.
    If the user is not logged in or no launch record is found, it returns an error message.
    """
    try:
        user_id = str(session.get("user_id") or request.json.get("user_id"))
        if not user_id:
            return jsonify({"error": "User not logged in"}), 401
        if not check_userid_valid(user_id):
            return jsonify({"error": "Invalid Access"}), 404

        connection = connect_to_rds()

        with connection.cursor() as cursor:
            # Get launch_id for the user
            sql = "SELECT launch_id,api_id FROM launch WHERE user_id_fk = %s LIMIT 1"
            cursor.execute(sql, (user_id,))
            result = cursor.fetchone()
            if result is None:
                return (
                    jsonify({"error": "No launch record found for given user_id"}),
                    404,
                )

            launch_id, api_id = result

            # Get subagent settings
            sql = """
                SELECT name, voice_type, website_name
                FROM subagents
                JOIN launch ON subagents.launch_id_fk = launch.launch_id
                WHERE subagents.launch_id_fk = %s
            """
            cursor.execute(sql, (launch_id,))
            settings = cursor.fetchone()

            if settings is None:
                return jsonify({"error": "No settings found for this user"}), 404

            response_data = {
                "assistant_name": settings[0],
                "voice_type": settings[1],
                "sync_website": settings[2],
                "api_key": api_id,
            }

            return jsonify(response_data)

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@agent_bp.route("/process-query-key", methods=["POST"])
def checkquerywithApiKey():
    try:
        print("Query made by:", session.get("user", {}))

        data = request.json
        querytext = data.get("query", "").strip()
        api_key = data.get("api_key")
        if not api_key:
            return jsonify({"error": "API key is required"}), 400
        website = (
            remove_https_prefix(data.get("website")) if data.get("website") else None
        )
        if not website:
            return jsonify({"error": "Website is required"}), 400
        if not querytext:
            return jsonify({"error": "Query is required"}), 400

        connection = connect_to_rds()
        with connection.cursor() as cursor:
            # Check if the API key exists for the user
            cursor.execute(
                "SELECT user_id_fk,website_name FROM launch WHERE api_id = %s ",
                (api_key,),
            )
            user_row = cursor.fetchone()
            if not user_row:
                return jsonify({"error": "Invalid API key"}), 401

            userid, userwebsite = user_row
            if website != userwebsite and website != "bytoid.ai":
                return (
                    jsonify({"error": "API key does not match the provided website"}),
                    401,
                )
            if not check_userid_valid(userid):
                return jsonify({"error": "Invalid access"}), 404

        response_data = []
        # Check for exact match in passed_ques.yaml
        passed_yaml_path = f"{pathconfig.basepath}/{userid}/passed_ques.yaml"
        valid_ones = load_yaml_file(passed_yaml_path)
        if valid_ones and isinstance(valid_ones[0], list):
            valid_ones = [item for sublist in valid_ones for item in sublist]

        if valid_ones:
            for each in valid_ones:
                user_query = each.get("User", "").strip().lower()
                if user_query == querytext.lower():
                    response_data.append(
                        {
                            "id": "",
                            "match_score": "",
                            "extracted_answer": each.get("Ai Response", ""),
                            "full_text": "",
                        }
                    )
                    return jsonify(response_data), 200

        # If no exact match, perform vector search
        top_k = 1
        query_input = QueryInput(user_id=userid, query_text=querytext, top_k=top_k)
        lance_client = LanceClient(user_id=userid)
        results = lance_client.query_vector(query_input)

        for r in results:
            clean_text = r.get("text", "").encode().decode("unicode_escape")
            relevant = lance_client.extract_relevant_text(querytext, clean_text)

            response_data.append(
                {
                    "id": r.get("id", ""),
                    "match_score": round(r.get("_distance", 0.0), 4),
                    "extracted_answer": relevant,
                    "full_text": clean_text,
                }
            )
        connection.close()

        return jsonify(response_data), 200

    except Exception as e:
        print("❌ Error during query processing:", e)
        return jsonify({"error": str(e)}), 400


@agent_bp.route("/process-drive", methods=["POST"])
def download_files():
    """
    Takes the picker metadata from the frontend and makes a sharable with the service account
    after completion of sharing we process the file or folder if not retry after 3-4 seconds
    then download the files in data folder
    after download preprocess with langchain and send it as embedding to lancedb
    """
    ok, val = check_lancedb()
    if not ok:
        logger.info(f"LanceDB service down: {val}")
        return jsonify({"error": f"service down! Please try again later"}), 503

    if not Main_service:
        return (
            jsonify({"error": "Google Drive service not initialized."}),
            500,
        )

    try:
        ensure_dir("data")
    except Exception as e:
        return jsonify({"error": f"Failed to create download directory: {e}"}), 500

    data = request.json
    if not data or "files" not in data or not isinstance(data["files"], list):
        return (
            jsonify(
                {
                    "error": "Invalid request payload. Expected JSON with a 'files' array."
                }
            ),
            400,
        )
    if len(data["files"]) == 0:
        return jsonify({"error": "No files Picked"}), 400

    apikey = data["api_key"]
    if not apikey:
        return jsonify({"error": "API key is required"}), 400
    userid = fetch_userid_from_launch(apikey)
    if not userid:
        return jsonify({"error": "User ID not found for the provided API key"}), 401
    if not check_userid_valid(userid):
        return jsonify({"error": "Invalid access"}), 404
    access_token = get_token(userid, value=True)

    user_service = None
    if access_token:
        user_service = GetEmailandDriveService(access_token)
        if user_service:
            all_downloaded_paths, is_downloaded = Mediatorservice(
                data, userid, user_service
            )
            if is_downloaded and len(all_downloaded_paths) > 0:
                folderpath = os.path.commonpath(all_downloaded_paths)
                all_file_data = asyncio.run(
                    process_and_update_yaml(
                        all_downloaded_paths=all_downloaded_paths,
                        userid=userid,
                        provider="google",
                        folderpath=folderpath,
                    )
                )
                return {
                    "message": "Successfully processed files",
                    "files": all_file_data,  # Return full file history
                }, 200
            else:
                return {"message": "Problem with accessing files"}, 400
        else:
            return {"message": "cant access drive"}, 400
    else:
        redirect("https://bytoid.ai/login")


@agent_bp.route("/clarifications", methods=["POST"])
def makeuserDocClarifications(userid=None, industry=None):
    """ "
    It takes the user_id and industry from the request or defaults to the provided parameters,
    retrieves the failed questions from a YAML file, and returns clarifications for those questions.
    If the file does not exist, it triggers the preProcessDocWithUsecases function to
    process the document and generate clarifications.
    It returns a JSON response with the clarifications or an error message if the user_id is not provided.
    """
    data = request.json
    fetched_userid = data.get("userid") or userid
    print("fetched_userid", fetched_userid)
    if not check_userid_valid(fetched_userid):
        return jsonify({"error": "Invalid access"}), 404
    if fetched_userid:
        task_run = task_status.get(fetched_userid, {}).get("status", "not started")
        if task_run == "running":
            return (
                jsonify(
                    {"message": "currently generating clarifications for the user."}
                ),
                400,
            )

    fetched_industry = get_line_of_business(fetched_userid)
    if not fetched_industry:
        return jsonify({"error": "No line of business present"}), 401

    if not fetched_userid:
        return jsonify({"error": "User ID is required"}), 400

    failed_path = f"{pathconfig.basepath}/{fetched_userid}/failed_ques.yaml"
    failed_entries = load_yaml_file(failed_path)

    if not failed_entries:
        logger.info("⚠ failed_ques.yaml not found or empty, regenerating QAs...")

        # Load file metadata to get all Present files
        user_files_path = os.path.join(
            f"{pathconfig.basepath}/{fetched_userid}", "users_fileData.yaml"
        )
        if not os.path.exists(user_files_path):
            return (
                jsonify({"error": "Please upload a document to have clarifications"}),
                404,
            )

        file_data = load_yaml_file(user_files_path) or []

        present_files = []
        for key, entries in file_data.items():
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict):
                        if entry.get("FileStatus") == "Present" and entry.get(
                            "filename"
                        ):
                            present_files.append(entry.get("filename"))

        if not present_files:
            return (
                jsonify({"error": "Please upload a document to have clarifications"}),
                404,
            )

        # Trigger background QA generation for all present files
        result = run_background_task(
            userid=fetched_userid,
            industry=fetched_industry,
            filenames=present_files,
            func=preProcessDocWithUsecases,
        )
        print(f"[DEBUG] Background task queued: {result}")
        return (
            jsonify({"message": "Currently generating clarifications for the user."}),
            400,
        )

    if not failed_entries:
        # File exists but is empty
        return "No clarifications required"

    # Extract clarifications
    if failed_entries and isinstance(failed_entries[0], list):
        failed_entries = [item for sublist in failed_entries for item in sublist]
    clarifications = []
    for entry in failed_entries:
        clarification = {
            "usecase": entry.get("Rephrased Question", "").strip(),
            "response": entry.get("Ai Response"),
            "quote": entry.get("quote", "").strip(),
        }
        clarifications.append(clarification)

    return jsonify(clarifications)


@agent_bp.route("/clarification_update", methods=["POST"])
def updateClarifications(userid=None, industry=None):
    data = request.json
    fetched_userid = data.get("userid") or userid
    if not fetched_userid:
        return jsonify({"error": "User ID is required"}), 400
    if not check_userid_valid(fetched_userid):
        return jsonify({"error": "Invalid access"}), 404

    fetched_queries = data.get("queries")
    if not fetched_queries or not isinstance(fetched_queries, list):
        return jsonify({"error": "Queries must be a non-empty list"}), 400

    passed_path = f"{pathconfig.basepath}/{fetched_userid}/passed_ques.yaml"
    failed_path = f"{pathconfig.basepath}/{fetched_userid}/failed_ques.yaml"

    passed_entries = load_yaml_file(passed_path) or []
    failed_entries = load_yaml_file(failed_path) or []
    if failed_entries and isinstance(failed_entries[0], list):
        failed_entries = [item for sublist in failed_entries for item in sublist]
    if passed_entries and isinstance(passed_entries[0], list):
        passed_entries = [item for sublist in passed_entries for item in sublist]
    print(len(failed_entries), "failed entries length")

    prompts = load_yaml_file(path=pathconfig.agent_template)
    # Create mapping from rephrased question → original user question
    rephrased_to_user_map = {
        entry.get("Rephrased Question", "").strip(): entry.get("User", "").strip()
        for entry in failed_entries
    }
    for query in fetched_queries:
        rephrased = query.get("usecase", "").strip()
        reply = query.get("reply", "").strip()
        usecase = rephrased_to_user_map.get(rephrased, "").strip()

        if not usecase:
            continue  # Skip blank entries

        res = evaluator_llama(
            prompts.get("customer_response_checker"),
            usecase,
            reply,
            industry,
        )

        # If the LLaMA result is a string, extract JSON
        if isinstance(res, str):
            match = re.search(r"\{.*\}", res, re.DOTALL)
            if match:
                try:
                    parsed_output = yaml.safe_load(match.group(0))
                except Exception as e:
                    print("❌ Failed to parse LLaMA JSON:", e)
                    parsed_output = {}
            else:
                print("❌ Could not extract JSON from LLaMA response")
                parsed_output = {}
        else:
            parsed_output = res  # already a dict

        is_valid = parsed_output.get("is_valid", False)
        refined_response = parsed_output.get("refined_response", "").strip()

        failed_entries_backup = failed_entries[:]
        if is_valid:
            # Remove from failed entries
            failed_entries = [e for e in failed_entries if e.get("User", "") != usecase]

            # Avoid duplicates in passed
            if not any(e.get("User", "") == usecase for e in passed_entries):
                # Find the corresponding failed entry to get rephrased
                matching_entry = next(
                    (e for e in failed_entries_backup if e.get("User", "") == usecase),
                    {},
                )
                quote = matching_entry.get("quote", "").strip()
                filename = matching_entry.get("filename", "").strip()
                passed_entries.append(
                    {
                        "User": usecase,
                        "rephrased_response": matching_entry.get(
                            "Rephrased Question", ""
                        ).strip(),
                        "Ai Response": refined_response,
                        "quote": quote,
                        "filename": filename,
                        "date_processed": datetime.now().isoformat(timespec="seconds"),
                        "doc_value": matching_entry.get("doc_value", ""),
                    }
                )
                if matching_entry.get("is_audio"):
                    logger.info(
                        f"Removing transcript clarifications for {matching_entry['is_audio']}"
                    )
                    passed_entries[-1]["is_audio"] = matching_entry["is_audio"]
                    passed_entries[-1]["rec_id"] = matching_entry.get("rec_id", "")
                    remove_transcript_clarifications(
                        userid=fetched_userid,
                        config_path=matching_entry["is_audio"],
                        rec_id=matching_entry.get("rec_id", ""),
                    )

        else:
            # If it's not valid, move it to failed entries (if not already there)
            if not any(e.get("User", "") == usecase for e in failed_entries):
                matching_entry = next(
                    (e for e in failed_entries_backup if e.get("User", "") == usecase),
                    {},
                )
                failed_entries.append(
                    {
                        "User": usecase,
                        "rephrased_response": matching_entry.get(
                            "Rephrased Question", ""
                        ).strip(),
                        "Ai Response": reply,
                        "quote": quote,
                        "filename": filename,
                    }
                )

    # ✅ Save YAML files
    with open(passed_path, "w", encoding="utf-8") as pf:
        yaml.dump(passed_entries, pf, allow_unicode=True, sort_keys=False)

    with open(failed_path, "w", encoding="utf-8") as ff:
        yaml.dump(failed_entries, ff, allow_unicode=True, sort_keys=False)
    if len(failed_entries) > 0:
        clarifications = []

        for entry in failed_entries:
            usecase = entry.get("User")

            # Find matching reply in fetched_queries by usecase
            matching_query = next(
                (
                    q
                    for q in fetched_queries
                    if q.get("usecase", "").strip() == usecase.strip()
                ),
                None,
            )

            clarification = {
                "usecase": entry.get("Rephrased Question", "").strip(),
                "response": (
                    matching_query.get("reply", "").strip() if matching_query else ""
                ),
                "quote": entry.get("quote", "").strip() if "quote" in entry else "",
            }

            clarifications.append(clarification)

        return (
            jsonify(
                {
                    "clarifications": clarifications,
                    "message": "Clarifications partially updated",
                }
            ),
            207,
        )

    return (
        jsonify(
            {
                "clarifications": [],
                "message": "All clarifications updated successfully",
            }
        ),
        200,
    )


@agent_bp.route("/get-usersDocs", methods=["Get"])
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
    yaml_path = f"{pathconfig.basepath}/{userid}/users_fileData.yaml"
    if not os.path.exists(yaml_path):
        return jsonify({"error": "No documents found for this user"}), 404

    all_file_data = load_yaml_file(yaml_path) or {}

    return jsonify(all_file_data), 200


@agent_bp.route("/delete_file", methods=["DELETE"])
def delete_file():
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

    yaml_path = f"{pathconfig.basepath}/{userid}/users_fileData.yaml"
    if not os.path.exists(yaml_path):
        return jsonify({"error": "No documents found for this user"}), 404

    # Load main file metadata YAML
    all_file_data = load_yaml_file(yaml_path) or {}

    if source not in all_file_data or not isinstance(all_file_data[source], list):
        return jsonify({"error": f"No entries found for source '{source}'"}), 404

    # Step 1: Delete vectors from LanceDB
    lance_agent = LanceClient(user_id=userid)
    delete_result = lance_agent.delete_file_Data(foldername=filename)
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
    with open(yaml_path, "w") as f:
        yaml.safe_dump(all_file_data, f, sort_keys=False)

    # Step 4: Delete related passed/failed Q&A entries
    success = deletefilebasedData(filename, userid)
    if not success:
        logger.warning(
            f"Failed to delete question entries for user {userid}, file {filename}"
        )

    # Reload for returning updated data
    all_file_data

    return (
        jsonify(
            {
                "message": "File deleted and related question entries removed successfully",
                "data": all_file_data,
            }
        ),
        200,
    )


@agent_bp.route("/get-ai-suggestion", methods=["POST"])
def get_ai_suggestion():
    try:
        data = request.json

        # Validate required inputs
        if not data or "usecase" not in data:
            return jsonify({"error": "usecase Query is required"}), 400

        query_text = data["usecase"].strip()
        if not query_text:
            return jsonify({"error": "Query cannot be empty"}), 400

        userid = data.get("userid")
        if not userid:
            return jsonify({"error": "User ID is required"}), 400
        if not check_userid_valid(userid):
            return jsonify({"error": "Invalid access"}), 404

        # Load prompt template
        prompts = load_yaml_file(path=pathconfig.agent_template)
        fetched_industry = get_line_of_business(userid)
        if not fetched_industry:
            return jsonify({"error": "No line of business found"}), 404
        QA_assist_prompt_template = prompts.get("business_owner_QA_assist")

        if not QA_assist_prompt_template:
            return jsonify({"error": "Prompt template not found"}), 500

        # Format prompt and get AI response
        full_prompt = QA_assist_prompt_template.format(
            question=query_text, business_type=fetched_industry
        )
        ai_suggestion = get_fireworks_response(full_prompt, role="user")

        return jsonify({"suggestion": ai_suggestion}), 200

    except Exception as e:
        print("❌ Error during AI suggestion processing:", e)
        return jsonify({"error": "Internal server error"}), 500


@agent_bp.route("/create-ticket", methods=["POST"])
def create_sub_ticket():
    try:
        data = request.json
        communication_id = data.get("communication_id")
        priority = data.get("priority")
        status = data.get("status")
        tick = create_ticket_Communication_assigned(
            communication_id=communication_id, priority=priority, status=status
        )
        if tick:
            return {"message": "created ticket successfully"}, 200
    except Exception as e:
        return jsonify({"error": f"Internal server error {e}"}), 500


@agent_bp.route("/process_audio", methods=["POST"])
def process_audio():
    api_key = request.form.get("api_key")
    if not api_key:
        return jsonify({"error": "API key is required"}), 400
    userid, agentid = get_user_agent_id(api_key)
    if not userid:
        return jsonify({"error": "User ID is required"}), 400
    if not check_userid_valid(userid):
        return jsonify({"error": "Invalid access"}), 404

    if "audio_file" not in request.files:
        return jsonify({"error": "Audio file is required"}), 400

    audio_file = request.files["audio_file"]
    filename = secure_filename(audio_file.filename)
    local_audio_path = os.path.join("/tmp", filename)
    audio_file.save(local_audio_path)
    duration_from_frontend = request.form.get("duration_seconds")
    transcript_local_path = None
    config_local_path = None

    try:
        # 🔹 Load or create per-user config
        config_present = False
        config_Exists = fetch_document_link(agentid)
        if config_Exists is None:
            logger.info(
                f"Creating new audio config for user {userid} as no existing config found"
            )
            config_filename = f"{uuid.uuid4().hex[:8]}.json"
            config = {"user_id": userid, "recordings": []}
            trans_filename = config_filename
        else:
            # unwrap tuple if needed
            if isinstance(config_Exists, (tuple, list)):
                config_filename = config_Exists[0]
            else:
                config_filename = config_Exists

            logger.info(
                f"Loading existing audio config for user {userid}: {config_filename}"
            )
            config = read_json_from_s3(config_filename)
            trans_filename = extract_filename(config_filename)
            config_present = True
        # 🔹 Upload original audio file to S3
        audio_s3_path = upload_any_file(
            local_audio_path, user_id=userid, file_name=filename, type="audio"
        )

        # 🔹 Run Speech-to-Text
        main_process = Speech2TextService(userid=userid)
        transcript_text = asyncio.run(main_process.transcribe_audio(local_audio_path))

        if not transcript_text:
            return jsonify({"error": "Failed to transcribe audio"}), 500

        prompts = load_yaml_file(path=pathconfig.agent_template)
        clean_transcription_prompt = prompts.get("clean_transcription_prompt")
        val = evaluate_transcript(clean_transcription_prompt, transcript_text)
        if not val:
            return jsonify({"error": "Failed to evaluate transcript"}), 500

        # 🔹 Build transcript metadata
        now = datetime.utcnow().isoformat(timespec="seconds")
        transcript_data = {
            "id": str(uuid.uuid4().hex[:8]),
            "filename": filename,
            "date": now,
            "text": val["clean_text"],
            "summary": val["summary"],
        }

        # 🔹 Save transcript to JSON
        transcript_filename = f"{os.path.splitext(filename)[0]}_transcript.json"
        transcript_local_path = os.path.join("/tmp", transcript_filename)
        with open(transcript_local_path, "w", encoding="utf-8") as f:
            json.dump(transcript_data, f, ensure_ascii=False, indent=2)

        # 🔹 Upload transcript file to S3
        transcript_s3_path = upload_any_file(
            transcript_local_path,
            user_id=userid,
            file_name=transcript_filename,
            type="audio",
        )
        if val["clarifications"]:
            clarific_transcriptions(
                userid, val, filename, trans_filename, transcript_data["id"]
            )

        # 🔹 Add new recording entry
        config["recordings"].append(
            {
                "id": transcript_data["id"],
                "title": transcript_data["filename"],
                "date": transcript_data["date"],
                "preview": " ".join(transcript_text.split()[:20]),
                "audio_location": audio_s3_path["s3_key"],
                "transcript_location": transcript_s3_path["s3_key"],
                "summary": val["summary"],
                "clarifications": len(val["clarifications"]),
                "duration": duration_from_frontend or "unknown",
            }
        )

        # 🔹 Save updated config
        config_local_path = os.path.join("/tmp", trans_filename)
        with open(config_local_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

        new_configpath = upload_any_file(
            config_local_path, user_id=userid, file_name=trans_filename, type="audio"
        )
        if not config_present:
            logger.info(
                f"New audio config created for user {userid}: {new_configpath['s3_key']}"
            )
            update_agent_document_link(new_configpath["s3_key"], agentid)

        return (
            jsonify(
                {
                    "message": "Transcription successful",
                    "audio_file": audio_s3_path,
                    "transcript_file": transcript_s3_path,
                    "config_updated": True,
                }
            ),
            200,
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        # 🔹 Clean up local temp files
        for f in [local_audio_path, transcript_local_path, config_local_path]:
            if f and os.path.exists(f):
                try:
                    os.remove(f)
                except Exception as cleanup_err:
                    print(f"Failed to delete temp file {f}: {cleanup_err}")


@agent_bp.route("/get-audio-config", methods=["GET"])
def get_audio_config():
    api_key = request.args.get("api_key")
    if not api_key:
        return jsonify({"error": "API key is required"}), 400
    userid, agentid = get_user_agent_id(api_key)
    if not userid:
        return jsonify({"error": "User ID is required"}), 400
    if not check_userid_valid(userid):
        return jsonify({"error": "Invalid access"}), 404

    config_filename = fetch_document_link(agentid)
    if not config_filename:
        return jsonify({"error": "No audios found for this user"}), 404
    try:
        config = read_json_from_s3(config_filename)
        for rec in config.get("recordings", []):
            # Convert S3 paths to public URLs
            rec["audio_location"] = attach_CLDFRNT_url(rec["audio_location"])
            rec["transcript_location"] = attach_CLDFRNT_url(rec["transcript_location"])
        return jsonify(config), 200
    except FileNotFoundError:
        return jsonify({"error": "No audio config found for this user"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@agent_bp.route("/update-transcript", methods=["POST"])
def update_transcript():
    data = request.json or {}

    api_key = data.get("api_key")
    if not api_key:
        return jsonify({"error": "API key is required"}), 400
    userid, agentid = get_user_agent_id(api_key)
    if not userid:
        return jsonify({"error": "User ID is required"}), 400

    if not check_userid_valid(userid):
        return jsonify({"error": "Invalid access"}), 404

    filename = data.get("filename")  # title or file reference
    transcript_data = data.get("transcript_data")

    if not filename:
        return jsonify({"error": "Filename is required"}), 400
    if not transcript_data:
        return jsonify({"error": "Transcript data is required"}), 400

    try:
        # Load user config JSON
        config_filename = fetch_document_link(agentid)
        if not config_filename:
            return jsonify({"error": "No audio config found for this user"}), 404

        config = read_json_from_s3(config_filename)
        if not config:
            return jsonify({"error": "User config file could not be read"}), 404

        update_loc = None
        for rec in config.get("recordings", []):
            if rec.get("title") == filename:
                update_loc = rec.get("transcript_location")
                rec["updated_date"] = datetime.utcnow().isoformat(timespec="seconds")
                break

        if not update_loc:
            return jsonify({"error": "Transcript not found in user config"}), 404

        # Load existing transcript
        transcript_maindata = read_json_from_s3(update_loc)
        if not transcript_maindata:
            return jsonify({"error": "Transcript data not found"}), 404

        # Update transcript text
        transcript_maindata["text"] = transcript_data

        # Save updated transcript locally
        local_transcript_path = "/tmp/temp_transcript.json"
        with open(local_transcript_path, "w", encoding="utf-8") as f:
            json.dump(transcript_maindata, f, ensure_ascii=False, indent=2)

        # Upload updated transcript
        upload_any_file(
            local_transcript_path,
            user_id=userid,
            file_name=extract_transcript_filename(update_loc),
            type="audio",
        )

        # Save updated config locally
        local_config_path = "/tmp/temp_config.json"
        with open(local_config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

        # Upload updated config
        upload_any_file(
            local_config_path,
            user_id=userid,
            file_name=config_filename,
            type="audio",
        )

        # Cleanup
        os.remove(local_config_path)
        os.remove(local_transcript_path)

        return (
            jsonify(
                {
                    "message": "Transcript updated successfully",
                    "changed_filename": filename,
                    "config_updated": True,
                }
            ),
            200,
        )

    except FileNotFoundError:
        return jsonify({"error": "Transcript file not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@agent_bp.route("/delete-audio", methods=["DELETE"])
def delete_audio():
    data = request.json
    api_key = data.get("api_key")
    if not api_key:
        return jsonify({"error": "API key is required"}), 400
    userid, agentid = get_user_agent_id(api_key)

    if not userid:
        return jsonify({"error": "User ID is required"}), 400

    if not check_userid_valid(userid):
        return jsonify({"error": "Invalid access"}), 404

    audio_location = data.get("audio_location")
    if not audio_location:
        return jsonify({"error": "Audio location is required"}), 400

    try:
        # 🔹 Load user config
        config_filename = fetch_document_link(agentid)
        if not config_filename:
            return jsonify({"error": "No audio config found for this user"}), 404

        config = read_json_from_s3(config_filename)

        # 🔹 Find matching recording
        recording_to_delete = None
        filename = None
        for rec in config.get("recordings", []):
            if rec.get("audio_location") in audio_location:
                filename = rec.get("title")
                recording_to_delete = rec
                break

        if not recording_to_delete:
            return jsonify({"error": "Recording not found in config"}), 404

        # 🔹 Delete audio + transcript from S3
        delete_file_from_s3(recording_to_delete["audio_location"])
        delete_file_from_s3(recording_to_delete["transcript_location"])

        # 🔹 Remove from config
        config["recordings"].remove(recording_to_delete)

        # 🔹 Save updated config
        local_config_path = "/tmp/temp_config.json"
        with open(local_config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

        upload_any_file(
            local_config_path, user_id=userid, file_name=config_filename, type="audio"
        )
        # delete_agent_document_link(userid)
        os.remove(local_config_path)
        if filename:
            success = deletefilebasedData(filename, userid)
            if not success:
                logger.warning(
                    f"Failed to delete question entries for user {userid}, file {filename}"
                )

        return (
            jsonify(
                {
                    "message": "Audio and transcript deleted successfully",
                    "config_updated": True,
                }
            ),
            200,
        )

    except FileNotFoundError:
        return jsonify({"error": "Config file not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@agent_bp.route("/scrape", methods=["POST"])
def scrape():
    data = request.get_json()
    url = data.get("url")
    if not url:
        return jsonify({"error": "URL required"}), 400

    links = scrape_links(url, max_pages=100)  # adjust limit
    return jsonify({"url": url, "links": links, "count": len(links)})


@agent_bp.route("/check-dbfunc", methods=["POST"])
def check_lancedb():
    """
    Checks if the LanceDB service is running and returns its status.
    """
    try:
        data = request.json
        userid = data.get("userid")
        val = fetch_document_link(userid)
        return jsonify({"status": "func is running", "value": val}), 200
    except Exception as e:
        logger.error(f"Error checking func: {e}")
        return jsonify({"error": "Internal server error"}), 500
