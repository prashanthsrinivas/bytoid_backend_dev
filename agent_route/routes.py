import json
import requests
import os
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from datetime import timezone
from flask import Blueprint, request, jsonify, session, redirect
import yt_dlp
import asyncio
import tempfile
import os
import random
from agent_route.s_t_s import Speech2TextService

# Keep youtube_transcript_api as fallback
try:
    from youtube_transcript_api import YouTubeTranscriptApi

    YOUTUBE_TRANSCRIPT_AVAILABLE = True
except ImportError:
    YOUTUBE_TRANSCRIPT_AVAILABLE = False

# Add PyTube for fallback
try:
    from pytube import YouTube

    PYTUBE_AVAILABLE = True
except ImportError:
    PYTUBE_AVAILABLE = False
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from collections import deque

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
    get_evaluator_fireworks,
    get_firework_embedding,
    get_fireworks_response,
)
from utils.normal import ensure_dir, load_yaml_file
import uuid
import asyncio
import traceback
from db.rds_db import connect_to_rds, safe_execute
import re
from datetime import datetime
import yaml


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
    get_business_info,
    get_line_of_business,
    get_user_agent_id,
    update_agent_document_link,
)
import pymysql
from dotenv import load_dotenv
from collections import defaultdict
from datetime import datetime, timedelta



agent_bps = Blueprint("agents", __name__)
logger = get_logger(__name__)

load_dotenv()

user_query_history = defaultdict(list)


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
                print("Updating existing launch and subagent")

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


@agent_bps.route("/process-query-key-og", methods=["POST"])
def checkquerywithApiKeyog():
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
    print(f"****cleaned : {cleaned}")

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

def generate_fallback_response(user_id, query, previous_query, previous_response):
    
    fallback_response = ""
    is_repeated = False

    # Normalize the query
    normalized_query = query.lower().strip()
    now = datetime.now()
    
    # Get this user's recent query history
    user_history = user_query_history[user_id]
    
    # Check how many times THIS USER asked THIS question in the last 10 minutes
    recent_same_queries = [
        q for q in user_history 
        if q['query'] == normalized_query and (now - q['timestamp']) < timedelta(minutes=10)
    ]
    repeated_len = len(recent_same_queries)
    
    # If this specific user has asked the same question 2+ times, give repeat response
    if repeated_len >= 1:
            print(f"repeated_len : {repeated_len}")
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

            modified_yaml = get_fireworks_response(filled_prompt, "system")
            
            try:
                parsed_yaml = parse_llm_response(modified_yaml)
            except ValueError as e:
                print(f"🔥 Fallback response parsing failed: {e}")
                return jsonify({"error": "Failed to parse fallback response"}), 500

            fallback_response = parsed_yaml.get("response")
            is_repeated = True

    # Store this query for THIS USER
    user_query_history[user_id].append({
        'query': normalized_query,
        'timestamp': now
    })
     
    # Clean up old queries for THIS USER (keep only last 10 minutes)
    user_query_history[user_id] = [
        q for q in user_query_history[user_id] 
        if (now - q['timestamp']) < timedelta(minutes=10)
    ]
    
    # Optional: Limit storage per user to prevent memory bloat
    if len(user_query_history[user_id]) > 50:
        user_query_history[user_id] = user_query_history[user_id][-50:]
    
    return {
        "fallback_response":fallback_response,
        "is_repeated":is_repeated
    }


def semantically_repeated_response(user_id, query, previous_query, previous_response):
    
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

            modified_yaml = get_fireworks_response(filled_prompt, "system")
            
            try:
                parsed_yaml = parse_llm_response(modified_yaml)
            except ValueError as e:
                print(f"🔥 Fallback response parsing failed: {e}")
                return jsonify({"error": "Failed to parse fallback response"}), 500

            fallback_response = parsed_yaml.get("response")
    
            return fallback_response
                


@agent_bps.route("/process-query-key", methods=["POST"])
def checkquerywithApiKey():
    try:
        print("Query made by:", session.get("user", {}))
        response_data = []

        data = request.json
        previous_query = data.get("previous_query","").strip()
        previous_response = data.get("previous_response").strip()
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

        #check for repitative user queries
        repeated_check_ans = generate_fallback_response(userid, querytext, previous_query, previous_response)
        repeated_fallback_response = repeated_check_ans["fallback_response"]
        is_repeated = repeated_check_ans["is_repeated"]      
  
        if is_repeated:
            response_data.append(
                {
                    "id": "",
                    "match_score": "",
                    "extracted_answer": repeated_fallback_response,
                    "full_text": "",
                }
            )
            return jsonify(response_data), 200


        # validate the input query
        validated_respone = load_yaml_file(path=pathconfig.query_validation)
        template = validated_respone.get("query_validation")
        filled_prompt = (
            template.replace("{{message_text}}", str(querytext))
            .replace("{{previous_query}}", str(previous_query))
            .replace("{{previous_response}}", str(previous_response))
            )
        modified_yaml = get_fireworks_response(filled_prompt, role="system")

        try:
            result = parse_llm_response(modified_yaml)
        except ValueError as e:
            print(f"🔥 Query validation parsing failed: {e}")
            return jsonify({"error": "Failed to parse query validation response"}), 500
        validated_query = result.get("question")
        type = result.get("type")
        summary = result.get("summary")
        print(f"type : {type}")
        print(f"summary : {summary}")

        if type == "general" or type == "gratitude" or type == "emotional" or type == "unknown" or type == "abuse":
            response_data.append(
                {
                    "id": "",
                    "match_score": "",
                    "extracted_answer": validated_query,
                    "full_text": "",
                }
            )
            return jsonify(response_data), 200

        elif type == "repetition":
            response = semantically_repeated_response(userid, querytext, previous_query, previous_response)
            response_data.append(
                {
                    "id": "",
                    "match_score": "",
                    "extracted_answer": response,
                    "full_text": "",
                }
            )
            return jsonify(response_data), 200

            

        else:

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
            base_doc_ans = []
            if validated_query:
                top_k = 3
                query_input = QueryInput(
                    user_id=userid, query_text=validated_query, top_k=top_k
                )
                lance_client = LanceClient(user_id=userid)
                results = lance_client.query_vector(query_input)
                for r in results:
                    clean_text = r.get("text", "").encode().decode("unicode_escape")
                    base_doc_ans.append(clean_text)

            # Fetch business info
            businessdata = get_business_info(connection=connection, userid=userid)

            business_name = (
                businessdata.get("BusinessName") if businessdata else "Our Organization"
            )
            business_address = businessdata.get("BillingAddress") if businessdata else ""
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

            modified_yaml = get_fireworks_response(filled_prompt, "system")

            try:
                result = parse_llm_response(modified_yaml)
            except ValueError as e:
                print(f"🔥 Base evaluation parsing failed: {e}")
                return jsonify({"error": "Failed to parse base evaluation response"}), 500

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
            print(f"base_response : {base_response}")
            print(f"no_answer_found : {no_answer_found}")

            if not no_answer_found:
                response_data.append(
                    {
                        "id": "",
                        "match_score": "",
                        "extracted_answer": base_response,
                        "full_text": "",
                    }
                )
                return jsonify(response_data), 200
            
            elif no_answer_found == "Partial":
                # genereate fall back response when no_answer_found is true or partial
                print(f"inside partial part")

                website_urls = get_website_url(api_key)
                youtube_urls = get_youtube_url(api_key)

                print(f"website_urls: {website_urls}")
                print(f"youtube_urls: {youtube_urls}")

                fallback_respone = load_yaml_file(path=pathconfig.query_validation)
                template = fallback_respone.get("fallback_partial_answer")
                filled_prompt = template.replace(
                    "{{website_urls}}", ", ".join(website_urls) if website_urls else ""
                ).replace("{{youtube_urls}}", ", ".join(youtube_urls) if youtube_urls else ""
                ).replace("{{base_response}}", base_response
                ).replace("{{previous_query}}", str(previous_query)
                ).replace("{{previous_response}}", str(previous_response))
                modified_yaml = get_fireworks_response(filled_prompt, role="system")

                try:
                    parsed_yaml = parse_llm_response(modified_yaml)
                except ValueError as e:
                    print(f"🔥 Fallback response parsing failed: {e}")
                    return jsonify({"error": "Failed to parse fallback response"}), 500

                fallback_response = parsed_yaml.get("response")

                print(f"fallback response: {fallback_response}")

                response_data.append(
                    {
                        "id": "",
                        "match_score": "",
                        "extracted_answer": fallback_response,
                        "full_text": "",
                    }
                )
                return jsonify(response_data), 200

            else:
                print(f"inside true part")
                fallback_respone = load_yaml_file(path=pathconfig.query_validation)
                prompt = fallback_respone.get("fallback_no_answer")
                print(f"str(querytext) : {str(querytext)}")
                filled_prompt = (
                prompt.replace("{{user_query}}", str(querytext))
                .replace("{{previous_query}}", str(previous_query))
                .replace("{{previous_response}}", str(previous_response))
                )
                modified_yaml = get_fireworks_response(filled_prompt, role="system")

                try:
                    parsed_yaml = parse_llm_response(modified_yaml)
                except ValueError as e:
                    print(f"🔥 Fallback response parsing failed: {e}")
                    return jsonify({"error": "Failed to parse fallback response"}), 500

                fallback_response = parsed_yaml.get("response")

                print(f"fallback response: {fallback_response}")

                response_data.append(
                    {
                        "id": "",
                        "match_score": "",
                        "extracted_answer": fallback_response,
                        "full_text": "",
                    }
                )
                return jsonify(response_data), 200

    except Exception as e:
        print("❌ Error during query processing:", e)
        return jsonify({"error": str(e)}), 400
    finally:
        if connection:
            connection.close()


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

def getFilenameData(fetched_userid):
    # Load user files metadata YAML
        user_files_path = f"{fetched_userid}/yaml/users_fileData.yaml"
        file_data = load_yaml_from_s3(user_files_path) or []

        present_files = []

        if isinstance(file_data, dict):
            for key, entries in file_data.items():
                if isinstance(entries, list):
                    for entry in entries:
                        if (
                            isinstance(entry, dict)
                            and entry.get("FileStatus") == "Present"
                        ):
                            if entry.get("filename"):
                                present_files.append(entry.get("filename"))
        elif isinstance(file_data, list):
            for entry in file_data:
                if isinstance(entry, dict) and entry.get("FileStatus") == "Present":
                    if entry.get("filename"):
                        present_files.append(entry.get("filename"))
        return present_files
    
# print("filenamedataons3",getFilenameData("100805564263044911738"))

@agent_bps.route("/clarifications", methods=["POST"])
def makeuserDocClarifications(userid=None, industry=None):
    """
    Retrieves clarifications for a user based on failed questions.
    If the YAML files don't exist, triggers background QA generation.
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
                    {"message": "Currently generating clarifications for the user."}
                ),
                400,
            )

    if not fetched_userid:
        return jsonify({"error": "User ID is required"}), 400

    # Load failed questions YAML
    failed_path = f"{fetched_userid}/yaml/failed_ques.yaml"
    failed_entries = load_yaml_from_s3(failed_path) or []

    if not failed_entries:
        logger.info("⚠ failed_ques.yaml not found or empty, regenerating QAs...")
        fetched_industry = get_line_of_business(fetched_userid)
        if not fetched_industry:
            return jsonify({"error": "No line of business present"}), 401
        
        present_files=getFilenameData(fetched_userid)        

        if not present_files:
            return (
                jsonify({"error": "Please upload a document to have clarifications"}),
                404,
            )

        # Trigger background QA generation
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


class YouTubeScrapingClient:
    def __init__(self, user_id: str):
        load_dotenv()
        self.lancedb_url = os.getenv("LANCE_DB_IP")
        self.user_id = user_id
        self.dimension = 2880
        # self.embeddings = OpenAIEmbeddings(
        #     model="text-embedding-3-large",
        #     openai_api_key=os.getenv("OPENAI_API_KEY"),
        #     dimensions=self.dimension,
        # )
        self.embeddings = get_firework_embedding()
        self.speech_service = Speech2TextService(user_id)

        # Proxy list for rotation (add your proxy servers here)
        self.proxies = [
            # Add your proxy servers here
            # "http://proxy1:port",
            # "http://proxy2:port",
        ]

    def get_rotating_proxy(self):
        """Get a rotating proxy from available proxies"""
        if self.proxies:
            return random.choice(self.proxies)
        return None

    def extract_video_id(self, youtube_url):
        """Extract video ID from various YouTube URL formats"""
        patterns = [
            r"(?:https?:\/\/)?(?:www\.)?youtube\.com\/watch\?v=([a-zA-Z0-9_-]+)",
            r"(?:https?:\/\/)?(?:www\.)?youtu\.be\/([a-zA-Z0-9_-]+)",
            r"(?:https?:\/\/)?(?:www\.)?youtube\.com\/embed\/([a-zA-Z0-9_-]+)",
            r"(?:https?:\/\/)?(?:www\.)?youtube\.com\/v\/([a-zA-Z0-9_-]+)",
        ]

        for pattern in patterns:
            match = re.search(pattern, youtube_url)
            if match:
                return match.group(1)
        return None

    def extract_with_pytube(self, youtube_url):
        """Extract metadata and audio using PyTube"""
        if not PYTUBE_AVAILABLE:
            print(f"[YOUTUBE] PyTube not available")
            return None, None

        try:
            print(f"[YOUTUBE] Trying PyTube extraction for: {youtube_url}")
            yt = YouTube(youtube_url)

            # Get metadata
            metadata = {
                "title": yt.title or "YouTube Video",
                "author": yt.author or "Unknown",
                "duration": yt.length,
                "description": yt.description or "",
                "view_count": yt.views or 0,
                "upload_date": "",
            }

            print(
                f"[YOUTUBE] PyTube metadata: {metadata['title']} by {metadata['author']}"
            )

            # Download audio
            audio_stream = yt.streams.filter(only_audio=True).first()
            if not audio_stream:
                raise Exception("No audio stream available")

            # Download to temporary location
            temp_dir = tempfile.gettempdir()
            audio_file = audio_stream.download(output_path=temp_dir)

            # Rename to a clean name
            file_ext = os.path.splitext(audio_file)[1]
            clean_audio_path = os.path.join(
                temp_dir, f"youtube_audio_pytube_{os.getpid()}{file_ext}"
            )

            import shutil

            shutil.move(audio_file, clean_audio_path)

            print(f"[YOUTUBE] PyTube audio downloaded: {clean_audio_path}")
            return metadata, clean_audio_path

        except Exception as e:
            print(f"[YOUTUBE] PyTube extraction failed: {e}")
            return None, None

    def get_video_metadata_and_audio_with_proxy(self, youtube_url):
        """Get video metadata and extract audio using yt-dlp with proxy"""
        try:
            print(f"[YOUTUBE] Starting yt-dlp with proxy extraction for: {youtube_url}")
            proxy = self.get_rotating_proxy()

            with tempfile.TemporaryDirectory() as temp_dir:
                output_template = os.path.join(temp_dir, "audio.%(ext)s")

                ydl_opts = {
                    "format": "bestaudio",
                    "outtmpl": output_template,
                    "quiet": False,
                    "no_warnings": False,
                    # Enhanced headers to avoid bot detection
                    "http_headers": {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "en-us,en;q=0.5",
                        "Accept-Encoding": "gzip,deflate",
                        "Accept-Charset": "ISO-8859-1,utf-8;q=0.7,*;q=0.7",
                        "Keep-Alive": "300",
                        "Connection": "keep-alive",
                    },
                    "extractor_args": {
                        "youtube": {
                            "skip": ["hls", "dash"],
                            "player_skip": ["configs"],
                        }
                    },
                    "retries": 5,
                    "fragment_retries": 5,
                    "sleep_interval": 2,
                    "max_sleep_interval": 10,
                }

                # Add proxy if available
                if proxy:
                    ydl_opts["proxy"] = proxy
                    print(f"[YOUTUBE] Using proxy: {proxy}")

                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    print(f"[YOUTUBE] Extracting video info with proxy...")
                    info = ydl.extract_info(youtube_url, download=False)

                    metadata = {
                        "title": info.get("title", "YouTube Video"),
                        "author": info.get("uploader", info.get("channel", "Unknown")),
                        "duration": info.get("duration", None),
                        "description": info.get("description", ""),
                        "view_count": info.get("view_count", 0),
                        "upload_date": info.get("upload_date", ""),
                    }

                    print(
                        f"[YOUTUBE] Proxy extracted metadata: {metadata['title']} by {metadata['author']}"
                    )

                    print(f"[YOUTUBE] Starting audio download with proxy...")
                    ydl.download([youtube_url])

                    # Find downloaded file
                    audio_file = None
                    for file in os.listdir(temp_dir):
                        file_path = os.path.join(temp_dir, file)
                        if os.path.isfile(file_path):
                            print(
                                f"[YOUTUBE] Found file: {file} ({os.path.getsize(file_path)} bytes)"
                            )
                            audio_file = file_path
                            break

                    if not audio_file:
                        raise Exception("No audio file found after download")

                    # Copy to permanent location
                    import shutil

                    file_ext = os.path.splitext(audio_file)[1] or ".webm"
                    clean_audio_path = os.path.join(
                        tempfile.gettempdir(),
                        f"youtube_audio_proxy_{os.getpid()}{file_ext}",
                    )
                    shutil.copy2(audio_file, clean_audio_path)
                    print(f"[YOUTUBE] Proxy audio copied to: {clean_audio_path}")

                    return metadata, clean_audio_path

        except Exception as e:
            print(f"[YOUTUBE] yt-dlp with proxy extraction failed: {e}")
            return None, None

    def get_transcript_with_proxy(self, video_id):
        """Get transcript using YouTube Transcript API with proxy simulation"""
        if not YOUTUBE_TRANSCRIPT_AVAILABLE:
            print(f"[YOUTUBE] YouTube transcript API not available")
            return None

        try:
            print(
                f"[YOUTUBE] Trying transcript API with enhanced headers for {video_id}"
            )

            # Simulate different session/headers to avoid blocking
            import requests

            session = requests.Session()
            session.headers.update(
                {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.5",
                    "Accept-Encoding": "gzip, deflate",
                    "Connection": "keep-alive",
                    "Upgrade-Insecure-Requests": "1",
                }
            )

            # Add delay to avoid rate limiting
            import time

            time.sleep(random.uniform(1, 3))

            transcript_data = YouTubeTranscriptApi.get_transcript(video_id)
            print(f"[YOUTUBE] Transcript API found {len(transcript_data)} segments")

            # Combine transcript segments
            full_transcript = ""
            for entry in transcript_data:
                text = entry.get("text", "").strip()
                if text:
                    full_transcript += text + " "

            result = full_transcript.strip()
            print(f"[YOUTUBE] Transcript extracted: {len(result)} characters")
            print(f"[YOUTUBE] Transcript preview: {result[:200]}...")
            return result

        except Exception as e:
            error_msg = str(e)
            if (
                "YouTube is blocking requests from your IP" in error_msg
                or "cloud provider" in error_msg
            ):
                print(
                    f"[YOUTUBE] YouTube blocked transcript API (cloud provider restriction)"
                )
                return None
            else:
                print(f"[YOUTUBE] Transcript API with proxy simulation failed: {e}")
                return None

    def extract_transcript_selenium(self, youtube_url):
        """Extract transcript using browser automation (Selenium)"""
        try:
            print(f"[YOUTUBE] Trying Selenium transcript extraction for: {youtube_url}")

            # Setup Selenium driver (reuse existing setup)
            driver = (
                self._setup_selenium_driver()
                if hasattr(self, "_setup_selenium_driver")
                else None
            )

            if not driver:
                # Basic Chrome setup for transcript extraction
                from selenium.webdriver.chrome.service import Service

                chrome_options = Options()
                chrome_options.add_argument("--headless")
                chrome_options.add_argument("--no-sandbox")
                chrome_options.add_argument("--disable-dev-shm-usage")
                chrome_options.add_argument("--disable-gpu")
                chrome_options.add_argument(
                    "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )

                driver = webdriver.Chrome(options=chrome_options)

            driver.get(youtube_url)

            # Wait for page to load
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )

            # Try to find and click transcript button
            try:
                # Look for transcript button (multiple possible selectors)
                transcript_selectors = [
                    "//button[@aria-label='Show transcript']",
                    "//button[contains(@aria-label, 'transcript')]",
                    "//button[contains(text(), 'Transcript')]",
                    "//*[@id='transcript-button']",
                ]

                transcript_button = None
                for selector in transcript_selectors:
                    try:
                        transcript_button = driver.find_element(By.XPATH, selector)
                        break
                    except:
                        continue

                if transcript_button:
                    driver.execute_script("arguments[0].click();", transcript_button)
                    time.sleep(2)

                    # Extract transcript text
                    transcript_selectors = [
                        ".transcript-segment",
                        ".ytd-transcript-segment-renderer",
                        "[data-purpose='transcript-segment']",
                    ]

                    transcript_text = ""
                    for selector in transcript_selectors:
                        try:
                            elements = driver.find_elements(By.CSS_SELECTOR, selector)
                            if elements:
                                transcript_text = " ".join([el.text for el in elements])
                                break
                        except:
                            continue

                    if transcript_text:
                        print(
                            f"[YOUTUBE] Selenium transcript extracted: {len(transcript_text)} characters"
                        )
                        return transcript_text

            except Exception as e:
                print(f"[YOUTUBE] Selenium transcript button not found or failed: {e}")

            # Try to extract title and basic info even if transcript fails
            try:
                title_element = driver.find_element(
                    By.CSS_SELECTOR, "h1.ytd-video-primary-info-renderer"
                )
                title = title_element.text if title_element else "YouTube Video"
                print(f"[YOUTUBE] Selenium extracted title: {title}")

                # Could return basic metadata even without transcript
                return None
            except:
                pass

            return None

        except Exception as e:
            print(f"[YOUTUBE] Selenium transcript extraction failed: {e}")
            return None
        finally:
            if "driver" in locals():
                try:
                    driver.quit()
                except:
                    pass
        """Get video metadata and extract audio using yt-dlp"""
        try:
            print(f"[YOUTUBE] Starting yt-dlp extraction for: {youtube_url}")
            with tempfile.TemporaryDirectory() as temp_dir:
                # Use a clean filename template
                output_template = os.path.join(temp_dir, "audio.%(ext)s")

                ydl_opts = {
                    "format": "bestaudio",  # Just get best audio, no conversion
                    "outtmpl": output_template,
                    "quiet": False,  # Enable verbose output for debugging
                    "no_warnings": False,
                    # Enhanced headers to avoid bot detection
                    "http_headers": {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "en-us,en;q=0.5",
                        "Accept-Encoding": "gzip,deflate",
                        "Accept-Charset": "ISO-8859-1,utf-8;q=0.7,*;q=0.7",
                        "Keep-Alive": "300",
                        "Connection": "keep-alive",
                    },
                    # Add extractor arguments for YouTube
                    "extractor_args": {
                        "youtube": {
                            "skip": ["hls", "dash"],
                            "player_skip": ["configs"],
                        }
                    },
                    # Retry settings
                    "retries": 5,
                    "fragment_retries": 5,
                    # Add some delay to avoid rate limiting
                    "sleep_interval": 2,
                    "max_sleep_interval": 10,
                }

                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    print(f"[YOUTUBE] Extracting video info...")
                    # Get video info first
                    info = ydl.extract_info(youtube_url, download=False)

                    # Extract metadata
                    metadata = {
                        "title": info.get("title", "YouTube Video"),
                        "author": info.get("uploader", info.get("channel", "Unknown")),
                        "duration": info.get("duration", None),
                        "description": info.get("description", ""),
                        "view_count": info.get("view_count", 0),
                        "upload_date": info.get("upload_date", ""),
                    }

                    print(
                        f"[YOUTUBE] Extracted metadata: {metadata['title']} by {metadata['author']} ({metadata['duration']}s)"
                    )

                    print(f"[YOUTUBE] Starting audio download...")
                    # Download audio
                    ydl.download([youtube_url])

                    # Find the downloaded audio file
                    audio_file = None
                    print(f"[YOUTUBE] Checking temp directory: {temp_dir}")
                    for file in os.listdir(temp_dir):
                        file_path = os.path.join(temp_dir, file)
                        if os.path.isfile(file_path):
                            print(
                                f"[YOUTUBE] Found file: {file} ({os.path.getsize(file_path)} bytes)"
                            )
                            audio_file = file_path
                            break

                    if not audio_file:
                        raise Exception("No audio file found after download")

                    # Copy the file to a new location with a clean name since temp_dir will be deleted
                    import shutil

                    file_ext = os.path.splitext(audio_file)[1] or ".webm"
                    clean_audio_path = os.path.join(
                        tempfile.gettempdir(), f"youtube_audio_{os.getpid()}{file_ext}"
                    )
                    shutil.copy2(audio_file, clean_audio_path)
                    print(f"[YOUTUBE] Audio copied to: {clean_audio_path}")

                    return metadata, clean_audio_path

        except Exception as e:
            print(f"[YOUTUBE] yt-dlp extraction failed: {e}")
            import traceback

            traceback.print_exc()
            return None, None

    def get_transcript_fallback(self, video_id):
        """Fallback to YouTube transcript API if yt-dlp fails"""
        if not YOUTUBE_TRANSCRIPT_AVAILABLE:
            print(f"[YOUTUBE] YouTube transcript API not available")
            return None

        try:
            print(f"[YOUTUBE] Trying fallback transcript API for {video_id}")
            # Use the correct API method
            transcript_data = YouTubeTranscriptApi.get_transcript(video_id)
            print(
                f"[YOUTUBE] Found transcript data with {len(transcript_data)} segments"
            )

            # Combine transcript segments
            full_transcript = ""
            for entry in transcript_data:
                text = entry.get("text", "").strip()
                if text:
                    full_transcript += text + " "

            result = full_transcript.strip()
            print(f"[YOUTUBE] Transcript extracted: {len(result)} characters")
            print(f"[YOUTUBE] Transcript preview: {result[:200]}...")
            return result

        except Exception as e:
            error_msg = str(e)
            if (
                "YouTube is blocking requests from your IP" in error_msg
                or "cloud provider" in error_msg
            ):
                print(
                    f"[YOUTUBE] YouTube blocked our server IP (cloud provider restriction)"
                )
                return None
            else:
                print(f"[YOUTUBE] Fallback transcript API failed for {video_id}: {e}")
                import traceback

                traceback.print_exc()
                return None

    def split_audio_file(self, audio_file_path, max_duration_minutes=10):
        """Split long audio files into smaller segments for better transcription"""
        try:
            # For now, return the original file since ffmpeg is not available
            # This will be enhanced when ffmpeg is installed
            print(f"[YOUTUBE] ffmpeg not available, using original file (no splitting)")
            return [audio_file_path]

        except Exception as e:
            print(f"[YOUTUBE] Error splitting audio: {e}")
            return [audio_file_path]

    async def transcribe_audio_segments(self, audio_segments):
        """Transcribe multiple audio segments and combine them"""
        try:
            all_transcripts = []

            for i, segment_file in enumerate(audio_segments):
                print(
                    f"[YOUTUBE] Transcribing segment {i+1}/{len(audio_segments)}: {segment_file}"
                )
                print(f"[YOUTUBE] File exists: {os.path.exists(segment_file)}")
                if os.path.exists(segment_file):
                    print(f"[YOUTUBE] File size: {os.path.getsize(segment_file)} bytes")

                transcript = await self.speech_service.transcribe_audio(segment_file)
                if transcript:
                    all_transcripts.append(transcript)
                    print(f"[YOUTUBE] Segment {i+1} transcript: {transcript[:100]}...")
                else:
                    print(f"[YOUTUBE] No transcript for segment {i+1}")

                # Clean up segment file if it's different from original
                try:
                    if len(audio_segments) > 1 and segment_file != audio_segments[0]:
                        os.remove(segment_file)
                except:
                    pass

            # Combine all transcripts
            combined_transcript = " ".join(all_transcripts)
            print(
                f"[YOUTUBE] Combined transcript length: {len(combined_transcript)} characters"
            )

            return combined_transcript if combined_transcript.strip() else None

        except Exception as e:
            print(f"[YOUTUBE] Error transcribing segments: {e}")
            import traceback

            traceback.print_exc()
            return None

    async def transcribe_audio(self, audio_file_path):
        """Transcribe audio using the existing Speech2TextService with segmentation for long files"""
        try:
            print(f"[YOUTUBE] Starting transcription for: {audio_file_path}")

            # Split audio if it's too long (currently returns original file)
            audio_segments = self.split_audio_file(
                audio_file_path, max_duration_minutes=10
            )

            # Transcribe all segments
            transcript = await self.transcribe_audio_segments(audio_segments)

            return transcript
        except Exception as e:
            print(f"[YOUTUBE] Error in transcribe_audio: {e}")
            import traceback

            traceback.print_exc()
            return None

    def scrape_youtube_video(self, youtube_url):
        """Main method now using hybrid approach"""
        return self.scrape_youtube_video_hybrid(youtube_url)

    def scrape_youtube_video_hybrid(self, youtube_url):
        """Hybrid approach with multiple fallbacks for robust YouTube scraping"""
        import logging

        logger = logging.getLogger(__name__)

        video_id = self.extract_video_id(youtube_url)
        if not video_id:
            logger.error(f"[HYBRID] Failed to extract video ID from: {youtube_url}")
            return None

        logger.info(f"[HYBRID] Starting hybrid YouTube processing for: {youtube_url}")
        print(f"[HYBRID] Starting hybrid YouTube processing for: {youtube_url}")

        # Method priority order
        methods = [
            ("yt-dlp_with_proxy", lambda: self.extract_with_ytdlp_proxy(youtube_url)),
            ("pytube", lambda: self.extract_with_pytube(youtube_url)),
            (
                "transcript_api_proxy",
                lambda: self.get_transcript_only_with_proxy(video_id, youtube_url),
            ),
            ("selenium_transcript", lambda: self.extract_with_selenium(youtube_url)),
            ("original_ytdlp", lambda: self.get_video_metadata_and_audio(youtube_url)),
            (
                "original_transcript",
                lambda: self.get_transcript_only_original(video_id, youtube_url),
            ),
        ]

        for method_name, method_func in methods:
            try:
                logger.info(f"[HYBRID] 🔄 Trying {method_name}...")
                print(f"[HYBRID] 🔄 Trying {method_name}...")
                result = method_func()

                if result and (
                    (
                        isinstance(result, tuple) and result[0] and result[1]
                    )  # Audio + metadata
                    or (
                        isinstance(result, dict) and result.get("transcript_raw")
                    )  # Transcript result
                ):
                    logger.info(f"[HYBRID] ✅ Success with {method_name}")
                    print(f"[HYBRID] ✅ Success with {method_name}")

                    # Convert transcript-only result to full result format
                    if isinstance(result, dict) and "transcript_raw" in result:
                        return result

                    # Convert audio result to full format
                    if isinstance(result, tuple):
                        metadata, audio_file = result
                        return self.process_audio_to_transcript(
                            metadata, audio_file, youtube_url, video_id, method_name
                        )

            except Exception as e:
                logger.error(f"[HYBRID] ❌ {method_name} failed: {e}")
                print(f"[HYBRID] ❌ {method_name} failed: {e}")
                continue

        # If all methods fail, return informative error
        logger.error(f"[HYBRID] 🚫 All methods failed for {youtube_url}")
        print(f"[HYBRID] 🚫 All methods failed for {youtube_url}")
        return {
            "url": youtube_url,
            "video_id": video_id,
            "title": "YouTube Video",
            "content": "Unable to access this video - All extraction methods failed",
            "error": "all_methods_failed",
            "metadata": {
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "scraping_method": "hybrid_all_failed",
                "note": "Tried: yt-dlp with proxy, PyTube, transcript API, Selenium, and original methods",
            },
        }

    def extract_with_ytdlp_proxy(self, youtube_url):
        """Extract using yt-dlp with proxy support"""
        return self.get_video_metadata_and_audio_with_proxy(youtube_url)

    def get_video_metadata_and_audio(self, youtube_url):
        """Original method - fallback to proxy version"""
        return self.get_video_metadata_and_audio_with_proxy(youtube_url)

    def get_transcript_only_with_proxy(self, video_id, youtube_url):
        """Get transcript only using API with proxy simulation"""
        transcript = self.get_transcript_with_proxy(video_id)
        if transcript:
            # Try to get basic metadata
            try:
                oembed_url = (
                    f"https://www.youtube.com/oembed?url={youtube_url}&format=json"
                )
                response = requests.get(oembed_url, timeout=10)
                if response.status_code == 200:
                    oembed_data = response.json()
                    title = oembed_data.get("title", f"YouTube Video {video_id}")
                    author = oembed_data.get("author_name", "Unknown")
                else:
                    title = f"YouTube Video {video_id}"
                    author = "Unknown"
            except:
                title = f"YouTube Video {video_id}"
                author = "Unknown"

            formatted_content = f"""
**YouTube Video Analysis**

**Title:** {title}
**Author:** {author}
**Video URL:** {youtube_url}

**Transcript:**
{transcript}
"""

            return {
                "url": youtube_url,
                "video_id": video_id,
                "title": title,
                "content": formatted_content,
                "transcript_raw": transcript,
                "metadata": {
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                    "scraping_method": "transcript_api_with_proxy_simulation",
                    "author": author,
                    "video_id": video_id,
                    "content_length": len(transcript),
                },
            }
        return None

    def extract_with_selenium(self, youtube_url):
        """Extract using Selenium browser automation"""
        transcript = self.extract_transcript_selenium(youtube_url)
        if transcript:
            video_id = self.extract_video_id(youtube_url)
            return {
                "url": youtube_url,
                "video_id": video_id,
                "title": "YouTube Video (Selenium)",
                "content": f"**Transcript:**\n{transcript}",
                "transcript_raw": transcript,
                "metadata": {
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                    "scraping_method": "selenium_browser_automation",
                    "video_id": video_id,
                    "content_length": len(transcript),
                },
            }
        return None

    def get_transcript_only_original(self, video_id, youtube_url):
        """Get transcript using original method"""
        transcript = self.get_transcript_fallback(video_id)
        if transcript:
            return self.get_transcript_only_with_proxy(
                video_id, youtube_url
            )  # Reuse formatting
        return None

    def process_audio_to_transcript(
        self, metadata, audio_file, youtube_url, video_id, method_name
    ):
        """Process audio file to transcript using Whisper"""
        try:
            # Transcribe audio using async function
            import concurrent.futures

            def run_transcription():
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    return loop.run_until_complete(self.transcribe_audio(audio_file))
                finally:
                    loop.close()

            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(run_transcription)
                transcript = future.result(timeout=300)  # 5 minute timeout

            if not transcript:
                return {
                    "url": youtube_url,
                    "video_id": video_id,
                    "title": metadata["title"],
                    "content": "Failed to transcribe audio from this video",
                    "error": "transcription_failed",
                    "metadata": {
                        "scraped_at": datetime.now(timezone.utc).isoformat(),
                        "scraping_method": f"{method_name}_transcription_failed",
                        "author": metadata["author"],
                        "video_id": video_id,
                    },
                }

            # Format content with video info and transcript
            formatted_content = f"""
**YouTube Video Analysis**

**Title:** {metadata['title']}
**Author:** {metadata['author']}
**Video URL:** {youtube_url}
**Duration:** {metadata['duration']} seconds

**Transcript:**
{transcript}
"""

            return {
                "url": youtube_url,
                "video_id": video_id,
                "title": metadata["title"],
                "content": formatted_content,
                "transcript_raw": transcript,
                "metadata": {
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                    "scraping_method": f"{method_name}_with_whisper",
                    "author": metadata["author"],
                    "video_id": video_id,
                    "duration": metadata["duration"],
                    "content_length": len(transcript),
                    "description": (
                        metadata["description"][:500] if metadata["description"] else ""
                    ),
                },
            }

        except Exception as e:
            print(f"[HYBRID] Error processing audio to transcript: {e}")
            return None
        finally:
            # Cleanup temporary audio file
            if audio_file and os.path.exists(audio_file):
                try:
                    os.remove(audio_file)
                except:
                    pass
        """Main method to scrape YouTube video and extract transcript using yt-dlp + Whisper"""
        audio_file = None
        try:
            print(f"[YOUTUBE] Processing video: {youtube_url}")

            # Extract video ID
            video_id = self.extract_video_id(youtube_url)
            if not video_id:
                return None

            # Get video metadata and audio
            metadata, audio_file = self.get_video_metadata_and_audio(youtube_url)
            if not metadata or not audio_file:
                print(f"[YOUTUBE] yt-dlp failed, trying fallback transcript API...")
                # Try fallback to transcript API
                transcript = self.get_transcript_fallback(video_id)
                if transcript:
                    print(
                        f"[YOUTUBE] Fallback transcript successful: {len(transcript)} chars"
                    )
                    # Try to get comprehensive metadata using multiple methods
                    title = f"YouTube Video {video_id}"
                    author = "Unknown"
                    duration = None

                    try:
                        # Method 1: Try oembed API
                        oembed_url = f"https://www.youtube.com/oembed?url={youtube_url}&format=json"
                        response = requests.get(oembed_url, timeout=10)
                        if response.status_code == 200:
                            oembed_data = response.json()
                            title = oembed_data.get("title", title)
                            author = oembed_data.get("author_name", author)
                            print(f"[YOUTUBE] oEmbed metadata: {title} by {author}")
                    except Exception as e:
                        print(f"[YOUTUBE] oEmbed failed: {e}")

                    try:
                        # Method 2: Try yt-dlp info extraction without download
                        ydl_opts = {
                            "quiet": True,
                            "no_warnings": True,
                            "skip_download": True,
                        }
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            info = ydl.extract_info(youtube_url, download=False)
                            title = info.get("title", title)
                            author = info.get("uploader", info.get("channel", author))
                            duration = info.get("duration", duration)
                            print(f"[YOUTUBE] yt-dlp info: {title} by {author}")
                    except Exception as e:
                        print(f"[YOUTUBE] yt-dlp info extraction failed: {e}")

                    # Use extracted metadata
                    formatted_content = f"""
**YouTube Video Analysis**

**Title:** {title}
**Author:** {author}
**Video URL:** {youtube_url}
{f"**Duration:** {duration} seconds" if duration else ""}

**Transcript:**
{transcript}
"""
                    return {
                        "url": youtube_url,
                        "video_id": video_id,
                        "title": title,
                        "content": formatted_content,
                        "transcript_raw": transcript,
                        "metadata": {
                            "scraped_at": datetime.now(timezone.utc).isoformat(),
                            "scraping_method": "youtube_transcript_api_fallback",
                            "author": author,
                            "video_id": video_id,
                            "duration": duration,
                            "content_length": len(transcript),
                        },
                    }
                else:
                    return {
                        "url": youtube_url,
                        "video_id": video_id,
                        "title": "YouTube Video",
                        "content": "Unable to access this video - YouTube is blocking requests from cloud servers",
                        "error": "youtube_ip_blocked",
                        "metadata": {
                            "scraped_at": datetime.now(timezone.utc).isoformat(),
                            "scraping_method": "blocked_by_youtube",
                            "note": "YouTube blocks most cloud provider IPs (AWS, Google Cloud, Azure) to prevent automated access. This affects both audio download and transcript extraction.",
                        },
                    }

            # Transcribe audio - run in a new thread to avoid event loop conflicts
            import concurrent.futures

            def run_transcription():
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    return loop.run_until_complete(self.transcribe_audio(audio_file))
                finally:
                    loop.close()

            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(run_transcription)
                transcript = future.result(timeout=300)  # 5 minute timeout

            if not transcript:
                return {
                    "url": youtube_url,
                    "video_id": video_id,
                    "title": metadata["title"],
                    "content": "Failed to transcribe audio from this video",
                    "error": "transcription_failed",
                    "metadata": {
                        "scraped_at": datetime.now(timezone.utc).isoformat(),
                        "scraping_method": "yt-dlp + whisper",
                        "author": metadata["author"],
                        "video_id": video_id,
                    },
                }

            # Format content with video info and transcript
            formatted_content = f"""
**YouTube Video Analysis**

**Title:** {metadata['title']}
**Author:** {metadata['author']}
**Video URL:** {youtube_url}
**Duration:** {metadata['duration']} seconds

**Transcript:**
{transcript}
"""

            return {
                "url": youtube_url,
                "video_id": video_id,
                "title": metadata["title"],
                "content": formatted_content,
                "transcript_raw": transcript,
                "metadata": {
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                    "scraping_method": "yt-dlp + whisper",
                    "author": metadata["author"],
                    "video_id": video_id,
                    "duration": metadata["duration"],
                    "content_length": len(transcript),
                    "description": (
                        metadata["description"][:500] if metadata["description"] else ""
                    ),
                },
            }

        except Exception as e:
            print(f"[YOUTUBE] Error processing video: {e}")
            return None
        finally:
            # Cleanup temporary audio file
            if audio_file and os.path.exists(audio_file):
                try:
                    os.remove(audio_file)
                except:
                    pass


# Add this helper function for YouTube content summarization
def summarize_youtube_data_advanced(youtube_data):
    """
    Summarize YouTube video content similar to web scraping summarization
    """
    try:
        video_url = youtube_data.get("url", "N/A")
        title = youtube_data.get("title", "YouTube Video")
        transcript = youtube_data.get("transcript_raw", "")
        author = youtube_data.get("metadata", {}).get("author", "Unknown")

        # Check if transcript is substantial enough
        MIN_TRANSCRIPT_LENGTH = (
            20  # Further reduced to 20 characters to catch more content
        )
        if not transcript or len(transcript.strip()) < MIN_TRANSCRIPT_LENGTH:
            logger.warning(
                f"Transcript for {video_url} is too short to summarize ({len(transcript)} chars)."
            )

            # Check if this is due to YouTube IP blocking
            if youtube_data.get("error") == "youtube_ip_blocked":
                return f"""**YouTube Video Analysis**

**Title:** {title}
**Author/Channel:** {author}
**Video URL:** {video_url}

**Access Limitation Notice:**
This video could not be processed because YouTube is blocking requests from cloud server IPs (AWS, Google Cloud, Azure, etc.) to prevent automated access.

**Possible Solutions:**
- Use a proxy or VPN service
- Implement YouTube cookies authentication
- Access the video from a non-cloud IP address
- Use alternative video processing methods

**Video Information:**
While we cannot access the content directly, this appears to be a legitimate YouTube video. You may need to manually review the content or use alternative processing methods."""

            # Return a more detailed basic summary for very short content instead of failing
            if transcript and len(transcript.strip()) > 0:
                return f"""**YouTube Video Analysis**

**Title:** {title}
**Author/Channel:** {author}
**Video URL:** {video_url}

**Content Summary:**
This video contains limited speech content. The available transcript shows: "{transcript[:200]}{'...' if len(transcript) > 200 else ''}"

While the transcript is brief, this appears to be a short-form video or one with minimal spoken content. The video may focus more on visual elements, music, or brief commentary rather than extended dialogue."""
            else:
                return f"""**YouTube Video Analysis**

**Title:** {title}
**Author/Channel:** {author}
**Video URL:** {video_url}

**Content Summary:**
This video appears to contain no speech content or the audio could not be processed. This could be:
- A music video without lyrics
- A visual-only video (animations, montages, etc.)
- A video where speech recognition was unsuccessful
- Content that is primarily instrumental or ambient

The video may rely on visual storytelling, music, or non-verbal communication rather than spoken content."""

        # Load prompt template
        yaml_prompts = load_yaml_file(path=pathconfig.agent_template)
        summary_prompt_template = yaml_prompts.get("youtube_summary_prompt_template")

        if not summary_prompt_template:
            # Fallback to web scraping template if YouTube-specific doesn't exist
            summary_prompt_template = yaml_prompts.get("scrape_summary_prompt_template")
            if not summary_prompt_template:
                logger.error("No summary prompt template found in YAML file.")
                return None

        # Create the prompt
        full_prompt = f"""
Please analyze and summarize this YouTube video content:

**Video Title:** {title}
**Author/Channel:** {author}
**Video URL:** {video_url}

**Video Transcript:**
{transcript}

Please provide a comprehensive summary that captures:
1. Main topics and key points discussed
2. Important insights or conclusions
3. Any actionable information or recommendations
4. Overall theme and purpose of the video

Format the summary to be informative and well-structured.
"""

        # Get AI response
        ai_response = get_fireworks_response(full_prompt, role="system")

        if ai_response and isinstance(ai_response, str) and ai_response.strip():
            return ai_response.strip()
        else:
            logger.error(f"AI failed to generate summary for YouTube video {video_url}")
            return None

    except Exception as e:
        logger.error(f"Error during YouTube summarization: {e}")
        traceback.print_exc()
        return None


def evaluate_youtube_content(clarification_prompt, youtube_data, summary_text):
    """
    Evaluate YouTube content to extract clarifications
    """
    try:
        # Combine video content for evaluation
        full_content = f"""
        Video URL: {youtube_data.get('url', '')}
        Video Title: {youtube_data.get('title', '')}
        Author/Channel: {youtube_data.get('metadata', {}).get('author', 'Unknown')}
        Summary: {summary_text}
        Transcript Preview: {youtube_data.get('transcript_raw', '')[:2000]}...
        """

        # Replace placeholder in prompt (assuming you have a YouTube-specific prompt)
        filled_prompt = clarification_prompt.replace(
            "{{youtube_content}}", full_content
        )

        # Get AI response
        ai_response = get_evaluator_fireworks(filled_prompt, "system")

        # Parse response
        try:
            result = json.loads(ai_response)
        except json.JSONDecodeError:
            json_text = re.search(r"\{.*\}", ai_response, re.DOTALL)
            result = json.loads(json_text.group(0)) if json_text else {}

        return {
            "summary": summary_text,
            "clarifications": result.get("clarifications", []),
            "clean_content": summary_text,
        }

    except Exception as e:
        logger.error(f"Error evaluating YouTube content: {e}")
        return None


def clarific_youtube(user_id, val, video_url, title):
    """
    Process clarifications from YouTube content
    """
    clarification_responses = []
    failed_key = f"{user_id}/yaml/failed_ques.yaml"

    failed_ques = flatten_list(load_yaml_from_s3(failed_key) or [])
    failed_data = failed_ques

    # Check for existing YouTube clarifications to prevent duplicates
    existing_questions = set()
    for existing_item in failed_data:
        if (
            existing_item.get("is_youtube")
            and existing_item.get("filename") == video_url
        ):
            existing_questions.add(existing_item.get("User", "").strip().lower())

    quote_summary = val["summary"] if "summary" in val else title

    # Process new clarifications
    for actual_q in val.get("clarifications", []):
        actual_q = actual_q.strip()
        if not actual_q or actual_q.lower() in existing_questions:
            continue

        entry_obj = {
            "User": actual_q,
            "Rephrased Question": actual_q,
            "Ai Response": "",
            "quote": quote_summary,
            "filename": video_url,
            "doc_value": 0,
            "is_youtube": True,
            "youtube_url": video_url,
            "youtube_title": title,
        }
        clarification_responses.append(entry_obj)
        existing_questions.add(actual_q.lower())

    # Merge and save
    updated_data = failed_data + clarification_responses
    save_yaml_to_s3(data=updated_data, user_id=user_id, filename="failed_ques.yaml")

    return clarification_responses


# Add the main YouTube scraping endpoint
@agent_bps.route("/scrape-youtube", methods=["POST"])
def scrape_youtube_route():
    """
    Scrape YouTube video, get transcript, summarize, and extract clarifications
    """
    try:
        data = request.get_json()
        api_key = data.get("api_key")
        youtube_url = data.get("url")

        if not api_key or not youtube_url:
            return jsonify({"error": "api_key and url are required"}), 400

        user_id = fetch_userid_from_launch(api_key)
        if not user_id:
            return jsonify({"error": "Invalid API Key"}), 401

        # Check for duplicates
        youtube_metadata_path = f"{user_id}/yaml/scraped_youtube.yaml"
        existing_videos = load_yaml_from_s3(youtube_metadata_path) or []

        for video in existing_videos:
            if video.get("status") == "active" and video.get("url") == youtube_url:
                return (
                    jsonify(
                        {
                            "error": "Duplicate video found",
                            "message": f"YouTube video '{youtube_url}' has already been processed.",
                            "existing_entry": video,
                        }
                    ),
                    409,
                )

        # Step 1: Scrape YouTube video
        youtube_scraper = YouTubeScrapingClient(user_id=user_id)
        scraped_data = youtube_scraper.scrape_youtube_video(youtube_url)

        if not scraped_data:
            return (
                jsonify({"error": "Failed to access YouTube video or get transcript"}),
                500,
            )

        if scraped_data.get("error") == "transcript_unavailable":
            return (
                jsonify(
                    {
                        "error": "Transcript not available",
                        "message": "This YouTube video doesn't have captions/transcript available",
                    }
                ),
                422,
            )

        # Step 2: Summarize
        summary_text = summarize_youtube_data_advanced(scraped_data)

        if summary_text == "UNSUITABLE_CONTENT":
            return (
                jsonify(
                    {
                        "error": "Video content could not be analyzed",
                        "details": "The transcript was too short or not suitable for summarization",
                    }
                ),
                422,
            )

        if not summary_text:
            return jsonify({"error": "Failed to generate video summary"}), 500

        # Step 3: Extract clarifications
        prompts = load_yaml_file(path=pathconfig.agent_template)
        # Use existing clarification prompt or create YouTube-specific one
        clarification_prompt = prompts.get(
            "extract_youtube_clarifications_prompt"
        ) or prompts.get("extract_scraping_clarifications_prompt")

        val = evaluate_youtube_content(clarification_prompt, scraped_data, summary_text)
        if not val:
            return (
                jsonify(
                    {"error": "Failed to evaluate video content for clarifications"}
                ),
                500,
            )

        # Step 4: Process clarifications
        if val["clarifications"]:
            clarific_youtube(
                user_id, val, youtube_url, scraped_data.get("title", "No Title")
            )

        # Step 5: Create embedding and save to LanceDB
        embedding_client = YouTubeScrapingClient(user_id=user_id)
        embedding_vector = embedding_client.embeddings.embed_query(summary_text)

        timestamp = datetime.now(timezone.utc).isoformat()
        lancedb_payload = {
            "user_id": user_id,
            "url": youtube_url,
            "title": scraped_data.get("title", "YouTube Video"),
            "content": summary_text,
            "timestamp": timestamp,
            "metadata": scraped_data.get("metadata", {}),
            "embedding": embedding_vector,
        }

        # Step 6: Save to LanceDB
        lancedb_server_url = os.getenv("LANCE_DB_IP")
        if not lancedb_server_url:
            return jsonify({"error": "LANCE_DB_IP environment variable not set"}), 500

        try:
            response = requests.post(
                f"{lancedb_server_url}/insert_scraped_data",
                json=lancedb_payload,
                timeout=30,
            )
            if response.status_code != 200:
                raise Exception(f"LanceDB returned status {response.status_code}")
        except Exception as e:
            logger.error(f"LanceDB Error: {e}")
            return jsonify({"error": f"Vector database error: {str(e)}"}), 500

        # Step 7: Save YouTube metadata
        video_entry = {
            "url": youtube_url,
            "video_id": scraped_data.get("video_id"),
            "title": scraped_data.get("title", "YouTube Video"),
            "author": scraped_data.get("metadata", {}).get("author", "Unknown"),
            "summary": summary_text,
            "timestamp": timestamp,
            "clarifications_count": len(val.get("clarifications", [])),
            "status": "active",
        }

        existing_videos.append(video_entry)
        save_yaml_to_s3(existing_videos, user_id, "scraped_youtube.yaml")

        # Step 8: Validate clarifications if any
        if val.get("clarifications"):
            validate_youtube_clarifications(user_id)

        return (
            jsonify(
                {
                    "status": "success",
                    "summary": summary_text,
                    "url": youtube_url,
                    "title": scraped_data.get("title"),
                    "author": scraped_data.get("metadata", {}).get("author"),
                    "timestamp": timestamp,
                    "clarifications_found": len(val.get("clarifications", [])),
                }
            ),
            200,
        )

    except Exception as e:
        logger.error(f"Error in YouTube scraping route: {e}")
        traceback.print_exc()
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


def validate_youtube_clarifications(user_id):
    """
    Validate clarifications from YouTube videos
    """
    try:
        prompts = load_yaml_file(path=pathconfig.agent_template)

        passes_key = f"{user_id}/yaml/passed_ques.yaml"
        failed_key = f"{user_id}/yaml/failed_ques.yaml"

        passed_data = flatten_list(load_yaml_from_s3(passes_key) or [])
        failed_data = flatten_list(load_yaml_from_s3(failed_key) or [])

        # Filter YouTube clarifications
        youtube_clarifications = [
            item
            for item in failed_data
            if item.get("is_youtube") and not item.get("Ai Response")
        ]

        if not youtube_clarifications:
            logger.info("No YouTube clarifications to validate")
            return

        # Get answers for clarifications using existing function
        content = fetch_youtube_ques_with_docs(youtube_clarifications, user_id)

        # Process similar to scraping validation
        batch_size = 10
        valid_responses, updated_clarification_responses = [], []

        for i in range(0, len(content), batch_size):
            batch = content[i : i + batch_size]
            res_raw = evaluator_batch_llama_youtube(
                prompts.get(
                    "youtube_response_validator_batch",
                    prompts.get("scraping_response_validator_batch"),
                ),
                batch,
            )

            # Parse and process results (similar to scraping validation)
            try:
                match = re.search(r"\[\s*\{.*?\}\s*\]", res_raw, re.DOTALL)
                if match:
                    json_str = match.group(0).replace("{{", "{").replace("}}", "}")
                    res_json = json.loads(json_str)
                else:
                    res_json = json.loads(res_raw)
            except:
                try:
                    clean_response = res_raw.replace("{{", "{").replace("}}", "}")
                    match = re.search(r"\[\s*\{.*?\}\s*\]", clean_response, re.DOTALL)
                    res_json = yaml.safe_load(match.group(0)) if match else []
                except:
                    res_json = []

            # Process results
            for original_item, eval_result in zip(batch, res_json):
                actual_q = original_item["query"]
                related_res = eval_result.get("related", False)
                usecase_res = eval_result.get("has_usecase_details", False)
                filename = original_item.get("filename", "").strip()

                # Find original entry
                original_entry = None
                for item in youtube_clarifications:
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
                    "is_youtube": True,
                    "youtube_url": original_entry.get("youtube_url", ""),
                    "youtube_title": original_entry.get("youtube_title", ""),
                }

                if related_res and usecase_res:
                    entry_obj["date_processed"] = datetime.now().isoformat(
                        timespec="seconds"
                    )
                    valid_responses.append(entry_obj)
                else:
                    updated_clarification_responses.append(entry_obj)

        # Update files
        npassed_data = append_passed_with_ai_diff(passed_data, valid_responses)

        answered_keys = {(v.get("User"), v.get("filename")) for v in valid_responses}
        failed_data = [
            e
            for e in failed_data
            if not (
                e.get("is_youtube")
                and (e.get("User"), e.get("filename")) in answered_keys
            )
        ]

        # Update failed questions with new responses
        for updated_item in updated_clarification_responses:
            for i, item in enumerate(failed_data):
                if (
                    item.get("User") == updated_item.get("User")
                    and item.get("filename") == updated_item.get("filename")
                    and item.get("is_youtube")
                ):
                    failed_data[i] = updated_item
                    break

        # Save
        if npassed_data:
            save_yaml_to_s3(npassed_data, user_id, "passed_ques.yaml")
        if failed_data:
            save_yaml_to_s3(failed_data, user_id, "failed_ques.yaml")

        logger.info(f"✅ Validated YouTube clarifications for user {user_id}")

    except Exception as e:
        logger.error(f"Error validating YouTube clarifications: {e}")
        traceback.print_exc()


def fetch_youtube_ques_with_docs(clarification_list, user_id):
    """
    Fetch answers for YouTube clarifications using LanceDB
    """
    content = []

    for item in clarification_list:
        question_text = item.get("User", "").strip()
        filename = item.get("filename", "")  # YouTube URL

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


def evaluator_batch_llama_youtube(prompt_template_str, qa_list):
    """
    Evaluate YouTube-based questions and answers using LLaMA
    """
    qa_input_block = "\n".join(
        [
            f"{i+1}.\nUser Question: {item['query']}\nAI Response: {item['response_text']}"
            for i, item in enumerate(qa_list)
        ]
    )

    full_prompt = prompt_template_str.replace("{qa_list}", qa_input_block)

    try:
        llama_response = get_fireworks_response(full_prompt, role="user")
        return llama_response
    except Exception as e:
        print(f"🔥 LLaMA Evaluator batch Error for YouTube: {e}")
        return []


# Add route to get YouTube summaries
@agent_bps.route("/get-youtube-summaries", methods=["GET"])
def get_youtube_summaries():
    """Get all YouTube video summaries for a user"""
    try:
        api_key = request.args.get("api_key")
        if not api_key:
            return jsonify({"error": "api_key is required"}), 400

        user_id = fetch_userid_from_launch(api_key)
        if not user_id:
            return jsonify({"error": "Invalid API Key"}), 401

        youtube_metadata_path = f"{user_id}/yaml/scraped_youtube.yaml"
        videos_data = load_yaml_from_s3(youtube_metadata_path)

        if videos_data is None:
            return jsonify([]), 200

        active_videos = [v for v in videos_data if v.get("status") == "active"]
        return jsonify(active_videos), 200

    except Exception as e:
        logger.error(f"Error fetching YouTube summaries: {e}")
        return jsonify({"error": str(e)}), 500


# Add route to delete YouTube summary
@agent_bps.route("/delete-youtube-summary", methods=["DELETE"])
def delete_youtube_summary():
    """Delete a YouTube video summary and related clarifications"""
    try:
        data = request.get_json()
        api_key = data.get("api_key")
        url_to_delete = data.get("url")

        if not api_key or not url_to_delete:
            return jsonify({"error": "api_key and url are required"}), 400

        user_id = fetch_userid_from_launch(api_key)
        if not user_id:
            return jsonify({"error": "Invalid API Key"}), 401

        # Delete from LanceDB
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

        # Update metadata
        youtube_metadata_path = f"{user_id}/yaml/scraped_youtube.yaml"
        videos_data = load_yaml_from_s3(youtube_metadata_path) or []

        updated_videos = []
        for video in videos_data:
            if video.get("url") == url_to_delete:
                video["status"] = "deleted"
                video["deleted_at"] = datetime.now().isoformat()
            updated_videos.append(video)

        save_yaml_to_s3(updated_videos, user_id, "scraped_youtube.yaml")

        # Delete clarifications
        success = deletefilebasedData(url_to_delete, user_id)
        if not success:
            logger.warning(
                f"Failed to delete YouTube clarification entries for user {user_id}"
            )

        return jsonify({"message": "YouTube video summary deleted successfully"}), 200

    except Exception as e:
        logger.error(f"Error deleting YouTube summary: {e}")
        return jsonify({"error": str(e)}), 500


class WebScrapingLanceClient:
    def __init__(self, user_id: str):
        load_dotenv()
        self.lancedb_url = os.getenv("LANCE_DB_IP")
        self.user_id = user_id
        self.dimension = 2880
        self.embeddings = get_firework_embedding()

    def _setup_selenium_driver(self):
        """Setup Chrome driver with appropriate options"""
        try:
            chrome_options = Options()
            chrome_options.add_argument("--headless")
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--window-size=1920,1080")
            chrome_options.add_argument(
                "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            )

            # Try different Chrome/Chromium paths
            chrome_paths = [
                "/usr/bin/google-chrome",
                "/usr/bin/chromium-browser",
                "/usr/bin/chromium",
                "/snap/bin/chromium",
            ]

            for chrome_path in chrome_paths:
                if os.path.exists(chrome_path):
                    chrome_options.binary_location = chrome_path
                    print(f"[SELENIUM] Using Chrome binary: {chrome_path}")
                    break
            else:
                raise Exception(
                    "Chrome/Chromium binary not found. Please install Chrome or Chromium."
                )

            driver = webdriver.Chrome(options=chrome_options)
            print("[SELENIUM] Chrome driver initialized successfully")
            return driver

        except Exception as e:
            print(f"[SELENIUM] Chrome driver setup failed: {e}")
            raise

    def _extract_internal_links(self, soup, base_url, base_domain):
        """Extract internal links from the same domain"""
        links = []
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            absolute_url = urljoin(base_url, href)

            # Only include links from same domain, exclude fragments and queries
            if (
                urlparse(absolute_url).netloc == base_domain
                and not absolute_url.endswith(
                    (".pdf", ".jpg", ".png", ".gif", ".css", ".js")
                )
                and "#" not in absolute_url.split("/")[-1]
            ):
                links.append(absolute_url)

        return list(set(links))  # Remove duplicates

    def _compile_multilevel_content(self, level_content):
        """Compile content from all levels into comprehensive summary"""
        compiled = f"**Website Overview:**\nThis analysis covers {sum(len(pages) for pages in level_content.values())} pages across {len([k for k, v in level_content.items() if v])} levels.\n\n"

        for level, pages in level_content.items():
            if not pages:
                continue

            compiled += f"**Level {level} ({'Homepage' if level == 0 else f'Sub-pages Level {level}'}):**\n"

            for page in pages:
                compiled += f"- **{page['title']}** ({page['word_count']} words): {page['content'][:200]}...\n"

            compiled += "\n"

        return compiled

    def scrape_website(self, url: str, use_selenium=True, max_depth=2, max_pages=20):
        """Main scraping method - can use either Selenium or requests"""
        if use_selenium:
            return self.scrape_website_multilevel_enhanced(url, max_depth, max_pages)
        else:
            return self._scrape_single_page_requests_enhanced(url)

    def _scrape_single_page_requests(self, url: str):
        """Robust single-page scraping method using requests"""
        try:
            print(f"[REQUESTS] Attempting to scrape: {url}")
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Accept-Encoding": "gzip, deflate",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            }

            # Make request with longer timeout
            response = requests.get(
                url, headers=headers, timeout=15, allow_redirects=True
            )
            print(f"[REQUESTS] Response status: {response.status_code}")
            response.raise_for_status()

            soup = BeautifulSoup(response.content, "html.parser")
            title = soup.find("title")
            title_text = title.get_text().strip() if title else "Scraped Website"
            content = self._extract_content_with_structure(soup)

            print(
                f"[REQUESTS] Successfully scraped: {title_text} ({len(content)} chars)"
            )

            return {
                "url": url,
                "title": title_text,
                "content": content,
                "metadata": {
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                    "scraping_method": "requests_single_page",
                    "content_length": len(content),
                    "status_code": response.status_code,
                },
            }
        except Exception as e:
            print(f"[REQUESTS] Error scraping {url}: {e}")
            import traceback

            traceback.print_exc()
            return None

    def _extract_content_with_structure(self, soup, url=""):
        """Enhanced content extraction that preserves headings and structure"""

        # Remove unwanted elements
        for element in soup(["script", "style", "nav", "header", "footer", "aside"]):
            element.decompose()

        content_data = {
            "headings": [],
            "main_content": "",
            "meta_info": {},
            "structured_content": [],
        }

        # Extract meta information
        title_tag = soup.find("title")
        content_data["meta_info"]["title"] = (
            title_tag.get_text().strip() if title_tag else ""
        )

        meta_desc = soup.find("meta", attrs={"name": "description"})
        content_data["meta_info"]["description"] = (
            meta_desc.get("content", "") if meta_desc else ""
        )

        # Extract all headings with hierarchy
        headings = soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
        for heading in headings:
            heading_text = heading.get_text().strip()
            if heading_text:
                content_data["headings"].append(
                    {
                        "level": int(heading.name[1]),  # h1 -> 1, h2 -> 2, etc.
                        "text": heading_text,
                        "tag": heading.name,
                    }
                )

        # Extract structured content sections
        main_content_areas = soup.find_all(
            ["main", "article", "section", "div"],
            class_=lambda x: x
            and any(
                term in x.lower()
                for term in ["content", "main", "article", "body", "text"]
            ),
        )

        if not main_content_areas:
            main_content_areas = [soup.find("body")] if soup.find("body") else [soup]

        for area in main_content_areas:
            if area:
                # Extract paragraphs and lists
                paragraphs = area.find_all(["p", "div", "li"])
                for para in paragraphs[:20]:  # Limit to avoid too much content
                    text = para.get_text().strip()
                    if text and len(text) > 20:  # Filter out short/empty content
                        content_data["structured_content"].append(
                            {
                                "type": para.name,
                                "text": text[:300] + "..." if len(text) > 300 else text,
                            }
                        )

        # Compile main content
        all_text = soup.get_text()
        lines = (line.strip() for line in all_text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        content_data["main_content"] = " ".join(chunk for chunk in chunks if chunk)

        return content_data

    def scrape_website_multilevel_enhanced(
        self, url: str, max_depth: int = 3, max_pages: int = 50
    ):
        """Enhanced multi-level scraping with detailed structure extraction"""
        driver = None
        try:
            # Try Selenium first, fallback to requests
            try:
                driver = self._setup_selenium_driver()
            except Exception as selenium_error:
                print(f"[SELENIUM] Failed: {selenium_error}")
                print("[FALLBACK] Using requests method")
                return self._scrape_single_page_requests_enhanced(url)

            scraped_data = {
                "url": url,
                "title": "",
                "content": "",
                "detailed_analysis": {
                    "total_pages": 0,
                    "levels": {},
                    "all_headings": [],
                    "site_structure": {},
                },
                "metadata": {
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                    "levels_scraped": {},
                    "total_pages": 0,
                    "scraping_method": "selenium_multilevel_enhanced",
                },
            }

            base_domain = urlparse(url).netloc
            visited = set()
            to_visit = deque([(url, 0)])
            pages_scraped = 0
            level_detailed_content = {i: [] for i in range(max_depth + 1)}

            while to_visit and pages_scraped < max_pages:
                current_url, depth = to_visit.popleft()

                if current_url in visited or depth > max_depth:
                    continue

                print(f"[ENHANCED] Scraping Level {depth}: {current_url}")

                try:
                    driver.get(current_url)
                    WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located((By.TAG_NAME, "body"))
                    )
                    time.sleep(2)

                    soup = BeautifulSoup(driver.page_source, "html.parser")

                    # Enhanced content extraction
                    content_data = self._extract_content_with_structure(
                        soup, current_url
                    )

                    page_analysis = {
                        "url": current_url,
                        "title": content_data["meta_info"]["title"],
                        "description": content_data["meta_info"]["description"],
                        "headings": content_data["headings"],
                        "main_content": content_data["main_content"][
                            :2000
                        ],  # Limit for storage
                        "structured_content": content_data["structured_content"][
                            :10
                        ],  # Top 10 sections
                        "word_count": len(content_data["main_content"].split()),
                        "heading_count": len(content_data["headings"]),
                        "content_type": self._classify_page_type(content_data),
                    }

                    level_detailed_content[depth].append(page_analysis)

                    # Collect all headings for site-wide analysis
                    for heading in content_data["headings"]:
                        heading["source_url"] = current_url
                        heading["level_depth"] = depth
                        scraped_data["detailed_analysis"]["all_headings"].append(
                            heading
                        )

                    # Extract links for next level
                    if depth < max_depth:
                        links = self._extract_internal_links(
                            soup, current_url, base_domain
                        )
                        for link in links[:8]:  # Limit links per page
                            if link not in visited:
                                to_visit.append((link, depth + 1))

                    visited.add(current_url)
                    pages_scraped += 1

                    print(
                        f"[ENHANCED] ✅ Analyzed: {page_analysis['title']} "
                        f"({page_analysis['word_count']} words, {page_analysis['heading_count']} headings)"
                    )

                except Exception as e:
                    print(f"[ENHANCED] ❌ Error analyzing {current_url}: {e}")
                    continue

            # Compile enhanced final content
            scraped_data["title"] = (
                level_detailed_content[0][0]["title"]
                if level_detailed_content[0]
                else "Website"
            )
            scraped_data["content"] = self._compile_enhanced_multilevel_content(
                level_detailed_content
            )

            # Enhanced metadata
            scraped_data["detailed_analysis"]["total_pages"] = pages_scraped
            scraped_data["detailed_analysis"]["levels"] = level_detailed_content
            scraped_data["detailed_analysis"]["site_structure"] = (
                self._analyze_site_structure(level_detailed_content)
            )

            scraped_data["metadata"]["levels_scraped"] = {
                f"level_{i}": len(pages)
                for i, pages in level_detailed_content.items()
                if pages
            }
            scraped_data["metadata"]["total_pages"] = pages_scraped

            return scraped_data

        except Exception as e:
            print(f"[ENHANCED] Fatal error: {e}")
            return None

        finally:
            if driver:
                driver.quit()

    def _classify_page_type(self, content_data):
        """Classify page type based on content and headings"""
        title = content_data["meta_info"]["title"].lower()
        headings_text = " ".join([h["text"].lower() for h in content_data["headings"]])

        if any(word in title for word in ["home", "welcome", "index"]):
            return "homepage"
        elif any(word in title for word in ["about", "company", "team"]):
            return "about_page"
        elif any(word in title for word in ["contact", "reach", "support"]):
            return "contact_page"
        elif any(
            word in headings_text for word in ["product", "service", "buy", "price"]
        ):
            return "product_page"
        elif any(word in headings_text for word in ["blog", "news", "article"]):
            return "blog_page"
        else:
            return "information_page"

    def _analyze_site_structure(self, level_content):
        """Analyze overall site structure and patterns"""
        structure_analysis = {
            "navigation_depth": len([k for k, v in level_content.items() if v]),
            "page_types_distribution": {},
            "common_headings": {},
            "content_patterns": [],
        }

        # Analyze page type distribution
        all_page_types = []
        for level, pages in level_content.items():
            for page in pages:
                page_type = page.get("content_type", "unknown")
                all_page_types.append(page_type)

        from collections import Counter

        structure_analysis["page_types_distribution"] = dict(Counter(all_page_types))

        # Find common heading patterns
        all_headings = []
        for level, pages in level_content.items():
            for page in pages:
                for heading in page.get("headings", []):
                    all_headings.append(heading["text"].lower())

        heading_counter = Counter(all_headings)
        structure_analysis["common_headings"] = dict(heading_counter.most_common(10))

        return structure_analysis

    def _compile_enhanced_multilevel_content(self, level_content):
        """Compile enhanced content with detailed structure analysis"""

        total_pages = sum(len(pages) for pages in level_content.values())
        active_levels = len([k for k, v in level_content.items() if v])

        compiled = f"**Website Overview:**\n"
        compiled += f"This comprehensive analysis covers {total_pages} pages across {active_levels} levels of depth. "
        compiled += f"The website structure reveals detailed content organization with specific headings and page classifications.\n\n"

        for level, pages in level_content.items():
            if not pages:
                continue

            level_name = "Homepage" if level == 0 else f"Sub-pages Level {level}"
            compiled += f"**Level {level} ({level_name}) - {len(pages)} pages:**\n"

            for page in pages:
                compiled += f"- **{page['title']}** ({page['content_type'].replace('_', ' ').title()}):\n"

                # Add exact headings found
                if page.get("headings"):
                    compiled += f"  * Key Headings: "
                    headings_by_level = {}
                    for h in page["headings"][:8]:  # Limit to top 8 headings
                        level_key = f"H{h['level']}"
                        if level_key not in headings_by_level:
                            headings_by_level[level_key] = []
                        headings_by_level[level_key].append(h["text"])

                    heading_summary = []
                    for h_level in sorted(headings_by_level.keys()):
                        heading_summary.append(
                            f"{h_level}: {', '.join(headings_by_level[h_level][:3])}"
                        )
                    compiled += " | ".join(heading_summary) + "\n"

                # Add content summary
                compiled += f"  * Content: {page['main_content'][:200]}...\n"
                compiled += f"  * Stats: {page['word_count']} words, {page['heading_count']} headings\n\n"

        # Add site-wide insights
        compiled += f"**Site Structure Insights:**\n"
        compiled += f"The website demonstrates a hierarchical structure with clear content organization. "
        compiled += f"Navigation patterns show systematic information architecture with specific page types "
        compiled += f"serving distinct user needs. Content quality and depth vary by page type and level.\n\n"

        return compiled

    def _scrape_single_page_requests_enhanced(self, url: str):
        """Enhanced single-page scraping with structure extraction"""
        try:
            print(f"[REQUESTS ENHANCED] Scraping: {url}")
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }

            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()

            soup = BeautifulSoup(response.content, "html.parser")
            content_data = self._extract_content_with_structure(soup, url)

            return {
                "url": url,
                "title": content_data["meta_info"]["title"] or "Scraped Website",
                "content": f"**Single Page Analysis:**\n\n**Headings Found:**\n"
                + "\n".join(
                    [
                        f"- {h['tag'].upper()}: {h['text']}"
                        for h in content_data["headings"][:15]
                    ]
                )
                + f"\n\n**Main Content:**\n{content_data['main_content'][:2000]}...",
                "detailed_analysis": {
                    "total_pages": 1,
                    "levels": {
                        0: [
                            {
                                "url": url,
                                "title": content_data["meta_info"]["title"],
                                "headings": content_data["headings"],
                                "content_type": self._classify_page_type(content_data),
                                "word_count": len(content_data["main_content"].split()),
                            }
                        ]
                    },
                    "all_headings": content_data["headings"],
                },
                "metadata": {
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                    "scraping_method": "requests_enhanced_single_page",
                    "content_length": len(content_data["main_content"]),
                },
            }
        except Exception as e:
            print(f"[REQUESTS ENHANCED] Error: {e}")
            return None


@agent_bps.route("/scrape", methods=["POST"])
def scrape_website_route():
    """This function handles the web request, scrapes data, and saves it."""
    try:
        data = request.get_json()
        user_id = data.get("user_id")
        url_to_scrape = data.get("url")

        if not user_id or not url_to_scrape:
            return jsonify({"error": "user_id and url are required"}), 400

        # --- Step 1: Scrape the website (This part is correct) ---
        scraper = WebScrapingLanceClient(user_id=user_id)
        scraped_data = scraper.scrape_website(
            url=url_to_scrape, use_selenium=True, max_depth=3, max_pages=25
        )

        if not scraped_data:
            return jsonify({"error": "Failed to scrape the website content"}), 500

        # --- Step 2: NEW - Process the scraped text to get an embedding ---
        embedding_client = WebScrapingLanceClient(user_id=user_id)

        full_content = f"{scraped_data['title']}\n\n{scraped_data['content']}"
        embedding_vector = embedding_client.embeddings.embed_query(full_content)

        # --- Step 3: NEW - Prepare the payload for the LanceDB server ---
        lancedb_payload = {
            "user_id": user_id,
            "url": scraped_data["url"],
            "title": scraped_data["title"],
            "content": scraped_data["content"],
            "timestamp": scraped_data["metadata"]["scraped_at"],
            "metadata": scraped_data["metadata"],
            "embedding": embedding_vector,
        }

        # --- Step 4: NEW - Send the data to your LanceDB/FastAPI server ---
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
            return (
                jsonify(
                    {
                        "status": "success",
                        "message": "Website scraped and data saved successfully.",
                        "scraped_content": scraped_data,
                        "lancedb_response": response.json(),
                    }
                ),
                200,
            )
        else:
            return (
                jsonify(
                    {
                        "error": "Failed to save data to LanceDB server.",
                        "status_code": response.status_code,
                        "details": response.text,
                        # It's good practice to also return the data that failed to save
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

        # Step 1: Scrape
        scraper = WebScrapingLanceClient(user_id=user_id)
        scraped_data = scraper.scrape_website(
            url=url_to_scrape, use_selenium=True, max_depth=3, max_pages=25
        )
        if not scraped_data:
            return (
                jsonify({"error": "Failed to access or scrape website content."}),
                500,
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
        if not lancedb_server_url:
            return jsonify({"error": "LANCE_DB_IP environment variable not set"}), 500

        try:
            response = requests.post(
                f"{lancedb_server_url}/insert_scraped_data",
                json=lancedb_payload,
                timeout=30,  # Add timeout
            )

            if response.status_code != 200:
                logger.error(
                    f"LanceDB HTTP Error {response.status_code}: {response.text}"
                )
                raise Exception(f"LanceDB returned status {response.status_code}")

        except requests.exceptions.ConnectionError as e:
            logger.error(
                f"Cannot connect to LanceDB server at {lancedb_server_url}: {e}"
            )
            return (
                jsonify(
                    {
                        "error": "Vector database service unavailable",
                        "details": f"Cannot connect to {lancedb_server_url}",
                    }
                ),
                503,
            )
        except requests.exceptions.Timeout as e:
            logger.error(f"LanceDB request timeout: {e}")
            return jsonify({"error": "Vector database request timeout"}), 504
        except Exception as e:
            logger.error(f"LanceDB Error: {e}")
            return jsonify({"error": f"Vector database error: {str(e)}"}), 500
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
        ai_response = get_evaluator_fireworks(filled_prompt, "system")

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
