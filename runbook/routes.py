import base64
from datetime import datetime
import logging
import os
import traceback
import json
import time, uuid, traceback
from utils.app_configs import IS_DEV
from playbook.background_worker import JobManager
from playbook.helperzz import assign_runbook_playbook
from runbook.helper import (
    Modify_default_structure,
    analyze_questions_with_references,
    extract_qna_from_instruction,
    fetch_cloudwatch_logs,
    merge_document_data,
    parse_cloudwatch_url,
    run_runbook_execution_engine,
    save_runbook_schedule,
    schedule_runbook_log,
    structure_payload_generation,
    trigger_runbook_from_playbook,
)
from db.lance_db_service import LanceDBServer
from credits_route.route import Credits

from radar.radar_helpers import (
    extract_file_payload,
)
from flask import Blueprint, jsonify, request, session, g
from runbook.helper2 import modify_run_runbook_execution_engine
from runbook.utils import get_playbook_instruction, send
from services.redis_service import RedisService
from utils.s3_utils import upload_any_file
import time, uuid, os, json
from datetime import datetime

from websockets_custom.ws_instance import ws_service, msg_builder_main

runbook_bp = Blueprint("runbook", __name__)
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG if IS_DEV else logging.INFO)
dbserver = LanceDBServer()

ws_sender = ws_service
msg_builder = msg_builder_main


async def execute_runbook_create(data, job_id=None, session_id=None):
    user_id = data.get("user_id")
    progress = 0

    # ✅ single flag
    should_emit = bool(job_id and session_id)

    async def emit(msg):
        if should_emit:
            await send(ws_sender, msg, user_id)

    try:
        # 🚀 INIT
        await emit(
            msg_builder.job_progress(
                job_id, session_id, "init", "Starting runbook setup...", 5
            )
        )

        # 📂 FILE PROCESSING
        progress = 10
        await emit(
            msg_builder.job_progress(
                job_id,
                session_id,
                "files_processing",
                "Processing uploaded files...",
                progress,
            )
        )

        json_files = data.get("files") or []
        structure_file = data.get("structure_file")
        default_view_template = data.get("default_view_template") or ""

        structure_file_data = None
        if structure_file:
            structure_file_data = extract_file_payload(
                structure_file, default_filename="structure_file"
            )

        files_data = [
            extract_file_payload(f, default_filename=f"file_{i}")
            for i, f in enumerate(json_files)
            if f
        ] or None
        data_sources_full = json.dumps(data.get("data_sources", {}))
        reference_sources_full = json.dumps(data.get("reference_sources", {}))

        # 📦 BASE DATA
        runbook_data = {
            "runbook_id": f"runbook_{uuid.uuid4().hex[:6]}",
            "user_id": user_id,
            "name": data.get("name"),
            "description": data.get("description"),
            "runbook_type": data.get("runbook_type"),
            "schedule": data.get("schedule"),
            "input_type": data.get("input_type"),
            "playbook_id": data.get("playbook_id"),
            "playbook_source": data.get("playbook_source"),
            "api_source": data.get("api_source"),
            "log_file": data.get("log_file"),
            "structure_theme": json.dumps(default_view_template),
            "api_endpoint": data.get("endpoint_id"),
            "app_id": data.get("app_id"),
            "log_source": data.get("log_source"),
            "files": {},
            "links": json.dumps(data.get("links", {})),
            "data_sources": data_sources_full,
            "reference_sources": reference_sources_full,
            "main_source": data.get("main_source"),
            "refernce_main_source": data.get("refernce_main_source"),
            "is_template": data.get("is_template"),
            "created_at": datetime.utcnow().isoformat(),
            "runbook_evidence_config": None,
        }

        # ☁️ LOG FETCH
        log_source = None
        if data.get("log_source"):
            progress = 15
            await emit(
                msg_builder.job_progress(
                    job_id,
                    session_id,
                    "log_fetch",
                    "Fetching logs from CloudWatch...",
                    progress,
                )
            )

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

        if structure_file_data:
            filename = f'structure_file_{runbook_data["runbook_id"]}.json'
            path = os.path.join("/tmp", filename)

            with open(path, "w") as f:
                json.dump(structure_file_data, f)

            res = upload_any_file(
                file_path=path,
                user_id=user_id,
                type="structure_file",
                file_name=filename,
            )
            await emit(
                msg_builder.job_progress(
                    job_id,
                    session_id,
                    "structure_preparation",
                    "Preparing runbook structure...",
                    12,
                )
            )

            runbook_data["files"]["structure_file"] = res.get("s3_key")

            default_view_template = await structure_payload_generation(
                user_id=user_id,
                analyze_input="",
                structure_file=structure_file_data,
                emit=emit,
                job_id=job_id,
                session_id=session_id,
                mprogress=12,
            )

            runbook_data["structure_theme"] = json.dumps(default_view_template)
            progress = 15

            if default_view_template:
                if "blocks" in default_view_template:
                    val = default_view_template["blocks"]
                    if val:
                        await emit(
                            msg_builder.job_progress(
                                job_id,
                                session_id,
                                "structure_preparation",
                                "structure for report is generated successfully",
                                15,
                            )
                        )
                await emit(
                    msg_builder.job_progress(
                        job_id,
                        session_id,
                        "structure_preparation",
                        "structure for report is generated successfully",
                        15,
                    )
                )

        runbook_data["files"] = json.dumps(runbook_data["files"])

        # 💾 DB SAVE
        await emit(
            msg_builder.job_progress(
                job_id,
                session_id,
                "db_save",
                "Saving runbook configuration...",
                20,
            )
        )

        result = await dbserver.insert_runbook(runbook_data)

        # ⚙️ EXECUTION FLOW
        input_type = runbook_data.get("input_type")

        if input_type == "logs" and not runbook_data["is_template"]:
            progress = 30
            await emit(
                msg_builder.job_progress(
                    job_id,
                    session_id,
                    "log_schedule",
                    "Scheduling log-based execution...",
                    progress,
                )
            )
            schedule_runbook_log(runbook_data)

        elif input_type == "api" and not runbook_data["is_template"]:
            progress = 30
            await emit(
                msg_builder.job_progress(
                    job_id,
                    session_id,
                    "api_fetch",
                    "Fetching latest API data...",
                    progress,
                )
            )

            latest = await dbserver.get_app_runs(
                user_id=user_id,
                app_id=str(runbook_data.get("app_id")),
                endpoint_id=str(runbook_data.get("api_endpoint")),
            )

            if latest:
                latest = sorted(
                    latest, key=lambda x: x.get("created_at", 0), reverse=True
                )[0]
                progress = 35

                await emit(
                    msg_builder.job_progress(
                        job_id,
                        session_id,
                        "execution",
                        "Executing runbook...",
                        progress,
                    )
                )

                runbook_data["runtime_input"] = json.dumps(latest)

                files_obj = json.loads(runbook_data.get("files")) or {}

                await run_runbook_execution_engine(
                    dbserver=dbserver,
                    user_id=user_id,
                    runbook=runbook_data,
                    structure_file=files_obj.get("structure_file"),
                    structure_file_payload=default_view_template,
                    job_id=job_id,
                    session_id=session_id,
                    progress=progress,
                )

        elif input_type == "playbook":
            progress = 30
            await emit(
                msg_builder.job_progress(
                    job_id,
                    session_id,
                    "playbook_setup",
                    "Preparing playbook execution...",
                    progress,
                )
            )

            if runbook_data["is_template"]:
                assign_runbook_playbook(
                    runbook_id=runbook_data["runbook_id"],
                    playbook=runbook_data["playbook_id"],
                    userid=user_id,
                )
            else:
                files_obj = json.loads(runbook_data.get("files")) or {}
                progress = 35
                await run_runbook_execution_engine(
                    dbserver=dbserver,
                    user_id=user_id,
                    runbook=runbook_data,
                    structure_file=files_obj.get("structure_file"),
                    structure_file_payload=default_view_template,
                    job_id=job_id,
                    session_id=session_id,
                    progress=progress,
                )
        if result:
            # ✅ SUCCESS
            await emit(
                msg_builder.job_success(
                    job_id,
                    session_id,
                    "Runbook created and processed successfully.",
                )
            )
        else:
            await emit(
                msg_builder.job_error(
                    job_id,
                    session_id,
                    "Runbook execution failed. Please try again.",
                )
            )

        return result

    except Exception as e:
        logger.error("Runbook execution error: %s", e, exc_info=IS_DEV)

        await emit(
            msg_builder.job_error(
                job_id,
                session_id,
                "Runbook execution failed. Please try again.",
            )
        )

        raise


@runbook_bp.route("/runbook/create", methods=["POST"])
async def create_runbook():
    try:
        # ✅ form fields only
        data = request.form.to_dict()

        user_id = data.get("user_id")
        session_id = data.get("session_id") or None

        # ✅ files
        structure_file = request.files.get("structure_file")
        files_main = request.files.getlist("files")  # ✅ FIX

        # ✅ structure file
        if structure_file:
            file_content = structure_file.read()
            data["structure_file"] = {
                "filename": structure_file.filename,
                "content_type": structure_file.content_type,
                "data": base64.b64encode(file_content).decode("utf-8"),
            }

        # ✅ multiple files
        if files_main:
            files = []
            for file in files_main:
                file_content = file.read()
                files.append(
                    {
                        "filename": file.filename,
                        "content_type": file.content_type,
                        "data": base64.b64encode(file_content).decode("utf-8"),
                    }
                )
            data["files"] = files

        if not user_id:
            return jsonify({"error": "Unauthorized"}), 401

        job_id = await JobManager.submit_job(
            execute_runbook_create,
            data,
            session_id=session_id,
        )

        return jsonify({"success": True, "job_id": job_id, "status": "queued"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


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


async def execute_modify_runbook(data, job_id=None, session_id=None):
    try:
        dbserver = LanceDBServer()

        # ✅ single flag
        should_emit = bool(job_id and session_id)

        async def emit(msg):
            if should_emit:
                await send(ws_sender, msg, user_id)

        user_id = data.get("user_id")
        runbook_id = data.get("runbook_id")
        result_id = data.get("result_id")
        analyze_input = data.get("analyze_input") or data.get("user_input")
        # 🚀 INIT
        await emit(
            msg_builder.job_progress(
                job_id, session_id, "init", "updating of runbook report started", 5
            )
        )

        runbook_data = await dbserver.get_runbook_by_id(user_id, runbook_id)
        await emit(
            msg_builder.job_progress(
                job_id,
                session_id,
                "files",
                f"Using existing template {'Successful ✅' if runbook_data else 'Unsuccessful ❌'}",
                10,
            )
        )
        if isinstance(runbook_data, list):
            runbook_data = runbook_data[0] if runbook_data else None

        if isinstance(runbook_data, str):
            runbook_data = json.loads(runbook_data)

        runbook_data["analyze_input"] = analyze_input
        # runbook_data["report_viewer"] = data.get("report_viewer")

        # -----------------------------
        # STRUCTURE FILE HANDLING
        # -----------------------------
        structure_file = data.get("structure_file")
        structure_file_payload = None

        if structure_file:
            structure_file_payload = extract_file_payload(
                structure_file, default_filename="structure_file"
            )

            filename = f"structure_file_{result_id}.json"
            path = os.path.join("/tmp", filename)

            with open(path, "w", encoding="utf-8") as f:
                json.dump(structure_file_payload, f, indent=2)

            upload_res = upload_any_file(
                file_path=path,
                user_id=user_id,
                type="structure_file",
                file_name=filename,
            )

            structure_file = upload_res.get("s3_key")
            await emit(
                msg_builder.job_progress(
                    job_id,
                    session_id,
                    "structure_preparation",
                    "Preparing runbook structure...",
                    12,
                )
            )

            structure_file_payload = await structure_payload_generation(
                user_id=user_id,
                analyze_input=analyze_input,
                structure_file=structure_file_payload,
                emit=emit,
                job_id=job_id,
                session_id=session_id,
                mprogress=12,
            )
            if structure_file_payload:
                if "blocks" in structure_file_payload:
                    val = structure_file_payload["blocks"]
                    if val:
                        await emit(
                            msg_builder.job_progress(
                                job_id,
                                session_id,
                                "structure_preparation",
                                "structure for report is generated successfully",
                                15,
                            )
                        )
                await emit(
                    msg_builder.job_progress(
                        job_id,
                        session_id,
                        "structure_preparation",
                        "structure for report is generated successfully",
                        15,
                    )
                )
        else:
            files_obj = json.loads(runbook_data.get("files") or "{}")
            structure_file = files_obj.get("structure_file")
            await emit(
                msg_builder.job_progress(
                    job_id,
                    session_id,
                    "structure_preparation",
                    "using exisitng structure",
                    15,
                )
            )

        return await modify_run_runbook_execution_engine(
            user_id=user_id,
            runbook=runbook_data,
            structure_file=structure_file,
            result_id=result_id,
            structure_file_payload=structure_file_payload
            or runbook_data.get("structure_theme"),
            is_prev_needed=True,
            job_id=job_id,
            session_id=session_id,
            progress=15,
        )

    except Exception as e:
        logger.error("Runbook trigger error: %s", e, exc_info=IS_DEV)


@runbook_bp.route("/runbook/modify", methods=["POST"])
async def modify_runbook():
    data = request.form.to_dict()

    user_id = data.get("user_id")
    session_id = data.get("session_id") or None

    # ✅ files
    structure_file = request.files.get("structure_file")
    files_main = request.files.getlist("files")  # ✅ FIX

    # ✅ structure file
    if structure_file:
        file_content = structure_file.read()
        data["structure_file"] = {
            "filename": structure_file.filename,
            "content_type": structure_file.content_type,
            "data": base64.b64encode(file_content).decode("utf-8"),
        }

    # ✅ multiple files
    if files_main:
        files = []
        for file in files_main:
            file_content = file.read()
            files.append(
                {
                    "filename": file.filename,
                    "content_type": file.content_type,
                    "data": base64.b64encode(file_content).decode("utf-8"),
                }
            )
        data["files"] = files

    user_id = data.get("user_id")
    session_id = data.get("session_id") or None
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401

    # 🔥 SUBMIT BACKGROUND JOB
    job_id = await JobManager.submit_job(
        execute_modify_runbook, data, session_id=session_id
    )

    return jsonify({"success": True, "job_id": job_id, "status": "queued"})


@runbook_bp.route("/runbook/results/<runbook_id>", methods=["GET"])
async def get_runbook_results(runbook_id):

    user_id = session.get("user_id") or request.args.get("user_id")

    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401

    results = await dbserver.get_runbook_results(user_id, runbook_id)
    runbook_details = await dbserver.get_runbook_by_id(user_id, runbook_id)
    filtered_results = [r for r in results if r.get("status") == "completed"]
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

        # ✅ Remove protected fields
        updates = {k: v for k, v in data.items() if k not in ["runbook_id", "user_id"]}

        # ✅ Normalize payload (CRITICAL FIX)
        def normalize_payload(payload):
            normalized = {}
            for k, v in payload.items():
                if isinstance(v, (dict, list)):
                    normalized[k] = json.dumps(v)
                else:
                    normalized[k] = v
            return normalized

        updates = normalize_payload(updates)

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
        logger.debug("Runbook by playbook result: %s", result)
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


async def execute_structure_extract(data, job_id=None, session_id=None, main=False):
    try:
        user_id = data.get("user_id")
        analyze_input = data.get("analyze_input")
        structure_file = data.get("structure_file")
        default_structure = data.get("default_structure")

        should_emit = bool(job_id and session_id)

        # ✅ unified emit (single payload)
        async def emit(payload):
            if should_emit:
                await send(ws_sender, payload, user_id)

        # ✅ initial message
        await emit(
            msg_builder.job_progress(
                job_id=job_id,
                session_id=session_id,
                stage="init",
                message="Starting generating structure",
                progress=5,
            )
        )

        # ✅ fallback default structure
        if not default_structure and not structure_file:
            with open("runbook/default_temp.json", "r", encoding="utf-8") as file:
                default_structure = json.load(file)

        # =============================
        # 🔹 STRUCTURE FROM FILE
        # =============================
        if structure_file:
            structure_file_data = extract_file_payload(
                structure_file,
                default_filename="structure_file",
            )
            # print("len", structure_file_data)

            structure_file_payload = await structure_payload_generation(
                user_id=user_id,
                analyze_input=analyze_input or "",
                structure_file=structure_file_data,
                emit=emit,
                job_id=job_id,
                session_id=session_id,
                mprogress=12,
            )
            if main:

                await emit(
                    msg_builder.job_success(
                        job_id=job_id,
                        session_id=session_id,
                        message="Successfully extracted structure",
                    )
                )
            else:
                await emit(
                    msg_builder.job_progress(
                        job_id=job_id,
                        session_id=session_id,
                        stage="structure",
                        message="Successfully extracted structure",
                    )
                )

        # =============================
        # 🔹 DEFAULT STRUCTURE FLOW
        # =============================
        else:
            structure_file_payload = await Modify_default_structure(
                user_id=user_id,
                analyze_input=analyze_input or "",
                default_structure=default_structure,
            )
            if main:

                await emit(
                    msg_builder.job_success(
                        job_id=job_id,
                        session_id=session_id,
                        message="Successfully modified default structure",
                    )
                )
            else:
                await emit(
                    msg_builder.job_progress(
                        job_id=job_id,
                        session_id=session_id,
                        stage="structure",
                        message="Successfully extracted structure",
                    )
                )

        return {"success": True, "data": structure_file_payload}

    except Exception as e:
        traceback_str = traceback.format_exc()
        logger.error("Structure extraction error: %s", e, exc_info=IS_DEV)

        await emit(
            msg_builder.job_error(
                job_id=job_id,
                session_id=session_id,
                message="Structure extraction failed",
            )
        )

        return {
            "success": False,
            "error": str(e),
            "trace": traceback_str,
        }


@runbook_bp.route("/runbook/structure_extract", methods=["POST"])
async def structure_extract():

    if request.is_json:
        data = request.get_json()
    else:
        data = request.form.to_dict()
    # print(data)
    uploaded_file = request.files.get("structure_file")
    # print(request.files)

    if uploaded_file:
        # You can't pass a raw file object to a background job easily
        # because it might be closed after the request ends.
        # Recommendation: Save it to a temp path or read the content.
        file_content = uploaded_file.read()
        b64_string = base64.b64encode(file_content).decode("utf-8")
        data["structure_file"] = {
            "filename": uploaded_file.filename,
            "content_type": uploaded_file.content_type,
            "data": b64_string,  # Pass the actual bytes
        }

    user_id = data.get("user_id")
    session_id = data.get("session_id") or None
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401

    # 🚀 Submit as background job
    job_id = await JobManager.submit_job(
        execute_structure_extract, data, session_id=session_id, main=True
    )

    return jsonify({"success": True, "job_id": job_id, "status": "queued"})


@runbook_bp.route("/runbook/structure_extract_modify", methods=["POST"])
async def structure_extract_modify():
    try:
        if request.is_json:
            data = request.get_json()
        else:
            data = request.form.to_dict()

        user_id = data.get("user_id")
        runbook_id = data.get("runbook_id")
        default_structure = data.get("default_structure")

        updates = {"structure_theme": json.dumps(default_structure)}

        updated_row = await dbserver.update_runbook(
            user_id=user_id, runbook_id=runbook_id, updates=updates
        )

        return (
            jsonify({"success": True, "data": updated_row}),
            200,
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@runbook_bp.route("/check_pb_output", methods=["POST"])
async def check_pb_output():
    try:
        data = request.get_json()

        user_id = data.get("user_id")
        pb_id = data.get("playbook_id")
        rb_id = data.get("runbook_id")

        # -----------------------------
        # Fetch Runbook
        # -----------------------------
        runbook = await dbserver.get_runbook_by_id(user_id=user_id, runbook_id=rb_id)

        if isinstance(runbook, list):
            runbook = runbook[0] if runbook else None

        if isinstance(runbook, str):
            runbook = json.loads(runbook)

        # -----------------------------
        # Fetch Instruction
        # -----------------------------
        runtime_input = await get_playbook_instruction(user_id, pb_id)

        # -----------------------------
        # Extract Questions
        # -----------------------------
        questions = await extract_qna_from_instruction(runtime_input)

        # -----------------------------
        # Analyze
        # -----------------------------
        # -----------------------------
        # SYNC MODE
        # -----------------------------

        document_data = []

        if runbook.get("reference_sources"):
            analyzed_results = await analyze_questions_with_references(
                questions,
                runbook.get("reference_sources"),
                runbook.get("reference_main_source"),
                user_id,
                runbook,
            )

            merged = await merge_document_data(analyzed_results, runtime_input)

            runbook["runtime_input"] = json.dumps(merged)
            document_data.append(merged.get("chat"))
            logger.debug("Document data preview: %s", document_data[0][:500])

        else:
            runbook["runtime_input"] = json.dumps(runtime_input.get("chat", []))

        return (
            jsonify(
                {
                    "status": "completed",
                    "questions": questions,
                    "final": document_data,
                }
            ),
            200,
        )

    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500
