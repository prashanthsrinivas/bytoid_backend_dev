import asyncio
import inspect
import json
import time
import pymysql
from agent_route.doc_clarity import QueryData
from apiConnector.helpers import _execute_endpoint_internal
from credits_route.route import Credits
from cust_helpers import pathconfig
from db.db_checkers import get_notes_data
from db.lance_db_service import LanceDBServer
from flask import Blueprint, jsonify, request
from db.rds_db import connect_to_rds
import uuid

# from radar.lang_maps import run_language_tests
from radar.radar_helpers import (
    _safe_json_parse,
    extract_file_payload,
    extract_json,
    process_file_payloads,
)
from services.redis_service import RedisService
from umail.routes import get_sorted_lance_emails
from utils.fireworkzz import (
    get_firework_embedding,
    get_think_fire_response2_og,
    get_think_fire_response2_og2,
)
import os
from utils.img_tokens import image_credit_cost
from utils.normal import load_yaml_file
from utils.base_logger import get_logger

radar_bp = Blueprint("radar", __name__)
RADAR_TEMPLATE = load_yaml_file(path=pathconfig.radar_prompts)
# print("RADAR_TEMPLATE type:", type(RADAR_TEMPLATE))
logger = get_logger(__name__)

# # Run tests
# run_language_tests()


@radar_bp.route("/radar/apps/list/<userid>", methods=["GET"])
def radarapp(userid):
    conn = connect_to_rds()
    cur = conn.cursor(pymysql.cursors.DictCursor)

    cur.execute(
        """
        SELECT
            a.id AS app_id,
            a.app_name,

            e.id   AS endpoint_id,
            e.name,
            e.path,
            e.updated_at

        FROM external_apps a
        LEFT JOIN external_app_endpoints e
            ON a.id = e.app_id
        WHERE a.user_id = %s
        ORDER BY a.id, e.id
    """,
        (userid,),
    )

    rows = cur.fetchall()
    apps = {}

    for row in rows:
        app_id = row["app_id"]

        if app_id not in apps:
            apps[app_id] = {"id": app_id, "app_name": row["app_name"], "endpoints": []}

        # Only add endpoint if it exists
        if row["endpoint_id"] is not None:
            endpoint = {
                "id": row["endpoint_id"],
                "name": row["name"],
                "path": row["path"],
                "updated_at": row["updated_at"],
            }

            apps[app_id]["endpoints"].append(endpoint)

    return jsonify(list(apps.values()))


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

    return data_for_review


from concurrent.futures import ThreadPoolExecutor

radar_executor = ThreadPoolExecutor(max_workers=4)


def run_radar_review_sync(
    user_id, job_id, data, date_uniqueid, btype, files=None, structure_file=None
):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        loop.run_until_complete(
            run_radar_review_redis(
                user_id,
                job_id,
                data,
                date_uniqueid,
                btype,
                files,
                structure_file,
            )
        )
    except Exception:
        logger.exception("Thread runner crashed")
    finally:
        loop.close()


import asyncio


async def merge_radar(raw_chunks, user_id, credits, output_language="english"):

    if not raw_chunks:
        return {}

    prompt = f"""
        You are a deterministic JSON merger.

        Combine ALL input JSON objects into ONE valid JSON.

        STRICT REQUIREMENTS:

        1. Keep ALL blocks from ALL chunks
        2. NEVER delete anything
        3. NEVER summarize
        4. NEVER rewrite
        5. ONLY merge
        6. Preserve exact content
        7. make sure that output language is {output_language}

        Final format:

        {{
        "document_meta": {{...}},
        "structure_rationale": "...",
        "blocks": [...],
        "estimated_word_count": number
        }}

        INPUT JSON:

        {json.dumps(raw_chunks, ensure_ascii=False)}

        OUTPUT ONLY VALID JSON.
        """

    response = await get_think_fire_response2_og(
        user_id=user_id,
        user_message=prompt,
        credits=credits,
        total_input_chars=len(prompt),
    )
    return json.loads(response)


import copy


def merge_radar_chunks_deterministic(raw_chunks, output_language="english"):

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
    }

    block_index = {}

    for chunk in raw_chunks:

        # document_meta merge safely
        if "document_meta" in chunk:
            merged["document_meta"].update(chunk["document_meta"])

        # structure rationale
        if chunk.get("structure_rationale") and not merged["structure_rationale"]:
            merged["structure_rationale"] = chunk["structure_rationale"]

        # analysis
        merged["analysis_depth"] = chunk.get("analysis_depth", merged["analysis_depth"])

        merged["analysis_depth_rationale"] = chunk.get(
            "analysis_depth_rationale", merged["analysis_depth_rationale"]
        )

        # recommendation
        merged["recommendation_depth"] = chunk.get(
            "recommendation_depth", merged["recommendation_depth"]
        )

        merged["recommendation_depth_rationale"] = chunk.get(
            "recommendation_depth_rationale",
            merged["recommendation_depth_rationale"],
        )

        # recommendation intent
        for intent in chunk.get("recommendation_intent", []):
            if intent not in merged["recommendation_intent"]:
                merged["recommendation_intent"].append(intent)

        # intent/core/confidence
        merged["intent_type"] = chunk.get("intent_type", merged["intent_type"])

        merged["core_objective"] = chunk.get("core_objective", merged["core_objective"])

        merged["confidence_level"] = (
            chunk.get("confidence_level")
            or chunk.get("document_meta", {}).get("confidence_level")
            or merged["confidence_level"]
        )

        # word count fix
        if "estimated_word_count" in chunk:
            merged["estimated_word_count"] += chunk["estimated_word_count"]
        elif "document_meta" in chunk:
            merged["estimated_word_count"] += chunk["document_meta"].get(
                "estimated_word_count", 0
            )

        # blocks merge
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

    # cleanup
    merged = {k: v for k, v in merged.items() if v not in [None, "", [], {}]}

    return merged


# Example usage
# merged_report = await merge_radar_chunks([chunk1, chunk2, chunk3], user_id, credits)
async def run_radar_review_redis(
    user_id,
    job_id,
    data,
    date_uniqueid,
    btype,
    files=None,
    structure_file=None,
    session_id=None,
):
    redis = RedisService()
    from runbook.utils import send
    from websockets_custom.ws_instance import ws_service, msg_builder_main

    msg_builder = msg_builder_main
    if not session_id:
        session_id = data.get("session_id")

    # ✅ single flag
    should_emit = bool(job_id and session_id)

    async def emit(msg):
        if should_emit:
            await send(ws_service, msg, user_id)

    job_key = f"radar:job:{job_id}"
    user_lock_key = f"radar:user_lock:{user_id}"

    review_temp = None
    conn = None

    try:

        # ---------------------------------
        # SAFE JOB UPDATE FUNCTION
        # ---------------------------------
        async def update(**kwargs):
            try:
                state = await redis.get(job_key)
                if not state:
                    return

                state.update(kwargs)

                if kwargs.get("status") in ("completed", "failed"):
                    state["ended_at"] = int(time.time())

                await redis.set(job_key, state, ex=7200)

            except Exception:
                logger.exception("❌ JOB REDIS UPDATE FAILED")

        # ---------------------------------
        # START
        # ---------------------------------
        await update(status="running")
        await emit(
            msg_builder.job_progress(
                job_id,
                session_id,
                "INIT",
                "started generating of report",
                10,
            )
        )
        logger.info("🚀 RADAR START job_id=%s", job_id)

        # ---------------------------------
        # DB CONNECTION
        # ---------------------------------
        await emit(
            msg_builder.job_progress(
                job_id,
                session_id,
                "Inputs",
                "checking input configurations",
                20,
            )
        )

        conn = connect_to_rds()
        dbserver = LanceDBServer()
        credits = Credits(db=conn)

        # ---------------------------------
        # INPUT EXTRACTION
        # ---------------------------------
        await emit(
            msg_builder.job_progress(
                job_id,
                session_id,
                "data",
                "processing the input data",
                25,
            )
        )

        userid = data.get("userid")
        name = data.get("name")
        user_analyze_input = data.get("analyze_input")

        main_source = data.get("main_source")
        data_sources = data.get("data_sources", {})
        reference_sources = data.get("reference_sources", {})
        refernce_main_source = data.get("refernce_main_source")

        INP_LINKS, STR_LINKS = [], []
        file_data_payload, structure_file_payload = [], []

        data_checked = []
        reference_RWA = []

        # ---------------------------------
        # FILE PROCESSING
        # ---------------------------------
        if files:
            await emit(
                msg_builder.job_progress(
                    job_id,
                    session_id,
                    "Files",
                    "processing the uploaded files",
                    30,
                )
            )

            process_file_payloads(
                user_id=user_id,
                files=files,
                inp_links=INP_LINKS,
                extracted_payload=file_data_payload,
            )

        if structure_file:
            await emit(
                msg_builder.job_progress(
                    job_id,
                    session_id,
                    "Files",
                    "processing the structure files",
                    35,
                )
            )

            process_file_payloads(
                user_id=user_id,
                files=[structure_file],
                inp_links=STR_LINKS,
                extracted_payload=structure_file_payload,
            )

        # ---------------------------------
        # LANGUAGE DETECTION
        # ---------------------------------
        await emit(
            msg_builder.job_progress(
                job_id,
                session_id,
                "query",
                "processing the query",
                40,
            )
        )

        lang_prompt = RADAR_TEMPLATE["language_wordcount_extractor"]
        lang_check = lang_prompt.replace("{{analyze_input}}", user_analyze_input or "")

        result = await get_think_fire_response2_og(
            user_message=lang_check,
            user_id=user_id,
            credits=credits,
            total_input_chars=len(lang_check),
        )

        langs_word = json.loads(result)
        output_language = langs_word.get("language", "English")
        output_word_count = langs_word.get("word_count")
        await emit(
            msg_builder.job_progress(
                job_id,
                session_id,
                "query",
                f"processing the report generation with {output_language} language",
                42,
            )
        )

        # ---------------------------------
        # STRUCTURE GENERATION
        # ---------------------------------
        if structure_file_payload:
            await emit(
                msg_builder.job_progress(
                    job_id,
                    session_id,
                    "structure generation",
                    f"Generating structure blueprint...",
                    45,
                )
            )

            structure_prompt_template = RADAR_TEMPLATE["structure_prompt_template"]

            base_struc_prompt = (
                structure_prompt_template.replace(
                    "{{document_file_data}}", json.dumps(structure_file_payload)
                )
                .replace("{{file_links}}", json.dumps(STR_LINKS))
                .replace(
                    "{{user_original_prompt_or_context}}", user_analyze_input or ""
                )
                .replace("{{output_language}}", output_language)
            )

            base_chars = len(base_struc_prompt)

            for img in STR_LINKS:
                base_chars -= len(img)
                base_chars += image_credit_cost(img)

            result = await get_think_fire_response2_og(
                user_message=base_struc_prompt,
                user_id=user_id,
                credits=credits,
                total_input_chars=base_chars,
            )

            structure_file_payload = json.loads(result)

        # ---------------------------------
        # EMBEDDING
        # ---------------------------------
        if main_source == "knowledge" or refernce_main_source == "knowledge":

            embedding = await get_firework_embedding()
            vector = embedding.embed_query(user_analyze_input)

            payload = QueryData(user_id=userid, embedding=vector, top_k=3)

            await credits.update_ai_credits_redis(
                user_id=userid,
                credit_type="embedding",
                total_chars=len(user_analyze_input),
                reference_id="embedding_generation",
            )
        else:
            payload = None

        # ---------------------------------
        # DATA RETRIEVAL
        # ---------------------------------
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

        if refernce_main_source and reference_sources:
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
        # LAST RESPONSE
        # ---------------------------------
        last_radar_response = ""

        if date_uniqueid:
            val = await dbserver.radar_get_review_last_response(
                user_id=user_id,
                radar_id=date_uniqueid,
            )

            if val:
                last_radar_response = json.dumps(val)
                if not output_word_count:
                    output_word_count = (
                        val.get("estimated_word_count")
                        or val.get("document_meta", {}).get("estimated_word_count")
                        or 800
                    )
                await emit(
                    msg_builder.job_progress(
                        job_id,
                        session_id,
                        "previous report",
                        "extracted old report",
                        55,
                    )
                )

        # ---------------------------------
        # TEMPLATE SELECTION
        # ---------------------------------
        if btype == "review":
            review_temp = (
                RADAR_TEMPLATE["radar_review_template_structure"]
                if structure_file_payload
                else RADAR_TEMPLATE["radar_review_template_no_structure"]
            )
        elif btype == "analyze":
            review_temp = (
                RADAR_TEMPLATE["radar_analysis_prompt_structure"]
                if structure_file_payload
                else RADAR_TEMPLATE["radar_analysis_prompt_no_structure"]
            )
        elif btype == "decide":
            review_temp = (
                RADAR_TEMPLATE["radar_recommendations_prompt_structure"]
                if structure_file_payload
                else RADAR_TEMPLATE["radar_recommendations_prompt_no_structure"]
            )

        # ---------------------------------
        # PROMPT BUILD
        # ---------------------------------
        await emit(
            msg_builder.job_progress(
                job_id,
                session_id,
                "report",
                "Generating Report",
                55,
            )
        )

        if not output_word_count:
            output_word_count = 800

        base_prompt = (
            review_temp.replace("{{analyze_input}}", user_analyze_input or "")
            .replace("{{file_data}}", json.dumps(file_data_payload))
            .replace("{{structure_file_data}}", json.dumps(structure_file_payload))
            .replace("{{file_links}}", json.dumps(INP_LINKS))
            .replace("{{data_sources}}", json.dumps(data_checked))
            .replace("{{reference_sources}}", json.dumps(reference_RWA))
            .replace("{{last_radar_response}}", last_radar_response)
            .replace("{{output_language}}", output_language)
            .replace("{{requested_word_count}}", str(output_word_count))
        )

        base_chars = len(base_prompt)

        for img in INP_LINKS:
            base_chars -= len(img)
            base_chars += image_credit_cost(img)

        # ---------------------------------
        # LLM EXECUTION
        # ---------------------------------
        result = await get_think_fire_response2_og2(
            user_message=base_prompt,
            user_id=user_id,
            credits=credits,
            total_input_chars=base_chars,
            language=output_language,
            words_count=output_word_count,
        )
        await emit(
            msg_builder.job_progress(
                job_id,
                session_id,
                "report generation",
                "generated content for report",
                70,
            )
        )

        merged_report = merge_radar_chunks_deterministic(raw_chunks=result)
        await emit(
            msg_builder.job_progress(
                job_id,
                session_id,
                "report generation",
                "merging content for report",
                75,
            )
        )
        refactor_result = _safe_json_parse(merged_report)

        # ---------------------------------
        # SAVE RESULT
        # ---------------------------------
        await emit(
            msg_builder.job_progress(
                job_id,
                session_id,
                "report generation",
                "saving report ",
                90,
            )
        )

        if refactor_result:
            await dbserver.radar_upsert_review(
                user_id=user_id,
                name=name,
                radar_id=date_uniqueid,
                review_id=job_id,
                user_input=user_analyze_input,
                new_result=refactor_result,
                status="completed",
                main_source=main_source,
                data_sources=data_sources,
                reference_sources=reference_sources,
                refernce_main_source=refernce_main_source,
            )

        await update(status="completed", result=refactor_result)
        await emit(
            msg_builder.job_success(
                job_id,
                session_id,
                "✅ Radar report generated successfully ",
            )
        )

    except Exception:
        logger.exception("❌ RADAR FAILED job_id=%s", job_id)

        await update(status="failed", error="Error Occured in Generation")

        await send("❌ Radar generation failed", "failed", 0, "error")

    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                logger.exception("❌ DB CLOSE FAILED")


async def create_radar_job(userid, data, mode):

    redis = RedisService()

    now = int(time.time())
    job_id = f"{mode}_{uuid.uuid4().hex[:8]}"
    date_uniqueid = data.get("id") or f"radar_{now}"

    job_key = f"radar:job:{job_id}"
    user_lock_key = f"radar:user_lock:{userid}"

    # check lock
    # existing = await redis.get(user_lock_key)

    # if existing:
    #     return None, existing

    # extract files
    files_data = None
    json_files = data.get("files")

    if isinstance(json_files, list):
        files_data = [
            extract_file_payload(f, default_filename=f"file_{i}")
            for i, f in enumerate(json_files)
            if extract_file_payload(f)
        ] or None

    structure_file_data = None
    if data.get("structure_file"):
        structure_file_data = extract_file_payload(
            data.get("structure_file"),
            default_filename="structure_file",
        )

    if not (files_data or data.get("data_sources") or data.get("reference_sources")):
        return None, {"error": "At least one input source required"}

    state = {
        "job_id": job_id,
        "id": date_uniqueid,
        "review_id": job_id,
        "userid": userid,
        "mode": mode,
        "status": "pending",
        "user_input": data.get("analyze_input"),
        "name": data.get("name"),
        "files_data": files_data,
        "structure_file_data": structure_file_data,
        "data_sources": data.get("data_sources"),
        "reference_sources": data.get("reference_sources"),
        "main_source": data.get("main_source"),
        "reference_main_source": data.get("reference_main_source"),
        "result": None,
        "error": None,
        "started_at": now,
        "ended_at": None,
    }

    await redis.set(job_key, state, ex=7200)
    await redis.set(user_lock_key, {"job_id": job_id}, ex=1800)

    return job_id, state


def group_radars(rows: list[dict]):
    grouped = {}

    for row in rows:
        radar_id = row.get("id")
        if not radar_id:
            continue

        if radar_id not in grouped:
            grouped[radar_id] = {
                "id": radar_id,
                "name": row.get("name"),
                "reviews": [],
            }

        grouped[radar_id]["reviews"].append(
            {
                "review_id": row.get("review_id"),
                "user_input": row.get("user_input"),
                "status": row.get("status"),
                "started_at": row.get("started_at", 0),
            }
        )

    # 🔽 Sort reviews inside each radar (latest first)
    for radar in grouped.values():
        radar["reviews"].sort(key=lambda r: r["started_at"], reverse=True)

    # 🔽 Sort radars themselves by latest activity
    return sorted(
        grouped.values(),
        key=lambda r: r["reviews"][0]["started_at"],
        reverse=True,
    )


@radar_bp.route("/radar/reviews/<userid>", methods=["GET"])
async def list_radar_reviews(userid):
    dbserver = LanceDBServer()
    rows = await dbserver.radar_get_review_index(userid)
    return jsonify(group_radars(rows))


@radar_bp.route("/radar/docs", methods=["POST"])
async def get_radar_doc_byid():
    data = request.get_json(force=True)
    userid = data.get("userid")
    id = data.get("id")
    docid = data.get("docid")
    dbserver = LanceDBServer()
    data = await dbserver.radar_get_review(user_id=userid, review_id=docid)
    return jsonify(data)


@radar_bp.route("/radar/review", methods=["POST"])
async def radar_review():

    data = request.get_json()

    userid = data.get("userid")

    if not userid:
        return jsonify({"error": "userid required"}), 400

    job_id, state = await create_radar_job(userid, data, "review")

    if not job_id:

        return jsonify({"error": "Another job running", "job": state}), 409

    radar_executor.submit(
        run_radar_review_sync,
        userid,
        job_id,
        data,
        state["id"],
        "review",
        state.get("files_data"),
        state.get("structure_file_data"),
    )

    return jsonify(state)


@radar_bp.route("/radar/analyze", methods=["POST"])
async def radar_analyze():

    data = request.get_json()

    userid = data.get("userid")

    job_id, state = await create_radar_job(userid, data, "analyze")

    if not job_id:
        return jsonify(state), 409

    radar_executor.submit(
        run_radar_review_sync,
        userid,
        job_id,
        data,
        state["id"],
        "analyze",
        state.get("files_data"),
        state.get("structure_file_data"),
    )

    return jsonify(state)


@radar_bp.route("/radar/decide", methods=["POST"])
async def radar_decide():

    data = request.get_json()

    userid = data.get("userid")

    job_id, state = await create_radar_job(userid, data, "decide")

    if not job_id:
        return jsonify(state), 409

    radar_executor.submit(
        run_radar_review_sync,
        userid,
        job_id,
        data,
        state["id"],
        "decide",
        state.get("files_data"),
        state.get("structure_file_data"),
    )

    return jsonify(state)


@radar_bp.route("/radar/status", methods=["GET"])
async def radar_status():

    job_id = request.args.get("job_id")

    if not job_id:
        return jsonify({"error": "job_id required"}), 400

    redis = RedisService()

    job = await redis.get(f"radar:job:{job_id}")

    if not job:
        return jsonify({"job_id": job_id, "status": "not_found"}), 404

    return jsonify(job)


@radar_bp.route("/radar/current", methods=["GET"])
async def radar_current():

    userid = request.args.get("userid")

    redis = RedisService()

    job_id = await redis.get(f"radar:user_lock:{userid}")

    if not job_id:
        return jsonify({"status": "idle"})

    job = await redis.get(f"radar:job:{job_id}")

    return jsonify(job)


@radar_bp.route("/radar/changeblock", methods=["POST"])
async def radar_change_block_preview():
    data = request.get_json(force=True)

    user_id = data.get("userid")
    review_id = data.get("review_id")
    result_id = data.get("result_id")
    block_id = data.get("block_id")
    micro_block = data.get("micro_id")
    user_requested_change = data.get("user_input")
    credits = Credits()

    if not user_id or not block_id or not user_requested_change:
        return (
            jsonify({"error": "userid, block_id, and user_input are required"}),
            400,
        )

    # 🔥 Either result_id OR review_id must be present
    if not (result_id or review_id):
        return (
            jsonify({"error": "Either result_id or review_id must be provided"}),
            400,
        )

    dbserver = LanceDBServer()
    record = None
    original_json = None
    if review_id:
        record = await dbserver.radar_get_review(user_id=user_id, review_id=review_id)

        if not record or not record.get("result"):
            return jsonify({"error": "RADAR review not found"}), 404

        original_json = record["result"]
    elif result_id:
        record = await dbserver.runbook_get_result(user_id=user_id, result_id=result_id)

        if not record or not record.get("result"):
            return jsonify({"error": "runbook review result not found"}), 404

        original_json = record["result"]
    else:
        return jsonify({"error": "Either review_id or result_id is required"}), 400

    try:
        review_temp = RADAR_TEMPLATE["radar_change_block_prompt"]
    except KeyError:
        return jsonify({"error": "Missing radar_change_block_prompt template"}), 500

    prompt = (
        review_temp.replace("{{original_json}}", json.dumps(original_json, indent=2))
        .replace("{{block_change}}", block_id)
        .replace("{{microblock}}", micro_block or "")
        .replace("{{user_requested_change}}", user_requested_change)
    )

    try:
        llm_response = await get_think_fire_response2_og(
            user_message=prompt,
            user_id=user_id,
            credits=credits,
        )

        if llm_response == "INSUFFICIENT":
            return jsonify({"error": "Insufficient AI credits"}), 402

        # Try strict parse first, then extract JSON from text
        payload = _safe_json_parse(llm_response)
        if not payload:
            try:
                json_text = extract_json(llm_response)
                payload = json.loads(json_text)
            except Exception as parse_err:
                return (
                    jsonify(
                        {
                            "error": "Invalid model response",
                            "details": str(parse_err),
                            "raw_preview": llm_response[:1000],
                        }
                    ),
                    502,
                )

        # If model rejects the change, pass it through cleanly
        if payload.get("status") in {"rejected", "error"}:
            return jsonify(payload), 400

        required_keys = {"block_id", "block_type", "changed_block"}
        if not required_keys.issubset(payload.keys()):
            return (
                jsonify(
                    {
                        "error": "Invalid model response",
                        "details": "Missing required preview keys",
                        "raw_preview": llm_response[:1000],
                    }
                ),
                502,
            )

        return jsonify(payload)

    except Exception as e:
        return (
            jsonify({"error": "Failed to generate block preview", "details": str(e)}),
            500,
        )


@radar_bp.route("/radar/changeblock/confirm", methods=["POST"])
async def radar_change_block_confirm():
    data = request.get_json(force=True)

    user_id = data.get("userid")
    review_id = data.get("review_id")
    result_id = data.get("result_id")
    block_id = data.get("block_id")
    micro_block = data.get("micro_id")
    changed_block = data.get("changed_block")

    if not user_id or not block_id or not changed_block:
        return (
            jsonify({"error": "userid, block_id, and user_input are required"}),
            400,
        )

    # 🔥 Either result_id OR review_id must be present
    if not (result_id or review_id):
        return (
            jsonify({"error": "Either result_id or review_id must be provided"}),
            400,
        )
    dbserver = LanceDBServer()
    record = None
    updated_json = None
    if review_id:
        record = await dbserver.radar_get_review(user_id=user_id, review_id=review_id)

        if not record or not record.get("result"):
            return jsonify({"error": "RADAR review not found"}), 404

        updated_json = record["result"]
    elif result_id:
        record = await dbserver.runbook_get_result(user_id=user_id, result_id=result_id)

        if not record or not record.get("result"):
            return jsonify({"error": "Runbook review result not found"}), 404

        updated_json = record["result"]
    else:
        return jsonify({"error": "Either review_id or result_id is required"}), 400

    # 🔧 Safe deterministic merge
    block_found = False

    for block in updated_json.get("blocks", []):
        if block.get("block_id") == block_id:
            if micro_block:
                for micro in block.get("micro_blocks", []):
                    if micro.get("micro_id") == micro_block:
                        micro.update(changed_block)
                        block_found = True
                        break
            else:
                block.update(changed_block)
                block_found = True

        if block_found:
            break

    if not block_found:
        return jsonify({"error": "Target block or micro-block not found"}), 404

    try:
        if review_id:
            await dbserver.radar_update_result(
                user_id=user_id,
                review_id=review_id,
                new_result=updated_json,
            )
        elif result_id:
            await dbserver.update_runbook_result(
                user_id=user_id,
                result_id=result_id,
                new_result=updated_json,
            )

        return jsonify(
            {
                "status": "ok",
                "message": "RADAR block updated successfully",
                "block_id": block_id,
                "micro_id": micro_block or None,
            }
        )

    except Exception as e:
        return (
            jsonify({"error": "Failed to persist RADAR update", "details": str(e)}),
            500,
        )


@radar_bp.route("/radar/knowledge/analyze", methods=["POST"])
async def radar_knowledge_analyze():
    import os
    import inspect

    data = request.get_json(force=True)

    # 🔹 Input
    userid = data.get("userid")
    name = data.get("name")
    user_analyze_input = data.get("analyze_input")
    data_sources = data.get("data_sources", {})

    if not userid or not user_analyze_input:
        return jsonify({"error": "userid and analyze_input are required"}), 400

    # 🔹 Files & dynamic top_k logic
    filenames = data_sources.get("filenames", [])
    file_count = len(filenames)
    credits = Credits()

    # 1 file → depth, many files → precision
    top_k = 3 if file_count == 1 else 2  # 🔥 improved

    # 🔹 Create embedding
    embedding = await get_firework_embedding()
    vector = embedding.embed_query(user_analyze_input)

    dbserver = LanceDBServer()

    total_output_chars = len(vector)
    total_chars = len(user_analyze_input) + total_output_chars

    await credits.update_ai_credits_redis(
        user_id=userid,
        credit_type="embedding",
        total_chars=total_chars,
        reference_id=inspect.stack()[0].function,
    )

    payload = QueryData(user_id=userid, embedding=vector, top_k=top_k)

    data_for_review = []

    # 🔹 Knowledge retrieval
    for file in filenames:
        ftype = file.get("type")

        # 📄 DOCS
        if ftype == "docs":
            fname = file.get("filename")

            results = await dbserver.query_vector_filename(
                query=payload, filename=fname
            )

            for item in results or []:
                data_for_review.append(
                    {
                        "type": "docs",
                        "source": fname,
                        "text": item.get("text", ""),
                        "distance": item.get("_distance") or item.get("score"),
                    }
                )

        # 🔊 AUDIO
        elif ftype == "aud":
            bfname = file.get("filename")
            base = os.path.basename(bfname)
            name_without_ext = os.path.splitext(base)[0]
            transcript_name = f"{name_without_ext}_transcript.json"

            results = await dbserver.rec_query_vector_foldername(
                query=payload, foldername=transcript_name
            )

            for item in results or []:
                data_for_review.append(
                    {
                        "type": "audio",
                        "source": transcript_name,
                        "text": item.get("text", ""),
                        "distance": item.get("_distance") or item.get("score"),
                    }
                )

        # 🌐 SCRAPE
        elif ftype == "scrape":
            url = file.get("url")

            result = dbserver.search_scraped_data_by_url(query=payload, url=url)

            if result:
                data_for_review.append(
                    {
                        "type": "scrape",
                        "source": url,
                        "text": result.get("text", ""),
                        "distance": result.get("_distance") or result.get("score"),
                    }
                )

    # 🔹 No data found
    if not data_for_review:
        return (
            jsonify(
                {
                    "userid": userid,
                    "query": user_analyze_input,
                    "answer": None,
                    "message": "No relevant data found",
                }
            ),
            400,
        )

    # 🔥 Sort & take top results
    sorted_results = sorted(
        data_for_review, key=lambda x: x.get("distance", float("inf"))
    )[:top_k]

    # 🔥 Combine context (VERY IMPORTANT)
    combined_context = "\n\n".join(
        [r.get("text", "") for r in sorted_results if r.get("text")]
    )

    best_result = sorted_results[0]

    similarity = (
        round(1 - best_result["distance"], 4)
        if best_result.get("distance") is not None
        else None
    )

    # 🔥 FINAL PROMPT
    prompt = f"""
        You are a highly accurate AI answer generator.

        USER QUERY:
        {user_analyze_input}

        CONTEXT:
        {combined_context}

        INSTRUCTIONS:
        - Answer ONLY using the provided context.
        - Do NOT hallucinate or add external knowledge.
        - never invent or create answer.
        - If the answer is not clearly present, say something politely as we cant find the query in the documents

        STYLE RULES:
        - short → 1-2 lines
        - medium → 3-5 lines
        - detailed → structured explanation with bullet points

        - Prefer exact phrases from context when possible.
        - If multiple points exist, summarize clearly.

        OUTPUT:
        Return only the final answer.
        """

    response = await get_think_fire_response2_og(
        user_id=userid,
        user_message=prompt,
        credits=credits,
        total_input_chars=len(prompt),
    )

    ai_made = response
    if len(response) > 2:
        ai_made = response
    else:
        ai_made = best_result.get("text")

    return (
        jsonify(
            {
                "userid": userid,
                "query": user_analyze_input,
                "answer": ai_made,
                "source": {
                    "type": best_result.get("type"),
                    "reference": best_result.get("source"),
                },
                "meta": {
                    "files_processed": file_count,
                    "top_k_used": top_k,
                    "distance": best_result.get("distance"),
                    "similarity": similarity,
                },
            }
        ),
        200,
    )


@radar_bp.route("/radar/delete", methods=["POST"])
async def delete_radar_files():
    data = request.get_json(force=True)

    user_id = data.get("user_id")
    review_id = data.get("review_id")

    dbserver = LanceDBServer()
    result = await dbserver.radar_delete_review(user_id, review_id)

    if not result["deleted"]:
        return jsonify({"message": "review not found", "details": result}), 404

    return jsonify({"message": "file deleted successfully", "details": result}), 200
