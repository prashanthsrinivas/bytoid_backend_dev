import re
import uuid
from db.db_checkers import (
    check_subagent_by_playbook,
    create_subagent_to_playbook,
    get_subagent_by_userid,
)
from flask import Blueprint, request, jsonify
import json
from cust_helpers import pathconfig
from services.workflow_service import WorkflowRunnerV2
from utils.fireworkzz import get_fireworks_response
from .helperzz import *
from utils.pb_config_utils import *
from utils.normal import load_yaml_file
from .wf_runner import WorkflowRunner

playbook_bp = Blueprint("playbook", __name__)


@playbook_bp.route("/create_instruction", methods=["POST"])
def create_new_instruction():
    data = request.json
    userid = data["user_id"]
    print("data input", data)
    playbook_id, config_path, subagent_id = returnconfigandpath(userid)
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
    playbook_id, config_path, subagent_id = returnconfigandpath(userid)

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
    playbook["WorkflowDate"] = datetime.now().isoformat()
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
    playbook["WorkflowDate"] = datetime.now().isoformat()

    return save_playbook_to_s3(playbook, user_id, "Step deleted successfully", filename)


@playbook_bp.route("/modify_instruction", methods=["POST"])
def modify_instruction():
    try:
        body = request.json
        if not body:
            return jsonify({"status": "error", "message": "Empty request body"}), 400

        update_instruction = body.get("modify_instructions")
        additional_data = body.get("additional_data") or ""
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
        services_functions = read_function_jsons()
        full_prompt = (
            update_prompt_template.replace("{existing_workflow}", workflow_json_str)
            .replace("{update_instruction}", update_instruction)
            .replace("{services_section}", services_functions)
            .replace("{additional_data}", additional_data)
            .replace("{todays_date}", datetime.now().strftime("%A, %d %B %Y")),
        )

        # Call LLM
        llm_response = get_fireworks_response(full_prompt, role="system")

        try:
            cleaned_response = extract_json_from_llm_output(llm_response)

            # Now parse
            parsed_json = json.loads(cleaned_response)

            if (
                isinstance(parsed_json, dict)
                and "unrelated_instruction_message" in parsed_json
            ):
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": parsed_json["unrelated_instruction_message"],
                        }
                    ),
                    400,
                )

            if not parsed_json or "steps" not in parsed_json:
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": "Modified JSON is missing 'steps' section.",
                        }
                    ),
                    500,
                )

            # Sync top-level fields
            fields_to_sync = [
                "name",
                "description",
                "ai_mode",
                "trigger_mode",
                "trigger_input",
            ]
            for field in fields_to_sync:
                new_value = parsed_json.get(field)
                if new_value is not None:
                    original_json["workflow"][field] = new_value
                    if field in original_json["workflow"].get("input_data", {}):
                        original_json["input_data"][field] = new_value
                    elif field == "name":
                        original_json["input_data"]["title"] = new_value
                    elif field == "description":
                        original_json["input_data"]["description"] = new_value

            # Optional context section
            if "context_section" in parsed_json:
                original_json["context_section"] = parsed_json["context_section"]

            # Always update steps
            original_json["workflow"]["steps"] = parsed_json["steps"]
            original_json["WorkflowDate"] = datetime.now().isoformat()

            message = parsed_json.get(
                "modified_message", "Workflow updated successfully."
            )
            result = returnconfigandpath(user_id)
            if isinstance(result, tuple) and len(result) == 3:
                _, config_path, _ = result
            else:
                return result
            update_playbook_config(
                configpath=config_path,
                user_id=user_id,
                name=original_json["filename"],
                filepath=f"{user_id}/workflow/{filename}",
                title=original_json["workflow"]["name"],
                description=original_json["workflow"]["description"],
                num_steps=len(original_json["workflow"]["steps"]),
            )

            return save_playbook_to_s3(original_json, user_id, message, filename)

        except json.JSONDecodeError as jerr:
            print(f"⚠️ JSON parsing failed: {jerr}")
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Invalid JSON returned from LLM.",
                        "raw_output": llm_response,
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
                        "message": f"Unexpected error during JSON processing: {inner_e}",
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

        # ⛓ Get config path
        result = returnconfigandpath(user_id)
        if isinstance(result, tuple) and len(result) == 3:
            _, config_path, _ = result
        else:
            return result  # Early return if returnconfigandpath() returned an error response

        # Load prompt + workflow JSON
        yaml_data = load_yaml_file(path=pathconfig.play_template)
        workflow_json = read_json_from_s3(f"{user_id}/workflow/{filename}")

        if workflow_json and workflow_json.get("clarification_questions"):
            return (
                jsonify(
                    {
                        "status": "success",
                        "message": "clarifications already made",
                        "data": workflow_json,
                    }
                ),
                200,
            )

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

        workflow_json_str = json.dumps(workflow_json, indent=2)
        full_prompt = update_prompt_template.replace(
            "{workflow_json}", workflow_json_str
        )

        # 🔥 Get LLM output
        llm_output = get_fireworks_response(full_prompt, role="system")

        # 🧼 Extract valid JSON block (remove any ```json or ```yaml markdown)
        json_match = re.search(r"```(?:json)?\n([\s\S]+?)```", llm_output)
        if json_match:
            cleaned_output = json_match.group(1).strip()
        else:
            cleaned_output = llm_output.strip()

        # ✅ Parse JSON
        try:
            parsed_json = json.loads(cleaned_output)
        except json.JSONDecodeError as je:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": f"Failed to parse JSON: {str(je)}",
                        "raw_output": cleaned_output,
                    }
                ),
                500,
            )

        if (
            not isinstance(parsed_json, dict)
            or "clarification_questions" not in parsed_json
        ):
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "LLM output missing 'clarification_questions'",
                        "raw_output": cleaned_output,
                    }
                ),
                500,
            )

        # ✅ Store questions
        questions = parsed_json["clarification_questions"]
        workflow_json["clarifications_generated"] = True
        workflow_json["clarification_questions"] = questions

        # 🔄 Update clarification count in config
        update_playbook_clarifications(
            configpath=config_path,
            user_id=user_id,
            name=filename,
            clarifications_required=len(questions),
        )

        # 💾 Save updated workflow
        return save_playbook_to_s3(
            workflow_json, user_id, "clarifications added", filename
        )
        # clarifications=True,

    except Exception as e:
        print("⚠️ Error while generating workflow clarifications:", str(e))
        return jsonify({"status": "error", "message": str(e)}), 500


@playbook_bp.route("/workflow-clarifications/remove-question", methods=["POST"])
def remove_clarification_question():
    try:
        body = request.json
        user_id = body.get("user_id")
        filename = body.get("filename")
        quote = body.get("quote")  # Step title
        target_question = body.get("question")  # Exact question string to remove

        if not all([user_id, filename, quote, target_question]):
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Missing one or more required fields: user_id, filename, quote, question",
                    }
                ),
                400,
            )

        # 🔍 Load the workflow
        workflow_json = read_json_from_s3(f"{user_id}/workflow/{filename}")
        if not workflow_json:
            return (
                jsonify({"status": "error", "message": "Workflow file not found"}),
                404,
            )

        clarification_data = workflow_json.get("clarification_questions", [])
        updated_clarifications = []

        quote_found = False
        question_found = False

        # 🔄 Process each quote entry
        for entry in clarification_data:
            if entry.get("quote") == quote:
                quote_found = True
                updated_questions = [
                    q
                    for q in entry.get("questions", [])
                    if q.get("question") != target_question
                ]

                if len(updated_questions) < len(entry.get("questions", [])):
                    question_found = True

                if updated_questions:
                    updated_clarifications.append(
                        {"quote": quote, "questions": updated_questions}
                    )
                # else: this quote is removed entirely (0 questions left)
            else:
                updated_clarifications.append(entry)

        if not quote_found:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": f"Quote '{quote}' not found in clarification questions",
                    }
                ),
                404,
            )

        if not question_found:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": f"Question not found under quote '{quote}'",
                    }
                ),
                404,
            )

        # 💾 Save updated clarifications
        workflow_json["clarification_questions"] = updated_clarifications

        return save_playbook_to_s3(
            workflow_json,
            user_id,
            "clarification question removed",
            filename,
            clarifications=True,
        )

    except Exception as e:
        print("⚠️ Error while removing clarification question:", str(e))
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

        # Validate input
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

        # Load paths
        result = returnconfigandpath(user_id)
        if not isinstance(result, tuple) or len(result) != 3:
            return result  # Error returned from helper

        _, config_path, _ = result

        # Load validation prompt
        promptfile = load_yaml_file(path=pathconfig.play_template)
        validation_prompt = promptfile.get("evaluate_clarification_answer")

        if not validation_prompt:
            return (
                jsonify({"status": "error", "message": "Prompt template not found"}),
                500,
            )

        # Validate with LLM
        validated = answer_clarification_question_validate(
            validation_prompt, question_text, answer_text
        )

        if validated.get("status") != "yes":
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": validated.get(
                            "message", "Answer not valid for this question."
                        ),
                    }
                ),
                400,
            )

        corrected_answer = validated.get("corrected_answer", answer_text)

        # Load workflow
        workflow_json = read_json_from_s3(f"{user_id}/workflow/{filename}")
        clarifications = workflow_json.get("clarification_questions", [])
        clarification_answers = workflow_json.get("clarification_answers", [])

        found = False
        updated_clarifications = []

        for entry in clarifications:
            if entry.get("quote") == quote:
                remaining_questions = []
                for q in entry.get("questions", []):
                    if q.get("question") == question_text:
                        clarification_answers.append(
                            {
                                "quote": quote,
                                "question": question_text,
                                "answer": corrected_answer,
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

        # Update workflow with answers
        workflow_json["clarification_questions"] = updated_clarifications
        workflow_json["clarification_answers"] = clarification_answers

        # Update config count
        remaining_count = sum(
            len(e.get("questions", [])) for e in updated_clarifications
        )

        update_playbook_clarifications(
            configpath=config_path,
            user_id=user_id,
            name=filename,
            clarifications_required=remaining_count,
        )

        # Save updated workflow
        return save_playbook_to_s3(
            workflow_json,
            user_id,
            "clarifications added",
            filename,
            clarifications=True,
        )

    except Exception as e:
        print("⚠️ Error while updating clarification answer:", str(e))
        return jsonify({"status": "error", "message": str(e)}), 500


@playbook_bp.route("/workflow-aisuggest", methods=["POST"])
def workflow_ai_suggest():
    try:
        body = request.json
        user_id = body.get("user_id")
        category = body.get("quote")  # "quote" is the category
        question = body.get("question")
        filename = body.get("filename")

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

        # Load prompt template from YAML
        promptfile = load_yaml_file(path=pathconfig.play_template)
        workflow_json = read_json_from_s3(filepath=f"{user_id}/workflow/{filename}")
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
        llm_output = get_fireworks_response(prompt_input, role="system")
        ai_answer = llm_output.strip()

        # Clean markdown formatting if returned (optional safety)
        if ai_answer.startswith("```"):
            ai_answer = ai_answer.strip("` \n")

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


@playbook_bp.route("/clarifications-reset", methods=["POST"])
def workflow_clarifications_reset():
    try:
        data = request.json
        user_id = data.get("user_id")
        filename = data.get("filename")
        workflow_json = read_json_from_s3(f"{user_id}/workflow/{filename}")
        workflow_json["clarifications_generated"] = False
        if "clarification_questions" in workflow_json:
            workflow_json.pop("clarification_questions")
            return save_playbook_to_s3(
                workflow_json, user_id, "clarifications reset", filename
            )
        else:
            return {"message": "no clarifications present"}
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@playbook_bp.route("/run_workflow", methods=["POST"])
def runWorkflow():
    data = request.json
    userid = data.get("user_id")
    filename = data.get("filename")

    if not userid:
        return jsonify({"message": "Not a valid userid", "status": "error"}), 400
    if not filename:
        return jsonify({"message": "Not a valid filename", "status": "error"}), 400
    try:
        with WorkflowRunnerV2(userid=userid, filename=filename) as runner:
            bad = runner.execute()
            print(bad)
            result = runner.get_execution_log()
            print("dasdsad", result)
            return jsonify({"status": "success", "gmail_api_response": result})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


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

    try:
        with WorkflowRunnerV2(userid=userid, filename=filename) as runner:
            steps = runner.steps
            # step_id is likely a UUID string
            selected_step = steps[step_id]
            if not selected_step:
                return jsonify({"message": "Step not found", "status": "error"}), 404

            # Execute and capture the actual output
            step_result = runner._execute_step(selected_step)

            # Also include log if you want
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


@playbook_bp.route("/test-playground-step", methods=["POST"])
def testworkflowbyinput():
    data = request.json
    userid = data.get("user_id")
    userinput = data.get("userinput")
    filename = data.get("filename")
    print("test-playground-step", userinput)
    # Connect to DB
    with WorkflowRunnerV2(userid=userid, filename=filename) as service:
        return service.execute_from_text_input(user_input=userinput)
