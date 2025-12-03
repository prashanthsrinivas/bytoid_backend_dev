from collections import defaultdict
from datetime import datetime
import json
import os, time
from typing import *
import re
from cust_helpers import pathconfig
from db.db_checkers import fetch_contacts_by_user, get_userinfo
from db.rds_db import connect_to_rds
from playbook.helperzz import save_playbook_to_s3
from utils.base_logger import get_logger
from utils.fireworkzz import (
    get_fireworks_response,
    get_fireworks_response2,
)
from utils.normal import (
    can_reply_to_email,
    load_yaml_file,
    read_function_jsons,
    read_function_jsons2,
)
from utils.s3_utils import read_json_from_s3
from dotenv import load_dotenv
import copy, uuid

load_dotenv()


class WorkflowRunnerV2:
    def __init__(
        self,
        userid: str,
        filename: str,
        workflowJson=None,
        contacts=None,
        testing=False,
        on_update=None,
    ):
        self.userid = userid
        self.filename = filename
        self.connection = connect_to_rds()
        self.wf_loc = f"{userid}/workflow/{filename}"
        base_workflow = workflowJson or read_json_from_s3(self.wf_loc)
        self.workflow_json = copy.deepcopy(base_workflow)
        self.userdetails = get_userinfo(self.userid)
        self.contacts = contacts or fetch_contacts_by_user(self.userid)
        self.testing = testing
        self.current_wf_id = None
        # Correctly load steps from workflow['steps'] instead of top-level steps
        workflow_steps = self.workflow_json.get("workflow", {}).get("steps", [])
        self.steps = {step["id"]: step for step in workflow_steps}
        self.input_data = self.workflow_json.get("input_data", {})
        self.chat_history = self.workflow_json.get("chat", [])
        self.chat_log = self.workflow_json.get("chat_log", {})
        self.execution_log: list[dict] = []
        self.previous_data = self.get_current_execution_data()
        self.logger = get_logger(__name__)
        self.ai_made_output = {}
        self.current_implemented_functions = read_function_jsons()
        self.on_update = on_update

    def is_yes(self, text: str) -> bool:
        yes_words = {
            "yes",
            "y",
            "yeah",
            "yep",
            "sure",
            "ok",
            "okay",
            "ya",
            "affirmative",
            "confirm",
            "correct",
        }
        text = text.lower()

        # extract alphabetic words only
        words = re.findall(r"[a-zA-Z]+", text)

        return any(word in yes_words for word in words)

    def get_current_execution_data(self):
        if "testing" in self.workflow_json and self.testing:
            return self.workflow_json["testing"]
        elif "online" in self.workflow_json and not self.testing:
            return self.workflow_json["online"]
        else:
            return {}

    def get_current_chats(self):
        allchats = self.workflow_json.get("chat", [])
        chat_log = self.workflow_json.get("chat_log", {})
        if chat_log:
            last_chat_check = chat_log.get("last_chat_summarized")
            last_summarization = chat_log.get("chat_summarization") or ""
            if last_chat_check:
                if allchats:
                    mixchats = allchats[-10:]
                else:
                    mixchats = []
                return {"chat": mixchats, "chat_summarization": last_summarization}
        else:
            return {"chat": allchats, "chat_summarization": ""}

    def generate_unique_id(self, existing_ids):
        while True:
            uid = str(uuid.uuid4().int)[0:6]
            if uid not in existing_ids:
                return uid

    def prompt_template_load(self):
        return load_yaml_file(path=pathconfig.play_template)

    def send_update(self, event, data):
        if self.on_update:
            self.on_update(event, data)

    def check_affirmative(self, userinput):
        affirmative_words = [
            "yes",
            "y",
            "ok",
            "sure",
            "confirm",
            "correct",
            "that's right",
            "yep",
            "go ahead",
            "please proceed",
        ]

        pattern = (
            r"\b(" + "|".join(re.escape(word) for word in affirmative_words) + r")\b"
        )

        def is_affirmative(user_input: str) -> bool:
            if not user_input:
                return False
            return bool(re.search(pattern, user_input.lower()))

        return is_affirmative(user_input=userinput)

    def get_chat_summarization(self, chats_obj=None):
        template_data = self.prompt_template_load()

        chat_block = template_data.get("chat_summarization", {})
        prompt_instructions = chat_block.get("instructions")

        if not isinstance(prompt_instructions, str):
            raise TypeError(
                "Invalid template: chat_summarization.instructions must be a string"
            )

        # ✅ Normalize default
        if chats_obj is None:
            chats_obj = self.get_current_chats()

        # ✅ Ensure dict structure
        if isinstance(chats_obj, list):
            chats_obj = {"chat": chats_obj, "chat_summarization": ""}

        if not isinstance(chats_obj, dict):
            raise TypeError(
                f"chat_summarization expects dict or list of chat items, got {type(chats_obj)}"
            )

        # ✅ Safe extract
        new_chat = chats_obj.get("chat", [])
        previous_summary = chats_obj.get("chat_summarization", "")

        # Serialize new chat messages
        new_chat_json = json.dumps(new_chat, ensure_ascii=False, indent=2)

        prompt_text = (
            prompt_instructions.replace("{{chat}}", new_chat_json)
            .replace("{{chat_summarization}}", previous_summary or "")
            .strip()
        )

        result = self.get_parsed_fireworks_response(prompt_text)
        # print("res chat summarize", result)

        if result and "summary" in result:
            return result["summary"]
        return None

    def handle_workflow_reset(self, ai_result, user_input: str):
        """
        Executes workflow reset only when confirmed.
        This function no longer logs chat — only computes result and execution logs.
        Chat logging happens only in check_input_tone().
        """
        reset_action = ai_result.get("reset")  # "all" or "id"
        step_id = ai_result.get("step_id")
        clarification = ai_result.get("clarification_needed", False)
        message = ai_result.get("response_message") or ai_result.get("message")
        now = datetime.now()

        # 🟡 Clarification — no reset yet, return message & signal to log chat
        if clarification:
            return {
                "response_message": message,
                "reset_needed": True,
                "clarification_needed": True,
                "reset": reset_action,
                "step_id": step_id,
                "log_status": "clarification",
            }

        # ✅ Reset ALL
        if reset_action == "all":
            target = "testing" if self.testing else "online"
            self.workflow_json[target] = {}

            response_text = (
                "All testing data has been reset. You can start testing again."
                if self.testing
                else "All workflow progress has been reset."
            )

            exec_details = {"action": "reset_all"}
            result = {
                "response_message": response_text,
                "workflow_intent": True,
                "reset": "all",
            }

        # ✅ Reset specific STEP
        elif reset_action == "id" and step_id is not None:
            target = "testing" if self.testing else "online"
            self.workflow_json[target].pop(str(step_id), None)

            response_text = f"Step {step_id} has been reset. You can retest this step."
            exec_details = {"action": "reset_step", "step_id": step_id}
            result = {
                "response_message": response_text,
                "workflow_intent": True,
                "reset": "id",
                "step_id": step_id,
            }

        else:
            # ❓ Unclear reset request
            return {
                "response_message": "Reset request unclear. Which step?",
                "clarification_needed": True,
                "log_status": "clarification",
            }

        # # ✅ Log execution (NOT chat)
        # self.workflow_json.setdefault("execution_logs", []).append(
        #     {
        #         "timestamp": now.isoformat(),
        #         "input": user_input,
        #         "output": result["response_message"],
        #         "status": "success",
        #         "details": exec_details,
        #         "step_id": result.get("step_id"),
        #     }
        # )
        self.saveworkflowtos3()
        return result

    def ai_input_intent_classifier(self, userinput):
        """
        Classifies user input into one of: workflow, explanation, resetStep, or normal_conversation.
        Includes workflow-level context from input_data to improve domain awareness.
        """
        template_data = self.prompt_template_load()
        prompt_instructions = template_data.get("input_intent_classifier", {})
        if not isinstance(prompt_instructions, str):
            raise TypeError(
                "Invalid template structure: expected string for 'instructions'."
            )

        # Retrieve chat context
        chats_obj = self.get_current_chats()
        new_chat = chats_obj.get("chat", [])
        previous_summary = chats_obj.get("chat_summarization", "")
        custeps = self.steps
        inputdata = self.input_data or {}

        # Prepare workflow step summaries
        steptitles = [
            {
                str(step["id"]): {
                    "title": step.get("title", ""),
                    "description": step.get("objective", ""),
                }
            }
            for _, step in custeps.items()
        ]

        # Serialize chat history
        new_chat_json = json.dumps(new_chat, ensure_ascii=False, indent=2)

        # Build replacements safely
        prompt_text = (
            prompt_instructions.replace("{{user_input}}", userinput)
            .replace("{{chat}}", new_chat_json)
            .replace("{{chat_summarization}}", previous_summary or "")
            .replace(
                "{{workflow_titles}}",
                json.dumps(steptitles, ensure_ascii=False, indent=2),
            )
            .replace("{{input_data.title}}", inputdata.get("title", ""))
            .replace("{{input_data.description}}", inputdata.get("description", ""))
            .replace("{{input_data.category}}", inputdata.get("category", ""))
            .replace("{{input_data.tags}}", ", ".join(inputdata.get("tags", [])))
            .strip()
        )

        # Get parsed result from Fireworks
        result = self.get_parsed_fireworks_response(prompt_text)
        return result

    def ai_conversation_handler(self, userinput):
        template_data = self.prompt_template_load()
        prompt_instructions = template_data.get("workflow_conversation_handler", {})
        if not isinstance(prompt_instructions, str):
            raise TypeError(
                "Invalid template structure: expected string for 'instructions'."
            )
        chats_obj = self.get_current_chats()
        new_chat = chats_obj.get("chat", [])
        previous_summary = chats_obj.get("chat_summarization", "")

        # Serialize new chat messages
        new_chat_json = json.dumps(new_chat, ensure_ascii=False, indent=2)

        prompt_text = (
            prompt_instructions.replace("{{user_input}}", userinput)
            .replace("{{chat_history}}", new_chat_json)
            .replace("{{chat_summarization}}", previous_summary or "")
            .strip()
        )
        result = self.get_parsed_fireworks_response(prompt_text)
        if result and "reply" in result:
            return result["reply"]
        return result

    def ai_detect_trigger_type(self, userinput):
        template_data = self.prompt_template_load()
        prompt_instructions = template_data.get("detect_trigger_type", {})
        if not isinstance(prompt_instructions, str):
            raise TypeError(
                "Invalid template structure: expected string for 'instructions'."
            )

        chats_obj = self.get_current_chats()
        new_chat = chats_obj.get("chat", [])
        previous_summary = chats_obj.get("chat_summarization", "")
        previous_data = self.previous_data
        baseworkflow = self.workflow_json.get("workflow", {})
        base_ai_instruction = None
        inputdata = self.input_data or {}

        # ✅ correct handling when chats are empty
        if new_chat:
            base_chat = new_chat[-1]
        else:
            base_chat = {}
        step_id = base_chat.get("step_id")
        now = datetime.now()
        todays_date = now.isoformat()
        # print("step id", step_id)
        if step_id != "all":
            if not step_id:
                step_match = re.search(r"\bstep\s*(\d+)", userinput, re.IGNORECASE)
                step_id = int(step_match.group(1)) if step_match else None

            # Safe validation
            if (
                step_id not in [None, "", "null"]  # handles null, "", None, JSON null
                and str(step_id).strip().isdigit()  # prevents non-numeric cases
                and int(step_id) > 0  # avoid zero or negative indexes
                and self.is_yes(userinput)  # yes-intent check
            ):
                last_step = int(step_id)
                baseworkflow = self.steps.get(last_step)
                base_ai_instruction = baseworkflow.get("ai_instructions")
        else:
            userinput = f"{userinput} continue full workflow exection"

        # ✅ serialize properly
        last_chat = json.dumps(base_chat, ensure_ascii=False, indent=2)
        new_chat_json = json.dumps(new_chat, ensure_ascii=False, indent=2)
        custeps = self.steps

        # Proper steptitles list for prompt
        steptitles = [
            {
                str(step["id"]): {
                    "title": step["title"],
                    "description": step["objective"],
                }
            }
            for _, step in custeps.items()
        ]

        # ✅ Build prompt (do NOT double-dump JSON for {{current_chats}})
        prompt_text = (
            prompt_instructions.replace("{{user_input}}", userinput)
            .replace("{{workflow_json}}", json.dumps(baseworkflow))
            .replace("{{previous_data}}", json.dumps(previous_data))
            .replace("{{current_chats}}", new_chat_json)
            .replace("{{previous_chat_summary}}", previous_summary)
            .replace("{{last_chat}}", last_chat)
            .replace("{{ai_instruction}}", base_ai_instruction or "")
            .replace(
                "{{workflow_titles}}",
                json.dumps(steptitles, ensure_ascii=False, indent=2),
            )
            .replace("{{input_data.title}}", inputdata.get("title", ""))
            .replace("{{input_data.description}}", inputdata.get("description", ""))
            .replace("{{input_data.category}}", inputdata.get("category", ""))
            .replace("{{input_data.tags}}", ", ".join(inputdata.get("tags", [])))
            .replace("{{todays_datetime}}", ", ".join(str(todays_date)))
        ).strip()

        newresultds = self.get_parsed_fireworks_response(prompt_text)
        # print("res workflow detector", newresultds)
        return newresultds

    def ai_detect_current_step(self, userinput):
        template_data = self.prompt_template_load()
        prompt_instructions = template_data.get("detect_current_step", {})
        if not isinstance(prompt_instructions, str):
            raise TypeError(
                "Invalid template structure: expected string for 'instructions'."
            )
        chats_obj = self.get_current_chats()
        new_chat = chats_obj.get("chat", [])
        baseworkflow = self.workflow_json.get("workflow", {})
        # Serialize new chat messages
        new_chat_json = json.dumps(new_chat, ensure_ascii=False, indent=2)

        prompt_text = (
            prompt_instructions.replace("{{user_input}}", userinput)
            .replace("{{chats}}", new_chat_json)
            .replace(
                "{{workflow_json}}",
                json.dumps(baseworkflow, ensure_ascii=False, indent=2),
            )
            .strip()
        )
        result = self.get_parsed_fireworks_response(prompt_text)
        if result and "step_id" in result:
            return result["step_id"]
        return result

    def ai_detect_and_route_input(self, userinput, extracted_id=None):
        template_data = self.prompt_template_load()
        prompt_instructions = template_data.get("detect_and_route_input", {})
        if not isinstance(prompt_instructions, str):
            raise TypeError(
                "Invalid template structure: expected string for 'instructions'."
            )

        chats_obj = self.get_current_chats()
        new_chat = chats_obj.get("chat", [])
        previous_data = self.previous_data
        baseworkflow = self.workflow_json.get("workflow", {})
        base_ai_instruction = None
        lastly_ai_attached = self.workflow_json.get("last_ai_discovered", {}) or {}
        if "pre_user_data" not in self.workflow_json:
            self.workflow_json["pre_user_data"] = {}
        user_made_arguments = self.workflow_json.get("pre_user_data", {})
        custeps = self.steps
        inputdata = self.input_data
        now = datetime.now()
        todays_date = now.isoformat()

        # -----------------------------
        # 1️⃣ Identify the latest confirmation step
        # -----------------------------
        last_chat = new_chat[-1] if new_chat else []

        # print("extracted id", extracted_id)
        # print("last  chat", last_chat)

        # Step titles for prompt
        steptitles = [
            {
                str(step["id"]): {
                    "title": step["title"],
                    "description": step["objective"],
                }
            }
            for _, step in custeps.items()
        ]

        def build_prompt(u_input):
            return (
                prompt_instructions.replace("{{user_input}}", u_input)
                .replace("{{workflow_json}}", json.dumps(baseworkflow))
                .replace("{{previous_data}}", json.dumps(previous_data))
                .replace(
                    "{{current_chats}}",
                    json.dumps(new_chat, ensure_ascii=False, indent=2),
                )
                .replace(
                    "{{last_chat}}", json.dumps(last_chat, ensure_ascii=False, indent=2)
                )
                .replace("{{ai_instruction}}", base_ai_instruction or "")
                .replace(
                    "{{previous_trigger_attachements}}", json.dumps(lastly_ai_attached)
                )
                .replace("{{user_made_arguments}}", json.dumps(user_made_arguments))
                .replace(
                    "{{workflow_titles}}",
                    json.dumps(steptitles, ensure_ascii=False, indent=2),
                )
                .replace("{{input_data.title}}", inputdata.get("title", ""))
                .replace("{{input_data.description}}", inputdata.get("description", ""))
                .replace("{{input_data.category}}", inputdata.get("category", ""))
                .replace("{{input_data.tags}}", ", ".join(inputdata.get("tags", [])))
                .replace("{{todays_datetime}}", ", ".join(str(todays_date)))
            ).strip()

        modinput = userinput
        if extracted_id:
            modinput = f"{userinput} - it is step {extracted_id}"

        # -----------------------------
        # 2️⃣ Initial AI call with raw input
        # -----------------------------
        # print("user input", modinput)
        ai_result = self.get_parsed_fireworks_response(build_prompt(modinput))
        # print("ai detect initial", ai_result)

        # If AI already returned wf_single_runner=True, no need for further steps
        if ai_result.get("wf_single_runner") == True:
            # print("runner single", ai_result.get("wf_single_runner"))
            return ai_result

        def build_second_stage_prompt(u_input, ai_result):
            second_stage_instructions = template_data.get(
                "second_stage_confirmation_handler", ""
            )
            return (
                second_stage_instructions.replace("{{user_input}}", u_input)
                .replace("{{ai_result}}", json.dumps(ai_result, ensure_ascii=False))
                .replace(
                    "{{current_chats}}",
                    json.dumps(new_chat, ensure_ascii=False, indent=2),
                )
                .replace(
                    "{{previous_data}}", json.dumps(previous_data, ensure_ascii=False)
                )
                .replace("{{workflow_json}}", json.dumps(baseworkflow))
                .replace(
                    "{{previous_trigger_attachements}}",
                    json.dumps(lastly_ai_attached, ensure_ascii=False),
                )
            ).strip()

        if last_chat and ai_result.get("step_id") and self.check_affirmative(userinput):
            # -----------------------------
            # 3️⃣ Second AI call with augmented confirmation input
            # -----------------------------
            prompt_instructions = template_data.get(
                "second_stage_confirmation_handler", {}
            )

            ai_result_confirm = self.get_parsed_fireworks_response(
                build_second_stage_prompt(userinput, ai_result)
            )
            # print("second AI attempt:", ai_result_confirm)

            if ai_result_confirm.get("wf_single_runner"):
                if ai_result_confirm.get("response_message") == "":
                    ai_result_confirm["response_message"] = (
                        f"Confirmed step {last_chat['step_id']}. Proceeding with {userinput}",
                    )
                if ai_result_confirm.get("step_id") == "" or None:
                    ai_result_confirm["step_id"] = ai_result["step_id"]
                if ai_result_confirm["step_id"] == extracted_id:
                    # print("not skipping making second return")
                    return ai_result_confirm

        # -----------------------------
        # 5️⃣ Return AI result as fallback
        # -----------------------------
        # print("going into fallbacks")
        return ai_result

    def ai_decision_Check(self, userinput, extracted_id=None):
        # Load template
        template_data = self.prompt_template_load()
        prompt_instructions = template_data.get("decision_type_check", {})
        if not isinstance(prompt_instructions, str):
            raise TypeError(
                "Invalid template structure: 'decision_type_check' must be a string."
            )

        # Base workflow data
        chats_obj = self.get_current_chats()
        chat_history = chats_obj.get("chat", [])
        workflow_json = self.workflow_json.get("workflow", {})
        steps = self.steps
        input_data = self.input_data

        # Identify current step
        current_step = steps.get(extracted_id)
        if current_step is None:
            raise ValueError(
                f"Step ID {extracted_id} does not exist in workflow steps."
            )
        # AI instructions from step if exists
        step_ai_instruction = current_step.get("ai_instructions", "")

        # -----------------------
        # PROMPT BUILDER
        # -----------------------
        def build_prompt(u_input):
            p = prompt_instructions
            p = p.replace("{{user_input}}", u_input)
            p = p.replace("{{workflow_json}}", json.dumps(workflow_json))
            p = p.replace(
                "{{chat_history}}", json.dumps(chat_history, ensure_ascii=False)
            )
            p = p.replace(
                "{{current_step}}", json.dumps(current_step, ensure_ascii=False)
            )
            p = p.replace("{{ai_instruction}}", step_ai_instruction)
            p = p.replace("{{input_data}}", input_data)
            return p.strip()

        # -----------------------
        # LLM EXECUTION
        # -----------------------
        ai_result = self.get_parsed_fireworks_response(build_prompt(userinput))
        # print("AI Decision Result:", ai_result)

        # Default: return AI evaluation
        return ai_result

    def ai_explain_workflow_steps(self, userinput):
        template_data = self.prompt_template_load()
        prompt_section = template_data.get("explain_workflow", {})
        prompt_instructions = prompt_section.get("instructions", "")

        if not isinstance(prompt_instructions, str):
            raise TypeError(
                "Invalid template structure: expected string for 'instructions' field inside 'explain_workflow'."
            )

        previous_data = self.previous_data
        baseworkflow = self.workflow_json.get("workflow", {})
        # 🔍 detect step number from user input
        step_match = re.search(r"\bstep\s*(\d+)", userinput, re.IGNORECASE)
        step_id = int(step_match.group(1)) if step_match else None
        if step_id:
            ##print("step id found", step_id)
            baseworkflow = self.steps[step_id]
            ##print("step base workflow", baseworkflow)

        # ✅ Build the prompt text with placeholders
        prompt_text = (
            prompt_instructions.replace("{{user_input}}", userinput)
            .replace("{{workflow_json}}", json.dumps(baseworkflow))
            .replace("{{previous_data}}", json.dumps(previous_data))
        ).strip()
        ##print("final prompt", prompt_text)

        newresultds = self.get_parsed_fireworks_response(prompt_text)
        # print("res explanation", newresultds)
        return newresultds

    def ai_reset_intent_handler(self, userinput):
        template_data = self.prompt_template_load()
        custeps = self.steps

        # Map id -> title
        step_map = {str(step["id"]): step["title"] for _, step in custeps.items()}

        # Proper steptitles list for prompt
        steptitles = [{str(step["id"]): step["title"]} for _, step in custeps.items()]
        ##print("step titles", steptitles)

        chats_obj = self.get_current_chats()
        new_chat = chats_obj.get("chat", [])
        previous_summary = chats_obj.get("chat_summarization", "")
        previous_data = self.previous_data
        ##print("completed step ids", done_step_ids)

        # ✅ Build executed steps with titles
        done_steps_with_titles = {
            sid: step_map.get(str(sid), "") for sid in previous_data
        }
        ##print("completed steps with titles", done_steps_with_titles)

        prompt_base = template_data.get("reset_intent_handler")
        prompt_text = (
            prompt_base.replace("{{user_input}}", userinput)
            .replace("{{step_titles}}", json.dumps(steptitles))
            .replace("{{previous_data}}", json.dumps(previous_data))
            .replace("{{current_chats}}", json.dumps(new_chat))
            .replace("{{previous_chat_summary}}", previous_summary)
            .replace(
                "{{done_steps_with_titles}}",
                json.dumps(done_steps_with_titles),
            )
        ).strip()

        newresultds = self.get_parsed_fireworks_response(prompt_text)
        # print("res reset", newresultds)
        return newresultds

    def ai_pre_gather_details(self, userinput):
        template_data = self.prompt_template_load()
        chats_obj = self.get_current_chats()
        new_chat = chats_obj.get("chat", [])
        previous_data = self.previous_data

        allfuns = read_function_jsons2(Full=True)
        used_functions = {}
        workflowjson = self.workflow_json.get("workflow", {}).get("steps", [])
        if "pre_user_data" not in self.workflow_json:
            self.workflow_json["pre_user_data"] = {}

        # Collect only functions used in this workflow
        for step in workflowjson:
            fn_name = step.get("function_call", {}).get("function_name")
            if fn_name and fn_name in allfuns:
                used_functions[fn_name] = allfuns[fn_name]

        # Load prompt and fill placeholders
        prompt_base = template_data.get("gather_workflow_missing_inputs")
        found_details = self.workflow_json["pre_user_data"] or {}
        now = datetime.now()
        todays_date = now.isoformat()

        prompt_text = (
            prompt_base.replace("{{user_input}}", userinput)
            .replace("{{previous_data}}", json.dumps(previous_data))
            .replace("{{current_chats}}", json.dumps(new_chat))
            .replace("{{workflowjson}}", json.dumps(workflowjson))
            .replace("{{used_functions}}", json.dumps(used_functions))
            .replace("{{found_details}}", json.dumps(found_details))
            .replace("{{todays_date}}", str(todays_date))
        ).strip()

        # Get AI results
        newresultds = self.get_parsed_fireworks_response(prompt_text)
        # print("res", newresultds)

        founded = newresultds.get("founded", {}) or {}
        needed = newresultds.get("needed", {}) or {}

        # ✅ Move needed items with actual values into founded
        needed_clean = {}
        for k, v in needed.items():
            if v not in ("", None, [], {}):
                founded[k] = v
            else:
                needed_clean[k] = v
        needed = needed_clean

        # ✅ Merge founded into pre_user_data (update only if missing or changed)
        for key, val in founded.items():
            if (
                key not in self.workflow_json["pre_user_data"]
                or self.workflow_json["pre_user_data"][key] != val
            ):
                self.workflow_json["pre_user_data"][key] = val

        # ✅ Determine still-missing keys
        missing_keys = [
            k
            for k, v in needed.items()
            if k != "dynamic_inputs" and v in ("", None, [], {})
        ]

        if missing_keys:
            message = "I need to know: " + ", ".join(missing_keys)
        else:
            message = False  # All needed values filled
        self.saveworkflowtos3()

        return message

    def ai_execute_helper(self, all_step_results, arguments_needed, ai_instructions):
        template_data = self.prompt_template_load()
        prompt_instructions = template_data.get("execution_helper", {})

        if not isinstance(prompt_instructions, str):
            raise TypeError(
                "Invalid template structure: expected string for 'execution_helper' string."
            )
        found_details = self.workflow_json["pre_user_data"] or {}

        formatted_prompt = (
            prompt_instructions.replace(
                "{{arguments_needed}}", json.dumps(arguments_needed, ensure_ascii=False)
            )
            .replace(
                "{{all_step_results}}", json.dumps(all_step_results, ensure_ascii=False)
            )
            .replace(
                "{{ai_instructions}}", json.dumps(ai_instructions, ensure_ascii=False)
            )
            .replace("{{existing_details}}", json.dumps(found_details))
            .strip()
        )

        result = self.get_parsed_fireworks_response(formatted_prompt)
        # print("res ai_execute_helper", result)
        return result

    def get_parsed_fireworks_response(self, prompt_text, role="system"):
        """
        Get and parse Fireworks response.
        Retries once if the response is empty, invalid, or {}.
        """
        for attempt in range(2):  # attempt 0 = first, attempt 1 = retry once
            response_text = get_fireworks_response2(
                prompt_text, role=role, temp=0.3
            ).strip()
            response_text = re.sub(
                r"^```(?:json)?\s*|\s*```$", "", response_text, flags=re.MULTILINE
            ).strip()

            if not response_text:
                print(f"[Retry {attempt+1}] Empty response from Fireworks.")
                time.sleep(0.3)
                continue
            ##print("res text", response_text)

            try:
                ai_result = json.loads(response_text)
                if not ai_result:  # empty dict {}
                    print(f"[Retry {attempt+1}] Empty JSON object from Fireworks.")
                    time.sleep(0.3)
                    continue
                return ai_result  # ✅ Valid response received

            except json.JSONDecodeError:
                print(f"[Retry {attempt+1}] Failed to parse JSON response.")
                time.sleep(0.3)
                continue

        # After one retry
        # print("[Error] Fireworks response invalid after one retry.")
        return {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.connection:
            self.connection.close()

    def get_attendees(self, attendees=None):
        """
        Normalize attendees input to a clean list of valid email strings.
        Handles:
        - "all"
        - ["all"]
        - ["email1", "email2"]
        - [{"email": "abc@x.com"}, {"email": "y@x.com"}]
        - {"email": "abc@x.com"}
        """

        def contact_email_list():
            emails = []
            if hasattr(self, "contacts") and isinstance(self.contacts, list):
                for contact in self.contacts:
                    email = (
                        contact.get("email") if isinstance(contact, dict) else contact
                    )
                    if can_reply_to_email(email):
                        emails.append(email)
            return emails

        # Testing override
        if self.testing:
            main_test_mail = os.getenv("TEST_EMAIL")
            secondary_mail = os.getenv("TEST_EMAIL2")
            return (
                [secondary_mail]
                if self.userdetails.get("email") == main_test_mail
                else [main_test_mail]
            )

        # Normalize different input types
        if isinstance(attendees, str):
            if attendees.lower() == "all":
                return contact_email_list()
            return [attendees] if can_reply_to_email(attendees) else []

        elif isinstance(attendees, dict):
            email = attendees.get("email")
            return [email] if email and can_reply_to_email(email) else []

        elif isinstance(attendees, list):
            # Handle "all" in list
            if any(isinstance(a, str) and a.lower() == "all" for a in attendees):
                return contact_email_list()

            # Extract emails from mixed list types
            emails = []
            for a in attendees:
                if isinstance(a, str) and can_reply_to_email(a):
                    emails.append(a)
                elif isinstance(a, dict):
                    email = a.get("email")
                    if email and can_reply_to_email(email):
                        emails.append(email)
            return emails

        # Invalid input
        return []

    def check_step_exists(self, step_id):
        """
        Checks if a step exists in self.steps.
        Accepts step_id as int or str, compares against both formats of keys.
        """
        if step_id is None:
            return False

        # Check against integer keys
        try:
            if int(step_id) in self.steps:
                return True
        except (ValueError, TypeError):
            pass

        # Check against string keys
        if str(step_id) in self.steps:
            return True

        return False

    def saveworkflowtos3(self):
        """
        Saves workflow updates to S3 while protecting core sections like 'input_data' and 'workflow'.
        All other keys can be added or updated freely.
        """
        # Keys that must NOT be modified
        unallowed_keys = {"input_data", "workflow"}

        # Read the original workflow JSON from S3
        original_json = read_json_from_s3(self.wf_loc)

        # Update all keys except protected ones
        for key, value in self.workflow_json.items():
            if key not in unallowed_keys:
                original_json[key] = value  # update or add new key

        # Save updated workflow back to S3
        return save_playbook_to_s3(
            original_json,
            self.userid,
            "workflow updated successfully",
            self.filename,
        )

    def execute(self, userinput=None):
        current_step_id = self._get_first_step()
        if not current_step_id:
            self.logger.error("No valid start step found.")
            return

        visited = defaultdict(int)
        MAX_RETRIES_PER_STEP = 2

        # should be list not dict
        all_step_results = []
        if "chat" not in self.workflow_json:
            self.workflow_json["chat"] = []
        if "chat_log" not in self.workflow_json:
            self.workflow_json["chat_log"] = {}
        if "last_ai_discovered" not in self.workflow_json:
            self.workflow_json["last_ai_discovered"] = {}

        while current_step_id:
            visited[current_step_id] += 1
            if visited[current_step_id] > MAX_RETRIES_PER_STEP:
                self.logger.warning(
                    f"Max retries exceeded at step {current_step_id}. Ending execution."
                )
                break

            step = self.steps.get(current_step_id)
            if not step:
                self.logger.error(f"Step ID {current_step_id} not found.")
                break

            # pause if unmet requirements
            needs = step.get("requirements_needed")
            if (
                needs
                and isinstance(needs, list)
                and len(needs) > 0
                and not self.testing
            ):
                self.logger.warning(
                    f"Step '{step['title']}' requires inputs: {needs}. Pausing execution."
                )

                # Return pause state
                return {
                    "status": "paused",
                    "pending_requirements": needs,
                    "execution_log": self.execution_log,
                    "all_step_results": all_step_results,
                    "next_step": current_step_id,
                }

            try:
                self.logger.info(f"Executing step: {step['title']} [{step['id']}]")
                if all_step_results:
                    cu_step = self.steps[current_step_id]
                    if step.get("decision_point"):
                        val = self.ai_decision_Check(
                            userinput=user_input, extracted_id=extracted_id
                        )
                        if "next_step_id" in val:
                            user_input = {val["response_text"]}
                            extracted_id = val["next_step_id"]
                    else:
                        # Extract only argument names for the current step
                        function_args = (
                            cu_step.get("function_call", {}).get("arguments", {}) or {}
                        )
                        arguments_needed = list(function_args.keys())
                        # print("current step arguments", arguments_needed)

                        ai_result = self.ai_execute_helper(
                            all_step_results=all_step_results,
                            arguments_needed=arguments_needed,
                            ai_instructions=cu_step.get("ai_instructions"),
                        )

                        # print("res value to main execute", ai_result)

                        result = self._execute_step(
                            step_id=step["id"], ai_result=ai_result, compl=True
                        )
                else:
                    # print("normal execute")
                    result = self._execute_step(step_id=step["id"], compl=True)

                # ensure next_step exists
                if "next_step" not in result or not result["next_step"]:
                    result["next_step"] = step.get("next_step")

                values = {
                    "step_id": step["id"],
                    "step_title": step["title"],
                    "result": result["execution_details"],
                }

                # store result
                all_step_results.append(values)
                self.execution_log.append(values)

                # move forward
                current_step_id = result.get("next_step")

            except Exception as e:
                self.logger.error(f"Error executing step {step['id']}: {e}")
                fallback = self._find_fallback(step)

                if fallback:
                    self.logger.warning(
                        f"Switching to fallback step: {fallback['title']} [{fallback['id']}]"
                    )
                    current_step_id = fallback["id"]
                else:
                    self.logger.error("No fallback defined. Ending execution.")
                    break

        now = datetime.now()
        chat_entry = {
            "id": self.generate_unique_id([e["id"] for e in self.chat_history]),
            "date": now.isoformat(),
            "input": userinput or "run workflow",
            "output": "workflow executed successfully",
            "status": "success",
            "step_id": "All",
        }

        self.workflow_json["chat"].append(chat_entry)
        new_summary = self.get_chat_summarization()
        # ✅ Correct chat_log handling
        chat_log = self.workflow_json.setdefault("chat_log", {})
        chat_log["last_chat_summarized"] = len(self.workflow_json["chat"])
        chat_log["chat_summarization"] = new_summary
        # --- Save workflow ---
        self.saveworkflowtos3()
        return self.execution_log

    def get_execution_log(self) -> List[Dict[str, Any]]:
        return self.execution_log

    def _get_first_step(self) -> Optional[str]:
        referenced = set()
        for step in self.steps.values():
            ns = step.get("next_step")
            if isinstance(ns, list):
                referenced.update(ns)
            elif isinstance(ns, str):
                referenced.add(ns)
        for step_id in self.steps:
            if step_id not in referenced:
                return step_id
        return None

    def storeargument_results(self, nfunction_args, execution_result=None):
        """
        Store argument values from function arguments and execution result.
        Execution result can be dict, list, primitive, or JSON string.
        """
        try:
            if "pre_user_data" not in self.workflow_json:
                self.workflow_json["pre_user_data"] = {}
            pud = self.workflow_json.setdefault("pre_user_data", {})

            RESERVED = {
                "success",
                "error",
                "message",
                "status",
                "workflow_intent",
                "execution_status",
            }

            CONTACT_KEYS = {
                "email",
                "emails",
                "attendees",
                "receipent_emails",
                "recipient_emails",
            }

            def is_meaningful(v):
                return v not in ("", None, [], {})

            # -------------------------------------------------------------
            # CONTACT EMAIL HANDLER
            # -------------------------------------------------------------
            def add_to_contacts(value):
                """Extract emails from value & replace contacts with the new list."""
                if not value:
                    pud["contacts"] = []
                    return

                def normalize_email(e):
                    if not isinstance(e, str):
                        return None
                    e = e.strip()
                    return e if "@" in e and "." in e else None

                extracted = []

                if isinstance(value, str):
                    em = normalize_email(value)
                    if em:
                        extracted.append(em)

                elif isinstance(value, list):
                    for item in value:
                        em = normalize_email(item)
                        if em:
                            extracted.append(em)

                elif isinstance(value, dict):
                    # attendees = [{"email": "..."}]
                    for val in value.values():
                        em = normalize_email(val)
                        if em:
                            extracted.append(em)

                # Remove duplicates
                extracted = list(dict.fromkeys(extracted))

                # 🔥 REPLACE the contacts instead of merging
                pud["contacts"] = extracted

            # -------------------------------------------------------------
            # GENERIC STORE
            # -------------------------------------------------------------
            def store_value(k, v):
                """Store key/value safely into pre_user_data."""

                # SPECIAL CONTACT RULE
                if k in CONTACT_KEYS:
                    add_to_contacts(v)
                    return

                if k in RESERVED:
                    return
                if not is_meaningful(v):
                    return

                current = pud.get(k)

                # Merge dictionaries
                if isinstance(current, dict) and isinstance(v, dict):
                    for a, b in v.items():
                        if is_meaningful(b):
                            current[a] = b
                    pud[k] = current
                    return

                # Merge lists
                if isinstance(current, list) and isinstance(v, list):
                    merged = list(
                        dict.fromkeys(current + [x for x in v if is_meaningful(x)])
                    )
                    pud[k] = merged
                    return

                # Overwrite with meaningful primitive/dict/list
                pud[k] = v

            # -------------------------------------------------------------
            # RECURSIVE EXTRACTOR
            # -------------------------------------------------------------
            def extract_all(obj):
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if k in RESERVED:
                            continue
                        if isinstance(v, (dict, list)):
                            extract_all(v)
                        else:
                            if is_meaningful(v):
                                store_value(k, v)

                elif isinstance(obj, list):
                    for item in obj:
                        if isinstance(item, (dict, list)):
                            extract_all(item)

            # -------------------------------------------------------------
            # 1. Store all function arguments
            # -------------------------------------------------------------
            for k, v in nfunction_args.items():
                store_value(k, v)

            # -------------------------------------------------------------
            # 2. Store raw execution_result values
            # -------------------------------------------------------------
            if isinstance(execution_result, (dict, list)):
                extract_all(execution_result)

            # -------------------------------------------------------------
            # 3. Parse JSON in return_str field
            # -------------------------------------------------------------
            if execution_result:
                if isinstance(execution_result, dict):
                    ret = execution_result.get("return_str")
                    if isinstance(ret, str):
                        try:
                            import json

                            js = json.loads(ret)
                            extract_all(js)
                        except Exception:
                            pass

                # -------------------------------------------------------------
                # 4. If execution_result is a JSON string itself
                # -------------------------------------------------------------
                if isinstance(execution_result, str):
                    try:
                        import json

                        js = json.loads(execution_result)
                        extract_all(js)
                    except Exception:
                        pass

            # print("pud", pud)
            # print("actual values", self.workflow_json["pre_user_data"])
            return pud

        except Exception as upd_exc:
            # print("Error updating pre_user_data:", upd_exc)
            return None

    def _execute_step(self, step_id, ai_result=None, compl=False) -> Dict[str, Any]:
        step = self.steps[step_id]
        self.current_wf_id = step_id
        function_args = None
        if ai_result:
            function_args = ai_result.get("function_args", {})

        default_message = "Step executed successfully."
        execution_status = "failed"

        try:
            # ==========================================================
            # FUNCTION CALL PATH
            # ==========================================================
            if step.get("function_call"):
                func_call = step.get("function_call", {})
                func_name = func_call.get("function_name")
                nfunction_args = func_call.get("arguments", {}) or {}

                # Merge AI args
                if function_args:
                    nfunction_args.update(function_args)

                # Execute function
                try:
                    execution_result = self._trigger_function(func_name, nfunction_args)
                except Exception as e:
                    execution_result = {"success": False, "error": str(e)}

                # Normalize execution result dict
                if not isinstance(execution_result, dict):
                    execution_result = {
                        "success": True,
                        "return_str": str(execution_result),
                    }

                # print("execution result", execution_result)

                # Normalize "success" string
                success_flag = execution_result.get("success", True)
                if isinstance(success_flag, str):
                    success_flag = success_flag.lower() == "true"

                raw_error = execution_result.get("error")
                is_http_error = raw_error and "HttpError" in str(raw_error)

                has_error = bool(raw_error) or not success_flag or is_http_error

                if has_error:
                    execution_status = "failed"
                    if is_http_error or ("gmail" in func_name.lower()):
                        message = (
                            "There was a problem sending the email. "
                            "Please check if the recipient address is valid and try again."
                        )
                    else:
                        message = (
                            execution_result.get("return_str")
                            or raw_error
                            or default_message
                        )
                else:
                    execution_status = "success"

                    # Different output formats
                    if execution_result is None:
                        message = default_message
                    elif isinstance(execution_result, dict):
                        if "email_body" in execution_result:
                            message = execution_result["email_body"]
                        elif "email_body_html" in execution_result:
                            message = execution_result["email_body_html"]
                        else:
                            message = (
                                execution_result.get("return_str") or default_message
                            )
                    elif isinstance(execution_result, (list, tuple, set)):
                        message = (
                            ", ".join(map(str, execution_result)) or default_message
                        )
                    elif isinstance(execution_result, (int, float, bool, str)):
                        message = str(execution_result) or default_message
                    else:
                        message = str(execution_result) or default_message

                execution_details = execution_result

                self.storeargument_results(
                    nfunction_args=nfunction_args, execution_result=execution_result
                )

            # ==========================================================
            # SELF LEARN STEP PATH
            # ==========================================================
            else:
                self._handle_self_learn(step)
                execution_status = "success"
                execution_details = {"type": "self_learn"}
                message = default_message

            # Final response
            result = {
                "workflow_intent": True,
                "step_id": step_id,
                "message": message,
                "execution_status": execution_status,
                "execution_details": execution_details,
            }

        except Exception as e:
            # ==========================================================
            # FALLBACK LOGIC
            # ==========================================================
            fallback = self._find_fallback(step)
            if fallback:
                try:
                    self._handle_self_learn(fallback)
                    result = {
                        "workflow_intent": True,
                        "step_id": fallback["id"],
                        "message": "Fallback step executed successfully.",
                        "execution_status": "fallback",
                        "execution_details": {"error": str(e)},
                    }
                except Exception:
                    result = {
                        "workflow_intent": False,
                        "step_id": None,
                        "message": "problem in testing the step",
                        "execution_status": "failed",
                        "execution_details": {"error": str(e)},
                    }
            else:
                result = {
                    "workflow_intent": False,
                    "step_id": None,
                    "message": "problem in testing the step",
                    "execution_status": "failed",
                    "execution_details": {"error": str(e)},
                }

        # ==========================================================
        # LOG RESULTS (TESTING / ONLINE)
        # ==========================================================
        if execution_status == "success" and compl:
            now = datetime.now()

            if result.get("step_id"):
                step_id = int(result["step_id"])
                key = str(step_id)

                step = self.steps[step_id]
                title = step.get("title")

                log_entry = {
                    "title": title,
                    "date": now.isoformat(),
                    "input": "complete execution",
                    "output": result.get("message"),
                    "status": execution_status,
                    "details": execution_result or execution_details,
                }
                # print("log entry", log_entry)

                target_section = "testing" if self.testing else "online"
                self.workflow_json.setdefault(target_section, {})
                self.workflow_json[target_section][key] = log_entry

            self.saveworkflowtos3()

        return result

    def _handle_communication(self, step: Dict[str, Any]) -> Dict[str, Any]:
        ai_output = f"[COMMUNICATION] {step['ai_instructions']}"

        # Trigger function call if present
        func_call = step.get("function_call")
        if func_call:
            func_name = func_call["function_name"]
            args = func_call.get("arguments", {})

            # Normalize attendees/contacts if needed
            if "contacts" in args and args["contacts"] == "all":
                args["contacts"] = self.contacts

            self.logger.info(f"Calling function {func_name} with args {args}")
            result = self._trigger_function(func_name, args)
            return {"output": result, "next_step": step.get("next_step")}

        return {"output": ai_output, "next_step": step.get("next_step")}

    def _handle_self_learn(self, step: Dict[str, Any]) -> Dict[str, Any]:
        # Here, you could trigger AI generation / LLM response
        self.logger.info(f"[SELF-LEARN] {step['ai_instructions']}")
        result = get_fireworks_response(step["ai_instructions"], role="system")
        self.ai_made_output[step["id"]] = result
        return {"output": result, "next_step": step.get("next_step")}

    def _handle_navigation(self, step: Dict[str, Any]) -> Dict[str, Any]:
        ai_output = f"[NAVIGATION] Go to {step.get('page_url')}"
        self.logger.info(ai_output)
        return {"output": ai_output, "next_step": step.get("next_step")}

    def _find_fallback(self, step: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        fallback_candidates = [
            s
            for s in self.steps.values()
            if "fallback" in s.get("title", "").lower()
            or "fallback" in s.get("objective", "").lower()
        ]
        for fb in fallback_candidates:
            if fb["id"] != step["id"]:
                return fb
        return None

    def _trigger_function(self, func_name: str, args: dict) -> Any:
        """
        Dynamically calls a service function with given arguments.

        Supports:
        - "automate.somefunc" -> AutoMateService
        - "gmail.sensomeil" -> GmailService
        - "google_meet.some" -> GoogleMeetService
        - "twilio.some" -> TwilioService

        Notes:
        - Automatically replaces "contacts": "all" with self.contacts
        - Handles different constructor requirements per service
        """
        import importlib

        # Map service prefix to module path and class
        service_classes = {
            "automate": ("services.automate_service", "AutoMateService"),
            "gmail": ("services.gmail_service", "GmailService"),
            "google_meet": ("services.meet_service", "GoogleMeetService"),
            "twilio": ("services.twillo_service", "TwilioService"),
        }

        try:
            service_prefix, method_name = func_name.split(".", 1)
        except ValueError:
            raise ValueError(f"Invalid func_name format: {func_name}")

        if service_prefix not in service_classes:
            raise ValueError(f"Service {service_prefix} not recognized")

        module_path, class_name = service_classes[service_prefix]

        # Dynamically import module and class
        module = importlib.import_module(module_path)
        service_class = getattr(module, class_name)

        # print("function triggering in process", func_name, args)

        # Prepare constructor arguments for each service
        if service_prefix == "google_meet":
            contacts = self.get_attendees(self.contacts)
            instance = service_class(
                contacts=contacts,
                userid=self.userid,
                testing=self.testing,
                workflow=self.workflow_json,
                wf_id=self.current_wf_id,
            )
        # print("triggering google service")
        elif service_prefix == "gmail":
            instance = service_class(
                user_id=self.userid,
                connection=None,
                testing=self.testing,
                workflow=self.workflow_json,
                wf_id=self.current_wf_id,
            )
        # print("triggering gmail service")
        elif service_prefix == "automate":
            instance = service_class(
                userid=self.userid,
                testing=self.testing,
                workflow=self.workflow_json,
                wf_id=self.current_wf_id,
            )
        # print("triggering automate service")
        elif service_prefix == "twilio":
            instance = service_class(
                account_sid=self.input_data.get("twilio_account_sid"),
                auth_token=self.input_data.get("twilio_auth_token"),
                from_whatsapp_number=self.input_data.get("twilio_whatsapp_number"),
                from_sms_number=self.input_data.get("twilio_sms_number"),
                from_call_number=self.input_data.get("twilio_call_number"),
                testing=self.testing,
                workflow=self.workflow_json,
            )

        # print("triggering twilio service")
        else:
            raise ValueError(f"Constructor not handled for service {service_prefix}")

        # Get the method
        func = getattr(instance, method_name, None)
        if not func:
            raise ValueError(f"Method '{method_name}' not found in {class_name}")

        attendee_keys = {
            "to_email",
            "attendees",
            "email",
            "emails",
            "contacts",
            "receipent_emails",
        }

        for k, v in list(args.items()):
            if k in attendee_keys:
                # Empty or None → get_attendees()
                if not v or v in ("", None, [], {}):
                    args[k] = self.get_attendees()
                # 'all' or case-insensitive 'ALL' → get_attendees('all')
                elif isinstance(v, str) and v.strip().lower() == "all":
                    args[k] = self.get_attendees("all")

        # print("method name", method_name)
        # print("instance", instance)
        # print("arguments", args)

        # Call the function
        return func(**args)
        # return "ok"

    def check_input_tone(self, user_input: str):
        result = self.ai_input_intent_classifier(userinput=user_input)
        ai_result = {}
        # print("result by input checker", result)

        if result and "intent" in result:
            intent = result["intent"]

            if intent == "normal_conversation":
                convo = self.ai_conversation_handler(userinput=user_input)
                ai_result = {
                    "response_message": convo,
                    "wf_single_runner": False,
                    "workflow_improv": False,
                    "reset": False,
                    "log_status": "normal",
                }
            # print("convo ai_result", ai_result)

            elif intent == "workflow":
                ma_res = self.ai_detect_trigger_type(userinput=user_input)
                if "bs_wf_single_runner" in ma_res and ma_res["bs_wf_single_runner"]:
                    extracted_id = None
                    if "step_id" in ma_res:
                        extracted_id = ma_res["step_id"]
                        step = self.steps.get(int(extracted_id))
                        if step.get("decision_point"):
                            val = self.ai_decision_Check(
                                userinput=user_input, extracted_id=extracted_id
                            )
                            if "next_step_id" in val:
                                valres = val["response_text"]
                                user_input = f"{valres} where {user_input}"
                                extracted_id = val["next_step_id"]

                    route = self.ai_detect_and_route_input(
                        userinput=user_input, extracted_id=extracted_id
                    )
                    # print("base route workflow", route)
                    ai_result = {
                        "response_message": route.get("response_message", ""),
                        "wf_single_runner": bool(route.get("wf_single_runner", False)),
                        "confirm_step": bool(route.get("confirm_step", False)),
                        "step_id": route.get("step_id"),
                        "log_status": "workflow",
                        "trigger_step": route.get("trigger_step", {}),
                    }
                    # ✅ Workflow text execute
                    if "wf_single_runner" in ai_result and ai_result.get(
                        "wf_single_runner"
                    ):
                        # print("running execute from text input")
                        return self.execute_from_text_input(
                            user_input=ai_result["response_message"],
                            base_input=user_input,
                            step_id=ai_result.get("step_id"),
                        )
                elif "bs_workflow_runner" in ma_res and ma_res.get(
                    "bs_workflow_runner"
                ):
                    val = self.ai_pre_gather_details(userinput=user_input)
                    if val is False:
                        # print("running self.execute")
                        return self.execute()
                    else:
                        ai_result = {
                            "response_message": val,
                            "log_status": "enquiry",
                            "step_id": "All",
                        }

                    # ✅ Workflow improvement
                elif "bs_workflow_improv" in ma_res and ma_res.get(
                    "bs_workflow_improv"
                ):
                    # print("running impov request")
                    improv_result = self.update_steps_workflow(user_input)
                    ai_result["response_message"] = improv_result.get(
                        "response_message", str(improv_result)
                    )
                    ai_result["log_status"] = "improv"

            elif intent == "resetStep":
                reset = self.ai_reset_intent_handler(userinput=user_input)

                res = {
                    "response_message": reset.get("message", ""),
                    "reset_needed": reset.get("reset_needed", False),
                    "reset": reset.get("reset"),
                    "step_id": reset.get("step_id"),
                    "clarification_needed": reset.get("clarification_needed", False),
                    "workflow_runner": False,
                    "workflow_improv": False,
                    "improv_input": None,
                }

                if res.get("reset_needed"):
                    ai_result = self.handle_workflow_reset(res, user_input)
            elif intent == "explanation":
                route = self.ai_explain_workflow_steps(userinput=user_input)
                ai_result = {
                    "response_message": route.get("reply", ""),
                    "step_id": route.get("step_id"),
                    "log_status": "explanation",
                }

            else:
                return {"message": "problem with server."}
        else:
            return {"message": "problem with server."}

        # print("log chat check")
        try:
            if (
                ai_result.get("response_message") != ""
                and ai_result.get("response_message") != None
            ):
                now = datetime.now()
                chat_entry = {
                    "id": self.generate_unique_id([e["id"] for e in self.chat_history]),
                    "date": now.isoformat(),
                    "input": user_input,
                    "output": ai_result.get("response_message", ""),
                    "status": ai_result.get("log_status", "normal"),
                    "step_id": ai_result.get("step_id", None),
                    "confirm_step": ai_result.get("confirm_step", False),
                }
                if "chat" not in self.workflow_json:
                    self.workflow_json["chat"] = []
                if "chat_log" not in self.workflow_json:
                    self.workflow_json["chat_log"] = {}
                if "last_ai_discovered" not in self.workflow_json:
                    self.workflow_json["last_ai_discovered"] = {}

                self.workflow_json["chat"].append(chat_entry)
                new_summary = self.get_chat_summarization()
                if "trigger_step" in ai_result:
                    self.workflow_json["last_ai_discovered"] = ai_result["trigger_step"]

                chat_log = self.workflow_json["chat_log"]
                chat_log["last_chat_summarized"] = len(self.workflow_json["chat"])
                chat_log["chat_summarization"] = new_summary
                # print("saving before the details")

                self.saveworkflowtos3()

        except Exception as e:
            self.logger.warning(f"Failed to log chat entry: {e}")
        # print("returning result", ai_result)
        return ai_result

    def execute_from_text_input(self, user_input: str, step_id, base_input=None):
        """
        Executes a workflow step or handles human conversation dynamically.
        Tracks chat history, testing/online logs, execution logs, and saves workflow to S3.
        Skips execution if step already completed today unless `force=True`.
        """
        now = datetime.now()
        main_id_toexecute = None
        base_ai_instruction = None

        # --- Load AI prompt template ---
        template_data = load_yaml_file(path=pathconfig.play_template)
        prompt_instructions = template_data.get("select_and_prepare_step", {}).get(
            "instructions", ""
        )
        if not isinstance(prompt_instructions, str):
            raise TypeError(
                "Invalid template structure: expected string for 'instructions'."
            )

        # --- Ensure workflow JSON structure ---
        if "chat" not in self.workflow_json:
            self.workflow_json["chat"] = []
        if "execution_logs" not in self.workflow_json:
            self.workflow_json["execution_logs"] = []
        if "pre_user_data" not in self.workflow_json:
            self.workflow_json["pre_user_data"] = {}
        chats = self.workflow_json["chat"] or []
        # --- Assume you already have chat history loaded ---
        if chats:
            existing_ids = {entry["id"] for entry in chats}
            chid = self.generate_unique_id(existing_ids)
        else:
            chid = str(uuid.uuid4().int)[0:6]

        # --- Determine previous_data for AI context ---
        if self.testing:
            previous_data = self.workflow_json.get("testing", {})
            if "testing" not in self.workflow_json:
                self.workflow_json["testing"] = {}
        else:
            previous_data = self.workflow_json.get("online", {})
            if "online" not in self.workflow_json:
                self.workflow_json["online"] = {}
        if "workflow" in self.workflow_json:
            if step_id:
                # print("step", type(step_id), step_id)

                # clean + convert step id safely
                if isinstance(step_id, str) and step_id.strip().isdigit():
                    step_id = int(step_id)
                    main_id_toexecute = step_id

                # ensure step_id is int
                if isinstance(step_id, int):
                    pr_workflow = self.steps.get(step_id)
                    base_ai_instruction = pr_workflow.get("ai_instructions")
            else:
                step_match = re.search(r"\bstep\s*(\d+)", user_input, re.IGNORECASE)
                step_id = int(step_match.group(1)) if step_match else None
                if step_id:
                    pr_workflow = self.steps.get(step_id)
        else:
            pr_workflow = self.workflow_json

        lastly_ai_attached = self.workflow_json.get("last_ai_discovered", {}) or {}
        user_made_arguments = self.workflow_json.get("pre_user_data", {})

        # --- Prepare prompt with previous_data ---
        prompt_text = (
            prompt_instructions.replace("{{user_input}}", user_input)
            .replace("{{workflow_json}}", json.dumps(pr_workflow))
            .replace("{{previous_data}}", json.dumps(previous_data))
            .replace("{{ai_instruction}}", base_ai_instruction or "")
            .replace(
                "{{previous_trigger_attachements}}", json.dumps(lastly_ai_attached)
            )
            .replace("{{user_made_arguments}}", json.dumps(user_made_arguments))
        )

        # --- Call AI ---

        # Default result
        result = {
            "workflow_intent": False,
            "step_id": None,
            "message": "I could not understand that input.",
        }
        execution_status = "failed"
        execution_details = {}
        already_done = False
        workflow_intent = False

        ai_result = self.get_parsed_fireworks_response(prompt_text=prompt_text)

        # --- Process AI response ---
        if ai_result:
            # print("ai response", ai_result)

            step_id = ai_result.get("step_id")
            workflow_intent = ai_result.get("workflow_intent", False)
            message = ai_result.get("message", "")
            try:
                step_id = int(step_id)
            except (TypeError, ValueError):
                step_id = None
            if main_id_toexecute and step_id and step_id != main_id_toexecute:
                # print("retrying until i got correct one")
                ai_result = self.get_parsed_fireworks_response(prompt_text=prompt_text)
                step_id = ai_result.get("step_id")
                workflow_intent = ai_result.get("workflow_intent", False)
                message = ai_result.get("message", "")

            if workflow_intent and step_id and self.check_step_exists(step_id):
                result = self._execute_step(step_id=step_id, ai_result=ai_result)
            else:
                execution_status = "success"
                message = (
                    message
                    or "I can only help with workflow tasks. Could you rephrase your request in terms of your workflow?"
                )
                result = {
                    "workflow_intent": False,
                    "step_id": None,
                    "emails": [],
                    "contacts": [],
                    "function_args": {},
                    "force": False,
                    "message": message,
                }
        execution_status = result["execution_status"]
        # --- Record Chat ---
        chat_entry = {
            "id": chid,
            "date": now.isoformat(),
            "input": base_input or user_input,
            "output": result["message"],
            "status": execution_status,
            "step_id": result.get("step_id") or step_id,
        }
        self.workflow_json["chat"].append(chat_entry)
        new_summary = self.get_chat_summarization()
        # ✅ Correct chat_log handling
        chat_log = self.workflow_json.setdefault("chat_log", {})
        chat_log["last_chat_summarized"] = len(self.workflow_json["chat"])
        chat_log["chat_summarization"] = new_summary
        if execution_status == "success" and workflow_intent:
            # --- Record Testing / Online ---
            if result.get("step_id"):
                step_id = int(result["step_id"])
                key = str(step_id)

                step = self.steps[step_id]
                title = step.get("title")

                log_entry = {
                    "title": title,
                    "date": now.isoformat(),
                    "input": user_input,
                    "output": result.get("message"),
                    "status": execution_status,
                    "details": result["execution_details"],
                }

                target_section = "testing" if self.testing else "online"
                self.workflow_json.setdefault(target_section, {})
                self.workflow_json[target_section][key] = log_entry

        # --- Record Execution Logs ---
        self.workflow_json["execution_logs"].append(
            {
                "timestamp": now.isoformat(),
                "input": user_input,
                "output": result["message"],
                "status": execution_status,
                "details": result["execution_details"],
                "step_id": result.get("step_id"),
            }
        )
        self.workflow_json["last_ai_discovered"] = {}

        # --- Save workflow JSON back to S3 ---
        self.saveworkflowtos3()

        return result

    def update_steps_workflow(self, user_input: str):
        from playbook.routes import modify_instruction

        res = modify_instruction(
            ud_inst=user_input,
            user_id=self.userid,
            filename=self.filename,
            add_data=None,
        )
        if res:
            return "workflow updated."
        else:
            return None

        # print(user_input)
        # return user_input
