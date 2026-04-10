import asyncio
import json
import os
import time
import traceback
from urllib.parse import parse_qs, unquote, urlparse
import uuid
from agent_route.doc_clarity import QueryData
import boto3
import pandas as pd
import copy

from credits_route.route import Credits
from db.lance_db_service import LanceDBServer
from db.rds_db import connect_to_rds
from playbook.helperzz import base_name, assign_runbook_playbook

# from services.scheduler_service import SchedulerService
from utils.img_tokens import image_credit_cost
from utils.normal import load_yaml_file
from utils.s3_utils import S3_BUCKET, read_json_from_s3, s3bucket, upload_any_file
from utils.fireworkzz import (
    get_firework_embedding,
    get_think_fire_response2_og,
    get_think_fire_response2_og2,
)
from utils.normal import load_yaml_file
from radar.radar_helpers import (
    _safe_json_parse,
    extract_file_payload,
    process_file_payloads,
)

# from apiConnector.helpers import (
#     _execute_endpoint_internal,
#     _execute_app_internal,
#     build_full_url,
# )
from cust_helpers import pathconfig
from db.db_checkers import get_notes_data
from services.apiconnectors import APIConnector
from services.redis_service import RedisService
from utils.scheduler import scheduler
from apscheduler.triggers.cron import CronTrigger
from utils.base_logger import get_logger

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


def run_runbook_job_wrapper(runbook):
    # print("🚀 WRAPPER TRIGGERED")
    asyncio.run(run_runbook_job(runbook))


async def run_runbook_job(runbook):
    #  print(f"🔥 JOB TRIGGERED: {runbook['runbook_id']}")

    try:
        print(f"🚀 Running runbook {runbook['runbook_id']}")
        conn = connect_to_rds()
        dbserver = LanceDBServer()
        credits = Credits(db=conn)
        await run_runbook_execution_engine(
            conn=conn,
            dbserver=dbserver,
            credits=credits,
            user_id=runbook["user_id"],
            runbook=runbook,
        )
        print(f"✅ COMPLETED: {runbook['runbook_id']}")
    except Exception as e:
        print("❌ FULL ERROR:", traceback.format_exc())
        print(f"❌ Runbook failed: {e}")


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
        print("❌ S3 READ ERROR:", str(e))
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


async def retreval_from_sources(
    conn, dbserver, main_source, datasources, userid, payload
):
    from umail.routes import get_sorted_lance_emails
    from apiConnector.helpers import _execute_endpoint_internal

    data_for_review = []
    # -------------------------
    # APP SOURCE
    # -------------------------
    if main_source == "app" and datasources:
        endpoint_ids = datasources.get("endpoint_ids", [])

        for endpoint_id in endpoint_ids:
            try:
                result = await _execute_endpoint_internal(
                    endpoint_id=endpoint_id,
                    userid=userid,
                )
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
    elif main_source == "notes" and datasources:
        note_ids = datasources.get("note_ids", [])
        all_notes = get_notes_data(userid)  # expect list[ {note_id, content, ...} ]
        # print("len of all_notes", len(all_notes), all_notes)
        for note in all_notes.get("notes"):
            # print("type of note", type(note), note)
            if note.get("note_id") in note_ids:
                data_for_review.append(
                    {"type": "notes", "note_id": note.get("note_id"), "data": str(note)}
                )

    # -------------------------
    # EMAIL SOURCE
    # -------------------------
    elif main_source == "emails" and datasources:
        client_ids = datasources.get("client_ids", [])
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
    elif main_source == "knowledge" and datasources:
        filenames = datasources.get("filenames", [])
        for file in filenames:
            if file.get("type") == "docs":
                fname = file.get("filename")
                results = await dbserver.query_vector_filename(
                    query=payload, filename=fname
                )
                if results:
                    for item in results:
                        data_for_review.append(
                            {
                                "type": "docs",
                                "source": fname,
                                "data": str(item.get("text", "")),
                            }
                        )
                else:
                    newdas = await dbserver.fetch_by_filename(
                        user_id=userid, filename=fname
                    )
                    if newdas:
                        for item in newdas:
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
                    data_for_review.append(
                        {
                            "type": "scrape",
                            "source": url,
                            "data": str(results.get("text", "")),
                        }
                    )

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


async def run_runbook_execution_engine(
    conn,
    credits,
    user_id,
    runbook,
    dbserver=LanceDBServer(),
    structure_file_payload=None,
    files=None,
    structure_file=None,
    result_id=None,
):
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

    if not structure_file_payload:
        structure_file_payload = runbook["structure_theme"]

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
    try:
        analyze_input = ""
        if "analyze_input" in runbook and runbook["analyze_input"]:
            analyze_input = runbook["analyze_input"]
        else:
            analyze_input = runbook["description"] if "description" in runbook else ""

        file_data = ""
        if not result_id:
            file_data = await collect_runbook_inputs(runbook)
        user_analyze_input = analyze_input
        # print("RUNBOOK YAML:", runbook_yaml)
        # print("📥 INPUT RECEIVED:", str(file_data)[:200])
        # --------------------------------------------------
        # FILE PROCESSING  (same as radar)
        # --------------------------------------------------

        # INP_LINKS = []
        # STR_LINKS = []

        # file_data_payload = []
        # # structure_file_payload = []

        # if files:

        #     process_file_payloads(
        #         user_id=user_id,
        #         files=files,
        #         inp_links=INP_LINKS,
        #         extracted_payload=file_data_payload,
        #     )

        # if structure_file:

        #     process_file_payloads(
        #         user_id=user_id,
        #         files=(
        #             structure_file_content
        #             if isinstance(structure_file_content, list)
        #             else [structure_file_content]
        #         ),
        #         inp_links=STR_LINKS,
        #         extracted_payload=structure_file_payload,
        #     )

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
        # len(analyze_input.split())
        # print("language_data: ",lang_data)
        # print("output_word_count: ", output_word_count)
        # --------------------------------------------------
        # STRUCTURE GENERATION
        # --------------------------------------------------

        # if structure_file_payload:

        #     try:
        #         structure_prompt_key = runbook_yaml["radar"]["structure_prompt"]

        #         structure_prompt = RADAR_TEMPLATE[structure_prompt_key]

        #         structure_prompt = (
        #             structure_prompt.replace(
        #                 "{{document_file_data}}", json.dumps(structure_file_payload)
        #             )
        #             .replace("{{file_links}}", json.dumps(STR_LINKS))
        #             .replace(
        #                 "{{user_original_prompt_or_context}}",
        #                 user_analyze_input or "",
        #             )
        #             .replace("{{output_language}}", output_language)
        #         )
        #         base_chars = len(structure_prompt)

        #         for img in STR_LINKS:
        #             base_chars -= len(img)
        #             base_chars += image_credit_cost(img)

        #         result = await get_think_fire_response2_og(
        #             user_message=structure_prompt,
        #             user_id=user_id,
        #             credits=credits,
        #             total_input_chars=base_chars,
        #         )

        #         structure_file_payload = json.loads(result)
        #         logger.info("✅ STRUCTURE GENERATED")

        #     except Exception:
        #         logger.exception("❌ STRUCTURE GENERATION FAILED")
        #         raise
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

        if main_source:

            data_checked = await retreval_from_sources(
                conn,
                dbserver,
                main_source,
                data_sources,
                user_id,
                payload,
            )

        if refernce_main_source:

            reference_RWA = await retreval_from_sources(
                conn,
                dbserver,
                refernce_main_source,
                reference_sources,
                user_id,
                payload,
            )
        # ---------------------------------
        # LAST RESPONSE FETCH
        # ---------------------------------
        last_runbook_response = ""

        if runbook_id:

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
        # else:
        #     # Fallback to normal prompts
        #     review_temp = (
        #         RADAR_TEMPLATE.get(prompts.get("review"))
        #         or RADAR_TEMPLATE.get(prompts.get("analysis"))
        #         or RADAR_TEMPLATE.get(prompts.get("recommendation"))
        #     )

        # --------------------------------------------------
        # BUILD PROMPT
        # --------------------------------------------------
        # print("length of structure payload: ", len(structure_file_payload))
        # print("based structure: ", str(structure_file_payload))
        # if source_input:
        #     file_data_payload.append(source_input)
        if not output_word_count:
            output_word_count = 800
        base_prompt = (
            review_temp.replace("{{analyze_input}}", (user_analyze_input or ""))
            .replace("{{file_data}}", file_data)
            .replace("{{structure_file_data}}", json.dumps(structure_file_payload))
            .replace("{{file_links}}", "")
            .replace("{{data_sources}}", json.dumps(data_checked))
            .replace("{{reference_sources}}", json.dumps(reference_RWA))
            .replace(
                "{{last_radar_response}}",
                last_runbook_response,
            )
            .replace("{{output_language}}", output_language)
            .replace("{{requested_word_count}}", str(output_word_count))
        )

        # --------------------------------------------------
        # LLM CALL
        # --------------------------------------------------
        base_chars = len(base_prompt)

        # for img in INP_LINKS:
        #     base_chars -= len(img)
        #     base_chars += image_credit_cost(img)

        result = await get_think_fire_response2_og2(
            user_message=base_prompt,
            user_id=user_id,
            credits=credits,
            total_input_chars=base_chars,
            language=output_language,
            words_count=output_word_count,
        )
        # print("RAW LLM RESULT:", result)
        # --------------------------------------------------
        # MERGE RADAR CHUNKS
        # --------------------------------------------------

        merged_report = merge_runbook_chunks_deterministic(raw_chunks=result)

        # --------------------------------------------------
        # PARSE RESULT
        # --------------------------------------------------

        refactor_result = _safe_json_parse(merged_report)
        # print("refactor result: ",refactor_result)
        # --------------------------------------------------
        # RISK SCORE (NIST LLM BASED)
        # --------------------------------------------------

        risk_prompt_key = runbook_yaml["radar"].get(
            "risk_prompt", "nist_risk_score_prompt"
        )
        risk_prompt_template = RADAR_TEMPLATE[risk_prompt_key]

        # risk_prompt = risk_prompt_template.replace(
        #     "{{analysis_result}}", json.dumps(refactor_result)
        # )
        risk_prompt = risk_prompt_template.replace(
            "{{analysis_result}}", json.dumps(refactor_result)
        ).replace(
            "{{report_data}}",
            json.dumps(structure_file_content) if structure_file_content else "",
        )

        risk_llm_result = await get_think_fire_response2_og(
            user_message=risk_prompt,
            user_id=user_id,
            credits=credits,
            total_input_chars=len(risk_prompt),
        )

        # print("RISK RAW:", risk_llm_result)

        risk_data = _safe_json_parse(risk_llm_result)

        risk_score = risk_data.get("final_risk_score", 0)

        # attach full breakdown (VERY IMPORTANT)
        refactor_result["risk_analysis"] = risk_data
        refactor_result["risk_score"] = risk_score

        # --------------------------------------------------
        # STORE RESULT
        # --------------------------------------------------
        if refactor_result:
            await dbserver.insert_runbook_result(
                {
                    "execution_id": execution_id,
                    "result_id": new_result_id,
                    "runbook_id": runbook_id,
                    "user_id": user_id,
                    "status": "completed",
                    # "structure_theme":default or structure_file_payload
                    "risk_score": risk_score,
                    "result": refactor_result,
                    "started_at": started_at,
                    "ended_at": int(time.time()),
                    "input_mode": runbook.get("input_type"),
                }
            )

        return refactor_result
    except Exception as e:
        print("eror in rubook execution", e)
        print("❌ FULL ERROR:", traceback.format_exc())
        await dbserver.insert_runbook_result(
            {
                "execution_id": execution_id,
                "result_id": new_result_id,
                "runbook_id": runbook_id,
                "user_id": user_id,
                "status": "failed",
                "risk_score": risk_score,
                "result": refactor_result,
                "started_at": started_at,
                "ended_at": int(time.time()),
                "input_mode": runbook.get("input_type"),
            }
        )


async def trigger_runbooks_for_api_response(user_id, app_id, endpoint_id, record):
    try:
        dbserver = LanceDBServer()
        conn = connect_to_rds()
        credits = Credits(conn)

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
        if "files" in runbook:
            if "structure_file" in runbook["files"]:
                structure_file = runbook["files"]["structure_file"]

        structure_file_payload = runbook["structure_theme"]

        # print("📥 INPUT:", runtime_input)

        # ✅ 3. EXECUTE (THIS WILL CREATE RESULT ENTRY)
        await run_runbook_execution_engine(
            conn=conn,
            dbserver=dbserver,
            credits=credits,
            user_id=user_id,
            runbook=runbook,
            structure_file=structure_file,
            structure_file_payload=structure_file_payload,
        )

        conn.close()
        return {"status": "success"}

    except Exception as e:
        print("❌ Error in trigger_runbooks:", str(e))
        raise


async def get_playbook_instruction(user_id, filename):
    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"
    s3_key = f"{user_id}/workflow/{base_name(filename)}/{filename}"
    instruction_data = read_json_from_s3(s3_key)

    # qna = await extract_qna_from_instruction(instruction_data=instruction_data)
    # qna = instruction_data.get("chat", [])
    return instruction_data


async def extract_qna_from_instruction(instruction_data: dict):
    result = []

    try:
        chats = instruction_data.get("chat", [])

        for chat in chats:
            # ✅ Only success responses
            if chat.get("status") != "success":
                continue

            submission_date = chat.get("date")
            outputs = chat.get("output", [])

            responses = []

            for item in outputs:
                question = item.get("question")
                answer = item.get("user_answer")

                if question and answer:
                    responses.append({"question": question, "answer": answer})

            # ✅ Only append if there are valid responses
            if responses:
                result.append({"submitted_at": submission_date, "responses": responses})

    except Exception as e:
        print(f"Error extracting QnA: {e}")

    return result


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
    conn = connect_to_rds
    credits = Credits(conn)

    runbook = await dbserver.get_runbook_by_id(user_id=user_id, runbook_id=runbook_id)
    if isinstance(runbook, list):
        runbook = runbook[0] if runbook else None

    if isinstance(runbook, str):
        runbook = json.loads(runbook)

    print(f"Using runbook:{runbook.get('runbook_id')} - {runbook.get('name')}")
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

    # runbook["main_source"] = "knowledge"
    # runbook["reference_main_source"] = "knowledge"
    # print("out of range 2")
    structure_file = None
    if "files" in runbook:
        if "structure_file" in runbook["files"]:
            structure_file = runbook["files"]["structure_file"]
    structure_file_payload = runbook["structure_theme"]
    # print("out of range 3")
    # print("struct file: ", str(structure_file)[:10])
    runtime_input = await get_playbook_instruction(user_id, playbook_id)
    runbook["runtime_input"] = json.dumps(runtime_input.get("chat", []))

    print("executing runbook playbook")
    await run_runbook_execution_engine(
        dbserver=dbserver,
        conn=conn,
        credits=credits,
        user_id=user_id,
        runbook=runbook,
        structure_file=structure_file,
        structure_file_payload=structure_file_payload,
    )


async def playbook_runbook_execution(user_id, runbook):

    print("Inside executing playbook runbook : ", runbook["runbook_id"])
    await run_runbook_execution_engine(
        conn=conn, credits=Credits(conn), user_id=user_id, runbook=runbook
    )


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


async def structure_payload_generation(user_id, analyze_input, structure_file):
    try:

        structure_file_payload = []
        STR_LINKS = []

        ##processing file payload
        process_file_payloads(
            user_id=user_id,
            files=(
                structure_file if isinstance(structure_file, list) else [structure_file]
            ),
            inp_links=STR_LINKS,
            extracted_payload=structure_file_payload,
        )

        # language extraction lang_prompt = RADAR_TEMPLATE[lang_prompt_key]

        lang_prompt = RADAR_TEMPLATE["language_wordcount_extractor"]
        lang_prompt = lang_prompt.replace("{{analyze_input}}", str(analyze_input or ""))

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

        structure_prompt = RADAR_TEMPLATE["structure_prompt_template"]

        structure_prompt = (
            structure_prompt.replace(
                "{{document_file_data}}", json.dumps(structure_file_payload)
            )
            .replace("{{file_links}}", json.dumps(STR_LINKS))
            .replace(
                "{{user_original_prompt_or_context}}",
                analyze_input or "",
            )
            .replace("{{output_language}}", output_language)
        )
        base_chars = len(structure_prompt)

        for img in STR_LINKS:
            base_chars -= len(img)
            base_chars += image_credit_cost(img)

        result = await get_think_fire_response2_og(
            user_message=structure_prompt,
            user_id=user_id,
            credits=credits,
            total_input_chars=base_chars,
        )

        structure_file_payload = json.loads(result)
        logger.info("✅ STRUCTURE GENERATED")
        return structure_file_payload

    except Exception:
        logger.exception("❌ STRUCTURE GENERATION FAILED")
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
