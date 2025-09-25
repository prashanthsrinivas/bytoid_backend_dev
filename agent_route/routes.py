import json
import requests
import os
import time
from datetime import datetime
import urllib.parse
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from langchain_openai import OpenAIEmbeddings
from datetime import timezone
from flask import Blueprint, request, jsonify, session, redirect

from agent_route.Drive_downloader import (
    GetEmailandDriveService,
    Main_service,
    Mediatorservice,
)
from agent_route.ag_helperzz import (
    deletefilebasedData,
    process_and_update_yaml,
    remove_https_prefix,
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
import glob
from db.rds_db import connect_to_rds
import re
from datetime import datetime
import yaml

# YouTube processing imports
import subprocess
import tempfile
from werkzeug.utils import secure_filename
from utils.s3_utils import (
    attach_CLDFRNT_url,
    delete_file_from_s3,
    load_yaml_from_s3,
    read_json_from_s3,
    save_yaml_to_s3,
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
from dotenv import load_dotenv



agent_bps = Blueprint("agents", __name__)
logger = get_logger(__name__)

load_dotenv()


@agent_bps.route("/save-training-settings", methods=["POST"])
def save_training_settings():
    """
    Create or update launch + subagent for a user.
    Returns: api_key, assistant_name, sync_website, voice_type
    """
    connection = None
    try:
        data = request.get_json()
        print("dasdsa", data, session)
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
            print("no userid")
            return jsonify({"error": "User not logged in"}), 400
        if voice_type not in ["Man", "Woman"]:
            print("no voice")
            return jsonify({"error": "Invalid voice type"}), 400
        if not assistant_name:
            print("no name")
            return jsonify({"error": "Assistant name is required"}), 400
        if not sync_website:
            print("no website")
            return jsonify({"error": "Website is required"}), 400
        if not check_userid_valid(user_id):
            print("not a valid")
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
        if connection is not None:
            connection.close()


@agent_bps.route("/get-training-settings", methods=["GET"])
def get_training_settings():
    """
    It takes the user_id from the session or request,
    retrieves the launch_id and api_id for that user,
    and returns the subagent settings including assistant name, voice type, sync website, and api key.
    If the user is not logged in or no launch record is found, it returns an error message.
    """
    try:
        user_id = str(session.get("user_id") or request.args.get("user_id"))
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
    finally:
        if connection:
            connection.close()


@agent_bps.route("/process-query-key", methods=["POST"])
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
            if website != userwebsite and website != "dev.bytoid.ai":
                return (
                    jsonify({"error": "API key does not match the provided website"}),
                    401,
                )
            if not check_userid_valid(userid):
                return jsonify({"error": "Invalid access"}), 404

        response_data = []
        # Check for exact match in passed_ques.yaml
        passed_yaml_path = f"{userid}/yaml/passed_ques.yaml"
        valid_ones = load_yaml_from_s3(passed_yaml_path)
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


@agent_bps.route("/process-drive", methods=["POST"])
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
            print(all_downloaded_paths)
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
        redirect(f"{os.getenv('BASE_FRNT_URL')}/login")


@agent_bps.route("/clarifications", methods=["POST"])
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

    if not fetched_userid:
        return jsonify({"error": "User ID is required"}), 400

    failed_path = f"{fetched_userid}/yaml/failed_ques.yaml"
    failed_entries = load_yaml_from_s3(failed_path)

    if not failed_entries:
        logger.info("⚠ failed_ques.yaml not found or empty, regenerating QAs...")
        fetched_industry = get_line_of_business(fetched_userid)
        if not fetched_industry:
            return jsonify({"error": "No line of business present"}), 401

        # Load file metadata to get all Present files
        user_files_path = f"{fetched_userid}/yaml/users_fileData.yaml"

        file_data = load_yaml_from_s3(user_files_path) or []

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


@agent_bps.route("/clarification_update", methods=["POST"])
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

    passed_path = f"{fetched_userid}/yaml/passed_ques.yaml"
    failed_path = f"{fetched_userid}/yaml/failed_ques.yaml"

    passed_entries = load_yaml_from_s3(passed_path) or []
    failed_entries = load_yaml_from_s3(failed_path) or []
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
    # with open(passed_path, "w", encoding="utf-8") as pf:
    #     yaml.dump(passed_entries, pf, allow_unicode=True, sort_keys=False)

    # with open(failed_path, "w", encoding="utf-8") as ff:
    #     yaml.dump(failed_entries, ff, allow_unicode=True, sort_keys=False)
    if passed_entries:
        save_yaml_to_s3(passed_entries, userid, "passed_ques.yaml")
    if failed_entries:
        save_yaml_to_s3(failed_entries, userid, "failed_ques.yaml")

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


@agent_bps.route("/get-usersDocs", methods=["Get"])
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


@agent_bps.route("/delete_file", methods=["DELETE"])
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

    yaml_path = f"{userid}/yaml/users_fileData.yaml"
    # if not os.path.exists(yaml_path):
    #     return jsonify({"error": "No documents found for this user"}), 404

    # Load main file metadata YAML
    all_file_data = load_yaml_from_s3(yaml_path) or {}

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


# --- Main endpoint (updated) ---
@agent_bps.route("/get-ai-suggestion", methods=["POST"])
def get_ai_suggestion():
    try:
        data = request.json
        if not data or "usecase" not in data or "url" not in data:
            return jsonify({"error": "usecase and url are required"}), 400

        query_text = data["usecase"].strip()
        website_url = data["url"].strip()
        if not query_text or not website_url:
            return jsonify({"error": "Query and URL cannot be empty"}), 400

        userid = data.get("userid")
        if not userid:
            return jsonify({"error": "User ID is required"}), 400
        if not check_userid_valid(userid):
            return jsonify({"error": "Invalid access"}), 404

        # --- Load business context ---
        prompts = load_yaml_file(path=pathconfig.agent_template)
        QA_assist_prompt_template = prompts.get("business_owner_QA_assist")
        if not QA_assist_prompt_template:
            return jsonify({"error": "Prompt template not found"}), 500

        # --- Get user's business type ---
        fetched_industry = get_line_of_business(userid)

        # --- Scrape the website ---
        scraper = WebScrapingLanceClient(user_id=userid)
        scraped_data = scraper.scrape_website(
            url=website_url, use_selenium=True, max_depth=1
        )
        if not scraped_data:
            return jsonify({"error": "Scraping failed"}), 500

        # --- Save scraped data as JSON ---
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        json_filename = f"scrape_{timestamp}.json"
        json_path = os.path.join(
            "/home/ec2-user/bytoid/exe2/data/scrape_results", json_filename
        )
        # Convert to the expected format for saving
        scraped_data_for_json = {
            "url": scraped_data["url"],
            "title": scraped_data["title"],
            "results": [{"text": scraped_data["content"]}],  # Match expected format
            "metadata": scraped_data["metadata"],
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(scraped_data_for_json, f, ensure_ascii=False, indent=2)

        # --- Prepare context from scraped JSON ---
        context_text = scraped_data.get(
            "content", ""
        )  # Use content directly from enhanced scraper

        # --- Create final AI prompt ---
        full_prompt = QA_assist_prompt_template.format(
            question=query_text,
            business_type=fetched_industry,
        )
        full_prompt += f"\n\nHere is additional context from the company's website:\n{context_text}"

        # --- Get AI response ---
        ai_suggestion = get_fireworks_response(full_prompt, role="user")

        return jsonify({"suggestion": ai_suggestion, "scraped_file": json_path}), 200

    except Exception as e:
        print("❌ Error during AI suggestion processing:", e)
        return jsonify({"error": "Internal server error"}), 500


@agent_bps.route("/create-ticket", methods=["POST"])
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


@agent_bps.route("/process_audio", methods=["POST"])
def process_audio():
    print("request data", request.form, request.files)
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


@agent_bps.route("/get-audio-config", methods=["GET"])
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


@agent_bps.route("/update-transcript", methods=["POST"])
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


@agent_bps.route("/delete-audio", methods=["DELETE"])
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


# --- helper functions
def check_robots_txt(base_url, session):
    try:
        robots_url = urljoin(base_url, "/robots.txt")
        response = session.get(robots_url, timeout=5)
        if response.status_code == 200:
            paths = []
            for line in response.text.split("\n"):
                line = line.strip()
                if line.startswith(("Disallow:", "Allow:")):
                    path = line.split(":", 1)[1].strip().lstrip("/")
                    if path and path != "*" and not path.startswith("#"):
                        paths.append(path.split("?")[0])  # Remove query params
            return list(set(paths))
    except:
        pass
    return []


def check_endpoint(base_url, endpoint, session):
    try:
        url = urljoin(base_url, endpoint)
        response = session.get(url, timeout=5, allow_redirects=False)
        if response.status_code == 200:
            return {
                "endpoint": endpoint,
                "url": url,
                "status": response.status_code,
                "size": len(response.content),
                "accessible": True,
                "protected": False,
                "redirect": False,
            }
    except:
        pass
    return None


def discover_api_endpoints(content, base_url):
    import re

    endpoints = set()
    patterns = [
        r'["\']([^"\']*(?:/api/|/rest/|/graphql|/webhook)[^"\']*)["\']',
        r'url\s*:\s*["\']([^"\']+)["\']',
        r'fetch\s*\(\s*["\']([^"\']+)["\']',
        r'axios\.[a-z]+\s*\(\s*["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        matches = re.findall(pattern, content, re.IGNORECASE)
        for match in matches:
            if match.startswith("/") and not match.startswith("//"):
                endpoints.add(match.lstrip("/"))
            elif match.startswith(base_url):
                path = match.replace(base_url, "").lstrip("/")
                if path:
                    endpoints.add(path)
    return list(endpoints)



class WebScrapingLanceClient:
    def __init__(self, user_id: str):
        load_dotenv()
        self.lancedb_url = os.getenv("LANCE_DB_IP")
        self.user_id = user_id
        self.dimension = 3072
        self.embeddings = OpenAIEmbeddings(
            model="text-embedding-3-large",
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            dimensions=self.dimension,
        )
        # Selenium setup
        self.driver = None
        self.scraped_urls = set()
        self.max_depth = 3  # Maximum depth for multi-level scraping
        
        # YouTube processing setup (using yt-dlp for audio download + Whisper for transcription)
        self.speech_service = Speech2TextService(userid=self.user_id)

    def _setup_selenium_driver(self):
        """Setup Selenium Chrome driver with options."""
        if self.driver is None:
            try:
                from selenium import webdriver
                from selenium.webdriver.chrome.options import Options
                from selenium.webdriver.chrome.service import Service
                from webdriver_manager.chrome import ChromeDriverManager
                from selenium.webdriver.common.by import By
                from selenium.webdriver.support.ui import WebDriverWait
                from selenium.webdriver.support import expected_conditions as EC

                chrome_options = Options()
                chrome_options.add_argument("--headless")  # Run in background
                chrome_options.add_argument("--no-sandbox")
                chrome_options.add_argument("--disable-dev-shm-usage")
                chrome_options.add_argument("--disable-gpu")
                chrome_options.add_argument("--window-size=1920,1080")
                chrome_options.add_argument(
                    "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
                )

                service = Service(ChromeDriverManager().install())
                self.driver = webdriver.Chrome(service=service, options=chrome_options)
                print("✅ Selenium Chrome driver initialized successfully")

            except Exception as e:
                print(f"❌ Failed to setup Selenium driver: {e}")
                print("Falling back to requests-only scraping")
                self.driver = None

    def _cleanup_driver(self):
        """Cleanup Selenium driver."""
        if self.driver:
            try:
                self.driver.quit()
                self.driver = None
                print("✅ Selenium driver cleaned up")
            except Exception as e:
                print(f"Error cleaning up driver: {e}")

    def scrape_website(self, url: str, use_selenium: bool = True, max_depth: int = 3):
        """
        Enhanced scraping with Selenium support, multi-level depth scraping, and YouTube API integration.

        Args:
            url: The URL to scrape
            use_selenium: Whether to use Selenium for dynamic content
            max_depth: Maximum depth for branching (1-3 levels)
        """
        try:
            # 🎥 SPECIAL HANDLING: Check if this is a YouTube video
            if self._is_youtube_url(url):
                print(f"🎥 Detected YouTube video, using YouTube API...")
                youtube_result = self._scrape_youtube_video(url)
                
                # If YouTube API processing succeeded, return it
                if youtube_result:
                    print(f"✅ YouTube API processing successful!")
                    return youtube_result
                
                # If YouTube API processing failed, fall back to regular webpage scraping
                print(f"⚠️ YouTube API processing failed, falling back to webpage scraping...")
                print(f"🌐 Proceeding with regular webpage scraping for: {url}")

            self.max_depth = min(max_depth, 3)  # Cap at 3 levels
            self.scraped_urls.clear()

            if use_selenium:
                self._setup_selenium_driver()

            # Start scraping from the main URL
            main_content = self._scrape_single_page(
                url, use_selenium=use_selenium, current_depth=0
            )

            if not main_content:
                return None

            # Get all related content from branching
            all_scraped_content = []
            all_scraped_content.append(main_content)

            # Multi-level scraping
            if self.max_depth > 1:
                branch_content = self._scrape_branches(
                    main_content.get("all_links", []),
                    urllib.parse.urljoin(url, "/"),  # base domain
                    current_depth=1,
                    use_selenium=use_selenium,
                )
                all_scraped_content.extend(branch_content)

            # Combine all content
            combined_content = self._combine_scraped_content(all_scraped_content)

            # Add metadata about multi-level scraping
            combined_content["metadata"]["scraping_method"] = (
                "selenium" if use_selenium else "requests"
            )
            combined_content["metadata"]["max_depth_used"] = self.max_depth
            combined_content["metadata"]["total_pages_scraped"] = len(
                all_scraped_content
            )
            combined_content["metadata"]["unique_urls_scraped"] = len(self.scraped_urls)

            return combined_content

        except Exception as e:
            print(f"Error in enhanced scraping for {url}: {e}")
            return None
        finally:
            if use_selenium:
                self._cleanup_driver()

    def _scrape_single_page(
        self, url: str, use_selenium: bool = True, current_depth: int = 0
    ):
        """Scrape a single page using either Selenium or requests."""
        if url in self.scraped_urls:
            return None

        self.scraped_urls.add(url)

        try:
            if use_selenium and self.driver:
                return self._scrape_with_selenium(url, current_depth)
            else:
                return self._scrape_with_requests(url, current_depth)
        except Exception as e:
            print(f"Error scraping {url} at depth {current_depth}: {e}")
            return None

    def _scrape_with_selenium(self, url: str, current_depth: int = 0):
        """Scrape using Selenium for dynamic content."""
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC

            self.driver.get(url)

            # Wait for page to load
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )

            # Wait a bit more for dynamic content
            time.sleep(2)

            # Try to click on any "Load More" or "Show More" buttons
            self._handle_dynamic_loading()

            # Get page source after dynamic loading
            page_source = self.driver.page_source
            soup = BeautifulSoup(page_source, "html.parser")

            title = soup.find("title")
            title_text = title.get_text().strip() if title else url

            content = self._extract_content(soup)
            links = self._extract_links(soup, url)

            # Get additional Selenium-specific data
            page_height = self.driver.execute_script(
                "return document.body.scrollHeight"
            )
            viewport_height = self.driver.execute_script("return window.innerHeight")

            metadata = {
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "scraping_method": "selenium",
                "links_found": len(links),
                "content_length": len(content),
                "page_height": page_height,
                "viewport_height": viewport_height,
                "current_depth": current_depth,
                "links": links[:50],
            }

            return {
                "url": url,
                "title": title_text,
                "content": content,
                "metadata": metadata,
                "all_links": links,
            }

        except Exception as e:
            print(f"Selenium scraping error for {url}: {e}")
            # Fallback to requests
            return self._scrape_with_requests(url, current_depth)

    def _scrape_with_requests(self, url: str, current_depth: int = 0):
        """Fallback scraping using requests."""
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            }
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()

            soup = BeautifulSoup(response.content, "html.parser")

            title = soup.find("title")
            title_text = title.get_text().strip() if title else url

            content = self._extract_content(soup)
            links = self._extract_links(soup, url)

            metadata = {
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "scraping_method": "requests",
                "links_found": len(links),
                "content_length": len(content),
                "current_depth": current_depth,
                "links": links[:50],
            }

            return {
                "url": url,
                "title": title_text,
                "content": content,
                "metadata": metadata,
                "all_links": links,
            }
        except Exception as e:
            print(f"Requests scraping error for {url}: {e}")
            return None

    def _handle_dynamic_loading(self):
        """Handle dynamic content loading by clicking buttons and scrolling."""
        try:
            from selenium.webdriver.common.by import By
            from selenium.common.exceptions import (
                TimeoutException,
                NoSuchElementException,
            )

            # Common button texts for loading more content
            load_more_selectors = [
                "//button[contains(text(), 'Load More')]",
                "//button[contains(text(), 'Show More')]",
                "//button[contains(text(), 'View More')]",
                "//a[contains(text(), 'More')]",
                "//button[contains(@class, 'load-more')]",
                "//button[contains(@class, 'show-more')]",
            ]

            # Try clicking load more buttons
            for selector in load_more_selectors:
                try:
                    buttons = self.driver.find_elements(By.XPATH, selector)
                    for button in buttons[:3]:  # Limit to 3 clicks
                        if button.is_displayed() and button.is_enabled():
                            self.driver.execute_script("arguments[0].click();", button)
                            time.sleep(1)
                except:
                    continue

            # Scroll to bottom to trigger lazy loading
            last_height = self.driver.execute_script(
                "return document.body.scrollHeight"
            )
            scroll_attempts = 0
            max_scrolls = 3

            while scroll_attempts < max_scrolls:
                self.driver.execute_script(
                    "window.scrollTo(0, document.body.scrollHeight);"
                )
                time.sleep(2)

                new_height = self.driver.execute_script(
                    "return document.body.scrollHeight"
                )
                if new_height == last_height:
                    break

                last_height = new_height
                scroll_attempts += 1

        except Exception as e:
            print(f"Error handling dynamic loading: {e}")

    def _scrape_branches(
        self,
        links: list,
        base_domain: str,
        current_depth: int,
        use_selenium: bool = True,
    ):
        """Scrape branches up to max_depth levels."""
        if current_depth >= self.max_depth:
            return []

        branch_content = []
        processed_count = 0
        max_links_per_level = 10  # Limit links per level to avoid infinite crawling

        # Filter links to same domain only
        same_domain_links = []
        base_netloc = urllib.parse.urlparse(base_domain).netloc

        for link in links:
            try:
                link_netloc = urllib.parse.urlparse(link).netloc
                if link_netloc == base_netloc and link not in self.scraped_urls:
                    same_domain_links.append(link)
                    if len(same_domain_links) >= max_links_per_level:
                        break
            except:
                continue

        print(
            f"🔄 Scraping depth {current_depth}: Found {len(same_domain_links)} links to process"
        )

        for link in same_domain_links:
            if processed_count >= max_links_per_level:
                break

            page_content = self._scrape_single_page(
                link, use_selenium=use_selenium, current_depth=current_depth
            )

            if page_content:
                branch_content.append(page_content)
                processed_count += 1

                # Recursively scrape next level
                if current_depth + 1 < self.max_depth:
                    next_level_content = self._scrape_branches(
                        page_content.get("all_links", []),
                        base_domain,
                        current_depth + 1,
                        use_selenium,
                    )
                    branch_content.extend(next_level_content)

        print(f"✅ Completed depth {current_depth}: Scraped {processed_count} pages")
        return branch_content

    def _combine_scraped_content(self, content_list: list):
        """Combine content from multiple pages into a single structure."""
        if not content_list:
            return None

        main_content = content_list[0]  # First item is the main page

        # Combine all text content
        combined_text = main_content.get("content", "")
        all_titles = [main_content.get("title", "")]
        all_links = set(main_content.get("all_links", []))
        total_content_length = len(combined_text)

        # Add content from branch pages
        for item in content_list[1:]:
            if item and item.get("content"):
                combined_text += (
                    f"\n\n--- Content from {item.get('url', 'Unknown')} ---\n"
                )
                combined_text += item.get("content", "")
                all_titles.append(item.get("title", ""))
                all_links.update(item.get("all_links", []))
                total_content_length += len(item.get("content", ""))

        # Update metadata
        main_content["content"] = combined_text
        main_content["all_links"] = list(all_links)
        main_content["metadata"]["combined_content_length"] = total_content_length
        main_content["metadata"]["branch_titles"] = all_titles[1:]  # Exclude main title
        main_content["metadata"]["total_links_found"] = len(all_links)

        return main_content

    def _extract_content(self, soup):
        """Extract main text content from BeautifulSoup object."""
        # Remove script and style elements
        for script in soup(["script", "style", "nav", "footer", "aside"]):
            script.decompose()

        # Extract main content areas first
        main_content = ""

        # Try to find main content areas
        content_selectors = [
            "main",
            "article",
            ".content",
            ".main-content",
            "#content",
            "#main",
            ".post-content",
            ".entry-content",
        ]

        for selector in content_selectors:
            if "." in selector or "#" in selector:
                elements = soup.select(selector)
            else:
                elements = soup.find_all(selector)

            for element in elements:
                if element:
                    main_content += " " + element.get_text(separator=" ", strip=True)

        # If no main content found, get all text
        if not main_content.strip():
            text = soup.get_text(separator=" ", strip=True)
        else:
            text = main_content

        # Clean up the text
        lines = (line.strip() for line in text.splitlines() if line.strip())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        text = " ".join(chunk for chunk in chunks if chunk)

        return text

    def _extract_links(self, soup, base_url):
        """Extract all links from the page."""
        links = []
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            absolute_url = urllib.parse.urljoin(base_url, href)
            if absolute_url.startswith(("http://", "https://")):
                links.append(absolute_url)
        return list(set(links))

    def _check_yt_dlp_available(self) -> bool:
        """Check if yt-dlp is available for YouTube audio download."""
        try:
            subprocess.run(['yt-dlp', '--version'], capture_output=True, check=True)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            print("⚠️ yt-dlp not found. Install with: pip install yt-dlp")
            return False

    def _is_youtube_url(self, url: str) -> bool:
        """Check if URL is a YouTube video."""
        youtube_domains = [
            "youtube.com", "www.youtube.com", "youtu.be", "www.youtu.be",
            "m.youtube.com", "music.youtube.com"
        ]
        parsed_url = urllib.parse.urlparse(url.lower())
        return any(domain in parsed_url.netloc for domain in youtube_domains)

    def _extract_youtube_video_id(self, url: str) -> str:
        """Extract YouTube video ID from URL."""
        import re
        patterns = [
            r"(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/embed\/|youtube\.com\/v\/)([^&\n?#]+)",
            r"youtube\.com\/shorts\/([^&\n?#/]+)",
            r"youtube\.com\/watch\?.*v=([^&\n?#]+)",
        ]
        
        for pattern in patterns:
            match = re.search(pattern, url.lower())
            if match:
                video_id = match.group(1)
                # Clean up video ID
                video_id = video_id.split("&")[0].split("?")[0].split("#")[0]
                return video_id
        return None

    def _download_youtube_audio(self, url: str) -> dict:
        """Download YouTube audio using yt-dlp."""
        if not self._check_yt_dlp_available():
            return {"success": False, "error": "yt-dlp not available"}

        try:
            video_id = self._extract_youtube_video_id(url)
            if not video_id:
                return {"success": False, "error": "Could not extract video ID"}

            print(f"🎥 Downloading YouTube audio for video: {video_id}")

            # Create temporary file for audio
            temp_dir = tempfile.mkdtemp()
            audio_file = os.path.join(temp_dir, f"youtube_{video_id}.%(ext)s")

            # Download audio using yt-dlp
            cmd = [
                'yt-dlp',
                '--extract-audio',
                '--audio-format', 'mp3',
                '--audio-quality', '192K',
                '--no-playlist',
                '--output', audio_file,
                url
            ]

            print(f"🔄 Running yt-dlp command...")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

            if result.returncode != 0:
                print(f"❌ yt-dlp failed: {result.stderr}")
                return {"success": False, "error": f"yt-dlp failed: {result.stderr}"}

            # Find the actual downloaded file
            downloaded_files = [f for f in os.listdir(temp_dir) if f.startswith(f"youtube_{video_id}")]
            if not downloaded_files:
                return {"success": False, "error": "Audio file not found after download"}

            actual_audio_file = os.path.join(temp_dir, downloaded_files[0])
            
            # Extract basic video info from yt-dlp output
            title = "YouTube Video"
            duration = 0
            
            # Try to get video info
            try:
                info_cmd = ['yt-dlp', '--print', '%(title)s|%(duration)s', '--no-playlist', url]
                info_result = subprocess.run(info_cmd, capture_output=True, text=True, timeout=30)
                if info_result.returncode == 0:
                    parts = info_result.stdout.strip().split('|')
                    if len(parts) >= 2:
                        title = parts[0]
                        try:
                            duration = int(float(parts[1])) if parts[1] != 'None' else 0
                        except:
                            duration = 0
            except:
                pass

            print(f"✅ Audio downloaded: {title}")
            print(f"⏱️ Duration: {duration} seconds")

            return {
                "success": True,
                "audio_path": actual_audio_file,
                "title": title,
                "duration_seconds": duration,
                "video_id": video_id,
                "temp_dir": temp_dir
            }

        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Download timeout (5 minutes)"}
        except Exception as e:
            print(f"❌ Error downloading YouTube audio: {e}")
            return {"success": False, "error": f"Download error: {str(e)}"}

    def _scrape_youtube_video(self, url: str) -> dict:
        """Scrape YouTube video using yt-dlp + Whisper (via Fireworks AI)."""
        try:
            video_id = self._extract_youtube_video_id(url)
            if not video_id:
                return None

            print(f"🎥 Processing YouTube video: {video_id}")

            # Step 1: Download audio
            audio_result = self._download_youtube_audio(url)
            if not audio_result["success"]:
                print(f"❌ Failed to download audio: {audio_result['error']}")
                return None

            print(f"✅ Audio downloaded: {audio_result['title']}")
            
            # Step 2: Transcribe audio using Whisper (via Fireworks)
            try:
                print(f"🎧 Transcribing audio using Whisper...")
                transcript = asyncio.run(
                    self.speech_service.transcribe_audio(audio_result['audio_path'])
                )
                
                if not transcript:
                    print(f"⚠️ Whisper transcription returned empty result")
                    transcript = "No transcript could be generated for this video."
                else:
                    print(f"✅ Whisper transcription completed: {len(transcript)} characters")

            except Exception as e:
                print(f"❌ Whisper transcription failed: {e}")
                transcript = f"Transcription failed: {str(e)}"

            # Step 3: Clean up temporary files
            try:
                import shutil
                shutil.rmtree(audio_result['temp_dir'])
                print("🧹 Cleaned up temporary audio files")
            except Exception as e:
                print(f"⚠️ Could not clean up temp files: {e}")

            # Step 4: Combine content
            combined_content = f"""
Video Title: {audio_result['title']}
Video ID: {video_id}
Duration: {audio_result['duration_seconds']} seconds
Source URL: {url}

Transcript (Generated using Whisper AI):
{transcript}
            """.strip()

            # Step 5: Create metadata
            metadata = {
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "scraping_method": "youtube_yt-dlp_whisper",
                "video_id": video_id,
                "duration_seconds": audio_result['duration_seconds'],
                "content_length": len(combined_content),
                "transcript_length": len(transcript),
                "has_transcript": bool(transcript and transcript != "No transcript could be generated for this video."),
                "current_depth": 0,
                "links": [],
            }

            return {
                "url": url,
                "title": audio_result['title'],
                "content": combined_content,
                "metadata": metadata,
                "all_links": [],
                "youtube_data": {
                    "video_id": video_id,
                    "duration_seconds": audio_result['duration_seconds'],
                    "raw_transcript": transcript,
                    "has_transcript": metadata["has_transcript"],
                    "transcription_method": "whisper_fireworks"
                },
            }

        except Exception as e:
            print(f"❌ Error processing YouTube video: {e}")
            return None





        # Save to file (enhanced filename)
        base_dir = os.path.join("data", "scrape_results")
        ensure_dir(base_dir)

        scan_type = "comprehensive" if enable_directory_scan else "basic"
        filename = f"scrape_{scan_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        filepath = os.path.join(base_dir, filename)

        with open(filepath, "w") as f:
            json.dump(scrape_data, f, indent=4)

        return (
            jsonify({**scrape_data, "file_saved": filename, "status": "success"}),
            200,
        )

    # You can add your other methods like process_and_embed_scraped_data here...


@agent_bps.route("/scrape", methods=["POST"])
def scrape_website_route():
    """Enhanced web scraping with Selenium support and multi-level branching."""
    try:
        data = request.get_json()
        user_id = data.get("user_id")
        url_to_scrape = data.get("url")
        use_selenium = data.get(
            "use_selenium", True
        )  # Default to True for enhanced scraping
        max_depth = data.get("max_depth", 3)  # Default to 3 levels of branching

        if not user_id or not url_to_scrape:
            return jsonify({"error": "user_id and url are required"}), 400

        scraper = WebScrapingLanceClient(user_id=user_id)
        
        # Check if it's a YouTube URL
        is_youtube = scraper._is_youtube_url(url_to_scrape)

        print(f"🚀 Starting enhanced scraping for {url_to_scrape}")
        if is_youtube:
            print(f"   - Type: YouTube Video (yt-dlp + Whisper)")
            print(f"   - Max depth: N/A (YouTube videos don't have branching)")
        else:
            print(f"   - Type: Regular Website")
            print(f"   - Using Selenium: {use_selenium}")
            print(f"   - Max depth: {max_depth}")

        # --- Step 1: Enhanced scraping with multi-level support ---
        scraped_data = scraper.scrape_website(
            url=url_to_scrape, use_selenium=use_selenium, max_depth=max_depth
        )

        if not scraped_data:
            error_msg = (
                "Failed to scrape YouTube video" 
                if is_youtube 
                else "Failed to scrape the website content"
            )
            return jsonify({"error": error_msg}), 500

        print(f"✅ Scraping completed:")
        if is_youtube:
            print(f"   - Video title: {scraped_data.get('title', 'Unknown')}")
            print(f"   - Duration: {scraped_data['metadata'].get('duration_seconds', 0)} seconds")
            print(f"   - Has transcript: {scraped_data['metadata'].get('has_transcript', False)}")
        else:
            print(
                f"   - Pages scraped: {scraped_data['metadata'].get('total_pages_scraped', 1)}"
            )
            print(
                f"   - Content length: {scraped_data['metadata'].get('combined_content_length', 0)} chars"
            )
            print(
                f"   - Links found: {scraped_data['metadata'].get('total_links_found', 0)}"
            )

        # --- Step 2: Process the scraped text to get an embedding ---
        embedding_client = WebScrapingLanceClient(user_id=user_id)

        full_content = f"{scraped_data['title']}\n\n{scraped_data['content']}"
        embedding_vector = embedding_client.embeddings.embed_query(full_content)

        # --- Step 3: Prepare the payload for the LanceDB server ---
        lancedb_payload = {
            "user_id": user_id,
            "url": scraped_data["url"],
            "title": scraped_data["title"],
            "content": scraped_data["content"],
            "timestamp": scraped_data["metadata"]["scraped_at"],
            "metadata": scraped_data["metadata"],
            "embedding": embedding_vector,
        }

        # --- Step 4: Send the data to your LanceDB/FastAPI server ---
        lancedb_server_url = os.getenv("LANCE_DB_IP")
        if not lancedb_server_url:
            return (
                jsonify({"error": "LANCE_DB_IP environment variable is not set"}),
                500,
            )

        response = requests.post(
            f"{lancedb_server_url}/insert_scraped_data", json=lancedb_payload
        )

        # Check if the data was saved successfully
        if response.status_code == 200:
            success_message = (
                "YouTube video processed and data saved successfully using yt-dlp + Whisper."
                if is_youtube
                else "Website scraped and data saved successfully with enhanced multi-level scraping."
            )
            
            response_data = {
                "status": "success",
                "message": success_message,
                "scraped_content": scraped_data,
                "content_type": "youtube_video" if is_youtube else "website",
                "scraping_stats": {
                    "method": scraped_data["metadata"].get(
                        "scraping_method", "unknown"
                    ),
                    "total_content_length": scraped_data["metadata"].get(
                        "combined_content_length", len(scraped_data.get("content", ""))
                    ),
                },
                "lancedb_response": response.json(),
            }

            # Add content-type specific stats
            if is_youtube:
                response_data["youtube_stats"] = {
                    "video_id": scraped_data["metadata"].get("video_id", ""),
                    "duration_seconds": scraped_data["metadata"].get("duration_seconds", 0),
                    "view_count": scraped_data["metadata"].get("view_count", 0),
                    "like_count": scraped_data["metadata"].get("like_count", 0),
                    "comment_count": scraped_data["metadata"].get("comment_count", 0),
                    "has_transcript": scraped_data["metadata"].get("has_transcript", False),
                    "transcript_length": scraped_data["metadata"].get("transcript_length", 0),
                }
            else:
                # Add website-specific stats
                response_data["scraping_stats"].update(
                    {
                        "pages_scraped": scraped_data["metadata"].get(
                            "total_pages_scraped", 1
                        ),
                        "max_depth_used": scraped_data["metadata"].get(
                            "max_depth_used", 1
                        ),
                        "unique_urls": scraped_data["metadata"].get(
                            "unique_urls_scraped", 1
                        ),
                    }
                )

            return jsonify(response_data), 200
        else:
            return (
                jsonify(
                    {
                        "error": "Failed to save data to LanceDB server.",
                        "status_code": response.status_code,
                        "details": response.text,
                        "scraped_content_that_failed_to_save": scraped_data,
                    }
                ),
                500,
            )

    except Exception as e:
        logger.error(f"Error in /scrape route: {e}")
        traceback.print_exc()
        return (
            jsonify({"error": "An internal server error occurred", "details": str(e)}),
            500,
        )


# Flatten nested lists if any
def flatten_list(lst):
    flattened = []
    for item in lst:
        if isinstance(item, list):
            flattened.extend(flatten_list(item))
        else:
            flattened.append(item)
    return flattened


def append_passed_with_ai_diff(existing, new_entries):
    """
    Append new entries to existing if AI Response differs,
    keeping both old and new entries (no overwrite).
    """
    """
    Append new entries to existing:
    - Keep both old and new AI responses if they differ.
    - Avoid adding exact duplicates (same User, filename, Ai Response).
    """
    seen = set()  # (User, filename, Ai Response) triples

    # Add existing entries to seen
    for e in existing:
        key = (e.get("User"), e.get("filename"), e.get("Ai Response"))
        seen.add(key)

    for entry in new_entries:
        key = (entry.get("User"), entry.get("filename"), entry.get("Ai Response"))
        if key not in seen:
            existing.append(entry)
            seen.add(key)

    return existing


def summarize_scraped_data_advanced(scraped_json_data):
    """
    Takes scraped data, validates it, injects it into a prompt, and returns
    a natural language summary from the AI model.
    """
    try:
        url = scraped_json_data.get("url", "N/A")
        content = scraped_json_data.get("content", "")

        # ✅ FIX 1: Add a minimum content length check.
        # If the content is less than 250 characters, it's probably not summarizable.
        MIN_CONTENT_LENGTH = 250
        if not content or len(content.strip()) < MIN_CONTENT_LENGTH:
            logger.warning(
                f"Content for {url} is too short to summarize ({len(content)} chars)."
            )
            # Return a specific error code instead of None
            return "UNSUITABLE_CONTENT"

        # Load the updated prompt from your YAML file
        # Make sure the path in pathconfig.scrape_template is correct
        yaml_prompts = load_yaml_file(path=pathconfig.agent_template)
        summary_prompt_template = yaml_prompts.get("scrape_summary_prompt_template")

        if not summary_prompt_template:
            logger.error(
                "Prompt 'scrape_summary_prompt_template' not found in YAML file."
            )
            return None

        # Replace placeholders in the prompt
        full_prompt = summary_prompt_template.replace("{url}", str(url)).replace(
            "{website_content}", content
        )

        # Get the formatted text summary from the AI
        ai_response = get_fireworks_response(full_prompt, role="system")

        # Check if the AI response is valid
        if ai_response and isinstance(ai_response, str) and ai_response.strip():
            return ai_response.strip()
        else:
            logger.error(
                f"AI failed to generate a valid summary for {url}. Response: {ai_response}"
            )
            return None

    except Exception as e:
        logger.error(f"An exception occurred during summarization: {e}")
        traceback.print_exc()
        return None


@agent_bps.route("/scrape-and-summarize", methods=["POST"])
def scrape_and_summarize_route():
    """
    Handles adding a new website: scrapes, summarizes, embeds, saves to LanceDB,
    extracts clarifications, and returns the result for the frontend.
    """
    try:
        data = request.get_json()
        api_key = data.get("api_key")
        url_to_scrape = data.get("url")

        if not api_key or not url_to_scrape:
            return jsonify({"error": "api_key and url are required"}), 400

        user_id = fetch_userid_from_launch(api_key)
        if not user_id:
            return jsonify({"error": "Invalid API Key"}), 401

        # ADD THIS: Check for duplicates
        website_metadata_path = f"{user_id}/yaml/scraped_websites.yaml"
        existing_websites = load_yaml_from_s3(website_metadata_path) or []

        # Normalize URLs for comparison
        normalized_new_url = url_to_scrape.rstrip("/")
        for website in existing_websites:
            if website.get("status") == "active":
                existing_url = website.get("url", "").rstrip("/")
                if existing_url == normalized_new_url:
                    return (
                        jsonify(
                            {
                                "error": "Duplicate website found",
                                "message": f"Website '{url_to_scrape}' has already been added and processed.",
                                "existing_entry": website,
                            }
                        ),
                        409,
                    )

        # Step 1: Enhanced Scrape with multi-level support
        scraper = WebScrapingLanceClient(user_id=user_id)
        use_selenium = data.get("use_selenium", True)  # Allow frontend to control this
        max_depth = data.get(
            "max_depth", 2
        )  # Default to 2 levels for summarization (less than scrape route)

        print(f"🚀 Starting enhanced scraping for summarization: {url_to_scrape}")
        print(f"   - Type: Regular Website")
        print(f"   - Using Selenium: {use_selenium}")
        print(f"   - Max depth: {max_depth}")

        scraped_data = scraper.scrape_website(
            url=url_to_scrape, use_selenium=use_selenium, max_depth=max_depth
        )
        if not scraped_data:
            error_msg = "Failed to access or scrape website content."
            return jsonify({"error": error_msg}), 500

        print(f"✅ Enhanced scraping completed for summarization:")
        print(
            f"   - Pages scraped: {scraped_data['metadata'].get('total_pages_scraped', 1)}"
        )
        print(
            f"   - Content length: {scraped_data['metadata'].get('combined_content_length', 0)} chars"
            )

        # Step 2: Summarize
        summary_text = summarize_scraped_data_advanced(scraped_data)

        if summary_text == "UNSUITABLE_CONTENT":
            return (
                jsonify(
                    {
                        "error": "Website content could not be analyzed.",
                        "details": "The content was too short, may require a password, or is not suitable for summarization.",
                    }
                ),
                422,
            )

        if not summary_text:
            return (
                jsonify(
                    {"error": "The AI failed to generate a summary for the content."}
                ),
                500,
            )

        # Step 3: Extract clarifications from scraped content
        prompts = load_yaml_file(path=pathconfig.agent_template)
        clarification_prompt = prompts.get("extract_scraping_clarifications_prompt")

        # Evaluate scraped content for clarifications
        val = evaluate_scraped_content(clarification_prompt, scraped_data, summary_text)
        if not val:
            return (
                jsonify(
                    {"error": "Failed to evaluate scraped content for clarifications"}
                ),
                500,
            )

        # Step 4: Process clarifications if any exist
        if val["clarifications"]:
            clarific_scraping(
                user_id, val, url_to_scrape, scraped_data.get("title", "No Title")
            )

        # Step 5: Embed
        embedding_client = WebScrapingLanceClient(user_id=user_id)
        embedding_vector = embedding_client.embeddings.embed_query(summary_text)

        # Step 6: Prepare Payload for LanceDB
        timestamp = datetime.now(timezone.utc).isoformat()
        lancedb_payload = {
            "user_id": user_id,
            "url": url_to_scrape,
            "title": scraped_data.get("title", "No Title"),
            "content": summary_text,
            "timestamp": timestamp,
            "metadata": scraped_data.get("metadata", {}),
            "embedding": embedding_vector,
        }

        # Step 7: Save to LanceDB
        lancedb_server_url = os.getenv("LANCE_DB_IP")
        response = requests.post(
            f"{lancedb_server_url}/insert_scraped_data", json=lancedb_payload
        )

        if response.status_code != 200:
            logger.error(f"LanceDB Error: {response.text}")
            raise Exception("Failed to save data to LanceDB")
        # ADD THIS: Save website metadata to YAML
        website_metadata_path = f"{user_id}/yaml/scraped_websites.yaml"
        existing_websites = load_yaml_from_s3(website_metadata_path) or []

        website_entry = {
            "url": url_to_scrape,
            "title": scraped_data.get("title", "No Title"),
            "summary": summary_text,
            "timestamp": timestamp,
            "clarifications_count": len(val.get("clarifications", [])),
            "status": "active",
        }

        existing_websites.append(website_entry)
        save_yaml_to_s3(existing_websites, user_id, "scraped_websites.yaml")

        # Step 8: Validate clarifications using AI
        if val.get("clarifications"):
            validate_scraping_clarifications(user_id)

        # Step 9: Return the correct object for the frontend
        return (
            jsonify(
                {
                    "status": "success",
                    "summary": summary_text,
                    "url": url_to_scrape,
                    "timestamp": timestamp,
                    "clarifications_found": len(val.get("clarifications", [])),
                }
            ),
            200,
        )

    except Exception as e:
        logger.error(f"Error in /scrape-and-summarize route: {e}")
        traceback.print_exc()
        return (
            jsonify({"error": "An internal server error occurred", "details": str(e)}),
            500,
        )


def evaluate_scraped_content(clarification_prompt, scraped_data, summary_text):
    """
    Evaluate scraped content to extract clarifications using AI.
    Similar to evaluate_transcript but for web scraping.
    """
    try:
        # Combine scraped content for evaluation
        full_content = f"""
        URL: {scraped_data.get('url', '')}
        Title: {scraped_data.get('title', '')}
        Summary: {summary_text}
        Original Content Preview: {scraped_data.get('content', '')[:2000]}...
        """

        # Replace placeholder in prompt
        filled_prompt = clarification_prompt.replace(
            "{{scraped_content}}", full_content
        )

        # Get AI response
        ai_response = get_fireworks_response(filled_prompt, "system")

        # Parse the response (assuming it returns JSON with clarifications)
        try:
            result = json.loads(ai_response)
        except json.JSONDecodeError:
            import re

            json_text = re.search(r"\{.*\}", ai_response, re.DOTALL)
            result = json.loads(json_text.group(0)) if json_text else {}

        return {
            "summary": summary_text,
            "clarifications": result.get("clarifications", []),
            "clean_content": summary_text,
        }

    except Exception as e:
        logger.error(f"Error evaluating scraped content: {e}")
        return None


def clarific_scraping(user_id, val, url, title):
    """
    Process clarifications extracted from scraped content with duplicate prevention.
    """
    clarification_responses = []
    failed_key = f"{user_id}/yaml/failed_ques.yaml"

    failed_ques = flatten_list(load_yaml_from_s3(failed_key) or [])
    failed_data = failed_ques

    # Load existing clarifications to check for duplicates
    existing_questions = set()
    for existing_item in failed_data:  # Now failed_data is defined
        if existing_item.get("is_scraping") and existing_item.get("filename") == url:
            existing_questions.add(existing_item.get("User", "").strip().lower())

    quote_summary = val["summary"] if "summary" in val else title

    # Process new clarifications with duplicate check
    for actual_q in val.get("clarifications", []):
        actual_q = actual_q.strip()
        if not actual_q or actual_q.lower() in existing_questions:
            continue  # Skip duplicates

        entry_obj = {
            "User": actual_q,
            "Rephrased Question": actual_q,
            "Ai Response": "",
            "quote": quote_summary,
            "filename": url,
            "doc_value": 0,
            "is_scraping": True,
            "scrape_url": url,
            "scrape_title": title,
        }
        clarification_responses.append(entry_obj)
        existing_questions.add(actual_q.lower())

    # Merge old + new clarifications
    updated_data = failed_data + clarification_responses  # Now this works

    # Save back into YAML
    save_yaml_to_s3(data=updated_data, user_id=user_id, filename="failed_ques.yaml")

    return clarification_responses


def validate_scraping_clarifications(user_id):
    """
    Validate clarifications from failed_ques.yaml by getting answers and evaluating them.
    Similar to the document processing validation but for scraping clarifications.
    """
    try:
        # Load prompts
        prompts = load_yaml_file(path=pathconfig.agent_template)

        # File paths in S3
        passes_key = f"{user_id}/yaml/passed_ques.yaml"
        failed_key = f"{user_id}/yaml/failed_ques.yaml"

        passed_data = flatten_list(load_yaml_from_s3(passes_key) or [])
        failed_data = flatten_list(load_yaml_from_s3(failed_key) or [])

        # Filter only scraping-related clarifications that need validation
        scraping_clarifications = [
            item
            for item in failed_data
            if item.get("is_scraping") and not item.get("Ai Response")
        ]

        if not scraping_clarifications:
            logger.info("No scraping clarifications to validate")
            return

        # Get answers for clarifications
        content = fetch_scraping_ques_with_docs(scraping_clarifications, user_id)

        # Batch process for evaluation
        batch_size = 10
        valid_responses, updated_clarification_responses = [], []

        for i in range(0, len(content), batch_size):
            batch = content[i : i + batch_size]
            res_raw = evaluator_batch_llama_scraping(
                prompts.get("scraping_response_validator_batch"), batch
            )

            # Parse evaluator response
            try:
                # First try to find JSON array
                match = re.search(r"\[\s*\{.*?\}\s*\]", res_raw, re.DOTALL)
                if match:
                    json_str = match.group(0)
                    # Clean up any template artifacts
                    json_str = json_str.replace("{{", "{").replace("}}", "}")
                    res_json = json.loads(json_str)
                else:
                    # Fallback: try to parse the entire response
                    res_json = json.loads(res_raw)
            except json.JSONDecodeError as e:
                logger.error(f"❌ JSON parsing failed, trying YAML: {e}")
                try:
                    # Remove template artifacts before YAML parsing
                    clean_response = res_raw.replace("{{", "{").replace("}}", "}")
                    match = re.search(r"\[\s*\{.*?\}\s*\]", clean_response, re.DOTALL)
                    res_json = yaml.safe_load(match.group(0)) if match else []
                except Exception as yaml_e:
                    logger.error(f"❌ Both JSON and YAML parsing failed: {yaml_e}")
                    res_json = []
            except Exception as e:
                logger.error(f"❌ Unexpected error parsing evaluator response: {e}")
                res_json = []

            # Process evaluation results
            for original_item, eval_result in zip(batch, res_json):
                actual_q = original_item["query"]
                related_res = eval_result.get("related", False)
                usecase_res = eval_result.get("has_usecase_details", False)
                filename = original_item.get("filename", "").strip()

                # Find original clarification entry
                original_entry = None
                for item in scraping_clarifications:
                    if (
                        item.get("User") == actual_q
                        and item.get("filename") == filename
                    ):
                        original_entry = item
                        break

                if not original_entry:
                    continue

                entry_obj = {
                    "User": actual_q,
                    "Rephrased Question": original_entry.get("Rephrased Question", ""),
                    "Ai Response": eval_result.get("explanation", ""),
                    "quote": original_entry.get("quote", ""),
                    "filename": filename,
                    "doc_value": original_item.get("doc_value", ""),
                    "is_scraping": True,
                    "scrape_url": original_entry.get("scrape_url", ""),
                    "scrape_title": original_entry.get("scrape_title", ""),
                }

                if related_res and usecase_res:
                    entry_obj["date_processed"] = datetime.now().isoformat(
                        timespec="seconds"
                    )
                    valid_responses.append(entry_obj)
                else:
                    updated_clarification_responses.append(entry_obj)

        # Update passed questions
        npassed_data = append_passed_with_ai_diff(passed_data, valid_responses)

        # Remove answered questions from failed_data and add updated clarifications
        answered_keys = {(v.get("User"), v.get("filename")) for v in valid_responses}
        failed_data = [
            e
            for e in failed_data
            if not (
                e.get("is_scraping")
                and (e.get("User"), e.get("filename")) in answered_keys
            )
        ]

        # Update failed questions with new AI responses
        for updated_item in updated_clarification_responses:
            # Replace old entry with updated one
            for i, item in enumerate(failed_data):
                if (
                    item.get("User") == updated_item.get("User")
                    and item.get("filename") == updated_item.get("filename")
                    and item.get("is_scraping")
                ):
                    failed_data[i] = updated_item
                    break

        # Save back to S3
        if npassed_data:
            save_yaml_to_s3(npassed_data, user_id, "passed_ques.yaml")
        if failed_data:
            save_yaml_to_s3(failed_data, user_id, "failed_ques.yaml")

        logger.info(f"✅ Validated scraping clarifications for user {user_id}")

    except Exception as e:
        logger.error(f"Error validating scraping clarifications: {e}")
        traceback.print_exc()


def fetch_scraping_ques_with_docs(clarification_list, user_id):
    """
    Fetch answers for scraping clarifications using LanceDB.
    Similar to fetch_ques_with_docs but for scraping-based questions.
    """
    content = []

    for item in clarification_list:
        question_text = item.get("User", "").strip()
        filename = item.get("filename", "")  # This will be the URL

        if not question_text:
            continue

        # Get answer from LanceDB
        base_doc_ans = []
        if question_text:
            top_k = 3
            query_input = QueryInput(
                user_id=user_id, query_text=question_text, top_k=top_k
            )
            lance_client = LanceClient(user_id=user_id)
            results = lance_client.query_vector(query_input)
            for r in results:
                clean_text = r.get("text", "").encode().decode("unicode_escape")
                base_doc_ans.append(clean_text)

        response_text = (
            " ".join(base_doc_ans) if base_doc_ans else "No relevant information found."
        )

        content.append(
            {
                "query": question_text,
                "response_text": response_text,
                "filename": filename,
                "doc_value": item.get("doc_value", 0),
            }
        )

    return content


def evaluator_batch_llama_scraping(prompt_template_str, qa_list):
    """
    Evaluate scraping-based questions and answers using LLaMA.
    Similar to evaluator_batch_llama but specifically for scraping content.
    """
    qa_input_block = "\n".join(
        [
            f"{i+1}.\nUser Question: {item['query']}\nAI Response: {item['response_text']}"
            for i, item in enumerate(qa_list)
        ]
    )

    # Use replace instead of format to avoid KeyError with JSON braces
    full_prompt = prompt_template_str.replace("{qa_list}", qa_input_block)

    try:
        llama_response = get_fireworks_response(full_prompt, role="user")
        return llama_response
    except Exception as e:
        print(f"🔥 LLaMA Evaluator batch Error for scraping: {e}")
        return []


@agent_bps.route("/get-website-summaries", methods=["GET"])
def get_website_summaries():
    """Fetches all saved website summaries for a given user."""
    try:
        api_key = request.args.get("api_key")
        if not api_key:
            return jsonify({"error": "api_key is required"}), 400

        user_id = fetch_userid_from_launch(api_key)
        if not user_id:
            return jsonify({"error": "Invalid API Key"}), 401

        # Load from metadata YAML with better error handling
        website_metadata_path = f"{user_id}/yaml/scraped_websites.yaml"
        websites_data = load_yaml_from_s3(website_metadata_path)

        if websites_data is None:
            # File doesn't exist yet - return empty array
            logger.info(f"No scraped websites file found for user {user_id}")
            return jsonify([]), 200

        # Filter only active websites
        active_websites = [w for w in websites_data if w.get("status") == "active"]

        return jsonify(active_websites), 200

    except Exception as e:
        logger.error(f"Error fetching summaries: {e}")
        return jsonify({"error": str(e)}), 500


@agent_bps.route("/delete-website-summary", methods=["DELETE"])
def delete_website_summary():
    """Deletes a website summary and its related clarifications."""
    try:
        data = request.get_json()
        api_key = data.get("api_key")
        url_to_delete = data.get("url")

        if not api_key or not url_to_delete:
            return jsonify({"error": "api_key and url are required"}), 400

        user_id = fetch_userid_from_launch(api_key)
        if not user_id:
            return jsonify({"error": "Invalid API Key"}), 401

        # Step 1: Delete from LanceDB
        lance_client = LanceClient(user_id=user_id)
        delete_result = lance_client.delete_file_Data(foldername=url_to_delete)

        if delete_result.get("status") != "success":
            return (
                jsonify(
                    {
                        "error": "Failed to delete from LanceDB",
                        "details": delete_result.get("message"),
                    }
                ),
                500,
            )

        # Step 2: Update website metadata
        website_metadata_path = f"{user_id}/yaml/scraped_websites.yaml"
        websites_data = load_yaml_from_s3(website_metadata_path) or []

        updated_websites = []
        for website in websites_data:
            if website.get("url") == url_to_delete:
                website["status"] = "deleted"
                website["deleted_at"] = datetime.now().isoformat()
            updated_websites.append(website)

        save_yaml_to_s3(updated_websites, user_id, "scraped_websites.yaml")

        # Step 3: Delete related clarifications
        success = deletefilebasedData(url_to_delete, user_id)
        if not success:
            logger.warning(
                f"Failed to delete clarification entries for user {user_id}, URL {url_to_delete}"
            )

        return (
            jsonify(
                {
                    "message": "Website summary and related clarifications deleted successfully"
                }
            ),
            200,
        )

    except Exception as e:
        logger.error(f"Error deleting summary: {e}")
        return jsonify({"error": str(e)}), 500


@agent_bps.route("/get-clarifications", methods=["GET"])
def fetch_scraping_clarifications():
    try:
        api_key = request.args.get("api_key")
        if not api_key:
            return jsonify({"error": "api_key is required"}), 400

        user_id = fetch_userid_from_launch(api_key)
        if not user_id:
            return jsonify({"error": "Invalid API Key"}), 401

        # Load failed questions (clarifications)
        failed_key = f"{user_id}/yaml/failed_ques.yaml"
        failed_entries = flatten_list(
            load_yaml_from_s3(failed_key) or []
        )  # CHANGED: failed_data to failed_entries

        # Filter only scraping clarifications that need user input
        scraping_clarifications = [
            item
            for item in failed_entries  # CHANGED: failed_data to failed_entries
            if item.get("is_scraping") and not item.get("Ai Response")
        ]

        return (
            jsonify(
                {
                    "status": "success",
                    "clarifications": scraping_clarifications,
                    "total_count": len(scraping_clarifications),
                }
            ),
            200,
        )

    except Exception as e:
        logger.error(f"Error fetching clarifications: {e}")
        return jsonify({"error": str(e)}), 500


@agent_bps.route("/update-clarification", methods=["POST"])
def update_single_scraping_clarification():
    try:
        data = request.get_json()
        api_key = data.get("api_key")
        question_id = data.get("question_id")
        user_answer = data.get("answer")

        if not all([api_key, question_id, user_answer]):
            return jsonify({"error": "Missing required fields"}), 400

        user_id = fetch_userid_from_launch(api_key)
        if not user_id:
            return jsonify({"error": "Invalid API Key"}), 401

        # Load current failed questions
        failed_key = f"{user_id}/yaml/failed_ques.yaml"
        failed_entries = flatten_list(
            load_yaml_from_s3(failed_key) or []
        )  # CHANGED: failed_data to failed_entries

        # Find and update the specific clarification
        updated = False
        for item in failed_entries:  # CHANGED: failed_data to failed_entries
            if (item.get("User") + "|" + item.get("filename")) == question_id:
                item["Ai Response"] = user_answer
                item["user_provided_answer"] = True
                item["updated_at"] = datetime.now().isoformat()
                updated = True
                break

        if not updated:
            return jsonify({"error": "Clarification not found"}), 404

        # Save updated data
        save_yaml_to_s3(
            data=failed_entries, user_id=user_id, filename="failed_ques.yaml"
        )  # CHANGED: failed_data to failed_entries

        # Optionally trigger re-validation
        validate_scraping_clarifications(user_id)

        return jsonify({"status": "success", "message": "Clarification updated"}), 200

    except Exception as e:
        logger.error(f"Error updating clarification: {e}")
        return jsonify({"error": str(e)}), 500


@agent_bps.route("/check-dbfunc", methods=["POST"])
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
