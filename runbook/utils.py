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
        if not result:
            return ""
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
    return ""


async def get_playbook_instruction(user_id, filename):
    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"
    s3_key = f"{user_id}/workflow/{base_name(filename)}/{filename}"
    instruction_data = read_json_from_s3(s3_key)

    # qna = await extract_qna_from_instruction(instruction_data=instruction_data)
    # qna = instruction_data.get("chat", [])
    return instruction_data


# =====================================================================
# Connector data resolution (generic + AWS / Azure / GCP)
# ---------------------------------------------------------------------
# Resolves a selected API-connector endpoint into the data fed to the
# runbook/report "responses" (blocks) and "evidence" sections.
#
# Strategy: prefer the LATEST SCHEDULED RUN cached in S3 under the
# provider-specific prefix, decrypted with that provider's per-user KMS
# helper. If no cached run exists, fall back to a live fetch via the
# provider's internal executor.
#
# Runs are stored under, and encrypted for, the CONNECTOR OWNER's
# user_id (the admin who created/scheduled it) — not necessarily the
# runbook owner — so we look the owner up from the endpoint table and
# use it for both the S3 prefix and decryption.
# =====================================================================
_CONNECTOR_PROVIDERS = {
    "custom": {"table": "external_app_endpoints", "s3_prefix": "apiconnectors"},
    "aws": {"table": "aws_external_app_endpoints", "s3_prefix": "aws_connector"},
    "azure": {"table": "azure_external_app_endpoints", "s3_prefix": "azure_connector"},
    "gcp": {"table": "gcp_external_app_endpoints", "s3_prefix": "gcp_connector"},
}


def _connector_endpoint_owner(conn, provider, endpoint_id):
    """Return {'user_id', 'app_id'} for a connector endpoint, or None."""
    import pymysql

    cfg = _CONNECTOR_PROVIDERS.get(provider)
    if not cfg:
        return None
    # table name comes from the fixed _CONNECTOR_PROVIDERS map, not user input
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(
            f"SELECT user_id, app_id FROM {cfg['table']} WHERE id=%s",
            (endpoint_id,),
        )
        return cur.fetchone()


def _connector_decrypt(provider, user_id, val):
    if provider == "aws":
        from aws_integration.helpers import _dec_run
    elif provider == "azure":
        from azure_integration.helpers import _dec_run
    elif provider == "gcp":
        from gcp_integration.helpers import _dec_run
    else:
        from apiConnector.helpers import _dec_run
    return _dec_run(user_id, val)


async def _connector_live_execute(provider, endpoint_id, user_id):
    if provider == "aws":
        from aws_integration.helpers import _execute_aws_endpoint_internal as _fn
    elif provider == "azure":
        from azure_integration.helpers import _execute_azure_endpoint_internal as _fn
    elif provider == "gcp":
        from gcp_integration.helpers import _execute_gcp_endpoint_internal as _fn
    else:
        from apiConnector.helpers import _execute_endpoint_internal as _fn
    return await _fn(endpoint_id, user_id)


async def resolve_connector_endpoint_data(
    conn, provider, endpoint_id, app_id=None, max_chars=8000
):
    """Resolve one connector endpoint to (data_str, source).

    source is 'scheduled' (latest cached run) or 'live' (fresh fetch fallback).
    Raises ValueError if the endpoint can't be found.
    """
    from utils.s3_utils import get_filedata_endp, getallendpointdetails

    provider = (provider or "custom").lower()
    cfg = _CONNECTOR_PROVIDERS.get(provider)
    if not cfg:
        raise ValueError(f"Unknown connector provider: {provider}")

    owner = _connector_endpoint_owner(conn, provider, endpoint_id)
    if not owner:
        raise ValueError(f"{provider} endpoint {endpoint_id} not found")
    owner_user_id = owner["user_id"]
    app_id = app_id or owner["app_id"]

    # 1. Latest scheduled run from the provider-prefixed S3 path
    prefix = f"{owner_user_id}/{cfg['s3_prefix']}/{app_id}/{endpoint_id}/"
    try:
        files = getallendpointdetails(prefix)  # newest first
    except Exception as e:
        logger.warning(
            "Connector run listing failed (%s ep=%s): %s", provider, endpoint_id, e
        )
        files = []

    if files:
        record = get_filedata_endp(prefix + files[0]["file"])
        # each S3 object is a list of appended run records; newest is last
        if isinstance(record, list):
            record = record[-1] if record else {}
        resp = record.get("response") if isinstance(record, dict) else record
        resp = _connector_decrypt(provider, owner_user_id, resp)
        # execute() result is {success, status_code, response}; prefer payload
        if isinstance(resp, dict) and "response" in resp:
            resp = resp["response"]
        return str(resp)[:max_chars], "scheduled"

    # 2. Fallback: live fetch via the provider executor (owner context)
    result = await _connector_live_execute(provider, endpoint_id, owner_user_id)
    resp = result.get("response") if isinstance(result, dict) else result
    return str(resp)[:max_chars], "live"


async def retreval_from_sources(
    conn, dbserver, main_source, filesources, userid, payload
):
    from umail.routes import get_sorted_lance_emails

    logger.debug("Sources — main: %s  files: %s", main_source, filesources)
    filesources = normalize_json_field(filesources)

    data_for_review = []
    extracted_text_len = 0
    # -------------------------
    # APP SOURCE  (generic + AWS / Azure / GCP connectors)
    # -------------------------
    if main_source == "app" and filesources:
        # Enriched shape: apps=[{provider, app_id, endpoint_id}]
        app_entries = list(filesources.get("apps") or [])
        # Legacy shape: endpoint_ids=[...] → generic ("custom") connectors
        for eid in filesources.get("endpoint_ids", []):
            app_entries.append({"provider": "custom", "endpoint_id": eid})

        for entry in app_entries:
            provider = (entry.get("provider") or "custom").lower()
            endpoint_id = entry.get("endpoint_id")
            app_id = entry.get("app_id")
            if endpoint_id is None:
                continue
            try:
                data_str, run_source = await resolve_connector_endpoint_data(
                    conn, provider, endpoint_id, app_id
                )
                extracted_text_len += len(data_str)
                data_for_review.append(
                    {
                        "type": "app",
                        "provider": provider,
                        "endpoint_id": endpoint_id,
                        "run_source": run_source,
                        "data": data_str,
                    }
                )
            except Exception as e:
                data_for_review.append(
                    {"endpoint_id": endpoint_id, "provider": provider, "error": str(e)}
                )

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
    # -------------------------
    # POLICIES & PROCEDURES (always appended as governance context regardless of main_source)
    # -------------------------
    policy_ids = (filesources or {}).get("policy_ids", [])
    if policy_ids:
        import os as _os
        from utils.s3_utils import load_yaml_from_s3
        _framework_owner = _os.getenv("FRAMEWORK_OWNER", "service@bytoid.ca")
        for policy_id in policy_ids:
            # Policies are stored under the service account path, not the individual user path
            s3_key = f"{_framework_owner}/policies/{policy_id}.yaml"
            try:
                policy_data = load_yaml_from_s3(s3_key)
                if policy_data:
                    content = policy_data.get("content", "")
                    title = policy_data.get("title", policy_id)
                    policy_type = policy_data.get("type", "policy")
                    extracted_text_len += len(content)
                    data_for_review.append({
                        "type": "policy",
                        "source": title,
                        "data": f"[{policy_type.upper()}] {title}\n\n{content}",
                    })
            except Exception as e:
                logger.warning("Failed to load policy %s: %s", policy_id, e)

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


def get_policies_for_frameworks(framework_names=None, framework_ids=None):
    """Return list of policy_ids for policies that belong to any of the given
    framework names or framework ids. Scans the shared framework policies
    stored under the FRAMEWORK_OWNER S3 path.

    Args:
        framework_names (list[str] | None): list of framework display names
        framework_ids (list[str] | None): list of framework UUIDs or ids

    Returns:
        list[str]: unique policy_id values
    """
    from utils.s3_utils import list_all_files, load_yaml_from_s3
    import os as _os

    framework_names = framework_names or []
    framework_ids = framework_ids or []

    _framework_owner = _os.getenv("FRAMEWORK_OWNER", "service@bytoid.ca")
    prefix = f"{_framework_owner}/policies/"

    objs = list_all_files(prefix)
    policy_ids = set()

    for obj in objs or []:
        key = obj.get("Key") if isinstance(obj, dict) else obj
        if not key or not key.startswith(prefix):
            continue
        try:
            data = load_yaml_from_s3(key)
            if not data:
                continue

            pid = data.get("policy_id") or _os.path.splitext(_os.path.basename(key))[0]

            # match by framework display names
            p_frameworks = data.get("frameworks") or []
            if any(fn in p_frameworks for fn in framework_names if fn):
                policy_ids.add(pid)
                continue

            # match by framework ids stored on the policy (flexible fields)
            pf_ids = data.get("framework_ids") or data.get("framework_id")
            if pf_ids:
                if isinstance(pf_ids, list) and any(fid in pf_ids for fid in framework_ids if fid):
                    policy_ids.add(pid)
                    continue
                if isinstance(pf_ids, str) and pf_ids in framework_ids:
                    policy_ids.add(pid)
                    continue

        except Exception:
            # ignore policies we can't read
            continue

    return list(policy_ids)


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
        result_id=msg.get("result_id"),
        previous_result_id=msg.get("previous_result_id"),
    )



