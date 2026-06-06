import json
import os
import time
import uuid
from agent_route.doc_clarity import QueryData

from credits_route.route import Credits
from db.lance_db_service import LanceDBServer
from db.rds_db import connect_to_rds

# from services.scheduler_service import SchedulerService
from utils.normal import load_yaml_file
from utils.s3_utils import read_json_from_s3, s3bucket, S3_BUCKET
from utils.fireworkzz import (
    get_firework_embedding,
    get_think_bedrok_response,
    get_think_fire_response2_og,
    get_think_fire_response2_og2,
    get_think_bedrock_vision_image,
)
from radar.radar_helpers import extract_files_content, IMAGE_EXTENSIONS
from utils.normal import load_yaml_file

from cust_helpers import pathconfig
from utils.base_logger import get_logger
from utils.app_configs import IS_DEV
from .utils import *
from .utils import _safe_json_parse_full
from .utils import _safe_json_parse
from .risk_engine import (
    get_risk_config,
    compute_risk,
    apply_risk_overrides,
    risk_analysis_disabled,
)
from runbook.helper import run_evidence_analysis, reduce_data_for_report

dbserver = LanceDBServer()
conn = connect_to_rds()
credits = Credits(conn)


RUNBOOK_TEMPLATE = load_yaml_file(path=pathconfig.runbook_prompts)
RADAR_TEMPLATE = load_yaml_file(path=pathconfig.radar_prompts)
logger = get_logger(__name__, log_level="DEBUG" if IS_DEV else "INFO")

CHART_BLOCK_PROMPT = """
📊 CHART BLOCK SYSTEM (STRICT MODE)

Charts are STRUCTURED BLOCKS — NOT free-form.

You MUST follow the chart_schema EXACTLY.

─────────────────────────────
SUPPORTED TYPES:
bar, line, pie, doughnut, radar, scatter

─────────────────────────────
REQUIRED STRUCTURE:

{
  "block_id": "char_{id}",
  "block_type": "chart",
  "title": "string",
  "editable": true,
  "exportable": true,
  "micro_blocks": [
    {
      "type": "chart_schema",
      "chart_type": "allowed type",
      "title": "string",
      "data": {
        "labels": [],
        "datasets": []
      },
      "html": ""
    }
  ]
}

─────────────────────────────
STRICT RULES:

✓ labels required
✓ datasets required
✓ dataset = { label, data }

❌ DO NOT ADD:
options, legend, config, extra keys

─────────────────────────────
TYPE RULES:

BAR/LINE → numeric arrays  
PIE/DOUGHNUT → single dataset + color array  
RADAR → labels must match data  
SCATTER → data = [{x,y}] & labels = []

─────────────────────────────
COLOR RULES:

"#4CAF50" positive  
"#F44336" negative  
"#2196F3" neutral  
"#FFC107" warning  
"#9C27B0" comparison
"""


def has_chart_block(instructions):
    return any(b.get("block_type") == "chart" for b in instructions)


async def modify_run_runbook_execution_engine(
    user_id,
    runbook,
    dbserver=LanceDBServer(),
    structure_file_payload=None,
    files=None,
    structure_file=None,
    result_id=None,
    is_prev_needed=False,
    job_id=None,
    session_id=None,
    progress=None,
    is_playbook_based_execution=False,
):
    import json, time, uuid
    from websockets_custom.ws_instance import ws_service, msg_builder_main
    from runbook.utils import send

    msg_builder = msg_builder_main

    # ----------------------------
    # HELPERS
    # ----------------------------
    def safe_json_load(data, default):
        try:
            if isinstance(data, str):
                return json.loads(data)
            return data
        except:
            return default

    async def emit(msg):
        if job_id and session_id:
            await send(ws_service, msg, user_id)

    # ----------------------------
    # INIT
    # ----------------------------
    conn = connect_to_rds()
    credits = Credits(conn)

    runbook_id = runbook["runbook_id"]

    # Pop playbook evidence blobs (always, so they don't pollute the runbook dict)
    _evidences_urls = runbook.pop("_playbook_evidences_urls", [])
    _ev_overview = runbook.pop("_playbook_evidence_overview", {})
    _ev_questions = runbook.pop("_playbook_ev_questions", [])
    if _evidences_urls:
        is_playbook_based_execution = True

    main_source = runbook.get("main_source")
    data_sources = runbook.get("data_sources")
    reference_sources = runbook.get("reference_sources")
    refernce_main_source = runbook.get("reference_main_source")

    # Initialized here so all execution paths (update_only, add/update/delete)
    # have these defined before the unconditional evidence-analysis block below.
    data_checked = []
    reference_RWA = []

    # ----------------------------
    # STRUCTURE LOAD
    # ----------------------------
    structure_file_content = None
    if structure_file:
        structure_file_content = read_json_from_s3(structure_file)

    raw_structure = structure_file_payload or runbook.get("structure_theme")
    structure_file_payload = safe_json_load(raw_structure, {})
    if data_sources and len(data_sources) > 1:
        data_sources = normalize_json_field(data_sources)
    if reference_sources and len(reference_sources) > 1:
        reference_sources = normalize_json_field(reference_sources)

    if not structure_file_payload or "blocks" not in structure_file_payload:
        raise ValueError("structure_file_payload missing 'blocks'")

    if not progress:
        progress = 35
    analyze_input = runbook.get("analyze_input") or runbook.get("description") or ""

    await emit(
        msg_builder.job_progress(
            job_id, session_id, "report setup", "started creating report", progress
        )
    )
    user_analyze_input = analyze_input
    # ----------------------------
    # RUNBOOK INIT
    # ----------------------------
    execution_id = f"exec_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    new_result_id = f"result_{uuid.uuid4().hex[:6]}"

    await dbserver.insert_runbook_result(
        {
            "execution_id": execution_id,
            "result_id": new_result_id,
            "runbook_id": runbook_id,
            "user_id": user_id,
            "status": "running",
            "started_at": int(time.time()),
        }
    )

    runbook_yaml = render_runbook_yaml(runbook)

    # ----------------------------
    # INPUT RESOLUTION
    # ----------------------------
    analyze_input = runbook.get("analyze_input") or runbook.get("description") or ""

    file_data = await collect_runbook_inputs(runbook)

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
        lang_data = safe_json_load(result, {})

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

    # ----------------------------
    # PREVIOUS RESULT
    # ----------------------------
    last_runbook_response = {}
    if is_prev_needed:
        val = await dbserver.get_latest_runbook_result(
            user_id=user_id, runbook_id=runbook_id, result_id=result_id
        )
        if val:
            last_runbook_response = safe_json_load(val.get("result"), {})

    existing_blocks = last_runbook_response.get("blocks", [])

    # Build an explicit title→id index so the analyzer doesn't have to dig
    # through the full document to resolve a block reference by title.
    block_index = []
    for b in existing_blocks:
        if not isinstance(b, dict):
            continue
        title = (
            b.get("title")
            or b.get("name")
            or b.get("heading")
            or ""
        )
        bid = b.get("block_id")
        if bid:
            block_index.append({"block_id": bid, "title": str(title)})

    # ----------------------------
    # ANALYZER
    # ----------------------------
    analyzer_prompt = (
        RADAR_TEMPLATE["radar_update_analyzer_prompt"]
        .replace("{{analyze_input}}", analyze_input)
        .replace("{{last_radar_response}}", json.dumps(last_runbook_response))
        .replace("{{available_blocks}}", json.dumps(block_index, indent=2))
    )

    analysis_raw = await get_think_fire_response2_og(
        user_message=analyzer_prompt,
        user_id=user_id,
        credits=credits,
        total_input_chars=len(analyzer_prompt),
    )

    await emit(
        msg_builder.job_progress(
            job_id, session_id, "query analysis", "analyzing request", 55
        )
    )

    analysis_json = safe_json_load(analysis_raw, {})
    logger.debug("Analysis raw: %s", analysis_json)

    add_blocks = analysis_json.get("addblocks", [])
    update_blocks = analysis_json.get("updateblocks", [])
    delete_blocks = analysis_json.get("deleteblocks", [])
    update_only_content = analysis_json.get("updateonlycontent", [])
    restructure_content = analysis_json.get("restructure", [])

    if (
        not add_blocks
        and not update_blocks
        and not delete_blocks
        and not update_only_content
        and not restructure_content
    ):
        # Fallback: the analyzer LLM came back empty, but the user may have
        # referenced a block by its title. Try a Python-side title-substring
        # match against the index we built above. If we find one, force a
        # updateblocks route so regeneration still happens.
        instr_lower = (analyze_input or "").lower()
        matched_blocks = [
            entry for entry in block_index
            if entry["title"]
            and len(entry["title"]) > 3
            and entry["title"].lower() in instr_lower
        ]

        if matched_blocks:
            logger.info(
                "Analyzer empty; title-substring fallback matched %s",
                [m["block_id"] for m in matched_blocks],
            )
            update_blocks = [
                {
                    "block_id": m["block_id"],
                    "changes": [analyze_input],
                    "target_multiplier": 1.0,
                    "reason": "Title-substring fallback (analyzer returned empty)",
                }
                for m in matched_blocks
            ]
        else:
            logger.warning(
                "Modify analyzer returned empty classification for input: %s "
                "(no title-substring fallback hit either)",
                analyze_input,
            )
            await emit(
                msg_builder.job_error(
                    job_id,
                    session_id,
                    "Could not identify which section to modify. "
                    "Please reference the section by its exact title.",
                )
            )
            return None

    # ----------------------------
    # UPDATE ONLY
    # ----------------------------
    if (
        not add_blocks
        and not update_blocks
        and not delete_blocks
        and update_only_content
    ):

        await emit(
            msg_builder.job_progress(
                job_id, session_id, "query analysis", "content-only update", 60
            )
        )

        updater_prompt = (
            RADAR_TEMPLATE["radar_update_only_patch"]
            .replace("{{last_radar_response}}", json.dumps(last_runbook_response))
            .replace("{{analyze_input}}", json.dumps(update_only_content))
        )

        result = await get_think_bedrok_response(
            user_message=updater_prompt,
            user_id=user_id,
            credits=credits,
            total_input_chars=len(updater_prompt),
        )

        merged_result = safe_json_load(result, {})

    else:

        # DELETE
        if delete_blocks:
            delete_ids = {b["block_id"] for b in delete_blocks}
            existing_blocks = [
                b for b in existing_blocks if b["block_id"] not in delete_ids
            ]

        # UPDATE
        if update_blocks:

            await emit(
                msg_builder.job_progress(
                    job_id,
                    session_id,
                    "query analysis",
                    "updating sections intelligently",
                    62,
                )
            )

            blocks_to_update = [
                b
                for b in existing_blocks
                if b["block_id"] in [u["block_id"] for u in update_blocks]
            ]

            if not blocks_to_update:
                logger.warning(
                    "updateblocks: analyzer returned block_ids %s but none match "
                    "existing blocks (have %s)",
                    [u.get("block_id") for u in update_blocks],
                    [b.get("block_id") for b in existing_blocks],
                )
                await emit(
                    msg_builder.job_error(
                        job_id,
                        session_id,
                        "Could not locate the section(s) to modify. "
                        "Try referencing the section by its exact title.",
                    )
                )
                return None

            is_chart_update = has_chart_block(update_blocks)

            # Derive per-block word-count targets from target_multiplier so the
            # LLM knows exactly how much content to generate.
            def _block_word_target(block, instruction):
                multiplier = instruction.get("target_multiplier", 1.0)
                if multiplier == 1.0:
                    return None  # quality change only, no size target
                # Count visible words in existing HTML content
                existing_html = ""
                for mb in block.get("micro_blocks", []):
                    existing_html += mb.get("html", "")
                import re as _re
                words = len(_re.findall(r"\S+", _re.sub(r"<[^>]+>", " ", existing_html)))
                target = max(50, int(words * multiplier))
                return target

            update_instructions_with_targets = []
            for instr in update_blocks:
                bid = instr.get("block_id")
                matching = next((b for b in blocks_to_update if b.get("block_id") == bid), None)
                if matching:
                    target = _block_word_target(matching, instr)
                    entry = dict(instr)
                    if target:
                        entry["target_visible_word_count"] = target
                    update_instructions_with_targets.append(entry)
                else:
                    update_instructions_with_targets.append(instr)

            update_prompt = f"""
        You are a RADAR REPORT BLOCK MODIFIER.

        GOAL:
        Rewrite ONLY the specified blocks exactly as instructed. Produce real,
        substantive content — not placeholders, not repetition of existing text.

        -----------------------------------

        BLOCKS TO UPDATE:
        {json.dumps(blocks_to_update)}

        USER REQUEST:
        {analyze_input}

        UPDATE INSTRUCTIONS (includes target word counts where applicable):
        {json.dumps(update_instructions_with_targets)}

        -----------------------------------
        STRICT RULES:

        1. OUTPUT MUST BE JSON ARRAY ONLY — no explanations, no markdown wrapper.
        2. DO NOT change block_id or micro_id.
        3. DO NOT add new blocks; DO NOT remove blocks.
        4. Preserve micro_blocks array structure (count, order, content_type) exactly.
        5. Where target_visible_word_count is provided, the generated html in that
           block MUST contain at least that many visible words (count only words a
           human would read — exclude HTML tags, JSON keys, IDs).
        6. Maintain the existing executive tone, formatting, and HTML tag style.
        7. Content MUST be substantive and coherent — never pad with filler phrases.
        8. PLACEHOLDER DETECTION: if a micro_block's html contains only stub text
           (cells filled with "text", empty strings, "TBD", "TODO", "Lorem ipsum",
           or single-word boilerplate), you MUST replace it with substantive content
           derived from (a) the user's instruction and (b) the rest of the document
           for context. NEVER echo placeholder html back.
        9. NO-RETURN-ON-FAIL: if you genuinely cannot generate substantive content
           for a block (e.g. the instruction is incoherent), OMIT it from the output
           array. Do NOT return the block unchanged. The caller treats an omitted
           block as a regeneration failure.

        {CHART_BLOCK_PROMPT if is_chart_update else ""}

        -----------------------------------
        OUTPUT FORMAT:

        [
        {{
            "block_id": "same_id",
            ...full block with updated micro_blocks...
        }}
        ]
        """

            # Use the smart model — block regeneration produces structured
            # JSON with table/matrix payloads that the fast model frequently
            # mangles (markdown-wrapping, truncation, or echoing placeholders).
            res = await get_think_bedrok_response(
                user_message=update_prompt,
                user_id=user_id,
                credits=credits,
                total_input_chars=len(update_prompt),
            )

            # _safe_json_parse_full strips ```json fences, handles embedded
            # JSON, and tolerates dict-vs-list shapes — much more forgiving
            # than the strict safe_json_load when models add prose.
            parsed = _safe_json_parse_full(res)
            if isinstance(parsed, list):
                updated_blocks = parsed
            elif isinstance(parsed, dict):
                updated_blocks = [parsed]
            else:
                updated_blocks = []

            requested_ids = {b["block_id"] for b in blocks_to_update}
            returned_ids = {
                b.get("block_id")
                for b in updated_blocks
                if isinstance(b, dict)
            }

            if not updated_blocks or not (requested_ids & returned_ids):
                logger.warning(
                    "updateblocks: LLM produced no matching blocks "
                    "(requested=%s, returned=%s, raw_response_head=%s)",
                    requested_ids,
                    returned_ids,
                    str(res)[:200],
                )
                await emit(
                    msg_builder.job_error(
                        job_id,
                        session_id,
                        "Could not regenerate the requested section(s). "
                        "Try referencing the section by its exact title.",
                    )
                )
                return None

            updated_map = {b["block_id"]: b for b in updated_blocks}

            existing_blocks = [
                updated_map.get(b["block_id"], b) for b in existing_blocks
            ]

            logger.debug("Updated blocks: %s", updated_blocks)

        # ADD
        if add_blocks:
            is_chart_update = has_chart_block(add_blocks)
            await emit(
                msg_builder.job_progress(
                    job_id,
                    session_id,
                    "query analysis",
                    "generating new sections intelligently",
                    65,
                )
            )

            add_prompt = f"""
        You are a RADAR REPORT BLOCK GENERATOR.

        GOAL:
        Generate NEW blocks that should be added to an existing report.

        -----------------------------------
        CURRENT BLOCKS:
        {json.dumps(existing_blocks)}

        USER REQUEST:
        {analyze_input}

        INSTRUCTIONS (WHAT TO ADD):
        {json.dumps(add_blocks)}

        -----------------------------------
        STRICT RULES:

        1. OUTPUT MUST BE JSON ARRAY ONLY
        2. Each block MUST contain:
        - block_id
        - block_type
        - title
        - content OR micro_blocks

        3. DO NOT:
        - duplicate existing blocks
        - modify existing blocks
        - remove anything

        4. MAINTAIN:
        - same tone, style, and structure as existing report
        - consistent formatting

        5. INSERTION:
        - If instruction has "insert_position": respect it
        - Else decide logical placement ("start" or "end")

        6. CONTENT QUALITY:
        - must be meaningful, not placeholders
        - must align with user request

        7. KEEP RESPONSE CLEAN:
        - no explanations
        - no extra text
        
        {CHART_BLOCK_PROMPT if is_chart_update else ""}


        -----------------------------------
        OUTPUT FORMAT:

        [
        {{
            "block_id": "new_block_1",
            "block_type": "text",
            "title": "...",
            "content": "...",
            "insert_position": "end"
        }}
        ]
        """

            res = await get_think_fire_response2_og(
                user_message=add_prompt,
                user_id=user_id,
                credits=credits,
                total_input_chars=len(add_prompt),
            )

            new_blocks = safe_json_load(res, [])

            for b in new_blocks:
                b["block_id"] = f"{b.get('block_id','blk')}_{uuid.uuid4().hex[:6]}"

                pos = b.get("insert_position", "end")

                if pos == "start":
                    existing_blocks.insert(0, b)
                else:
                    existing_blocks.append(b)

            logger.debug("New blocks: %s", new_blocks)
        # RESTRUCTURE
        if restructure_content:
            # --------------------------------------------------
            # OPTIONAL RADAR DATA SOURCES
            # --------------------------------------------------

            data_checked = []
            reference_RWA = []

            # --------------------------------------------------
            # PLAYBOOK EVIDENCE FILES (images + docs)
            # Injected into data_checked; no re-analysis — the
            # previous result's evidence blocks are preserved.
            # --------------------------------------------------
            if is_playbook_based_execution and _evidences_urls:
                import mimetypes as _mimetypes, tempfile as _tempfile, base64 as _b64

                _cf_prefix = (os.getenv("CLOUDFRNT", "")).rstrip("/") + "/"
                _s3_client = s3bucket()
                _ev_admissible = _ev_overview.get("admissible", [])
                _evidence_summary = (
                    "\n".join(f"- {ev.get('artifact', '')}" for ev in _ev_admissible)
                    or "No specific evidence types configured."
                )

                for url in _evidences_urls:
                    s3_key = (
                        url.replace(_cf_prefix, "", 1)
                        if url.startswith(_cf_prefix)
                        else url
                    )
                    fname = os.path.basename(s3_key)
                    ext = os.path.splitext(fname)[1].lower()
                    try:
                        tmp_path = os.path.join(_tempfile.gettempdir(), fname)
                        _s3_client.download_file(
                            Bucket=S3_BUCKET, Key=s3_key, Filename=tmp_path
                        )
                        with open(tmp_path, "rb") as fh:
                            file_bytes = fh.read()
                        try:
                            os.unlink(tmp_path)
                        except Exception:
                            pass

                        ct = (
                            _mimetypes.guess_type(fname)[0]
                            or "application/octet-stream"
                        )
                        extracted = extract_files_content(
                            [
                                {
                                    "filename": fname,
                                    "data": file_bytes,
                                    "content_type": ct,
                                }
                            ]
                        )
                        for item in extracted:
                            if item.get("type") in IMAGE_EXTENSIONS:
                                ct = _mimetypes.guess_type(fname)[0] or "image/jpeg"
                                import base64 as _b64

                                b64 = _b64.b64encode(file_bytes).decode()
                                data_uri = f"data:{ct};base64,{b64}"

                                _ev_admissible = _ev_overview.get("admissible", [])
                                _evidence_summary = (
                                    "\n".join(
                                        f"- {ev.get('artifact', '')}"
                                        for ev in _ev_admissible
                                    )
                                    or "No specific evidence types configured."
                                )

                                logger.info(
                                    "Running vision extraction on image: %s", fname
                                )
                                vision_result = await get_think_bedrock_vision_image(
                                    data_uri=data_uri,
                                    evidence_summary=_evidence_summary,
                                    user_id=user_id,
                                    credits=credits,
                                )

                                if vision_result:
                                    meta = vision_result.get("image_meta", {})
                                    logger.info(
                                        "Image vision result — type=%s timestamps=%s log_entries=%d",
                                        meta.get("image_type", "unknown"),
                                        meta.get("timestamps", []),
                                        len(meta.get("log_entries", [])),
                                    )
                                    # Build a single text blob with all extracted info
                                    parts = []
                                    if meta.get("extracted_text"):
                                        parts.append(
                                            f"Extracted text:\n{meta['extracted_text']}"
                                        )
                                    if meta.get("timestamps"):
                                        parts.append(
                                            "Timestamps: "
                                            + ", ".join(meta["timestamps"])
                                        )
                                    if meta.get("log_entries"):
                                        parts.append(
                                            "Log entries:\n"
                                            + "\n".join(meta["log_entries"])
                                        )
                                    for found_item in vision_result.get("found", []):
                                        artifact = found_item.get("artifact", "")
                                        content = found_item.get("content", "")
                                        if artifact and content:
                                            parts.append(f"[{artifact}] {content}")
                                    if parts:
                                        data_checked.append(
                                            {
                                                "type": "image",
                                                "source": s3_key,
                                                "data": "\n\n".join(parts),
                                            }
                                        )
                                continue
                            else:
                                data_checked.append(
                                    {
                                        "type": "docs",
                                        "source": s3_key,
                                        "data": item["content"],
                                    }
                                )
                    except Exception as _e:
                        logger.warning(
                            "Modify evidence extraction failed for %s: %s", s3_key, _e
                        )

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

            # --------------------------------------------------
            # REDUCE DATA TO STAY WITHIN TOKEN LIMITS
            # --------------------------------------------------
            reduced_datachecked = await reduce_data_for_report(
                data_checked, structure_file_payload, user_id, credits, label="evidence"
            )
            reduced_referencerwa = await reduce_data_for_report(
                reference_RWA,
                structure_file_payload,
                user_id,
                credits,
                label="governance framework",
            )

            restructure_prompt = (
                RADAR_TEMPLATE["radar_update_template_structure"]
                .replace("{{structure_file_data}}", json.dumps(structure_file_payload))
                .replace("{{last_radar_response}}", json.dumps(last_runbook_response))
                .replace("{{analyze_input}}", json.dumps(restructure_content))
                .replace(
                    "{{data_sources}}",
                    (
                        reduced_datachecked
                        if isinstance(reduced_datachecked, str)
                        else json.dumps(reduced_datachecked)
                    ),
                )
                .replace(
                    "{{reference_sources}}",
                    (
                        reduced_referencerwa
                        if isinstance(reduced_referencerwa, str)
                        else json.dumps(reduced_referencerwa)
                    ),
                )
                .replace("{{file_data}}", file_data)
                # 🔥 CONFIG
                .replace("{{output_language}}", output_language)
                .replace("{{requested_word_count}}", str(output_word_count))
            )

            result = await get_think_bedrok_response(
                user_message=restructure_prompt,
                user_id=user_id,
                credits=credits,
                total_input_chars=len(restructure_prompt),
                language=output_language,
                words_count=output_word_count,
            )
            merged_report = merge_runbook_chunks_deterministic(result)

            merged_result = safe_json_load(merged_report, {})

        else:
            logger.warning(
                "Modify: reached no-op fall-through — analyzer returned empty "
                "classification yet upstream guard did not catch it. "
                "analyze_input=%s",
                analyze_input,
            )
            merged_result = last_runbook_response or {}
            merged_result["blocks"] = existing_blocks

    await emit(
        msg_builder.job_progress(
            job_id,
            session_id,
            "Report",
            "generating new report content done",
            80,
        )
    )

    # =========================================================
    # RISK ANALYSIS (UNCHANGED)
    # =========================================================
    # Risk analysis can be turned off per-runbook at creation time.
    if risk_analysis_disabled(runbook):
        merged_result["risk_analysis"] = None
        merged_result["risk_score"] = None
        merged_result["risk_analysis_disabled"] = True
    else:
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

        risk_cfg = get_risk_config(user_id)
        risk_prompt = (
            RADAR_TEMPLATE["nist_risk_score_prompt"]
            .replace("{{analysis_result}}", json.dumps(merged_result))
            .replace(
                "{{report_data}}",
                json.dumps(structure_file_content) if structure_file_content else "",
            )
            .replace("{{impact_scale}}", str(risk_cfg.get("impact_scale", 5)))
            .replace("{{likelihood_scale}}", str(risk_cfg.get("likelihood_scale", 5)))
        )

        risk_result = await get_think_bedrok_response(
            user_message=risk_prompt,
            user_id=user_id,
            credits=credits,
            total_input_chars=len(risk_prompt),
        )

        risk_data = _safe_json_parse(risk_result) or {}

        # Deterministic scoring from the configured Impact x Likelihood scales.
        computed = compute_risk(risk_data.get("risks", []), risk_cfg)
        computed["justification"] = risk_data.get("justification", "")

        # Re-apply manual risk overrides from the prior report (loaded into
        # last_runbook_response via is_prev_needed) so user edits survive chat-modify.
        try:
            prior_ra = (last_runbook_response or {}).get("risk_analysis")
            computed, dropped = apply_risk_overrides(computed, prior_ra)
            if dropped:
                computed["dropped_overrides"] = dropped
                logger.info(
                    "Chat-modify dropped %d manual risk override(s) for runbook %s",
                    len(dropped),
                    runbook_id,
                )
        except Exception:
            logger.warning("apply_risk_overrides (modify) failed", exc_info=IS_DEV)

        merged_result["risk_analysis"] = computed
        merged_result["risk_score"] = computed["final_risk_score"]

    if data_checked:
        report_viewer = data_sources.get("report_viewer")
        evidence_analysis = await run_evidence_analysis(
            data_checked, report_viewer, user_id, credits
        )
        merged_result["evidence_analysis"] = evidence_analysis

    await emit(
        msg_builder.job_progress(
            job_id,
            session_id,
            "risk analysis",
            "generated risk analysis and saving it to report",
            90,
        )
    )

    merged_result["result_id"] = new_result_id
    merged_result["previous_result_id"] = result_id

    # Give each regenerated report an individual, human-readable name (runbook
    # name + 2-3 word AI descriptor from the first paragraph). Best-effort.
    try:
        from runbook.report_naming import build_report_name

        merged_result["report_name"] = await build_report_name(
            runbook.get("name"), merged_result, credits, user_id
        )
    except Exception:
        logger.warning("report_name generation failed", exc_info=IS_DEV)

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

    # Activity feed: emit one field-event per changed whitelisted section so
    # the step drawer surfaces "who changed what" alongside state transitions.
    # If the engine didn't already fetch the previous result (is_prev_needed=False),
    # fetch it here just for diffing. Best-effort — never block the save path.
    try:
        from services.document_activity_service import emit_field_diff_events

        diff_before = last_runbook_response
        if not diff_before and result_id:
            try:
                prev = await dbserver.get_latest_runbook_result(
                    user_id=user_id, runbook_id=runbook_id, result_id=result_id
                )
                if prev:
                    diff_before = safe_json_load(prev.get("result"), {})
            except Exception:
                diff_before = {}

        if diff_before:  # only emit when we have something to compare
            # Activity events key by the NEW result_id (matches the workflow's
            # doc_id) so the per-report drawer surfaces them. previous_result_id
            # records the lineage for the UI to chain edits by result rather
            # than wall clock.
            emit_field_diff_events(
                doc_type="runbook",
                doc_id=new_result_id,
                previous_result_id=result_id,
                new_result_id=new_result_id,
                actor_user_id=user_id,
                before=diff_before,
                after=merged_result or {},
            )
    except Exception:
        logger.exception("emit_field_diff_events failed for runbook %s", runbook_id)

    if merged_result:
        await emit(
            msg_builder.job_success(
                job_id,
                session_id,
                "generated risk analysis and saving it to report",
                result_id=new_result_id,
                previous_result_id=result_id,
            )
        )
        name = runbook["name"] or runbook_id
        await emit(
            msg_builder.global_session_msg(
                session_id=session_id,
                message=f"generated report for {name}",
            )
        )

    return merged_result
