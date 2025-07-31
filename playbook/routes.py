import re
import uuid
from db.db_checkers import (
    check_subagent_by_playbook,
    create_subagent_to_playbook,
    get_subagent_by_userid,
)
from flask import Blueprint, request, jsonify
import yaml
import json
import tempfile
import os
from cust_helpers import pathconfig
from utils.fireworkzz import get_fireworks_response
from .helperzz import clean_yaml_block, triggeraicontextfinder
from utils.pb_config_utils import *
from utils.normal import ensure_dir, load_yaml_file

playbook_bp = Blueprint("playbook", __name__)


def returninsructdata(data):
    name = data.get("title")
    description = data.get("description")
    triggermode = data.get("trigger_mode")
    ai_mode = data.get("ai_mode", "normal")
    priority = data.get("priority", "normal")

    # Clean communication_channels and tags (remove numeric keys like 0, 1)
    communication_channels = list(
        set(
            [ch for ch in data.get("communication_channels", []) if isinstance(ch, str)]
        )
    )

    # Build trigger_input depending on trigger_mode
    trigger_input_list = []

    if triggermode.lower() == "scheduled":
        schedule = data.get("scheduled_options", {})
        frequency = schedule.get("frequency", "daily")
        start_time = schedule.get("startTime", "09:00")
        end_time = schedule.get("endTime", "17:00")

        trigger_input_list.extend(
            [
                f"schedule: {frequency}",
                f"in_time: {start_time}",
                f"out_time: {end_time}",
            ]
        )

        if frequency == "custom":
            start_date = schedule.get("startDate", "")
            end_date = schedule.get("endDate", "")
            if start_date:
                trigger_input_list.append(f"start_date: {start_date}")
            if end_date:
                trigger_input_list.append(f"end_date: {end_date}")
    else:
        raw_input = data.get("trigger_input", "")
        if raw_input:
            trigger_input_list.append(raw_input)

    # Construct instruction_input for template formatting
    instruction_input = {
        "name": name,
        "description": description,
        "trigger_mode": triggermode,
        "trigger_input": yaml.dump(trigger_input_list).strip(),
        "communication_mode": (
            communication_channels[0] if communication_channels else "auto"
        ),
        "ai_mode": ai_mode,
    }
    return instruction_input


from datetime import datetime


def convert_dates(obj):
    if isinstance(obj, dict):
        return {k: convert_dates(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert_dates(item) for item in obj]
    elif isinstance(obj, datetime.date):
        return obj.isoformat()
    return obj


def clean_json_block(raw: str) -> str:
    """
    Extracts the first valid JSON code block from a model response.
    """
    match = re.search(r"```json(.*?)```", raw, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return raw.strip()


def create_playbook(data, filename=None):
    # Extract core fields
    userid = data["user_id"]
    instruction_input = returninsructdata(data)
    template_data = load_yaml_file(path=pathconfig.play_template)
    ques = triggeraicontextfinder(instruction_input, userid, template_data)
    if ques and len(ques) > 0:
        context_items = [
            f"- {item.get('Ai Response', '').strip()}"
            for item in ques
            if item.get("Ai Response")
        ]
        context_block = "context_section:\n" + "\n".join(context_items)
    else:
        context_block = "context_section: []"

    instruction_input["context_section"] = context_block
    template = template_data.get("create_instruction")

    if not template or not isinstance(template, str):
        raise ValueError(
            "Missing or invalid 'create_instruction' template in YAML file."
        )

    try:
        full_prompt = template.format(**instruction_input)
    except KeyError as e:
        raise ValueError(f"Missing placeholder in instruction input: {e}")

    # Send prompt to LLM (Fireworks or any API)
    raw_response = get_fireworks_response(full_prompt, role="system")

    # Clean output YAML (strip markdown)
    cleaned_j = clean_json_block(raw_response)

    try:
        # Parse JSON directly from model output
        response_dict = json.loads(cleaned_j)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON from model output:\n{raw_response}\nError: {e}")

    # Save result to JSON file
    filename = filename or f"{uuid.uuid4().hex[:8]}.json"
    ensure_dir(f"{pathconfig.basepath}/test/")
    filepath = os.path.join(f"{pathconfig.basepath}/test/", filename)

    full_output = {
        "filename": filename,
        "input_data": data,  # original data from frontend
        "workflow": response_dict,  # AI-generated YAML parsed into dict
    }
    # full_output = convert_dates(full_outputs)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(full_output, f, indent=2)
    res = upload_any_file(
        file_path=filepath, user_id=userid, file_name=filename, type="workflow"
    )
    os.remove(filepath)

    return full_output, res["s3_key"]


@playbook_bp.route("/create_instruction", methods=["POST"])
def create_new_instruction():
    data = request.json
    userid = data["user_id"]
    if not userid:
        return jsonify({"error": "userid is required"}), 400
    subagent_id = get_subagent_by_userid(userid)
    if not subagent_id:
        return jsonify({"error": "no agent found"}), 400
    playbook_id, config_path = None, None
    playbook_id, config_path = check_subagent_by_playbook(subagent_id)
    if not playbook_id:
        config_s3_path = create_empty_playbook_config(userid)
        print("created new empty playbook")

        playb_id = str(uuid.uuid4())

        playbook_id, config_path = create_subagent_to_playbook(
            playb_id, subagent_id, config_s3_path
        )
    # config_path = "107642411636394027005/workflow/config_playbook_0195b8dd.json"
    print("found the play", config_path)
    full_output, npath = create_playbook(data)
    print("made the new instruction", npath)
    update_playbook_config(
        configpath=config_path,
        user_id=userid,
        name=full_output["filename"],
        filepath=npath,
        title=full_output["workflow"]["name"],
        description=full_output["workflow"]["description"],
        num_steps=len(full_output["workflow"]["steps"]),
    )

    return jsonify({"status": "success", "data": full_output})


@playbook_bp.route("/update_instruction", methods=["POST"])
def updateInstruction():
    data = request.json
    userid = data["user_id"]
    filename = data["filename"]
    if not userid:
        return jsonify({"error": "userid is required"}), 400
    subagent_id = get_subagent_by_userid(userid)
    if not subagent_id:
        return jsonify({"error": "no agent found"}), 400
    playbook_id, config_path = None, None
    playbook_id, config_path = check_subagent_by_playbook(subagent_id)
    full_output, npath = create_playbook(data, filename)
    print("made the new instruction", npath)
    update_playbook_config(
        configpath=config_path,
        user_id=userid,
        name=full_output["filename"],
        filepath=npath,
        title=full_output["workflow"]["name"],
        description=full_output["workflow"]["description"],
        num_steps=len(full_output["workflow"]["steps"]),
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

    s3_key = f"{user_id}/workflow/{filename}"

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


def save_playbook_to_s3(playbook, user_id, success_message, filname):
    with tempfile.NamedTemporaryFile(
        delete=False, suffix=".json", mode="w"
    ) as tmp_file:
        json.dump(playbook, tmp_file, indent=2)
        temp_file_path = tmp_file.name

    upload_any_file(
        file_path=temp_file_path, type="workflow", user_id=user_id, file_name=filname
    )
    os.remove(temp_file_path)

    return (
        jsonify({"status": "success", "message": success_message, "data": playbook}),
        200,
    )


def format_step_data(stepdata: dict) -> dict:
    """Return a cleaned step definition with only populated fields."""
    out = {}

    # always worth keeping if present
    for src, tgt in [
        ("id", "id"),
        ("title", "title"),
        ("stepType", "type"),
        ("objective", "objective"),
    ]:
        val = stepdata.get(src)
        if val:
            out[tgt] = val

    # Always include decision_point (true or false)
    out["decision_point"] = bool(stepdata.get("isDecisionPoint", False))

    # Only include decision_type if applicable
    if stepdata.get("isDecisionPoint") and stepdata.get("decisionType"):
        out["decision_type"] = stepdata["decisionType"]

    # lists / strings that might be empty
    if stepdata.get("conditions"):
        out["condition"] = stepdata["conditions"]

    if stepdata.get("nextStepIds"):
        out["next_step"] = stepdata["nextStepIds"]

    if stepdata.get("instructions"):
        out["ai_instructions"] = stepdata["instructions"]

    # communication extras
    if stepdata.get("stepType") == "communication":
        if stepdata.get("communicationMode"):
            out["communication_mode"] = stepdata["communicationMode"]
        if stepdata.get("selectedIntegrations"):
            out["channels"] = stepdata["selectedIntegrations"]
        if stepdata.get("calendar_type"):
            out["calendar_type"] = stepdata["calendar_type"]

    # navigation extras
    if stepdata.get("stepType") == "navigation" and stepdata.get("pageUrl"):
        out["page_url"] = stepdata["pageUrl"]

    return out


# @playbook_bp.route("/add_a_step", methods=["POST"])
# def add_a_step():
#     body = request.json
#     step_data = body.get("stepdata")
#     user_id = body.get("user_id")
#     filename = body.get("filename")
#     previous_step_id = (
#         step_data.get("parentStepId") if "parentStepId" in step_data else None
#     )  # optional

#     if not step_data or not user_id:
#         return jsonify({"status": "error", "message": "Missing step or user_id"}), 400

#     playbook = read_json_from_s3(f"{user_id}/workflow/{filename}")
#     workflow = playbook.setdefault("workflow", {})
#     steps = workflow.setdefault("steps", [])

#     # Check for duplicate title
#     step_title = step_data.get("title", "").strip().lower()
#     for s in steps:
#         if s.get("title", "").strip().lower() == step_title:
#             return (
#                 jsonify(
#                     {
#                         "status": "error",
#                         "message": "Step with this title already exists",
#                     }
#                 ),
#                 409,
#             )

#     # Assign UUID if not already present
#     new_step_id = str(uuid.uuid4())
#     step_data["id"] = step_data.get("id", new_step_id)

#     # Add to previous step's next_step if applicable
#     if previous_step_id:
#         for step in steps:
#             if step.get("id") == previous_step_id:
#                 if step.get("decision_point", False):
#                     step.setdefault("next_step", [])
#                     if isinstance(step["next_step"], list):
#                         step["next_step"].append(step_data["id"])
#                 else:
#                     step["next_step"] = step_data["id"]
#                 break
#         else:
#             return (
#                 jsonify({"status": "error", "message": "Previous step ID not found"}),
#                 404,
#             )

#     # Append new step
#     steps.append(step_data)
#     playbook["workflow"]["steps"] = steps


#     return save_playbook_to_s3(playbook, user_id, "Step added successfully", filename)
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

    playbook = read_json_from_s3(f"{user_id}/workflow/{filename}")
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
    # print(body)
    step_data = format_step_data(step_data)

    playbook = read_json_from_s3(f"{user_id}/workflow/{filename}")
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
    return save_playbook_to_s3(playbook, user_id, "Step edited successfully", filename)


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

    playbook = read_json_from_s3(f"{user_id}/workflow/{filename}")
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

    return save_playbook_to_s3(playbook, user_id, "Step deleted successfully", filename)


@playbook_bp.route("/reeval")
def reevalchecker():
    from utils.chatopenzz import reEvaluateinstructionJson

    val = reEvaluateinstructionJson()
    return val


def is_inappropriate(instruction: str) -> bool:
    foul_words = [
        "damn",
        "stupid",
        "f***",
        "shit",
        "useless",
        "dumb",
        "crap",
        "loser",
        "garbage",
        "wtf",
        "nonsense",
        "monkey",
        "banana",
        "asdf",
    ]
    # Combine into regex pattern with word boundaries
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(word) for word in foul_words) + r")\b",
        re.IGNORECASE,
    )

    if pattern.search(instruction):
        return True

    # Check if it's random gibberish
    if len(instruction.split()) <= 3 and not any(
        char.isalpha() for char in instruction
    ):
        return True

    return False


@playbook_bp.route("/modify_instruction", methods=["POST"])
def modify_instruction():
    try:
        body = request.json
        if not body:
            return jsonify({"status": "error", "message": "Empty request body"}), 400

        update_instruction = body.get("modify_instructions")
        user_id = body.get("user_id")
        filename = body.get("filename")

        if not update_instruction or not user_id or not filename:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Missing required fields: 'modify_instructions', 'user_id', or 'filename'.",
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

        # Load YAML template
        yaml_data = load_yaml_file(path=pathconfig.play_template)
        if not yaml_data:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "modify_instruction YAML file not found.",
                    }
                ),
                500,
            )

        update_prompt_template = yaml_data.get("modify_instruction")
        if not update_prompt_template:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "modify_instruction key missing in YAML template.",
                    }
                ),
                500,
            )

        # Load original workflow
        original_json = read_json_from_s3(f"{user_id}/workflow/{filename}")
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
        full_prompt = update_prompt_template.replace(
            "{existing_workflow}", workflow_json_str
        ).replace("{update_instruction}", update_instruction)

        # Call LLM
        modified_yaml = get_fireworks_response(full_prompt, role="system")
        # 1. Clean markdown code block if present
        if modified_yaml.strip().startswith("```"):
            # remove triple backticks and optional language hints (e.g., ```yaml or ```python)
            lines = modified_yaml.strip().splitlines()
            lines = [line for line in lines if not line.strip().startswith("```")]
            modified_yaml = "\n".join(lines)

        try:
            parsed_yaml = yaml.safe_load(modified_yaml)

            # Detect unrelated instruction with reasons
            if (
                isinstance(parsed_yaml, dict)
                and "unrelated_instruction_message" in parsed_yaml
            ):
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": parsed_yaml["unrelated_instruction_message"],
                        }
                    ),
                    400,
                )

            # Validate structure
            if not parsed_yaml or "steps" not in parsed_yaml:
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": "Modified YAML is missing 'steps' section.",
                        }
                    ),
                    500,
                )

            # Update dynamic fields
            fields_to_sync = [
                "name",
                "description",
                "ai_mode",
                "trigger_mode",
                "trigger_input",
            ]
            for field in fields_to_sync:
                new_value = parsed_yaml.get(field)
                if new_value is not None:
                    original_json["workflow"][field] = new_value
                    if field in original_json["workflow"].get("input_data", {}):
                        original_json["input_data"][field] = new_value
                    elif field == "name":  # compatibility fallback
                        original_json["input_data"]["title"] = new_value
                    elif field == "description":
                        original_json["input_data"]["description"] = new_value

            # Optional sections
            # optional_sections = ["context_section", "clarification_questions"]
            # for section in optional_sections:
            #     if section in parsed_yaml:
            #         original_json[section] = parsed_yaml[section]
            section = "context_section"
            if section in parsed_yaml:
                original_json[section] = parsed_yaml[section]

            # Always update steps
            original_json["workflow"]["steps"] = parsed_yaml["steps"]

            # Determine message
            message = parsed_yaml.get(
                "modified_message", "Workflow updated successfully."
            )

            return save_playbook_to_s3(
                original_json,
                user_id,
                message,
                filename,
            )

        except yaml.YAMLError as yerr:
            print(f"⚠️ YAML parsing failed: {yerr}")
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Invalid YAML returned from LLM.",
                        "raw_output": modified_yaml,
                    }
                ),
                500,
            )

        except Exception as inner_e:
            print(f"⚠️ Unexpected error during parsing: {inner_e}")
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": f"Unexpected error during YAML processing: {inner_e}",
                    }
                ),
                500,
            )

    except Exception as outer_e:
        print(f"❌ modify_instruction fatal error: {outer_e}")
        return (
            jsonify(
                {"status": "error", "message": f"Internal server error: {outer_e}"}
            ),
            500,
        )


@playbook_bp.route("/workflow-clarifications", methods=["POST"])
def generate_clarification_questions():
    try:
        body = request.json
        user_id = body.get("user_id")
        filename = body.get("filename")

        if not user_id or not filename:
            return (
                jsonify({"status": "error", "message": "Missing user_id or filename"}),
                400,
            )

        # Load prompt + workflow JSON
        yaml_data = load_yaml_file(path=pathconfig.play_template)
        workflow_json = read_json_from_s3(f"{user_id}/workflow/{filename}")

        update_prompt_template = yaml_data.get(
            "generate_workflow_clarification_questions"
        )
        if not update_prompt_template:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Prompt 'generate_workflow_clarification_questions' not found",
                    }
                ),
                500,
            )

        # Inject workflow into the prompt
        workflow_json_str = json.dumps(workflow_json, indent=2)
        full_prompt = update_prompt_template.replace(
            "{workflow_json}", workflow_json_str
        )

        # Get model output
        modified_yaml = get_fireworks_response(full_prompt,role="system")
        cleaned_output = re.sub(
            r"```(?:yaml|json)?\n([\s\S]+?)```", r"\1", modified_yaml
        ).strip()
        parsed_yaml = yaml.safe_load(cleaned_output)

        if (
            not isinstance(parsed_yaml, dict)
            or "clarification_questions" not in parsed_yaml
        ):
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "LLM output missing 'clarification_questions'",
                    }
                ),
                500,
            )

        questions = parsed_yaml["clarification_questions"]

        # Validate structure
        for entry in questions:
            if "quote" not in entry or "questions" not in entry:
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": "Each item must contain 'quote' and 'questions'",
                        }
                    ),
                    500,
                )
            for q in entry["questions"]:
                if "question" not in q or "answer" not in q:
                    return (
                        jsonify(
                            {
                                "status": "error",
                                "message": "Each question must have 'question' and 'answer'",
                            }
                        ),
                        500,
                    )

        # Append to workflow JSON
        workflow_json["clarification_questions"] = questions

        # Save back to S3
        return save_playbook_to_s3(
            workflow_json, user_id, "clarifications added", filename
        )

    except Exception as e:
        print("⚠️ Error while generating workflow suggestions:", str(e))
        return jsonify({"status": "error", "message": str(e)}), 500


@playbook_bp.route("/workflow-clarifications-answer", methods=["POST"])
def answer_clarification_question():
    try:
        body = request.json
        user_id = body.get("user_id")
        filename = body.get("filename")
        quote = body.get("quote")
        question_text = body.get("question")
        answer_text = body.get("answer")

        if not all([user_id, filename, quote, question_text, answer_text]):
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Missing one or more required fields",
                    }
                ),
                400,
            )

        # Load the workflow from S3
        workflow_json = read_json_from_s3(f"{user_id}/workflow/{filename}")

        # Pull existing clarifications and answers
        clarifications = workflow_json.get("clarification_questions", [])
        clarification_answers = workflow_json.get("clarification_answers", [])

        found = False

        # Iterate and remove the question from clarification_questions
        updated_clarifications = []
        for entry in clarifications:
            if entry.get("quote") == quote:
                remaining_questions = []
                for q in entry.get("questions", []):
                    if q.get("question") == question_text:
                        # Add to answered list
                        clarification_answers.append(
                            {
                                "quote": quote,
                                "question": question_text,
                                "answer": answer_text,
                            }
                        )
                        found = True
                    else:
                        remaining_questions.append(q)
                if remaining_questions:
                    updated_clarifications.append(
                        {"quote": quote, "questions": remaining_questions}
                    )
            else:
                updated_clarifications.append(entry)

        if not found:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Matching quote/question not found in clarification_questions.",
                    }
                ),
                404,
            )

        # Update workflow JSON
        workflow_json["clarification_questions"] = updated_clarifications
        workflow_json["clarification_answers"] = clarification_answers

        return save_playbook_to_s3(
            workflow_json,
            user_id=user_id,
            success_message="Clarification answer added successfully",
            filname=filename,
        )

    except Exception as e:
        print("⚠️ Error while updating clarification answer:", str(e))
        return jsonify({"status": "error", "message": str(e)}), 500
