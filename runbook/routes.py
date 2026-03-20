from asyncio.log import logger
import csv
import io
import json
import time
import asyncio
import logging
import traceback
import uuid
import json
import os
import pymysql

from apiConnector.helpers import _execute_endpoint_internal,_execute_app_internal, build_full_url
from cust_helpers import pathconfig
from db.db_checkers import get_notes_data
from services.apiconnectors import APIConnector
from services.redis_service import RedisService
from db.lance_db_service import LanceDBServer
from credits_route.route import Credits
from db.rds_db import connect_to_rds

from umail.routes import get_sorted_lance_emails
from utils.fireworkzz import get_think_fire_response2_og, get_think_fire_response2_og2
from utils.normal import load_yaml_file
from radar.radar_helpers import _safe_json_parse, process_file_payloads
from flask import Blueprint, jsonify,request, session
from utils.s3_utils import S3_BUCKET, s3bucket, upload_any_file
from utils.scheduler import scheduler
from apscheduler.triggers.cron import CronTrigger



runbook_bp = Blueprint("runbook", __name__)
RUNBOOK_TEMPLATE = load_yaml_file(path=pathconfig.runbook_prompts)
RADAR_TEMPLATE = load_yaml_file(path=pathconfig.radar_prompts)
logger = logging.getLogger(__name__)


def schedule_runbook(runbook):

    cron_expr = runbook.get("schedule")

    if not cron_expr:
        return
    if cron_expr == "1m":
        cron_expr = "*/1 * * * *"
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
        args = [runbook],
        replace_existing=True
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
            credits = credits,
            user_id=runbook["user_id"],
            runbook=runbook
        )
        print(f"✅ COMPLETED: {runbook['runbook_id']}")
    except Exception as e:
        print("❌ FULL ERROR:", traceback.format_exc())
        print(f"❌ Runbook failed: {e}")


async def build_runbook_prompt(runbook):

    input_type = runbook["input_type"]

    if input_type == "questionnaire":

        return f"""
        Generate security review based on questionnaire answers.
        Use playbook: {runbook.get("playbook_id")}
        """

    elif input_type == "api":

        return f"""
        Analyze API endpoint response for security risks.

        Endpoint:
        {runbook.get("api_endpoint")}
        """

    elif input_type == "logs":

        return f"""
        Analyze logs for anomalies.

        Log source:
        {runbook.get("log_source")}
        """


def render_runbook_yaml(runbook):

    template = RUNBOOK_TEMPLATE["runbook"]

    rendered = json.loads(
        json.dumps(template)
        .replace("${runbook_name}", runbook.get("name",""))
        .replace("${runbook_type}", runbook.get("runbook_type",""))

        .replace("${schedule_type}", runbook.get("schedule_type","cron"))
        .replace("${cron_expression}", runbook.get("cron",""))

        .replace("${input_type}", runbook.get("input_type",""))

        .replace("${playbook_id}", str(runbook.get("playbook_id") or ""))
        .replace("${api_endpoint}", str(runbook.get("api_endpoint") or ""))
        .replace("${log_source}", str(runbook.get("log_source") or ""))
    )

    return rendered

async def fetch_logs_from_url(url):

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            text = await resp.text()

    reader = csv.DictReader(io.StringIO(text))

    return list(reader)

def read_csv_logs(file_path):

    with open(file_path, newline="") as f:
        reader = csv.DictReader(f)
        return list(reader)

async def get_logs_data(user_id,source):
   # case 1 → url log file
    if source.startswith("http"):

        logs =  fetch_logs_from_url(source)

        # case 2 → csv file path
    elif source.endswith(".csv"):

        logs = read_csv_logs(source)
    return logs

def get_playbook_answers(user_id, source):
    pass

def read_csv_logs_from_s3(s3_key):
    try:
        # print("📥 Fetching S3 key:", s3_key)

        response = s3bucket().get_object(
            Bucket=S3_BUCKET,
            Key=s3_key
        )

        content = response["Body"].read()
        import pandas as pd
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
                    logs.append({
                        "message": line
                    })

            return logs

    except Exception as e:
        print("❌ S3 READ ERROR:", str(e))
        raise Exception(f"S3 log read failed: {s3_key} | {str(e)}")

def reconstruct_and_format_logs(logs):

    buffer = ""
    merged_logs = []

    for item in logs:
        line = item.get("message", "").strip()

        if not line:
            continue

        buffer += line

        # ✅ detect complete JSON
        if buffer.endswith("}"):
            try:
                parsed = json.loads(buffer)

                clean_line = (
                    f"{parsed.get('Time')} | "
                    f"{parsed.get('LogLevel')} | "
                    f"{parsed.get('Message')}"
                )

                merged_logs.append(clean_line)
                buffer = ""

            except Exception:
                # continue accumulating
                continue

    return "\n".join(merged_logs)

def calculate_risk_score(result):

    score = 0

    if "critical" in str(result).lower():
        score += 40

    if "vulnerability" in str(result).lower():
        score += 30

    if "misconfiguration" in str(result).lower():
        score += 20

    return min(score,100)

async def execute_app_via_endpoint(app_id: int, user_id: str):
    conn = connect_to_rds()
    cur = conn.cursor(pymysql.cursors.DictCursor)

    try:
        # 1️⃣ Get endpoints for this app
        cur.execute(
            """
            SELECT id
            FROM global_app_endpoints
            WHERE app_id = %s
            """,
            (app_id,)
        )

        endpoints = cur.fetchall()
        print("endpoints:",endpoints)
        if not endpoints:
            raise ValueError("No active endpoints found for this app")

        results = []

        # 2️⃣ Execute each endpoint
        for ep in endpoints:
            endpoint_id = ep["id"]

            try:
                response = await _execute_global_endpoint_internal(
                    endpoint_id=endpoint_id,
                    user_id=user_id,
                    app_id=app_id
                )

                results.append({
                    "endpoint_id": endpoint_id,
                    "status": "success",
                    "response": response
                })

            except Exception as e:
                results.append({
                    "endpoint_id": endpoint_id,
                    "status": "failed",
                    "error": str(e)
                })

        return {
            "app_id": app_id,
            "results": results
        }

    finally:
        cur.close()
        conn.close()

async def collect_runbook_inputs(runbook):

    if runbook.get("playbook_id"):

        return f"Playbook questionnaire {runbook['playbook_id']}"

    elif runbook.get("api_endpoint"):
        if runbook.get("api_source_type") == "global":
            result = await _execute_global_endpoint_internal(endpoint_id=runbook["api_endpoint"],user_id=runbook["user_id"],app_id=runbook["app_id"])
        elif runbook.get("api_source_type") == "user":
            result = await _execute_endpoint_internal(runbook["api_endpoint"],runbook["user_id"])
        # print("api result:",result)
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

    data_for_review = []
    # -------------------------
    # APP SOURCE
    # -------------------------
    if main_source == "app":
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
    elif main_source == "notes":
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
    elif main_source == "emails":
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
    elif main_source == "knowledge":
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
    # -------------------------
    # QUESTIONNAIRE SOURCE
    # -------------------------
    elif main_source == "questionnaire":

        playbook_id = datasources.get("playbook_id")

        data = await get_playbook_answers(
            user_id=userid,
            playbook_id=playbook_id
        )

        data_for_review.append({
            "type": "questionnaire",
            "playbook_id": playbook_id,
            "data": str(data)
        })
    # -------------------------
    # API SOURCE
    # -------------------------
    elif main_source == "api":

        endpoint_ids = datasources.get("endpoint_ids", [])

        for endpoint in endpoint_ids:
            try:

                result = await _execute_endpoint_internal(
                    endpoint_id=endpoint,
                    userid=userid
                )

                data_for_review.append({
                    "type": "api",
                    "endpoint_id": endpoint,
                    "data": str(result)
                })

            except Exception as e:

                data_for_review.append({
                    "type": "api",
                    "endpoint_id": endpoint,
                    "error": str(e)
                })
    # -------------------------
    # LOG SOURCE
    # -------------------------
    elif main_source == "logs":

        log_source = datasources.get("log_source")

        s3_key = datasources.get("log_source")

        if not s3_key:
            raise Exception("Missing log_source")

        logs = read_csv_logs_from_s3(s3_key)

        data_for_review.append({
            "type": "logs",
            "source": logs,
            "data": str(logs)
        })

    return data_for_review

import copy


def merge_runbook_chunks_deterministic(
    raw_chunks,
    output_language="english",
    runbook_id=None,
    execution_id=None
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
            "output_language": output_language
        }
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
        merged["analysis_depth"] = chunk.get(
            "analysis_depth",
            merged["analysis_depth"]
        )

        merged["analysis_depth_rationale"] = chunk.get(
            "analysis_depth_rationale",
            merged["analysis_depth_rationale"]
        )

        # -----------------------------
        # recommendation depth
        # -----------------------------
        merged["recommendation_depth"] = chunk.get(
            "recommendation_depth",
            merged["recommendation_depth"]
        )

        merged["recommendation_depth_rationale"] = chunk.get(
            "recommendation_depth_rationale",
            merged["recommendation_depth_rationale"]
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
        merged["intent_type"] = chunk.get(
            "intent_type",
            merged["intent_type"]
        )

        merged["core_objective"] = chunk.get(
            "core_objective",
            merged["core_objective"]
        )

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

    # -----------------------------
    # deterministic block ordering
    # -----------------------------
    merged["blocks"] = sorted(
        merged["blocks"],
        key=lambda x: x.get("block_id", "")
    )

    # -----------------------------
    # cleanup empty fields
    # -----------------------------
    merged = {
        k: v
        for k, v in merged.items()
        if v not in [None, "", [], {}]
    }

    return merged

async def run_runbook_execution_engine(
    conn,
    dbserver,
    credits,
    user_id,
    runbook,
    files=None,
    structure_file=None,
    datasources=None,
    reference_RWA=None,
):
    

    runbook_id = runbook["runbook_id"]

    execution_id = f"exec_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    started_at = int(time.time())
    risk_score = None
    refactor_result = {}

    await dbserver.insert_runbook_result({
        "execution_id": execution_id,
        "runbook_id": runbook_id,
        "user_id": user_id,
        "status": "running",
        "started_at": started_at,
        "input_mode": runbook.get("input_type")
    })
    # --------------------------------------------------
    # RENDER RUNBOOK TEMPLATE
    # --------------------------------------------------

    runbook_yaml = render_runbook_yaml(runbook)

    # --------------------------------------------------
    # RESOLVE RUNBOOK INPUT
    # --------------------------------------------------
    try:
        analyze_input = await collect_runbook_inputs(runbook)

        user_analyze_input = analyze_input
        print("RUNBOOK YAML:", runbook_yaml)
        print("ANALYZE INPUT:", analyze_input)
        # --------------------------------------------------
        # FILE PROCESSING  (same as radar)
        # --------------------------------------------------

        INP_LINKS = []
        STR_LINKS = []

        file_data_payload = []
        structure_file_payload = []

        if files:

            process_file_payloads(
                user_id=user_id,
                files=files,
                inp_links=INP_LINKS,
                extracted_payload=file_data_payload,
            )

        if structure_file:

            process_file_payloads(
                user_id=user_id,
                files=[structure_file],
                inp_links=STR_LINKS,
                extracted_payload=structure_file_payload,
            )

        # --------------------------------------------------
        # LANGUAGE + WORD COUNT (same radar logic)
        # --------------------------------------------------

        lang_prompt_key = runbook_yaml["radar"]["language_prompt"]

        lang_prompt = RADAR_TEMPLATE[lang_prompt_key]

        lang_prompt = lang_prompt.replace(
            "{{analyze_input}}",
            str(user_analyze_input or analyze_input)
        )

        # print("lang_prompt: ",lang_prompt)

        result = await get_think_fire_response2_og(
            user_message=lang_prompt,
            user_id=user_id,
            credits=credits,
            total_input_chars=len(lang_prompt),
        )
        print("LANG RESULT RAW:", result)
        lang_data = json.loads(result)

        output_language = lang_data.get("language", "English")
        output_word_count = lang_data.get("word_count") or len(analyze_input) 
        # print("language_data: ",lang_data)
        print("output_word_count: ",output_word_count)
        # --------------------------------------------------
        # STRUCTURE GENERATION
        # --------------------------------------------------

        if structure_file_payload:

            structure_prompt_key = runbook_yaml["radar"]["structure_prompt"]

            structure_prompt = RADAR_TEMPLATE[structure_prompt_key]

            structure_prompt = (
                structure_prompt
                .replace("{{document_file_data}}", json.dumps(structure_file_payload))
                .replace("{{file_links}}", json.dumps(STR_LINKS))
                .replace("{{user_original_prompt_or_context}}", analyze_input)
                .replace("{{output_language}}", output_language)
            )

            result = await get_think_fire_response2_og(
                user_message=structure_prompt,
                user_id=user_id,
                credits=credits,
                total_input_chars=len(structure_prompt),
            )

            structure_file_payload = json.loads(result)

        # --------------------------------------------------
        # PROMPT SELECTION
        # --------------------------------------------------

        btype = runbook_yaml["radar"]["format"]

        prompts = runbook_yaml["radar"]["prompts"]
        structure_prompts = runbook_yaml["radar"]["structure_prompts"]

        if btype == "review":

            if structure_file_payload:
                review_temp = RADAR_TEMPLATE[structure_prompts["review"]]
            else:
                review_temp = RADAR_TEMPLATE[prompts["review"]]

        elif btype == "analyze":

            if structure_file_payload:
                review_temp = RADAR_TEMPLATE[structure_prompts["analysis"]]
            else:
                review_temp = RADAR_TEMPLATE[prompts["analysis"]]

        elif btype == "decide":

            if structure_file_payload:
                review_temp = RADAR_TEMPLATE[structure_prompts["recommendation"]]
            else:
                review_temp = RADAR_TEMPLATE[prompts["recommendation"]]

        # --------------------------------------------------
        # OPTIONAL RADAR DATA SOURCES
        # --------------------------------------------------

        data_checked = []

        if datasources:

            data_checked = await retreval_from_sources(
                conn,
                dbserver,
                datasources.get("main_source"),
                datasources,
                user_id,
                analyze_input
            )

        # --------------------------------------------------
        # BUILD PROMPT
        # --------------------------------------------------

        base_prompt = (
            review_temp
            .replace("{{analyze_input}}", (analyze_input or ""))
            .replace("{{file_data}}", json.dumps(file_data_payload))
            .replace("{{structure_file_data}}", json.dumps(structure_file_payload))
            .replace("{{file_links}}", json.dumps(INP_LINKS))
            .replace("{{data_sources}}", json.dumps(data_checked))
            .replace("{{reference_sources}}", json.dumps(reference_RWA or []))
            .replace("{{output_language}}", output_language)
            .replace("{{requested_word_count}}", str(output_word_count))
        )

        # --------------------------------------------------
        # LLM CALL
        # --------------------------------------------------

        result = await get_think_fire_response2_og2(
            user_message=base_prompt,
            user_id=user_id,
            credits=credits,
            total_input_chars=len(base_prompt),
            language=output_language,
            words_count=output_word_count,
        )
        # print("RAW LLM RESULT:", result)
        # --------------------------------------------------
        # MERGE RADAR CHUNKS
        # --------------------------------------------------

        merged_report = merge_runbook_chunks_deterministic(
            raw_chunks=result
        )

        # --------------------------------------------------
        # PARSE RESULT
        # --------------------------------------------------

        refactor_result = _safe_json_parse(merged_report)
        # print("refactor result: ",refactor_result)
        # --------------------------------------------------
        # RISK SCORE (runbook specific)
        # --------------------------------------------------

        # risk_score = calculate_risk_score(refactor_result)

        # refactor_result["risk_score"] = risk_score
        # --------------------------------------------------
        # RISK SCORE (NIST LLM BASED)
        # --------------------------------------------------

        risk_prompt_key = runbook_yaml["radar"].get("risk_prompt", "nist_risk_score_prompt")
        risk_prompt_template = RADAR_TEMPLATE[risk_prompt_key]

        risk_prompt = risk_prompt_template.replace(
            "{{analysis_result}}",
            json.dumps(refactor_result)
        )

        risk_llm_result = await get_think_fire_response2_og(
            user_message=risk_prompt,
            user_id=user_id,
            credits=credits,
            total_input_chars=len(risk_prompt),
        )

        print("RISK RAW:", risk_llm_result)

        risk_data = _safe_json_parse(risk_llm_result)

        risk_score = risk_data.get("final_risk_score", 0)

        # attach full breakdown (VERY IMPORTANT)
        refactor_result["risk_analysis"] = risk_data
        refactor_result["risk_score"] = risk_score

        # --------------------------------------------------
        # STORE RESULT
        # --------------------------------------------------

        await dbserver.insert_runbook_result({
                "execution_id": execution_id,
                "runbook_id": runbook_id,
                "user_id": user_id,
                "status": "completed",
                "risk_score": risk_score,
                "result": refactor_result,
                "started_at": started_at,
                "ended_at": int(time.time()),
                "input_mode": runbook.get("input_type")
            })

        return refactor_result
    except Exception as e:
        print("❌ FULL ERROR:", traceback.format_exc())
        await dbserver.insert_runbook_result({
            "execution_id": execution_id,
            "runbook_id": runbook_id,
            "user_id": user_id,
            "status": "completed",
            "risk_score": risk_score,
            "result": refactor_result,
            "started_at": started_at,
            "ended_at": int(time.time()),
            "input_mode": runbook.get("input_type")
        })
        

@runbook_bp.route("/runbook/create", methods=["POST"])
async def create_runbook():

    data = request.form.to_dict()
    user_id = data.get("user_id")

    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        
        runbook_data = {
            "runbook_id": str(uuid.uuid4()),
            "user_id": user_id,
            "name": data.get("name"),
            "description": data.get("description"),
            "runbook_type": data.get("runbook_type"),
            "schedule": data.get("schedule"),  # cron expression
            "input_type": data.get("input_type"),
            "playbook_id": data.get("playbook_id"),
            "api_endpoint": data.get("endpoint_id"),
            "log_source": data.get("log_source"),
            "files": data.get("files", []),
            "links": data.get("links", []),
            "data_sources": data.get("data_sources", []),
            "reference_sources": data.get("reference_sources", []),
        }
        file = request.files.get("log_file")
        log_source = None

        # CASE 1: file upload
        if file:
            result = upload_file_object(file, data.get("user_id"))

            if result["status"] != "success":
                return jsonify({"error": "File upload failed"}), 500

            log_source = result["s3_key"]

        # CASE 2: URL input
        elif data.get("log_source"):
            log_source = data.get("log_source")

            # ✅ decide log_source safely

        runbook_data["log_source"] = log_source
        # print("result:",log_source)
        # print("📦 RECEIVED FILE:", file.filename if file else None)
        # print("FINAL LOG SOURCE:", runbook_data["log_source"])
        dbserver = LanceDBServer()
        result = await dbserver.insert_runbook(runbook_data)
        runbook_data["app_id"] = data.get("app_id")
        runbook_data["api_source_type"] = data.get("api_source_type")
        schedule_runbook(runbook_data,)

        return jsonify({"success": True, "runbook": result})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error":str(e),"trace": traceback.format_exc()}),500

def upload_file_object(file, user_id):
    try:
        temp_path = f"/tmp/{uuid.uuid4()}_{file.filename}"

        # save file locally
        file.save(temp_path)

        # upload to S3
        result = upload_any_file(
            file_path=temp_path,
            user_id=user_id,
            type="runbook"
        )

        # cleanup
        os.remove(temp_path)

        return result

    except Exception as e:
        return {"status": "error", "message": str(e)}
@runbook_bp.route("/runbook/results/<runbook_id>", methods=["GET"])
async def get_runbook_results(runbook_id):

    user_id = session.get("user_id")

    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401

    dbserver = LanceDBServer()

    results = await dbserver.get_runbook_results(user_id, runbook_id)

    return jsonify({
        "success": True,
        "results": results
    })

@runbook_bp.route("/runbooks/list/<user_id>", methods=["GET"])
async def list_runbooks(user_id):

    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401

    dbserver = LanceDBServer()
    runbooks = await dbserver.get_all_runbooks(user_id)

    return jsonify({"success": True, "runbooks": runbooks})


@runbook_bp.route("/runbook/<runbook_id>", methods=["GET"])
async def get_runbook(runbook_id):

    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401
    dbserver = LanceDBServer()
    runbook = await dbserver.get_runbook_by_id(user_id, runbook_id)

    return jsonify({"success": True, "runbook": runbook})


@runbook_bp.route("/runbook/delete/<runbook_id>", methods=["DELETE"])
async def delete_runbook(runbook_id):
    try:
        user_id = session.get("user_id")
        if not user_id:
            return jsonify({"error": "Unauthorized"}), 401
        dbserver = LanceDBServer()
        await dbserver.delete_runbook(user_id, runbook_id)

        return jsonify({"success": True}),200
    except Exception as e:
        return jsonify({"error":str(e)}),500

@runbook_bp.route("/runbook/update/<runbook_id>", methods=["POST"])
async def update_runbook_api(runbook_id):
    try:
        data = request.json or {}

        user_id = data.get("user_id")

        if not user_id:
            return jsonify({"error": "Unauthorized"}), 401

        dbserver = LanceDBServer()

        # Remove protected fields
        updates = {k: v for k, v in data.items() if k not in ["runbook_id", "user_id"]}

        updated = await dbserver.update_runbook(user_id, runbook_id, updates)

        return jsonify({
            "success": True,
            "runbook": updated
        }), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

async def _execute_global_endpoint_internal(
    user_id,
    app_id,
    endpoint_id,
    runtime_params=None,
    context=None,
):
    """
    Internal executor for GLOBAL app endpoints.

    runtime_params:
        {
            "base_url": "",
            "path": "",
            "method": "GET",
            "headers": {},
            "query_params": {},
            "path_params": {},
            "body": {},
            "timeout": 30,
            "config": {}   # auth override
        }
    """

    runtime_params = runtime_params or {}

    conn = connect_to_rds()
    cur = conn.cursor(pymysql.cursors.DictCursor)

    try:
        # ----------------------------------
        # 1️⃣ Validate endpoint exists
        # ----------------------------------
        cur.execute(
            """
            SELECT *
            FROM global_app_endpoints
            WHERE id = %s AND app_id = %s
            """,
            (endpoint_id, app_id),
        )
        endpoint = cur.fetchone()

        if not endpoint:
            raise ValueError("Endpoint not found")

        # ----------------------------------
        # 2️⃣ Check installed app
        # ----------------------------------
        cur.execute(
            """
            SELECT *
            FROM external_apps
            WHERE user_id = %s
              AND source_global_app_id = %s
              AND status = "active"
            """,
            (user_id, app_id),
        )
        installed_app = cur.fetchone()

        # ----------------------------------
        # 3️⃣ Extract runtime input
        # ----------------------------------
        frontend_base_url = runtime_params.get("base_url")
        path = runtime_params.get("path") or endpoint.get("path")
        method = runtime_params.get("method") or endpoint.get("method", "GET")

        frontend_headers = runtime_params.get("headers", {})
        query_params = runtime_params.get("query_params", {})
        path_params = runtime_params.get("path_params", {})
        request_body = runtime_params.get("body", {})
        timeout = runtime_params.get("timeout", 30)
        frontend_auth = runtime_params.get("config", {})

        if not path:
            raise ValueError("path is required")

        # ----------------------------------
        # 4️⃣ Resolve base URL
        # ----------------------------------
        if installed_app:
            base_url = installed_app.get("base_url") or frontend_base_url
        else:
            base_url = frontend_base_url

        if not base_url:
            raise ValueError("base_url required")

        # ----------------------------------
        # 5️⃣ Merge headers
        # ----------------------------------
        final_headers = {}

        if installed_app and installed_app.get("headers"):
            try:
                final_headers.update(json.loads(installed_app.get("headers") or "{}"))
            except Exception:
                pass

        final_headers.update(frontend_headers or {})

        # ----------------------------------
        # 6️⃣ Merge auth
        # ----------------------------------
        if installed_app and installed_app.get("auth_config"):
            try:
                final_auth = json.loads(installed_app.get("auth_config") or "{}")
            except Exception:
                final_auth = {}
        else:
            final_auth = {}

        if frontend_auth:
            final_auth.update(frontend_auth)

        # ----------------------------------
        # 7️⃣ Build final URL
        # ----------------------------------
        final_url = build_full_url(
            base_url=base_url,
            path=path,
            path_params=path_params,
        )

        # ----------------------------------
        # 8️⃣ Build config
        # ----------------------------------
        config = {
            "auth": final_auth,
            "request": {
                "url": final_url,
                "method": method,
                "headers": final_headers,
                "query_params": query_params,
                "body": request_body,
            },
            "timeout": timeout,
        }

        # ----------------------------------
        # 9️⃣ Execute
        # ----------------------------------
        connector = APIConnector(userid=user_id, config=config, context=context)
        result = connector.execute()

        return result

    finally:
        cur.close()
        conn.close()