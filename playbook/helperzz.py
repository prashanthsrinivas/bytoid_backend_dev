import json
import re
import yaml
from agent_route.doc_clarity import fetch_ques_with_docs
from utils.fireworkzz import evaluator_context_llama, get_fireworks_response
from utils.pb_config_utils import *
from utils.normal import ensure_dir, load_yaml_file
from datetime import datetime
import tempfile
import os
from flask import jsonify
from db.db_checkers import (
    check_subagent_by_playbook,
    get_subagent_by_userid,
)
from cust_helpers import pathconfig


def clean_yaml_block(text: str) -> str:
    """
    Extracts and cleans the first YAML block inside triple backticks.
    """
    # Match first triple-backtick block optionally prefixed by 'yaml'
    # match = re.search(r"```(?:yaml)?\s*\n(.*?)\n```", text, re.DOTALL)
    match = re.search(r"```(?:yaml|yml)?\s*\n(.*?)```", text, re.DOTALL)

    if match:
        return match.group(1).strip()

    # Fallback: if no fenced code block found, return original
    return text.strip()


def normalize_input(instruction_input):
    normalized = instruction_input.copy()

    # Convert 'trigger_input_list' to 'trigger_input'
    if "trigger_input_list" in normalized:
        normalized["trigger_input"] = normalized.pop("trigger_input_list")

    # Fix 'triggermode' typo to 'trigger_mode'
    if "triggermode" in normalized:
        normalized["trigger_mode"] = normalized.pop("triggermode")

    return normalized


def extract_questions(text):
    # Match only proper questions (ending with ? or starting with What/How/Are/etc.)
    lines = text.strip().split("\n")
    questions = []
    for line in lines:
        line = line.strip("•-– ").strip()
        if line.endswith("?") or re.match(r"^(What|How|Are|Do|Which|Can|Is)\b", line):
            questions.append(line)
    return questions


def check_doc_context_needed(instruction_input, templatedata):
    template = templatedata.get("checklanceneeded")

    if not template or not isinstance(template, str):
        raise ValueError("Missing or invalid template for 'checklanceneeded' in YAML.")

    try:
        flat_input = {
            "name": instruction_input.get("name"),
            "description": instruction_input.get("description"),
            "trigger_input": instruction_input.get("trigger_input")
            or instruction_input.get("trigger_input_list", []),
            "commuinicaton": instruction_input.get("communication_mode")
            or instruction_input.get("trigger_input_list", []),
        }
        full_prompt = template.format(**flat_input)
    except KeyError as e:
        raise ValueError(f"Template expects missing key: {e}")

    response = get_fireworks_response(full_prompt, role="system")

    try:
        result = json.loads(response)
        if isinstance(result, list):
            # ✅ Convert [{'question': '...'}] → ['...']
            if all(isinstance(q, dict) and "question" in q for q in result):
                return [q["question"] for q in result]
            elif all(isinstance(q, str) for q in result):
                return result
    except json.JSONDecodeError:
        pass

    # Fallback: extract questions from natural language
    return extract_questions(response)


def evallogic(templatedata, batch):
    valid_responses = []
    res_raw = evaluator_context_llama(
        templatedata.get("context_workflow_validator_batch"), batch
    )
    # Extract the JSON array block using regex
    if isinstance(res_raw, str):
        match = re.search(r"\[\s*{.*?}\s*\]", res_raw, re.DOTALL)
        if match:
            json_block = match.group(0)
            try:
                res_json = yaml.safe_load(json_block)
                print("✅ Extracted & parsed response block.")
            except Exception as e:
                print(f"❌ YAML parsing error: {e}")
                res_json = []
        else:
            print("❌ Could not extract JSON list from model output.")
            res_json = []
    elif isinstance(res_raw, list):
        # Already structured response
        res_json = res_raw
    else:
        print("❌ Unexpected type of response from evaluator.")
        res_json = []

    # Evaluate results
    for original_item, eval_result in zip(batch, res_json):
        actual_q = original_item["query"]
        related_res = eval_result.get("related", False)
        usecase_res = eval_result.get("has_usecase_details", False)
        filename = original_item.get("filename", "").strip()
        entry_obj = {
            "User": actual_q,
            "Ai Response": eval_result.get("explanation", ""),
        }

        if related_res and usecase_res:
            valid_responses.append(entry_obj)
    return valid_responses


def triggeraicontextfinder(instruction_input, userid, templatedata):
    normalized = normalize_input(instruction_input)
    ques = check_doc_context_needed(normalized, templatedata)
    if len(ques) > 0:
        content = fetch_ques_with_docs(ques, userid)
        batch_size = 10
        valid_responses = []

        print(len(content), "context length")

        for i in range(0, len(content), batch_size):
            print("loop index for context", i)
            batch = content[i : i + batch_size]
            responses = evallogic(templatedata, batch)
            if len(responses) == 0:
                print("⚠️ First evaluation attempt failed. Retrying...")
                responses = evallogic(templatedata, batch)
            valid_responses.extend(responses)

            print(len(valid_responses))
        return valid_responses


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
        frequency = schedule.get("frequency", "daily").lower()
        start_time = schedule.get("startTime", "09:00")
        end_time = schedule.get("endTime")  # optional

        # Always add frequency and start time
        trigger_input_list.append(f"schedule: {frequency}")
        trigger_input_list.append(f"in_time: {start_time}")

        # Add end time only if present
        if end_time:
            trigger_input_list.append(f"out_time: {end_time}")

        # Add frequency-specific values
        if frequency == "weekly":
            weekly_day = schedule.get("weeklyDay")
            if weekly_day:
                trigger_input_list.append(f"day: {weekly_day}")

        elif frequency == "monthly":
            monthly_date = schedule.get("monthlyDate")
            if monthly_date is not None:
                trigger_input_list.append(f"date: {monthly_date}")

        elif frequency == "custom":
            start_date = schedule.get("startDate")
            end_date = schedule.get("endDate")  # optional
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
        "clarifications_generated": False,
    }
    # full_output = convert_dates(full_outputs)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(full_output, f, indent=2)
    res = upload_any_file(
        file_path=filepath, user_id=userid, file_name=filename, type="workflow"
    )
    os.remove(filepath)

    return full_output, res["s3_key"]


def returnconfigandpath(userid):
    if not userid:
        return jsonify({"error": "userid is required"}), 400
    subagent_id = get_subagent_by_userid(userid)
    if not subagent_id:
        return jsonify({"error": "no agent found"}), 400
    playbook_id, config_path = check_subagent_by_playbook(subagent_id)
    return playbook_id, config_path, subagent_id


def save_playbook_to_s3(
    playbook, user_id, success_message, filname, clarifications=None
):
    with tempfile.NamedTemporaryFile(
        delete=False, suffix=".json", mode="w"
    ) as tmp_file:
        json.dump(playbook, tmp_file, indent=2)
        temp_file_path = tmp_file.name

    upload_any_file(
        file_path=temp_file_path, type="workflow", user_id=user_id, file_name=filname
    )
    os.remove(temp_file_path)
    if clarifications:
        return (
            jsonify(
                {
                    "status": "success",
                    "message": success_message,
                    "data": playbook["clarification_questions"],
                }
            ),
            200,
        )
    else:
        return (
            jsonify(
                {"status": "success", "message": success_message, "data": playbook}
            ),
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


def extract_json_from_llm_output(text):
    text = text.strip()
    if text.startswith("```json") and text.endswith("```"):
        return text[7:-3].strip()  # remove ```json and ```
    if text.startswith("```") and text.endswith("```"):
        return text[3:-3].strip()  # remove generic ```
    return text


def answer_clarification_question_validate(prompt_template, question, answer):
    try:
        # Format the prompt with the question and answer
        prompt_input = prompt_template.format(
            question=question.strip(), answer=answer.strip()
        )

        # Get LLM response
        llm_output = get_fireworks_response(prompt_input, role="system")

        # Clean and parse JSON
        cleaned = llm_output.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned.replace("```json", "").replace("```", "").strip()

        validated = json.loads(cleaned)

        # Ensure required keys exist
        if not isinstance(validated, dict) or "status" not in validated:
            return {"status": "no", "message": "Invalid response format from LLM."}

        return validated

    except Exception as e:
        return {"status": "no", "message": f"Failed to parse LLM output: {e}"}
