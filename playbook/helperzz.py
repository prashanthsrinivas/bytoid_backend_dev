import json
import re
import yaml
from agent_route.doc_clarity import fetch_ques_with_docs
from agent_route.routes import getFilenameData
from utils.chatopenzz import get_evaluator_gpt4
from utils.fireworkzz import (
    evaluator_context_llama,
    get_evaluator_fireworks,
    get_fireworks_response,
)
from utils.pb_config_utils import *
from utils.normal import ensure_dir, load_yaml_file, read_function_jsons
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
        filenames = getFilenameData(userid)
        content = fetch_ques_with_docs(ques, userid, filenames)
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
    """
    Prepares instruction input for the AI template safely.
    """
    name = data.get("title", "")
    description = data.get("description", "")
    triggermode = data.get("trigger_mode", "")
    ai_mode = data.get("ai_mode", "normal")
    inp_steps = data.get("steps", [])

    # Communication channels
    communication_channels = list(
        set(
            [ch for ch in data.get("communication_channels", []) if isinstance(ch, str)]
        )
    )

    # Trigger input
    trigger_input_list = []
    if triggermode.lower() == "scheduled":
        schedule = data.get("scheduled_options", {})
        freq = schedule.get("frequency", "daily")
        start = schedule.get("startTime", "09:00")
        trigger_input_list.append(f"schedule: {freq}")
        trigger_input_list.append(f"in_time: {start}")
        end_time = schedule.get("endTime")
        if end_time:
            trigger_input_list.append(f"out_time: {end_time}")
    else:
        raw_input = data.get("trigger_input", "")
        if raw_input:
            trigger_input_list.append(raw_input)

    instruction_input = {
        "name": name,
        "description": description,
        "trigger_mode": triggermode,
        "trigger_input": trigger_input_list,
        "communication_mode": communication_channels,
        "ai_mode": ai_mode,
        "inp_steps": inp_steps,
        "context_section": "[]",  # to be updated
        "services_section": "",  # to be updated
        "additional_data": data.get("additional_data", ""),
        "todays_date": datetime.now().strftime(
            "%A, %d %B %Y"
        ),  # e.g., Monday, 21 October 2025
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


class SafeDict(dict):
    def __missing__(self, key):
        return f"<missing {key}>"


def create_playbook(data, filename=None):
    """
    Creates a workflow JSON by sending a formatted prompt to the AI safely.
    """
    userid = data["user_id"]
    instruction_input = returninsructdata(data)

    # Build context section
    template_data = load_yaml_file(path=pathconfig.play_template)
    ques = triggeraicontextfinder(instruction_input, userid, template_data)
    if ques:
        context_items = [
            f"- {item.get('Ai Response','').strip()}"
            for item in ques
            if item.get("Ai Response")
        ]
        instruction_input["context_section"] = yaml.dump(
            context_items, default_flow_style=False
        )
    else:
        instruction_input["context_section"] = "[]"

    # Attach dynamic services section
    instruction_input["services_section"] = read_function_jsons()

    # Load template
    template = template_data.get("create_instruction")
    if not template:
        raise ValueError("Missing 'create_instruction' template in YAML file.")

        # --- Build full prompt safely via f-string to avoid .format issues ---
        #     full_prompt = f"""
        # You are an **AI Workflow Generation Assistant**.

        # ## INPUT FIELDS
        # name: {instruction_input['name']}
        # description: {instruction_input['description']}
        # trigger_mode: {instruction_input['trigger_mode']}
        # trigger_input:
        # {instruction_input['trigger_input']}
        # communication_mode:
        # {instruction_input['communication_mode']}
        # ai_mode: {instruction_input['ai_mode']}
        # context_section:
        # {instruction_input['context_section']}
        # additional_data:
        # {instruction_input['additional_data']}

        # """
    full_prompt = template.format(**instruction_input)

    # print("full_prompt", full_prompt)

    # Send prompt to AI
    # raw_response = get_fireworks_response(full_prompt, role="system")
    raw_response = get_evaluator_fireworks(full_prompt, role="system")
    # raw_response = get_evaluator_gpt4(full_prompt)
    # print("raw response", raw_response)

    # Clean AI response
    cleaned_j = clean_json_block(raw_response)
    try:
        response_dict = json.loads(cleaned_j)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON from AI:\n{raw_response}\nError: {e}")

    # Save workflow JSON
    filename = filename or f"{uuid.uuid4().hex[:8]}.json"
    ensure_dir(f"{pathconfig.basepath}/test/")
    filepath = os.path.join(f"{pathconfig.basepath}/test/", filename)
    print(response_dict)
    full_output = {
        "filename": filename,
        "input_data": data,  # original data from frontend
        "workflow": response_dict,  # AI-generated YAML parsed into dict
        "clarifications_generated": False,
        "WorkflowDate": datetime.now().isoformat(),
    }
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


def generate_meeting_email_body(details: dict, field_data: dict) -> str:
    summary = details.get("summary", "")
    start_time = details.get("start_time", "")
    end_time = details.get("end_time", "")
    timezone = details.get("timezone", "")
    hangout_link = details.get("hangoutLink", "Link not available")

    first_name = field_data.get("first_name", "there")
    last_name = field_data.get("last_name", "")
    business_name = field_data.get("BusinessName", "")
    line_of_business = field_data.get("LineOfBusiness", "")
    billing_address = field_data.get("BillingAddress", "")
    business_email = field_data.get("BusinessEmail", "")
    website = field_data.get("WebsiteUrl", "")
    social_links = field_data.get("sociallinks", "")

    return f"""
<div style="max-width:600px;margin:auto;padding:24px;border:1px solid #e5e7eb;border-radius:12px;font-family:sans-serif;color:#1f2937;">
  <p style="font-size:16px;margin-bottom:16px;">Hi <strong>{first_name}</strong>,</p>
  <p style="margin-bottom:24px;">A meeting has been scheduled for you. Below are the details:</p>

  <div style="margin-bottom:24px;">
    <h2 style="color:#1d4ed8;font-size:18px;font-weight:600;margin-bottom:8px;">📌 Meeting Details</h2>
    <ul style="padding-left:20px;">
      {"<li><strong>Summary:</strong> " + summary + "</li>" if summary else ""}
      {"<li><strong>Date & Time:</strong> " + start_time + " to " + end_time + " (" + timezone + ")</li>" if start_time and end_time else ""}
      <li><strong>Join Link:</strong> <a href="{hangout_link}" style="color:#2563eb;text-decoration:underline;">{hangout_link}</a></li>
    </ul>
  </div>

  {"<div style='margin-bottom:24px;'><h2 style='color:#047857;font-size:18px;font-weight:600;margin-bottom:8px;'>🧾 Business Info</h2><ul style='padding-left:20px;'>" if business_name or line_of_business else ""}
    {f"<li><strong>Business Name:</strong> {business_name}</li>" if business_name else ""}
    {f"<li><strong>Line of Business:</strong> {line_of_business}</li>" if line_of_business else ""}
  {"</ul></div>" if business_name or line_of_business else ""}

  {"<div style='margin-bottom:24px;'><h2 style='color:#7c3aed;font-size:18px;font-weight:600;margin-bottom:8px;'>📍 Contact Info</h2><ul style='padding-left:20px;'>" if billing_address or business_email or website else ""}
    {f"<li><strong>Billing Address:</strong> {billing_address}</li>" if billing_address else ""}
    {f"<li><strong>Business Email:</strong> {business_email}</li>" if business_email else ""}
    {f"<li><strong>Website:</strong> <a href='{website}' style='color:#2563eb;text-decoration:underline;'>{website}</a></li>" if website else ""}
  {"</ul></div>" if billing_address or business_email or website else ""}

  {f"<div style='margin-bottom:24px;'><h2 style='color:#be185d;font-size:18px;font-weight:600;margin-bottom:8px;'>🌐 Social Links</h2><p>{social_links}</p></div>" if social_links else ""}

  <p style="margin-top:32px;font-size:14px;color:#6b7280;">Regards,<br><strong>Your Team</strong></p>
</div>
"""
