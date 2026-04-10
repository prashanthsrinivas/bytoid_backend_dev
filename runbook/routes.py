from datetime import datetime
import logging
import os
import traceback
import json

from playbook.background_worker import JobManager
from playbook.helperzz import assign_runbook_playbook
from runbook.helper import (
    Modify_default_structure,
    activate_runbook_schedule,
    fetch_cloudwatch_logs,
    get_playbook_instruction,
    parse_cloudwatch_url,
    reconstruct_sources,
    run_runbook_execution_engine,
    save_runbook_schedule,
    schedule_runbook_log,
    store_runbook_trigger_schedule,
    structure_payload_generation,
    trigger_runbook_from_playbook,
)
from db.lance_db_service import LanceDBServer
from credits_route.route import Credits
from db.rds_db import connect_to_rds

from radar.radar_helpers import (
    extract_file_payload,
)
from flask import Blueprint, jsonify, request, session
from services.redis_service import RedisService
from utils.s3_utils import upload_any_file


runbook_bp = Blueprint("runbook", __name__)
logger = logging.getLogger(__name__)
dbserver = LanceDBServer()


async def execute_runbook_create(data):
    import time, uuid, os, json, traceback

    user_id = data.get("user_id")

    json_files = data.get("files") or []
    structure_file = data.get("structure_file")
    default_view_template = data.get("default_view_template") or ""

    structure_file_data = None
    if structure_file:
        structure_file_data = extract_file_payload(
            structure_file,
            default_filename="structure_file",
        )

    files_data = []
    for i, f in enumerate(json_files):
        payload = extract_file_payload(f, default_filename=f"file_{i}")
        if payload:
            files_data.append(payload)

    files_data = files_data or None

    data_sources_full = json.dumps(data.get("data_sources", {}))
    reference_sources_full = json.dumps(data.get("reference_sources", {}))

    runbook_data = {
        "runbook_id": f"runbook_{uuid.uuid4().hex[:6]}",
        "user_id": user_id,
        "name": data.get("name"),
        "description": data.get("description"),
        "runbook_type": data.get("runbook_type"),
        "schedule": data.get("schedule"),
        "input_type": data.get("input_type"),
        "playbook_id": data.get("playbook_id"),
        "structure_theme": json.dumps(default_view_template),
        "api_endpoint": data.get("endpoint_id"),
        "app_id": data.get("app_id"),
        "log_source": data.get("log_source"),
        "files": json.dumps(data.get("files", {})),
        "links": json.dumps(data.get("links", {})),
        "data_sources": data_sources_full,
        "reference_sources": reference_sources_full,
        "main_source": data.get("main_source"),
        "refernce_main_source": data.get("refernce_main_source"),
        "is_template": data.get("is_template"),
        "created_at": int(time.time()),
    }
    print("loc 1")

    log_source = None

    # FILE or CloudWatch logic (same as yours)
    if data.get("log_source"):
        parsed = parse_cloudwatch_url(data.get("log_source"))

        if parsed["status"] != "success":
            raise Exception("Invalid CloudWatch URL")

        cw_result = fetch_cloudwatch_logs(
            log_group=parsed["log_group"],
            log_stream=parsed["log_stream"],
            region=parsed["region"],
        )

        if cw_result["status"] != "success":
            raise Exception(cw_result["error"])

        log_source = cw_result["logs"]

    runbook_data["log_source"] = log_source

    dbserver = LanceDBServer()
    conn = connect_to_rds()
    credits = Credits(db=conn)
    print("loc 2")

    res = ""
    if structure_file_data:
        filename = f'structure_file_{runbook_data["runbook_id"]}.json'
        config_local_path = os.path.join("/tmp", filename)

        with open(config_local_path, "w", encoding="utf-8") as f:
            json.dump(structure_file_data, f, ensure_ascii=False, indent=2)

        res = upload_any_file(
            file_path=config_local_path,
            user_id=user_id,
            type="structure_file",
            file_name=filename,
        )

        runbook_data["files"]["structure_file"] = res.get("s3_key")
        default_view_template = await structure_payload_generation(
            analyze_input="", structure_file=structure_file_data
        )
        runbook_data["structure_theme"] = default_view_template

    result = await dbserver.insert_runbook(runbook_data)

    # runbook_data["app_id"] = data.get("app_id")
    # runbook_data["main_source"] = data.get("main_source")
    # runbook_data["reference_main_source"] = data.get("refernce_main_source")

    # runbook_data["api_source_type"] = data.get("api_source_type")

    # runbook_data["data_sources"] = data_sources_full
    # runbook_data["reference_sources"] = reference_sources_full
    # runbook_data["is_template"] = data.get("is_template")
    print("loc 3")

    # EXECUTION
    if runbook_data.get("input_type") == "logs":
        schedule_runbook_log(runbook_data)
        print("loc 4")

    elif runbook_data.get("input_type") == "api":
        print("loc 5")
        latest = await dbserver.get_app_runs(
            user_id=user_id,
            app_id=str(runbook_data.get("app_id")),
            endpoint_id=str(runbook_data.get("api_endpoint")),
        )

        if isinstance(latest, list) and latest:
            latest = sorted(latest, key=lambda x: x.get("created_at", 0), reverse=True)[
                0
            ]

            runbook_data["runtime_input"] = (
                latest.get("response") or latest.get("text") or json.dumps(latest)
            )

            await run_runbook_execution_engine(
                conn=conn,
                dbserver=dbserver,
                credits=credits,
                user_id=user_id,
                runbook=runbook_data,
                structure_file=runbook_data["files"].get("structure_file"),
                structure_file_payload=default_view_template,
            )

    elif runbook_data.get("input_type") == "playbook":
        print("loc 6")
        if runbook_data["is_template"]:
            assign_runbook_playbook(
                runbook_id=runbook_data["runbook_id"],
                playbook=runbook_data["playbook_id"],
                userid=user_id,
            )
            # await run_runbook_execution_engine(
            #             conn=conn,
            #             dbserver=dbserver,
            #             credits=credits,
            #             user_id=user_id,
            #             runbook=runbook_data,
            #             structure_file=(
            #                 runbook_data["files"][0] if runbook_data.get("files") else None
            #             ),
            #         )
        else:
            await run_runbook_execution_engine(
                conn=conn,
                dbserver=dbserver,
                credits=credits,
                user_id=user_id,
                runbook=runbook_data,
                structure_file=(runbook_data["files"].get("structure_file"),),
                structure_file_payload=default_view_template,
            )

    return result


@runbook_bp.route("/runbook/create", methods=["POST"])
async def create_runbook():

    if request.is_json:
        data = request.get_json()
    else:
        data = request.form.to_dict()

    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401

    # 🔥 SUBMIT BACKGROUND JOB
    job_id = await JobManager.submit_job(execute_runbook_create, data)

    return jsonify({"success": True, "job_id": job_id, "status": "queued"})


@runbook_bp.route("/runbook/status/<job_id>", methods=["GET"])
async def get_job_status(job_id):
    try:
        redis_service = RedisService()

        job = await redis_service.get(f"job:{job_id}")

        if not job:
            return jsonify({"error": "Job not found"}), 404

        return jsonify(job), 200
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


async def execute_modify_runbook(data):
    try:

        conn = connect_to_rds
        dbserver = LanceDBServer()
        credits = Credits(conn)

        user_id = data.get("user_id")
        runbook_id = data.get("runbook_id")
        result_id = data.get("result_id")
        analyze_input = data.get("analyze_input") or data.get("user_input")

        runbook_data = await dbserver.get_runbook_by_id(user_id, runbook_id)
        updates = {}
        structure_file_data = None
        if data.get("structure_file"):
            structure_file_data = extract_file_payload(
                data.get("structure_file"),
                default_filename="structure_file",
            )

        data_sources_full = data.get("data_sources", {})
        data_sources_db = extract_filenames(data_sources_full) or []
        res = ""
        user_structure_file = None
        if structure_file_data:
            filename = f"structure_file_{result_id}.json"
            config_local_path = os.path.join("/tmp", filename)

            with open(config_local_path, "w", encoding="utf-8") as f:
                json.dump(structure_file_data, f, ensure_ascii=False, indent=2)

            res = upload_any_file(
                file_path=config_local_path,
                user_id=user_id,
                type="structure_file",
                file_name=filename,
            )

            user_structure_file = res.get("s3_key")

        # updated_runbook = await dbserver.update_runbook(user_id,runbook_id,updates)
        if isinstance(runbook_data, list):
            runbook_data = runbook_data[0] if runbook_data else None

        if isinstance(runbook_data, str):
            runbook_data = json.loads(runbook_data)

        runbook_data["analyze_input"] = analyze_input
        if not runbook_data.get("data_sources_full"):
            runbook_data["data_sources_full"] = reconstruct_sources(
                runbook_data.get("data_sources", [])
            )
        if not runbook_data.get("reference_sources_full"):
            runbook_data["reference_sources_full"] = reconstruct_sources(
                runbook_data.get("reference_sources", [])
            )

        runbook_data["main_source"] = "knowledge"
        runbook_data["reference_main_source"] = "knowledge"
        structure_file = (
            (runbook_data.get("files") or [])[0]
            if isinstance(runbook_data.get("files"), list)
            else []
        )
        print("structure_file: ", structure_file)
        # runbook_data["data_sources_full"]["filenames"] += data_sources_full["filenames"]

        return await run_runbook_execution_engine(
            conn=conn,
            credits=credits,
            user_id=user_id,
            runbook=runbook_data,
            structure_file=user_structure_file
            or structure_file,  # updated_runbook["files"][0] if updated_runbook.get("files") else None,
            result_id=result_id,
        )

    except Exception as e:
        print("error", e)


@runbook_bp.route("/runbook/modify", methods=["POST"])
async def modify_runbook():
    if request.is_json:
        data = request.get_json()
    else:
        data = request.form.to_dict()

    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401

    # 🔥 SUBMIT BACKGROUND JOB
    job_id = await JobManager.submit_job(execute_modify_runbook, data)

    return jsonify({"success": True, "job_id": job_id, "status": "queued"})


@runbook_bp.route("/runbook/results/<runbook_id>", methods=["GET"])
async def get_runbook_results(runbook_id):

    user_id = session.get("user_id")

    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401

    results = await dbserver.get_runbook_results(user_id, runbook_id)
    runbook_details = await dbserver.get_runbook_by_id(user_id, runbook_id)
    filtered_results = [
        r
        for r in results
        if r.get("status") == "completed" and (r.get("risk_score")) != 0
    ]
    # filtered_results = [r for r in results if r.get("status") == "completed"]

    return (
        jsonify(
            {"success": True, "results": filtered_results, "runbook": runbook_details}
        ),
        200,
    )


@runbook_bp.route("/runbook/results_list/<user_id>", methods=["GET"])
async def redult_list(user_id):
    try:
        result = await dbserver.get_runbook_results_by_user_id(user_id)
        runbooks = await dbserver.get_all_runbooks(user_id)
        runbook_ids = {rb.get("runbook_id") for rb in runbooks if rb.get("runbook_id")}
        filtered_results = [
            r
            for r in result
            if r.get("status") == "completed"
            and (r.get("risk_score") or 0) != 0
            and r.get("runbook_id") in runbook_ids
        ]
        # filtered_results = [
        #     r
        #     for r in result
        #     if r.get("status") == "completed" and r.get("runbook_id") in runbook_ids
        # ]

        return (
            jsonify(
                {"success": True, "results": filtered_results, "runbook": runbooks}
            ),
            200,
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@runbook_bp.route("/runbooks/list/<user_id>", methods=["GET"])
async def list_runbooks(user_id):

    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401

    # dbserver = LanceDBServer()
    runbooks = await dbserver.get_all_runbooks(user_id)

    return jsonify({"success": True, "runbooks": runbooks})


@runbook_bp.route("/runbook/<runbook_id>", methods=["GET"])
async def get_runbook(runbook_id):

    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401
    # dbserver = LanceDBServer()
    runbook = await dbserver.get_runbook_by_id(user_id, runbook_id)

    return jsonify({"success": True, "runbook": runbook})


@runbook_bp.route("/allrunbook/<user_id>", methods=["GET"])
async def get_all_runbook(user_id):
    try:
        # dbserver = LanceDBServer()
        result = await dbserver.get_user_runbook(user_id)
        return jsonify({"result": result, "all": len(result)}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@runbook_bp.route("/runbook/delete/<runbook_id>", methods=["DELETE"])
async def delete_runbook(runbook_id):
    try:
        user_id = session.get("user_id")
        if not user_id:
            return jsonify({"error": "Unauthorized"}), 401
        # dbserver = LanceDBServer()
        await dbserver.delete_runbook(user_id, runbook_id)
        await dbserver.delete_runbook_result(user_id, runbook_id)

        return jsonify({"success": True}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@runbook_bp.route("/runbook/delete_all", methods=["POST"])
async def delete_all():
    try:
        data = request.get_json()
        user_id = data.get("user_id")
        runbook_id = data.get("runbook_id", [])
        if not user_id:
            return jsonify({"error": "Unauthorized"}), 401
        # dbserver = LanceDBServer()
        await dbserver.delete_all_runbook(user_id, runbook_id)

        return jsonify({"success": True, "deleted_ids": len(runbook_id)}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@runbook_bp.route("/runbook/delete_result", methods=["DELETE"])
async def delte_result():
    try:
        data = request.get_json()
        user_id = data.get("user_id")
        runbook_id = data.get("runbook_id")
        result_id = data.get("result_id")
        if not user_id:
            return jsonify({"error": "Unauthorized"}), 401

        await dbserver.delete_runbook_result_by_id(user_id, runbook_id, result_id)

        return jsonify({"success": True}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@runbook_bp.route("/runbook/update/<runbook_id>", methods=["POST"])
async def update_runbook_api(runbook_id):
    try:
        data = request.json or {}

        user_id = data.get("user_id")

        if not user_id:
            return jsonify({"error": "Unauthorized"}), 401

        # Remove protected fields
        updates = {
            k: v
            for k, v in data.items()
            if k
            not in [
                "runbook_id",
                "user_id",
            ]
        }

        updated = await dbserver.update_runbook(user_id, runbook_id, updates)

        return jsonify({"success": True, "runbook": updated}), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@runbook_bp.route("/runbook/results_delete/<runbook_id>", methods=["DELETE"])
async def delete_runbook_results(runbook_id):
    try:
        user_id = session.get("user_id")
        if not user_id:
            return jsonify({"error": "Unauthorized"}), 401
        # dbserver = LanceDBServer()
        await dbserver.delete_runbook_result(user_id, runbook_id)

        return jsonify({"success": True}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@runbook_bp.route("/create_playbook_runbook", methods=["POST"])
async def create_playbook_runbook():
    # from utils.celery_base import create_playbook_runbook_task

    data = request.get_json()
    user_id = data.get("user_id")
    playbook_id = data.get("playbook_id")
    runbook_id = data.get("runbook_id")

    if not user_id or not playbook_id:
        return jsonify({"error": "Missing user_id or playbook_id"}), 400
    try:

        # create_playbook_runbook_task.delay(user_id, playbook_id)
        result = await trigger_runbook_from_playbook(
            playbook_id=playbook_id, user_id=user_id, runbook_id=runbook_id
        )

        return jsonify({"status": result}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@runbook_bp.route("/runbook/check_playbook/<playbook_id>", methods=["GET"])
async def check_playbook_runbook(playbook_id):
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401

    try:
        result = await dbserver.get_runbook_by_playbookid(user_id, playbook_id)
        print(result)
        if not result:
            return jsonify({"status": False, "message": "No runbook is present"}), 400
        return jsonify({"status": True, "result": result}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


def extract_filenames(source):
    if not source:
        return []

    # Case 1: dict with filenames
    if isinstance(source, dict):
        files = source.get("filenames", [])

        result = []
        for f in files:
            if not isinstance(f, dict):
                continue

            ftype = f.get("type")

            if ftype == "scrape" and f.get("url"):
                result.append(f"scrape:{f.get('url')}")

            elif f.get("filename"):
                result.append(f"{ftype}:{f.get('filename')}")

        return result

    # Case 2: already list[str]
    if isinstance(source, list):
        return [str(x) for x in source if x]

    return []


# ai changesin blocks of report


@runbook_bp.route("/result/<result_id>", methods=["GET"])
async def result_by_id(result_id):
    try:
        user_id = session.get("user_id")
        res = await dbserver.runbook_get_result(user_id, result_id)
        return jsonify({"result": res}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@runbook_bp.route("/schedule_runbook", methods=["POST"])
async def schedule_runbook():
    import json
    from datetime import datetime

    body = request.json or {}

    user_id = body["user_id"]
    runbook_id = body["runbook_id"]
    scheduled = body.get("scheduledActivation", {})

    schedule_type = scheduled.get("frequency")
    timezone = scheduled.get("timezone", "UTC")

    if not schedule_type:
        return jsonify({"error": "Missing frequency"}), 400

    # ----------------------------------------
    # Normalize schedule data
    # ----------------------------------------
    data = {
        "startTime": scheduled.get("startTime"),
        "weekday": scheduled.get("weekday"),
        "datetime": scheduled.get("datetime"),
    }

    # ========================================
    # STEP 1: SAVE SCHEDULE
    # ========================================
    result = await save_runbook_schedule(
        user_id=user_id,
        runbook_id=runbook_id,
        schedule_type=schedule_type,
        timezone=timezone,
        data=data,
    )

    # ========================================
    # STEP 2: ACTIVATE SCHEDULE (CELERY)
    # ========================================
    # result = await activate_runbook_schedule(user_id, runbook_id)

    return jsonify(
        {
            "status": "success",
            "runbook_id": runbook_id,
            "schedule_type": schedule_type,
            "scheduler_result": result,
        }
    )


@runbook_bp.route("/runbook/structure_extract", methods=["POST"])
async def structure_extract():
    try:
        data = request.get_json()

        user_id = data.get("user_id")
        analyze_input = data.get("analyze_input")
        structure_file = data.get("structure_file")
        default_structure = data.get("default_structure")

        if not default_structure and not structure_file:
            with open("runbook/default_temp.json", "r", encoding="utf-8") as file:
                default_structure = json.load(file)

        if structure_file:
            structure_file_payload = await structure_payload_generation(
                user_id=user_id,
                analyze_input=analyze_input or "",
                structure_file=structure_file,
            )
        else:
            structure_file_payload = await Modify_default_structure(
                user_id=user_id,
                analyze_input=analyze_input or "",
                default_structure=default_structure,
            )
        return (
            jsonify({"success": True, "data": structure_file_payload}),
            200,
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@runbook_bp.route("/runbook/structure_extract_modify", methods=["POST"])
async def structure_extract_modify():
    try:
        data = request.get_json()

        user_id = data.get("user_id")
        runbook_id = data.get("runbook_id")
        default_structure = data.get("default_structure")

        updates = {
            "structure_theme": default_structure  
        }

        updated_row = await dbserver.update_runbook(user_id=user_id,
                                               runbook_id=runbook_id,
                                               updates=updates)

        return (
            jsonify({"success": True, "data": updated_row}),
            200,
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@runbook_bp.route("/test/check/runbooks", methods=["GET"])
async def check_runbook_imple():
    from utils.fireworkzz import get_think_fire_response2_og
    from utils.normal import load_yaml_file
    from cust_helpers import pathconfig
    from radar.radar_helpers import _safe_json_parse

    RADAR_TEMPLATE = load_yaml_file(path=pathconfig.radar_prompts)
    print("RADAR_TEMPLATE type:", type(RADAR_TEMPLATE))
    risk_prompt_template = RADAR_TEMPLATE.get("nist_risk_score_prompt")
    user_id = "109161866299858012556"
    result_id = "result_ee166f"
    credits = Credits()
    file_data = [
        {
            "filename": "Risk-Assessment-Report-Template.pdf",
            "type": ".pdf",
            "content": "I\nAPPENDIX C: RISK ASSESSMENT REPORT TEMPLATE\nRISK ASSESSMENT REPORT (RAR)\n<ORGANIZATION> <SYSTEM NAME> <DATE>\nRecord of Changes:\nVersion\nDate\nSections Modified\nDescription of Changes\n1.0\nDD MM YY\nInitial RAR\nSystem Description\nThe <System Name/Unique Identifier> consists of <System Description> processing <Classification Level> data. The risk categorization for this system is assessed as <e.g., Moderate-Low-Low>.\n< System Name/Unique Identifier> is located <insert physical environment details>. The system <list all system connections and inter-connections, or state “has no connections, (wired or wireless)>. This system is used for <system purpose/function>, in support of performance on the <list all program and/or contract information>. The system <provide any system-specific details, such as Mobility>.\nThe Information Owner is <insert POC information, including address and phone number>.\nThe Information System Security Manager (ISSM) is <insert Point of Contact (POC) information, including address and phone number>.\nThe Information System Security Officer (ISSO) is <insert POC information, including address and phone number>.\nScope\nThe scope of this risk assessment is focused on the system’s use of resources and controls to mitigate vulnerabilities exploitable by threat agents (internal and external) identified during the Risk Management Framework (RMF) control selection process, based on the system’s categorization.\nThis initial assessment will be a Tier 3 or “information system level” risk assessment. While not entirely comprehensive of all threats and vulnerabilities to the system, this assessment will include any known risks related to the incomplete or inadequate implementation of the National Institute of Standards and Technology (NIST) Special Publication (SP) 800-53 controls selected for this system. This document will be updated after certification testing to include any vulnerabilities or observations by the independent\nPage | 1\nI\nassessment team. Data collected during this assessment may be used to support higher level risk assessments at the mission/business or organization level.\n<Identify assumptions, constraints, timeframe. This section will include the following information:\nRange or scope of threats considered in the assessment\nSummary of tools/methods used to ensure NIST SP 800-53 compliance\nDetails regarding any instances of non-compliance\nRelevant operating conditions and physical security conditions\nTimeframe supported by the assessment (Example: security-relevant changes that are anticipated before the authorization, expiration of the existing authorization, etc.).>\nPurpose\n<Provide details on why this risk assessment is being conducted, including whether it is an initial or other subsequent assessment, and state the circumstances that prompted the assessment. Example: This initial risk assessment was conducted to document areas where the selection and implementation of RMF controls may have left residual risk. This will provide security control assessors and authorizing officials an upfront risk profile.>\nRisk Assessment Approach\nThis initial risk assessment was conducted using the guidelines outlined in the NIST SP 800-30, Guide for Conducting Risk Assessments. A <SELECT QUALITATIVE / QUANTITATIVE / SEMI-QUANTITATIVE> approach will be utilized for this assessment. Risk will be determined based on a threat event, the likelihood of that threat event occurring, known system vulnerabilities, mitigating factors, and consequences/impact to mission.\nThe following table is provided as a list of sample threat sources. Use this table to determine relevant threats to the system.\nTable 1: Sample Threat Sources (see NIST SP 800-30 for complete list)\nTYPE OF THREAT SOURCE\nDESCRIPTION\nADVERSARIAL - Individual (outsider, insider, trusted, privileged) - Group (ad-hoc or established) - Organization (competitor, supplier, partner,\ncustomer) - Nation state\nIndividuals, groups, organizations, or states that seek to exploit the organization’s dependence on cyber resources (e.g., information in electronic form, information and communications, and the communications and information-handling capabilities provided by those technologies.)\nPage | 2\nI\nTYPE OF THREAT SOURCE\nDESCRIPTION\nADVERSARIAL - Standard user - Privileged user/Administrator\nErroneous actions taken by individuals in the course of executing everyday responsibilities.\nSTRUCTURAL - IT Equipment (storage, processing, comm., display,\nsensor, controller)\nEnvironmental conditions\nFailures of equipment, environmental controls, or software due to aging, resource depletion, or other circumstances which exceed expected operating parameters.\nTemperature/humidity controls \uf0b7 Power supply\nSoftware\nOperating system \uf0b7 Networking \uf0b7 General-purpose application \uf0b7 Mission-specific application\nENVIRONMENTAL - Natural or man-made (fire, flood, earthquake, etc.) - Unusual natural event (e.g., sunspots) - Infrastructure failure/outage (electrical, telecomm)\nNatural disasters and failures of critical infrastructures on which the organization depends, but is outside the control of the organization. Can be characterized in terms of severity and duration.\nThe following tables from the NIST SP 800-30 were used to assign values to likelihood, impact, and risk:\nTable 2: Assessment Scale – Likelihood of Threat Event Initiation (Adversarial)\nQualitative Values\nSemi-Quantitative Values\nDescription\nVery High\n96-100\n10\nAdversary is almost certain to initiate the threat event.\nHigh\n80-95\n8\nAdversary is highly likely to initiate the threat event.\nModerate\n21-79\n5\nAdversary is somewhat likely to initiate the threat event.\nLow\n5-20\n2\nAdversary is unlikely to initiate the threat event.\nVery Low\n0-4\n0\nAdversary is highly unlikely to initiate the threat event\nPage | 3\nI\nTable 3: Assessment Scale – Likelihood of Threat Event Occurrence (Non-adversarial)\nQualitative Values\nSemi-Quantitative Values\nDescription\nVery High\n96-100\n10\nError, accident, or act of nature is almost certain to occur; or occurs more than 100 times per year.\nHigh\n80-95\n8\nError, accident, or act of nature is highly likely to occur; or occurs between 10-100 times per year.\nModerate\n21-79\n5\nError, accident, or act of nature is somewhat likely to occur; or occurs between 1-10 times per year.\nLow\n5-20\n2\nError, accident, or act of nature is unlikely to occur; or occurs less than once a year, but more than once every 10 years.\nVery Low\n0-4\n0\nError, accident, or act of nature is highly unlikely to occur; or occurs less than once every 10 years.\nTable 4: Assessment Scale – Impact of Threat Events\nQualitative Values\nSemi-Quantitative Values\nDescription\nVery High\n96-100\n10\nThe threat event could be expected to have multiple severe or catastrophic adverse effects on organizational operations, organizational assets, individuals, other organizations, or the Nation.\nHigh\n80-95\n8\nThe threat event could be expected to have a severe or catastrophic adverse effect on organizational operations, organizational assets, individuals, other organizations, or the Nation. A severe or catastrophic adverse effect means that, for example, the threat event might: (i) cause a severe degradation in or loss of mission capability to an extent and duration that the organization is not able to perform one or more of its primary functions; (ii) result in major damage to organizational assets; (iii) result in major financial loss; or (iv) result in severe or catastrophic harm to individuals involving loss of life or serious life threatening injuries.\nPage | 4\nI\nQualitative Values\nModerate\nLow\nVery Low\nQualitative Values\nVery High\nHigh\nModerate\nLow\nSemi-Quantitative Values\nDescription\n21-79\n5\nThe threat event could be expected to have a serious adverse effect on organizational operations, organizational assets, individuals other organizations, or the Nation. A serious adverse effect means that, for example, the threat event might: (i) cause a significant degradation in mission capability to an extent and duration that the organization is able to perform its primary functions, but the effectiveness of the functions is significantly reduced; (ii) result in significant damage to organizational assets; (iii) result in significant financial loss; or (iv) result in significant harm to individuals that does not involve loss of life or serious life threatening injuries.\n5-20\n2\nThe threat event could be expected to have a limited adverse effect on organizational operations, organizational assets, individuals other organizations, or the Nation. A limited adverse effect means that, for example, the threat event might: (i) cause a degradation in mission capability to an extent and duration that the organization is able to perform its primary functions, but the effectiveness of the functions is noticeably reduced; (ii) result in minor damage to organizational assets; (iii) result in minor financial loss; or (iv) result in minor harm to individuals.\n0-4\n0\nThe threat event could be expected to have a negligible adverse effect on organizational operations, organizational assets, individuals other organizations, or the Nation.\nTable 5: Assessment Scale – Level of Risk\nSemi-Quantitative Values\nDescription\n96-100\n10\nThreat event could be expected to have multiple severe or catastrophic adverse effects on organizational operations, organizational assets, individuals, other organizations, or the Nation.\n80-95\n8\nThreat event could be expected to have a severe or catastrophic adverse effect on organizational operations, organizational assets, individuals, other organizations, or the Nation.\n21-79\n5\nThreat event could be expected to have a serious adverse effect on organizational operations, organizational assets, individuals, other organizations, or the Nation.\n5-20\n2\nThreat event could be expected to have a limited adverse effect on organizational operations, organizational assets, individuals, other organizations, or the Nation.\nPage | 5\nI\nQualitative Values\nSemi-Quantitative Values\nDescription\nVery Low\n0-4\n0\nThreat event could be expected to have a negligible adverse effect on organizational operations, organizational assets, individuals, other organizations, or the Nation.\nTable 6: Assessment Scale – Level of Risk (Combination of Likelihood and Impact)\nLikelihood (That Occurrence Results in Adverse Impact)\nVery Low\nLow\nLevel of Impact\nModerate\nHigh\nVery High\nVery High\nVery Low\nLow\nModerate\nHigh\nVery High\nHigh\nVery Low\nLow\nModerate\nHigh\nVery High\nModerate\nVery Low\nLow\nModerate\nModerate\nHigh\nLow\nVery Low\nLow\nLow\nLow\nModerate\nVery Low\nVery Low\nVery Low\nVery Low\nLow\nLow\nPage | 6\nI\nRisk Assessment Approach\nDetermine relevant threats to the system. List the risks to system in the Risk Assessment Results table below and detail the relevant mitigating factors and controls. Refer to NIST SP 800-30 for further guidance, examples, and suggestions.\nRisk Assessment Results\nThreat Event\nVulnerabilities / Predisposing Characteristics\nMitigating Factors\nSecurity Control(s)\nLikelihood (Tables 2 & 3)\nImpact (Table 4)\nRisk (Tables 5 & 6)\ne.g. Hurricane\nPower Outage\nBackup generators\nPE-12\nModerate\nLow\nLow\nLikelihood / Impact / Risk = Very High, High, Moderate, Low, or Very Low\n_____________________________\nSignature Government Information Owner\n_____________________________\nPrinted Name, Title, and Phone Number\nNote: Information Owner acknowledgment is only provided if necessary or required by the DCSA AO. (Examples: Legacy Operating Systems, Risk concerns raised based on the results of the RAR, deviations from the DCSA baseline, etc.)\nPage | 7",
        }
    ]
    refactor_result = await dbserver.runbook_get_result(user_id, result_id)
    risk_prompt = risk_prompt_template.replace(
        "{{analysis_result}}", json.dumps(refactor_result)
    ).replace("{{report_data}}", json.dumps(file_data) if file_data else "")

    risk_llm_result = await get_think_fire_response2_og(
        user_message=risk_prompt,
        user_id=user_id,
        credits=credits,
        total_input_chars=len(risk_prompt),
    )

    # print("RISK RAW:", risk_llm_result)

    risk_data = _safe_json_parse(risk_llm_result)

    risk_score = risk_data.get("final_risk_score", 0)

    return jsonify({"status": risk_data})


@runbook_bp.route("/test/check_rst/runbooks", methods=["POST"])
async def check_runbook_structure():
    from utils.fireworkzz import get_think_fire_response2_og
    from utils.normal import load_yaml_file
    from cust_helpers import pathconfig
    from radar.radar_helpers import _safe_json_parse, process_file_payloads

    import json

    # ✅ Get form-data instead of JSON
    user_analyze_input = request.form.get("user_input")
    user_file = request.files.get("userfile")  # file comes from form-data

    RADAR_TEMPLATE = load_yaml_file(path=pathconfig.radar_prompts)
    print("RADAR_TEMPLATE type:", type(RADAR_TEMPLATE))

    credits = Credits()
    structure_prompt = RADAR_TEMPLATE.get("structure_prompt_template")

    user_id = "109161866299858012556"

    structure_file_payload = []

    # ✅ Process uploaded file
    process_file_payloads(
        user_id=user_id,
        files=[user_file] if user_file else [],
        inp_links=[],
        extracted_payload=structure_file_payload,
    )
    print(structure_file_payload)

    # ✅ Build prompt
    structure_prompt = (
        structure_prompt.replace(
            "{{document_file_data}}", json.dumps(structure_file_payload)
        )
        .replace("{{file_links}}", "")
        .replace(
            "{{user_original_prompt_or_context}}",
            user_analyze_input or "",
        )
        .replace("{{output_language}}", "English")
    )

    base_chars = len(structure_prompt)

    # ✅ Call LLM
    result = await get_think_fire_response2_og(
        user_message=structure_prompt,
        user_id=user_id,
        credits=credits,
        total_input_chars=base_chars,
    )

    structure_file_payloads = json.loads(result)
    logger.info("✅ STRUCTURE GENERATED  %s", structure_file_payloads)

    return jsonify({"status": structure_file_payloads})


# @runbook_bp.route("/updateschema", methods=["POST"])
# async def update_schema():
#     try:
#         data = request.get_json()
#         user_id = data.get("user_id")
#         dbserver = LanceDBServer()
#         res = await dbserver.migrate_runbook_table(user_id=user_id)
#         return jsonify({"message": res}), 200
#     except Exception as e:
#         return jsonify({"error": str(e)})
