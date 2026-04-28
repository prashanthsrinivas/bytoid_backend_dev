import json
import time
import uuid
from agent_route.doc_clarity import QueryData

from credits_route.route import Credits
from db.lance_db_service import LanceDBServer
from db.rds_db import connect_to_rds

# from services.scheduler_service import SchedulerService
from utils.normal import load_yaml_file
from utils.s3_utils import read_json_from_s3
from utils.fireworkzz import (
    get_firework_embedding,
    get_think_bedrok_response,
    get_think_fire_response2_og,
    get_think_fire_response2_og2,
)
from utils.normal import load_yaml_file

from cust_helpers import pathconfig
from utils.base_logger import get_logger
from utils.app_configs import IS_DEV
from .utils import *
from .utils import _safe_json_parse_full
from .utils import _safe_json_parse
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

    main_source = runbook.get("main_source")
    data_sources = runbook.get("data_sources")
    reference_sources = runbook.get("reference_sources")
    refernce_main_source = runbook.get("reference_main_source")

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

    # ----------------------------
    # ANALYZER
    # ----------------------------
    analyzer_prompt = (
        RADAR_TEMPLATE["radar_update_analyzer_prompt"]
        .replace("{{analyze_input}}", analyze_input)
        .replace("{{last_radar_response}}", json.dumps(last_runbook_response))
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
            is_chart_update = has_chart_block(update_blocks)

            update_prompt = f"""
        You are a RADAR REPORT BLOCK MODIFIER.

        GOAL:
        Update ONLY the specified blocks based on instructions.

        -----------------------------------

        BLOCKS TO UPDATE:
        {json.dumps(blocks_to_update)}

        USER REQUEST:
        {analyze_input}

        UPDATE INSTRUCTIONS:
        {json.dumps(update_blocks)}

        -----------------------------------
        STRICT RULES:

        1. OUTPUT MUST BE JSON ARRAY ONLY
        2. DO NOT change block_id
        3. DO NOT add new blocks
        4. DO NOT remove blocks
        5. ONLY modify requested fields

        6. MAINTAIN:
        - existing structure
        - formatting
        - tone consistency

        7. If instruction is unclear → improve content logically
        
         {CHART_BLOCK_PROMPT if is_chart_update else ""}

        -----------------------------------
        OUTPUT FORMAT:

        [
        {{
            "block_id": "same_id",
            ...
        }}
        ]
        """

            res = await get_think_fire_response2_og(
                user_message=update_prompt,
                user_id=user_id,
                credits=credits,
                total_input_chars=len(update_prompt),
            )

            updated_blocks = safe_json_load(res, [])

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

    risk_result = await get_think_bedrok_response(
        user_message=risk_prompt,
        user_id=user_id,
        credits=credits,
        total_input_chars=len(risk_prompt),
    )

    risk_data = _safe_json_parse(risk_result)

    merged_result["risk_analysis"] = risk_data
    merged_result["risk_score"] = risk_data.get("final_risk_score", 0)

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
    if merged_result:
        await emit(
            msg_builder.job_success(
                job_id,
                session_id,
                "generated risk analysis and saving it to report",
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
