import datetime
import os
import re
import shutil
import yaml
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
from utils.normal import ensure_dir, load_yaml_file
from flask import jsonify
import requests
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup

logger = get_logger(__name__)


def get_usecases_for_smb(smb_name, data):
    for entry in data:
        if entry.get("SMB") == smb_name:
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


def log_removal(before_list, after_list):
    """Logs how many items were removed."""
    removed = len(before_list) - len(after_list)
    if removed > 0:
        print(f"✅ Removed {removed} matching entries from failed_entries")
    else:
        print("⚠️ No matching entries removed from failed_entries")


def deletefilebasedData(filename, userid):
    try:
        main_folder = f"{pathconfig.basepath}/{userid}"
        os.makedirs(main_folder, exist_ok=True)

        for ques_file in ["passed_ques.yaml", "failed_ques.yaml"]:
            ques_path = os.path.join(main_folder, ques_file)
            if os.path.exists(ques_path):
                ques_data = load_yaml_file(ques_path)

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
        logger.error(
            f"Error deleting question entries for user {userid}, file {filename}: {e}",
            exc_info=True,
        )
        return False


async def process_and_update_yaml(all_downloaded_paths, userid, provider, folderpath):
    """
    Process files, delete processed ones, and store/update metadata in a provider-based YAML structure.

    :param all_downloaded_paths: list of downloaded file paths
    :param userid: ID of the user
    :param provider: Provider name (e.g., "google", "zoho")
    :param folderpath: Temporary folder path containing files
    :param pathconfig: Config object containing basepath
    """

    processed_filenames = []
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
    for path in all_downloaded_paths:
        filename = os.path.basename(path)
        lance_client = LanceClient(user_id=userid)
        result = await lance_client.process_document(file_path=path, filename=filename)

        if result.get("vectors_made", 0) > 0:
            current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            processed_filenames.append(
                {
                    "filename": filename,
                    "FileStatus": "Present",
                    "upload_date": current_date,
                    "updated_date": None,
                }
            )
            os.remove(path)
            logger.info(f"[🗑] Deleted processed file: {path}")

    if not processed_filenames:
        return  # Nothing to merge

    # Remove the folder after processing
    if os.path.isdir(folderpath):
        shutil.rmtree(folderpath)

    # Ensure user directory exists
    ensure_dir(f"{pathconfig.basepath}/{userid}")
    yaml_path = os.path.join(f"{pathconfig.basepath}/{userid}", "users_fileData.yaml")

    # Load existing YAML or initialize structure
    if os.path.exists(yaml_path):
        existing_data = load_yaml_file(yaml_path) or {}
    else:
        existing_data = {}

    if provider not in existing_data:
        existing_data[provider] = []

    provider_files = existing_data[provider]

    # Merge processed filenames into provider section
    for item in processed_filenames:
        fname = item["filename"]
        file_status = item["FileStatus"]

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
    with open(yaml_path, "w") as f:
        yaml.safe_dump(existing_data, f, sort_keys=False)

    logger.info(
        f"[✅] Updated YAML for provider '{provider}' with {len(processed_filenames)} files."
    )
    indusries = get_industry_names_from_yaml(f"{pathconfig.basepath}/smb_usecases.yaml")
    matched_industry = find_matching_industry(industry, indusries)
    if matched_industry:
        new_or_updated_files = [item["filename"] for item in processed_filenames]
        # need to make this queue process
        result = run_background_task(
            userid=userid,
            industry=matched_industry,
            filenames=new_or_updated_files,
            func=preProcessDocWithUsecases,
        )
        print(f"[DEBUG] Background task queued: {result}")
    yaml_path = os.path.join(f"{pathconfig.basepath}/{userid}", "users_fileData.yaml")
    all_file_data = load_yaml_file(yaml_path) or {}
    return all_file_data


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
