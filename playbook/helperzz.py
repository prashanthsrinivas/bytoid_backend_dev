import asyncio
import json
import re
import yaml
from agent_route.doc_clarity import fetch_ques_with_docs
from agent_route.routes import getFilenameData
from services.meet_service import GoogleMeetService
from services.microsoft_calender_service import MicrosoftGraphCalendarService
from utils.async_check import run_async
from utils.fireworkzz import (
    evaluator_context_llama,
    get_evaluator_fireworks,
    get_fireworks_response2,
)
from utils.pb_config_utils import *
from utils.normal import (
    ensure_dir,
    load_yaml_file,
    read_function_jsons,
    read_function_jsons2,
)
from datetime import datetime
import tempfile
import os
from flask import jsonify
from db.db_checkers import (
    check_subagent_by_playbook,
    fetch_user_Social,
    get_subagent_by_userid,
)
from cust_helpers import pathconfig
from request_context import current_user_id


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


async def check_doc_context_needed(instruction_input, templatedata, userid):
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

    # response = get_fireworks_response(full_prompt, role="system")
    response = await get_fireworks_response2(
        user_message=full_prompt, role="system", temp=0.4, user_id=userid
    )

    # print("len of respinse", len(response), response)
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


async def evallogic(templatedata, batch, userid):
    valid_responses = []
    res_raw = await evaluator_context_llama(
        templatedata.get("context_workflow_validator_batch"), batch, userid=userid
    )
    # Extract the JSON array block using regex
    if isinstance(res_raw, str):
        match = re.search(r"\[\s*{.*?}\s*\]", res_raw, re.DOTALL)
        if match:
            json_block = match.group(0)
            try:
                res_json = yaml.safe_load(json_block)
            # print("✅ Extracted & parsed response block.")
            except Exception as e:
                print(f"❌ YAML parsing error: {e}")
                res_json = []
        else:
            # print("❌ Could not extract JSON list from model output.")
            res_json = []
    elif isinstance(res_raw, list):
        # Already structured response
        res_json = res_raw
    else:
        # print("❌ Unexpected type of response from evaluator.")
        res_json = []

    # Evaluate results
    for original_item, eval_result in zip(batch, res_json):
        actual_q = original_item["query"]
        related_res = eval_result.get("related", False)
        usecase_res = eval_result.get("has_usecase_details", False)
        entry_obj = {
            "User": actual_q,
            "Ai Response": eval_result.get("explanation", ""),
        }

        if related_res and usecase_res:
            valid_responses.append(entry_obj)
    return valid_responses


async def triggeraicontextfinder(instruction_input, userid, templatedata, contacts):
    normalized = normalize_input(instruction_input)
    ques = await check_doc_context_needed(normalized, templatedata, userid=userid)
    print("len of the questions made", len(ques), ques)
    if len(ques) > 0:
        # filenames = getFilenameData(userid)
        content = run_async(fetch_ques_with_docs(ques, userid, contacts))
        batch_size = 10
        valid_responses = []

        print(len(content), "context length")

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(
                    evallogic, templatedata, content[i : i + batch_size], userid=userid
                )
                for i in range(0, len(content), batch_size)
            ]

        valid_responses = []
        for f in futures:
            valid_responses.extend(f.result())
        return valid_responses


def normalize_contacts(contacts):
    # Case 1: string "all" / "All"
    if isinstance(contacts, str):
        if contacts.strip().lower() == "all":
            return "All"
        return contacts

    # Case 2: list ["all"] / ["All"]
    if isinstance(contacts, list):
        lowered = [str(c).strip().lower() for c in contacts if c]

        if len(lowered) == 1 and lowered[0] == "all":
            return "All"

        # Otherwise assume list of emails / resolved contacts
        return contacts

    # Fallback safety
    return "All"


def returninsructdata(data):
    """
    Prepares instruction input for the AI template safely.
    """
    name = data.get("title", "")
    description = data.get("description", "")
    triggermode = data.get("trigger_mode", "")
    ai_mode = data.get("ai_mode", "normal")
    inp_steps = data.get("steps", [])
    contacts = normalize_contacts(data.get("contacts", "All"))

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
        "selected_contacts": contacts,
        "ai_mode": ai_mode,
        "inp_steps": inp_steps,
        "context_section": "[]",  # to be updated
        "services_section": "",  # to be updated
        "additional_data": data.get("additional_data", ""),
        "main_user_account_type": "",
        "user_timezone": "UTc",
        "todays_date": datetime.now().strftime(
            "%A, %d %B %Y"
        ),  # e.g., Monday, 21 October 2025
    }

    return instruction_input


def clean_json_block(raw):
    if isinstance(raw, dict):
        return json.dumps(raw)

    match = re.search(r"```json(.*?)```", raw, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()

    return raw.strip()


INTERNAL_DATA_PATTERN = re.compile(
    r"\b("
    # Roles / people
    r"owner|incharge|manager|employee|staff|admin|team|hr|finance|support|"
    # Calendar / availability
    r"calendar|availability|available|free\s+slot|schedule|reschedule|meeting\s+time|time\s+slot|"
    # Business data
    r"sales|revenue|profit|inventory|stock|orders?|crm|leads?|pipeline|metrics|analytics|report|performance|status|"
    # Stored assets
    r"file|files|document|documents|audio|recording|transcript|pdf|docx|ppt|spreadsheet|data\s+from|"
    # Internal refs
    r"internal|from\s+system|from\s+database|stored|existing|previous|history|current"
    r")\b",
    re.IGNORECASE,
)


def cheap_internal_data_hint(instruction_input):
    text = " ".join(
        filter(
            None,
            (
                instruction_input.get("name", ""),
                instruction_input.get("description", ""),
                " ".join(instruction_input.get("trigger_input", [])),
            ),
        )
    )
    return bool(INTERNAL_DATA_PATTERN.search(text))


async def needs_internal_data(instruction_input, template_data, user_id):
    """
    Determine whether the instruction requires internal data.
    Step 1: cheap regex check
    Step 2: call LLM only if regex signals potential dependency
    """
    # Step 1: Regex gate
    if not cheap_internal_data_hint(instruction_input):
        print("no need for llm")
        return False  # no LLM call needed, safe shortcut

    # Step 2: LLM authoritative check
    fn_temp_prompt = template_data.get("detect_internal_data_dependency")
    main_fnprompt = fn_temp_prompt.format(
        instruction_input_as_json=json.dumps(instruction_input, separators=(",", ":")),
    )
    functions_res = await get_fireworks_response2(
        user_message=main_fnprompt, role="system", temp=0, user_id=user_id
    )

    # Clean response
    cleaned = functions_res.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.replace("```json", "").replace("```", "").strip()

    try:
        validated = json.loads(cleaned)
    except Exception as e:
        return {
            "status": "Not possible",
            "reason": f"Invalid JSON returned by functions_checker: {str(e)}",
        }
    # print("validated", validated)
    return validated.get("needs_internal_data", False)


async def minimize_functions(
    instruction_input, template_data, functions_ds, actual_social, userid
):
    # functions_datas = read_function_jsons2(Full=True)
    # print("the functions data", len(functions_datas))

    fn_temp_prompt = template_data.get("functions_checker")
    #  users_social_behaviour=actual_social,
    main_fnprompt = fn_temp_prompt.format(
        instruction_input_as_json=json.dumps(instruction_input, separators=(",", ":")),
        all_function_details=json.dumps(functions_ds, separators=(",", ":")),
        Actucal_user_socaial=actual_social,
    )

    functions_res = await get_fireworks_response2(
        user_message=main_fnprompt, role="system", temp=0, user_id=userid
    )

    # ----------------------------
    # Clean LLM output
    # ----------------------------
    cleaned = functions_res.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.replace("```json", "").replace("```", "").strip()

    try:
        validated = json.loads(cleaned)
    except Exception as e:
        return {
            "status": "Not possible",
            "reason": f"Invalid JSON returned by functions_checker: {str(e)}",
        }
    # print("validated", validated)
    # ----------------------------
    # FAIL CASE (authoritative)
    # ----------------------------
    if validated.get("status") == "Not possible":
        return None, None

    # ----------------------------
    required = validated.get("required_functions", [])
    # print("the required values", required)
    checker_dict = {
        "is_googlecalender": False,
        "is_microsoftcalender": False,
    }

    if not required:
        return functions_ds

    expanded_functions = []

    for fn_name in required:
        fn = functions_ds.get(fn_name)
        if not fn:
            continue

        # 🚫 Skip inactive / upcoming functions
        if fn.get("status") != "active":
            continue

        # ✅ Provider detection ONLY for active services
        if "microsoft_calendar" in fn_name:
            checker_dict["is_microsoftcalender"] = True
        elif "google_meet" in fn_name:
            checker_dict["is_googlecalender"] = True

        expanded_functions.append(fn)

    if len(expanded_functions) > 0:
        return expanded_functions, checker_dict
    else:
        return functions_ds, None


import re


def replace_section(
    prompt,
    section_title,
    replacement,
):
    """
    Replaces a markdown section starting with '## section_title'
    until the next '## ' or end of prompt.

    If replacement is None or empty, removes the section completely.
    """
    pattern = rf"(## {re.escape(section_title)}\n(?:.*?\n)*?)(?=## |\Z)"
    match = re.search(pattern, prompt, flags=re.DOTALL)

    if not match:
        raise ValueError(f"Section '{section_title}' not found in prompt")

    if not replacement:
        return prompt[: match.start()] + prompt[match.end() :]

    return (
        prompt[: match.start()] + replacement.rstrip() + "\n\n" + prompt[match.end() :]
    )


async def create_playbook(
    data, template_data, minor_data, functions_ds, nfilename=None
):
    userid = data["user_id"]
    actual_social = fetch_user_Social(user_id=userid)

    # token = current_user_id.set(userid)
    # print("actuial social", actual_social)
    try:
        instruction_input = returninsructdata(data)
        # print("instruction input", instruction_input)
        result = await needs_internal_data(
            template_data=template_data,
            instruction_input=instruction_input,
            user_id=userid,
        )
        print("result -->", result)

        if result:
            ques = await triggeraicontextfinder(
                instruction_input,
                userid,
                template_data,
                contacts=instruction_input["selected_contacts"],
            )
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
        functions_datas, checker_dict = await minimize_functions(
            instruction_input=instruction_input,
            template_data=template_data,
            functions_ds=functions_ds,
            actual_social=actual_social,
            userid=userid,
        )
        if not functions_datas:
            print("NO functions present")
            return (
                jsonify(
                    {
                        "message": "cant able to generate the workflow as per requirements"
                    }
                ),
                400,
            )

        # Attach dynamic services section
        instruction_input["services_section"] = json.dumps(
            functions_datas, separators=(",", ":")
        )
        template = template_data.get("create_instruction")
        if not template:
            raise ValueError("Missing 'create_instruction' template in YAML file.")
        instruction_input["main_user_account_type"] = actual_social
        base_prompt = template.format(**instruction_input)

        full_prompt = base_prompt
        # print("checkerdict", checker_dict)
        if checker_dict:
            meeting_rules = []

            if checker_dict.get("is_googlecalender"):
                rule = minor_data.get("google_meet_rules")
                if rule:
                    ser = GoogleMeetService(userid=userid)
                    timez = ser.get_user_timezone()
                    instruction_input["user_timezone"] = timez or "UTC"
                    meeting_rules.append(rule.strip())

            if checker_dict.get("is_microsoftcalender"):
                rule = minor_data.get("microsoft_meet_rules")
                if rule:
                    ser = MicrosoftGraphCalendarService(userid=userid)
                    timez = ser.get_user_timezone()
                    instruction_input["user_timezone"] = timez or "UTC"
                    meeting_rules.append(rule.strip())

            base_prompt = template.format(**instruction_input)

            replacement_text = "\n\n".join(meeting_rules) if meeting_rules else None
            full_prompt = replace_section(
                prompt=base_prompt,
                section_title="MEETING FUNCTION LINKING RULES",
                replacement=replacement_text,
            )

        raw_response = await get_fireworks_response2(
            user_message=full_prompt, role="system", user_id=userid
        )
        # print("raw response", raw_response)
        # Clean AI response
        cleaned_j = clean_json_block(raw_response)
        try:
            response_dict1 = json.loads(cleaned_j)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON from AI:\n{raw_response}\nError: {e}")
        print("data from initial", response_dict1)
        eval_pr = template_data.get("evaluate_workflow_execution_validity")
        base_Eval_prompt = eval_pr.format(
            instruction_input=json.dumps(instruction_input, separators=(",", ":")),
            workflow_json=json.dumps(response_dict1, separators=(",", ":")),
            functions_data=json.dumps(functions_datas, separators=(",", ":")),
        )
        raw_response = await get_evaluator_fireworks(
            user_message=base_Eval_prompt, role="system", user_id=userid
        )
        print("raw response", len(raw_response))
        # Clean AI response
        cleaned_j = clean_json_block(raw_response)
        try:
            response_dict = json.loads(cleaned_j)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON from AI:\n{raw_response}\nError: {e}")
        filename = nfilename or f"{uuid.uuid4().hex[:8]}.json"
        ensure_dir(f"{pathconfig.basepath}/test/")
        filepath = os.path.join(f"{pathconfig.basepath}/test/", filename)
    except Exception as e:
        print("error on playbook creation", e)
    finally:
        #   current_user_id.reset(token)
        print("ss")

    full_output = {
        "filename": filename,
        "input_data": data,  # original data from frontend
        "workflow": response_dict.get("workflow"),  # AI-generated YAML parsed into dict
        "clarifications_generated": False,
        "WorkflowDate": datetime.now().isoformat(),
    }
    delete_file_from_s3(filepath=f"{userid}/workflow/{filename}")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(full_output, f, indent=2)
    res = upload_any_file(
        file_path=filepath, user_id=userid, file_name=filename, type="workflow"
    )
    os.remove(filepath)

    return full_output, res["s3_key"]

    # return {"first": response_dict1, "eval": response_dict}, "checks"
    # return response_dict, "checks"


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
        return {
            "status": "success",
            "message": success_message,
            "data": playbook["clarification_questions"],
        }

    else:
        return {"status": "success", "message": success_message, "data": playbook}


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
