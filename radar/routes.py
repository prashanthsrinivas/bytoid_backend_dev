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
from services.redis_service import RedisService
from umail.routes import get_sorted_lance_emails
from utils.fireworkzz import get_firework_embedding, get_think_fire_response2_og
import os

from utils.normal import load_yaml_file

radar_bp = Blueprint("radar", __name__)
RADAR_TEMPLATE = load_yaml_file(path=pathconfig.radar_prompts)


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


import json, html, re


def normalize_text(x):
    try:
        if x is None:
            return ""
        if isinstance(x, str):
            s = x
        else:
            s = json.dumps(x, ensure_ascii=False, indent=2)
    except:
        s = str(x)

    s = html.unescape(s)
    s = re.sub(r"<script.*?>.*?</script>", "", s, flags=re.S)
    s = re.sub(r"<style.*?>.*?</style>", "", s, flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:120000]


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


def build_documents(sources):
    if not sources:
        return "No evidence found."

    docs = []
    for i, s in enumerate(sources, 1):
        src = s.get("source") or s.get("endpoint_id") or s.get("note_id") or "unknown"
        raw = s.get("data", s)

        docs.append(
            f"""
            DOCUMENT {i}
            Source: {src}
            Type: {s.get("type","unknown")}
            Content:
            {normalize_text(raw)}
            """
        )
    return "\n".join(docs)


from concurrent.futures import ThreadPoolExecutor

radar_executor = ThreadPoolExecutor(max_workers=4)


def run_radar_review_sync(user_id, review_id, data, date_uniqueid, type):
    asyncio.run(run_radar_review_redis(user_id, review_id, data, date_uniqueid, type))


def extract_json(text: str) -> str:
    """
    Extract the first valid JSON object from LLM output.
    """
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        raise ValueError("No JSON object found in LLM output")

    json_text = match.group(0)

    # Remove trailing commas (very common LLM issue)
    json_text = re.sub(r",\s*}", "}", json_text)
    json_text = re.sub(r",\s*]", "]", json_text)

    return json_text.strip()


def _safe_json_parse(value):
    if value is None:
        return {}

    # Already parsed
    if isinstance(value, (dict, list)):
        return value

    if not isinstance(value, str):
        return {}

    s = value.strip()

    # 🔑 ONLY CASE WE HANDLE:
    # "\"{ ... }\""  or "\"[ ... ]\""
    if s.startswith('"') and s.endswith('"'):
        inner = s[1:-1]

        # unescape quotes
        inner = inner.replace('\\"', '"')

        # must now be valid JSON
        if inner.startswith("{") or inner.startswith("["):
            try:
                return json.loads(inner)
            except Exception:
                return {}

    # If string already starts with JSON directly
    if s.startswith("{") or s.startswith("["):
        try:
            return json.loads(s)
        except Exception:
            return {}

    return {}


async def run_radar_review_redis(user_id, review_id, data, date_uniqueid, btype):
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
    try:
        await update(status="running")
        # print("🚀 RADAR START", review_id)

        userid = data.get("userid")
        name = data.get("name")
        user_analyze_input = data.get("analyze_input")
        main_source = data.get("main_source")
        data_sources = data.get("data_sources", {})
        reference_sources = data.get("reference_sources", {})
        refernce_main_source = data.get("refernce_main_source")

        conn = connect_to_rds()
        dbserver = LanceDBServer()
        credits = Credits(db=conn)

        payload = None
        if main_source == "knowledge" or refernce_main_source == "knowledge":
            embedding = await get_firework_embedding()
            vector = embedding.embed_query(user_analyze_input)
            payload = QueryData(user_id=userid, embedding=vector, top_k=3)
            total_output_chars = len(vector)

            total_chars = len(user_analyze_input) + total_output_chars

            await credits.update_ai_credits_redis(
                user_id=userid,
                credit_type="embedding",
                total_chars=total_chars,
                reference_id=inspect.stack()[0].function,
            )

        data_checked = await retreval_from_sources(
            conn, dbserver, main_source, data_sources, userid, payload
        )

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
            .replace("{{data_sources}}", json.dumps(data_checked, indent=2))
            .replace("{{reference_sources}}", json.dumps(reference_RWA or {}, indent=2))
            .replace("{{last_radar_response}}", last_radar_response)
        )
        # print("len of base prompt", len(base_prompt))

        result = await get_think_fire_response2_og(
            user_message=base_prompt,
            user_id=userid,
            credits=credits,
        )
        # print("result", type(result), len(result), result)

        refactor_result = _safe_json_parse(result)
        # print("result of refactored", type(refactor_result), refactor_result)
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

            # print("✅ RADAR DONE", review_id)
            await update(status="completed", result=refactor_result)

    except Exception as e:
        # print("❌ RADAR ERROR", review_id, str(e))
        await update(status="failed", error=str(e))

    finally:
        if conn:
            conn.close()


@radar_bp.route("/radar/review", methods=["POST"])
async def radar_review():
    data = request.get_json(force=True)
    # print("the input request", data)
    userid = data.get("userid")

    if not userid:
        return jsonify({"error": "userid is required"}), 400

    redis = RedisService()
    key = f"radar:review:{userid}"

    existing = await redis.get(key)
    now = int(time.time())

    if existing:
        status = existing.get("status")
        started_at = existing.get("started_at", now)

        # ⛔ Pending but still valid → return it
        if status == "pending" and now - started_at <= 180:
            return jsonify(existing)

        # ⛔ Running → always return it
        if status == "running":
            # 🕒 Check for running timeout
            if now - started_at <= 180:
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
        if status == "pending" and now - started_at > 180:
            pass

    # ✅ Create NEW review
    review_id = f"review_{str(uuid.uuid4().hex[:8])}"
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
        run_radar_review_sync, userid, review_id, data, date_uniqueid, "review"
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
    data = request.get_json(force=True)
    # print("analyze input request", data)
    userid = data.get("userid")

    if not userid:
        return jsonify({"error": "userid is required"}), 400

    redis = RedisService()
    key = f"radar:analyze:{userid}"

    existing = await redis.get(key)
    now = int(time.time())

    if existing:
        status = existing.get("status")
        started_at = existing.get("started_at", now)

        # ⛔ Pending but still valid → return it
        if status == "pending" and now - started_at <= 180:
            return jsonify(existing)

        # ⛔ Running → always return it
        if status == "running":
            # 🕒 Check for running timeout
            if now - started_at <= 180:
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
        if status == "pending" and now - started_at > 180:
            pass

    # ✅ Create NEW review
    review_id = f"analyze_{str(uuid.uuid4().hex[:8])}"
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
        run_radar_review_sync, userid, review_id, data, date_uniqueid, "analyze"
    )

    return jsonify(state)


@radar_bp.route("/radar/decide", methods=["POST"])
async def radar_decide():
    data = request.get_json(force=True)
    # print("the input request", data)
    userid = data.get("userid")

    if not userid:
        return jsonify({"error": "userid is required"}), 400

    redis = RedisService()
    key = f"radar:decide:{userid}"

    existing = await redis.get(key)
    now = int(time.time())

    if existing:
        status = existing.get("status")
        started_at = existing.get("started_at", now)

        # ⛔ Pending but still valid → return it
        if status == "pending" and now - started_at <= 180:
            return jsonify(existing)

        # ⛔ Running → always return it
        if status == "running":
            # 🕒 Check for running timeout
            if now - started_at <= 180:
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
        if status == "pending" and now - started_at > 180:
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
        run_radar_review_sync, userid, review_id, data, date_uniqueid, "decide"
    )

    return jsonify(state)


@radar_bp.route("/radar/changeblock", methods=["POST"])
async def radar_change_block_preview():
    data = request.get_json(force=True)
    # print("data from frontend for change block", data)

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
    # print("record", type(record), record)

    if not record or not record.get("result"):
        return jsonify({"error": "RADAR review not found"}), 404

    original_json = record["result"]
    review_temp = RADAR_TEMPLATE["radar_change_block_prompt"]

    # Build PREVIEW prompt (returns only changed block)
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

        payload = _safe_json_parse(llm_response)
        # print("payload", payload)

        # Explicit LLM rejection
        if payload.get("status") == "error":
            return jsonify(payload), 400

        # Must return ONLY block preview
        required_keys = {"block_id", "block_type", "changed_block"}
        if not required_keys.issubset(payload.keys()):
            return (
                jsonify(
                    {
                        "error": "Invalid model response",
                        "details": "Missing required preview keys",
                    }
                ),
                500,
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


@radar_bp.route("/radar/fileout", methods=["POST"])
async def download_radar_files():
    data = request.get_json(force=True)

    user_id = data.get("userid")
    review_id = data.get("review_id")
    filetype = data.get("filetype")

    dbserver = LanceDBServer()

    record = await dbserver.radar_get_review(user_id=user_id, review_id=review_id)

    if not record or not record.get("result"):
        return jsonify({"error": "RADAR review not found"}), 404

    updated_json = record["result"]
