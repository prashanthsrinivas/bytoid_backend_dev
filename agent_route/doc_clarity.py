from langchain_openai import OpenAIEmbeddings
import yaml
from utils.normal import load_yaml_file
from typing import List
import logging
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
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


def save_yaml_file(entry, filepath):
    with open(filepath, "a", encoding="utf-8") as f:
        yaml.dump([entry], f, sort_keys=False, allow_unicode=True)


def save_yaml(entry, folder, filename="responses.yaml"):
    os.makedirs(folder, exist_ok=True)  # Ensure folder exists
    filepath = os.path.join(folder, filename)
    with open(filepath, "a", encoding="utf-8") as f:
        yaml.dump([entry], f, sort_keys=False, allow_unicode=True)


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
    if not os.path.exists(filepath):
        return []
    with open(filepath, "r", encoding="utf-8") as f:
        existing = yaml.safe_load(f) or []

    # Flatten nested lists, if any
    flat_existing = []
    for item in existing:
        if isinstance(item, list):
            flat_existing.extend(item)
        else:
            flat_existing.append(item)

    # Normalize filenames for comparison (strip/lower/no extension)
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

    return filtered


def preProcessDocWithUsecases(industry=None, userid=None, filenames=None):
    """
    Generate questions and answers for specific files (new or updated).
    Merge results into existing passed_ques.yaml and failed_ques.yaml without losing old entries.
    """

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

    main_folder = f"{pathconfig.basepath}/{userid}"
    os.makedirs(main_folder, exist_ok=True)

    ques_filepath = os.path.join(main_folder, "main_ques.yaml")
    passes_files = os.path.join(main_folder, "passed_ques.yaml")
    failed_ques = os.path.join(main_folder, "failed_ques.yaml")

    # Step 1: Fetch docs & generate questions only for these files
    usecases_with_docs = fetch_usecases_with_docs(usecases, userid, filenames=filenames)
    all_entries = generate_yaml_ques_batch(usecases_with_docs, prompts, industry)

    logger.info(f"✅ Generated {len(all_entries)} question entries for {industry}.")

    # Step 2: Extract actual questions & mappings
    all_ques = []
    actual_to_rephrased = {}
    actual_to_quote = {}
    for entry in all_entries:
        for q in entry.get("questions", []):
            actual = q.get("actual_one", "").strip()
            rephrased = q.get("rephrased", "").strip()
            quote = q.get("quote", "").strip()
            if actual:
                all_ques.append(actual)
                actual_to_rephrased[actual] = rephrased
                actual_to_quote[actual] = quote

    logger.info(f"filenames type: {type(filenames)}, value: {filenames}")
    logger.info(f"usecases type: {type(all_ques)}, value: {all_ques}")

    # Step 3: Get answers from LanceDB
    content = fetch_ques_with_docs(all_ques, userid, filenames=filenames)
    batch_size = 10
    valid_responses = []
    clarification_responses = []

    logger.info(f"📊 Total questions for evaluation: {len(content)}")

    for i in range(0, len(content), batch_size):
        batch = content[i : i + batch_size]
        res_raw = evaluator_batch_llama(
            prompts.get("new_response_validator_batch"), batch, industry
        )

        match = re.search(r"\[\s*{.*?}\s*\]", res_raw, re.DOTALL)
        if match:
            try:
                res_json = yaml.safe_load(match.group(0))
                logger.INFO("✅ Extracted JSON block from evaluator.")
            except Exception as e:
                logger.error(f"❌ Error parsing evaluator JSON: {e}")
                res_json = []
        else:
            logger.error("❌ No JSON array block found in evaluator output.")
            res_json = []

        for original_item, eval_result in zip(batch, res_json):
            actual_q = original_item["query"]
            related_res = eval_result.get("related", False)
            usecase_res = eval_result.get("has_usecase_details", False)
            filename = original_item.get("filename", "").strip()

            entry_obj = {
                "User": actual_q,
                "Rephrased Question": actual_to_rephrased.get(actual_q, ""),
                "Ai Response": eval_result.get("explanation", ""),
                "quote": actual_to_quote.get(actual_q, ""),
                "filename": filename,
            }

            if related_res and usecase_res:
                valid_responses.append(entry_obj)
            else:
                clarification_responses.append(entry_obj)

    # Step 4: Merge into existing YAMLs
    existing_passed = remove_entries_for_files(passes_files, filenames)
    existing_failed = remove_entries_for_files(failed_ques, filenames)

    if valid_responses:
        merged_passed = existing_passed + valid_responses
        save_yaml_file(merged_passed, filepath=passes_files)

    if clarification_responses:
        merged_failed = existing_failed + clarification_responses
        save_yaml_file(merged_failed, filepath=failed_ques)

    logger.info(f"✅ Merged QAs into {passes_files} and {failed_ques}")
    return {
        "processed_files": filenames,
        "passed_path": passes_files,
        "failed_path": failed_ques,
    }
