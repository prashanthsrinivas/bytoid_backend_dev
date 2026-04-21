import asyncio
import json
import os
import time
import traceback
from urllib.parse import parse_qs, unquote, urlparse
import uuid
from agent_route.doc_clarity import QueryData
import boto3
import hashlib
from credits_route.route import Credits
from db.lance_db_service import LanceDBServer
from db.rds_db import connect_to_rds

# from services.scheduler_service import SchedulerService
from utils.img_tokens import image_credit_cost
from utils.normal import load_yaml_file
from utils.s3_utils import read_json_from_s3, upload_any_file
from utils.fireworkzz import (
    get_firework_embedding,
    get_think_bedrok_response,
    get_think_fire_response2_og,
    get_think_fire_response2_og2,
)
from utils.normal import load_yaml_file
from radar.radar_helpers import process_file_payloads

from cust_helpers import pathconfig
from utils.base_logger import get_logger
from .utils import *
from .utils import _safe_json_parse
from .utils import _safe_json_parse_full
from utils.scheduler import scheduler
from apscheduler.triggers.cron import CronTrigger

dbserver = LanceDBServer()
conn = connect_to_rds()
credits = Credits(conn)


RUNBOOK_TEMPLATE = load_yaml_file(path=pathconfig.runbook_prompts)
RADAR_TEMPLATE = load_yaml_file(path=pathconfig.radar_prompts)
logger = get_logger(__name__)


def schedule_runbook_log(runbook):

    cron_expr = runbook.get("schedule")

    if not cron_expr:
        return
    if cron_expr == "1m":
        cron_expr = "*/1 * * * *"
    elif cron_expr == "2m":
        cron_expr = "*/2 * * * *"
    elif cron_expr == "5m":
        cron_expr = "*/5 * * * *"
    elif cron_expr == "10m":
        cron_expr = "*/10 * * * *"
    elif cron_expr == "15m":
        cron_expr = "*/15 * * * *"
    elif cron_expr == "1h":
        cron_expr = "0 * * * *"
    elif cron_expr == "daily":
        cron_expr = "0 0 * * *"

    trigger = CronTrigger.from_crontab(cron_expr)

    # ✅ FIX: wrap async properly
    scheduler.add_job(
        run_runbook_job_wrapper,
        trigger=trigger,
        id=runbook["runbook_id"],
        args=[runbook],
        replace_existing=True,
    )

    print(f"✅ Scheduled runbook {runbook['runbook_id']}")


async def run_runbook_job(runbook):
    #  print(f"🔥 JOB TRIGGERED: {runbook['runbook_id']}")

    try:
        print(f"🚀 Running runbook {runbook['runbook_id']}")
        dbserver = LanceDBServer()
        await run_runbook_execution_engine(
            dbserver=dbserver,
            user_id=runbook["user_id"],
            runbook=runbook,
        )
        print(f"✅ COMPLETED: {runbook['runbook_id']}")
    except Exception as e:
        print("❌ FULL ERROR:", traceback.format_exc())
        print(f"❌ Runbook failed: {e}")


def run_runbook_job_wrapper(runbook):
    # print("🚀 WRAPPER TRIGGERED")
    asyncio.run(run_runbook_job(runbook))


async def run_runbook_execution_engine(
    user_id,
    runbook,
    dbserver=LanceDBServer(),
    structure_file_payload=None,
    files=None,
    structure_file=None,
    result_id=None,
    is_prev_needed=False,
    document_data=None,
    job_id=None,
    session_id=None,
    progress=None,
):
    from websockets_custom.ws_instance import ws_service, msg_builder_main
    from runbook.utils import send

    msg_builder = msg_builder_main

    # ✅ single flag
    should_emit = bool(job_id and session_id)

    async def emit(msg):
        if should_emit:
            await send(ws_service, msg, user_id)

    conn = connect_to_rds()
    credits = Credits(conn)
    runbook_id = runbook["runbook_id"]
    main_source = runbook["main_source"] if "main_source" in runbook else None
    data_sources = runbook["data_source"] if "data_source" in runbook else None
    reference_sources = (
        runbook["reference_sources"] if "reference_sources" in runbook else None
    )
    refernce_main_source = (
        runbook["reference_main_source"] if "reference_main_source" in runbook else None
    )
    if structure_file:
        structure_file_content = read_json_from_s3(structure_file)
    else:
        structure_file_content = None

    if data_sources and len(data_sources) > 5:
        data_sources = normalize_json_field(data_sources)
    if reference_sources and len(reference_sources) > 5:
        reference_sources = normalize_json_field(reference_sources)

    if not structure_file_payload:
        raw_structure = runbook.get("structure_theme")

        if isinstance(raw_structure, str):
            try:
                structure_file_payload = json.loads(raw_structure)
            except Exception:
                raise ValueError("Invalid JSON in structure_theme")

        elif isinstance(raw_structure, dict):
            structure_file_payload = raw_structure

        else:
            raise ValueError("structure_theme must be str or dict")
    progress = 35

    await emit(
        msg_builder.job_progress(
            job_id,
            session_id,
            "report setup",
            "started creating report",
            progress,
        )
    )

    execution_id = f"exec_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    new_result_id = f"result_{uuid.uuid4().hex[:6]}"
    started_at = int(time.time())
    risk_score = None
    refactor_result = {}
    await dbserver.insert_runbook_result(
        {
            "execution_id": execution_id,
            "result_id": new_result_id,
            "runbook_id": runbook_id,
            "user_id": user_id,
            "status": "running",
            "started_at": started_at,
            "input_mode": runbook.get("input_type"),
        }
    )
    # --------------------------------------------------
    # RENDER RUNBOOK TEMPLATE
    # --------------------------------------------------

    runbook_yaml = render_runbook_yaml(runbook)

    # --------------------------------------------------
    # RESOLVE RUNBOOK INPUT
    # --------------------------------------------------
    print("hello 2")
    try:
        analyze_input = ""
        if "analyze_input" in runbook and runbook["analyze_input"]:
            analyze_input = runbook["analyze_input"]
        else:
            analyze_input = runbook["description"] if "description" in runbook else ""

        file_data = ""
        if not result_id and not document_data:
            file_data = await collect_runbook_inputs(runbook)
        user_analyze_input = analyze_input

        # --------------------------------------------------
        # LANGUAGE + WORD COUNT (same radar logic)
        # --------------------------------------------------
        output_language = "English"
        output_word_count = 500
        if user_analyze_input:

            lang_prompt_key = runbook_yaml["radar"]["language_prompt"]

            lang_prompt = RADAR_TEMPLATE[lang_prompt_key]

            lang_prompt = lang_prompt.replace(
                "{{analyze_input}}", str(user_analyze_input or "")
            )

            # print("lang_prompt: ",lang_prompt)

            result = await get_think_fire_response2_og(
                user_message=lang_prompt,
                user_id=user_id,
                credits=credits,
                total_input_chars=len(lang_prompt),
            )
            # print("LANG RESULT RAW:", result)
            lang_data = json.loads(result)

            output_language = lang_data.get("language", "English")
            output_word_count = lang_data.get("word_count")

            progress = 40
            re_msg = f"generating report in {output_language}"

            await emit(
                msg_builder.job_progress(
                    job_id,
                    session_id,
                    "runbook setup",
                    re_msg,
                    progress,
                )
            )
        # ---------------------------------
        # EMBEDDING GENERATION
        # ---------------------------------
        payload = None
        if main_source == "knowledge" or refernce_main_source == "knowledge":

            embedding = await get_firework_embedding()

            vector = embedding.embed_query(user_analyze_input)

            payload = QueryData(
                user_id=user_id,
                embedding=vector,
                top_k=3,
            )

            await credits.update_ai_credits_redis(
                user_id=user_id,
                credit_type="embedding",
                total_chars=len(user_analyze_input),
                reference_id="embedding_generation",
            )
        # --------------------------------------------------
        # OPTIONAL RADAR DATA SOURCES
        # --------------------------------------------------

        data_checked = []
        reference_RWA = []
        if document_data:
            reference_RWA.append(document_data)

        if main_source and data_sources:

            data_checked = await retreval_from_sources(
                conn,
                dbserver,
                main_source,
                data_sources,
                user_id,
                payload,
            )
            if data_checked and len(data_checked) > 10:
                progress = 45

                await emit(
                    msg_builder.job_progress(
                        job_id,
                        session_id,
                        "report setup",
                        "extracted information from selected Responses & Evidences",
                        progress,
                    )
                )

        if refernce_main_source and not document_data:

            reference_RWA = await retreval_from_sources(
                conn,
                dbserver,
                refernce_main_source,
                reference_sources,
                user_id,
                payload,
            )
            if reference_RWA and len(reference_RWA) > 10:
                if main_source and data_sources:
                    progress = 50
                else:
                    progress = 45

                await emit(
                    msg_builder.job_progress(
                        job_id,
                        session_id,
                        "report setup",
                        "extracted information from selected Governance Framework",
                        progress,
                    )
                )
        # ---------------------------------
        # LAST RESPONSE FETCH
        # ---------------------------------
        last_runbook_response = ""

        # if runbook_id or result_id :
        if is_prev_needed:
            val = await dbserver.get_latest_runbook_result(
                user_id=user_id, runbook_id=runbook_id, result_id=result_id
            )

            if val:
                last_runbook_response = json.dumps(val.get("result"))
                if not output_word_count:
                    output_word_count = (
                        val.get("estimated_word_count")
                        or val.get("document_meta", {}).get("estimated_word_count")
                        or 800  # fallback default
                    )

        # --------------------------------------------------
        # PROMPT SELECTION
        # --------------------------------------------------

        # prompts = runbook_yaml["radar"]["prompts"]
        structure_prompts = runbook_yaml["radar"]["structure_prompts"]

        # if structure_file_payload:
        # Prefer structure-based prompts
        review_temp = (
            RADAR_TEMPLATE.get(structure_prompts.get("review"))
            or RADAR_TEMPLATE.get(structure_prompts.get("analysis"))
            or RADAR_TEMPLATE.get(structure_prompts.get("recommendation"))
        )

        # -----------------------------
        # BLOCK-BY-BLOCK EXECUTION
        # -----------------------------
        final_blocks = []

        # -----------------------------
        # FORCE NORMALIZATION (FINAL FIX)
        # -----------------------------
        raw_structure = structure_file_payload or runbook.get("structure_theme")

        if isinstance(raw_structure, str):
            try:
                structure_file_payload = json.loads(raw_structure)
            except Exception as e:
                raise ValueError(f"Invalid JSON structure_theme: {e}")

        elif isinstance(raw_structure, dict):
            structure_file_payload = raw_structure

        else:
            raise ValueError("structure_theme must be str or dict")

        # -----------------------------
        # SAFETY CHECK (IMPORTANT)
        # -----------------------------
        if "blocks" not in structure_file_payload:
            raise ValueError("structure_file_payload missing 'blocks'")

        # print("FINAL TYPE:", type(structure_file_payload))
        # print("BLOCK COUNT:", len(structure_file_payload["blocks"]))
        # print("len file data", len(file_data))
        # print("data checked", len(data_checked))
        # print("reference data", len(reference_RWA))
        # print("last response data", len(last_runbook_response))

        progress = 55

        await emit(
            msg_builder.job_progress(
                job_id,
                session_id,
                "report generation",
                "starting to generate report",
                progress,
            )
        )
        if is_prev_needed:
            base_prompt = (
                review_temp.replace("{{analyze_input}}", analyze_input)
                .replace("{{file_data}}", file_data)
                .replace("{{structure_file_data}}", json.dumps(structure_file_payload))
                .replace("{{data_sources}}", json.dumps(data_checked))
                .replace("{{reference_sources}}", json.dumps(reference_RWA))
                .replace("{{output_language}}", output_language)
                .replace("{{last_radar_response}}", json.dumps(last_runbook_response))
                .replace("{{requested_word_count}}", str(output_word_count))
            )

            # =========================================================
            # INITIAL GENERATION
            # =========================================================
            result = await get_think_bedrok_response(
                user_message=base_prompt,
                user_id=user_id,
                credits=credits,
                total_input_chars=len(base_prompt),
                language=output_language,
                words_count=output_word_count,
            )
            await emit(
                msg_builder.job_progress(
                    job_id,
                    session_id,
                    "report generation",
                    "generated content for report",
                    60,
                )
            )

            merged_report = merge_runbook_chunks_deterministic(result)
            await emit(
                msg_builder.job_progress(
                    job_id,
                    session_id,
                    "report generation",
                    "merging content for report",
                    70,
                )
            )
            merged_result = _safe_json_parse_full(merged_report)
            # print("mergeed result", type(merged_result), merged_result)
        else:

            for idx, block in enumerate(structure_file_payload["blocks"]):

                block_payload = {"blocks": [block]}  # isolate single block

                block_prompt = (
                    review_temp.replace(
                        "{{structure_file_data}}", json.dumps(block_payload)
                    )
                    .replace("{{analyze_input}}", analyze_input)
                    .replace("{{data_sources}}", json.dumps(data_checked))
                    .replace("{{file_data}}", file_data)
                    .replace("{{reference_sources}}", json.dumps(reference_RWA))
                    .replace("{{last_radar_response}}", last_runbook_response)
                    .replace("{{requested_word_count}}", "200")
                )

                result = await get_think_bedrok_response(
                    user_message=block_prompt,
                    user_id=user_id,
                    credits=credits,
                    total_input_chars=len(block_prompt),
                    language="English",
                    words_count=200,
                )

                parsed = _safe_json_parse(result)

                # -----------------------------
                # STRICT BLOCK EXTRACTION
                # -----------------------------
                if not parsed:
                    raise ValueError(
                        f"LLM returned invalid JSON at block {idx}: RAW parse failed"
                    )

                if isinstance(parsed, dict) and "blocks" in parsed:
                    final_blocks.append(parsed["blocks"][0])
                elif isinstance(parsed, dict) and "block_id" in parsed:
                    final_blocks.append(parsed)
                else:
                    raise ValueError(f"Unexpected schema at block {idx}: {parsed}")
            # -----------------------------
            # FINAL MERGE
            # -----------------------------
            await emit(
                msg_builder.job_progress(
                    job_id,
                    session_id,
                    "report generation",
                    "generated content for report",
                    60,
                )
            )
            merged_result = {
                "document_meta": parsed.get("document_meta", {}),
                "estimated_word_count": sum(
                    b.get("word_count", 0) for b in final_blocks
                ),
                "structure_rationale": "Block-by-block deterministic execution",
                "blocks": final_blocks,
            }
            await emit(
                msg_builder.job_progress(
                    job_id,
                    session_id,
                    "report generation",
                    "merging content for report",
                    70,
                )
            )
        # print(" hello 6")
        if merged_result:
            progress = 85

            await emit(
                msg_builder.job_progress(
                    job_id,
                    session_id,
                    "report generation",
                    "report generated and now trying to make risk analysis",
                    progress,
                )
            )
        newdata_risk = ""
        if structure_file_content:
            riskbaseprompt = """
                You are a risk data compressor.

                INPUT:
                {{structure_file_content}}

                TASK:
                Extract ONLY critical fields needed for risk scoring.

                STRICT RULES:
                - Output must be under 1500 tokens
                - Remove all descriptions, explanations, and duplicates
                - Keep only:
                - metrics
                - scores
                - counts
                - risk indicators
                - important flags
                - Convert verbose text → short key-value pairs
                - Ignore UI structure, headings, formatting

                OUTPUT FORMAT (STRICT JSON):
                {
                "key_metrics": {},
                "risk_indicators": [],
                "scores": {},
                "flags": []
                }
                """
            newdata_risk = await get_think_fire_response2_og(
                user_message=riskbaseprompt,
                user_id=user_id,
                credits=credits,
                total_input_chars=len(riskbaseprompt),
            )
            structure_file_content = newdata_risk

        risk_prompt = (
            RADAR_TEMPLATE["nist_risk_score_prompt"]
            .replace("{{analysis_result}}", json.dumps(merged_result))
            .replace(
                "{{report_data}}",
                json.dumps(structure_file_content) if structure_file_content else "",
            )
        )
        if merged_result:
            progress = 80

            await emit(
                msg_builder.job_progress(
                    job_id,
                    session_id,
                    "risk analysis",
                    "generating risk analysis",
                    progress,
                )
            )

        risk_result = await get_think_fire_response2_og(
            user_message=risk_prompt,
            user_id=user_id,
            credits=credits,
            total_input_chars=len(risk_prompt),
        )

        risk_data = _safe_json_parse(risk_result)

        merged_result["risk_analysis"] = risk_data
        merged_result["risk_score"] = risk_data.get("final_risk_score", 0)
        await emit(
            msg_builder.job_progress(
                job_id,
                session_id,
                "risk analysis",
                "generated risk analysis and saving it to report",
                95,
            )
        )

        await dbserver.insert_runbook_result(
            {
                "execution_id": execution_id,
                "result_id": new_result_id,
                "runbook_id": runbook_id,
                "user_id": user_id,
                "status": "completed",
                "risk_score": merged_result["risk_score"],
                "result": merged_result,
                "started_at": int(time.time()),
                "ended_at": int(time.time()),
            }
        )

        return merged_result

    except Exception as e:
        print("runbook error:", e)
        await dbserver.insert_runbook_result(
            {
                "execution_id": execution_id,
                "result_id": new_result_id,
                "runbook_id": runbook_id,
                "user_id": user_id,
                "status": "failed",
                "result": {},
                "started_at": int(time.time()),
                "ended_at": int(time.time()),
            }
        )
        return None


# # =========================================================
# # 🔧 HELPER: MERGE FIXED BLOCKS
# # =========================================================
# def merge_fixed_blocks(original, fixed_blocks):
#     fixed_map = {b["block_id"]: b for b in fixed_blocks}

#     for i, block in enumerate(original.get("blocks", [])):
#         bid = block.get("block_id")
#         if bid in fixed_map:
#             original["blocks"][i] = fixed_map[bid]

#     return original


# # =========================================================
# # 🔧 HELPER: EXTRACT BLOCKS TO FIX
# # =========================================================
# def get_blocks_by_ids(report, block_ids):
#     return [b for b in report.get("blocks", []) if b.get("block_id") in block_ids]


# # =========================================================
# # 🔧 HELPER: TYPE LEAK DETECTION (FAST FAIL)
# # =========================================================
# def detect_type_leakage(report):
#     issues = []

#     TYPE_MAP = {
#         "narrative": ["paragraph"],
#         "list": ["bullet"],
#         "table": ["table"],
#         "matrix": ["matrix"],
#     }

#     for block in report.get("blocks", []):
#         block_id = block.get("block_id")
#         block_type = block.get("block_type")
#         micro_blocks = block.get("micro_blocks", [])

#         # ❌ 1. Missing micro_blocks
#         if not micro_blocks:
#             issues.append(
#                 {
#                     "block_id": block_id,
#                     "issue": "Missing micro_blocks",
#                     "fix_instruction": "Create micro_block and move content into it",
#                 }
#             )
#             continue

#         expected_types = TYPE_MAP.get(block_type, [])

#         for micro in micro_blocks:
#             micro_id = micro.get("micro_id")
#             ctype = micro.get("content_type")
#             html = micro.get("html")
#             data = micro.get("data")

#             # ❌ 2. TYPE MISMATCH
#             if ctype not in expected_types:
#                 issues.append(
#                     {
#                         "block_id": block_id,
#                         "micro_id": micro_id,
#                         "issue": f"Type mismatch: block_type={block_type}, content_type={ctype}",
#                         "fix_instruction": f"Convert to {expected_types[0]}",
#                     }
#                 )

#             # ❌ 3. TYPE LEAKAGE (html in structured)
#             if ctype in ["table", "matrix", "bullet"] and html:
#                 issues.append(
#                     {
#                         "block_id": block_id,
#                         "micro_id": micro_id,
#                         "issue": "Structured type contains html",
#                         "fix_instruction": "Remove html and use data field only",
#                     }
#                 )

#             # ❌ 4. EMPTY DATA VALIDATION
#             if ctype == "table" and not data.get("rows"):
#                 issues.append(
#                     {
#                         "block_id": block_id,
#                         "micro_id": micro_id,
#                         "issue": "Table missing rows",
#                         "fix_instruction": "Provide rows matching schema",
#                     }
#                 )

#             if ctype == "bullet" and not data.get("items"):
#                 issues.append(
#                     {
#                         "block_id": block_id,
#                         "micro_id": micro_id,
#                         "issue": "List missing items",
#                         "fix_instruction": "Provide items array",
#                     }
#                 )

#             if ctype == "matrix" and not data.get("matrix"):
#                 issues.append(
#                     {
#                         "block_id": block_id,
#                         "micro_id": micro_id,
#                         "issue": "Matrix missing data",
#                         "fix_instruction": "Provide full matrix",
#                     }
#                 )

#             # ❌ 5. NARRATIVE RULE
#             if ctype == "paragraph":
#                 if not html:
#                     issues.append(
#                         {
#                             "block_id": block_id,
#                             "micro_id": micro_id,
#                             "issue": "Narrative missing html",
#                             "fix_instruction": "Provide html content",
#                         }
#                     )
#                 if data:
#                     issues.append(
#                         {
#                             "block_id": block_id,
#                             "micro_id": micro_id,
#                             "issue": "Narrative should not have data",
#                             "fix_instruction": "Set data to {}",
#                         }
#                     )

#     return issues


# # =========================================================
# # 🚀 MAIN ENGINE
# # =========================================================
# async def run_runbook_execution_engine(
#     conn,
#     credits,
#     user_id,
#     runbook,
#     dbserver=LanceDBServer(),
#     structure_file_payload=None,
#     files=None,
#     structure_file=None,
#     result_id=None,
#     is_prev_needed=False,
# ):

#     runbook_id = runbook["runbook_id"]

#     main_source = runbook.get("main_source")
#     data_sources = runbook.get("data_source")

#     reference_sources = runbook.get("reference_sources")
#     refernce_main_source = runbook.get("reference_main_source")

#     if structure_file:
#         structure_file_content = read_json_from_s3(structure_file)
#     else:
#         structure_file_content = None

#     if not structure_file_payload:
#         structure_file_payload = runbook["structure_theme"]

#     execution_id = f"exec_{int(time.time())}_{uuid.uuid4().hex[:6]}"
#     new_result_id = f"result_{uuid.uuid4().hex[:6]}"
#     started_at = int(time.time())

#     refactor_result = {}
#     risk_score = None

#     await dbserver.insert_runbook_result(
#         {
#             "execution_id": execution_id,
#             "result_id": new_result_id,
#             "runbook_id": runbook_id,
#             "user_id": user_id,
#             "status": "running",
#             "started_at": started_at,
#             "input_mode": runbook.get("input_type"),
#         }
#     )

#     try:
#         # =========================================================
#         # INPUT PREP
#         # =========================================================
#         runbook_yaml = render_runbook_yaml(runbook)

#         analyze_input = runbook.get("analyze_input") or runbook.get("description", "")

#         file_data = ""
#         if not result_id:
#             file_data = await collect_runbook_inputs(runbook)

#         # =========================================================
#         # LANGUAGE DETECTION
#         # =========================================================
#         output_language = "English"
#         output_word_count = 800

#         if analyze_input:
#             lang_prompt = RADAR_TEMPLATE[
#                 runbook_yaml["radar"]["language_prompt"]
#             ].replace("{{analyze_input}}", analyze_input)

#             result = await get_think_fire_response2_og(
#                 user_message=lang_prompt,
#                 user_id=user_id,
#                 credits=credits,
#                 total_input_chars=len(lang_prompt),
#             )

#             lang_data = json.loads(result)
#             output_language = lang_data.get("language", "English")
#             wc = lang_data.get("word_count")

#             if isinstance(wc, int) and wc > 0:
#                 output_word_count = wc
#             else:
#                 output_word_count = 800

#         # =========================================================
#         # DATA RETRIEVAL
#         # =========================================================
#         data_checked = []
#         reference_RWA = []

#         payload = None

#         if main_source == "knowledge" or refernce_main_source == "knowledge":
#             embedding = await get_firework_embedding()
#             vector = embedding.embed_query(analyze_input)

#             payload = QueryData(user_id=user_id, embedding=vector, top_k=3)

#         if main_source:
#             data_checked = await retreval_from_sources(
#                 conn, dbserver, main_source, data_sources, user_id, payload
#             )

#         if refernce_main_source:
#             reference_RWA = await retreval_from_sources(
#                 conn,
#                 dbserver,
#                 refernce_main_source,
#                 reference_sources,
#                 user_id,
#                 payload,
#             )

#         # =========================================================
#         # BASE PROMPT
#         # =========================================================
#         structure_prompts = runbook_yaml["radar"]["structure_prompts"]

#         review_temp = (
#             RADAR_TEMPLATE.get(structure_prompts.get("review"))
#             or RADAR_TEMPLATE.get(structure_prompts.get("analysis"))
#             or RADAR_TEMPLATE.get(structure_prompts.get("recommendation"))
#         )

#         base_prompt = (
#             review_temp.replace("{{analyze_input}}", analyze_input)
#             .replace("{{file_data}}", file_data)
#             .replace("{{structure_file_data}}", json.dumps(structure_file_payload))
#             .replace("{{data_sources}}", json.dumps(data_checked))
#             .replace("{{reference_sources}}", json.dumps(reference_RWA))
#             .replace("{{output_language}}", output_language)
#             .replace("{{requested_word_count}}", str(output_word_count))
#         )

#         # =========================================================
#         # INITIAL GENERATION
#         # =========================================================
#         result = await get_think_bedrok_response(
#             user_message=base_prompt,
#             user_id=user_id,
#             credits=credits,
#             total_input_chars=len(base_prompt),
#             language=output_language,
#             words_count=output_word_count,
#         )

#         merged_report = merge_runbook_chunks_deterministic(result)
#         refactor_result = _safe_json_parse(merged_report)

#         # =========================================================
#         # 🔁 VALIDATION LOOP (BLOCK LEVEL)
#         # =========================================================
#         MAX_RETRIES = 3
#         previous_error_set = set()

#         for attempt in range(MAX_RETRIES):

#             print(f"🔁 Validation Attempt {attempt+1}")

#             # 🚨 FAST TYPE CHECK
#             leakage_errors = detect_type_leakage(refactor_result)

#             if leakage_errors:
#                 vrefactor = {
#                     "is_report_ok": False,
#                     "errors": leakage_errors,
#                     "affected_blocks": list(set(e["block_id"] for e in leakage_errors)),
#                 }
#             else:
#                 verifier_prompt = (
#                     RADAR_TEMPLATE["Report_verify_prompt"]
#                     .replace("{{report_generated}}", json.dumps(refactor_result))
#                     .replace(
#                         "{{structure_file_data}}", json.dumps(structure_file_payload)
#                     )
#                 )

#                 v_result = await get_think_fire_response2_og(
#                     user_message=verifier_prompt,
#                     user_id=user_id,
#                     credits=credits,
#                     total_input_chars=len(verifier_prompt),
#                 )

#                 vrefactor = _safe_json_parse(v_result)

#             if not vrefactor:
#                 break

#             if vrefactor.get("is_report_ok"):
#                 print("✅ VALID REPORT")
#                 break

#             errors = vrefactor.get("errors", [])
#             affected_blocks = vrefactor.get("affected_blocks", [])

#             if not errors or not affected_blocks:
#                 break

#             # 🧠 EARLY STOP CHECK
#             current_error_set = set(e["issue"] for e in errors)
#             if current_error_set == previous_error_set:
#                 print("⚠️ No progress, stopping")
#                 break

#             previous_error_set = current_error_set

#             # =========================================================
#             # 🔧 PARTIAL BLOCK FIX
#             # =========================================================
#             blocks_to_fix = get_blocks_by_ids(refactor_result, affected_blocks)

#             block_fix_prompt = (
#                 RADAR_TEMPLATE["Report_block_fixer_prompt"]
#                 .replace("{{blocks_to_fix}}", json.dumps(blocks_to_fix))
#                 .replace("{{errors}}", json.dumps(errors))
#                 .replace("{{structure_file_data}}", json.dumps(structure_file_payload))
#             )

#             fix_result = await get_think_bedrok_response(
#                 user_message=block_fix_prompt,
#                 user_id=user_id,
#                 credits=credits,
#                 total_input_chars=len(block_fix_prompt),
#                 language=output_language,
#                 words_count=output_word_count,
#             )

#             parsed_fix = _safe_json_parse(fix_result)

#             if not parsed_fix or "fixed_blocks" not in parsed_fix:
#                 break

#             refactor_result = merge_fixed_blocks(
#                 refactor_result, parsed_fix["fixed_blocks"]
#             )

#         # =========================================================
#         # RISK SCORE
#         # =========================================================
#         risk_prompt = RADAR_TEMPLATE["nist_risk_score_prompt"].replace(
#             "{{analysis_result}}", json.dumps(refactor_result)
#         )

#         risk_llm_result = await get_think_fire_response2_og(
#             user_message=risk_prompt,
#             user_id=user_id,
#             credits=credits,
#             total_input_chars=len(risk_prompt),
#         )

#         risk_data = _safe_json_parse(risk_llm_result)
#         risk_score = risk_data.get("final_risk_score", 0)

#         refactor_result["risk_analysis"] = risk_data
#         refactor_result["risk_score"] = risk_score

#         # =========================================================
#         # STORE RESULT
#         # =========================================================
#         await dbserver.insert_runbook_result(
#             {
#                 "execution_id": execution_id,
#                 "result_id": new_result_id,
#                 "runbook_id": runbook_id,
#                 "user_id": user_id,
#                 "status": "completed",
#                 "risk_score": risk_score,
#                 "result": refactor_result,
#                 "started_at": started_at,
#                 "ended_at": int(time.time()),
#             }
#         )

#         return refactor_result

#     except Exception as e:
#         print("❌ ERROR:", e)
#         print(traceback.format_exc())

#         await dbserver.insert_runbook_result(
#             {
#                 "execution_id": execution_id,
#                 "result_id": new_result_id,
#                 "runbook_id": runbook_id,
#                 "user_id": user_id,
#                 "status": "failed",
#                 "result": {},
#                 "started_at": started_at,
#                 "ended_at": int(time.time()),
#             }
#         )

#         return {}


async def trigger_runbooks_for_api_response(user_id, app_id, endpoint_id, record):
    try:
        dbserver = LanceDBServer()

        print("🚀 trigger_runbooks_for_api_response START")

        # ✅ 1. GET TEMPLATE RUNBOOK
        runbook = await dbserver.get_runbooks_by_endpoint(
            user_id=user_id, app_id=app_id, endpoint_id=endpoint_id
        )

        if not runbook:
            print("⚠️ No runbook found")
            return

        # ✅ safety
        if isinstance(runbook, str):
            runbook = json.loads(runbook)

        if not isinstance(runbook, dict):
            print("❌ Invalid runbook format")
            return

        print(f"Using runbook:{runbook.get('runbook_id')} - {runbook.get('name')}")

        # ✅ 2. PREPARE EXECUTION INPUT
        runtime_input = record.get("original") or record.get("text")
        # print("api trig 1")
        if isinstance(runtime_input, dict):
            runtime_input = json.dumps(runtime_input)

        runbook["runtime_input"] = runtime_input
        # runbook["execution_id"] = f"exec_{int(time.time())}"
        runbook["app_id"] = app_id
        # reconstruct data_sources_full
        # if not runbook.get("data_sources_full"):
        #     runbook["data_sources_full"] = reconstruct_sources(
        #         runbook.get("data_sources", [])
        #     )

        # # reconstruct reference_sources_full
        # if not runbook.get("reference_sources_full"):
        #     runbook["reference_sources_full"] = reconstruct_sources(
        #         runbook.get("reference_sources", [])
        #     )

        # runbook["main_source"] = "app"
        # runbook["reference_main_source"] = "knowledge"
        structure_file = None

        files = runbook.get("files")

        # 🔥 FIX: normalize files if it's a string
        if isinstance(files, str):
            try:
                files = json.loads(files)
            except Exception as e:
                print("Failed to parse files:", e)
                files = {}

        # now safely use it
        if isinstance(files, dict):
            structure_file = files.get("structure_file")
        structure_file_payload = runbook["structure_theme"]

        # print("📥 INPUT:", runtime_input)

        # ✅ 3. EXECUTE (THIS WILL CREATE RESULT ENTRY)
        await run_runbook_execution_engine(
            dbserver=dbserver,
            user_id=user_id,
            runbook=runbook,
            structure_file=structure_file,
            structure_file_payload=structure_file_payload,
        )
        return {"status": "success"}

    except Exception as e:
        print("❌ Error in trigger_runbooks:", str(e))
        raise


def upload_file_object(file, user_id):
    try:
        temp_path = f"/tmp/{uuid.uuid4()}_{file.filename}"

        # save file locally
        file.save(temp_path)

        # upload to S3
        result = upload_any_file(file_path=temp_path, user_id=user_id, type="runbook")

        # cleanup
        os.remove(temp_path)

        return result

    except Exception as e:
        return {"status": "error", "message": str(e)}


def fetch_cloudwatch_logs(log_group, log_stream=None, region="ca-central-1", limit=100):
    try:
        client = boto3.client("logs", region_name=region)

        kwargs = {
            "logGroupName": log_group,
            "limit": limit,
        }

        if log_stream:
            kwargs["logStreamNames"] = [log_stream]

        response = client.filter_log_events(**kwargs)

        logs = []
        for event in response.get("events", []):
            logs.append({"timestamp": event["timestamp"], "message": event["message"]})

        return {"status": "success", "logs": logs}

    except Exception as e:
        return {"status": "error", "error": str(e)}


def parse_cloudwatch_url(url: str):
    try:
        parsed = urlparse(url)

        # Extract region
        query_params = parse_qs(parsed.query)
        region = query_params.get("region", ["ca-central-1"])[0]

        fragment = parsed.fragment  # everything after #

        # Decode twice (important for CloudWatch URLs)
        decoded = unquote(unquote(fragment))

        # Extract log group
        log_group_match = re.search(r"log-group/([^/]+)", decoded)
        log_group = unquote(log_group_match.group(1)) if log_group_match else None

        # Extract log stream
        log_stream_match = re.search(r"log-events/(.+)", decoded)
        log_stream = unquote(log_stream_match.group(1)) if log_stream_match else None

        return {
            "status": "success",
            "log_group": log_group,
            "log_stream": log_stream,
            "region": region,
        }

    except Exception as e:
        return {"status": "error", "error": str(e)}


def reconstruct_sources(filenames):
    result = []

    for item in filenames:
        if not item or ":" not in item:
            continue

        ftype, value = item.split(":", 1)

        if ftype == "scrape":
            result.append({"type": "scrape", "url": value})

        elif ftype in ["docs", "voice", "aud"]:
            result.append({"type": ftype, "filename": value})

    return {"filenames": result}


async def trigger_runbook_from_playbook(playbook_id, user_id, runbook_id):
    dbserver = LanceDBServer()
    print("inside trigger playbook")
    print("details", playbook_id, runbook_id, user_id)

    runbook = await dbserver.get_runbook_by_id(user_id=user_id, runbook_id=runbook_id)
    # print(type(runbook), len(runbook), runbook)
    if isinstance(runbook, list):
        runbook = runbook[0] if runbook else None

    if isinstance(runbook, str):
        runbook = json.loads(runbook)

    print(f"Using runbook:{runbook.get('runbook_id')} - {runbook.get('name')}")
    # print("out of range 2")
    structure_file = None

    files = runbook.get("files")

    # 🔥 FIX: normalize files if it's a string
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except Exception as e:
            print("Failed to parse files:", e)
            files = {}

    # now safely use it
    if isinstance(files, dict):
        structure_file = files.get("structure_file")

    raw_structure = runbook.get("structure_theme")
    structure_file_payload = None
    if isinstance(raw_structure, str):
        try:
            structure_file_payload = json.loads(raw_structure)
        except Exception:
            raise ValueError("Invalid JSON in structure_theme")

    elif isinstance(raw_structure, dict):
        structure_file_payload = raw_structure
    instruction_data = await get_playbook_instruction(user_id, playbook_id)
    print("DEBUG KEYS:", instruction_data.keys())
    # runbook["runtime_input"] = json.dumps(runtime_input.get("chat", []))

    print("runtime_input type:", type(instruction_data))
    print("🚀 BEFORE extraction")
    questions = await extract_qna_from_instruction(instruction_data)
    print("✅ AFTER extraction")

    print(f"Total questions: {len(questions)}")
    print("Sample question:", questions[0] if questions else "None")

    print(runbook.get("reference_sources"))
    document_data = None
    if runbook.get("reference_sources"):
        analyzed_results = await analyze_questions_with_references(
            questions,
            runbook.get("reference_sources"),
            runbook.get("reference_main_source"),
            user_id,
            runbook,
        )
        if not analyzed_results:
            print("⚠️ No analysis results generated")

        print("📦 analyzed_results type:", type(analyzed_results))
        print(
            "📦 first item type:",
            type(analyzed_results[0]) if analyzed_results else "empty",
        )
        # print("📦 sample item:", analyzed_results[0] if analyzed_results else "None")
        if analyzed_results:
            merged = await merge_document_data(analyzed_results, instruction_data)
            # runbook["runtime_input"] = json.dumps(merged["chat"])
            document_data = merged.get("chat")
            print("document_data : ", len(document_data))
            # print("🔍 After merge sample:", merged[0])
    else:
        runbook["runtime_input"] = json.dumps(instruction_data.get("chat", []))

    # print("final: ", str(runbook.get("runtime_input"))[:100])
    print("executing runbook playbook")
    await run_runbook_execution_engine(
        dbserver=dbserver,
        user_id=user_id,
        runbook=runbook,
        structure_file=structure_file,
        structure_file_payload=structure_file_payload,
        document_data=document_data,
    )
    # ws_service.emit(user_id=user_id,message=)


async def extract_qna_from_instruction(instruction_data):
    result = []

    try:
        print("inside extract qna---")

        # ✅ Handle string input
        if isinstance(instruction_data, str):
            instruction_data = json.loads(instruction_data)

        print("DEBUG KEYS:", instruction_data.keys())

        chats = instruction_data.get("chat", [])
        print(f"DEBUG: total chats = {len(chats)}")

        for chat in chats:
            outputs = chat.get("output", [])
            print(f"DEBUG: outputs count = {len(outputs)}")

            for item in outputs:
                if not isinstance(item, dict):
                    # print("⚠️ Skipping invalid item:", item)
                    continue

                qid = item.get("id")
                question = item.get("question")
                comment = item.get("comment")

                # 🔥 Normalize user answer
                options = item.get("options", {}) or {}
                raw_answer = item.get("user_answer")
                question_type = ""

                if isinstance(raw_answer, str) and raw_answer in options:
                    answer = options.get(raw_answer)
                    question_type = "MCQ"
                else:
                    answer = raw_answer
                    question_type = "DESCRIPTIVE"

                if not qid or not question:
                    continue

                result.append(
                    {
                        "id": qid,
                        "question": question,
                        "user_answer": answer,
                        "options": options,
                        "question_type": question_type,
                        "comment": comment,
                        "section": item.get("section"),
                    }
                )

        print(f"✅ Extracted questions: {len(result)}")

    except Exception as e:
        print(f"❌ Error extracting QnA: {e}")

    return result


import json


async def merge_document_data(analyzed_results, instruction_data):

    # 🔥 Ensure instruction_data is dict
    if isinstance(instruction_data, str):
        instruction_data = json.loads(instruction_data)

    result_map = {
        item.get("id"): item
        for item in analyzed_results
        if isinstance(item, dict) and item.get("id")
    }

    chats = instruction_data.get("chat", [])

    for chat in chats:
        outputs = chat.get("output", [])

        # 🔥 Normalize outputs list
        clean_outputs = []
        for o in outputs:
            if isinstance(o, str):
                try:
                    o = json.loads(o)  # ✅ FIX: convert string → dict
                except Exception:
                    # print("⚠️ Skipping invalid output:", o)
                    continue
            clean_outputs.append(o)

        chat["output"] = clean_outputs  # ✅ overwrite with cleaned data

        for item in clean_outputs:
            if not isinstance(item, dict):
                # print("⚠️ Still invalid item:", item)
                continue

            qid = item.get("id")

            if not qid or qid not in result_map:
                continue

            result = result_map[qid]

            item["document_data"] = result.get("document_data", {})
            item["evaluation_data"] = result.get("evaluation_data", {})

    return instruction_data


async def playbook_runbook_execution(user_id, runbook):

    print("Inside executing playbook runbook : ", runbook["runbook_id"])
    await run_runbook_execution_engine(user_id=user_id, runbook=runbook)


async def create_runbook_for_playbook(playbook_id, user_id):
    playbook_result = await get_playbook_instruction(
        user_id=user_id, filename=playbook_id
    )

    workflow = playbook_result.get("workflow", {})
    name = workflow.get("name", "")
    description = workflow.get("description", "")
    # building runbook data for playbook
    runbook_data = {
        "runbook_id": str(uuid.uuid4()),
        "user_id": user_id,
        "name": name,
        "description": description,
        "runbook_type": "playbook",
        "schedule": "",  # cron expression
        "input_type": "playbook",
        "playbook_id": playbook_id,
        "api_endpoint": "",
        "log_source": "",
        "files": [],
        "links": [],
        "data_sources": [],
        "reference_sources": [],
        "created_at": int(time.time()),
    }
    print("Creating new runbook for playbook : ", playbook_id)
    # Insert runbook details
    result = await dbserver.insert_runbook(runbook_data)

    runbook_data["main_source"] = ""
    runbook_data["reference_main_source"] = ""

    return runbook_data


import json
import re


def safe_json_parsestruy(data):
    # ✅ Case 1: Already parsed
    if isinstance(data, (list, dict)):
        return data

    # ✅ Case 2: Must be string-like
    if not isinstance(data, (str, bytes, bytearray)):
        raise ValueError(f"Unexpected type: {type(data)}")

    text = data.decode() if isinstance(data, (bytes, bytearray)) else data

    # ✅ Extract JSON using regex (handles ```json blocks or extra text)
    match = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
    if match:
        text = match.group(1)

    # ✅ Final parse
    return json.loads(text)


async def structure_payload_generation(
    user_id,
    analyze_input,
    structure_file,
    emit=None,
    session_id=None,
    job_id=None,
    mprogress=None,
):
    from websockets_custom.ws_instance import ws_service, msg_builder_main

    msg_builder = msg_builder_main
    try:
        structure_file_payload = []
        STR_LINKS = []
        credits = Credits()

        # =============================
        # 🔹 STEP 1: FILE PROCESSING
        # =============================
        if not mprogress:
            if emit:
                await emit(
                    msg_builder.job_progress(
                        job_id=job_id,
                        session_id=session_id,
                        stage="structure_processing",
                        message="📂 Processing structure files...",
                        progress=10,
                    )
                )

        process_file_payloads(
            user_id=user_id,
            files=(
                structure_file if isinstance(structure_file, list) else [structure_file]
            ),
            inp_links=STR_LINKS,
            extracted_payload=structure_file_payload,
        )

        # =============================
        # 🔹 STEP 2: LANGUAGE DETECTION
        # =============================
        if not mprogress:
            if emit:
                await emit(
                    msg_builder.job_progress(
                        job_id=job_id,
                        session_id=session_id,
                        stage="language_detection",
                        message="🌐 Detecting language & word count...",
                        progress=25,
                    )
                )

        lang_prompt = RADAR_TEMPLATE["language_wordcount_extractor"].replace(
            "{{analyze_input}}", str(analyze_input or "")
        )

        result = await get_think_fire_response2_og(
            user_message=lang_prompt,
            user_id=user_id,
            credits=credits,
            total_input_chars=len(lang_prompt),
        )

        lang_data = json.loads(result)
        output_language = lang_data.get("language", "English")
        output_word_count = lang_data.get("word_count", "800")
        if not output_word_count:
            output_word_count = "800"
        if not mprogress:
            if emit:
                await emit(
                    msg_builder.job_progress(
                        job_id=job_id,
                        session_id=session_id,
                        stage="language_detection",
                        message=f"Using {output_language} as language for report",
                        progress=25,
                    )
                )

        # =============================
        # 🔹 STEP 3: STRUCTURE GENERATION
        # =============================
        if not mprogress:
            if emit:
                await emit(
                    msg_builder.job_progress(
                        job_id=job_id,
                        session_id=session_id,
                        stage="structure_generation",
                        message="🧠 Generating structure...",
                        progress=60,
                    )
                )

        structure_prompt = RADAR_TEMPLATE["structure_prompt_template"]

        structure_prompt = (
            structure_prompt.replace(
                "{{document_file_data}}", json.dumps(structure_file_payload)
            )
            .replace("{{file_links}}", json.dumps(STR_LINKS))
            .replace("{{user_original_prompt_or_context}}", analyze_input or "")
            .replace("{{output_language}}", output_language)
            .replace("{{output_word_count}}", output_word_count)
        )

        base_chars = len(structure_prompt)

        for img in STR_LINKS:
            base_chars -= len(img)
            base_chars += image_credit_cost(img)

        reresult = await get_think_bedrok_response(
            user_message=structure_prompt,
            user_id=user_id,
            credits=credits,
            total_input_chars=base_chars,
        )

        structure_file_payload = safe_json_parsestruy(reresult)

        def get_block_hash(block):
            return hashlib.md5(json.dumps(block, sort_keys=True).encode()).hexdigest()

        # ✅ Normalize + Merge blocks WITHOUT duplication
        payload = structure_file_payload

        merged_blocks = []
        seen_hashes = set()
        meta = None

        # 🔥 Handle BOTH cases properly
        if isinstance(payload, list):
            docs = payload

        elif isinstance(payload, dict):
            # if already final structure, just return
            if "blocks" in payload:
                docs = [payload]
            else:
                docs = payload.get("data", {}).get("data", [])

        else:
            docs = []

        for doc in docs:
            blocks = doc.get("blocks", [])

            for b in blocks:
                h = get_block_hash(b)

                # ✅ Skip duplicates safely
                if h in seen_hashes:
                    continue

                seen_hashes.add(h)
                merged_blocks.append(b)

            # take first meta
            if not meta:
                meta = doc.get("document_meta")

        structure_file_payload = {
            "blocks": merged_blocks,
            "document_meta": meta,
            "success": True,
        }

        # =============================
        # 🔹 STEP 4: DONE
        # =============================
        if not mprogress:
            if emit:
                await emit(
                    msg_builder.job_progress(
                        job_id=job_id,
                        session_id=session_id,
                        stage="structure_done",
                        message="✅ Structure ready",
                        progress=50,
                    )
                )

        logger.info("✅ STRUCTURE GENERATED")
        return structure_file_payload

    except Exception:
        logger.exception("❌ STRUCTURE GENERATION FAILED")

        if emit:
            await emit(
                msg_builder.job_error(
                    job_id=job_id,
                    session_id=session_id,
                    message="Structure generation failed",
                )
            )

        raise


async def Modify_default_structure(user_id, analyze_input, default_structure):
    try:
        default_structure_payload = []

        prompt = RUNBOOK_TEMPLATE["default_structure_modification__prompt"]

        prompt = prompt.replace("{{analyze_input}}", analyze_input).replace(
            "{{default_structure}}", json.dumps(default_structure, indent=2)
        )

        base_char = len(prompt)

        result = await get_think_fire_response2_og(
            user_message=prompt,
            user_id=user_id,
            credits=credits,
            total_input_chars=base_char,
        )
        default_structure_payload = json.loads(result)
        logger.info("✅DEFAULT STRUCTURE MODIFIED")

        return default_structure_payload
    except Exception as e:
        logger.exception("❌ Failed to modify default structure")
        raise e


async def pick_best_source_for_workflow(instruction_text, source_contexts, top_k=2):

    embedding = await get_firework_embedding()
    wf_vec = embedding.embed_query(instruction_text[:3000])  # truncate

    # best_source = None
    # best_score = -1

    selected = []

    for source, ctx in source_contexts.items():
        text = " ".join([c.get("data", "") for c in ctx])
        src_vec = embedding.embed_query(text[:3000])

        score = cosine_similarity(wf_vec, src_vec)

        selected.append((source, score))
        # if score > best_score:
        #     best_score = score
        #     best_source = source

    selected.sort(key=lambda x: x[1], reverse=True)
    return selected[:top_k]


import math


def cosine_similarity(vec1, vec2):
    if not vec1 or not vec2:
        return 0.0

    # 🔥 Ensure same length
    min_len = min(len(vec1), len(vec2))
    vec1 = vec1[:min_len]
    vec2 = vec2[:min_len]

    dot_product = sum(a * b for a, b in zip(vec1, vec2))
    norm1 = math.sqrt(sum(a * a for a in vec1))
    norm2 = math.sqrt(sum(b * b for b in vec2))

    if norm1 == 0 or norm2 == 0:
        return 0.0

    return dot_product / (norm1 * norm2)


def extract_sources(reference_source):
    sources = []

    if isinstance(reference_source, dict):
        files = reference_source.get("filenames", [])

        for f in files:
            if isinstance(f, dict) and f.get("filename"):
                sources.append(f["filename"])

    elif isinstance(reference_source, list):
        # fallback (if already list)
        sources = reference_source

    return sources


async def analyze_questions_with_references(
    questions,
    reference_source,
    reference_main_source,
    user_id,
    runbook,
    progress_logs=None,
):

    print("--inside analyze_questions_with_references ")
    results = []
    payload = None
    if reference_main_source == "knowledge":

        embedding = await get_firework_embedding()
        value = runbook.get("analyze_input") or runbook.get("description")

        vector = embedding.embed_query(value)

        payload = QueryData(
            user_id=user_id,
            embedding=vector,
            top_k=3,
        )

        await credits.update_ai_credits_redis(
            user_id=user_id,
            credit_type="embedding",
            total_chars=len(value),
            reference_id="embedding_generation",
        )
    # 🔥 FETCH CONTEXT ONLY ONCE
    print("reference_main_source: ", reference_main_source)
    print("reference_source: ", reference_source)

    instruction_text = "\n".join(
        [
            f"Q: {q.get('question', '')} A: {q.get('user_answer', '')}"
            for q in questions
            if isinstance(q, dict)
        ]
    )
    if isinstance(reference_source, str):
        try:
            reference_source = json.loads(reference_source)
        except Exception as e:
            print("⚠️ Failed to parse reference_source:", e)
            reference_source = {}

    if not isinstance(reference_source, dict):
        reference_source = {}
    if isinstance(reference_source, str):
        try:
            reference_source = json.loads(reference_source)
        except Exception as e:
            print("⚠️ Second decode failed:", e)
            reference_source = {}
    files = reference_source.get("filenames", [])
    all_source_contexts = {}
    for file in files:
        fname = file.get("filename")
        single_source = {"filenames": [file]}

        ctx = await retreval_from_sources(
            conn,
            dbserver,
            reference_main_source,
            single_source,
            user_id,
            payload,
        )
        all_source_contexts[fname] = ctx

    # 🔥 Step C: Pick best source ONCE

    best_source = await pick_best_source_for_workflow(
        instruction_text, all_source_contexts
    )

    print("✅ Selected BEST SOURCE:", best_source)

    # 🔥 Step D: Use ONLY that source
    context_text = ""
    for source in best_source:
        best_context_chunks = all_source_contexts.get(source, [])
        context_text += "\n".join([c.get("data", "") for c in best_context_chunks])

    # print("context :",context_text)

    # 🔥 Create tasks with shared context
    # tasks = [
    #     analyze_single_question(
    #         q,
    #         context_text,  # ✅ pass precomputed context
    #         user_id,
    #     )
    #     for q in questions
    # ]

    # responses = await asyncio.gather(*tasks, return_exceptions=True)

    # for q, res in zip(questions, responses):
    #     if isinstance(res, Exception):
    #         print(f"Error analyzing question {q.get('id')}: {res}")
    #         continue

    #     res["id"] = q.get("id")
    #     results.append(res)
    BATCH_SIZE = 5
    all_results = []

    for i in range(0, len(questions), BATCH_SIZE):
        chunk = questions[i : i + BATCH_SIZE]

        print(f"🚀 Processing batch {i//BATCH_SIZE + 1}")

        batch_result = await analyze_single_question(chunk, context_text, user_id)

        if not batch_result:
            log(f"⚠️ Batch {i//BATCH_SIZE + 1} returned empty")

            print("⚠️ Empty batch result")
            continue
        print(f"✅ Batch {i//BATCH_SIZE + 1} completed")
        all_results.extend(batch_result)

    print(f"🎯 Total analyzed results: {len(results)}")

    return all_results

    return results


async def analyze_single_question(
    question_item,
    context_text,  # ✅ already computed
    user_id,
):
    # question_text = question_item.get("question")
    # user_answer = question_item.get("user_answer")
    # options = question_item.get("options")
    # question_type = question_item.get("question_type")
    qna_list = []

    for q in question_item:
        if not isinstance(q, dict):
            continue

        options = q.get("options") if q.get("question_type") == "MCQ" else None

        qna_list.append(
            {
                "id": q.get("id"),
                "question": q.get("question"),
                "question_type": q.get("question_type", "DESCRIPTIVE"),
                "user_answer": q.get("user_answer"),
                "comment": q.get("comment"),
                "options": options,
            }
        )

    # 🧠 Build prompt
    prompt = RUNBOOK_TEMPLATE["process_question_analysis_prompt"]
    # prompt = (
    #     prompt.replace("{{question}}", question_text or "")
    #     .replace("{{user_answer}}", user_answer or "")
    #     .replace("{{question_type}}", question_type or "DESCRIPTIVE")
    #     .replace(
    #         "{{options}}",
    #         (
    #             json.dumps(options, indent=2)
    #             if (question_type == "MCQ" and options)
    #             else None
    #         ),
    #     )
    #     .replace("{{context}}", context_text or "No reference data available")
    # )
    prompt = prompt.replace(
        "{{questions_json}}", json.dumps(qna_list, indent=2)
    ).replace("{{context}}", context_text or "No reference data available")

    # print("🧠 FINAL PROMPT:\n", prompt[:50])
    base_char = len(prompt)

    # 🤖 LLM call
    result = await get_think_fire_response2_og(
        user_message=prompt,
        user_id=user_id,
        credits=credits,
        total_input_chars=base_char,
    )

    # 🧹 Safe JSON parsing
    try:
        parsed = json.loads(result)
    except Exception:
        print("❌ Invalid JSON from LLM:", result[:500])
        return []

    return parsed


async def store_runbook_trigger_schedule(user_id, runbook_id, schedule):
    try:
        res = await dbserver.update_runbook_schedule(user_id, runbook_id, schedule)
    except:
        raise Exception


async def save_runbook_schedule(
    *,
    user_id: str,
    runbook_id: str,
    schedule_type: str,
    timezone: str,
    data: dict,
):
    import json, asyncio
    from datetime import datetime

    schedule_obj = {
        "type": schedule_type,
        "timezone": timezone,
        "data": data,
        "celery": {
            "task_id": None,
            "entry_name": None,
            "stop_key": None,
        },
        "execution_unique_key": None,
        "status": "scheduled",
        "last_run_at": None,
        "next_run_at": None,
        "created_at": datetime.utcnow().isoformat(),
    }

    res = await dbserver.update_runbook_schedule(user_id, runbook_id, schedule_obj)

    return {"status": "saved", "result": res}


async def activate_runbook_schedule(user_id: str, runbook_id: str):
    import json, asyncio, uuid
    from datetime import datetime

    row = await dbserver.get_runbook_by_id(user_id=user_id, runbook_id=runbook_id)

    if not row:
        raise Exception("Runbook not found")

    schedule = json.loads(row["schedule"])

    schedule_type = schedule["type"]
    timezone = schedule["timezone"]
    data = schedule["data"]

    uniquekey = f"{runbook_id}_{uuid.uuid4()}"

    # -----------------------------------
    # SELECT TASK (NEW CELERY TASKS)
    # -----------------------------------
    # if row.get("playbook_id"):
    #     task_name = "tasks.trigger_runbook_from_playbook_task"
    #     args = [user_id, row["playbook_id"], runbook_id]

    # elif row.get("api_endpoint"):
    #     task_name = "tasks.trigger_runbook_from_api_task"
    #     args = [user_id, row["api_endpoint"], row["api_endpoint"], {}]

    # else:
    #     raise Exception("No trigger source found")

    # -----------------------------------
    # SCHEDULING
    # -----------------------------------
    # if schedule_type == "daily":
    #     hour, minute = map(int, data["startTime"].split(":"))

    #     result = await SchedulerService.schedule_daily(
    #         hour, minute, user_id, task_name, timezone, args
    #     )

    #     schedule["celery"]["entry_name"] = result["entry_name"]

    # elif schedule_type == "weekly":
    #     hour, minute = map(int, data["startTime"].split(":"))

    #     result = await SchedulerService.schedule_weekly(
    #         data["weekday"], hour, minute, user_id, task_name, timezone, args
    #     )

    #     schedule["celery"]["entry_name"] = result["entry_name"]

    # elif schedule_type == "one_time":
    #     dt = datetime.fromisoformat(data["datetime"])

    #     result = await SchedulerService.schedule_one_time(
    #         dt, user_id, task_name, timezone, args
    #     )

    #     schedule["celery"]["task_id"] = result["task_id"]

    # elif schedule_type == "custom":
    #     result = await SchedulerService.schedule_custom(
    #         start_date=data["startDate"],
    #         start_time=data["startTime"],
    #         userid=user_id,
    #         filename=task_name,
    #         timezone=timezone,
    #         contacts=args,
    #     )

    #     schedule["celery"]["task_id"] = result["task_id"]

    # else:
    #     raise Exception("Unsupported schedule type")

    # schedule["execution_unique_key"] = uniquekey

    # -----------------------------------
    # SAVE BACK
    # -----------------------------------
    await dbserver.update_runbook_schedule(user_id, runbook_id, schedule)

    return {
        "status": "activated",
        "runbook_id": runbook_id,
    }


async def trigger_scheduled_playbook_runbook(user_id, runbook_id):
    pass


async def trigger_scheduled_api_runbook(user_id, runbook_id):
    pass
