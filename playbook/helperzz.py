import asyncio
import json
import re
import yaml
from agent_route.doc_clarity import fetch_ques_with_docs
from services.meet_service import GoogleMeetService
from services.microsoft_calender_service import MicrosoftGraphCalendarService
from utils.fireworkzz import (
    evaluator_context_llama,
    get_evaluator_fireworks,
    get_fireworks_response2,
)
from utils.pb_config_utils import *
from utils.normal import (
    ensure_dir,
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
from utils.s3_utils import upload_exefileany_file


def base_name(filename):
    name_without_ext = os.path.splitext(filename)[0]
    # print(name_without_ext)
    # print(name_without_ext[:8])

    # Always take first 8 characters (playbook ID)
    return name_without_ext[:8]


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


async def check_doc_context_needed(instruction_input, templatedata, userid, credits):
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
        user_message=full_prompt,
        role="system",
        temp=0.4,
        user_id=userid,
        credits=credits,
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


async def evallogic(templatedata, batch, userid, credits):
    valid_responses = []
    res_raw = await evaluator_context_llama(
        templatedata.get("context_workflow_validator_batch"),
        batch,
        credits,
        userid=userid,
    )
    # print("res jaw in eval", res_raw)
    # Extract the JSON array block using regex
    if isinstance(res_raw, str):
        match = re.search(r"\[\s*{.*?}\s*\]", res_raw, re.DOTALL)
        if match:
            json_block = match.group(0)
            try:
                res_json = yaml.safe_load(json_block)
                # print("✅ Extracted & parsed response block.")
            except Exception as e:
                # print(f"❌ YAML parsing error: {e}")
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
    # print("✅ Extracted & parsed response block.", res_json)
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


async def triggeraicontextfinder(
    instruction_input, userid, templatedata, contacts, credits
):
    normalized = normalize_input(instruction_input)

    ques = await check_doc_context_needed(normalized, templatedata, userid, credits)
    # print("len of the questions made", len(ques), ques)

    if not ques:
        return []

    content = await fetch_ques_with_docs(ques, userid, contacts, credits)
    batch_size = 10

    # print(len(content), "context length")

    async def run_batches(templatedata, content, batch_size, userid):
        tasks = [
            evallogic(
                templatedata,
                content[i : i + batch_size],
                userid=userid,
                credits=credits,
            )
            for i in range(0, len(content), batch_size)
        ]

        results = await asyncio.gather(*tasks)

        valid_responses = []
        for r in results:
            valid_responses.extend(r)

        return valid_responses

    # ✅ JUST AWAIT — NO asyncio.run
    valid_responses = await run_batches(templatedata, content, batch_size, userid)

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
    inp_steps = data.get("steps") or []

    # Normalize contacts
    contacts = normalize_contacts(data.get("contacts") or [])

    # ✅ FIX: Normalize communication_channels safely
    raw_channels = data.get("communication_channels") or []
    communication_channels = list({ch for ch in raw_channels if isinstance(ch, str)})

    instruction_input = {
        "name": name,
        "description": description,
        "communication_mode": communication_channels,
        "selected_contacts": contacts,
        "inp_steps": inp_steps,
        "context_section": "[]",
        "services_section": "",
        "additional_data": data.get("additional_data", ""),
        "main_user_account_type": "",
        "user_timezone": "UTC",
        "todays_date": datetime.now().strftime("%A, %d %B %Y"),
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


async def needs_internal_data(instruction_input, template_data, user_id, credits):
    """
    Determine whether the instruction requires internal data.
    Step 1: cheap regex check
    Step 2: call LLM only if regex signals potential dependency
    """
    # Step 1: Regex gate
    if not cheap_internal_data_hint(instruction_input):
        # print("no need for llm")
        return False  # no LLM call needed, safe shortcut

    # Step 2: LLM authoritative check
    fn_temp_prompt = template_data.get("detect_internal_data_dependency")
    main_fnprompt = fn_temp_prompt.format(
        instruction_input_as_json=json.dumps(instruction_input, separators=(",", ":")),
    )
    functions_res = await get_fireworks_response2(
        user_message=main_fnprompt,
        role="system",
        temp=0,
        user_id=user_id,
        credits=credits,
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
    instruction_input, template_data, functions_ds, actual_social, userid, credits
):
    # functions_datas = read_function_jsons2(Full=True)
    # print("the functions data", len(functions_ds))

    fn_temp_prompt = template_data.get("functions_checker")
    #  users_social_behaviour=actual_social,
    main_fnprompt = fn_temp_prompt.format(
        instruction_input_as_json=json.dumps(instruction_input, separators=(",", ":")),
        all_function_details=json.dumps(functions_ds, separators=(",", ":")),
        Actucal_user_socaial=actual_social,
    )

    functions_res = await get_fireworks_response2(
        user_message=main_fnprompt,
        role="system",
        temp=0,
        user_id=userid,
        credits=credits,
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

    if not required:
        return functions_ds, None

    checker_dict = {
        "is_googlecalender": False,
        "is_microsoftcalender": False,
    }
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
    # print("len of expanded funcitons", len(expanded_functions))

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
    data,
    template_data,
    minor_data,
    functions_ds,
    db,
    credits,
    nfilename=None,
):
    userid = data["user_id"]
    actual_social = fetch_user_Social(user_id=userid, connection=db)
    filename = nfilename or f"{uuid.uuid4().hex[:8]}.json"
    ensure_dir(f"{pathconfig.basepath}/test/")
    filepath = os.path.join(f"{pathconfig.basepath}/test/", filename)

    # token = current_user_id.set(userid)
    # print("actuial social", actual_social)
    try:
        instruction_input = returninsructdata(data)
        # print("instruction input", instruction_input)
        result = await needs_internal_data(
            template_data=template_data,
            instruction_input=instruction_input,
            user_id=userid,
            credits=credits,
        )
        # print("result -->", result)

        if result:
            ques = await triggeraicontextfinder(
                instruction_input,
                userid,
                template_data,
                contacts=instruction_input["selected_contacts"],
                credits=credits,
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
        # print("before minimizing")
        functions_datas, checker_dict = await minimize_functions(
            instruction_input=instruction_input,
            template_data=template_data,
            functions_ds=functions_ds,
            actual_social=actual_social,
            userid=userid,
            credits=credits,
        )
        if not functions_datas:
            # print("NO functions present")
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
        # print("instruiction input", len(instruction_input))
        try:
            base_prompt = template.format(**instruction_input)
        except Exception as e:
            # print("TEMPLATE ERROR:", e)
            # print(template)
            raise
        # print("base prompt", base_prompt)

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
            if replacement_text:
                full_prompt = replace_section(
                    prompt=base_prompt,
                    section_title="MEETING FUNCTION LINKING RULES",
                    replacement=replacement_text,
                )

        raw_response = await get_fireworks_response2(
            user_message=full_prompt, role="system", user_id=userid, credits=credits
        )
        # print("raw response", len(raw_response))
        # Clean AI response
        cleaned_j = clean_json_block(raw_response)
        try:
            response_dict1 = json.loads(cleaned_j)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON from AI:\n{raw_response}\nError: {e}")
        # print("data from initial", response_dict1)
        eval_pr = template_data.get("evaluate_workflow_execution_validity")
        base_Eval_prompt = eval_pr.format(
            instruction_input=json.dumps(instruction_input, separators=(",", ":")),
            workflow_json=json.dumps(response_dict1, separators=(",", ":")),
            functions_data=json.dumps(functions_datas, separators=(",", ":")),
        )
        raw_response = await get_evaluator_fireworks(
            user_message=base_Eval_prompt,
            role="system",
            user_id=userid,
            credits=credits,
        )
        # print("raw response eval", len(raw_response))
        # Clean AI response
        cleaned_j = clean_json_block(raw_response)
        try:
            response_dict = json.loads(cleaned_j)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON from AI:\n{raw_response}\nError: {e}")

    except Exception as e:
        print("error on playbook creation", e)

    first_char = base_name(filename=filename)

    full_output = {
        "filename": filename,
        "input_data": data,  # original data from frontend
        "workflow": response_dict.get("workflow"),  # AI-generated YAML parsed into dict
        "clarifications_generated": False,
        "WorkflowDate": datetime.now().isoformat(),
        "is_global": False,
        "autotest": {"status": False, "count": 0},
        "runbook_id": None,
    }
    delete_file_from_s3(filepath=f"{userid}/workflow/{first_char}/{filename}")
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


def save_execution_playbook_to_s3(playbook, user_id, success_message, filepath):
    with tempfile.NamedTemporaryFile(
        delete=False, suffix=".json", mode="w"
    ) as tmp_file:
        json.dump(playbook, tmp_file, indent=2)
        temp_file_path = tmp_file.name

    upload_exefileany_file(file_path=temp_file_path, bfilepath=filepath)
    os.remove(temp_file_path)
    return {"status": "success", "message": success_message, "data": playbook}


def format_step_data(stepdata: dict) -> dict:
    """Return a cleaned step definition matching the new workflow step schema."""
    out = {}

    # ---- Core fields (always safe to keep if present) ----
    for src, tgt in [
        ("id", "id"),
        ("title", "title"),
        ("stepType", "type"),
        ("objective", "objective"),
    ]:
        val = stepdata.get(src)
        if val is not None and val != "":
            out[tgt] = val

    # ---- Decision handling (always include boolean) ----
    out["decision_point"] = bool(stepdata.get("isDecisionPoint", False))

    if out["decision_point"] and stepdata.get("decisionType"):
        out["decision_type"] = stepdata["decisionType"]

    # ---- Conditions ----
    if stepdata.get("conditions"):
        out["condition"] = stepdata["conditions"]

    # ---- Next step ----
    if stepdata.get("nextStepIds"):
        out["next_step"] = stepdata["nextStepIds"]
    elif stepdata.get("next_step") is not None:
        # Explicit null is allowed
        out["next_step"] = stepdata["next_step"]

    # ---- AI instructions ----
    if stepdata.get("instructions"):
        out["ai_instructions"] = stepdata["instructions"]

    # ---- Function call (NEW FORMAT – MUST KEEP AS-IS) ----
    if stepdata.get("function_call"):
        out["function_call"] = stepdata["function_call"]

    # ---- Requirements ----
    if stepdata.get("requirements_needed"):
        out["requirements_needed"] = stepdata["requirements_needed"]
    else:
        out["requirements_needed"] = []

    # ---- Scheduler ----
    out["is_scheduler"] = stepdata.get("is_scheduler")

    # ---- Fallback ----
    if stepdata.get("fallback_step") is not None:
        out["fallback_step"] = stepdata["fallback_step"]

    # ---- Communication step extras ----
    if stepdata.get("stepType") == "communication":
        if stepdata.get("communicationMode"):
            out["communication_mode"] = stepdata["communicationMode"]
        if stepdata.get("selectedIntegrations"):
            out["channels"] = stepdata["selectedIntegrations"]
        if stepdata.get("calendar_type"):
            out["calendar_type"] = stepdata["calendar_type"]

    # ---- Navigation step extras ----
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


def update_playbook_schedule_and_runtime(
    userid,
    filename,
    schedule=None,
    runtime=None,
    status=None,
):
    """
    Atomic update:
    - Updates base workflow JSON (current_schedule)
    - Updates playbooksconfig.json (schedule, runtime, status)
    """

    playbook_id, config_path, subagent_id = returnconfigandpath(userid)
    config_data = read_json_from_s3(config_path) or {}

    # ----------------------------------
    # Ensure user block
    # ----------------------------------
    if userid not in config_data:
        config_data[userid] = {"playbooklist": []}

    playbooks = config_data[userid]["playbooklist"]

    # ----------------------------------
    # Update BASE WORKFLOW JSON
    # ----------------------------------
    if schedule is not None:
        wf_loc = f"{userid}/workflow/{base_name(filename=filename)}/{filename}"
        main_workflow = read_json_from_s3(wf_loc) or {}

        # Always overwrite to keep source of truth
        main_workflow["current_schedule"] = schedule

        save_playbook_to_s3(
            main_workflow,
            userid,
            "workflow schedule updated",
            filename,
        )

    # ----------------------------------
    # Find or create playbook entry
    # ----------------------------------
    entry = next((pb for pb in playbooks if pb.get("name") == filename), None)

    if not entry:
        entry = {
            "name": filename,
            "schedule": {},
            "runtime": {},
            "status": "Stopped",
        }
        playbooks.append(entry)

    # ----------------------------------
    # Apply updates
    # ----------------------------------
    if schedule is not None:
        entry["schedule"] = schedule

    if runtime is not None:
        entry.setdefault("runtime", {})
        entry["runtime"].update(runtime)

    if status is not None:
        entry["status"] = status

    # ----------------------------------
    # Persist playbooksconfig.json
    # ----------------------------------
    local_path = f"/tmp/{userid}_playbooksconfig.json"
    with open(local_path, "w") as f:
        json.dump(config_data, f, indent=2)

    upload_any_file(
        file_path=local_path,
        user_id=userid,
        file_name=config_path,
        type="workflow",
    )

    os.remove(local_path)
    return True


def assign_runbook_playbook(runbook_id, playbook, userid):
    filename = playbook
    wf_loc = f"{userid}/workflow/{base_name(filename=filename)}/{filename}"
    main_workflow = read_json_from_s3(wf_loc) or {}

    # Always overwrite to keep source of truth
    main_workflow["runbook_id"] = runbook_id
    print("assigning runbook to playbook")
    return save_playbook_to_s3(
        main_workflow,
        userid,
        "workflow schedule updated",
        filename,
    )
