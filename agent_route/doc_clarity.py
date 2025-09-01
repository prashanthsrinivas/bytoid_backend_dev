import json
from agent_route.utils import extract_filename
from langchain_openai import OpenAIEmbeddings
import yaml
from utils.base_logger import get_logger
from utils.normal import load_yaml_file
from typing import List
from pydantic import BaseModel
import os
import difflib
import re
from cust_helpers import pathconfig
from utils.fireworkzz import evaluator_batch_llama
import requests
from utils.chatopenzz import (
    generate_usecases_questions,
    generate_usecases_questions_batch,
)
from datetime import datetime

from utils.s3_utils import (
    load_yaml_from_s3,
    read_json_from_s3,
    save_yaml_to_s3,
    upload_any_file,
)

logger = get_logger(__name__)
from dotenv import load_dotenv

load_dotenv()

dburl = os.getenv("LANCE_DB_IP")


class QueryInput(BaseModel):
    user_id: str
    query_text: str
    top_k: int = 5


class QueryData(BaseModel):
    user_id: str
    embedding: List[float]
    top_k: int = 5


def get_industry_names_from_yaml(file_path: str) -> set:
    with open(file_path, "r", encoding="utf-8") as file:
        data = yaml.safe_load(file)

    industry_names = set()
    for entry in data:
        industry = entry.get("SMB")
        if industry:
            industry_names.add(industry)

    return industry_names


# Flatten nested lists if any
def flatten_list(lst):
    flattened = []
    for item in lst:
        if isinstance(item, list):
            flattened.extend(flatten_list(item))
        else:
            flattened.append(item)
    return flattened


def get_usecases_for_smb(smb_name, data):
    for entry in data:
        if entry.get("SMB") == smb_name:
            return entry.get("Usecases", [])
    return []


def find_matching_industry(extracted_text: str, industries: set) -> str:
    matches = difflib.get_close_matches(
        extracted_text.lower(), [i.lower() for i in industries], n=1, cutoff=0.5
    )
    if matches:
        matched_lower = matches[0]
        # Return original-cased industry from set
        for industry in industries:
            if industry.lower() == matched_lower:
                return industry

    logger.warning(f"No matching industry found for: {extracted_text}")
    return None


def save_yaml_file(entries, filepath):
    with open(filepath, "w", encoding="utf-8") as f:
        yaml.dump(entries, f, sort_keys=False, allow_unicode=True)


def clean_question(line):
    line = line.strip().lstrip("1234567890. ").strip()  # remove numbering
    return line.strip("\"'")  # remove leading/trailing quotes


def generate_yaml_ques(usecase, prompts, industry, response_text):
    questions_text = generate_usecases_questions(
        prompts.get("usecase_prompt_template"),
        "gpt-3.5-turbo",
        usecase,
        industry,
        documents_contents=response_text,
    )

    question_list = [
        clean_question(line) for line in questions_text.split("\n") if line.strip()
    ]

    return {"UseCase": usecase, "questions": question_list}


def generate_yaml_ques_batch(usecases_with_docs, prompts, industry):
    questions_json = generate_usecases_questions_batch(
        prompts.get("usecase_prompt_template"),
        "gpt-3.5-turbo",
        industry,
        usecases_with_docs,
    )
    # logger.info(f"[🔍] Generated questions for {questions_json} industry.")

    all_entries = []
    for item in questions_json:
        # questions = [
        #     clean_question(q.split("—")[0].strip()) for q in item.get("questions", [])
        # ]
        all_entries.append(
            {"UseCase": item["usecase"], "questions": item.get("questions", [])}
        )
    logger.info(f"[🔍] Retrieved {len(all_entries)} questions for {industry} industry.")

    return all_entries


def fetch_ques_with_docs(
    usecases: list[str], userid: str, filenames: list[str]
) -> list[dict]:
    embeddings = OpenAIEmbeddings(
        model="text-embedding-3-large",
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        dimensions=3072,
    )

    vectors = embeddings.embed_documents(usecases)
    payload = {
        "user_id": userid,
        "embeddings": vectors,
        "top_k": 1,
        "filenames": filenames,
    }

    try:
        response = requests.post(f"{dburl}/query_batch", json=payload)
        response.raise_for_status()
        batch_results = response.json().get("results", [])
    except Exception as e:
        logger.error(f"[!] Batch query failed: {e}")
        return []

    usecases_with_docs = []
    for usecase, results in zip(usecases, batch_results):
        for r in results:
            usecases_with_docs.append(
                {
                    "query": usecase,
                    "response_text": r["text"].strip(),
                    "filename": r.get("foldername", "").strip(),
                    "doc_value": r.get("_distance", ""),
                }
            )

    return usecases_with_docs


def fetch_usecases_with_docs(
    usecases: list[str], userid: str, filenames: list[str]
) -> list[dict]:
    """
    Embeds all use cases and sends a single batch query to the LanceDB vector index.
    Returns a list of dictionaries with each use case and the top matching document.
    """
    embeddings = OpenAIEmbeddings(
        model="text-embedding-3-large",
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        dimensions=3072,
    )

    usecases_with_docs = []

    # Step 1: Embed all usecases in a single batch
    vectors = embeddings.embed_documents(usecases)

    # Step 2: Prepare and send batch query to LanceDB
    batch_payload = {
        "user_id": userid,
        "embeddings": vectors,
        "top_k": 1,
        "filenames": filenames,
    }

    try:
        response = requests.post(f"{dburl}/query_batch", json=batch_payload)
        response.raise_for_status()
        batch_results = response.json().get("results", [])
    except Exception as e:
        logger.error(f"[!] Batch query failed: {e}")
        return []

    # Step 3: Merge usecase and top document result
    for usecase, results in zip(usecases, batch_results):
        if results:
            # Combine up to top_k result texts
            # combined_text = "\n\n".join(
            #     r.get("text", "").strip() for r in results if r.get("text", "").strip()
            # ).strip()
            usecases_with_docs.append(
                {
                    "usecase": usecase,
                    "documents_contents": results[0].get("text", "").strip(),
                    "filename": results[0].get("foldername", "").strip(),
                }
            )
        else:
            logger.warning(f"[⚠️] No results found for usecase: {usecase}")
            usecases_with_docs.append(
                {
                    "usecase": usecase,
                    "documents_contents": "",
                    "filename": "",
                }
            )

    return usecases_with_docs

    # --- Helper: Remove old entries for given files ---


def remove_entries_for_files(filepath, filenames):
    """Remove all entries matching any of the given filenames."""
    if not os.path.exists(filepath):
        return []
    existing = load_yaml_file(filepath) or []

    flat_existing = flatten_list(existing)
    filenames_norm = [os.path.splitext(f.strip().lower())[0] for f in filenames]

    filtered = []
    for entry in flat_existing:
        if isinstance(entry, dict):
            file_val = (entry.get("filename") or "").strip().lower()
            file_val_no_ext = os.path.splitext(file_val)[0]
            if file_val_no_ext not in filenames_norm:
                filtered.append(entry)
        else:
            filtered.append(entry)

    logger.info(f"✅ Removed entries for given files: {len(filtered)} remaining")
    return filtered


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


def is_new_file(file_name, passed_data, failed_data):
    """Check if file has never been processed before."""
    file_no_ext = os.path.splitext(file_name.strip().lower())[0]
    return not any(
        os.path.splitext(e.get("filename", "").strip().lower())[0] == file_no_ext
        for e in passed_data + failed_data
    )


def append_to_failed_no_duplicates(failed_data, new_entries, passed_data):
    """
    Append new entries to failed_data only if not already in failed_data
    and not already in passed_data.
    """
    existing_keys = {(e.get("User"), e.get("filename")) for e in failed_data}
    passed_keys = {(p.get("User"), p.get("filename")) for p in passed_data}

    for entry in new_entries:
        key = (entry.get("User"), entry.get("filename"))
        if key not in existing_keys and key not in passed_keys:
            failed_data.append(entry)
            existing_keys.add(key)
    return failed_data


def preProcessDocWithUsecases(industry=None, userid=None, filenames=None):
    if not filenames or not isinstance(filenames, list):
        logger.warning("⚠ No filenames passed for QA processing.")
        return None

    data = load_yaml_file(path=pathconfig.smb_path)
    prompts = load_yaml_file(path=pathconfig.agent_template)
    if not data and not prompts:
        logger.error("❌ Missing usecase or prompt data.")
        return None

    usecases = get_usecases_for_smb(industry, data)
    if not usecases:
        logger.warning(f"⚠ No usecases found for industry: {industry}")
        return None

    logger.info(f"📂 Processing QAs for files: {filenames}")

    # File paths in S3
    passes_key = f"{userid}/yaml/passed_ques.yaml"
    failed_key = f"{userid}/yaml/failed_ques.yaml"

    passed_data = flatten_list(load_yaml_from_s3(passes_key) or [])
    failed_data = flatten_list(load_yaml_from_s3(failed_key) or [])

    # Determine if all files are new
    all_new = all(is_new_file(fn, passed_data, failed_data) for fn in filenames)

    if not all_new:
        failed_data = remove_entries_for_files(failed_data, filenames)

    # Step 1: Fetch docs & generate questions
    usecases_with_docs = fetch_usecases_with_docs(usecases, userid, filenames=filenames)
    all_entries = generate_yaml_ques_batch(usecases_with_docs, prompts, industry)
    logger.info(f"✅ Generated {len(all_entries)} question entries for {industry}.")

    # Step 2: Extract actual questions
    all_ques, actual_to_rephrased, actual_to_quote = [], {}, {}
    for entry in all_entries:
        for q in entry.get("questions", []):
            actual = q.get("actual_one", "").strip()
            rephrased = q.get("rephrased", "").strip()
            quote = q.get("quote", "").strip()
            if actual:
                all_ques.append(actual)
                actual_to_rephrased[actual] = rephrased
                actual_to_quote[actual] = quote

    # Step 3: Get answers & evaluate
    content = fetch_ques_with_docs(all_ques, userid, filenames=filenames)
    batch_size = 10
    valid_responses, clarification_responses = [], []

    for i in range(0, len(content), batch_size):
        batch = content[i : i + batch_size]
        res_raw = evaluator_batch_llama(
            prompts.get("new_response_validator_batch"), batch, industry
        )

        match = re.search(r"\[\s*{.*?}\s*\]", res_raw, re.DOTALL)
        try:
            res_json = yaml.safe_load(match.group(0)) if match else []
        except Exception as e:
            logger.error(f"❌ Error parsing evaluator JSON: {e}")
            res_json = []

        for original_item, eval_result in zip(batch, res_json):
            actual_q = original_item["query"]
            related_res = eval_result.get("related", False)
            usecase_res = eval_result.get("has_usecase_details", False)
            filename = original_item.get("filename", "").strip()
            distvalue = original_item.get("doc_value", "")

            entry_obj = {
                "User": actual_q,
                "Rephrased Question": actual_to_rephrased.get(actual_q, ""),
                "Ai Response": eval_result.get("explanation", ""),
                "quote": actual_to_quote.get(actual_q, ""),
                "filename": filename,
                "doc_value": distvalue,
            }

            if related_res and usecase_res:
                entry_obj["date_processed"] = datetime.now().isoformat(
                    timespec="seconds"
                )
                valid_responses.append(entry_obj)
            else:
                clarification_responses.append(entry_obj)

    # Step 4–6: Update passed/failed
    npassed_data = append_passed_with_ai_diff(passed_data, valid_responses)
    answered_keys = {(v.get("User"), v.get("filename")) for v in valid_responses}
    failed_data = [
        e
        for e in failed_data
        if (e.get("User"), e.get("filename")) not in answered_keys
    ]

    passed_keys = {(p.get("User"), p.get("filename")) for p in npassed_data}
    clarification_responses = [
        c
        for c in clarification_responses
        if (c.get("User"), c.get("filename")) not in passed_keys
    ]
    failed_data = append_to_failed_no_duplicates(
        failed_data, clarification_responses, npassed_data
    )

    # Save back to S3
    if npassed_data:
        save_yaml_to_s3(npassed_data, userid, "passed_ques.yaml")
    if failed_data:
        save_yaml_to_s3(failed_data, userid, "failed_ques.yaml")

    logger.info(f"✅ Saved updated QAs to S3")

    return {
        "processed_files": filenames,
        "passed_path": passes_key,
        "failed_path": failed_key,
    }


def clarific_transcriptions(userid, val, filename, config_filename, transcript_path):
    clarification_responses = []
    failed_key = f"{userid}/yaml/failed_ques.yaml"

    failed_ques = flatten_list(load_yaml_from_s3(failed_key) or [])

    # Load existing data if any
    quote_summary = val["summary"] if "summary" in val else filename
    # Load existing clarifications (safe fallback)
    try:
        failed_data = flatten_list(load_yaml_file(failed_ques) or [])
    except Exception:
        failed_data = []

    # Process new clarifications
    for actual_q in val.get("clarifications", []):
        actual_q = actual_q.strip()
        if not actual_q:
            continue

        entry_obj = {
            "User": actual_q,
            "Rephrased Question": actual_q,
            "Ai Response": "",
            "quote": quote_summary,
            "filename": filename,
            "doc_value": 0,
            "is_audio": f"{userid}/aud_scripts/{config_filename}",
            "rec_id": transcript_path,
        }
        clarification_responses.append(entry_obj)

    # Merge old + new clarifications
    updated_data = failed_data + clarification_responses

    # Save back into YAML
    save_yaml_file(updated_data, filepath=failed_ques)

    return clarification_responses


def remove_transcript_clarifications(userid, config_path, rec_id):
    """Remove one clarification count for a given transcript file in recordings config."""
    config = read_json_from_s3(config_path)
    trans_filename = extract_filename(config_path)

    if not config:
        logger.error(f"❌ Could not load config from {config_path}")
        return None

    recordings = config.get("recordings", [])
    logger.info(f"📂 Loaded {len(recordings)} recordings from {config_path}")
    logger.info(f"🎯 Looking for rec_id={rec_id}")

    changed = False
    for recording in recordings:
        logger.debug(
            f"🔎 Checking recording id={recording.get('id')} (clarifications={recording.get('clarifications')})"
        )
        if str(recording.get("id")) == str(rec_id):
            clari = recording.get("clarifications", 0)
            if isinstance(clari, int) and clari > 0:
                recording["clarifications"] = clari - 1
                changed = True
                logger.info(
                    f"✅ Clarification count for {rec_id} decreased: {clari} -> {recording['clarifications']}"
                )
            else:
                logger.warning(f"⚠ No clarifications left to remove for {rec_id}")
            break

    if changed:
        config_local_path = os.path.join("/tmp", trans_filename)
        with open(config_local_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

        upload_any_file(
            config_local_path, user_id=userid, file_name=trans_filename, type="audio"
        )
        os.remove(config_local_path)

        return config

    logger.warning(f"⚠ Recording with id={rec_id} not found in {config_path}")
    return None
