import json
from datetime import datetime
from credits_route.route import Credits
from dotenv import load_dotenv
from flask import (
    Blueprint,
    request,
    jsonify,
    session,
)
from playbook.background_worker import JobManager
from utils.app_configs import DEV_ORIGINS
from utils.async_check import run_async
from agent_route.ag_helperzz import (
    remove_https_prefix,
)
from agent_route.doc_clarity import (
    flatten_list,
    preProcessDocWithUsecases,
    remove_transcript_clarifications,
)
from agent_route.lance_agent import LanceClient, QueryInput
from cust_helpers import pathconfig
from utils.base_logger import get_logger
from utils.fireworkzz import (
    evaluator_llama,
    get_fireworks_response,
)
from utils.normal import load_yaml_file, parse_composite_user_id
import uuid
import traceback
from db.rds_db import connect_to_rds, safe_execute
import re
from datetime import datetime
import yaml


from utils.s3_utils import (
    load_yaml_from_s3,
    save_yaml_to_s3,
)
from .task_manager import run_background_task, task_status
from db.db_checkers import (
    create_ticket_Communication_assigned,
    fetch_document_link,
    fetch_userid_from_launch,
    check_userid_valid,
    get_business_info,
    get_line_of_business,
)
import pymysql
from dotenv import load_dotenv
from collections import defaultdict
from datetime import datetime, timedelta
from request_context import current_user_id

# # Keep youtube_transcript_api as fallback
# try:
#     from youtube_transcript_api import YouTubeTranscriptApi

#     YOUTUBE_TRANSCRIPT_AVAILABLE = True
# except ImportError:
#     YOUTUBE_TRANSCRIPT_AVAILABLE = False

# # Add PyTube for fallback
# try:
#     from pytube import YouTube

#     PYTUBE_AVAILABLE = True
# except ImportError:
#     PYTUBE_AVAILABLE = False
import os

agent_bps = Blueprint("agents", __name__)
logger = get_logger(__name__)

load_dotenv()

user_query_history = defaultdict(list)
dev_val = DEV_ORIGINS


@agent_bps.route("/save-training-settings", methods=["POST"])
def save_training_settings():
    """
    Create or update launch + subagent for a user.
    Returns: api_key, assistant_name, sync_website, voice_type
    """
    connection = None
    try:
        data = request.get_json()
        # print("dasdsa", data, session)
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
            # print("no userid")
            return jsonify({"error": "User not logged in"}), 400
        logged_in_user_id, user_id = parse_composite_user_id(user_id)
        # if voice_type not in ["Man", "Woman"]:
        #     # print("no voice")
        #     return jsonify({"error": "Invalid voice type"}), 400
        if not assistant_name and not sync_website:
            # print("no name")
            return jsonify({"error": "Assistant name / website is required"}), 400
        # if not sync_website:
        #     # print("no website")
        #     return jsonify({"error": "Website is required"}), 400
        if not check_userid_valid(user_id):
            # print("not a valid")
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
                # print("Creating new launch and subagent")

                launch_id = str(uuid.uuid4())
                sub_agent_id = str(uuid.uuid4())
                api_key = str(uuid.uuid4())

                # Insert subagent
                safe_execute(
                    cursor,
                    """
                    INSERT INTO subagents (
                        sub_agent_id, launch_id_fk, name, description, voice_type,
                        documentation_link, model_version, created_at, updated_at
                    ) VALUES (%s, NULL, %s, 'Registered', %s, NULL, NULL, NOW(), NOW())
                """,
                    (sub_agent_id, assistant_name, voice_type),
                )

                # Insert launch
                safe_execute(
                    cursor,
                    """
                    INSERT INTO launch (
                        launch_id, sub_agent_id_fk, user_id_fk, api_id, website_name
                    ) VALUES (%s, %s, %s, %s, %s)
                """,
                    (launch_id, sub_agent_id, user_id, api_key, sync_website),
                )

                # Link subagent
                safe_execute(
                    cursor,
                    """
                    UPDATE subagents
                    SET launch_id_fk = %s
                    WHERE sub_agent_id = %s
                """,
                    (launch_id, sub_agent_id),
                )

            else:
                # print("Updating existing launch and subagent")

                launch_id, api_key = launch

                # Update launch
                safe_execute(
                    cursor,
                    """
                    UPDATE launch
                    SET website_name = %s
                    WHERE launch_id = %s
                """,
                    (sync_website, launch_id),
                )

                # Update subagent
                safe_execute(
                    cursor,
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
                            safe_execute(
                                cursor,
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
    If no launch record is found (e.g., for Outlook/Microsoft users), return default settings.
    """
    connection = None
    try:
        user_id = str(session.get("user_id") or request.args.get("user_id"))
        if not user_id:
            return jsonify({"error": "User not logged in"}), 401
        logged_in_user_id, user_id = parse_composite_user_id(user_id)
        if not check_userid_valid(user_id):
            return jsonify({"error": "Invalid Access"}), 404

        connection = connect_to_rds()

        with connection.cursor() as cursor:
            # Get launch_id for the user
            sql = "SELECT launch_id,api_id FROM launch WHERE user_id_fk = %s LIMIT 1"
            cursor.execute(sql, (user_id,))
            result = cursor.fetchone()

            # If no launch record, return default settings (for Outlook/Microsoft users)
            if result is None:
                return (
                    jsonify(
                        {
                            "assistant_name": "Assistant",
                            "voice_type": "default",
                            "sync_website": "",
                            "api_key": "",
                        }
                    ),
                    200,
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
                # Return default settings if no subagent settings found
                return (
                    jsonify(
                        {
                            "assistant_name": "Assistant",
                            "voice_type": "default",
                            "sync_website": "",
                            "api_key": api_id or "",
                        }
                    ),
                    200,
                )

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


@agent_bps.route("/process-query-key-og", methods=["POST"])
def checkquerywithApiKeyog():
    try:
        # print("Query made by:", session.get("user", {}))

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
        credits = Credits(db=connection)
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
            if website != userwebsite or website != dev_val:
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
        lance_client = LanceClient(user_id=userid, credits=credits)
        results = run_async(lance_client.query_vector(query_input))

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
        connection.commit()

        return jsonify(response_data), 200

    except Exception as e:
        connection.rollback()
        # print("❌ Error during query processing:", e)
        return jsonify({"error": str(e)}), 400
    finally:
        connection.close()


def get_website_url(api_key):
    """Return a list of active website url for a user."""

    user_id = fetch_userid_from_launch(api_key)
    website_metadata_path = f"{user_id}/yaml/scraped_websites.yaml"
    websites_data = load_yaml_from_s3(website_metadata_path) or []
    website_urls = [w.get("url") for w in websites_data if w.get("status") == "active"]
    return website_urls


def get_youtube_url(api_key):
    """Return a list of active you_tube url for a user."""

    user_id = fetch_userid_from_launch(api_key)
    youtube_metadata_path = f"{user_id}/yaml/scraped_youtube.yaml"
    youtube_data = load_yaml_from_s3(youtube_metadata_path) or []
    youtube_urls = [w.get("url") for w in youtube_data if w.get("status") == "active"]
    return youtube_urls


def parse_llm_response(response_text):
    """
    Robustly parse LLM response that might be JSON or YAML,
    possibly wrapped in markdown code fences or preceded by preamble text.

    Args:
        response_text: Raw text from LLM

    Returns:
        dict: Parsed content

    Raises:
        ValueError: If parsing fails after all attempts
    """
    if not response_text or not response_text.strip():
        raise ValueError("Empty response from LLM")

    cleaned = response_text.strip()
    # print(f"****cleaned : {cleaned}")

    # Strategy 1: Extract content between code fences
    code_fence_pattern = r"```(?:json|yaml|yml)?\s*\n(.*?)\n```"
    code_fence_match = re.search(code_fence_pattern, cleaned, re.DOTALL)
    if code_fence_match:
        cleaned = code_fence_match.group(1).strip()
    else:
        # Strategy 2: Remove leading markdown code fence markers
        cleaned = re.sub(
            r"^```(?:json|yaml|yml)?\s*\n", "", cleaned, flags=re.MULTILINE
        )
        cleaned = re.sub(r"\n```\s*$", "", cleaned, flags=re.MULTILINE)

    # Strategy 3: Try to find JSON object or YAML content after preamble
    # Look for content starting with { or a YAML key pattern
    json_match = re.search(r"(\{.*\})", cleaned, re.DOTALL)
    yaml_match = re.search(
        r"^([a-zA-Z_][\w]*\s*:.*)", cleaned, re.DOTALL | re.MULTILINE
    )

    # Prepare multiple candidates to try parsing
    candidates = [cleaned]

    if json_match:
        candidates.insert(0, json_match.group(1).strip())

    if yaml_match:
        candidates.insert(0, yaml_match.group(1).strip())

    # Try parsing each candidate
    errors = []

    for candidate in candidates:
        if not candidate:
            continue

        # Try JSON first (faster and more strict)
        try:
            result = json.loads(candidate)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError as e:
            errors.append(f"JSON parse error: {e}")

        # Try YAML (more forgiving)
        try:
            result = yaml.safe_load(candidate)
            if result is None:
                continue
            if isinstance(result, dict):
                return result
        except yaml.YAMLError as e:
            errors.append(f"YAML parse error: {e}")

    # If all attempts failed, raise detailed error
    error_msg = f"Failed to parse LLM response after trying all strategies.\n"
    error_msg += f"Errors encountered:\n" + "\n".join(f"  - {e}" for e in errors)
    error_msg += f"\n\nRaw output (first 500 chars):\n{response_text[:500]}"
    raise ValueError(error_msg)


async def generate_fallback_response(
    user_id, query, previous_query, previous_response, credits
):

    fallback_response = ""
    is_repeated = False

    # Normalize the query
    normalized_query = query.lower().strip()
    now = datetime.now()

    # Get this user's recent query history
    user_history = user_query_history[user_id]

    # Check how many times THIS USER asked THIS question in the last 10 minutes
    recent_same_queries = [
        q
        for q in user_history
        if q["query"] == normalized_query
        and (now - q["timestamp"]) < timedelta(minutes=10)
    ]
    repeated_len = len(recent_same_queries)

    # If this specific user has asked the same question 2+ times, give repeat response
    if repeated_len >= 1:
        # print(f"repeated_len : {repeated_len}")
        fallback_respone = load_yaml_file(path=pathconfig.query_validation)
        prompt_template = fallback_respone.get("fallback_repeated_question")
        filled_prompt = (
            prompt_template.replace(
                "{{user_query}}",
                json.dumps(query, ensure_ascii=False, indent=2),
            )
            .replace("{{repeat_count}}", str(repeated_len))
            .replace("{{previous_query}}", str(previous_query))
            .replace("{{previous_response}}", str(previous_response))
        )

        modified_yaml = await get_fireworks_response(
            user_message=filled_prompt, role="system", user_id=user_id, credits=credits
        )

        try:
            parsed_yaml = parse_llm_response(modified_yaml)
        except ValueError as e:
            # print(f"🔥 Fallback response parsing failed: {e}")
            return jsonify({"error": "Failed to parse fallback response"}), 500

        fallback_response = parsed_yaml.get("response")
        is_repeated = True

    # Store this query for THIS USER
    user_query_history[user_id].append({"query": normalized_query, "timestamp": now})

    # Clean up old queries for THIS USER (keep only last 10 minutes)
    user_query_history[user_id] = [
        q
        for q in user_query_history[user_id]
        if (now - q["timestamp"]) < timedelta(minutes=10)
    ]

    # Optional: Limit storage per user to prevent memory bloat
    if len(user_query_history[user_id]) > 50:
        user_query_history[user_id] = user_query_history[user_id][-50:]

    return {"fallback_response": fallback_response, "is_repeated": is_repeated}


async def semantically_repeated_response(
    user_id, query, previous_query, previous_response, credits
):

    fallback_response = ""
    is_repeated = False

    # Normalize the query
    normalized_query = query.lower().strip()
    now = datetime.now()

    fallback_respone = load_yaml_file(path=pathconfig.query_validation)
    prompt_template = fallback_respone.get("fallback_repeated_question")
    filled_prompt = (
        prompt_template.replace(
            "{{user_query}}",
            json.dumps(query, ensure_ascii=False, indent=2),
        )
        .replace("{{previous_query}}", str(previous_query))
        .replace("{{previous_response}}", str(previous_response))
    )

    modified_yaml = await get_fireworks_response(
        user_message=filled_prompt, role="system", user_id=user_id, credits=credits
    )

    try:
        parsed_yaml = parse_llm_response(modified_yaml)
    except ValueError as e:
        # print(f"🔥 Fallback response parsing failed: {e}")
        return jsonify({"error": "Failed to parse fallback response"}), 500

    fallback_response = parsed_yaml.get("response")

    return fallback_response


async def process_query_worker(data, job_id=None):
    connection = None
    try:
        # job_id = None
        import json, time, uuid, base64, asyncio, threading
        from websockets_custom.ws_instance import ws_service, msg_builder_main
        from runbook.utils import send

        print("started on query")

        msg_builder = msg_builder_main
        # print("Query made by:", session.get("user", {}))
        response_data = []
        summary_generated = ""
        previous_query = data.get("previous_query", "").strip()
        previous_response = data.get("previous_response").strip()
        querytext = data.get("query", "").strip()
        conversation_summary = data.get("conversation_summary")
        session_id = data.get("session_id")
        # print(f"conversation_summary received: {conversation_summary}")
        api_key = data.get("api_key")
        if not api_key:
            return {"error": "API key is required"}, 400
        website = (
            remove_https_prefix(data.get("website")) if data.get("website") else None
        )
        if not website:
            return {"error": "Website is required"}, 400
        if not querytext:
            return {"error": "Query is required"}, 400
        print(job_id, session_id)

        connection = connect_to_rds()
        credits = Credits(db=connection)

        with connection.cursor() as cursor:
            # Check if the API key exists for the user
            cursor.execute(
                "SELECT user_id_fk,website_name FROM launch WHERE api_id = %s ",
                (api_key,),
            )
            user_row = cursor.fetchone()
            if not user_row:
                return {"error": "Invalid API key"}, 401

            userid, userwebsite = user_row

            async def emit(msg):
                if job_id and session_id:
                    await send(ws_service, msg, userid)

            await emit(
                msg_builder.job_progress(
                    job_id,
                    session_id,
                    "INIT",
                    "Validating user information",
                    15,
                )
            )

            if website not in dev_val:
                if website != userwebsite:
                    return (
                        {"error": "API key does not match the provided website"},
                        401,
                    )
            if not check_userid_valid(userid):
                return jsonify({"error": "Invalid access"}), 404

        try:
            # check for repitative user queries
            repeated_check_ans = await generate_fallback_response(
                userid, querytext, previous_query, previous_response, credits
            )
            repeated_fallback_response = repeated_check_ans["fallback_response"]
            is_repeated = repeated_check_ans["is_repeated"]
            await emit(
                msg_builder.job_progress(
                    job_id,
                    session_id,
                    "diagnosing",
                    "Checking previous responses",
                    25,
                )
            )

            if is_repeated:
                await emit(
                    msg_builder.job_success(
                        job_id,
                        session_id,
                        "retrieved message",
                    )
                )
                response_data.append(
                    {
                        "id": "",
                        "match_score": "",
                        "extracted_answer": repeated_fallback_response,
                        "full_text": "",
                        "conversation_summary": conversation_summary,
                        "job_id": job_id,
                    }
                )
                return response_data, 200

            # validate the input query
            validated_respone = load_yaml_file(path=pathconfig.query_validation)
            template = validated_respone.get("query_validation")
            filled_prompt = (
                template.replace("{{message_text}}", str(querytext))
                .replace("{{previous_query}}", str(previous_query))
                .replace("{{previous_response}}", str(previous_response))
                .replace("{{conversation_summary}}", str(conversation_summary))
            )
            modified_yaml = await get_fireworks_response(
                user_message=filled_prompt,
                role="system",
                user_id=userid,
                credits=credits,
            )

            try:
                result = parse_llm_response(modified_yaml)
            except ValueError as e:
                # print(f"🔥 Query validation parsing failed: {e}")
                return (
                    {"error": "Failed to parse query validation response"},
                    500,
                )
            validated_query = result.get("question")
            type = result.get("type")
            summary_generated = result.get("summary_generated")
            # print(f"type : {type}")
            # print(f"summary : {summary_generated}")
            await emit(
                msg_builder.job_progress(
                    job_id,
                    session_id,
                    "diagnosing",
                    "validating the query",
                    25,
                )
            )

            if (
                type == "general"
                or type == "gratitude"
                or type == "emotional"
                or type == "unknown"
                or type == "abuse"
            ):
                response_data.append(
                    {
                        "id": "",
                        "match_score": "",
                        "extracted_answer": validated_query,
                        "full_text": "",
                        "conversation_summary": summary_generated,
                        "job_id": job_id,
                    }
                )
                await emit(
                    msg_builder.job_success(
                        job_id,
                        session_id,
                        "retrieved message",
                    )
                )
                connection.commit()
                return response_data, 200

            elif type == "repetition":
                response = await semantically_repeated_response(
                    userid, querytext, previous_query, previous_response, credits
                )
                response_data.append(
                    {
                        "id": "",
                        "match_score": "",
                        "extracted_answer": response,
                        "full_text": "",
                        "conversation_summary": summary_generated,
                        "job_id": job_id,
                    }
                )
                connection.commit()
                await emit(
                    msg_builder.job_success(
                        job_id,
                        session_id,
                        "retrieved message",
                    )
                )
                return response_data, 200

            else:
                await emit(
                    msg_builder.job_progress(
                        job_id,
                        session_id,
                        "checking",
                        "checking on the knowledge based",
                        35,
                    )
                )
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
                                    "conversation_summary": summary_generated,
                                    "job_id": job_id,
                                }
                            )
                            await emit(
                                msg_builder.job_success(
                                    job_id, session_id, "retrieved message success"
                                )
                            )
                            return response_data, 200

                # If no exact match, perform vector search
                base_doc_ans = []
                if validated_query:
                    top_k = 10
                    query_input = QueryInput(
                        user_id=userid, query_text=validated_query, top_k=top_k
                    )
                    lance_client = LanceClient(user_id=userid, credits=credits)
                    # results = run_async(lance_client.mixed_query_vector(query_input))
                    # print("***** before calling query_vector")
                    results = await lance_client.query_vector(query_input)
                    for r in results:
                        clean_text = r.get("text", "").encode().decode("unicode_escape")
                        base_doc_ans.append(clean_text)
                    # print("***** after calling query_vector")
                await emit(
                    msg_builder.job_progress(
                        job_id,
                        session_id,
                        "extracting",
                        "extracting business information",
                        45,
                    )
                )

                # Fetch business info
                businessdata = get_business_info(connection=connection, userid=userid)

                business_name = (
                    businessdata.get("BusinessName")
                    if businessdata
                    else "Our Organization"
                )
                business_address = (
                    businessdata.get("BillingAddress") if businessdata else ""
                )
                business_website = (
                    businessdata.get("WebsiteUrl") if businessdata else ""
                ) or ""

                # Build final prompt for AI reply
                prompt_template = validated_respone.get("base_eval_response")
                filled_prompt = (
                    prompt_template.replace(
                        "{{user_query}}",
                        json.dumps(querytext, ensure_ascii=False, indent=2),
                    )
                    .replace(
                        "{{base_doc_ans}}",
                        json.dumps(base_doc_ans, ensure_ascii=False, indent=2),
                    )
                    .replace("{{business_name}}", business_name)
                    .replace("{{business_address}}", business_address)
                    .replace("{{business_website}}", business_website)
                )

                modified_yaml = await get_fireworks_response(
                    user_message=filled_prompt,
                    role="system",
                    user_id=userid,
                    credits=credits,
                )

                try:
                    result = parse_llm_response(modified_yaml)
                except ValueError as e:
                    # print(f"🔥 Base evaluation parsing failed: {e}")
                    return (
                        {"error": "Failed to parse base evaluation response"},
                        500,
                    )

                base_response = result.get("response")
                no_answer_found = result.get("no_answer_found")
                if isinstance(no_answer_found, str):
                    val = no_answer_found.strip().lower()
                else:
                    val = str(no_answer_found).lower()

                if val in ["true", "yes", "1"]:
                    no_answer_found = True
                elif val == "partial":
                    no_answer_found = "Partial"
                else:
                    no_answer_found = False
                # print(f"base_response : {base_response}")
                # print(f"no_answer_found : {no_answer_found}")

                if not no_answer_found:
                    response_data.append(
                        {
                            "id": "",
                            "match_score": "",
                            "extracted_answer": base_response,
                            "full_text": "",
                            "conversation_summary": summary_generated,
                            "job_id": job_id,
                        }
                    )
                    connection.commit()
                    await emit(
                        msg_builder.job_success(
                            job_id, session_id, "retrieved message successfully"
                        )
                    )
                    return response_data, 200

                elif no_answer_found == "Partial":
                    # genereate fall back response when no_answer_found is true or partial
                    # print(f"inside partial part")

                    website_urls = get_website_url(api_key)
                    youtube_urls = get_youtube_url(api_key)

                    # print(f"website_urls: {website_urls}")
                    # print(f"youtube_urls: {youtube_urls}")

                    fallback_respone = load_yaml_file(path=pathconfig.query_validation)
                    template = fallback_respone.get("fallback_partial_answer")
                    filled_prompt = (
                        template.replace(
                            "{{website_urls}}",
                            ", ".join(website_urls) if website_urls else "",
                        )
                        .replace(
                            "{{youtube_urls}}",
                            ", ".join(youtube_urls) if youtube_urls else "",
                        )
                        .replace("{{base_response}}", base_response)
                        .replace("{{previous_query}}", str(previous_query))
                        .replace("{{previous_response}}", str(previous_response))
                    )
                    modified_yaml = await get_fireworks_response(
                        user_message=filled_prompt,
                        role="system",
                        user_id=userid,
                        credits=credits,
                    )

                    try:
                        parsed_yaml = parse_llm_response(modified_yaml)
                    except ValueError as e:
                        # print(f"🔥 Fallback response parsing failed: {e}")
                        return (
                            {"error": "Failed to parse fallback response"},
                            500,
                        )

                    fallback_response = parsed_yaml.get("response")

                    # print(f"fallback response: {fallback_response}")

                    response_data.append(
                        {
                            "id": "",
                            "match_score": "",
                            "extracted_answer": fallback_response,
                            "full_text": "",
                            "conversation_summary": summary_generated,
                            "job_id": job_id,
                        }
                    )
                    await emit(
                        msg_builder.job_success(
                            job_id,
                            session_id,
                            "retrieved message successfully",
                        )
                    )
                    connection.commit()
                    return response_data, 200

                else:
                    # print(f"inside true part")
                    await emit(
                        msg_builder.job_progress(
                            job_id,
                            session_id,
                            "rechecking",
                            "rechecking the previous responses",
                            45,
                        )
                    )
                    fallback_respone = load_yaml_file(path=pathconfig.query_validation)
                    prompt = fallback_respone.get("fallback_no_answer")
                    # print(f"str(querytext) : {str(querytext)}")
                    filled_prompt = (
                        prompt.replace("{{user_query}}", str(querytext))
                        .replace("{{previous_query}}", str(previous_query))
                        .replace("{{previous_response}}", str(previous_response))
                    )
                    modified_yaml = await get_fireworks_response(
                        user_message=filled_prompt,
                        role="system",
                        user_id=userid,
                        credits=credits,
                    )

                    try:
                        parsed_yaml = parse_llm_response(modified_yaml)
                    except ValueError as e:
                        # print(f"🔥 Fallback response parsing failed: {e}")
                        return (
                            {"error": "Failed to parse fallback response"},
                            500,
                        )

                    fallback_response = parsed_yaml.get("response")

                    # print(f"summary_generated send : {summary_generated}")

                    response_data.append(
                        {
                            "id": "",
                            "match_score": "",
                            "extracted_answer": fallback_response,
                            "full_text": "",
                            "conversation_summary": summary_generated,
                            "job_id": job_id,
                        }
                    )
                    await emit(
                        msg_builder.job_success(
                            job_id,
                            session_id,
                            "retrieved message successfully",
                        )
                    )
                    connection.commit()
                    return response_data, 200
        except Exception as e:
            # print(f"error in cehckquerywithApiKey:{e} ")
            return {"error": str(e)}, 400

    except Exception as e:
        # print("❌ Error during query processing:", e)
        connection.rollback()
        return {"error": str(e)}, 400
    finally:
        if connection:
            connection.close()


@agent_bps.route("/process-query-key", methods=["POST"])
async def checkquerywithApiKey():
    data = request.json

    job_id = await JobManager.submit_job(process_query_worker, data)

    return jsonify({"status": "accepted", "job_id": job_id}), 202


def getFilenameData(fetched_userid):
    # Load user files metadata YAML
    user_files_path = f"{fetched_userid}/yaml/users_fileData.yaml"
    file_data = load_yaml_from_s3(user_files_path) or []

    present_files = []

    if isinstance(file_data, dict):
        for key, entries in file_data.items():
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("FileStatus") == "Present":
                        if entry.get("filename"):
                            present_files.append(entry.get("filename"))
    elif isinstance(file_data, list):
        for entry in file_data:
            if isinstance(entry, dict) and entry.get("FileStatus") == "Present":
                if entry.get("filename"):
                    present_files.append(entry.get("filename"))
    return present_files


##print("filenamedataons3",getFilenameData("100805564263044911738"))


@agent_bps.route("/clarifications", methods=["POST"])
def makeuserDocClarifications(userid=None, industry=None):
    """
    Retrieves clarifications for a user based on failed questions.
    If the YAML files don't exist, triggers background QA generation.
    """
    data = request.json
    db = connect_to_rds()
    fetched_userid = data.get("userid") or userid
    logged_in_user_id, fetched_userid = parse_composite_user_id(fetched_userid)
    # print("fetched_userid", fetched_userid)
    try:

        if not check_userid_valid(fetched_userid, db):
            return jsonify({"error": "Invalid access"}), 404

        if fetched_userid:
            task_run = task_status.get(fetched_userid, {}).get("status", "not started")
            if task_run == "running":
                return (
                    jsonify(
                        {"message": "Currently generating clarifications for the user."}
                    ),
                    400,
                )

        if not fetched_userid:
            return jsonify({"error": "User ID is required"}), 400

        # Load failed questions YAML
        failed_path = f"{fetched_userid}/yaml/failed_ques.yaml"
        failed_entries = load_yaml_from_s3(failed_path) or []
        credits = Credits(db)

        if not failed_entries:
            logger.info("⚠ failed_ques.yaml not found or empty, regenerating QAs...")
            fetched_industry = get_line_of_business(fetched_userid, db)
            if not fetched_industry:
                return jsonify({"error": "No line of business present"}), 401

            present_files = getFilenameData(fetched_userid)

            if not present_files:
                return (
                    jsonify(
                        {"error": "Please upload a document to have clarifications"}
                    ),
                    404,
                )

            def pre_process_wrapper(**kwargs):
                import asyncio

                return asyncio.run(preProcessDocWithUsecases(**kwargs))

            # Trigger background QA generation
            result = run_background_task(
                userid=fetched_userid,
                industry=fetched_industry,
                filenames=present_files,
                credits=credits,
                func=pre_process_wrapper,
            )
            # print(f"[DEBUG] Background task queued: {result}")
            return (
                jsonify(
                    {"message": "Currently generating clarifications for the user."}
                ),
                400,
            )

        # Flatten nested lists if needed
        if isinstance(failed_entries[0], list):
            failed_entries = [item for sublist in failed_entries for item in sublist]

        # Build clarifications
        clarifications = []
        for entry in failed_entries:
            if isinstance(entry, dict):
                clarification = {
                    "usecase": entry.get("Rephrased Question", "").strip(),
                    "response": entry.get("Ai Response"),
                    "quote": entry.get("quote", "").strip(),
                }
                clarifications.append(clarification)

        if not clarifications:
            return "No clarifications required"

        return jsonify(clarifications)
    except Exception as e:
        db.rollback()
    finally:
        db.close()


@agent_bps.route("/clarification_update", methods=["POST"])
async def updateClarifications(userid=None, industry=None):
    data = request.json
    fetched_userid = data.get("userid") or userid
    if not fetched_userid:
        return jsonify({"error": "User ID is required"}), 400
    logged_in_user_id, fetched_userid = parse_composite_user_id(fetched_userid)
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
    # print(len(failed_entries), "failed entries length")

    prompts = load_yaml_file(path=pathconfig.agent_template)
    # Create mapping from rephrased question → original user question
    rephrased_to_user_map = {
        entry.get("Rephrased Question", "").strip(): entry.get("User", "").strip()
        for entry in failed_entries
    }
    db = connect_to_rds()
    credits = Credits(db)
    try:
        for query in fetched_queries:
            rephrased = query.get("usecase", "").strip()
            reply = query.get("reply", "").strip()
            usecase = rephrased_to_user_map.get(rephrased, "").strip()

            if not usecase:
                continue  # Skip blank entries

            res = await evaluator_llama(
                prompts.get("customer_response_checker"),
                usecase,
                reply,
                industry,
                credits=credits,
                userid=fetched_userid,
            )

            # If the LLaMA result is a string, extract JSON
            if isinstance(res, str):
                match = re.search(r"\{.*\}", res, re.DOTALL)
                if match:
                    try:
                        parsed_output = yaml.safe_load(match.group(0))
                    except Exception as e:
                        # print("❌ Failed to parse LLaMA JSON:", e)
                        parsed_output = {}
                else:
                    # print("❌ Could not extract JSON from LLaMA response")
                    parsed_output = {}
            else:
                parsed_output = res  # already a dict

            is_valid = parsed_output.get("is_valid", False)
            refined_response = parsed_output.get("refined_response", "").strip()

            failed_entries_backup = failed_entries[:]
            if is_valid:
                failed_entries = [
                    e for e in failed_entries if e.get("User", "") != usecase
                ]

                # Avoid duplicates in passed
                if not any(e.get("User", "") == usecase for e in passed_entries):
                    # Find the corresponding failed entry to get rephrased
                    matching_entry = next(
                        (
                            e
                            for e in failed_entries_backup
                            if e.get("User", "") == usecase
                        ),
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
                            "date_processed": datetime.now().isoformat(
                                timespec="seconds"
                            ),
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
                        (
                            e
                            for e in failed_entries_backup
                            if e.get("User", "") == usecase
                        ),
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
                        matching_query.get("reply", "").strip()
                        if matching_query
                        else ""
                    ),
                    "quote": entry.get("quote", "").strip() if "quote" in entry else "",
                }

                clarifications.append(clarification)
            db.commit()

            return (
                jsonify(
                    {
                        "clarifications": clarifications,
                        "message": "Clarifications partially updated",
                    }
                ),
                207,
            )
        db.commit()

        return (
            jsonify(
                {
                    "clarifications": [],
                    "message": "All clarifications updated successfully",
                }
            ),
            200,
        )
    except Exception as e:
        db.rollback()
        # print("error in update clarification", e)
    finally:
        db.close()


# --- Main endpoint (updated) ---
@agent_bps.route("/get-ai-suggestion", methods=["POST"])
async def get_ai_suggestion():
    try:
        data = request.json
        db = connect_to_rds()
        credits = Credits(db)
        if not data or "usecase" not in data or "url" not in data:
            return jsonify({"error": "usecase and url are required"}), 400

        query_text = data["usecase"].strip()
        website_url = data["url"].strip()
        if not query_text or not website_url:
            return jsonify({"error": "Query and URL cannot be empty"}), 400

        userid = data.get("userid")
        if not userid:
            return jsonify({"error": "User ID is required"}), 400
        logged_in_user_id, userid = parse_composite_user_id(userid)
        if not check_userid_valid(userid, db):
            return jsonify({"error": "Invalid access"}), 404

        # --- Load business context ---
        prompts = load_yaml_file(path=pathconfig.agent_template)
        QA_assist_prompt_template = prompts.get("business_owner_QA_assist")
        if not QA_assist_prompt_template:
            return jsonify({"error": "Prompt template not found"}), 500

        # --- Get user's business type ---
        fetched_industry = get_line_of_business(userid, db)

        # --- Create final AI prompt ---
        full_prompt = QA_assist_prompt_template.format(
            question=query_text,
            business_type=fetched_industry,
        )
        # full_prompt += f"\n\nHere is additional context from the company's website:\n{context_text}"

        # --- Get AI response ---
        try:
            ai_suggestion = await get_fireworks_response(
                user_message=full_prompt, role="user", user_id=userid, credits=credits
            )
        except Exception as e:
            # print(f"error in get_ai_suggestion:{e} ")
            return jsonify({"error", e}), 500

        # return jsonify({"suggestion": ai_suggestion, "scraped_file": json_path}), 200
        db.commit()
        return jsonify({"suggestion": ai_suggestion}), 200

    except Exception as e:
        # print("❌ Error during AI suggestion processing:", e)
        db.rollback()
        return jsonify({"error": "Internal server error"}), 500
    finally:
        db.close()


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
