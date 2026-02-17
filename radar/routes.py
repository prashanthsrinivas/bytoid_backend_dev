import asyncio
import base64
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
from radar.radar_helpers import (
    _safe_json_parse,
    build_file_data_payload,
    extract_file_payload,
    extract_files_content,
    extract_json,
    process_file_payloads,
)
from services.redis_service import RedisService
from umail.routes import get_sorted_lance_emails
from utils.fireworkzz import get_firework_embedding, get_think_fire_response2_og
import os, io
from utils.img_tokens import image_credit_cost
from utils.normal import load_yaml_file
from utils.s3_utils import upload_any_file_and_get_url
from utils.base_logger import get_logger

radar_bp = Blueprint("radar", __name__)
RADAR_TEMPLATE = load_yaml_file(path=pathconfig.radar_prompts)

logger = get_logger(__name__)


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
    user_id, review_id, data, date_uniqueid, type, files=None, structure_file_data=None
):
    asyncio.run(
        run_radar_review_redis(
            user_id,
            review_id,
            data,
            date_uniqueid,
            type,
            files=files,
            structure_file=structure_file_data,
        )
    )


async def run_radar_review_redis(
    user_id, review_id, data, date_uniqueid, btype, files=None, structure_file=None
):
    redis = RedisService()

    if btype == "review":
        key = f"radar:review:{user_id}"
        review_temp = RADAR_TEMPLATE["radar_review_template"]
    elif btype == "analyze":
        key = f"radar:analyze:{user_id}"
        review_temp = RADAR_TEMPLATE["radar_analysis_prompt"]
    elif btype == "decide":
        key = f"radar:decide:{user_id}"
        review_temp = RADAR_TEMPLATE["radar_recommendations_prompt"]

    async def update(**kwargs):
        state = await redis.get(key)
        if not state:
            return
        if state.get("review_id") != review_id or state.get("id") != date_uniqueid:
            return
        state.update(kwargs)
        await redis.set(key, state, ex=1800)

    conn = None
    data_checked = []
    reference_RWA = []
    conn = connect_to_rds()
    dbserver = LanceDBServer()
    credits = Credits(db=conn)

    try:
        await update(status="running")
        logger.info("🚀 RADAR START %s", review_id)

        userid = data.get("userid")
        name = data.get("name")
        user_analyze_input = data.get("analyze_input")
        main_source = data.get("main_source")
        data_sources = data.get("data_sources", {})
        reference_sources = data.get("reference_sources", {})
        refernce_main_source = data.get("refernce_main_source")

        # ---- Unified file handling ----
        INP_LINKS = []  # image URLs
        STR_LINKS = []
        file_data_payload = []  # extracted document content
        structure_file_payload = []  # extracted structure content
        if files:
            process_file_payloads(
                user_id=user_id,
                files=files,
                inp_links=INP_LINKS,
                extracted_payload=file_data_payload,
            )

        # ---- Structure file (same logic, separate bucket) ----
        if structure_file:
            process_file_payloads(
                user_id=user_id,
                files=[structure_file],
                inp_links=STR_LINKS,
                extracted_payload=structure_file_payload,
            )
        if structure_file_payload:
            structure_prompt_template = RADAR_TEMPLATE["structure_prompt_template"]
            base_struc_prompt = (
                structure_prompt_template.replace(
                    "{{document_file_data}}", json.dumps(structure_file_payload) or ""
                )
                .replace(
                    "{{file_links}}",
                    json.dumps(STR_LINKS, indent=2) if STR_LINKS else "",
                )
                .replace(
                    "{{user_original_prompt_or_context}}", user_analyze_input or ""
                )
            )
            base_str_chars = len(base_struc_prompt)
            if STR_LINKS:
                for img in STR_LINKS:
                    base_str_chars -= len(img)
                    tokens = image_credit_cost(img)
                    base_str_chars += tokens

            result = await get_think_fire_response2_og(
                user_message=base_struc_prompt,
                user_id=user_id,
                credits=credits,
                total_input_chars=base_str_chars,
            )

            structure_blueprint = json.loads(result)
            structure_file_payload = structure_blueprint

        payload = None
        if (main_source and main_source == "knowledge") or (
            refernce_main_source and refernce_main_source == "knowledge"
        ):
            embedding = await get_firework_embedding()
            vector = embedding.embed_query(user_analyze_input)

            payload = QueryData(user_id=userid, embedding=vector, top_k=3)

            total_chars = len(user_analyze_input) + len(vector)
            await credits.update_ai_credits_redis(
                user_id=userid,
                credit_type="embedding",
                total_chars=total_chars,
                reference_id=inspect.stack()[0].function,
            )
        if main_source:

            data_checked = await retreval_from_sources(
                conn, dbserver, main_source, data_sources, userid, payload
            )
        if refernce_main_source:
            reference_RWA = await retreval_from_sources(
                conn, dbserver, refernce_main_source, reference_sources, userid, payload
            )

        last_radar_response = ""
        if date_uniqueid:
            val = await dbserver.radar_get_review_last_response(
                user_id=user_id, radar_id=date_uniqueid
            )
            last_radar_response = json.dumps(val) if val else ""

        base_prompt = (
            review_temp.replace("{{analyze_input}}", user_analyze_input)
            .replace("{{file_data}}", json.dumps(file_data_payload))
            .replace("{{structure_file_data}}", json.dumps(structure_file_payload))
            .replace("{{file_links}}", json.dumps(INP_LINKS, indent=2))
            .replace("{{data_sources}}", json.dumps(data_checked, indent=2))
            .replace("{{reference_sources}}", json.dumps(reference_RWA or {}, indent=2))
            .replace("{{last_radar_response}}", last_radar_response)
        )

        # print("file links", INP_LINKS)
        logger.info("file data %s", len(file_data_payload))
        logger.info("data sources %s", len(data_checked))
        # base_chars = len(base_prompt) - len(INP_LINKS)
        # base_chars += 100 * len(INP_LINKS)
        base_chars = len(base_prompt)
        if INP_LINKS:
            for img in INP_LINKS:
                base_chars -= len(img)
                tokens = image_credit_cost(img)
                base_chars += tokens

        result = await get_think_fire_response2_og(
            user_message=base_prompt,
            user_id=userid,
            credits=credits,
            total_input_chars=base_chars,
        )
        if result == "INSUFFICIENT":
            await update(status="failed", error="Insufficient credits")

        refactor_result = _safe_json_parse(result)

        if not name or name.strip() in ("", "Untitled Report"):
            name = refactor_result.get("document_meta", {}).get("persona", review_id)

        if refactor_result:
            await dbserver.radar_upsert_review(
                user_id=user_id,
                name=name,
                radar_id=date_uniqueid,
                review_id=review_id,
                user_input=user_analyze_input,
                new_result=refactor_result,
                status="completed",
                main_source=main_source,
                data_sources=data_sources,
                reference_sources=reference_sources,
                refernce_main_source=refernce_main_source,
            )

            print("✅ RADAR DONE %s", review_id)
            await update(status="completed", result=refactor_result)

    except Exception as e:
        print("❌ RADAR ERROR", review_id, str(e))
        await update(status="failed", error=str(e))

    finally:
        if conn:
            conn.close()


@radar_bp.route("/radar/review", methods=["POST"])
async def radar_review():

    data = request.get_json(silent=True)

    userid = data.get("userid")
    if not userid:
        return jsonify({"error": "userid is required"}), 400

    files_data = []

    json_files = data.get("files")
    structure_file = data.get("structure_file")

    # -------------------------------
    # Parse normal files
    # -------------------------------
    if isinstance(json_files, list):
        for idx, file_item in enumerate(json_files):
            extracted = extract_file_payload(
                file_item,
                default_filename=f"file_{idx}",
            )
            if extracted:
                files_data.append(extracted)

    if not files_data:
        files_data = None

    # -------------------------------
    # Parse structure_file (single)
    # -------------------------------
    structure_file_data = extract_file_payload(
        structure_file,
        default_filename="structure_file",
    )

    data_sources = data.get("data_sources")
    reference_sources = data.get("reference_sources")

    has_files = bool(files_data)
    has_data_sources = bool(data_sources)
    has_reference_sources = bool(reference_sources)

    if not (has_files or has_data_sources or has_reference_sources):
        return (
            jsonify(
                {
                    "error": (
                        "At least one input source is required. "
                        "Provide either files, data_sources, or reference_sources."
                    )
                }
            ),
            400,
        )

    # ---- Redis logic unchanged ----
    redis = RedisService()
    key = f"radar:review:{userid}"

    existing = await redis.get(key)
    now = int(time.time())

    if existing:
        status = existing.get("status")
        started_at = existing.get("started_at", now)

        if status == "pending" and now - started_at <= 600:
            return jsonify(existing)

        if status == "running":
            if now - started_at <= 600:
                return jsonify(existing)

            existing.update(
                {
                    "status": "failed",
                    "error": "Review execution timed out",
                    "ended_at": now,
                }
            )
            await redis.delete(key)
            return jsonify(existing)

        if status in ("completed", "failed"):
            await redis.delete(key)
            return jsonify(existing)

    # ---- Create new job ----
    review_id = f"review_{uuid.uuid4().hex[:8]}"
    date_uniqueid = data.get("id") or f"radar_{now}"

    state = {
        "review_id": review_id,
        "id": date_uniqueid,
        "user_input": data.get("analyze_input"),
        "name": data.get("name"),
        "status": "pending",
        "result": None,
        "error": None,
        "started_at": now,
        "main_source": data.get("main_source"),
        "data_sources": data_sources,
        "reference_sources": reference_sources,
        "refernce_main_source": data.get("refernce_main_source"),
    }

    await redis.set(key, state, ex=1800)

    # ---- Submit background task ----
    radar_executor.submit(
        run_radar_review_sync,
        userid,
        review_id,
        data,
        date_uniqueid,
        "review",
        files_data,
        structure_file_data,  # ✅ may be None
    )

    return jsonify(state)


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


@radar_bp.route("/radar/analyze", methods=["POST"])
async def radar_analyze():

    data = request.get_json(silent=True) or {}

    userid = data.get("userid")
    if not userid:
        return jsonify({"error": "userid is required"}), 400

    files_data = []

    json_files = data.get("files")
    structure_file = data.get("structure_file")

    # -------------------------------
    # Parse normal files
    # -------------------------------
    if isinstance(json_files, list):
        for idx, file_item in enumerate(json_files):
            extracted = extract_file_payload(
                file_item,
                default_filename=f"file_{idx}",
            )
            if extracted:
                files_data.append(extracted)

    if not files_data:
        files_data = None

    # -------------------------------
    # Parse structure_file (single)
    # -------------------------------
    structure_file_data = extract_file_payload(
        structure_file,
        default_filename="structure_file",
    )

    data_sources = data.get("data_sources")
    reference_sources = data.get("reference_sources")

    has_files = bool(files_data)
    has_data_sources = bool(data_sources)
    has_reference_sources = bool(reference_sources)

    if not (has_files or has_data_sources or has_reference_sources):
        return (
            jsonify(
                {
                    "error": (
                        "At least one input source is required. "
                        "Provide either files, data_sources, or reference_sources."
                    )
                }
            ),
            400,
        )

    redis = RedisService()
    key = f"radar:analyze:{userid}"

    existing = await redis.get(key)
    now = int(time.time())

    if existing:
        status = existing.get("status")
        started_at = existing.get("started_at", now)

        if status == "pending" and now - started_at <= 600:
            return jsonify(existing)

        if status == "running":
            if now - started_at <= 600:
                return jsonify(existing)

            existing.update(
                {
                    "status": "failed",
                    "error": "Analyze execution timed out",
                    "ended_at": now,
                }
            )
            await redis.delete(key)
            return jsonify(existing)

        if status in ("completed", "failed"):
            await redis.delete(key)
            return jsonify(existing)

    # ---- Create new job ----
    review_id = f"analyze_{uuid.uuid4().hex[:8]}"
    date_uniqueid = data.get("id") or f"radar_{now}"

    state = {
        "review_id": review_id,
        "id": date_uniqueid,
        "user_input": data.get("analyze_input"),
        "name": data.get("name"),
        "status": "pending",
        "result": None,
        "error": None,
        "started_at": now,
        "main_source": data.get("main_source"),
        "data_sources": data_sources,
        "reference_sources": reference_sources,
        "refernce_main_source": data.get("refernce_main_source"),
    }

    await redis.set(key, state, ex=1800)

    radar_executor.submit(
        run_radar_review_sync,
        userid,
        review_id,
        data,
        date_uniqueid,
        "analyze",
        files_data,
        structure_file_data,  # ✅ consistent with review
    )

    return jsonify(state)


@radar_bp.route("/radar/decide", methods=["POST"])
async def radar_decide():
    if request.is_json:
        data = request.get_json(silent=True) or {}
    else:
        data = request.form or {}
    # print("the input request", data)
    userid = data.get("userid")

    if not userid:
        return jsonify({"error": "userid is required"}), 400

    # ---- READ FILES INSIDE REQUEST CONTEXT (CRITICAL FIX) ----
    files_data = []
    json_files = data.get("files")
    structure_file = data.get("structure_file")

    if isinstance(json_files, list):
        for idx, file_item in enumerate(json_files):
            if not file_item:
                continue

            # Case A: frontend sends full data URL string
            if isinstance(file_item, str) and file_item.startswith("data:"):
                header, b64data = file_item.split(",", 1)
                content_type = header.split(";")[0].replace("data:", "")

                files_data.append(
                    {
                        "filename": f"file_{idx}",
                        "content_type": content_type,
                        "data": base64.b64decode(b64data),
                    }
                )

            # Case B: structured JSON object
            elif isinstance(file_item, dict) and "data" in file_item:
                files_data.append(
                    {
                        "filename": file_item.get("filename", f"file_{idx}"),
                        "content_type": file_item.get("content_type"),
                        "data": base64.b64decode(file_item["data"]),
                    }
                )
    # 3️⃣ structure_file (single file)
    if structure_file:
        # Case A: frontend sends full data URL string
        if isinstance(structure_file, str) and structure_file.startswith("data:"):
            header, b64data = structure_file.split(",", 1)
            content_type = header.split(";")[0].replace("data:", "")

            files_data.append(
                {
                    "filename": "structure_file",
                    "content_type": content_type,
                    "data": base64.b64decode(b64data),
                    "role": "structure",  # optional but VERY useful
                }
            )

        # Case B: structured JSON object
        elif isinstance(structure_file, dict) and "data" in structure_file:
            files_data.append(
                {
                    "filename": structure_file.get("filename", "structure_file"),
                    "content_type": structure_file.get("content_type"),
                    "data": base64.b64decode(structure_file["data"]),
                    "role": "structure",
                }
            )

    if not files_data:
        files_data = None
    redis = RedisService()
    key = f"radar:decide:{userid}"

    existing = await redis.get(key)
    now = int(time.time())

    if existing:
        status = existing.get("status")
        started_at = existing.get("started_at", now)

        # ⛔ Pending but still valid → return it
        if status == "pending" and now - started_at <= 600:
            return jsonify(existing)

        # ⛔ Running → always return it
        if status == "running":
            # 🕒 Check for running timeout
            if now - started_at <= 600:
                return jsonify(existing)

            # ⛔ Running but timed out → mark failed
            existing.update(
                {
                    "status": "failed",
                    "error": "Review execution timed out",
                    "ended_at": now,
                }
            )
            await redis.delete(key)  # short TTL for failed
            return jsonify(existing)

        # ⛔ Completed → return same result
        if status == "completed":
            await redis.delete(key)
            return jsonify(existing)

        # ❌ Failed OR stale pending → overwrite
        if status == "failed":
            await redis.delete(key)
            return jsonify(existing)

        # create new below as pending gone too overtime
        if status == "pending" and now - started_at > 600:
            pass

    # ✅ Create NEW review
    review_id = f"decide_{str(uuid.uuid4().hex[:8])}"
    date_uniqueid = data.get("id") or f"radar_{now}"

    state = {
        "review_id": review_id,
        "id": date_uniqueid,
        "user_input": data.get("analyze_input"),
        "name": data.get("name"),
        "status": "pending",
        "result": None,
        "error": None,
        "started_at": now,
        "main_source": data.get("main_source"),
        "data_sources": data.get("data_sources", {}),
        "reference_sources": data.get("reference_sources", {}),
        "refernce_main_source": data.get("refernce_main_source"),
    }

    await redis.set(key, state, ex=1800)

    radar_executor.submit(
        run_radar_review_sync,
        userid,
        review_id,
        data,
        date_uniqueid,
        "decide",
        files_data,
    )

    return jsonify(state)


@radar_bp.route("/radar/changeblock", methods=["POST"])
async def radar_change_block_preview():
    data = request.get_json(force=True)

    user_id = data.get("userid")
    review_id = data.get("review_id")
    block_id = data.get("block_id")
    micro_block = data.get("micro_id")
    user_requested_change = data.get("user_input")
    credits = Credits()

    if not user_id or not review_id or not block_id or not user_requested_change:
        return (
            jsonify(
                {"error": "userid, review_id, block_id, and user_input are required"}
            ),
            400,
        )

    dbserver = LanceDBServer()
    record = await dbserver.radar_get_review(user_id=user_id, review_id=review_id)

    if not record or not record.get("result"):
        return jsonify({"error": "RADAR review not found"}), 404

    original_json = record["result"]

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
    block_id = data.get("block_id")
    micro_block = data.get("micro_id")
    changed_block = data.get("changed_block")

    if not user_id or not review_id or not block_id or not changed_block:
        return (
            jsonify(
                {"error": "userid, review_id, block_id, and changed_block are required"}
            ),
            400,
        )

    dbserver = LanceDBServer()

    record = await dbserver.radar_get_review(user_id=user_id, review_id=review_id)

    if not record or not record.get("result"):
        return jsonify({"error": "RADAR review not found"}), 404

    updated_json = record["result"]

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
        await dbserver.radar_update_result(
            user_id=user_id,
            review_id=review_id,
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
    top_k = 3 if file_count == 1 else 1

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
    # print("data for review", data_for_review)
    # print("type of data", type(data_for_review))
    # 🔹 BEST result = LOWEST distance
    best_result = min(data_for_review, key=lambda x: x.get("distance", float("inf")))

    # 🔹 Optional similarity (cosine-based systems only)
    similarity = (
        round(1 - best_result["distance"], 4)
        if best_result.get("distance") is not None
        else None
    )

    return (
        jsonify(
            {
                "userid": userid,
                "query": user_analyze_input,
                "answer": best_result.get("text"),
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
