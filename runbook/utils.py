import json
import os
import uuid
import pandas as pd
import copy

from credits_route.route import Credits
from db.lance_db_service import LanceDBServer
from db.rds_db import connect_to_rds

# from services.scheduler_service import SchedulerService
from playbook.helperzz import base_name
from utils.normal import ensure_dir, load_yaml_file
from utils.s3_utils import S3_BUCKET, delete_file_from_s3, read_json_from_s3, s3bucket, upload_any_file
from utils.normal import load_yaml_file

from cust_helpers import pathconfig
from db.db_checkers import get_notes_data

from utils.base_logger import get_logger
from utils.app_configs import IS_DEV

dbserver = LanceDBServer()
conn = connect_to_rds()
credits = Credits(conn)


RUNBOOK_TEMPLATE = load_yaml_file(path=pathconfig.runbook_prompts)
RADAR_TEMPLATE = load_yaml_file(path=pathconfig.radar_prompts)
logger = get_logger(__name__, log_level="DEBUG" if IS_DEV else "INFO")


import re, io


REPORT_MESSAGES = {
    "start": "Initializing report generation...",
    "phases": [
        "Analyzing input data",
        "Structuring report sections",
        "Generating insights",
        "Compiling report",
    ],
    "chunk_start": "Generating report section {current}/{total}...",
    "chunk_success": "Report section {current} generated.",
    "chunk_warning": "Section {current} had minor formatting issues.",
    "chunk_error": "Issue while generating section {current}.",
    "final": "Report generation completed successfully.",
}
RISK_MESSAGES = {
    "start": "Initializing risk analysis...",
    "phases": [
        "Scanning risk inputs",
        "Evaluating threats",
        "Calculating impact",
        "Finalizing risk report",
    ],
    "chunk_start": "Analyzing risk segment {current}/{total}...",
    "chunk_success": "Risk segment {current} analyzed.",
    "chunk_warning": "Partial issue in risk segment {current}.",
    "chunk_error": "Error during risk analysis segment {current}.",
    "final": "Risk analysis completed successfully.",
}


def _safe_json_parse_full(value):
    if value is None:
        return None

    # ✅ KEEP LIST AS-IS
    if isinstance(value, list):
        return value  # <-- FIXED

    if isinstance(value, dict):
        return value  # also allow dict

    if not isinstance(value, str):
        return None

    s = value.strip()

    if "```" in s:
        s = re.sub(r"```json|```", "", s).strip()

    if s.startswith('"') and s.endswith('"'):
        try:
            s = json.loads(s)
        except Exception:
            s = s[1:-1]

    try:
        if s.startswith("{") or s.startswith("["):
            return json.loads(s)
    except Exception:
        pass

    try:
        match = re.search(r"(\{.*\}|\[.*\])", s, re.DOTALL)
        if match:
            return json.loads(match.group(1))
    except Exception:
        pass

    logger.error("JSON parse failed. Raw: %s", value)

    return None


def _safe_json_parse(value):
    if value is None:
        return None

    if isinstance(value, list):
        return value[0] if value else {}

    if not isinstance(value, str):
        return None

    s = value.strip()

    # -----------------------------
    # REMOVE MARKDOWN JSON BLOCKS
    # -----------------------------
    if "```" in s:
        s = re.sub(r"```json|```", "", s).strip()

    # -----------------------------
    # HANDLE DOUBLE-ENCODED STRING JSON
    # -----------------------------
    if s.startswith('"') and s.endswith('"'):
        try:
            s = json.loads(s)
        except Exception:
            s = s[1:-1]

    # -----------------------------
    # EXTRACT JSON OBJECT SAFELY
    # -----------------------------
    try:
        # direct parse
        if s.startswith("{") or s.startswith("["):
            return json.loads(s)
    except Exception:
        pass

    # -----------------------------
    # LAST RESORT: extract JSON blob
    # -----------------------------
    try:
        match = re.search(r"(\{.*\}|\[.*\])", s, re.DOTALL)
        if match:
            return json.loads(match.group(1))
    except Exception:
        pass

    # ❌ DO NOT hide failure silently anymore
    logger.error("JSON parse failed. Raw: %s", value)

    return None


def render_runbook_yaml(runbook):

    template = RUNBOOK_TEMPLATE["runbook"]

    rendered = json.loads(
        json.dumps(template)
        .replace("${runbook_name}", runbook.get("name", ""))
        .replace("${runbook_type}", runbook.get("runbook_type", ""))
        .replace("${schedule_type}", runbook.get("schedule_type", "cron"))
        .replace("${cron_expression}", runbook.get("cron", ""))
        .replace("${input_type}", runbook.get("input_type", ""))
        .replace("${playbook_id}", str(runbook.get("playbook_id") or ""))
        .replace("${api_endpoint}", str(runbook.get("api_endpoint") or ""))
        .replace("${log_source}", str(runbook.get("log_source") or ""))
    )

    return rendered


def read_csv_logs_from_s3(s3_key):
    try:
        # print("📥 Fetching S3 key:", s3_key)

        response = s3bucket().get_object(Bucket=S3_BUCKET, Key=s3_key)

        content = response["Body"].read()
        # ✅ Case 1: CSV
        if s3_key.endswith(".csv"):
            df = pd.read_csv(io.StringIO(content))
            return df.to_dict(orient="records")

        # ✅ Case 2: JSON logs
        elif s3_key.endswith(".json"):
            return json.loads(content.decode("utf-8"))

        elif s3_key.endswith(".xlsx") or s3_key.endswith(".xls"):
            df = pd.read_excel(io.BytesIO(content))
            return df.to_dict(orient="records")

        # ✅ Case 3: TXT logs (YOUR CASE)
        else:
            lines = content.splitlines()

            logs = []
            for line in lines:
                if line.strip():
                    logs.append({"message": line})

            return logs

    except Exception as e:
        logger.error("S3 read error: %s", e)
        raise Exception(f"S3 log read failed: {s3_key} | {str(e)}")


async def collect_runbook_inputs(runbook):
    # dbserver = LanceDBServer()
    if runbook.get("runtime_input"):
        return runbook["runtime_input"]

    elif runbook.get("playbook_id"):
        result = await get_playbook_instruction(
            user_id=runbook["user_id"], filename=str(runbook["playbook_id"])
        )
        return json.dumps(result.get("chat", []))

    elif runbook.get("api_endpoint"):
        result = await dbserver.get_app_runs(
            user_id=runbook["user_id"],
            app_id=str(runbook["app_id"]),
            endpoint_id=str(runbook["api_endpoint"]),
        )
        return json.dumps(result)[:3000]

    elif runbook.get("log_source"):

        s3_key = runbook.get("log_source")

        if not s3_key:
            raise Exception("Missing log_source")

        logs = read_csv_logs_from_s3(s3_key)
        # logs_str = reconstruct_and_format_logs(logs)
        formatted_logs = []

        for log in logs:
            try:
                message = log.get("message")

                if isinstance(message, str) and message.startswith("{"):
                    msg_json = json.loads(message)
                    msg = msg_json.get("Message", message)
                    level = msg_json.get("LogLevel", "")
                    time = msg_json.get("Time", "")
                    formatted_logs.append(f"[{time}] [{level}] {msg}")
                else:
                    formatted_logs.append(str(message))

            except Exception:
                formatted_logs.append(str(log))

        logs_str = "\n".join(formatted_logs)

        return logs_str
    raise ValueError("Runbook requires questionnaire/api/log input")


async def get_playbook_instruction(user_id, filename):
    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"
    s3_key = f"{user_id}/workflow/{base_name(filename)}/{filename}"
    instruction_data = read_json_from_s3(s3_key)

    # qna = await extract_qna_from_instruction(instruction_data=instruction_data)
    # qna = instruction_data.get("chat", [])
    return instruction_data


async def retreval_from_sources(
    conn, dbserver, main_source, filesources, userid, payload
):
    from umail.routes import get_sorted_lance_emails
    from apiConnector.helpers import _execute_endpoint_internal

    logger.debug("Sources — main: %s  files: %s", main_source, filesources)
    filesources = normalize_json_field(filesources)

    data_for_review = []
    extracted_text_len = 0
    # -------------------------
    # APP SOURCE
    # -------------------------
    if main_source == "app" and filesources:
        endpoint_ids = filesources.get("endpoint_ids", [])

        for endpoint_id in endpoint_ids:
            try:
                result = await _execute_endpoint_internal(
                    endpoint_id=endpoint_id,
                    userid=userid,
                )
                extracted_text_len += len(result.get("response"))
                data_for_review.append(
                    {
                        "type": "app",
                        "endpoint_id": endpoint_id,
                        "data": str(result.get("response")),
                    }
                )
            except Exception as e:
                data_for_review.append({"endpoint_id": endpoint_id, "error": str(e)})

    # -------------------------
    # NOTES SOURCE
    # -------------------------
    elif main_source == "notes" and filesources:
        note_ids = filesources.get("note_ids", [])
        all_notes = get_notes_data(userid)  # expect list[ {note_id, content, ...} ]
        # print("len of all_notes", len(all_notes), all_notes)
        for note in all_notes.get("notes"):
            # print("type of note", type(note), note)
            extracted_text_len += len(note)
            if note.get("note_id") in note_ids:
                data_for_review.append(
                    {"type": "notes", "note_id": note.get("note_id"), "data": str(note)}
                )

    # -------------------------
    # EMAIL SOURCE
    # -------------------------
    elif main_source == "emails" and filesources:
        client_ids = filesources.get("client_ids", [])
        for i in client_ids:
            data_for_review.append(
                {
                    "type": "emails",
                    "clientid": i,
                    "data": str(
                        get_sorted_lance_emails(
                            connection=conn, user_id=userid, client_id=i
                        )
                    ),
                }
            )
        # all_emails = get_emails_data(userid)

    # -------------------------
    # KNOWLEDGE SOURCE (LanceDB / Docs)
    # -------------------------
    elif main_source == "knowledge" and filesources:
        filenames = filesources.get("filenames", [])
        for file in filenames:
            if file.get("type") == "docs":
                fname = file.get("filename")
                # print("payload", payload)
                logger.debug("In full data extraction")
                newdas = await dbserver.fetch_by_filename(
                    user_id=userid, filename=fname
                )
                if newdas:
                    for item in newdas:
                        extracted_text_len += len(item.get("text", ""))
                        data_for_review.append(
                            {
                                "type": "docs",
                                "source": fname,
                                "data": str(item.get("text", "")),
                            }
                        )

            elif file.get("type") == "aud":
                bfname = file.get("filename")
                base = os.path.basename(bfname)
                name_without_ext = os.path.splitext(base)[0]
                fname = f"{name_without_ext}_transcript.json"

                results = await dbserver.rec_query_vector_foldername(
                    query=payload, foldername=fname
                )
                if results:
                    for item in results:
                        extracted_text_len += len(item.get("text", ""))
                        data_for_review.append(
                            {
                                "type": "audio",
                                "source": fname,
                                "data": str(item.get("text", "")),
                            }
                        )

            elif file.get("type") == "scrape":
                url = file.get("url")
                results = dbserver.search_scraped_data_by_url(query=payload, url=url)
                if results:
                    extracted_text_len += len(results.get("text", ""))
                    data_for_review.append(
                        {
                            "type": "scrape",
                            "source": url,
                            "data": str(results.get("text", "")),
                        }
                    )
    logger.info("Total extracted text length: %d", extracted_text_len)

    return data_for_review


def merge_runbook_chunks_deterministic(
    raw_chunks, output_language="english", runbook_id=None, execution_id=None
):

    if not raw_chunks:
        return {}

    merged = {
        "document_meta": {},
        "structure_rationale": "",
        "analysis_depth": None,
        "analysis_depth_rationale": None,
        "recommendation_depth": None,
        "recommendation_depth_rationale": None,
        "recommendation_intent": [],
        "confidence_level": None,
        "core_objective": None,
        "intent_type": None,
        "blocks": [],
        "estimated_word_count": 0,
        # Runbook specific metadata
        "runbook_meta": {
            "runbook_id": runbook_id,
            "execution_id": execution_id,
            "output_language": output_language,
        },
    }

    block_index = {}

    for chunk in raw_chunks:

        # -----------------------------
        # document_meta merge
        # -----------------------------
        if "document_meta" in chunk:
            merged["document_meta"].update(chunk["document_meta"])

        # -----------------------------
        # structure rationale
        # -----------------------------
        if chunk.get("structure_rationale") and not merged["structure_rationale"]:
            merged["structure_rationale"] = chunk["structure_rationale"]

        # -----------------------------
        # analysis depth
        # -----------------------------
        merged["analysis_depth"] = chunk.get("analysis_depth", merged["analysis_depth"])

        merged["analysis_depth_rationale"] = chunk.get(
            "analysis_depth_rationale", merged["analysis_depth_rationale"]
        )

        # -----------------------------
        # recommendation depth
        # -----------------------------
        merged["recommendation_depth"] = chunk.get(
            "recommendation_depth", merged["recommendation_depth"]
        )

        merged["recommendation_depth_rationale"] = chunk.get(
            "recommendation_depth_rationale", merged["recommendation_depth_rationale"]
        )

        # -----------------------------
        # recommendation intent
        # -----------------------------
        for intent in chunk.get("recommendation_intent", []):
            if intent not in merged["recommendation_intent"]:
                merged["recommendation_intent"].append(intent)

        # -----------------------------
        # intent / objective
        # -----------------------------
        merged["intent_type"] = chunk.get("intent_type", merged["intent_type"])

        merged["core_objective"] = chunk.get("core_objective", merged["core_objective"])

        # -----------------------------
        # confidence level
        # -----------------------------
        merged["confidence_level"] = (
            chunk.get("confidence_level")
            or chunk.get("document_meta", {}).get("confidence_level")
            or merged["confidence_level"]
        )

        # -----------------------------
        # word count aggregation
        # -----------------------------
        if "estimated_word_count" in chunk:
            merged["estimated_word_count"] += chunk["estimated_word_count"]

        elif "document_meta" in chunk:
            merged["estimated_word_count"] += chunk["document_meta"].get(
                "estimated_word_count", 0
            )

        # -----------------------------
        # blocks merge
        # -----------------------------
        for block in chunk.get("blocks", []):

            block_id = block.get("block_id")

            if not block_id:
                continue

            block.setdefault("micro_blocks", [])

            if block_id not in block_index:

                new_block = copy.deepcopy(block)

                merged["blocks"].append(new_block)

                block_index[block_id] = new_block

            else:

                existing_block = block_index[block_id]

                existing_block.setdefault("micro_blocks", [])

                existing_micro_ids = {
                    mb.get("micro_id") for mb in existing_block["micro_blocks"]
                }

                for micro in block["micro_blocks"]:

                    micro_id = micro.get("micro_id")

                    if micro_id not in existing_micro_ids:
                        existing_block["micro_blocks"].append(copy.deepcopy(micro))

    # # -----------------------------
    # # deterministic block ordering
    # # -----------------------------
    # merged["blocks"] = sorted(merged["blocks"], key=lambda x: x.get("block_id", ""))

    # -----------------------------
    # cleanup empty fields
    # -----------------------------
    merged = {k: v for k, v in merged.items() if v not in [None, "", [], {}]}

    return merged


def normalize_json_field(field):
    """
    Normalize JSON field using regex cleanup + safe parsing
    """
    if not field:
        return None

    # Already dict
    if isinstance(field, dict):
        return field

    if isinstance(field, str):
        field = field.strip()

        # 🔥 Step 1: Remove wrapping quotes (if entire JSON is quoted)
        # Matches: " {...} "
        if re.match(r'^"\s*\{.*\}\s*"$', field):
            field = field[1:-1]

        # 🔥 Step 2: Unescape escaped quotes
        # \" → "
        field = re.sub(r'\\"', '"', field)

        # 🔥 Step 3: Remove unnecessary outer whitespace again
        field = field.strip()

        # 🔥 Step 4: Parse JSON safely
        try:
            return json.loads(field)
        except Exception:
            return None

    return None


async def send(ws_sender, msg, user_id):
    if not msg:
        return

    await ws_sender.emit(
        user_id=user_id,
        message=msg.get("message"),
        scope=msg.get("scope", "global"),
        session_id=msg.get("session_id"),
        job_id=msg.get("job_id"),
        msg_type=msg.get("type"),
        stage=msg.get("stage"),
        progress=msg.get("progress"),
        feature="runbook",
    )



