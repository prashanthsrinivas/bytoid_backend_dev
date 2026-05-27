import datetime
import json
import os
import re
import shutil
from agent_route.doc_clarity import (
    find_matching_industry,
    get_industry_names_from_yaml,
    preProcessDocWithUsecases,
)
from agent_route.lance_agent import LanceClient
from agent_route.task_manager import run_background_task
from cust_helpers import pathconfig
from db.rds_db import connect_to_rds
from utils.base_logger import get_logger
from utils.normal import load_yaml_file
from flask import jsonify
import requests
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
import pymysql
from utils.s3_utils import S3_BUCKET, load_yaml_from_s3, s3bucket, save_yaml_to_s3
from request_context import current_user_id
import asyncio
from credits_route.route import Credits
import threading


logger = get_logger(__name__)

from utils.key_rotation_manager import SecureKMSService as _QaKMSService
_qa_kms = _QaKMSService()


def _enc_qa(user_id, v):
    """Encrypt a Q&A text field."""
    if not v or not isinstance(v, str):
        return v
    enc = _qa_kms.encrypt(user_id, v)
    return {"ciphertext": enc["ciphertext"], "iv": enc["iv"], "encrypted_key": enc["encrypted_key"]}


def _dec_qa(user_id, v):
    """Decrypt a Q&A field; pass through plaintext unchanged."""
    if isinstance(v, dict) and "encrypted_key" in v:
        return _qa_kms.decrypt(user_id, v["encrypted_key"], v["iv"], v["ciphertext"])
    return v


def _decrypt_qa_entries(user_id, entries):
    """Decrypt User and Ai Response in Q&A entries. Returns (entries, was_migrated)."""
    if not entries:
        return entries, False
    was_migrated = False
    for e in entries:
        raw_user = e.get("User")
        raw_ai = e.get("Ai Response")
        if isinstance(raw_user, str) and raw_user:
            was_migrated = True
        if isinstance(raw_ai, str) and raw_ai:
            was_migrated = True
        if raw_user is not None:
            e["User"] = _dec_qa(user_id, raw_user)
        if raw_ai is not None:
            e["Ai Response"] = _dec_qa(user_id, raw_ai)
    return entries, was_migrated


def _load_and_decrypt_qa(user_id, yaml_filename):
    """Load a Q&A YAML file from S3 and decrypt User/Ai Response fields. Lazily migrates."""
    path = f"{user_id}/yaml/{yaml_filename}"
    entries = load_yaml_from_s3(path) or []
    if not entries:
        return entries
    entries, was_migrated = _decrypt_qa_entries(user_id, entries)
    if was_migrated:
        try:
            import copy
            save_yaml_to_s3(_encrypt_qa_entries(user_id, copy.deepcopy(entries)), user_id, yaml_filename)
        except Exception:
            pass
    return entries


def _encrypt_qa_entries(user_id, entries):
    """Encrypt User and Ai Response fields in Q&A entries (mutates in place)."""
    for e in entries:
        if e.get("User") and isinstance(e["User"], str):
            e["User"] = _enc_qa(user_id, e["User"])
        if e.get("Ai Response") and isinstance(e["Ai Response"], str):
            e["Ai Response"] = _enc_qa(user_id, e["Ai Response"])
    return entries


# def get_usecases_for_smb(smb_name, data):
#     for entry in data:
#         if entry.get("SMB") == smb_name:
#             return entry.get("Usecases", [])
#     return []


def get_usecases_for_smb(industry: str, data: list) -> list:
    """
    industry examples:
    - 'Undergraduate Student'
    - 'Law Firm'
    - 'Healthcare Services'
    """

    if not industry or not isinstance(data, list):
        return []

    industry = industry.strip().lower()

    for entry in data:
        if not isinstance(entry, dict):
            continue

        for key, value in entry.items():
            # Skip the Usecases key
            if key.lower() == "usecases":
                continue

            # value is the persona name
            if isinstance(value, str) and value.strip().lower() == industry:
                return entry.get("Usecases", [])

    return []


def remove_https_prefix(url):
    """
    It removes the 'https://' or 'http://' prefix and 'www.' from the URL,
    and also removes any trailing slash at the end of the URL.
    """
    result = re.sub(r"^(https?://)?(www\.)?|/$", "", url)
    return result


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


def deletefilebasedData(filename, userid):
    """
    Delete all Q&A entries for a given file/URL from the user's YAML files in S3.
    Works on both passed_ques.yaml and failed_ques.yaml.
    Handles both regular files and scraped website URLs.
    """
    try:
        s3 = s3bucket()
        target_name = filename.strip()

        for ques_file in ["passed_ques.yaml", "failed_ques.yaml"]:
            s3_key = f"{userid}/yaml/{ques_file}"
            ques_data = load_yaml_from_s3(s3_key) or []

            # Flatten in case there are nested lists
            flat_data = []
            for item in ques_data:
                if isinstance(item, list):
                    flat_data.extend(item)
                else:
                    flat_data.append(item)

            # Filter out entries for the given filename/URL
            filtered_data = []
            for q in flat_data:
                if isinstance(q, dict):
                    file_value = (q.get("filename") or "").strip()

                    # Handle different types of entries
                    should_keep = True

                    if q.get("is_scraping"):
                        # For scraped websites, do exact URL match
                        if file_value == target_name:
                            should_keep = False
                    else:
                        # For regular files, compare without extensions
                        file_base = os.path.splitext(file_value)[0].lower()
                        target_base = os.path.splitext(target_name)[0].lower()
                        if file_base == target_base:
                            should_keep = False

                    if should_keep:
                        filtered_data.append(q)
                else:
                    filtered_data.append(q)  # keep unexpected data untouched

            if filtered_data:
                # Save filtered list back to S3
                save_yaml_to_s3(filtered_data, userid, ques_file)
            else:
                # If empty → delete object from S3
                try:
                    s3.delete_object(Bucket=S3_BUCKET, Key=s3_key)
                    logger.info(f"Deleted empty file {s3_key} from S3")
                except Exception as e:
                    logger.warning(f"Could not delete {s3_key} from S3: {e}")

        return True

    except Exception as e:
        logger.error(
            f"Error deleting question entries for user {userid}, file {filename}: {e}",
            exc_info=True,
        )
        return False


def normalize_url_for_comparison(url):
    """Normalize URL for consistent comparison"""
    if not url:
        return ""

    # Remove trailing slashes and convert to lowercase
    normalized = url.rstrip("/").lower()

    # Remove common protocol variations
    if normalized.startswith("https://"):
        normalized = normalized[8:]
    elif normalized.startswith("http://"):
        normalized = normalized[7:]

    return normalized


async def process_and_update_yaml(
    all_downloaded_paths, userid, provider, db, folderpath, credits=None, emit=None
):
    """
    Process files, delete processed ones, and store/update metadata in a provider-based YAML structure.

    :param all_downloaded_paths: list of downloaded file paths
    :param userid: ID of the user
    :param provider: Provider name (e.g., "google", "zoho")
    :param folderpath: Temporary folder path containing files
    :param credits: optional Credits instance
    :param emit: optional async callable(message: str, progress: int) — caller provides this,
                 typically wrapping msg_builder.job_progress via the websocket send helper
    """
    processed_filenames = []
    connection = db or connect_to_rds()
    if not credits:
        credits = Credits(connection)
    industry = None
    selected_id = None
    with connection.cursor(pymysql.cursors.DictCursor) as cursor:
        cursor.execute(
            "select permissions,user_type from users where user_id = %s", (userid,)
        )
        base_user = cursor.fetchone()
        if not base_user:
            return jsonify({"error": "User not found"}), 404
        base_user_type = base_user["user_type"]
        if base_user_type == "user":
            base_permission = base_user["permissions"]
            if isinstance(base_permission, str):
                base_permission = json.loads(base_permission)
            # print("base data permissions", base_permission)
            if base_permission and "invited_by" in base_permission:
                email = base_permission["invited_by"]
                cursor.execute("select user_id from users where email = %s", (email,))
                row = cursor.fetchone()
                if row:
                    selected_id = row["user_id"]
                # print("attached id", selected_id)
        else:
            selected_id = userid
        # print("admin selected", selected_id)
        # print("fetched", selected_id)
        cursor.execute(
            "SELECT LineOfBusiness FROM business_info WHERE user_id_fk = %s ",
            (selected_id,),
        )
        user_row = cursor.fetchone()
        if not user_row:
            return jsonify({"error": "No line of business present"}), 401
        industry = user_row["LineOfBusiness"]
    if not db:
        connection.close()

    total = len(all_downloaded_paths)
    for i, path in enumerate(all_downloaded_paths):
        filename = os.path.basename(path)
        try:
            if emit:
                pct = 20 + int(i / max(total, 1) * 60)
                await emit(f"Processing file {i + 1}/{total}: {filename}", pct)

            lance_client = LanceClient(user_id=userid, credits=credits)
            result = await lance_client.process_document(
                file_path=path, filename=filename, credits=credits
            )

            if result.get("error") == "INSUFFICIENT_CREDITS":
                return {
                    "status": "error",
                    "error": "INSUFFICIENT_CREDITS",
                    "message": "Credits exhausted. Please recharge to continue.",
                }

            if result.get("vectors_made", 0) > 0:
                current_date = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                processed_filenames.append(
                    {
                        "filename": filename,
                        "FileStatus": "Present",
                        "upload_date": current_date,
                        "updated_date": None,
                    }
                )
        finally:
            os.remove(path)
            logger.info(f"[🗑] Deleted processed file: {path}")

    if not processed_filenames:
        return {"message": "nothing to merge"}

    if emit:
        await emit("Updating file metadata...", 85)

    # Remove the folder after processing
    if os.path.isdir(folderpath):
        shutil.rmtree(folderpath)

    # Ensure user directory exists
    yaml_path = f"{userid}/yaml/users_fileData.yaml"

    # print(f"yaml_path : {yaml_path}")

    # Load existing YAML or initialize structure
    # # if os.path.exists(yaml_path):
    #     existing_data = load_yaml_from_s3(yaml_path) or {}
    # else:
    #     existing_data = {}

    existing_data = load_yaml_from_s3(yaml_path)
    if existing_data is None:
        existing_data = {}

    # print("------------------------")
    # print(f"yaml_path: {yaml_path}")
    # print(f"existing_data: {existing_data}")
    # print("------------------------")

    if provider not in existing_data:
        existing_data[provider] = []

    provider_files = existing_data[provider]

    # Merge processed filenames into provider section
    for item in processed_filenames:
        fname = item["filename"]
        file_status = item["FileStatus"]

        # If a deleted entry exists for this filename, remove it
        provider_files[:] = [
            entry
            for entry in provider_files
            if not (entry["filename"] == fname and entry["FileStatus"] == "Deleted")
        ]

        existing_non_deleted = next(
            (
                entry
                for entry in provider_files
                if entry["filename"] == fname and entry["FileStatus"] != "Deleted"
            ),
            None,
        )

        if existing_non_deleted:
            existing_non_deleted["updated_date"] = item["upload_date"]
            existing_non_deleted["FileStatus"] = file_status
        else:
            provider_files.append(item)

    # Write back to YAML
    # with open(yaml_path, "w") as f:
    #     yaml.safe_dump(existing_data, f, sort_keys=False)
    # print("------------------------")
    # print(f"processed_filenames:{processed_filenames}")
    # print("------------------------")

    save_yaml_to_s3(existing_data, userid, "users_fileData.yaml")
    logger.info(
        f"[✅] Updated YAML for provider '{provider}' with {len(processed_filenames)} files."
    )
    if emit:
        await emit(f"Saved metadata for {len(processed_filenames)} file(s)", 92)
    indusries = get_industry_names_from_yaml(f"{pathconfig.basepath}/smb_usecases.yaml")
    matched_industry = find_matching_industry(industry, indusries)
    if matched_industry:
        new_or_updated_files = [item["filename"] for item in processed_filenames]

        def run_background_task():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(
                    preProcessDocWithUsecases(
                        userid=userid,
                        industry=matched_industry,
                        filenames=new_or_updated_files,
                        credits=credits,
                    )
                )
            finally:
                loop.close()

        threading.Thread(target=run_background_task, daemon=True).start()

        logger.info("[DEBUG] Background task queued")
    all_file_data = load_yaml_from_s3(yaml_path) or {}
    return all_file_data


# print(
#     "values ", get_industry_names_from_yaml(f"{pathconfig.basepath}/smb_usecases.yaml")
# )


def scrape_links(base_url, max_pages=50):
    visited = set()
    to_visit = [base_url]
    all_links = []

    while to_visit and len(visited) < max_pages:
        url = to_visit.pop(0)
        if url in visited:
            continue
        visited.add(url)

        try:
            resp = requests.get(url, timeout=10)
            if "text/html" not in resp.headers.get("Content-Type", ""):
                continue
            soup = BeautifulSoup(resp.text, "html.parser")

            for link in soup.find_all("a", href=True):
                full_url = urljoin(url, link["href"])
                # Only follow same domain
                if urlparse(full_url).netloc == urlparse(base_url).netloc:
                    if full_url not in visited and full_url not in to_visit:
                        to_visit.append(full_url)
                    all_links.append(full_url)

        except Exception as e:
            print(f"Error scraping {url}: {e}")

    return list(set(all_links))  # deduplicate
