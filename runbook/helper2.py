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
from .utils import *
from .utils import _safe_json_parse_full
from .utils import _safe_json_parse

dbserver = LanceDBServer()
conn = connect_to_rds()
credits = Credits(conn)


RUNBOOK_TEMPLATE = load_yaml_file(path=pathconfig.runbook_prompts)
RADAR_TEMPLATE = load_yaml_file(path=pathconfig.radar_prompts)
logger = get_logger(__name__)


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

    # ✅ single flag
    should_emit = bool(job_id and session_id)

    async def emit(msg):
        if should_emit:
            await send(ws_service, msg, user_id)

    conn = connect_to_rds()
    credits = Credits(conn)

    runbook_id = runbook["runbook_id"]

    main_source = runbook.get("main_source")
    data_sources = runbook.get("data_source")
    reference_sources = runbook.get("reference_sources")
    refernce_main_source = runbook.get("reference_main_source")

    # ----------------------------
    # STRUCTURE LOAD
    # ----------------------------
    if structure_file:
        structure_file_content = read_json_from_s3(structure_file)
    else:
        structure_file_content = None

    raw_structure = structure_file_payload or runbook.get("structure_theme")

    if isinstance(raw_structure, str):
        structure_file_payload = json.loads(raw_structure)
    else:
        structure_file_payload = raw_structure

    if "blocks" not in structure_file_payload:
        raise ValueError("structure_file_payload missing 'blocks'")

    if data_sources and len(data_sources) > 5:
        data_sources = normalize_json_field(data_sources)
    if reference_sources and len(reference_sources) > 5:
        reference_sources = normalize_json_field(reference_sources)
    if not progress:
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

    output_language = "English"
    output_word_count = 500

    last_runbook_response = ""

    if is_prev_needed:
        val = await dbserver.get_latest_runbook_result(
            user_id=user_id, runbook_id=runbook_id, result_id=result_id
        )
        if val:
            last_runbook_response = json.dumps(val.get("result"))

    # ----------------------------
    # DATA SOURCES
    # ----------------------------
    data_checked = []
    reference_RWA = []

    payload = None

    if main_source == "knowledge" or refernce_main_source == "knowledge":

        embedding = await get_firework_embedding()

        vector = embedding.embed_query(analyze_input)

        payload = QueryData(
            user_id=user_id,
            embedding=vector,
            top_k=3,
        )

        await credits.update_ai_credits_redis(
            user_id=user_id,
            credit_type="embedding",
            total_chars=len(analyze_input),
            reference_id="embedding_generation",
        )

    if main_source and data_sources:
        print("in main source")

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

    # =========================================================
    # STEP 1: ANALYZER (NEW LAYER)
    # =========================================================
    analyzer_prompt = RADAR_TEMPLATE["radar_update_analyzer_prompt"]

    analyzer_prompt = analyzer_prompt.replace(
        "{{analyze_input}}", analyze_input
    ).replace("{{last_radar_response}}", last_runbook_response)

    analysis_raw = await get_think_fire_response2_og(
        user_message=analyzer_prompt,
        user_id=user_id,
        credits=credits,
        total_input_chars=len(analyzer_prompt),
    )
    await emit(
        msg_builder.job_progress(
            job_id,
            session_id,
            "query analysis",
            "checking the query asked for modification with existing report",
            55,
        )
    )

    try:
        analysis_json = json.loads(analysis_raw)
        print("new analysis input", analysis_json)
    except:
        raise ValueError("Analyzer returned invalid JSON")

    # REQUIRED STRUCTURE
    add_blocks = analysis_json.get("addblocks", [])
    update_blocks = analysis_json.get("updateblocks", [])
    delete_blocks = analysis_json.get("deleteblocks", [])
    update_only_content = analysis_json.get("updateonlycontent", [])

    is_update_only = (
        len(add_blocks) == 0
        and len(update_blocks) == 0
        and len(delete_blocks) == 0
        and len(update_only_content) > 0
    )
    if is_update_only:
        updater_prompt_template = RADAR_TEMPLATE["radar_update_only_patch"]

        base_prompt = updater_prompt_template.replace(
            "{{last_radar_response}}", last_runbook_response
        ).replace("{{analyze_input}}", json.dumps(update_only_content))
        await emit(
            msg_builder.job_progress(
                job_id,
                session_id,
                "query analysis",
                "making only content changes as per user query",
                60,
            )
        )
    else:
        # =========================================================
        # STEP 2: UPDATER (STRICT CONTROLLED EXECUTION)
        # =========================================================
        updater_prompt_template = RADAR_TEMPLATE["radar_update_template_structure"]

        base_prompt = (
            updater_prompt_template
            # 🔥 CORE INPUTS
            .replace("{{structure_file_data}}", json.dumps(structure_file_payload))
            .replace("{{last_radar_response}}", last_runbook_response)
            # 🔥 ANALYZER OUTPUT (SOURCE OF TRUTH)
            .replace(
                "{{analyze_input}}",
                json.dumps(
                    {
                        "addblocks": add_blocks,
                        "updateblocks": update_blocks,
                        "deleteblocks": delete_blocks,
                        "updateonlycontent": update_only_content,
                    }
                ),
            )
            # 🔥 SUPPORTING DATA
            .replace("{{data_sources}}", json.dumps(data_checked))
            .replace("{{reference_sources}}", json.dumps(reference_RWA))
            .replace("{{file_data}}", file_data)
            # 🔥 CONFIG
            .replace("{{output_language}}", output_language)
            .replace("{{requested_word_count}}", str(output_word_count))
        )
        await emit(
            msg_builder.job_progress(
                job_id,
                session_id,
                "query analysis",
                "making report changes",
                60,
            )
        )

    result = await get_think_bedrok_response(
        user_message=base_prompt,
        user_id=user_id,
        credits=credits,
        total_input_chars=len(base_prompt),
        language=output_language,
        words_count=output_word_count,
        emit=emit,
        job_id=job_id,
        session_id=session_id,
        mprogress=60,
        msg_builder=msg_builder,
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

    merged_report = merge_runbook_chunks_deterministic(result)
    await emit(
        msg_builder.job_progress(
            job_id,
            session_id,
            "report generation",
            "merging content for report",
            75,
        )
    )
    merged_result = _safe_json_parse_full(merged_report)
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
    print("risk result", risk_result)

    risk_data = _safe_json_parse(risk_result)

    merged_result["risk_analysis"] = risk_data
    merged_result["risk_score"] = risk_data.get("final_risk_score", 0)
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

    return merged_result
