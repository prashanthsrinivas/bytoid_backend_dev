import re
import uuid
from credits_route.route import Credits
from db.db_checkers import (
    check_subagent_by_playbook,
    create_subagent_to_playbook,
    get_subagent_by_userid,
    get_email_by_id,
    get_userid,
)
from db.rds_db import connect_to_rds
from flask import Blueprint, request, jsonify, Response, stream_with_context
import json, uuid
from cust_helpers import pathconfig
from services.redis_service import RedisService
from services.scheduler_service import SchedulerService
from services.workflow_service import WorkflowRunnerV2
from utils.fireworkzz import get_fireworks_response2
from .helperzz import *
from utils.pb_config_utils import *
from utils.normal import (
    load_yaml_file,
    read_function_jsons,
    read_function_jsons2,
    remove_not_found_entities,
)
from .background_worker import JobManager
import pytz, pymysql
from utils.FileHandler import FileProcessor
from utils.app_configs import ACCESSIBLE_IDS

playbook_bp = Blueprint("playbook", __name__)
PLAY_TEMPLATE = load_yaml_file(path=pathconfig.play_template)
MINOR_PROMPTS = load_yaml_file(path=pathconfig.minor_prompts)
ALL_FUNCTIONS = read_function_jsons2(Full=True)

from concurrent.futures import ThreadPoolExecutor
from utils.s3_utils import s3bucket, S3_BUCKET

executor = ThreadPoolExecutor(max_workers=4)


@playbook_bp.route("/create_instruction", methods=["POST"])
async def create_new_instruction():

    data = request.json

    job_id = await JobManager.submit_job(create_instruction_worker, data)

    return jsonify({"status": "accepted", "job_id": job_id})


async def create_instruction_worker(data, job_id=None, session_id=None):

    db = connect_to_rds()
    userid = data["user_id"]
    credits = Credits(db)

    try:

        db.begin()

        total_input_chars = 5000

        if not await credits.has_ai_credits(
            total_chars=total_input_chars,
            user_id=userid,
        ):
            db.rollback()
            return {"error": "INSUFFICIENT"}

        playbook_id, config_path, subagent_id = returnconfigandpath(userid)

        if not playbook_id:
            config_s3_path = create_empty_playbook_config(userid)
            playb_id = str(uuid.uuid4())

            playbook_id, config_path = create_subagent_to_playbook(
                playb_id, subagent_id, config_s3_path
            )

        full_output, npath = await create_playbook(
            data=data,
            template_data=PLAY_TEMPLATE,
            minor_data=MINOR_PROMPTS,
            functions_ds=ALL_FUNCTIONS,
            db=db,
            credits=credits,
        )

        update_playbook_config(
            configpath=config_path,
            user_id=userid,
            name=full_output["filename"],
            filepath=npath,
            title=full_output["workflow"]["name"],
            description=full_output["workflow"]["description"],
            num_steps=len(full_output["workflow"]["steps"]),
        )

        db.commit()

        await credits.cm.sync_credits_to_redis(userid)

        return full_output

    except Exception as e:
        db.rollback()
        raise e

    finally:
        db.close()


async def updateInstruction_worker(data, job_id=None, session_id=None):
    import json
    import asyncio

    userid = data.get("user_id")
    filename = data.get("filename")

    if not userid or not filename:
        return {"error": "user_id and filename required"}, 400

    # Ensure JSON extension
    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"

    # Get config + path
    playbook_id, config_path, subagent_id = returnconfigandpath(userid)
    s3_key = f"{userid}/workflow/{base_name(filename)}/{filename}"

    # 🔹 Load existing workflow
    try:
        existing_data = read_json_from_s3(s3_key)
    except Exception:
        existing_data = None

    # =========================================================
    # 🔍 STRICT COMPARISON LOGIC
    # =========================================================
    only_meta_changed = False

    if existing_data:
        old_input = existing_data.get("input_data", {}) or {}

        # 🔹 Normalize NEW input (from frontend)
        normalized_new_input = {
            "title": data.get("title"),
            "description": data.get("description"),
            "trigger_mode": data.get("trigger_mode"),
            "trigger_input": data.get("trigger_input"),
            "ai_mode": data.get("ai_mode"),
            "communication_channels": data.get("communication_channels") or [],
            "contacts": data.get("contacts") or [],
            "steps": data.get("steps") or [],
            "is_active": data.get("is_active"),
        }

        # 🔹 Normalize OLD input (from stored workflow)
        normalized_old_input = {
            "title": old_input.get("title"),
            "description": old_input.get("description"),
            "trigger_mode": old_input.get("trigger_mode"),
            "trigger_input": old_input.get("trigger_input"),
            "ai_mode": old_input.get("ai_mode"),
            "communication_channels": old_input.get("communication_channels") or [],
            "contacts": old_input.get("contacts") or [],
            "steps": old_input.get("steps") or [],
            "is_active": old_input.get("is_active"),
        }

        # 🔹 Extract allowed fields
        new_title = normalized_new_input.pop("title", None)
        new_desc = normalized_new_input.pop("description", None)

        old_title = normalized_old_input.pop("title", None)
        old_desc = normalized_old_input.pop("description", None)

        # 🔹 Compare ALL other fields strictly
        rest_same = normalized_new_input == normalized_old_input

        # 🔹 Final decision
        if rest_same and (new_title != old_title or new_desc != old_desc):
            only_meta_changed = True

    # =========================================================
    # 🔹 DB + Credits init
    # =========================================================
    db = connect_to_rds()
    credits = Credits(db)
    # print("this is ", existing_data and only_meta_changed)

    # =========================================================
    # ✅ CASE 1: ONLY TITLE / DESCRIPTION CHANGED
    # =========================================================
    if existing_data and only_meta_changed:
        try:
            existing_data["input_data"]["title"] = data.get("title")
            existing_data["input_data"]["description"] = data.get("description")

            existing_data["workflow"]["name"] = data.get("title")
            existing_data["workflow"]["description"] = data.get("description")
            ensure_dir(f"{pathconfig.basepath}/test/")
            filepath = os.path.join(f"{pathconfig.basepath}/test/", filename)
            delete_file_from_s3(
                filepath=f"{userid}/workflow/{base_name(filename)}/{filename}"
            )
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(existing_data, f, indent=2)
            res = upload_any_file(
                file_path=filepath, user_id=userid, file_name=filename, type="workflow"
            )
            os.remove(filepath)

            # Update DB config
            update_playbook_config(
                configpath=config_path,
                user_id=userid,
                name=filename,
                filepath=filename,
                title=data.get("title"),
                description=data.get("description"),
                num_steps=len(existing_data["workflow"].get("steps", [])),
            )

            db.commit()
            db.close()

            return existing_data

        except Exception as e:
            db.rollback()
            db.close()
            return {
                "status": "error",
                "message": "Metadata update failed",
                "error": str(e),
            }

    # =========================================================
    # ✅ CASE 2: FULL AI REGENERATION
    # =========================================================
    async def _create_and_update():
        total_input_chars = 5000

        if not await credits.has_ai_credits(
            total_chars=total_input_chars, user_id=userid
        ):
            raise Exception("INSUFFICIENT_CREDITS")

        full_output, npath = await create_playbook(
            data=data,
            template_data=PLAY_TEMPLATE,
            minor_data=MINOR_PROMPTS,
            functions_ds=ALL_FUNCTIONS,
            nfilename=filename,
            db=db,
            credits=credits,
        )

        update_playbook_config(
            configpath=config_path,
            user_id=userid,
            name=full_output["filename"],
            filepath=npath,
            title=full_output["workflow"]["name"],
            description=full_output["workflow"]["description"],
            num_steps=len(full_output["workflow"]["steps"]),
        )

        return full_output

    def run_in_thread():
        return asyncio.run(_create_and_update())

    future = executor.submit(run_in_thread)

    try:
        full_output = future.result(timeout=60)
    except Exception as e:
        db.rollback()
        db.close()

        if "INSUFFICIENT_CREDITS" in str(e):
            return {"status": "error", "message": "Insufficient credits"}

        return {
            "status": "error",
            "message": "Failed to update instruction",
            "error": str(e),
        }

    await credits.cm.sync_credits_to_redis(user_id=userid)
    db.commit()
    db.close()

    return full_output


@playbook_bp.route("/playbook/jbs/<job_id>", methods=["GET"])
async def job_status(job_id):
    from services.redis_service import RedisService

    redisservice = RedisService()

    job = await redisservice.get(f"job:{job_id}")

    if not job:
        return jsonify({"status": "not_found"}), 404

    return jsonify(job)


@playbook_bp.route("/update_instruction", methods=["POST"])
async def updateInstruction():
    data = request.json

    job_id = await JobManager.submit_job(updateInstruction_worker, data)

    return jsonify({"status": "accepted", "job_id": job_id})


@playbook_bp.route("/get_all_instructions", methods=["GET"])
def get_all_instructions():
    user_id = request.args.get("user_id")
    is_admin = False

    if not user_id:
        return jsonify({"error": "credentials required"}), 400

    subagent_id = get_subagent_by_userid(user_id)
    if not subagent_id:
        return jsonify({"error": "invalid credentials"}), 401

    playbook_id, config_path = check_subagent_by_playbook(subagent_id)

    # ✅ FIX: Do NOT fail if config_path is None
    if not config_path:
        return jsonify({"data": [], "is_admin": user_id in ACCESSIBLE_IDS})

    if user_id in ACCESSIBLE_IDS:
        is_admin = True

    try:
        config_data = read_json_from_s3(config_path)

        # ✅ Handle empty config properly
        if not config_data or user_id not in config_data:
            return jsonify({"data": [], "is_admin": is_admin})

        playbook_list = config_data[user_id].get("playbooklist", [])

        # Remove filepath for response
        for playbook in playbook_list:
            playbook.pop("filepath", None)
            if "referece" not in playbook:
                playbook["referece"] = ""

        return jsonify({"data": playbook_list, "is_admin": is_admin})

    except Exception as e:
        return jsonify({"error": f"Failed to fetch instructions: {str(e)}"}), 500


@playbook_bp.route("/get_single_instruction", methods=["GET"])
def get_single_instruction():
    user_id = request.args.get("user_id")
    filename = request.args.get("filename")

    if not user_id or not filename:
        return jsonify({"error": "user_id and filename are required"}), 400
    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"
    s3_key = f"{user_id}/workflow/{base_name(filename)}/{filename}"
    # print("s3 key", s3_key)

    try:
        instruction_data = read_json_from_s3(s3_key)
        if not instruction_data:
            return jsonify({"error": "Instruction not found"}), 404
        # instruction_data.pop("filepath", None)
        return jsonify(instruction_data)
    except Exception as e:
        return jsonify({"error": f"Failed to fetch instruction: {str(e)}"}), 500


@playbook_bp.route("/delete_instruction", methods=["DELETE"])
def delete_instruction():
    user_id = request.args.get("user_id")
    filename = request.args.get("filename")

    if not user_id or not filename:
        return jsonify({"error": "user_id and filename are required"}), 400
    if not user_id:
        return jsonify({"error": "userid is required"}), 400
    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"
    subagent_id = get_subagent_by_userid(user_id)
    if not subagent_id:
        return jsonify({"error": "no agent found"}), 400
    config_path = None
    playbook_id, config_path = check_subagent_by_playbook(subagent_id)
    # config_path = "107642411636394027005/workflow/config_playbook_0195b8dd.json"
    if not config_path:
        return jsonify({"error": "cant find selected instruction"}), 400
    try:
        success = deleteConfigdata(config_path, user_id, filename)
        if not success:
            return jsonify({"error": "Failed to delete instruction"}), 500
        return jsonify({"message": "Instruction deleted successfully"}), 200
    except Exception as e:
        return jsonify({"error": f"Failed to delete instruction: {str(e)}"}), 500


@playbook_bp.route("/add_a_step", methods=["POST"])
def add_a_step():
    body = request.json
    step_data = body.get("stepdata")
    user_id = body.get("user_id")
    filename = body.get("filename")
    previous_step_id = (
        step_data.get("parentStepId") if "parentStepId" in step_data else None
    )

    if not step_data or not user_id:
        return jsonify({"status": "error", "message": "Missing step or user_id"}), 400
    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"
    playbook = read_json_from_s3(f"{user_id}/workflow/{base_name(filename)}/{filename}")
    workflow = playbook.setdefault("workflow", {})
    steps = workflow.setdefault("steps", [])

    # Check for duplicate title
    step_title = step_data.get("title", "").strip().lower()
    for s in steps:
        if s.get("title", "").strip().lower() == step_title:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Step with this title already exists",
                    }
                ),
                409,
            )

    # Assign UUID if not already present
    new_step_id = str(uuid.uuid4())
    step_data["id"] = step_data.get("id", new_step_id)

    # Format step to ensure valid structure
    step_data = format_step_data(step_data)

    # Add to previous step's next_step if applicable
    if previous_step_id:
        for step in steps:
            if str(step.get("id")) == str(previous_step_id):
                if step.get("decision_point", False):
                    step.setdefault("next_step", [])
                    if isinstance(step["next_step"], list):
                        step["next_step"].append(step_data["id"])
                else:
                    step["next_step"] = step_data["id"]
                break
        else:
            return (
                jsonify({"status": "error", "message": "Previous step ID not found"}),
                404,
            )

    # Append new step
    steps.append(step_data)
    playbook["workflow"]["steps"] = steps
    playbook["WorkflowDate"] = datetime.now().isoformat()
    # -----------------------------
    # 🔥 Update config (ONLY num_steps)
    # -----------------------------
    playbook_id, config_path, subagent_id = returnconfigandpath(user_id)

    try:
        config_data = read_json_from_s3(config_path)
    except Exception as e:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Failed to read config",
                    "error": str(e),
                }
            ),
            500,
        )

    if not config_data or user_id not in config_data:
        return jsonify({"status": "error", "message": "User config not found"}), 404

    playbook_list = config_data[user_id].get("playbooklist", [])

    updated_payload = False
    for pb in playbook_list:
        if pb.get("name", "").replace(".json", "") == filename.replace(".json", ""):
            updated_payload = {
                "configpath": config_path,
                "user_id": user_id,
                "name": pb.get("name"),
                "filepath": pb.get("filepath"),
                "title": pb.get("title"),
                "description": pb.get("description"),
                "num_steps": len(steps),  # ✅ updated value
            }
            break
    if not updated_payload:
        return (
            jsonify({"status": "error", "message": "Playbook not found in config"}),
            404,
        )
    update_playbook_config(**updated_payload)

    return save_playbook_to_s3(playbook, user_id, "Step added successfully", filename)


@playbook_bp.route("/edit_a_step", methods=["POST"])
def edit_a_step():
    body = request.json
    step_data = body.get("stepdata")
    user_id = body.get("user_id")
    filename = body.get("filename")

    if not step_data or not user_id or "id" not in step_data:
        return (
            jsonify({"status": "error", "message": "Missing step id or user_id"}),
            400,
        )
    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"
    # print(body)
    step_data = format_step_data(step_data)

    playbook = read_json_from_s3(f"{user_id}/workflow/{base_name(filename)}/{filename}")
    steps = playbook.get("workflow", {}).get("steps", [])

    updated = False
    # for i, step in enumerate(steps):
    #     if step["id"] == step_data["id"]:
    #         steps[i] = step_data
    #         updated = True
    #         break
    for i, step in enumerate(steps):
        if str(step.get("id")) == str(step_data.get("id")):
            steps[i] = step_data
            updated = True
            break

    if not updated:
        return jsonify({"status": "error", "message": "Step ID not found"}), 404

    playbook["workflow"]["steps"] = steps
    playbook["WorkflowDate"] = datetime.now().isoformat()
    return save_playbook_to_s3(playbook, user_id, "Step edited successfully", filename)


@playbook_bp.route("/update_step_arguments", methods=["POST"])
def update_step_arguments():
    try:
        body = request.json or {}

        user_id = body.get("user_id")
        filename = body.get("filename")
        step_id = body.get("step_id")
        new_arguments = body.get("arguments")

        if not user_id or not filename or step_id is None or new_arguments is None:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Missing user_id, filename, step_id, or arguments",
                    }
                ),
                400,
            )

        if not filename.lower().endswith(".json"):
            filename = f"{filename}.json"

        # -----------------------------------------------------------
        # 1) Load playbook
        # -----------------------------------------------------------
        playbook = read_json_from_s3(
            f"{user_id}/workflow/{base_name(filename)}/{filename}"
        )

        steps = playbook.get("workflow", {}).get("steps", [])
        updated = False

        # -----------------------------------------------------------
        # 2) Update step arguments
        # -----------------------------------------------------------
        for step in steps:
            if str(step.get("id")) == str(step_id):

                if (
                    "function_call" not in step
                    or "arguments" not in step["function_call"]
                ):
                    return (
                        jsonify(
                            {
                                "status": "error",
                                "message": f"Step {step_id} does not contain function_call.arguments",
                            }
                        ),
                        400,
                    )

                # Replace ONLY arguments
                step["function_call"]["arguments"] = new_arguments

                # Remove fulfilled requirements
                req_list = step.get("requirements_needed", [])
                step["requirements_needed"] = [
                    r for r in req_list if r not in new_arguments
                ]

                updated = True
                break

        if not updated:
            return jsonify({"status": "error", "message": "Step ID not found"}), 404

        # -----------------------------------------------------------
        # 3) Extract CONTACTS ONLY
        # -----------------------------------------------------------
        def extract_contacts_from_arguments(args):
            CONTACT_KEYS = {
                "email",
                "emails",
                "attendees",
                "receipent_emails",
                "recipient_emails",
            }

            contacts = []

            def normalize_email(e):
                if isinstance(e, str):
                    e = e.strip()
                    if "@" in e and "." in e:
                        return e
                return None

            for k, v in args.items():
                if k not in CONTACT_KEYS:
                    continue

                if isinstance(v, str):
                    em = normalize_email(v)
                    if em:
                        contacts.append(em)

                elif isinstance(v, list):
                    for item in v:
                        em = normalize_email(item)
                        if em:
                            contacts.append(em)

                elif isinstance(v, dict):
                    for val in v.values():
                        em = normalize_email(val)
                        if em:
                            contacts.append(em)

            return contacts

        new_contacts = extract_contacts_from_arguments(new_arguments)

        # -----------------------------------------------------------
        # 4) Normalize + merge contacts (handles "all"/"All")
        # -----------------------------------------------------------
        if new_contacts:
            playbook.setdefault("input_data", {})

            existing_contacts = playbook["input_data"].get("contacts", [])

            # 🔥 Normalize legacy values
            if isinstance(existing_contacts, str):
                if existing_contacts.lower() == "all":
                    existing_contacts = []
                else:
                    existing_contacts = [existing_contacts]
            elif not isinstance(existing_contacts, list):
                existing_contacts = []

            playbook["input_data"]["contacts"] = list(
                dict.fromkeys(existing_contacts + new_contacts)
            )

        # -----------------------------------------------------------
        # 5) Save workflow
        # -----------------------------------------------------------
        playbook["workflow"]["steps"] = steps
        playbook["WorkflowDate"] = datetime.now().isoformat()

        return save_playbook_to_s3(
            playbook,
            user_id,
            "Step arguments updated successfully",
            filename,
        )

    except Exception as e:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": f"Unexpected error: {e}",
                }
            ),
            500,
        )


@playbook_bp.route("/delete_step_argument", methods=["POST"])
def delete_step_argument():
    body = request.json

    user_id = body.get("user_id")
    filename = body.get("filename")
    step_id = body.get("step_id")
    arg_name = body.get("argument_name")

    if not user_id or not filename or step_id is None or not arg_name:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Missing user_id, filename, step_id, or argument_name",
                }
            ),
            400,
        )
    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"
    # Load playbook
    playbook = read_json_from_s3(f"{user_id}/workflow/{base_name(filename)}/{filename}")
    steps = playbook.get("workflow", {}).get("steps", [])

    updated = False

    for step in steps:
        if str(step.get("id")) == str(step_id):

            # Step must contain function_call.arguments
            if "function_call" not in step or "arguments" not in step["function_call"]:
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": f"Step {step_id} has no function_call.arguments",
                        }
                    ),
                    400,
                )

            arguments = step["function_call"]["arguments"]

            # If argument not present → nothing to delete
            if arg_name not in arguments:
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": f"Argument '{arg_name}' not found in step {step_id}",
                        }
                    ),
                    404,
                )

            # DELETE the argument
            del arguments[arg_name]

            # Restore requirement
            req_list = step.get("requirements_needed", [])
            if arg_name not in req_list:
                req_list.append(arg_name)
                step["requirements_needed"] = req_list

            updated = True
            break

    if not updated:
        return jsonify({"status": "error", "message": "Step ID not found"}), 404

    # Update date
    playbook["WorkflowDate"] = datetime.now().isoformat()

    # SAVE
    return save_playbook_to_s3(
        playbook, user_id, "Argument deleted and requirement restored", filename
    )


@playbook_bp.route("/delete_a_step", methods=["POST"])
def delete_a_step():
    body = request.json

    step_id = body.get("step_id")
    user_id = body.get("user_id")
    filename = body.get("filename")

    # Validate inputs
    if not step_id or not user_id or not filename:
        return (
            jsonify(
                {"status": "error", "message": "Missing step_id, user_id, or filename"}
            ),
            400,
        )

    # Normalize step_id to string for safe comparisons
    step_id = str(step_id)

    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"

    try:
        playbook = read_json_from_s3(
            f"{user_id}/workflow/{base_name(filename)}/{filename}"
        )
    except Exception as e:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Failed to read playbook",
                    "error": str(e),
                }
            ),
            500,
        )

    workflow = playbook.get("workflow", {})
    steps = workflow.get("steps", [])

    if not steps:
        return (
            jsonify({"status": "error", "message": "No steps found in workflow"}),
            404,
        )

    # Check if step exists
    step_found = any(str(s.get("id")) == step_id for s in steps)

    if not step_found:
        return jsonify({"status": "error", "message": "Step ID not found"}), 404

    # -----------------------------
    # Remove the step
    # -----------------------------
    new_steps = [s for s in steps if str(s.get("id")) != step_id]

    # -----------------------------
    # Clean references to deleted step
    # -----------------------------
    for step in new_steps:

        if "next_step" not in step:
            continue

        next_step = step["next_step"]

        # Case 1: next_step is a list
        if isinstance(next_step, list):

            filtered = [nid for nid in next_step if str(nid) != step_id]

            if filtered:
                step["next_step"] = filtered
            else:
                del step["next_step"]

        # Case 2: next_step is single value
        else:
            if str(next_step) == step_id:
                del step["next_step"]

    # -----------------------------
    # Update workflow
    # -----------------------------
    playbook["workflow"]["steps"] = new_steps
    playbook["WorkflowDate"] = datetime.now().isoformat()

    # -----------------------------
    # 🔥 Update config (ONLY num_steps)
    # -----------------------------
    playbook_id, config_path, subagent_id = returnconfigandpath(user_id)

    try:
        config_data = read_json_from_s3(config_path)
    except Exception as e:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Failed to read config",
                    "error": str(e),
                }
            ),
            500,
        )

    if not config_data or user_id not in config_data:
        return jsonify({"status": "error", "message": "User config not found"}), 404

    playbook_list = config_data[user_id].get("playbooklist", [])

    updated_payload = False
    for pb in playbook_list:
        if pb.get("name", "").replace(".json", "") == filename.replace(".json", ""):
            updated_payload = {
                "configpath": config_path,
                "user_id": user_id,
                "name": pb.get("name"),
                "filepath": pb.get("filepath"),
                "title": pb.get("title"),
                "description": pb.get("description"),
                "num_steps": len(new_steps),  # ✅ updated value
            }
            break
    if not updated_payload:
        return (
            jsonify({"status": "error", "message": "Playbook not found in config"}),
            404,
        )
    update_playbook_config(**updated_payload)

    # -----------------------------
    # Save back to S3
    # -----------------------------
    return save_playbook_to_s3(playbook, user_id, "Step deleted successfully", filename)


async def modify_instruction(ud_inst=None, user_id=None, filename=None, add_data=None):
    db = connect_to_rds()
    credits = Credits(db)

    try:
        # -----------------------------
        # 1. INPUT HANDLING
        # -----------------------------
        if all(arg is None for arg in [ud_inst, user_id, filename]):
            body = request.json
            if not body:
                return (
                    jsonify({"status": "error", "message": "Empty request body"}),
                    400,
                )

            update_instruction = body.get("modify_instructions")
            additional_data = body.get("additional_data") or ""
            user_id = body.get("user_id")
            filename = body.get("filename")
        else:
            update_instruction = ud_inst
            additional_data = add_data or ""
            user_id = user_id
            filename = filename

        if not update_instruction or not user_id or not filename:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Missing required fields: modify_instructions, user_id, filename",
                    }
                ),
                400,
            )

        # -----------------------------
        # 1a. Credit preflight
        # -----------------------------
        total_input_chars = len(update_instruction)
        if not await credits.has_ai_credits(
            total_chars=total_input_chars, user_id=user_id
        ):
            return jsonify({"status": "error", "message": "Insufficient credits"}), 402

        # -----------------------------
        # 2. Validate instruction
        # -----------------------------
        if is_inappropriate(update_instruction):
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "The provided instruction is invalid or inappropriate.",
                    }
                ),
                400,
            )

        if not filename.lower().endswith(".json"):
            filename = f"{filename}.json"

        # -----------------------------
        # 3. LOAD PROMPTS
        # -----------------------------
        yaml_data = PLAY_TEMPLATE
        modify_prompt = yaml_data.get("modify_instruction")
        eval_prompt = yaml_data.get("evaluate_modified_workflow_execution_validity")

        if not modify_prompt or not eval_prompt:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Required modify/evaluator prompt missing in YAML.",
                    }
                ),
                500,
            )

        # -----------------------------
        # 4. LOAD EXISTING WORKFLOW
        # -----------------------------
        original_json = read_json_from_s3(
            f"{user_id}/workflow/{base_name(filename)}/{filename}"
        )
        if not original_json or "workflow" not in original_json:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Workflow JSON missing or malformed.",
                    }
                ),
                500,
            )

        workflow_json_str = json.dumps(original_json["workflow"], indent=2)
        contacts_pr = original_json["input_data"].get("contacts") or "All"
        services_functions = read_function_jsons()

        # -----------------------------
        # 5. BUILD MODIFY PROMPT
        # -----------------------------
        full_prompt = (
            modify_prompt.replace("{existing_workflow}", workflow_json_str)
            .replace("{update_instruction}", update_instruction)
            .replace("{services_section}", services_functions)
            .replace("{additional_data}", additional_data)
            .replace("{existing_contacts}", contacts_pr)
            .replace("{todays_date}", datetime.now().strftime("%Y-%m-%d"))
        )

        # -----------------------------
        # 6. MEETING INTENT DETECTION
        # -----------------------------
        def detect_meeting_intent(text: str) -> bool:
            return any(
                k in text.lower()
                for k in [
                    "meeting",
                    "schedule",
                    "reschedule",
                    "cancel",
                    "call",
                    "appointment",
                    "interview",
                ]
            )

        def detect_platform(text: str):
            text = text.lower()
            return {
                "google": any(k in text for k in ["google", "google meet", "meet"]),
                "microsoft": any(k in text for k in ["microsoft", "teams", "ms teams"]),
            }

        has_meeting_intent = detect_meeting_intent(update_instruction)
        checker_dict = detect_platform(update_instruction)

        if has_meeting_intent and not any(checker_dict.values()):
            actual_social = fetch_user_Social(user_id=user_id)
            if actual_social in checker_dict:
                checker_dict[actual_social] = True

        # -----------------------------
        # 7. INJECT MEETING RULES
        # -----------------------------
        if has_meeting_intent:
            meeting_rules = []

            if checker_dict.get("google"):
                rule = MINOR_PROMPTS.get("google_meet_rules")
                if rule:
                    meeting_rules.append(rule.strip())

            if checker_dict.get("microsoft"):
                rule = MINOR_PROMPTS.get("microsoft_meet_rules")
                if rule:
                    meeting_rules.append(rule.strip())

            if meeting_rules:
                full_prompt = replace_section(
                    prompt=full_prompt,
                    section_title="MEETING FUNCTION LINKING RULES",
                    replacement="\n\n".join(meeting_rules),
                )

        # -----------------------------
        # 8. CALL MODIFY LLM (WITH CREDITS)
        # -----------------------------
        db.begin()  # 🔐 start transaction

        llm_response = await get_fireworks_response2(
            user_message=full_prompt,
            role="system",
            temp=0.5,
            user_id=user_id,
            credits=credits,
        )

        if llm_response == "INSUFFICIENT":
            db.rollback()
            return jsonify({"status": "error", "message": "Insufficient credits"}), 402

        cleaned_response = extract_json_from_llm_output(llm_response)
        modified_json = json.loads(cleaned_response)

        if "unrelated_instruction_message" in modified_json:
            db.rollback()
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": modified_json["unrelated_instruction_message"],
                    }
                ),
                400,
            )

        if "steps" not in modified_json:
            db.rollback()
            return (
                jsonify(
                    {"status": "error", "message": "Modified workflow missing steps."}
                ),
                500,
            )

        final_workflow = modified_json

        # -----------------------------
        # 9. SYNC BACK TO ORIGINAL JSON
        # -----------------------------
        for key in ["name", "description", "ai_mode", "trigger_mode", "trigger_input"]:
            if key in final_workflow:
                original_json["workflow"][key] = final_workflow[key]

        if "context_section" in final_workflow:
            original_json["context_section"] = final_workflow["context_section"]

        original_json["workflow"]["steps"] = final_workflow["steps"]
        original_json["WorkflowDate"] = datetime.now().isoformat()

        # Update playbook config
        _, config_path, _ = returnconfigandpath(user_id)
        update_playbook_config(
            configpath=config_path,
            user_id=user_id,
            name=original_json["filename"],
            filepath=f"{user_id}/workflow/{filename}",
            title=original_json["workflow"]["name"],
            description=original_json["workflow"]["description"],
            num_steps=len(original_json["workflow"]["steps"]),
        )

        db.commit()  # ✅ commit transaction
        await credits.cm.sync_credits_to_redis(user_id)  # ✅ sync Redis after commit

        return save_playbook_to_s3(
            original_json, user_id, "Workflow updated successfully.", filename
        )

    except Exception as e:
        db.rollback()
        return (
            jsonify({"status": "error", "message": f"Internal server error: {e}"}),
            500,
        )

    finally:
        db.close()


async def modlmiddle(body):
    update_instruction = body.get("modify_instructions")
    additional_data = body.get("additional_data") or ""
    user_id = body.get("user_id")
    filename = body.get("filename")
    res = await modify_instruction(
        ud_inst=update_instruction,
        user_id=user_id,
        filename=filename,
        add_data=additional_data,
    )
    return res


@playbook_bp.route("/modify_instruction", methods=["POST"])
async def mod_instuct():
    data = request.json

    job_id = await JobManager.submit_job(modlmiddle, data)

    return jsonify({"status": "accepted", "job_id": job_id})


@playbook_bp.route("/run_workflow", methods=["POST"])
async def runWorkflow():
    data = request.json
    userid = data.get("user_id")
    filename = data.get("filename")
    testing = data.get("testing")

    if not userid or not filename:
        return jsonify({"status": "error", "message": "Invalid input"}), 400

    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"

    wf_loc = f"{userid}/workflow/{base_name(filename=filename)}/{filename}"
    workflow_json = read_json_from_s3(wf_loc)

    if not workflow_json:
        return jsonify({"status": "error", "message": "Workflow not found"}), 404

    # ==========================================
    # 🔍 PRE-VALIDATION CHECK
    # ==========================================
    workflow_steps = workflow_json.get("workflow", {}).get("steps", [])

    requires_questions = False

    for step in workflow_steps:
        function_call = step.get("function_call", {})
        function_name = function_call.get("function_name")

        if function_name == "automate.assign_or_show_questions_from_file":
            requires_questions = True
            break

    if requires_questions:
        assigned = workflow_json.get("assigned_questions")

        if not assigned:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "For this workflow to run, no assigned questions were found.\n\nPlease upload or assign a questionnaire file.",
                    }
                ),
                400,
            )

    # ==========================================
    # 🚀 EXECUTION
    # ==========================================
    db = connect_to_rds()
    credits = Credits(db=db)

    try:
        with WorkflowRunnerV2(
            userid=userid,
            filename=filename,
            workflowJson=workflow_json,
            testing=testing,
            db=db,
            credits=credits,
        ) as runner:
            await runner.execute()
            return jsonify(
                {"status": "success", "execution_log": runner.get_execution_log()}
            )

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@playbook_bp.route("/run_workflow_step", methods=["POST"])
def run_workflow_step():
    data = request.json
    userid = data.get("user_id")
    filename = data.get("filename")
    step_id = data.get("step_id")

    if not userid or not filename or not step_id:
        return jsonify({"status": "error", "message": "Invalid input"}), 400

    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"

    wf_loc = f"{userid}/workflow/{base_name(filename=filename)}/{filename}"
    workflow_json = read_json_from_s3(wf_loc)

    if not workflow_json:
        return jsonify({"status": "error", "message": "Workflow not found"}), 404

    db = connect_to_rds()
    credits = Credits(db=db)

    try:
        with WorkflowRunnerV2(
            userid=userid,
            filename=filename,
            workflowJson=workflow_json,
            db=db,
            credits=credits,
        ) as runner:

            step = runner.steps.get(step_id)
            if not step:
                return jsonify({"status": "error", "message": "Step not found"}), 404

            result = runner._execute_step(step)

            return jsonify(
                {
                    "status": "success",
                    "workflow_step_result": result,
                    "execution_log": runner.get_execution_log(),
                }
            )

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@playbook_bp.route("/test-playground-step", methods=["GET", "POST"])
def testworkflowbyinput_stream():
    data = request.json if request.method == "POST" else request.args

    userid = data.get("user_id")
    filename = data.get("filename")
    userinput = data.get("userinput")
    testing = data.get("is_testing") or True

    if not userid or not filename or not userinput:
        return jsonify({"status": "error", "message": "Invalid input"}), 400

    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"

    wf_loc = f"{userid}/workflow/{base_name(filename=filename)}/{filename}"
    workflow_json = read_json_from_s3(wf_loc)

    if not workflow_json:
        return jsonify({"status": "error", "message": "Workflow not found"}), 404

    def event_stream():
        db = connect_to_rds()
        credits = Credits(db=db)

        try:
            with WorkflowRunnerV2(
                userid=userid,
                filename=filename,
                workflowJson=workflow_json,
                testing=testing,
                db=db,
                credits=credits,
            ) as runner:
                result = asyncio.run(runner.check_input_tone(user_input=userinput))

            yield f"event: done\ndata: {json.dumps(result)}\n\n"

        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"

    return Response(stream_with_context(event_stream()), mimetype="text/event-stream")


@playbook_bp.route("/clear-playground-data", methods=["POST"])
def clear_playground_data():
    """
    Clears transient data (chat, online, testing) from a user's workflow file.
    Keeps workflow logic and metadata intact.
    """
    data = request.json
    userid = data.get("user_id")
    filename = data.get("filename")

    if not userid:
        return jsonify({"message": "Not a valid userid", "status": "error"}), 400
    if not filename:
        return jsonify({"message": "Not a valid filename", "status": "error"}), 400
    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"

    try:
        # 🔹 Load workflow JSON from S3
        workflow_json = read_json_from_s3(
            f"{userid}/workflow/{base_name(filename=filename)}/{filename}"
        )

        # 🔹 Remove transient sections
        for key in [
            "chat",
            "online",
            "testing",
            "chat_log",
            "execution_logs",
            "last_ai_discovered",
            "pre_user_data",
            "evidences_ques",
        ]:
            if key in workflow_json:
                del workflow_json[key]

        # 🔹 Save cleaned JSON back to S3
        # tmp_path = f"/tmp/{filename}"
        # with open(tmp_path, "w") as f:
        #     json.dump(workflow_json, f, indent=4)

        # upload_any_file(tmp_path, userid, filename)
        save_playbook_to_s3(workflow_json, userid, "Step edited successfully", filename)

        return (
            jsonify(
                {
                    "message": "Playground data (chat, online, testing) cleared successfully.",
                    "status": "success",
                    "filename": filename,
                }
            ),
            200,
        )

    except Exception as e:
        # print("Error clearing playground data:", e)
        return (
            jsonify({"message": f"Error clearing data: {str(e)}", "status": "error"}),
            500,
        )


@playbook_bp.route("/clear-testing-data", methods=["POST"])
def clear_testing_data():
    """
    Clears only the 'testing' section from a user's workflow JSON file.
    Keeps chat, online, and workflow structure intact.
    """
    data = request.json
    userid = data.get("user_id")
    filename = data.get("filename")

    if not userid:
        return jsonify({"message": "Not a valid userid", "status": "error"}), 400
    if not filename:
        return jsonify({"message": "Not a valid filename", "status": "error"}), 400
    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"
    try:
        # 🔹 Load workflow JSON from S3
        workflow_json = read_json_from_s3(
            f"{userid}/workflow/{base_name(filename=filename)}/{filename}"
        )

        # 🔹 Remove only testing section
        if "testing" in workflow_json:
            del workflow_json["testing"]
        else:
            return (
                jsonify(
                    {
                        "message": "No testing data found to clear.",
                        "status": "success",
                        "filename": filename,
                    }
                ),
                200,
            )

        # # 🔹 Save updated workflow JSON back to S3
        # tmp_path = f"/tmp/{filename}"
        # with open(tmp_path, "w") as f:
        #     json.dump(workflow_json, f, indent=4)

        # upload_any_file(tmp_path, userid, filename)
        save_playbook_to_s3(workflow_json, userid, "Step edited successfully", filename)

        return (
            jsonify(
                {
                    "message": "Testing data cleared successfully.",
                    "status": "success",
                    "filename": filename,
                }
            ),
            200,
        )

    except Exception as e:
        # print("Error clearing testing data:", e)
        return (
            jsonify(
                {"message": f"Error clearing testing data: {str(e)}", "status": "error"}
            ),
            500,
        )


@playbook_bp.route("/generate-workflow-input", methods=["POST"])
async def generate_workflow_input():
    db = connect_to_rds()
    credits = Credits(db)

    try:
        # -----------------------------
        # 1️⃣ INPUT HANDLING
        # -----------------------------
        data = request.get_json(force=True)
        userid = data.get("user_id")
        inp_description = data.get("description", "").strip()

        if not userid:
            return jsonify({"error": "Missing user_id"}), 400
        if not inp_description:
            return jsonify({"error": "Missing 'description' field"}), 400

        # -----------------------------
        # 2️⃣ CREDIT CHECK
        # -----------------------------
        total_input_chars = len(inp_description)
        if not await credits.has_ai_credits(
            total_chars=total_input_chars, user_id=userid
        ):
            return jsonify({"error": "Insufficient credits"}), 402

        # -----------------------------
        # 3️⃣ USER ACCOUNT TYPE & SERVICES
        # -----------------------------
        main_user_account_type = fetch_user_Social(user_id=userid, connection=db)
        # print("main user logged in:", main_user_account_type)

        available_modes = [
            "auto",
            "gmail",
            "google_meet",
            "microsoft_calendar",
            "outlook",
            "calendar",
        ]

        services_section = read_function_jsons2()

        prompt_yaml = PLAY_TEMPLATE
        prompt_template = prompt_yaml.get("create_workflow_context")
        prompt_text = yaml.dump(prompt_template, sort_keys=False)

        formatted_prompt = (
            prompt_text.replace("{{inp_description}}", inp_description)
            .replace("{{main_user_account_type}}", main_user_account_type)
            .replace("{{available_communication_modes}}", json.dumps(available_modes))
            .replace("{{services_section}}", json.dumps(services_section))
        )

        # -----------------------------
        # 4️⃣ CALL LLM WITH CREDITS
        # -----------------------------
        llm_output = await get_fireworks_response2(
            user_message=formatted_prompt,
            role="system",
            temp=0.3,
            user_id=userid,
            credits=credits,  # ✅ Pass credits for deduction
        )

        if llm_output == "INSUFFICIENT":
            return jsonify({"error": "Insufficient credits"}), 402

        llm_output = re.sub(
            r"^```(?:json)?\s*|\s*```$", "", llm_output, flags=re.MULTILINE
        ).strip()

        # -----------------------------
        # 5️⃣ PARSE LLM OUTPUT
        # -----------------------------
        try:
            workflow_data = json.loads(llm_output)
        except json.JSONDecodeError:
            return (
                jsonify(
                    {
                        "error": "Invalid JSON returned from LLM",
                        "raw_output": llm_output,
                    }
                ),
                500,
            )

        need_contacts = workflow_data.get("need_contacts") or {}
        resolved_report = {"found": {}, "not_found": [], "new": []}
        final_contacts = set()

        # -----------------------------
        # 6️⃣ RESOLVE CONTACTS FROM DB
        # -----------------------------
        with db.cursor() as cursor:
            # 6a. Names
            for name in need_contacts.get("names", []):
                key = name.strip()
                if not key:
                    continue
                query = """
                    SELECT DISTINCT uc.email_id
                    FROM users_clients uc
                    JOIN communication c
                    ON uc.communication_id_fk = c.communication_id
                    WHERE c.user_id_fk = %s
                      AND (LOWER(uc.first_name) LIKE %s
                           OR LOWER(uc.last_name) LIKE %s
                           OR LOWER(uc.email_id) LIKE %s)
                """
                like = f"%{key.lower()}%"
                cursor.execute(query, (userid, like, like, like))
                rows = [r[0] for r in cursor.fetchall()]
                if rows:
                    resolved_report["found"][key] = rows
                    final_contacts.update(rows)
                else:
                    resolved_report["not_found"].append(key)

            # 6b. Emails
            for email in need_contacts.get("emails", []):
                key = email.strip().lower()
                if not key:
                    continue
                query = """
                    SELECT uc.email_id
                    FROM users_clients uc
                    JOIN communication c
                    ON uc.communication_id_fk = c.communication_id
                    WHERE c.user_id_fk = %s
                      AND LOWER(uc.email_id) = %s
                """
                cursor.execute(query, (userid, key))
                row = cursor.fetchone()
                if row:
                    resolved_report["found"][email] = [row[0]]
                    final_contacts.add(row[0])
                else:
                    resolved_report["new"].append(email)

        # -----------------------------
        # 7️⃣ FINALIZE WORKFLOW
        # -----------------------------
        workflow_data["contacts"] = list(final_contacts)
        workflow_data["need_contacts"] = resolved_report
        not_found = resolved_report.get("not_found", [])

        wfname = workflow_data.get("name", "")
        wfdescription = workflow_data.get("description", "")

        workflow_data["name"] = remove_not_found_entities(wfname, not_found)
        workflow_data["description"] = remove_not_found_entities(
            wfdescription, not_found
        )
        workflow_data["need_caution"] = bool(
            resolved_report["not_found"] or resolved_report["new"]
        )
        db.commit()
        return jsonify(workflow_data)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        db.close()


@playbook_bp.route("/test-mid", methods=["POST"])
async def testmidcheck():
    from services.automate_service import AutoMateService

    body = request.get_json(force=True)

    # Lock user to avoid multiple parallel bulk sends
    user_id = body.get("user_id")

    # user_input = body.get("userinput")
    # length = "5 questions"
    # tone = "professional"
    # questions = body.get("questions")
    # keymap = body.get("keymap", None)
    filedata = body.get("ques_file")
    credits = Credits()
    try:
        ai = AutoMateService(userid=user_id, credits=credits)
        val = await ai.generate_questions_from_file(file_data=filedata)
        return jsonify({"data": val})
    except Exception as e:
        # print("❌ Error in /test-email_checks:", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@playbook_bp.route("/test-email-checks", methods=["GET"])
def test_email_checks():
    """
    Test route to trigger bulk email sending using Celery.
    """
    # emails = request.args.get("emails", type=int)
    try:
        from db.lance_db_service import LanceDBServer

        ser = LanceDBServer()
        val = ser.check_lance_db_Connection()
        return jsonify(
            {
                "status": val,
                # # "task_id": tasks,
                # "message": f"Bulk email task queued for {emails} emails",
            }
        )

    except Exception as e:
        # print("❌ Error in /test-email_checks:", e)
        return jsonify({"status": "error", "message": str(e)}), 500


def resolve_schedule_from_activation(scheduled):
    if not scheduled:
        raise ValueError("scheduledActivation missing")

    frequency = scheduled.get("frequency")
    if not frequency:
        raise ValueError("frequency missing")

    frequency = frequency.lower()
    timezone = scheduled.get("timezone", "UTC")

    # -------------------------
    # DAILY
    # -------------------------
    if frequency == "daily":
        return "daily", {
            "startTime": scheduled["startTime"],
            "endTime": scheduled.get("endTime"),
            "timezone": timezone,
        }

    # -------------------------
    # WEEKLY
    # -------------------------
    if frequency == "weekly":
        return "weekly", {
            "weekday": scheduled["weeklyDay"],
            "startTime": scheduled["startTime"],
            "endTime": scheduled.get("endTime"),
            "timezone": timezone,
        }

    # -------------------------
    # ONE-TIME / ONCE
    # -------------------------
    if frequency in ("one_time", "once"):
        start_date = scheduled["startDate"]
        start_time = scheduled["startTime"]
        timezone = scheduled.get("timezone", "UTC")

        return "one_time", {
            "datetime": f"{start_date}T{start_time}",
            "timezone": timezone,
        }
    # -------------------------
    # CUSTOM (NEW)
    # -------------------------
    if frequency == "custom":
        return "custom", {
            "startDate": scheduled["startDate"],
            "endDate": scheduled["endDate"],
            "startTime": scheduled["startTime"],
            "endTime": scheduled["endTime"],
            "timezone": timezone,
        }

    # -------------------------
    # UNSUPPORTED
    # -------------------------
    raise ValueError(f"Unsupported frequency: {frequency}")


@playbook_bp.route("/schedule-workflow-checker", methods=["POST"])
async def schedule_workflow_checker():
    try:
        body = request.json or {}
        userid = body.get("user_id")
        filename = body.get("filename")
        deployment = body.get("deployment", {})
        contacts = deployment.get("selectedContacts", [])
        scheduled = deployment.get("scheduledActivation", {})

        if not userid or not filename:
            return jsonify({"error": "Missing user_id or filename"}), 400

        if not filename.lower().endswith(".json"):
            filename = f"{filename}.json"

        # -----------------------------
        # Load workflow
        # -----------------------------
        wf_loc = f"{userid}/workflow/{base_name(filename)}/{filename}"
        workflow_json = read_json_from_s3(wf_loc)
        if not workflow_json:
            return jsonify({"status": "error", "message": "Workflow not found"}), 404

        # -----------------------------
        # Resolve earliest schedule time
        # -----------------------------
        try:
            schedule_type, data = resolve_schedule_from_activation(scheduled)

            if schedule_type == "daily":
                hour, minute = map(int, data["startTime"].split(":"))
                scheduled_dt = SchedulerService.preview_next_daily_time(
                    hour, minute, data["timezone"]
                )

            elif schedule_type == "weekly":
                hour, minute = map(int, data["startTime"].split(":"))
                scheduled_dt = SchedulerService.preview_next_weekly_time(
                    data["weekday"], hour, minute, data["timezone"]
                )

            elif schedule_type in ("one_time", "once"):
                tz = pytz.timezone(data["timezone"])
                naive_dt = datetime.fromisoformat(data["datetime"])
                scheduled_dt = tz.localize(naive_dt).astimezone(pytz.UTC)

            elif schedule_type == "custom":
                scheduled_dt = SchedulerService.preview_next_custom_time(
                    start_date=data["startDate"],
                    end_date=data["endDate"],
                    start_time=data["startTime"],
                    end_time=data["endTime"],
                    timezone=data["timezone"],
                )
                if not scheduled_dt:
                    return (
                        jsonify(
                            {
                                "status": "error",
                                "message": "No valid execution time in custom window",
                            }
                        ),
                        400,
                    )

            else:
                return jsonify({"error": "Unsupported schedule type"}), 400

            schedule_time_json = json.dumps(scheduled_dt.isoformat())

        except Exception as e:
            return jsonify({"error": f"Schedule resolution failed: {str(e)}"}), 400

        # -----------------------------
        # Build LLM prompt
        # -----------------------------
        prompt_template = PLAY_TEMPLATE.get("validate_schedule_workflow_problems")
        if not prompt_template:
            return jsonify({"error": "Missing LLM prompt template"}), 500

        base_workflow = {
            "workflow": workflow_json.get("workflow"),
            "input_data": workflow_json.get("input_data"),
        }

        full_prompt = (
            prompt_template.replace("{workflow_json}", json.dumps(base_workflow))
            .replace("{contacts}", json.dumps(contacts))
            .replace("{schedule_time}", schedule_time_json)
            .replace("{original_scheduled}", json.dumps(scheduled))
        )

        # -----------------------------
        # Call LLM WITH CREDITS
        # -----------------------------
        db = connect_to_rds()
        credits = Credits(db=db)
        total_chars = len(full_prompt)
        if not await credits.has_ai_credits(total_chars=total_chars, user_id=userid):
            return jsonify({"error": "Insufficient credits"}), 402

        llm_output = await get_evaluator_fireworks(
            user_message=full_prompt,
            role="system",
            temp=0.3,
            user_id=userid,
            credits=credits,  # Pass Credits for consumption
        )

        llm_output = re.sub(r"^```(?:json)?|```$", "", llm_output).strip()

        # -----------------------------
        # Parse JSON safely
        # -----------------------------
        try:
            response = json.loads(llm_output)
            if not isinstance(response, list):
                raise ValueError("Response must be a list")
        except Exception as e:
            return (
                jsonify(
                    {
                        "error": "Invalid LLM JSON",
                        "raw": llm_output,
                        "parse_error": str(e),
                    }
                ),
                500,
            )

        return jsonify(response)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        if "db" in locals() and db:
            db.close()


@playbook_bp.route("/schedule-workflow", methods=["POST"])
async def schedule_workflow():
    body = request.json or {}

    userid = body["user_id"]
    filename = body["filename"]

    deployment = body.get("deployment", {})
    contacts = deployment.get("selectedContacts", [])
    scheduled = deployment.get("scheduledActivation", {})

    timezone = scheduled.get("timezone", "UTC")

    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"

    wf_loc = f"{userid}/workflow/{base_name(filename=filename)}/{filename}"
    workflow_json = read_json_from_s3(wf_loc)

    if not workflow_json:
        return jsonify({"status": "error", "message": "Workflow not found"}), 404

    # --------------------------------------------------
    # Resolve schedule
    # --------------------------------------------------
    schedule_type, data = resolve_schedule_from_activation(scheduled)

    activation_schedule = {
        "type": schedule_type,
        "timezone": timezone,
        "data": data,
        "celery_task_id": None,
        "execution_unique_key": None,  # 👈 NEW
    }

    # --------------------------------------------------
    # DAILY
    # --------------------------------------------------
    if schedule_type == "daily":
        hour, minute = map(int, data["startTime"].split(":"))
        result = await SchedulerService.schedule_daily(
            hour, minute, userid, filename, timezone, contacts
        )
        activation_schedule["celery_task_id"] = result["entry_name"]
        activation_schedule["execution_unique_key"] = result["uniquekey"]

    # --------------------------------------------------
    # WEEKLY
    # --------------------------------------------------
    elif schedule_type == "weekly":
        hour, minute = map(int, data["startTime"].split(":"))
        result = await SchedulerService.schedule_weekly(
            data["weekday"], hour, minute, userid, filename, timezone, contacts
        )
        activation_schedule["celery_task_id"] = result["entry_name"]
        activation_schedule["execution_unique_key"] = result["uniquekey"]

    # --------------------------------------------------
    # ONE-TIME
    # --------------------------------------------------
    elif schedule_type == "one_time":
        dt = datetime.fromisoformat(data["datetime"])
        result = await SchedulerService.schedule_one_time(
            dt, userid, filename, timezone, contacts
        )
        activation_schedule["celery_task_id"] = result["task_id"]
        activation_schedule["execution_unique_key"] = result["uniquekey"]

    # --------------------------------------------------
    # CUSTOM (NEW)
    # --------------------------------------------------
    elif schedule_type == "custom":
        result = await SchedulerService.schedule_custom(
            start_date=data["startDate"],
            start_time=data["startTime"],
            userid=userid,
            filename=filename,
            timezone=data["timezone"],
            contacts=contacts,
        )
        activation_schedule["celery_task_id"] = result["task_id"]
        activation_schedule["execution_unique_key"] = result["uniquekey"]

    else:
        return jsonify({"status": "error", "message": "Unsupported schedule type"}), 400

    # --------------------------------------------------
    # Persist schedule + runtime
    # --------------------------------------------------
    update_playbook_schedule_and_runtime(
        userid=userid,
        filename=filename,
        schedule=activation_schedule,
        runtime={
            "is_running": False,
            "current_execution_id": None,
            "last_execution_id": None,
            "last_run_at": None,
            "last_execution_status": None,
        },
        status="scheduled",
    )

    return jsonify(
        {
            "status": "success",
            "schedule": activation_schedule,
            "scheduler_result": result,
        }
    )


@playbook_bp.route("/get-allfunctions")
def get_all_fns():
    return jsonify(read_function_jsons2())


@playbook_bp.route("/update-questions", methods=["POST"])
def updatequestionsworkflow():
    data = request.json
    # print("dadss", data)
    userid = data.get("user_id")
    answer = data.get("answer")
    comment = data.get("comment")
    filename = data.get("filename")
    chat_id = data.get("chat_id")
    question_id = data.get("question_id")

    if not userid:
        return jsonify({"message": "Not a valid userid", "status": "error"}), 400
    if not filename:
        return jsonify({"message": "Not a valid filename", "status": "error"}), 400
    if not question_id:
        return jsonify({"message": "Invalid question_id", "status": "error"}), 400
    if not chat_id:
        return jsonify({"message": "Invalid chat_id", "status": "error"}), 400
    if answer is None:
        return jsonify({"message": "Answer cannot be null", "status": "error"}), 400

    # token = current_user_id.set(userid)
    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"

    wf_loc = f"{userid}/workflow/{base_name(filename=filename)}/{filename}"
    workflow_json = read_json_from_s3(wf_loc)

    if not workflow_json:
        # current_user_id.reset(token)
        return (
            jsonify({"message": f"Workflow '{filename}' not found", "status": "error"}),
            404,
        )

    with WorkflowRunnerV2(
        userid=userid,
        filename=filename,
        workflowJson=workflow_json,
        testing=True,
    ) as service:
        result = asyncio.run(
            service.answer_questions(
                answer=answer, comment=comment, qid=question_id, chid=chat_id
            )
        )

    # current_user_id.reset(token)

    status_code = 200 if result.get("status") == "success" else 400
    return jsonify(result), status_code


@playbook_bp.route("/update-questions-bulk", methods=["POST"])
def updatequestionsbulkworkflow():
    data = request.json or {}
    userid = data.get("user_id")
    filename = data.get("filename")
    chat_id = data.get("chat_id")
    answers = data.get("answers")  # 🔥 BULK answers

    if not userid:
        return jsonify({"message": "Not a valid userid", "status": "error"}), 400

    if not filename:
        return jsonify({"message": "Not a valid filename", "status": "error"}), 400

    if not chat_id:
        return jsonify({"message": "Invalid chat_id", "status": "error"}), 400

    if not isinstance(answers, list) or not answers:
        return (
            jsonify({"message": "Answers must be a non-empty list", "status": "error"}),
            400,
        )

    for item in answers:
        if not item.get("question_id"):
            return (
                jsonify(
                    {
                        "message": "Each answer must include question_id",
                        "status": "error",
                    }
                ),
                400,
            )
        if item.get("answer") is None:
            return jsonify({"message": "Answer cannot be null", "status": "error"}), 400

    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"

    wf_loc = f"{userid}/workflow/{base_name(filename=filename)}/{filename}"
    workflow_json = read_json_from_s3(wf_loc)

    if not workflow_json:
        return (
            jsonify({"message": f"Workflow '{filename}' not found", "status": "error"}),
            404,
        )

    with WorkflowRunnerV2(
        userid=userid,
        filename=filename,
        workflowJson=workflow_json,
        testing=True,
    ) as service:
        result = asyncio.run(
            service.answer_questions_bulk(answers=answers, chid=chat_id)
        )

    # current_user_id.reset(token)

    status_code = 200 if result.get("status") == "success" else 400
    return jsonify(result), status_code


@playbook_bp.route("/update-form-field", methods=["POST"])
def updateformfieldworkflow():
    data = request.json

    userid = data.get("user_id")
    answer = data.get("answer")
    filename = data.get("filename")
    chat_id = data.get("chat_id")
    field_id = data.get("field_id")

    if not userid:
        return jsonify({"message": "Not a valid userid", "status": "error"}), 400

    if not filename:
        return jsonify({"message": "Not a valid filename", "status": "error"}), 400

    if not field_id:
        return jsonify({"message": "Invalid field_id", "status": "error"}), 400

    if not chat_id:
        return jsonify({"message": "Invalid chat_id", "status": "error"}), 400

    if answer is None:
        return jsonify({"message": "Answer cannot be null", "status": "error"}), 400

    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"

    wf_loc = f"{userid}/workflow/{base_name(filename=filename)}/{filename}"
    workflow_json = read_json_from_s3(wf_loc)

    if not workflow_json:
        return (
            jsonify({"message": f"Workflow '{filename}' not found", "status": "error"}),
            404,
        )

    with WorkflowRunnerV2(
        userid=userid,
        filename=filename,
        workflowJson=workflow_json,
        testing=True,
    ) as service:

        result = asyncio.run(
            service.update_form_field(
                field_id=field_id,
                answer=answer,
                chid=chat_id,
            )
        )

    status_code = 200 if result.get("status") == "success" else 400
    return jsonify(result), status_code


@playbook_bp.route("/update-form-fields-bulk", methods=["POST"])
def updateformfieldsbulkworkflow():
    data = request.json or {}

    userid = data.get("user_id")
    filename = data.get("filename")
    chat_id = data.get("chat_id")
    answers = data.get("answers")  # 🔥 BULK fields

    if not userid:
        return jsonify({"message": "Not a valid userid", "status": "error"}), 400

    if not filename:
        return jsonify({"message": "Not a valid filename", "status": "error"}), 400

    if not chat_id:
        return jsonify({"message": "Invalid chat_id", "status": "error"}), 400

    if not isinstance(answers, list) or not answers:
        return (
            jsonify({"message": "Answers must be a non-empty list", "status": "error"}),
            400,
        )

    for item in answers:
        if not item.get("field_id"):
            return (
                jsonify(
                    {
                        "message": "Each answer must include field_id",
                        "status": "error",
                    }
                ),
                400,
            )

        if item.get("answer") is None:
            return jsonify({"message": "Answer cannot be null", "status": "error"}), 400

    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"

    wf_loc = f"{userid}/workflow/{base_name(filename=filename)}/{filename}"
    workflow_json = read_json_from_s3(wf_loc)

    if not workflow_json:
        return (
            jsonify({"message": f"Workflow '{filename}' not found", "status": "error"}),
            404,
        )

    with WorkflowRunnerV2(
        userid=userid,
        filename=filename,
        workflowJson=workflow_json,
        testing=True,
    ) as service:

        result = asyncio.run(
            service.update_form_bulk(
                answers=answers,
                chid=chat_id,
            )
        )

    status_code = 200 if result.get("status") == "success" else 400
    return jsonify(result), status_code


@playbook_bp.route("/autocheck-workflow", methods=["POST"])
async def autocheckworkflow():
    data = request.json
    userid = data.get("user_id")
    filename = data.get("filename")

    if not userid:
        return jsonify({"message": "Not a valid userid", "status": "error"}), 400
    if not filename:
        return jsonify({"message": "Not a valid filename", "status": "error"}), 400
    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"
    # token = current_user_id.set(userid)

    # ✅ Pre-validate workflow existence
    wf_loc = f"{userid}/workflow/{base_name(filename=filename)}/{filename}"
    workflow_json = read_json_from_s3(wf_loc)
    if not workflow_json:
        return (
            jsonify(
                {
                    "message": f"Workflow file '{filename}' not found ",
                    "status": "error",
                }
            ),
            404,
        )
    db = connect_to_rds()
    credits = Credits(db=db)

    try:
        with WorkflowRunnerV2(
            userid=userid,
            filename=filename,
            workflowJson=workflow_json,
            testing=True,
            db=db,
            credits=credits,
        ) as runner:
            result = await runner.autocheckerworkflow()
            return jsonify({"message": result})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    # finally:
    # current_user_id.reset(token)
    # print("checking auto check workflow")


@playbook_bp.route("/autocheck-status-update", methods=["POST"])
def autocheckstatusupdate():
    data = request.json
    userid = data.get("user_id")
    filename = data.get("filename")
    count = data.get("count")
    status = data.get("status")

    if not userid:
        return jsonify({"message": "Not a valid userid", "status": "error"}), 400
    if not filename:
        return jsonify({"message": "Not a valid filename", "status": "error"}), 400
    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"
    # token = current_user_id.set(userid)
    # try:
    # ✅ Pre-validate workflow existence
    wf_loc = f"{userid}/workflow/{base_name(filename=filename)}/{filename}"
    workflow_json = read_json_from_s3(wf_loc)
    if not workflow_json:
        return (
            jsonify(
                {
                    "message": f"Workflow file '{filename}' not found ",
                    "status": "error",
                }
            ),
            404,
        )

    try:
        with WorkflowRunnerV2(
            userid=userid,
            filename=filename,
            workflowJson=workflow_json,
            testing=True,
        ) as runner:
            result = runner.update_statuscount(count=count, status=status)
            if result:
                return jsonify({"message": "Ok"})
            else:
                return jsonify({"error": "failed to update the auto checker"})
    except Exception as e:
        print(" auto status update check", e)
        return jsonify({"status": "error", "message": str(e)}), 500
    # finally:
    # current_user_id.reset(token)
    # print("updating auto check")


@playbook_bp.route("/workflow/conversation", methods=["POST"])
async def workflow_conversation():

    data = request.json

    user_id = data.get("user_id")
    filename = data.get("filename")
    user_message = data.get("user_message", "")
    testing = data.get("testing") or True

    if not user_id or not filename:
        return jsonify({"error": "invalid request"}), 400

    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"

    wf_loc = f"{user_id}/workflow/{base_name(filename=filename)}/{filename}"
    workflow_json = read_json_from_s3(wf_loc)

    if not workflow_json:
        return jsonify({"status": "error", "message": "Workflow not found"}), 404
    credits = Credits()
    try:
        with WorkflowRunnerV2(
            userid=user_id,
            filename=filename,
            workflowJson=workflow_json,
            testing=testing,
            credits=credits,
        ) as runner:

            result = await runner.make_workflow_conversation(user_message=user_message)
            return jsonify(result)

    except Exception as e:
        print("auto update", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@playbook_bp.route("/wf-form", methods=["POST"])
async def check_formcreation():
    data = request.json

    user_id = data.get("user_id")
    user_input = data.get("user_input")

    from services.automate_service import AutoMateService

    credits = Credits()
    val = AutoMateService(userid=user_id, credits=credits)

    kak = await val.generate_form_schema(user_input)

    return jsonify({"message": kak})


async def send_ques_byfile_bk(
    user_id, extracted_files, filename, job_id=None, session_id=None
):
    from services.automate_service import AutoMateService

    credits = Credits()
    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"

    wf_loc = f"{user_id}/workflow/{base_name(filename=filename)}/{filename}"
    workflow_json = read_json_from_s3(wf_loc)
    ai = AutoMateService(userid=user_id, credits=credits, workflow=workflow_json)

    result = await ai.generate_questions_from_file(extracted_files)
    return result


@playbook_bp.route("/generate_ques_by_file", methods=["POST"])
async def generate_ques_by_file():
    from radar.radar_helpers import extract_files_content

    user_id = request.form.get("user_id")
    uploaded_file = request.files.get("ques_file")
    wf_id = request.form.get("wf_filename")

    if not user_id:
        return jsonify({"error": "No userid provided"}), 400

    if not uploaded_file:
        return jsonify({"error": "No file provided"}), 400

    try:
        # ✅ Read file bytes
        file_bytes = uploaded_file.read()

        files = [
            {
                "filename": uploaded_file.filename,
                "content_type": uploaded_file.content_type,
                "data": file_bytes,
            }
        ]

        # ✅ Extract content
        extracted_files = extract_files_content(files)

        if not extracted_files:
            return jsonify({"error": "Could not extract content from file"}), 400

        # 🔥 SUBMIT BACKGROUND JOB (THIS WAS MISSING)
        job_id = await JobManager.submit_job(
            send_ques_byfile_bk, user_id, extracted_files, wf_id
        )

        return jsonify(
            {
                "status": "accepted",
                "job_id": job_id,
                "message": "Processing started in background",
            }
        )

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


async def answer_ques_file_bk(
    user_id,
    extracted_files,
    filename,
    step_id,
    file_keys,
    inp_links=None,
    job_id=None,
    session_id=None,
):

    credits = Credits()
    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"

    wf_loc = f"{user_id}/workflow/{base_name(filename=filename)}/{filename}"
    workflow_json = read_json_from_s3(wf_loc)
    with WorkflowRunnerV2(
        userid=user_id,
        filename=filename,
        workflowJson=workflow_json,
        testing=True,
        credits=credits,
    ) as runner:

        result = await runner.answer_ques_file_bk(
            extracted_files, step_id, file_keys, inp_links=inp_links or []
        )

    return result


@playbook_bp.route("/make_ans_by_files", methods=["POST"])
async def generate_ans_files():
    local_files = []
    try:
        import mimetypes
        import base64
        from utils.s3_utils import s3bucket
        from radar.radar_helpers import extract_files_content, IMAGE_EXTENSIONS

        user_id = request.form.get("user_id")
        file_keys = request.form.getlist("file_keys")
        step_id = request.form.get("step_id")
        wf_name = request.form.get("wf_name")

        if not user_id or not file_keys:
            return (
                jsonify(
                    {"status": "error", "message": "user_id and file_keys are required"}
                ),
                400,
            )

        s3 = s3bucket()
        if type(file_keys) == str:
            file_keys = [file_keys]

        extracted_payload = []
        inp_links = []

        for key in file_keys:
            if not key:
                continue
            try:
                fname = os.path.basename(key)
                temp_path = os.path.join(tempfile.gettempdir(), fname)
                s3.download_file(Bucket=S3_BUCKET, Key=key, Filename=temp_path)
                local_files.append(temp_path)

                with open(temp_path, "rb") as fh:
                    file_bytes = fh.read()

                ext = os.path.splitext(fname)[1].lower()
                content_type = (
                    mimetypes.guess_type(fname)[0] or "application/octet-stream"
                )

                if ext in IMAGE_EXTENSIONS:
                    b64 = base64.b64encode(file_bytes).decode()
                    inp_links.append(f"data:{content_type};base64,{b64}")
                else:
                    extracted = extract_files_content(
                        [
                            {
                                "filename": fname,
                                "data": file_bytes,
                                "content_type": content_type,
                            }
                        ]
                    )
                    extracted_payload.extend(extracted)

            except Exception as e:
                print(f"Failed to process {key}: {e}")

        if not extracted_payload and not inp_links:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Could not extract content from files",
                    }
                ),
                400,
            )

        job_id = await JobManager.submit_job(
            answer_ques_file_bk,
            user_id,
            extracted_payload,
            wf_name,
            step_id,
            file_keys,
            inp_links,
        )

        return jsonify(
            {
                "status": "accepted",
                "job_id": job_id,
                "message": "Processing started in background",
            }
        )

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        for path in local_files:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception as cleanup_error:
                print(f"Cleanup failed for {path}: {cleanup_error}")


@playbook_bp.route("/evidence_ques_ans_attach_playbook", methods=["POST"])
def evidence_ques_ans_attach_playbook():
    try:
        data = request.json or {}
        userid = data.get("user_id")
        filename = data.get("filename")
        question_id = data.get("question_id")
        user_answer = data.get("user_answer")
        comment = data.get("comment")

        if not userid or not filename or not question_id:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "user_id, filename, question_id are required",
                    }
                ),
                400,
            )

        if not filename.lower().endswith(".json"):
            filename = f"{filename}.json"

        wf_loc = f"{userid}/workflow/{base_name(filename=filename)}/{filename}"
        workflow_json = read_json_from_s3(wf_loc)

        if not workflow_json:
            return jsonify({"status": "error", "message": "Workflow not found"}), 404

        with WorkflowRunnerV2(
            userid=userid,
            filename=filename,
            workflowJson=workflow_json,
            testing=True,
        ) as runner:
            result = runner.answer_evidence_question(
                qid=question_id,
                user_answer=user_answer,
                comment=comment,
            )

        status_code = 200 if result.get("status") == "success" else 400
        return jsonify(result), status_code

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@playbook_bp.route("/make_s3upload", methods=["POST"])
def generatesigned_url_for_upload():
    try:

        s3 = s3bucket()
        data = request.get_json()

        user_id = data.get("user_id")
        files = data.get("filenames", [])

        if not user_id or not files:
            return (
                jsonify(
                    {"status": "error", "message": "user_id and filenames are required"}
                ),
                400,
            )

        # 🔒 Optional: limit number of files
        if len(files) > 100:
            return (
                jsonify({"status": "error", "message": "Too many files (max 100)"}),
                400,
            )

        response_files = []

        for original_filename in files:

            # 🔒 basic validation
            if not isinstance(original_filename, str) or not original_filename.strip():
                continue

            # 🔥 unique filename
            unique_id = uuid.uuid4().hex
            timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")

            filename = f"{timestamp}_{unique_id}_{original_filename}"
            s3_key = f"{user_id}/uploads/{filename}"

            presigned_url = s3.generate_presigned_url(
                ClientMethod="put_object",
                Params={"Bucket": S3_BUCKET, "Key": s3_key},
                ExpiresIn=3600,
            )

            response_files.append(
                {
                    "original_name": original_filename,
                    "file_key": s3_key,
                    "upload_url": presigned_url,
                }
            )

        return jsonify({"status": "success", "files": response_files})

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@playbook_bp.route("/pb_temp_clone", methods=["POST"])
def pb_temp_clone_min():
    try:

        data = request.json or {}

        user_id = data.get("user_id")
        filename = data.get("filename")
        is_global = data.get("is_global", False)

        if not user_id or not filename:
            return (
                jsonify(
                    {"status": "error", "message": "user_id and filename required"}
                ),
                400,
            )

        # ---------------------------------
        # ✅ Normalize filename
        # ---------------------------------
        if not filename.lower().endswith(".json"):
            filename = f"{filename}.json"

        base = base_name(filename=filename)

        # ---------------------------------
        # ✅ Load workflow
        # ---------------------------------
        wf_loc = f"{user_id}/workflow/{base}/{filename}"
        workflow_json = read_json_from_s3(wf_loc)

        if not workflow_json and is_global:
            workflow_json = read_json_from_s3(f"workflow/global/{base}/{filename}")

        if not workflow_json:
            return jsonify({"status": "error", "message": "Workflow not found"}), 404

        # ---------------------------------
        # ✅ Create new workflow
        # ---------------------------------
        new_filename = f"{base}_ch_{uuid.uuid4().hex[:6]}.json"
        # new_base = base_name(filename=new_filename)

        new_workflow = {
            "filename": new_filename,
            "reference_filename": filename,
            "input_data": workflow_json.get("input_data", {}),
            "workflow": workflow_json.get("workflow", {}),
            "WorkflowDate": datetime.now().isoformat(),
            "assigned_questions": workflow_json.get("assigned_questions", []),
            "runbook_id": workflow_json.get("runbook_id", None),
        }

        # ---------------------------------
        # ✅ Save new workflow to S3
        # ---------------------------------
        new_path = f"{user_id}/workflow/{base}/{new_filename}"

        with tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="w") as tmp:
            json.dump(new_workflow, tmp, indent=2)
            temp_file_path = tmp.name

        upload_any_file(
            file_path=temp_file_path,
            user_id=user_id,
            s3_key_C=new_path,  # 🔥 FORCE correct path
        )

        os.remove(temp_file_path)  # ✅ cleanup

        # ---------------------------------
        # ✅ Update chat_config
        # ---------------------------------
        config_path = f"{user_id}/workflow/chat_config.json"
        config_data = read_json_from_s3(config_path) or []

        found = False
        now = datetime.now().isoformat()

        for pb in config_data:
            if pb.get("original") == filename:
                pb.setdefault("runs", []).append(
                    {"name": new_filename, "created_at": now}
                )
                found = True
                break

        if not found:
            config_data.append(
                {
                    "name": new_filename,
                    "original": filename,
                    "description": workflow_json.get("input_data", {}).get(
                        "description"
                    ),
                    "title": workflow_json.get("input_data", {}).get("title"),
                    "num_steps": len(
                        workflow_json.get("workflow", {}).get("steps", [])
                    ),
                    "runs": [{"name": new_filename, "created_at": now}],
                }
            )

        # ---------------------------------
        # ✅ Save updated config
        # ---------------------------------
        with tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="w") as tmp:
            json.dump(config_data, tmp, indent=2)
            temp_config_path = tmp.name

        upload_any_file(
            file_path=temp_config_path,
            user_id=user_id,
            s3_key_C=config_path,  # 🔥 FORCE correct path
        )

        os.remove(temp_config_path)  # ✅ cleanup

        # ---------------------------------
        # ✅ Response
        # ---------------------------------
        return jsonify({"status": "success", "new_filename": new_filename})

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@playbook_bp.route("/list_chat_config", methods=["POST"])
def list_chat_config():
    try:
        data = request.json or {}
        user_id = data.get("user_id")

        if not user_id:
            return jsonify({"status": "error", "message": "user_id required"}), 400

        # ---------------------------------
        # ✅ Load config
        # ---------------------------------
        config_path = f"{user_id}/workflow/chat_config.json"
        config_data = read_json_from_s3(config_path) or []

        if not isinstance(config_data, list):
            return jsonify({"status": "error", "message": "Invalid config format"}), 500

        # ---------------------------------
        # ✅ Group by original
        # ---------------------------------
        grouped = {}

        for item in config_data:
            original = item.get("original")

            if not original:
                continue

            if original not in grouped:
                grouped[original] = {
                    "original": original,
                    "description": item.get("description"),
                    "title": item.get("title"),
                    "num_steps": item.get("num_steps"),
                    "runs": [],
                }

            # Add runs
            runs = item.get("runs", [])
            if isinstance(runs, list):
                grouped[original]["runs"].extend(runs)

        # ---------------------------------
        # ✅ Convert to list
        # ---------------------------------
        response_data = list(grouped.values())

        # Optional: sort runs by created_at DESC
        for group in response_data:
            group["runs"] = sorted(
                group["runs"], key=lambda x: x.get("created_at", ""), reverse=True
            )

        return jsonify({"status": "success", "data": response_data})

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@playbook_bp.route("/share_playbook_template", methods=["POST"])
def share_pb_template():
    try:
        data = request.json or {}

        user_id = data.get("user_id")
        filename = data.get("wf_filename")
        role_assigned = data.get("role_assigned")
        is_for_all = data.get("is_for_all", False)

        if not user_id or not filename:
            return (
                jsonify({"status": "error", "message": "requirements not satisfied"}),
                400,
            )

        # Normalize filename
        if not filename.lower().endswith(".json"):
            filename += ".json"

        base = base_name(filename)

        # Load workflow
        wf_loc = f"{user_id}/workflow/{base}/{filename}"
        workflow_json = read_json_from_s3(wf_loc)

        if not workflow_json:
            return jsonify({"status": "error", "message": "workflow not found"}), 404

        conn = connect_to_rds()
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        email_creator = get_email_by_id(user_id, conn)

        # New template
        new_filename = f"{base}_tp_{uuid.uuid4().hex[:8]}.json"
        new_base = base_name(new_filename)

        new_workflow = {
            "filename": new_filename,
            "reference_filename": filename,
            "input_data": workflow_json.get("input_data", {}),
            "workflow": workflow_json.get("workflow", {}),
            "WorkflowDate": datetime.now().isoformat(),
            "assigned_questions": workflow_json.get("assigned_questions", []),
            "is_global": is_for_all,
            "created_by": email_creator,
            "autotest": {"status": False, "count": 0},
            "runbook_id": workflow_json.get("runbook_id", None),
        }

        with tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="w") as tmp:
            json.dump(new_workflow, tmp, indent=2)
            temp_file_path = tmp.name

        # Fetch permissions
        cursor.execute("SELECT permissions FROM users WHERE user_id = %s", (user_id,))
        row = cursor.fetchone()

        if not row:
            return jsonify({"status": "error", "message": "User not found"}), 404

        owner_permissions = json.loads(row.get("permissions") or "{}")

        emails_set = set()

        for entry in owner_permissions.get("shared", []):
            role_id = entry.get("role", {}).get("id")

            if is_for_all or role_id == role_assigned:
                email = entry.get("email")
                if email:
                    emails_set.add(email.lower())

        if not emails_set:
            return jsonify({"status": "error", "message": "no users found"}), 404

        # 🔥 REDIS TRACKING
        redis_service = RedisService()

        share_id = f"share:{uuid.uuid4().hex}"

        shared_records = []

        for email in emails_set:
            role_userid = get_userid(email)
            if not role_userid:
                continue

            path = f"{role_userid}/workflow/{new_base}/{new_filename}"

            upload_any_file(
                file_path=temp_file_path,
                user_id=role_userid,
                s3_key_C=path,
            )

            playbook_id, config_path, subagent_id = returnconfigandpath(role_userid)

            if not playbook_id:
                config_s3_path = create_empty_playbook_config(role_userid)
                playb_id = str(uuid.uuid4())

                playbook_id, config_path = create_subagent_to_playbook(
                    playb_id, subagent_id, config_s3_path
                )

            update_playbook_config(
                configpath=config_path,
                user_id=role_userid,
                name=new_filename,
                filepath=path,
                referece=filename,
                title=new_workflow["workflow"]["name"],
                description=new_workflow["workflow"]["description"],
                num_steps=len(new_workflow["workflow"]["steps"]),
            )

            shared_records.append(
                {"email": email, "user_id": role_userid, "path": path}
            )

        # Save undo data in Redis (TTL 1 day)
        redis_data = {
            "template": new_filename,
            "reference": filename,
            "shared_with": shared_records,
            "created_by": email_creator,
            "created_at": datetime.now().isoformat(),
        }

        asyncio.run(redis_service.set(share_id, redis_data, ex=86400))

        os.remove(temp_file_path)
        cursor.close()
        conn.close()

        return (
            jsonify(
                {
                    "status": "success",
                    "message": "Template shared successfully",
                    "share_id": share_id,  # 🔥 important for undo
                    "shared_with": list(emails_set),
                    "is_global": is_for_all,
                }
            ),
            200,
        )

    except Exception as e:
        print("Error:", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@playbook_bp.route("/get_all_global_instructions", methods=["GET"])
def get_all_global_instructions():
    user_id = request.args.get("user_id")

    if not user_id:
        return jsonify({"error": "userid is required"}), 400

    config_path = "workflow/global/template_config.json"

    try:
        config_data = read_json_from_s3(config_path) or []

        playbook_list = config_data

        for pb_copy in playbook_list:
            pb_copy.pop("filepath", None)

        return jsonify({"data": playbook_list})

    except Exception as e:
        return jsonify({"error": f"Failed to fetch instructions: {str(e)}"}), 500


@playbook_bp.route("/get_single_global_instruction", methods=["GET"])
def get_single_global_instructions():
    user_id = request.args.get("user_id")
    filename = request.args.get("wf_filename")

    if not user_id or not filename:
        return jsonify({"error": "insufficient details"}), 400

    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"
    s3_key = f"workflow/global/{base_name(filename)}/{filename}"

    try:
        instruction_data = read_json_from_s3(s3_key)
        if not instruction_data:
            return jsonify({"error": "Instruction not found"}), 404
        # instruction_data.pop("filepath", None)
        return jsonify(instruction_data)
    except Exception as e:
        return jsonify({"error": f"Failed to fetch global playbook: {str(e)}"}), 500


@playbook_bp.route("/make_global_playbook", methods=["POST"])
def make_global_playbook():
    try:
        data = request.json or {}
        user_id = data.get("user_id")
        filename = data.get("wf_filename")

        if user_id not in ACCESSIBLE_IDS:
            return jsonify({"error": "UN-Authorized"}), 401

        if not filename:
            return jsonify({"error": "filename required"}), 400

        if not filename.lower().endswith(".json"):
            filename = f"{filename}.json"

        base = base_name(filename)

        # -----------------------
        # Load original workflow
        # -----------------------
        wf_loc = f"{user_id}/workflow/{base}/{filename}"
        workflow_json = read_json_from_s3(wf_loc)

        if not workflow_json:
            return jsonify({"status": "error", "message": "workflow not found"}), 404

        # -----------------------
        # Create new template
        # -----------------------
        new_filename = f"{uuid.uuid4().hex[:8]}.json"
        new_base = base_name(new_filename)

        new_workflow = {
            "filename": new_filename,
            "reference_filename": filename,
            "input_data": workflow_json.get("input_data", {}),
            "workflow": workflow_json.get("workflow", {}),
            "WorkflowDate": datetime.now().isoformat(),
            "assigned_questions": workflow_json.get("assigned_questions", []),
            "is_global": True,
            "created_by": "bytoid",
            "autotest": {"status": False, "count": 0},
            "runbook_id": workflow_json.get("runbook_id", None),
        }

        new_path = f"workflow/global/{new_base}/{new_filename}"

        # upload workflow
        with tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="w") as tmp:
            json.dump(new_workflow, tmp, indent=2)
            temp_file_path = tmp.name

        upload_any_file(temp_file_path, user_id, s3_key_C=new_path)

        # -----------------------
        # Update config
        # -----------------------
        config_path = "workflow/global/template_config.json"
        config_data = read_json_from_s3(config_path) or []

        config_data.append(
            {
                "filename": new_filename,
                "reference_filename": filename,
                "WorkflowDate": datetime.now().isoformat(),
                "filepath": new_path,
                "title": new_workflow["workflow"].get("name"),
                "description": new_workflow["workflow"].get("description"),
                "num_steps": len(new_workflow["workflow"].get("steps", [])),
                "is_global": True,
                "created_by": "bytoid",
            }
        )

        # save config
        with tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="w") as tmp:
            json.dump(config_data, tmp, indent=2)
            temp_config_path = tmp.name

        upload_any_file(temp_config_path, user_id, s3_key_C=config_path)

        # cleanup
        os.remove(temp_file_path)
        os.remove(temp_config_path)

        return (
            jsonify(
                {
                    "status": "success",
                    "message": "Global Template successfully created",
                    "template": new_filename,
                }
            ),
            200,
        )

    except Exception as e:
        print("error", e)
        return jsonify({"error": str(e)}), 500


@playbook_bp.route("/delete_global_playbook", methods=["DELETE"])
def delete_global_playbook():
    try:
        data = request.json or {}
        user_id = data.get("user_id")
        filename = data.get("wf_filename")

        if user_id not in ACCESSIBLE_IDS:
            return jsonify({"error": "UN-Authorized"}), 401

        if not filename:
            return jsonify({"error": "filename required"}), 400

        config_path = "workflow/global/template_config.json"

        success = deleteGlobalConfigdata(config_path, filename)

        if not success:
            return jsonify({"error": "Template not found or delete failed"}), 404

        return (
            jsonify(
                {"status": "success", "message": "Global template deleted successfully"}
            ),
            200,
        )

    except Exception as e:
        print("error", e)
        return jsonify({"error": str(e)}), 500


@playbook_bp.route("/install_global_playbook", methods=["POST"])
def install_global_playbook():
    try:
        data = request.json or {}
        user_id = data.get("user_id")
        filename = data.get("wf_filename")
        # ---------------------------------
        # Normalize filename
        # ---------------------------------
        if not filename.lower().endswith(".json"):
            filename = f"{filename}.json"

        base = base_name(filename=filename)

        # ---------------------------------
        # Load workflow
        # ---------------------------------
        global_path = f"workflow/global/{base}/{filename}"

        workflow_json = read_json_from_s3(global_path)

        if not workflow_json:
            return jsonify({"status": "error", "message": "workflow not found"}), 404
        # ---------------------------------
        # Create new workflow
        # ---------------------------------
        new_filename = f"{uuid.uuid4().hex[:8]}.json"
        new_base = base_name(filename=new_filename)

        new_workflow = {
            "filename": new_filename,
            "reference_filename": filename,
            "input_data": workflow_json.get("input_data", {}),
            "workflow": workflow_json.get("workflow", {}),
            "WorkflowDate": datetime.now().isoformat(),
            "assigned_questions": workflow_json.get("assigned_questions", []),
            "is_global": True,
            "created_by": "bytoid",
            "autotest": {"status": False, "count": 0},
            "runbook_id": workflow_json.get("runbook_id", None),
        }
        new_path = f"{user_id}/workflow/{new_base}/{new_filename}"
        with tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="w") as tmp:
            json.dump(new_workflow, tmp, indent=2)
            temp_file_path = tmp.name

        upload_any_file(
            file_path=temp_file_path,
            user_id=user_id,
            s3_key_C=new_path,
        )

        playbook_id, config_path, subagent_id = returnconfigandpath(userid=user_id)

        if not playbook_id:
            config_s3_path = create_empty_playbook_config(user_id)
            playb_id = str(uuid.uuid4())

            playbook_id, config_path = create_subagent_to_playbook(
                playb_id, subagent_id, config_s3_path
            )

        update_playbook_config(
            configpath=config_path,
            user_id=user_id,
            name=new_filename,  # ✅ FIXED
            filepath=new_path,
            referece=filename,
            title=new_workflow["workflow"]["name"],
            description=new_workflow["workflow"]["description"],
            num_steps=len(new_workflow["workflow"]["steps"]),
        )
        os.remove(temp_file_path)
        return (
            jsonify(
                {
                    "status": "success",
                    "message": "Global Template successfully",
                    "template": new_filename,
                }
            ),
            200,
        )
    except Exception as e:
        print("error", e)


@playbook_bp.route("/undo_share_playbook_template", methods=["POST"])
def undo_share_pb_template():
    try:
        data = request.json or {}

        user_id = data.get("user_id")
        filename = data.get("wf_filename")
        role_assigned = data.get("role_assigned")
        is_for_all = data.get("is_for_all", False)

        if not user_id or not filename:
            return (
                jsonify({"status": "error", "message": "requirements not satisfied"}),
                400,
            )

        if not filename.lower().endswith(".json"):
            filename += ".json"

        base = base_name(filename)

        # ---------------------------------
        # DB connection
        # ---------------------------------
        conn = connect_to_rds()
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        cursor.execute("SELECT permissions FROM users WHERE user_id = %s", (user_id,))
        row = cursor.fetchone()

        if not row:
            return jsonify({"status": "error", "message": "User not found"}), 404

        owner_permissions = json.loads(row.get("permissions") or "{}")

        emails_set = set()

        # ---------------------------------
        # SAME LOGIC AS SHARE
        # ---------------------------------
        for entry in owner_permissions.get("shared", []):
            role_name = entry.get("role", {}).get("name")

            if is_for_all or role_name == role_assigned:
                email = entry.get("email")
                if email:
                    emails_set.add(email.lower())

        if not emails_set:
            return jsonify({"status": "error", "message": "no users found"}), 404

        # ---------------------------------
        # DELETE FROM EACH USER
        # ---------------------------------
        deleted_users = []
        failed_users = []

        for email in emails_set:
            try:
                role_userid = get_userid(email)
                if not role_userid:
                    continue

                subagent_id = get_subagent_by_userid(role_userid)
                if not subagent_id:
                    failed_users.append(email)
                    continue

                playbook_id, config_path = check_subagent_by_playbook(subagent_id)

                if not config_path:
                    failed_users.append(email)
                    continue

                success = deleteConfigdata(
                    configpath=config_path, user_id=role_userid, name=filename
                )

                if success:
                    deleted_users.append(email)
                else:
                    failed_users.append(email)

            except Exception as e:
                print(f"Error deleting for {email}:", e)
                failed_users.append(email)

        cursor.close()
        conn.close()

        return (
            jsonify(
                {
                    "status": "success",
                    "message": "Undo completed",
                    "deleted_from": deleted_users,
                    "failed": failed_users,
                }
            ),
            200,
        )

    except Exception as e:
        print("Undo error:", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@playbook_bp.route("/edit_assigned_question", methods=["POST"])
async def edit_assigned_question():
    try:
        data = request.json or {}

        userid = data.get("user_id")
        filename = data.get("filename")
        question_id = data.get("question_id")
        new_question_text = data.get("new_question_text")

        # ----------------------------
        # VALIDATION
        # ----------------------------
        if not userid or not filename or not question_id or not new_question_text:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "user_id, filename, question_id, new_question_text are required",
                    }
                ),
                400,
            )

        if not filename.lower().endswith(".json"):
            filename = f"{filename}.json"

        wf_loc = f"{userid}/workflow/{base_name(filename=filename)}/{filename}"
        workflow_json = read_json_from_s3(wf_loc)

        if not workflow_json:
            return jsonify({"status": "error", "message": "Workflow not found"}), 404

        # ----------------------------
        # EXECUTION
        # ----------------------------
        with WorkflowRunnerV2(
            userid=userid,
            filename=filename,
            workflowJson=workflow_json,
            testing=True,
        ) as runner:

            result = runner.edit_assigned_question(
                qid=question_id, new_question=new_question_text
            )

            return jsonify(result)

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@playbook_bp.route("/delete_assigned_question", methods=["POST"])
async def delete_assigned_question():
    try:
        data = request.json or {}

        userid = data.get("user_id")
        filename = data.get("filename")
        question_id = data.get("question_id")

        # ----------------------------
        # VALIDATION
        # ----------------------------
        if not userid or not filename or not question_id:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "user_id, filename, question_id are required",
                    }
                ),
                400,
            )

        if not filename.lower().endswith(".json"):
            filename = f"{filename}.json"

        wf_loc = f"{userid}/workflow/{base_name(filename=filename)}/{filename}"
        workflow_json = read_json_from_s3(wf_loc)

        if not workflow_json:
            return jsonify({"status": "error", "message": "Workflow not found"}), 404

        # ----------------------------
        # EXECUTION
        # ----------------------------
        with WorkflowRunnerV2(
            userid=userid,
            filename=filename,
            workflowJson=workflow_json,
            testing=True,
        ) as runner:

            result = runner.delete_assigned_question(qid=question_id)

            return jsonify(result)

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@playbook_bp.route("/check_runbook_exists_playbook", methods=["POST"])
def check_runbook_exists_playbook():
    try:
        data = request.json or {}

        userid = data.get("user_id")
        filename = data.get("filename")

        if not userid or not filename:
            return (
                jsonify(
                    {"status": "error", "message": "user_id and filename are required"}
                ),
                400,
            )

        # ensure .json
        if not filename.lower().endswith(".json"):
            filename = f"{filename}.json"

        wf_loc = f"{userid}/workflow/{base_name(filename=filename)}/{filename}"

        workflow_json = read_json_from_s3(wf_loc)

        if not workflow_json:
            return jsonify({"status": "error", "message": "Workflow not found"}), 404

        runbook_id = workflow_json.get("runbook_id")

        return (
            jsonify(
                {
                    "status": "success",
                    "runbook": runbook_id,
                    "is_available": bool(runbook_id),
                }
            ),
            200,
        )

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@playbook_bp.route("/clear_runbook_exists_playbook", methods=["POST"])
async def clear_runbook_exists_playbook():
    from db.lance_db_service import LanceDBServer

    try:
        data = request.json or {}

        userid = data.get("user_id")
        filename = data.get("filename")

        if not userid or not filename:
            return (
                jsonify(
                    {"status": "error", "message": "user_id and filename are required"}
                ),
                400,
            )

        # ensure .json
        if not filename.lower().endswith(".json"):
            filename = f"{filename}.json"

        wf_loc = f"{userid}/workflow/{base_name(filename=filename)}/{filename}"

        workflow_json = read_json_from_s3(wf_loc)

        if not workflow_json:
            return jsonify({"status": "error", "message": "Workflow not found"}), 404

        runbook_id = workflow_json.get("runbook_id")

        if not runbook_id:
            return (
                jsonify(
                    {
                        "status": "success",
                        "message": "No runbook id to clear",
                        "runbook": None,
                    }
                ),
                200,
            )

        # ✅ clear runbook
        workflow_json["runbook_id"] = None
        dbserver = LanceDBServer()
        runbook_details = await dbserver.get_runbook_by_id(userid, runbook_id)
        if "playbook_id" in runbook_details:
            runbook_details["playbook_id"] = None
            dbserver.update_runbook(runbook_details)

        save_playbook_to_s3(
            workflow_json,
            userid,
            "workflow updated successfully",
            workflow_json.get("filename", filename),  # safer
        )

        return (
            jsonify({"status": "success", "message": "Runbook cleared successfully"}),
            200,
        )

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@playbook_bp.route("/morph_question", methods=["POST"])
def morph_questions():
    try:
        data = request.json or {}

        userid = data.get("user_id")
        filename = data.get("filename")
        question_id = data.get("question_id")
        morph_type = data.get("morph_type")
        new_question_text = data.get("new_question_text")
        new_options = data.get("new_options")

        # ----------------------------
        # VALIDATION
        # ----------------------------
        if not userid or not filename or not question_id or not new_question_text:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "user_id, filename, question_id, new_question_text are required",
                    }
                ),
                400,
            )

        if morph_type not in ["text_to_option", "option_to_text", "update_only"]:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Invalid morph_type. Allowed: text_to_option, option_to_text, update_only",
                    }
                ),
                400,
            )

        # Require options only for text_to_option
        if morph_type == "text_to_option":
            if not new_options or not isinstance(new_options, dict):
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": "new_options (dict) is required for text_to_option",
                        }
                    ),
                    400,
                )

        if not filename.lower().endswith(".json"):
            filename = f"{filename}.json"

        # ----------------------------
        # LOAD WORKFLOW
        # ----------------------------
        wf_loc = f"{userid}/workflow/{base_name(filename=filename)}/{filename}"
        workflow_json = read_json_from_s3(wf_loc)

        if not workflow_json:
            return jsonify({"status": "error", "message": "Workflow not found"}), 404

        # ----------------------------
        # EXECUTION
        # ----------------------------
        with WorkflowRunnerV2(
            userid=userid,
            filename=filename,
            workflowJson=workflow_json,
            testing=True,
        ) as runner:

            result = runner.morph_question(
                qid=question_id,
                new_question=new_question_text,
                morph_type=morph_type,
                new_options=new_options,
            )

            status_code = 200 if result.get("status") == "success" else 400
            return jsonify(result), status_code

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@playbook_bp.route("/assign_evidence_to_question", methods=["POST"])
def assign_evidence_to_question():
    try:
        data = request.json or {}

        userid = data.get("user_id")
        filename = data.get("filename")
        question_id = data.get("question_id")
        evidences_required = data.get("evidences_required")

        if not userid or not filename or not question_id:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "user_id, filename, question_id are required",
                    }
                ),
                400,
            )

        if evidences_required is None or not isinstance(evidences_required, list):
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "evidences_required must be a list",
                    }
                ),
                400,
            )

        if not filename.lower().endswith(".json"):
            filename = f"{filename}.json"

        wf_loc = f"{userid}/workflow/{base_name(filename=filename)}/{filename}"
        workflow_json = read_json_from_s3(wf_loc)

        if not workflow_json:
            return jsonify({"status": "error", "message": "Workflow not found"}), 404

        with WorkflowRunnerV2(
            userid=userid,
            filename=filename,
            workflowJson=workflow_json,
            testing=True,
        ) as runner:

            result = runner.assign_evidence_required(
                qid=question_id,
                evidences_required=evidences_required,
            )

            status_code = 200 if result.get("status") == "success" else 400
            return jsonify(result), status_code

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
