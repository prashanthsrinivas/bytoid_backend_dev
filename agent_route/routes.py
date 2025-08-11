from flask import Blueprint, request, jsonify, session, redirect
from google_route.routes import get_token
from utils.fireworkzz import evaluator_llama, get_fireworks_response
from utils.normal import load_yaml_file
from .lance_agent import *
from .Drive_downloader import *
from .doc_clarity import *
import uuid
import traceback
from db.rds_db import connect_to_rds
import json
import re
from datetime import datetime
import yaml
from .task_manager import run_background_task, task_status
from db.db_checkers import (
    create_ticket_Communication_assigned,
    fetch_userid_from_launch,
    check_userid_valid,
    get_line_of_business,
)

# from app import qa_chain

agent_bp = Blueprint("agent", __name__)


def remove_https_prefix(url):
    """
    It removes the 'https://' or 'http://' prefix and 'www.' from the URL,
    and also removes any trailing slash at the end of the URL.
    """
    result = re.sub(r"^(https?://)?(www\.)?|/$", "", url)
    return result


@agent_bp.route("/save-training-settings", methods=["POST"])
def save_training_settings():
    """
    It makes the following changes to the database:
    - If the user does not have a launch, it creates a new launch and subagent
    - If the user has a launch, it updates the existing launch and subagent
    - It validates the input data for required fields and formats
    - creates api key for the user if it does not exist
    - It returns a success message or an error message based on the operation outcome
    """
    try:
        # Parse incoming JSON
        data = request.get_json()
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
            return jsonify({"error": "website is required"}), 400
        if not check_userid_valid(user_id):
            return jsonify({"error": "Invalid User ID"}), 404

        connection = connect_to_rds()

        with connection.cursor() as cursor:
            # Check if launch exists for user
            sql = "SELECT 1 FROM launch WHERE user_id_fk = %s LIMIT 1"
            cursor.execute(sql, (user_id,))
            exists = cursor.fetchone()

            if not exists:
                print("creating new launch and subagent")

                launch_id = str(uuid.uuid4())
                sub_agent_id = str(uuid.uuid4())
                new_api_key = str(uuid.uuid4())

                # Insert new subagent
                insert_subagent_sql = """
                    INSERT INTO subagents (
                        sub_agent_id, launch_id_fk, name, description, voice_type,
                        documentation_link, model_version, created_at, updated_at
                    ) VALUES (%s, NULL, %s, 'Registered', %s, NULL, NULL, NULL, NULL)
                """
                cursor.execute(
                    insert_subagent_sql, (sub_agent_id, assistant_name, voice_type)
                )

                # Insert new launch
                insert_launch_sql = """
                    INSERT INTO launch (
                        launch_id, sub_agent_id_fk, user_id_fk, api_id, website_name
                    ) VALUES (%s, %s, %s, %s, %s)
                """
                cursor.execute(
                    insert_launch_sql,
                    (launch_id, sub_agent_id, user_id, new_api_key, sync_website),
                )

                # Link subagent to launch
                link_sql = """
                    UPDATE subagents
                    SET launch_id_fk = %s
                    WHERE sub_agent_id = %s
                """
                cursor.execute(link_sql, (launch_id, sub_agent_id))

            else:
                print("updating existing launch and subagent")

                # Update website name
                update_launch_sql = """
                    UPDATE launch
                    SET website_name = %s
                    WHERE user_id_fk = %s
                """
                cursor.execute(update_launch_sql, (sync_website, user_id))

                # Get launch_id
                get_launch_sql = (
                    "SELECT launch_id FROM launch WHERE user_id_fk = %s LIMIT 1"
                )
                cursor.execute(get_launch_sql, (user_id,))
                result = cursor.fetchone()
                if result is None:
                    raise ValueError("No launch record found for given user_id")

                launch_id = result[0]

                # Update subagent settings
                update_subagent_sql = """
                    UPDATE subagents
                    SET name = %s,
                        voice_type = %s
                    WHERE launch_id_fk = %s
                """
                cursor.execute(
                    update_subagent_sql, (assistant_name, voice_type, launch_id)
                )

            connection.commit()
            return jsonify({"status": "success"})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


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
            return jsonify({"error": "Invalid User ID"}), 404

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


@agent_bp.route("/process-data")
def ProcesstoLancedb():
    """
    This function processes documents from a specified folder and inserts them into LanceDB.
    It initializes a LanceClient with a user ID, processes the documents in the specified folder,
    and returns the count of inserted documents.
    Note: The commented-out section is an example of how to process multiple files.
    """
    # files = [
    #     {"Bookstore_Profile.docx": "data/Bookstore_Profile.docx"},
    #     {"Footwear_Store_Profile.docx": "data/Footwear_Store_Profile.docx"},
    #     {"Savory_Haven_Bistro.docx": "data/Savory_Haven_Bistro.docx"},
    #     {"Convenience_Store_Profile.docx": "data/Convenience_Store_Profile.docx"},
    # ]
    userid = "2345"
    folderpath = "data/bytoid ai agent"
    lance_client = LanceClient(user_id=userid)
    inserted_count = lance_client.process_document(
        file_path=folderpath, foldername=folderpath
    )
    return inserted_count
    # for file_dict in files:
    #     for key, value in file_dict.items():  # Unpack each dictionary correctly
    #         lance_client = LanceClient(user_id=key)
    #         inserted_count = lance_client.process_document(
    #             file_path=value, foldername=key
    #         )
    # return "successfully processed files"


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


def deletefilebasedData(filename, userid):
    try:
        main_folder = f"{pathconfig.basepath}/{userid}"
        os.makedirs(main_folder, exist_ok=True)

        for ques_file in ["passed_ques.yaml", "failed_ques.yaml"]:
            ques_path = os.path.join(main_folder, ques_file)
            if os.path.exists(ques_path):
                with open(ques_path, "r") as f:
                    ques_data = yaml.safe_load(f) or []

                # Flatten in case there are nested lists
                flat_data = []
                for item in ques_data:
                    if isinstance(item, list):
                        flat_data.extend(item)
                    else:
                        flat_data.append(item)

                target_name = filename.strip().lower()

                filtered_data = []
                for q in flat_data:
                    if isinstance(q, dict):
                        file_value = (q.get("filename") or "").strip().lower()
                        if (
                            os.path.splitext(file_value)[0]
                            != os.path.splitext(target_name)[0]
                        ):
                            filtered_data.append(q)
                    else:
                        # Keep unexpected data untouched
                        filtered_data.append(q)

                if filtered_data:
                    with open(ques_path, "w") as f:
                        yaml.safe_dump(filtered_data, f, sort_keys=False)
                else:
                    os.remove(ques_path)

        return True

    except Exception as e:
        logging.error(
            f"Error deleting question entries for user {userid}, file {filename}: {e}",
            exc_info=True,
        )
        return False


@agent_bp.route("/process-drive", methods=["POST"])
def download_files():
    """
    Takes the picker metadata from the frontend and makes a sharable with the service account
    after completion of sharing we process the file or folder if not retry after 3-4 seconds
    then download the files in data folder
    after download preprocess with langchain and send it as embedding to lancedb
    """

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
    access_token = get_token(userid, value=True)
    print("mainaccess", access_token)
    connection = connect_to_rds()
    industry = None
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT LineOfBusiness FROM business_info WHERE user_id_fk = %s ", (userid,)
        )
        user_row = cursor.fetchone()
        if not user_row:
            return jsonify({"error": "No line of business present"}), 401
        industry = user_row[0]
        connection.close()
    user_service = None
    if access_token:
        user_service = GetEmailandDriveService(access_token)
        if user_service:
            all_downloaded_paths, is_downloaded = Mediatorservice(
                data, userid, user_service
            )
            if is_downloaded and len(all_downloaded_paths) > 0:
                folderpath = os.path.commonpath(all_downloaded_paths)
                processed_filenames = []
                for path in all_downloaded_paths:
                    filename = os.path.basename(
                        path
                    )  # e.g., "Convenience_Store_Profile.docx"
                    lance_client = LanceClient(user_id=userid)
                    result = lance_client.process_document(
                        file_path=path, filename=filename
                    )
                    if result.get("vectors_made", 0) > 0:
                        current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        processed_filenames.append(
                            {
                                "filename": filename,
                                "FileStatus": "Present",
                                "upload_date": current_date,
                                "updated_date": None,  # This will appear as 'null' in YAML
                            }
                        )
                        os.remove(path)
                        logger.info(f"[🗑] Deleted processed file: {path}")

                # Now merge with existing YAML
                if processed_filenames:
                    if os.path.isdir(folderpath):
                        shutil.rmtree(folderpath)

                    ensure_dir(f"{pathconfig.basepath}/{userid}")
                    yaml_path = os.path.join(
                        f"{pathconfig.basepath}/{userid}", "users_fileData.yaml"
                    )

                    # Load existing entries if present
                    if os.path.exists(yaml_path):
                        with open(yaml_path, "r") as f:
                            existing_data = yaml.safe_load(f) or []
                    else:
                        existing_data = []

                    # Append or update
                    for item in processed_filenames:
                        fname = item["filename"]
                        file_status = item["FileStatus"]

                        # Check if there is an existing *non-deleted* version
                        existing_non_deleted = next(
                            (
                                entry
                                for entry in existing_data
                                if entry["filename"] == fname
                                and entry["FileStatus"] != "Deleted"
                            ),
                            None,
                        )

                        if existing_non_deleted:
                            # Update the non-deleted entry
                            existing_non_deleted["updated_date"] = item["upload_date"]
                            existing_non_deleted["FileStatus"] = file_status
                        else:
                            # Either no entry, or only deleted ones — add new
                            existing_data.append(item)

                    # Write back the merged values
                    with open(yaml_path, "w") as f:
                        yaml.safe_dump(existing_data, f, sort_keys=False)

                indusries = get_industry_names_from_yaml(
                    f"{pathconfig.basepath}/smb_usecases.yaml"
                )
                matched_industry = find_matching_industry(industry, indusries)
                if matched_industry:
                    new_or_updated_files = [
                        item["filename"] for item in processed_filenames
                    ]
                    # need to make this queue process
                    result = run_background_task(
                        userid=userid,
                        industry=matched_industry,
                        filenames=new_or_updated_files,
                        func=preProcessDocWithUsecases,
                    )
                    print(f"[DEBUG] Background task queued: {result}")

                with open(yaml_path, "r") as f:
                    all_file_data = yaml.safe_load(f) or []

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


def get_usecases_for_smb(smb_name, data):
    for entry in data:
        if entry.get("SMB") == smb_name:
            return entry.get("Usecases", [])
    return []


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
        return jsonify({"error": "Invalid User ID"}), 404
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
        present_files = [
            entry["filename"]
            for entry in file_data
            if entry.get("FileStatus") == "Present"
        ]

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


def safe_load_yaml_entries(path):
    """Load YAML file and ensure entries are dictionaries with 'User' and 'Ai Response'."""
    entries = load_yaml_file(path)
    sanitized = []
    for entry in entries:
        if isinstance(entry, dict):
            sanitized.append(entry)
        elif isinstance(entry, list) and len(entry) == 2:
            sanitized.append({"User": entry[0], "Ai Response": entry[1]})
        else:
            print("⚠️ Skipping invalid entry in YAML:", entry)
    return sanitized


def normalize_question(entry):
    if isinstance(entry, dict):
        return entry.get("User", "").strip().lower()
    elif isinstance(entry, list):
        for item in entry:
            if isinstance(item, dict):
                return item.get("User", "").strip().lower()
    return ""


def log_removal(before_list, after_list):
    """Logs how many items were removed."""
    removed = len(before_list) - len(after_list)
    if removed > 0:
        print(f"✅ Removed {removed} matching entries from failed_entries")
    else:
        print("⚠️ No matching entries removed from failed_entries")


@agent_bp.route("/clarification_update", methods=["POST"])
def updateClarifications(userid=None, industry=None):
    data = request.json
    fetched_userid = data.get("userid") or userid
    if not fetched_userid:
        return jsonify({"error": "User ID is required"}), 400
    if not check_userid_valid(fetched_userid):
        return jsonify({"error": "Invalid User ID"}), 404

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
                    }
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
        return jsonify({"error": "Invalid User ID"}), 404
    yaml_path = f"{pathconfig.basepath}/{userid}/users_fileData.yaml"
    if not os.path.exists(yaml_path):
        return jsonify({"error": "No documents found for this user"}), 404

    with open(yaml_path, "r") as f:
        all_file_data = yaml.safe_load(f) or []

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

    if not userid or not filename:
        return jsonify({"error": "User ID and filename are required"}), 400
    if not check_userid_valid(userid):
        return jsonify({"error": "Invalid User ID"}), 404

    yaml_path = f"{pathconfig.basepath}/{userid}/users_fileData.yaml"
    if not os.path.exists(yaml_path):
        return jsonify({"error": "No documents found for this user"}), 404

    # Load main file metadata YAML
    with open(yaml_path, "r") as f:
        all_file_data = yaml.safe_load(f) or []

    # Step 1: Delete vectors from LanceDB
    lance_agent = LanceClient(user_id=userid)
    delete_result = lance_agent.delete_file_Data(foldername=filename)

    if delete_result["status"] != "success":
        return jsonify({"error": delete_result["message"]}), 500

    # Step 2: Update YAML entry with FileStatus = Deleted and updated_date
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    file_found = False

    for entry in all_file_data:
        if entry.get("filename") == filename:
            if entry.get("FileStatus", "").lower() != "deleted":
                entry["FileStatus"] = "Deleted"
                entry["updated_date"] = current_time
                file_found = True
            else:
                return jsonify({"error": "File is already marked as deleted"}), 400
            break

    if not file_found:
        return jsonify({"error": "Filename not found in YAML"}), 404

    # Step 3: Save updated users_fileData.yaml
    with open(yaml_path, "w") as f:
        yaml.safe_dump(all_file_data, f, sort_keys=False)

    success = deletefilebasedData(filename, userid)
    if not success:
        # Optionally log or handle this scenario
        logging.warning(
            f"Failed to delete question entries for user {userid}, file {filename}"
        )

    return (
        jsonify(
            {
                "message": "File deleted and related question entries removed successfully",
                "filename": filename,
                "status": "Deleted",
                "updated_date": current_time,
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
            return jsonify({"error": "Invalid User ID"}), 404

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
