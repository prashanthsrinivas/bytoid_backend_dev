import re
import uuid
from db.db_checkers import (
    check_subagent_by_playbook,
    create_subagent_to_playbook,
    get_subagent_by_userid,
    save_or_update_workflow_schedule,
)
from db.rds_db import connect_to_rds
from flask import Blueprint, request, jsonify, Response, stream_with_context
import json, uuid
from cust_helpers import pathconfig
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
from request_context import current_user_id
import pytz

playbook_bp = Blueprint("playbook", __name__)
PLAY_TEMPLATE = load_yaml_file(path=pathconfig.play_template)
MINOR_PROMPTS = load_yaml_file(path=pathconfig.minor_prompts)
ALL_FUNCTIONS = read_function_jsons2(Full=True)

from concurrent.futures import ThreadPoolExecutor


executor = ThreadPoolExecutor(max_workers=4)


def base_name(filename):
    base_name = os.path.splitext(filename)[0]
    return base_name


@playbook_bp.route("/create_instruction", methods=["POST"])
def create_new_instruction():
    data = request.json
    userid = data["user_id"]

    playbook_id, config_path, subagent_id = returnconfigandpath(userid)

    if not playbook_id:
        config_s3_path = create_empty_playbook_config(userid)
        playb_id = str(uuid.uuid4())

        playbook_id, config_path = create_subagent_to_playbook(
            playb_id, subagent_id, config_s3_path
        )

    async def _create_and_update():
        full_output, npath = await create_playbook(
            data=data,
            template_data=PLAY_TEMPLATE,
            minor_data=MINOR_PROMPTS,
            functions_ds=ALL_FUNCTIONS,
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
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Failed to create instruction",
                    "error": str(e),
                }
            ),
            500,
        )

    return jsonify({"status": "success", "data": full_output})


@playbook_bp.route("/update_instruction", methods=["POST"])
def updateInstruction():
    data = request.json
    userid = data["user_id"]
    filename = data["filename"]

    playbook_id, config_path, subagent_id = returnconfigandpath(userid)
    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"

    async def _create_and_update():
        full_output, npath = await create_playbook(
            data=data,
            template_data=PLAY_TEMPLATE,
            minor_data=MINOR_PROMPTS,
            functions_ds=ALL_FUNCTIONS,
            nfilename=filename,
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
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Failed to update instruction",
                    "error": str(e),
                }
            ),
            500,
        )

    return jsonify({"status": "success", "data": full_output})


@playbook_bp.route("/get_all_instructions", methods=["GET"])
def get_all_instructions():
    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify({"error": "userid is required"}), 400
    subagent_id = get_subagent_by_userid(user_id)
    if not subagent_id:
        return jsonify({"error": "no agent found"}), 400
    config_path = None
    playbook_id, config_path = check_subagent_by_playbook(subagent_id)
    if not user_id or not config_path:
        return jsonify({"error": "user_id and config_path are required"}), 400
    # config_path = "107642411636394027005/workflow/config_playbook_0195b8dd.json"
    try:
        config_data = read_json_from_s3(config_path)
        if not config_data or user_id not in config_data:
            return jsonify({"data": []})  # No instructions yet
        playbook_list = config_data[user_id].get("playbooklist", [])
        for playbook in playbook_list:
            playbook.pop("filepath", None)
        return jsonify({"data": playbook_list})
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
            if step.get("id") == previous_step_id:
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
    for i, step in enumerate(steps):
        if step["id"] == step_data["id"]:
            steps[i] = step_data
            updated = True
            break

    if not updated:
        return jsonify({"status": "error", "message": "Step ID not found"}), 404

    playbook["workflow"]["steps"] = steps
    playbook["WorkflowDate"] = datetime.now().isoformat()
    return save_playbook_to_s3(playbook, user_id, "Step edited successfully", filename)


# @playbook_bp.route("/update_step_arguments", methods=["POST"])
# def update_step_arguments():
#     try:
#         body = request.json

#         user_id = body.get("user_id")
#         filename = body.get("filename")
#         step_id = body.get("step_id")
#         new_arguments = body.get("arguments")

#         if not user_id or not filename or step_id is None or new_arguments is None:
#             return (
#                 jsonify(
#                     {
#                         "status": "error",
#                         "message": "Missing user_id, filename, step_id, or arguments",
#                     }
#                 ),
#                 400,
#             )
#         if not filename.lower().endswith(".json"):
#             filename = f"{filename}.json"
#         # -----------------------------------------------------------
#         # 1) Load playbook from S3
#         # -----------------------------------------------------------
#         try:
#             playbook = read_json_from_s3(
#                 f"{user_id}/workflow/{base_name(filename)}/{filename}"
#             )
#         except Exception as e:
#             return (
#                 jsonify(
#                     {
#                         "status": "error",
#                         "message": f"Failed to load playbook: {e}",
#                     }
#                 ),
#                 500,
#             )

#         steps = playbook.get("workflow", {}).get("steps", [])
#         updated = False

#         # -----------------------------------------------------------
#         # 2) Update the step arguments
#         # -----------------------------------------------------------
#         try:
#             for step in steps:
#                 if step.get("id") == int(step_id):

#                     # Must have function_call.arguments
#                     if (
#                         "function_call" not in step
#                         or "arguments" not in step["function_call"]
#                     ):
#                         return (
#                             jsonify(
#                                 {
#                                     "status": "error",
#                                     "message": f"Step {step_id} does not contain function_call.arguments",
#                                 }
#                             ),
#                             400,
#                         )

#                     # Replace ONLY arguments
#                     step["function_call"]["arguments"] = new_arguments

#                     # Remove filled arguments from requirements_needed
#                     req_list = step.get("requirements_needed", [])
#                     for arg in new_arguments.keys():
#                         if arg in req_list:
#                             req_list.remove(arg)
#                     step["requirements_needed"] = req_list

#                     updated = True
#                     break
#         except Exception as e:
#             return (
#                 jsonify(
#                     {
#                         "status": "error",
#                         "message": f"Failed while updating step arguments: {e}",
#                     }
#                 ),
#                 500,
#             )

#         if not updated:
#             return jsonify({"status": "error", "message": "Step ID not found"}), 404

#         # -----------------------------------------------------------
#         # 3) Update workflow date
#         # -----------------------------------------------------------
#         playbook["WorkflowDate"] = datetime.now().isoformat()
#         if "pre_user_data" not in playbook:
#             playbook["pre_user_data"] = {}

#         # -----------------------------------------------------------
#         # 4) Call storeargument_results (inside WorkflowRunnerV2)
#         # -----------------------------------------------------------
#         try:
#             with WorkflowRunnerV2(
#                 userid=user_id,
#                 filename=filename,
#                 workflowJson=playbook,
#                 testing=True,
#             ) as runner:
#                 # print("adding values to the ")
#                 values = runner.storeargument_results(
#                     nfunction_args=new_arguments,
#                     execution_result={},  # satisfies signature
#                 )
#                 playbook["pre_user_data"] = values
#         except Exception as e:
#             return (
#                 jsonify(
#                     {
#                         "status": "error",
#                         "message": f"Failed in storeargument_results: {e}",
#                     }
#                 ),
#                 500,
#             )

#         # -----------------------------------------------------------
#         # 5) Save back to S3
#         # -----------------------------------------------------------
#         try:
#             return save_playbook_to_s3(
#                 playbook, user_id, "Step arguments updated successfully", filename
#             )
#         except Exception as e:
#             return (
#                 jsonify(
#                     {
#                         "status": "error",
#                         "message": f"Failed to save playbook: {e}",
#                     }
#                 ),
#                 500,
#             )


#     except Exception as main_e:
#         return (
#             jsonify(
#                 {
#                     "status": "error",
#                     "message": f"Unexpected error: {main_e}",
#                 }
#             ),
#             500,
#         )
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
            if step.get("id") == int(step_id):

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
        if step.get("id") == int(step_id):

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

    if not step_id or not user_id or not filename:
        return (
            jsonify(
                {"status": "error", "message": "Missing step_id, user_id, or filename"}
            ),
            400,
        )
    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"

    playbook = read_json_from_s3(f"{user_id}/workflow/{base_name(filename)}/{filename}")
    steps = playbook.get("workflow", {}).get("steps", [])

    # Check if the step exists
    step_found = any(s.get("id") == step_id for s in steps)
    if not step_found:
        return jsonify({"status": "error", "message": "Step ID not found"}), 404

    # 1. Remove the step itself
    new_steps = [s for s in steps if s.get("id") != step_id]

    # 2. Clean up all references to this step in next_step fields
    for step in new_steps:
        if "next_step" in step:
            if isinstance(step["next_step"], list):
                step["next_step"] = [nid for nid in step["next_step"] if nid != step_id]
                if not step["next_step"]:  # remove empty list
                    del step["next_step"]
            elif step["next_step"] == step_id:
                del step["next_step"]

    # 3. Save updated steps back to playbook
    playbook["workflow"]["steps"] = new_steps
    playbook["WorkflowDate"] = datetime.now().isoformat()

    return save_playbook_to_s3(playbook, user_id, "Step deleted successfully", filename)


@playbook_bp.route("/modify_instruction", methods=["POST"])
async def modify_instruction(ud_inst=None, user_id=None, filename=None, add_data=None):
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
        # 2. LOAD PROMPTS
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
        # 3. LOAD EXISTING WORKFLOW
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

        # Prepare prompt
        workflow_json_str = json.dumps(original_json["workflow"], indent=2)
        contacts_pr = original_json["input_data"]["contacts"] or "All"
        services_functions = read_function_jsons()

        # -----------------------------
        # 4. BUILD MODIFY PROMPT
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
        # 5. MEETING INTENT DETECTION
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
        # 6. INJECT MEETING RULES
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
        # 7. CALL MODIFY LLM
        # -----------------------------

        # token = current_user_id.set(user_id)
        # try:
        #     llm_response = await get_fireworks_response2(
        #         full_prompt, role="system", temp=0.5
        #     )
        # finally:
        #     current_user_id.reset(token)
        llm_response = await get_fireworks_response2(
            user_message=full_prompt, role="system", temp=0.5, user_id=user_id
        )

        cleaned_response = extract_json_from_llm_output(llm_response)
        modified_json = json.loads(cleaned_response)

        if "unrelated_instruction_message" in modified_json:
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
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Modified workflow missing steps.",
                    }
                ),
                500,
            )
        # print("modified json", modified_json)
        # # -----------------------------
        # # 8. RUN EVALUATOR (POST-MODIFY)
        # # -----------------------------
        # evaluator_prompt = eval_prompt
        # evaluator_prompt = evaluator_prompt.replace(
        #     "{instruction_input}",
        #     json.dumps(
        #         {
        #             "update_instruction": update_instruction,
        #             "todays_date": datetime.now().strftime("%Y-%m-%d"),
        #         },
        #         separators=(",", ":"),
        #     ),
        # )

        # evaluator_prompt = evaluator_prompt.replace(
        #     "{workflow_json}", json.dumps(modified_json, separators=(",", ":"))
        # )

        # evaluator_prompt = evaluator_prompt.replace(
        #     "{functions_data}", services_functions
        # )

        # # print("evaluator prompt", evaluator_prompt)

        # eval_response = get_evaluator_fireworks(evaluator_prompt, role="system",user_id=user_id)

        # # print("🔎 Evaluator RAW response:", repr(eval_response))

        # if not eval_response or not eval_response.strip():
        #     raise ValueError("Evaluator returned EMPTY response")

        # eval_cleaned = clean_json_block(eval_response)

        # # print("🔎 Evaluator CLEANED response:", repr(eval_cleaned))

        # if not eval_cleaned or not eval_cleaned.strip():
        #     raise ValueError("Evaluator returned NON-JSON or empty output")

        # eval_result = json.loads(eval_cleaned)

        # if "workflow" not in eval_result:
        #     raise ValueError("Evaluator JSON missing 'workflow' key")

        # final_workflow = eval_result["workflow"]
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
        result = returnconfigandpath(user_id)
        if isinstance(result, tuple) and len(result) == 3:
            _, config_path, _ = result

        # return original_json
        update_playbook_config(
            configpath=config_path,
            user_id=user_id,
            name=original_json["filename"],
            filepath=f"{user_id}/workflow/{filename}",
            title=original_json["workflow"]["name"],
            description=original_json["workflow"]["description"],
            num_steps=len(original_json["workflow"]["steps"]),
        )
        message = original_json.get(
            "modified_message", "Workflow updated successfully."
        )

        return save_playbook_to_s3(original_json, user_id, message, filename)

    except Exception as e:
        print(f"❌ modify_instruction fatal error: {e}")
        return (
            jsonify(
                {
                    "status": "error",
                    "message": f"Internal server error: {e}",
                }
            ),
            500,
        )


@playbook_bp.route("/workflow-aisuggest", methods=["POST"])
async def workflow_ai_suggest():
    try:
        body = request.json
        user_id = body.get("user_id")
        category = body.get("quote")  # "quote" is the category
        question = body.get("question")
        filename = body.get("filename")
        # token = current_user_id.set(user_id)
        # Validate required fields
        if not all([user_id, category, question, filename]):
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Missing required fields: user_id, quote (category), question, or filename.",
                    }
                ),
                400,
            )
        if not filename.lower().endswith(".json"):
            filename = f"{filename}.json"  # try:

        # Load prompt template from YAML
        promptfile = PLAY_TEMPLATE
        workflow_json = read_json_from_s3(
            filepath=f"{user_id}/workflow/{base_name(filename)}/{filename}"
        )
        validation_prompt = promptfile.get("ai_suggest_workflow_ans")

        if not validation_prompt:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Prompt template 'ai_suggest_workflow_ans' not found.",
                    }
                ),
                500,
            )

        # Format the prompt
        prompt_input = validation_prompt.format(
            category=category.strip() if category else "",
            question=question.strip() if question else "",
            workflow_json=json.dumps(workflow_json, indent=2),
        )

        # Call LLM
        # llm_output = get_fireworks_response(prompt_input, role="system")
        llm_output = await get_fireworks_response2(
            user_message=prompt_input, role="system", temp=0.7, user_id=user_id
        )
        ai_answer = llm_output.strip()

        # Clean markdown formatting if returned (optional safety)
        if ai_answer.startswith("```"):
            ai_answer = ai_answer.strip("` \n")
        # finally:
        #     current_user_id.reset(token)

        return (
            jsonify(
                {
                    "status": "success",
                    "user_id": user_id,
                    "filename": filename,
                    "category": category,
                    "question": question,
                    "ai_answer": ai_answer,
                }
            ),
            200,
        )

    except Exception as e:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "An error occurred while processing the question.",
                    "details": str(e),
                }
            ),
            500,
        )


@playbook_bp.route("/run_workflow", methods=["POST"])
async def runWorkflow():
    data = request.json
    userid = data.get("user_id")
    filename = data.get("filename")
    testing = data.get("testing")

    if not userid:
        return jsonify({"message": "Not a valid userid", "status": "error"}), 400
    if not filename:
        return jsonify({"message": "Not a valid filename", "status": "error"}), 400
    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"
    # token = current_user_id.set(userid)
    try:
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
                testing=testing,
            ) as runner:
                await runner.execute()
                result = runner.get_execution_log()
                return jsonify({"status": "success", "execution_log": result})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        # current_user_id.reset(token)
        print("ss")


@playbook_bp.route("/run_workflow_step", methods=["POST"])
def run_workflow_step():
    data = request.json
    userid = data.get("user_id")
    filename = data.get("filename")
    step_id = data.get("step_id")

    if not userid:
        return jsonify({"message": "Not a valid userid", "status": "error"}), 400
    if not filename:
        return jsonify({"message": "Not a valid filename", "status": "error"}), 400
    if not step_id:
        return jsonify({"message": "Missing step_id", "status": "error"}), 400
    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"
    # token = current_user_id.set(userid)
    try:
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
                userid=userid, filename=filename, workflowJson=workflow_json
            ) as runner:
                steps = runner.steps
                selected_step = steps.get(step_id)

                if not selected_step:
                    return (
                        jsonify(
                            {
                                "message": f"Step '{step_id}' not found in workflow.",
                                "status": "error",
                            }
                        ),
                        404,
                    )

                step_result = runner._execute_step(selected_step)
                execution_log = runner.get_execution_log()

                return jsonify(
                    {
                        "status": "success",
                        "workflow_step_result": step_result,
                        "execution_log": execution_log,
                    }
                )
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        # current_user_id.reset(token)
        print("ss")


@playbook_bp.route("/test-playground-step", methods=["GET", "POST"])
def testworkflowbyinput_stream():
    if request.method == "POST":
        data = request.json
        userid = data.get("user_id")
        userinput = data.get("userinput")
        filename = data.get("filename")
    else:
        userid = request.args.get("user_id")
        userinput = request.args.get("userinput")
        filename = request.args.get("filename")
    # print("details", userid, userinput, filename)

    if not userid:
        return jsonify({"message": "Not a valid userid", "status": "error"}), 400
    if not filename:
        return jsonify({"message": "Not a valid filename", "status": "error"}), 400
    if not userinput:
        return jsonify({"message": "Missing userinput", "status": "error"}), 400

    # token = current_user_id.set(userid)
    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"
    try:

        wf_loc = f"{userid}/workflow/{base_name(filename=filename)}/{filename}"
        workflow_json = read_json_from_s3(wf_loc)

        if not workflow_json:
            return (
                jsonify(
                    {"message": f"Workflow '{filename}' not found", "status": "error"}
                ),
                404,
            )

        def event_stream():
            try:
                with WorkflowRunnerV2(
                    userid=userid,
                    filename=filename,
                    workflowJson=workflow_json,
                    testing=True,
                ) as service:
                    # token = current_user_id.set(userid)
                    # try:
                    #     result = asyncio.run(service.check_input_tone(user_input=userinput))
                    # finally:
                    #     current_user_id.reset(token)
                    result = asyncio.run(service.check_input_tone(user_input=userinput))

                # print("result from back", result)

                yield f"event: done\ndata: {json.dumps(result)}\n\n"

            except Exception as e:
                yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"

    finally:
        # current_user_id.reset(token)
        print("ss")

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
        print("Error clearing playground data:", e)
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
        print("Error clearing testing data:", e)
        return (
            jsonify(
                {"message": f"Error clearing testing data: {str(e)}", "status": "error"}
            ),
            500,
        )


@playbook_bp.route("/generate-workflow-input", methods=["POST"])
async def generate_workflow_input():
    try:
        data = request.get_json(force=True)
        userid = data.get("user_id")
        inp_description = data.get("description", "").strip()
        if not userid:
            return {"error": "need userid"}
        if not inp_description:
            return {"error": "need input"}
        if not inp_description:
            return jsonify({"error": "Missing 'description' field"}), 400
        connection = connect_to_rds()
        main_user_account_type = fetch_user_Social(
            user_id=userid, connection=connection
        )
        print("maiun user logged in", main_user_account_type)

        # ✅ Available communication modes
        available_modes = [
            "auto",
            "gmail",
            "google_meet",
            "microsoft_calendar",
            "outlook",
            "calendar",
        ]

        # token = current_user_id.set(userid)
        # try:

        # ✅ Load all available service functions
        services_section = read_function_jsons2()

        # ✅ Load the YAML prompt
        prompt_yaml = PLAY_TEMPLATE
        prompt_template = prompt_yaml.get("create_workflow_context")
        prompt_text = yaml.dump(prompt_template, sort_keys=False)
        # ✅ Inject dynamic values
        formatted_prompt = (
            prompt_text.replace("{{inp_description}}", inp_description)
            .replace("{{main_user_account_type}}", main_user_account_type)
            .replace("{{available_communication_modes}}", json.dumps(available_modes))
            .replace("{{services_section}}", json.dumps(services_section))
        )
        ##print("fomatted prompt", formatted_prompt)

        # ✅ Call the LLM
        llm_output = await get_fireworks_response2(
            user_message=formatted_prompt, role="system", temp=0.3, user_id=userid
        )
        # llm_output = get_evaluator_gpt4(formatted_prompt).strip()
        llm_output = re.sub(
            r"^```(?:json)?\s*|\s*```$", "", llm_output, flags=re.MULTILINE
        ).strip()

        # ✅ Parse JSON safely
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

        with connection.cursor() as cursor:

            # =====================================================
            # 1️⃣ RESOLVE NAMES
            # =====================================================
            names = need_contacts.get("names", [])

            for name in names:
                key = name.strip()
                if not key:
                    continue

                query = """
                    SELECT DISTINCT uc.email_id
                    FROM users_clients uc
                    JOIN communication c
                    ON uc.communication_id_fk = c.communication_id
                    WHERE c.user_id_fk = %s
                    AND (
                            LOWER(uc.first_name) LIKE %s
                        OR LOWER(uc.last_name) LIKE %s
                        OR LOWER(uc.email_id) LIKE %s
                    )
                """

                like = f"%{key.lower()}%"
                cursor.execute(query, (userid, like, like, like))
                rows = [r[0] for r in cursor.fetchall()]

                if rows:
                    resolved_report["found"][key] = rows
                    final_contacts.update(rows)
                else:
                    resolved_report["not_found"].append(key)

            # =====================================================
            # 2️⃣ RESOLVE EXPLICIT EMAILS
            # =====================================================
            emails = need_contacts.get("emails", [])

            for email in emails:
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

            # =====================================================
            # 3️⃣ HANDLE GROUPS (NO SILENT DROP)
            # =====================================================
            # groups = need_contacts.get("groups", [])

            # for group in groups:
            #     key = group.strip()
            #     if not key:
            #         continue

            #     # Groups are acknowledged but unresolved for now
            #     resolved_report["not_found"].append(key)

        # =====================================================
        # 4️⃣ FINAL WORKFLOW MUTATION
        # =====================================================
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
        connection.close()
        # finally:
        #     current_user_id.reset(token)

        return jsonify(workflow_data)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@playbook_bp.route("/test-mid", methods=["POST"])
async def testmidcheck():
    from services.automate_service import AutoMateService

    body = request.get_json(force=True)

    # Lock user to avoid multiple parallel bulk sends
    user_id = body.get("user_id")

    user_input = body.get("userinput")
    length = "5 questions"
    tone = "professional"

    try:
        ai = AutoMateService(userid=user_id)
        val = await ai.generate_questions(
            user_input=user_input, length=length, tone=tone
        )
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


# @playbook_bp.route("/schedule-workflow-checker", methods=["POST"])
# def schedule_workflow_checker():
#     body = request.json or {}
#     print("body", body)

#     userid = body["user_id"]
#     filename = body["filename"]

#     deployment = body.get("deployment", {})
#     contacts = deployment.get("selectedContacts", [])
#     scheduled = deployment.get("scheduledActivation", {})

#     timezone = scheduled.get("timezone", "UTC")

#     if not filename.lower().endswith(".json"):
#         filename = f"{filename}.json"

#     wf_loc = f"{userid}/workflow/{base_name(filename=filename)}/{filename}"
#     workflow_json = read_json_from_s3(wf_loc)

#     if not workflow_json:
#         return jsonify({"status": "error", "message": "Workflow not found"}), 404

#     # --------------------------------------------------
#     # PREVIEW schedule (NO SIDE EFFECTS)
#     # --------------------------------------------------
#     try:
#         schedule_type, data = resolve_schedule_from_activation(scheduled)

#         # ---------- DAILY ----------
#         if schedule_type == "daily":
#             hour, minute = map(int, data["startTime"].split(":"))
#             scheduled_dt = SchedulerService.preview_next_daily_time(
#                 hour, minute, data["timezone"]
#             )

#         # ---------- WEEKLY ----------
#         elif schedule_type == "weekly":
#             hour, minute = map(int, data["startTime"].split(":"))
#             scheduled_dt = SchedulerService.preview_next_weekly_time(
#                 data["weekday"], hour, minute, data["timezone"]
#             )

#         # ---------- ONE-TIME ----------
#         elif schedule_type == "one_time":
#             scheduled_dt = data["datetime"]

#         # ---------- CUSTOM (NEW) ----------
#         elif schedule_type == "custom":
#             scheduled_dt = SchedulerService.preview_next_custom_time(
#                 start_date=data["startDate"],
#                 end_date=data["endDate"],
#                 start_time=data["startTime"],
#                 end_time=data["endTime"],
#                 timezone=data["timezone"],
#             )

#             if not scheduled_dt:
#                 return (
#                     jsonify(
#                         {
#                             "status": "error",
#                             "message": "No valid execution time in custom window",
#                         }
#                     ),
#                     400,
#                 )

#         else:
#             return jsonify({"error": "Unsupported schedule type"}), 400

#     except Exception as e:
#         return jsonify({"error": str(e)}), 400

#     # --------------------------------------------------
#     # Build LLM prompt (AI just needs context)
#     # --------------------------------------------------
#     prompt = PLAY_TEMPLATE["validate_schedule_workflow_problems"]

#     base_workflow = {
#         "workflow": workflow_json.get("workflow"),
#         "input_data": workflow_json.get("input_data"),
#     }
#     # print("base workfflow ", base_workflow)

#     full_prompt = (
#         prompt.replace("{workflow_json}", json.dumps(base_workflow))
#         .replace("{contacts}", json.dumps(contacts))
#         .replace("{schedule_time}", json.dumps(str(scheduled_dt)))
#     )
#     print("prompt", full_prompt)
#     # --------------------------------------------------
#     # LLM call
#     # --------------------------------------------------
#     llm_output = asyncio.run(
#         get_fireworks_response2(
#             user_message=full_prompt,
#             role="system",
#             temp=0.3,
#             user_id=userid,
#         )
#     )

#     llm_output = re.sub(r"^```(?:json)?|```$", "", llm_output).strip()

#     try:
#         response = json.loads(llm_output)
#         if not isinstance(response, list):
#             raise ValueError
#     except Exception:
#         return jsonify({"error": "Invalid LLM JSON", "raw": llm_output}), 500


#     return jsonify(response)
@playbook_bp.route("/schedule-workflow-checker", methods=["POST"])
def schedule_workflow_checker():
    body = request.json or {}
    print("body", body)

    userid = body["user_id"]
    filename = body["filename"]

    deployment = body.get("deployment", {})
    contacts = deployment.get("selectedContacts", [])
    scheduled = deployment.get("scheduledActivation", {})  # full user schedule object

    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"

    wf_loc = f"{userid}/workflow/{base_name(filename=filename)}/{filename}"
    workflow_json = read_json_from_s3(wf_loc)

    if not workflow_json:
        return jsonify({"status": "error", "message": "Workflow not found"}), 404

    # --------------------------------------------------
    # PREVIEW schedule → earliest trigger time (NO SIDE EFFECTS)
    # --------------------------------------------------
    try:
        schedule_type, data = resolve_schedule_from_activation(scheduled)

        if schedule_type == "daily":
            hour, minute = map(int, data["startTime"].split(":"))
            scheduled_dt = SchedulerService.preview_next_daily_time(
                hour, minute, data["timezone"]
            )
            schedule_time_json = json.dumps(str(scheduled_dt))

        elif schedule_type == "weekly":
            hour, minute = map(int, data["startTime"].split(":"))
            scheduled_dt = SchedulerService.preview_next_weekly_time(
                data["weekday"], hour, minute, data["timezone"]
            )
            schedule_time_json = json.dumps(str(scheduled_dt))

        # elif schedule_type == "one_time" or schedule_type == "once":
        #     # Safety: ensure one_time datetime is aware ISO string
        #     try:
        #         dt = datetime.fromisoformat(data["datetime"].replace("Z", "+00:00"))
        #         if dt.tzinfo is None:
        #             return (
        #                 jsonify({"error": "one_time schedule must include timezone"}),
        #                 400,
        #             )
        #         schedule_time_json = json.dumps(dt.isoformat())
        #     except Exception:
        #         return jsonify({"error": "Invalid datetime format for one_time"}), 400
        elif schedule_type in ("one_time", "once"):
            try:
                tz = pytz.timezone(data["timezone"])
                naive_dt = datetime.fromisoformat(data["datetime"])
                dt = tz.localize(naive_dt)

                schedule_time_json = json.dumps(dt.astimezone(pytz.UTC).isoformat())
            except Exception as e:
                return (
                    jsonify({"error": f"Invalid datetime for one_time: {str(e)}"}),
                    400,
                )

        elif schedule_type == "custom":
            earliest_dt = SchedulerService.preview_next_custom_time(
                start_date=data["startDate"],
                end_date=data["endDate"],
                start_time=data["startTime"],
                end_time=data["endTime"],
                timezone=data["timezone"],
            )

            if not earliest_dt:
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": "No valid execution time in custom window (e.g. window in past or invalid)",
                        }
                    ),
                    400,
                )

            schedule_time_json = json.dumps(
                earliest_dt.isoformat()
            )  # single ISO with offset

        else:
            return jsonify({"error": "Unsupported schedule type"}), 400

    except Exception as e:
        return jsonify({"error": f"Schedule resolution failed: {str(e)}"}), 400

    # --------------------------------------------------
    # Build LLM prompt with original user schedule for feasible fixes
    # --------------------------------------------------
    prompt = PLAY_TEMPLATE["validate_schedule_workflow_problems"]

    base_workflow = {
        "workflow": workflow_json.get("workflow"),
        "input_data": workflow_json.get("input_data"),
    }
    print("schedule time json", schedule_time_json)

    full_prompt = (
        prompt.replace("{workflow_json}", json.dumps(base_workflow))
        .replace("{contacts}", json.dumps(contacts))
        .replace("{schedule_time}", schedule_time_json)  # resolved earliest trigger
        .replace(
            "{original_scheduled}", json.dumps(scheduled)
        )  # full user schedule for fixes
    )

    # --------------------------------------------------
    # LLM call
    # --------------------------------------------------
    llm_output = asyncio.run(
        get_evaluator_fireworks(
            user_message=full_prompt,
            role="system",
            temp=0.3,
            user_id=userid,
        )
    )

    llm_output = re.sub(r"^```(?:json)?|```$", "", llm_output).strip()

    try:
        response = json.loads(llm_output)
        if not isinstance(response, list):
            raise ValueError("Response must be a list")
    except Exception as e:
        return (
            jsonify(
                {"error": "Invalid LLM JSON", "raw": llm_output, "parse_error": str(e)}
            ),
            500,
        )
    print("response for schedule", response)

    return jsonify(response)


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
    print("dadss", data)
    userid = data.get("user_id")
    answer = data.get("answer")
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
            service.answer_questions(answer=answer, qid=question_id, chid=chat_id)
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
    try:
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
                result = await runner.autocheckerworkflow()
                return jsonify({"message": result})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        # current_user_id.reset(token)
        print("checking auto check workflow")


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
    try:
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
            return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        # current_user_id.reset(token)
        print("updating auto check")
