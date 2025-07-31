import json
import re
import yaml
from agent_route.doc_clarity import fetch_ques_with_docs
from utils.fireworkzz import evaluator_context_llama, get_fireworks_response


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
