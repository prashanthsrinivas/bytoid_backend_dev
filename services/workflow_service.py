import asyncio
from collections import defaultdict
from datetime import datetime
import json
import random
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
    get_evaluator_fireworks,
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

PLAY_TEMPLATE = load_yaml_file(path=pathconfig.play_template)

now = datetime.now()


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
        if not PLAY_TEMPLATE:
            return load_yaml_file(path=pathconfig.play_template)
        else:
            return PLAY_TEMPLATE

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

    async def get_chat_summarization(self, chats_obj=None):
        template_data = PLAY_TEMPLATE

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

        result = await self.get_parsed_fireworks_response(prompt_text)
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

    async def ai_input_intent_classifier(self, userinput):
        """
        Classifies user input into one of: workflow, explanation, resetStep, or normal_conversation.
        Includes workflow-level context from input_data to improve domain awareness.
        """
        try:
            template_data = PLAY_TEMPLATE
            prompt_instructions = template_data.get("input_intent_classifier", {})
            # print("prompt inst 273", type(prompt_instructions))
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
            # print("values", prompt_text)
            # Get parsed result from Fireworks
            result = await self.get_parsed_fireworks_response(prompt_text)
            return result
        except Exception as e:
            print("e ai input intent classifier", e)

    async def ai_conversation_handler(self, userinput):
        template_data = PLAY_TEMPLATE
        prompt_instructions = template_data.get("workflow_conversation_handler", {})
        # print("prompt inst 323", type(prompt_instructions))
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
        result = await self.get_parsed_fireworks_response(prompt_text)
        if result and "reply" in result:
            return result["reply"]
        return result

    async def ai_detect_trigger_type(self, userinput):
        template_data = PLAY_TEMPLATE
        prompt_instructions = template_data.get("detect_trigger_type", {})
        # print("prompt vals 349", type(prompt_instructions))
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

        newresultds = await self.get_parsed_fireworks_response(prompt_text)
        print("res workflow detector", newresultds)
        return newresultds

    async def ai_detect_current_step(self, userinput):
        template_data = PLAY_TEMPLATE
        prompt_instructions = template_data.get("detect_current_step", {})
        # print("prompt vals 438", type(prompt_instructions))
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
        result = await self.get_parsed_fireworks_response(prompt_text)
        if result and "step_id" in result:
            return result["step_id"]
        return result

    async def ai_detect_and_route_input(self, userinput, extracted_id=None):
        """
        Detects the workflow step from user input, checks argument availability,
        and determines if the step is ready to execute.
        """
        template_data = PLAY_TEMPLATE
        detect_prompt = template_data.get("detect_and_route_input")
        clarification_prompt_template = template_data.get("step_clarification_prompt")

        if not isinstance(detect_prompt, str):
            raise TypeError(
                "Invalid template structure: 'detect_and_route_input' must be a string."
            )

        # Get current chat history
        chats_obj = self.get_current_chats()
        new_chat = chats_obj.get("chat", [])
        last_chat = new_chat[-1] if new_chat else []
        print("exttacted id", extracted_id, userinput)

        # Base workflow data and previous execution context
        if extracted_id:
            baseworkflow = self.steps[extracted_id]
        else:
            baseworkflow = self.workflow_json.get("workflow", {})
        base_ai_instruction = None
        lastly_ai_attached = self.workflow_json.get("last_ai_discovered", {}) or {}
        if "pre_user_data" not in self.workflow_json:
            self.workflow_json["pre_user_data"] = {}
        user_made_arguments = self.workflow_json.get("pre_user_data", {})

        inputdata = self.input_data
        now = datetime.now()
        todays_date = now.isoformat()

        # Step titles (if needed)
        steptitles = [
            {
                str(step["id"]): {
                    "title": step["title"],
                    "description": step["objective"],
                }
            }
            for _, step in self.steps.items()
        ]

        # Modify user input to include step_id if available
        modinput = userinput
        if extracted_id:
            modinput = f"{userinput} - so execute step {extracted_id}"

        # Build detect & route prompt
        def build_detect_prompt(u_input):
            return (
                detect_prompt.replace("{{user_input}}", u_input)
                .replace(
                    "{{workflow_json}}", json.dumps(self.workflow_json.get("workflow"))
                )
                .replace("{{previous_data}}", json.dumps(self.previous_data))
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
                .replace("{{workflow_titles}}", json.dumps(steptitles))
                .replace("{{input_data.title}}", inputdata.get("title", ""))
                .replace("{{input_data.description}}", inputdata.get("description", ""))
                .replace("{{input_data.category}}", inputdata.get("category", ""))
                .replace("{{input_data.tags}}", ", ".join(inputdata.get("tags", [])))
                .replace("{{todays_datetime}}", str(todays_date))
                .replace(
                    "{{force_reexecute_step_id}}",
                    str(extracted_id) if extracted_id else "",
                )
            ).strip()

        # Call AI to detect step
        ai_result = await self.get_parsed_fireworks_response(
            build_detect_prompt(modinput)
        )
        print("ai detect initial:", ai_result)
        if ai_result["step_id"] != extracted_id:
            ai_result = await self.get_eval_parsed_fireworks_response(
                build_detect_prompt(modinput)
            )
            print("ai_eval detect")
        # If AI detects a step but arguments missing, optionally use step_clarification_prompt
        if ai_result.get("clarification_needed") and clarification_prompt_template:
            step_id = ai_result.get("step_id")
            step_data = self.steps.get(step_id, {}) if step_id else {}
            clarification_prompt_text = clarification_prompt_template.replace(
                "{{step_id}}", str(step_id or "")
            )
            clarification_prompt_text = clarification_prompt_text.replace(
                "{{step_data}}", json.dumps(step_data)
            )
            clarification_prompt_text = clarification_prompt_text.replace(
                "{{user_input}}", userinput
            )
            clarification_prompt_text = clarification_prompt_text.replace(
                "{{previous_data}}", json.dumps(self.previous_data)
            )
            clarification_prompt_text = clarification_prompt_text.replace(
                "{{previous_trigger_attachements}}", json.dumps(lastly_ai_attached)
            )
            clarification_prompt_text = clarification_prompt_text.replace(
                "{{user_made_arguments}}", json.dumps(user_made_arguments)
            )
            clarification_prompt_text = clarification_prompt_text.replace(
                "{{input_data.title}}", inputdata.get("title", "")
            )
            clarification_prompt_text = clarification_prompt_text.replace(
                "{{input_data.description}}", inputdata.get("description", "")
            )
            clarification_prompt_text = clarification_prompt_text.replace(
                "{{todays_datetime}}", str(todays_date)
            )

            # Call AI for human-friendly clarification question
            clarification_result = await self.get_parsed_fireworks_response(
                clarification_prompt_text
            )
            print("clarification AI result:", clarification_result)
            ai_result["clarification_message"] = clarification_result.get("message", "")

        return ai_result

    async def ai_decision_Check(self, userinput, extracted_id=None):
        # Load template
        template_data = PLAY_TEMPLATE
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
        ai_result = await self.get_parsed_fireworks_response(build_prompt(userinput))
        # print("AI Decision Result:", ai_result)

        # Default: return AI evaluation
        return ai_result

    async def ai_explain_workflow_steps(self, userinput):
        template_data = PLAY_TEMPLATE
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

        newresultds = await self.get_parsed_fireworks_response(prompt_text)
        # print("res explanation", newresultds)
        return newresultds

    async def ai_reset_intent_handler(self, userinput):
        template_data = PLAY_TEMPLATE
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

        newresultds = await self.get_parsed_fireworks_response(prompt_text)
        print("res reset", newresultds)
        return newresultds

    async def ai_pre_gather_details(self, userinput):
        template_data = PLAY_TEMPLATE
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
        newresultds = await self.get_parsed_fireworks_response(prompt_text)
        # print("res", newresultds)
        resolved_inputs = newresultds.get("resolved_inputs", {}) or {}
        for key, val in resolved_inputs.items():
            if (
                key not in self.workflow_json["pre_user_data"]
                or self.workflow_json["pre_user_data"][key] != val
            ):
                self.workflow_json["pre_user_data"][key] = val

        self.saveworkflowtos3()

        return False

    async def ai_execute_helper(
        self, all_step_results, arguments_needed, ai_instructions
    ):
        template_data = PLAY_TEMPLATE
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

        result = await self.get_parsed_fireworks_response(formatted_prompt)
        # print("res ai_execute_helper", result)
        return result

    async def get_parsed_fireworks_response(self, prompt_text, role="system", temp=0.3):
        """
        Get and parse Fireworks response.
        Retries once if the response is empty, invalid, or {}.
        """
        for attempt in range(2):
            response_text = await get_fireworks_response2(
                user_message=prompt_text, role=role, temp=temp, user_id=self.userid
            )

            if not response_text:
                print(f"[Retry {attempt+1}] Empty response from Fireworks. ")
                await asyncio.sleep(0.3)
                continue

            response_text = response_text.strip()
            response_text = re.sub(
                r"^```(?:json)?\s*|\s*```$", "", response_text, flags=re.MULTILINE
            ).strip()

            try:
                ai_result = json.loads(response_text)
                if not ai_result:
                    print(f"[Retry {attempt+1}] Empty JSON object from Fireworks.")
                    await asyncio.sleep(0.3)
                    continue

                return ai_result  # ✅ Valid response

            except json.JSONDecodeError:
                print(
                    f"[Retry {attempt+1}] Failed to parse JSON response.{response_text}"
                )

                await asyncio.sleep(0.3)

                continue

        return {}

    async def get_eval_parsed_fireworks_response(
        self, prompt_text, role="system", temp=0.3
    ):
        """
        Get and parse Fireworks response.
        Retries once if the response is empty, invalid, or {}.
        """
        for attempt in range(2):
            response_text = await get_evaluator_fireworks(
                response_text=prompt_text, role=role, temp=temp, user_id=self.userid
            )

            if not response_text:
                print(f"[Retry {attempt+1}] Empty response from Fireworks. ")
                await asyncio.sleep(0.3)
                continue

            response_text = response_text.strip()
            response_text = re.sub(
                r"^```(?:json)?\s*|\s*```$", "", response_text, flags=re.MULTILINE
            ).strip()

            try:
                ai_result = json.loads(response_text)
                if not ai_result:
                    print(f"[Retry {attempt+1}] Empty JSON object from Fireworks.")
                    await asyncio.sleep(0.3)
                    continue

                return ai_result  # ✅ Valid response

            except json.JSONDecodeError:
                print(
                    f"[Retry {attempt+1}] Failed to parse JSON response.{response_text}"
                )

                await asyncio.sleep(0.3)

                continue

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
        # return original_json

    async def execute(self, userinput=None):
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

                        ai_result = await self.ai_execute_helper(
                            all_step_results=all_step_results,
                            arguments_needed=arguments_needed,
                            ai_instructions=cu_step.get("ai_instructions"),
                        )

                        # print("res value to main execute", ai_result)

                        result = await self._execute_step(
                            step_id=step["id"], ai_result=ai_result, compl=True
                        )
                else:
                    # print("normal execute")
                    result = await self._execute_step(step_id=step["id"], compl=True)

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
        new_summary = await self.get_chat_summarization()
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
        Store execution-safe argument values into pre_user_data.
        Guarantees flat structure: {key: value}, never {key: {key: value}}.
        Explicitly avoids question-related content.
        """
        try:
            # -------------------------------------------------------------
            # INIT
            # -------------------------------------------------------------
            self.workflow_json.setdefault("pre_user_data", {})
            pud = self.workflow_json["pre_user_data"]
            PLACEHOLDER_PATTERNS = (
                "{step_",
                "{workflow",
                "{execution",
                "{context",
            )

            def is_placeholder_value(v):
                if not isinstance(v, str):
                    return False
                v = v.strip()
                if not v:
                    return False
                if v.startswith("{") and v.endswith("}"):
                    return True
                return any(p in v for p in PLACEHOLDER_PATTERNS)

            RESERVED = {
                "success",
                "error",
                "message",
                "status",
                "workflow_intent",
                "execution_status",
                "return_str",
                "user_input",
            }

            CONTACT_KEYS = {
                "email",
                "emails",
                "attendees",
                "receipent_emails",
                "recipient_emails",
            }

            # 🚫 QUESTION FILTERS
            QUESTION_KEYS = {
                "question",
                "questions",
                "questions_mcq",
                "questions_normal",
                "questions_quiz",
                "options",
                "answer",
                "answers",
                "correct_answer",
                "explanation",
            }

            QUESTION_VALUE_HINTS = (
                "A)",
                "B)",
                "C)",
                "D)",
                "Section",
                "Multiple Choice",
                "MCQ",
            )

            QUESTION_ID_PREFIXES = ("qid_",)

            # -------------------------------------------------------------
            # HELPERS
            # -------------------------------------------------------------
            def is_meaningful(v):
                return v not in ("", None, [], {})

            def is_question_key(k):
                k = k.lower()
                return (
                    k in QUESTION_KEYS
                    or k.startswith("question_")
                    or k.endswith("_question")
                )

            def is_question_value(v):
                if isinstance(v, str):
                    if v.startswith(QUESTION_ID_PREFIXES):
                        return True
                    return any(h in v for h in QUESTION_VALUE_HINTS)
                if isinstance(v, list):
                    return any(is_question_value(x) for x in v)
                if isinstance(v, dict):
                    return any(is_question_key(k) for k in v.keys())
                return False

            # -------------------------------------------------------------
            # CONTACT HANDLER
            # -------------------------------------------------------------
            def add_to_contacts(value):
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
                    for val in value.values():
                        em = normalize_email(val)
                        if em:
                            extracted.append(em)

                pud["input_data"]["contacts"] = list(dict.fromkeys(extracted))

            # -------------------------------------------------------------
            # SAFE STORE (FLAT ONLY)
            # -------------------------------------------------------------
            def store_value(k, v):
                # HARD BLOCKS
                if k in RESERVED:
                    return
                if is_question_key(k) or is_question_value(v):
                    return
                if not is_meaningful(v):
                    return

                # # CONTACTS
                if k in CONTACT_KEYS:
                    add_to_contacts(v)
                    return
                if is_placeholder_value(v):
                    return

                # 🚫 NEVER STORE DICTS AS VALUES
                if isinstance(v, dict):
                    return

                # 🚫 NEVER NEST SAME KEY
                if isinstance(pud.get(k), dict):
                    return

                pud[k] = v

            # -------------------------------------------------------------
            # RECURSIVE FLATTENER
            # -------------------------------------------------------------
            def extract_all(obj):
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if k in RESERVED:
                            continue
                        if is_question_key(k) or is_question_value(v):
                            continue

                        # 🔥 ONLY recurse, never store dict wrapper
                        if isinstance(v, dict):
                            extract_all(v)
                        elif isinstance(v, list):
                            extract_all(v)
                        else:
                            store_value(k, v)

                elif isinstance(obj, list):
                    for item in obj:
                        extract_all(item)

            # -------------------------------------------------------------
            # 1️⃣ FUNCTION ARGS
            # -------------------------------------------------------------
            for k, v in nfunction_args.items():
                if isinstance(v, dict):
                    extract_all(v)
                else:
                    store_value(k, v)

            # -------------------------------------------------------------
            # 2️⃣ EXECUTION RESULT
            # -------------------------------------------------------------
            if isinstance(execution_result, (dict, list)):
                extract_all(execution_result)

            return pud

        except Exception:
            return None

    async def _execute_step(
        self, step_id, ai_result=None, compl=False
    ) -> Dict[str, Any]:
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
                    execution_result = await self._trigger_function(
                        step_id=step_id, func_name=func_name, args=nfunction_args
                    )
                except Exception as e:
                    execution_result = {"success": False, "error": str(e)}

                # print("execution result", execution_result)
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
                        elif "questions" in execution_result:
                            # 👇 surface questions to chat
                            questions = execution_result["questions"]
                            message = questions
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
                await self._handle_self_learn(step)
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
        stepid = step.get("id")
        if func_call:
            func_name = func_call["function_name"]
            args = func_call.get("arguments", {})

            # Normalize attendees/contacts if needed
            if "contacts" in args and args["contacts"] == "all":
                args["contacts"] = self.contacts

            self.logger.info(f"Calling function {func_name} with args {args}")
            result = self._trigger_function(
                step_id=stepid, func_name=func_name, args=args
            )
            return {"output": result, "next_step": step.get("next_step")}

        return {"output": ai_output, "next_step": step.get("next_step")}

    async def _handle_self_learn(self, step: Dict[str, Any]) -> Dict[str, Any]:
        # Here, you could trigger AI generation / LLM response
        self.logger.info(f"[SELF-LEARN] {step['ai_instructions']}")
        result = await get_fireworks_response(
            step["ai_instructions"], role="system", user_id=self.userid
        )
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

    async def _trigger_function(self, step_id, func_name: str, args: dict) -> Any:
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
                wf_id=step_id,
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

        print("method name", method_name)
        print("instance", instance)
        print("arguments", args)
        print("step currently", step_id)

        # Call the function
        import inspect

        result = func(**args)

        if inspect.isawaitable(result):
            result = await result

        return result

        # return "ok"

    async def check_input_tone(self, user_input: str):
        result = await self.ai_input_intent_classifier(userinput=user_input)
        ai_result = {}
        print("result by input checker", result)

        if result and "intent" in result:
            intent = result["intent"]

            if intent == "normal_conversation":
                convo = await self.ai_conversation_handler(userinput=user_input)
                ai_result = {
                    "response_message": convo,
                    "wf_single_runner": False,
                    "workflow_improv": False,
                    "reset": False,
                    "log_status": "normal",
                }
                print("convo ai_result", "phase 2 normal")

            elif intent == "workflow":
                ma_res = await self.ai_detect_trigger_type(userinput=user_input)
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

                    route = await self.ai_detect_and_route_input(
                        userinput=user_input, extracted_id=extracted_id
                    )
                    print("base route workflow", route)
                    ai_result = {
                        "response_message": route.get("response_message", ""),
                        "wf_single_runner": bool(route.get("wf_single_runner", False)),
                        "confirm_step": bool(route.get("confirm_step", False)),
                        "step_id": route.get("step_id"),
                        "log_status": "workflow",
                        "trigger_step": route.get("trigger_step", {}),
                        "clarification_needed": bool(
                            route.get("clarification_needed", False)
                        ),
                        "clarification_message": route.get("clarification_message", ""),
                    }
                    # -------------------------------
                    # Decide execution vs clarification
                    # -------------------------------
                    if ai_result.get("wf_single_runner") and not ai_result.get(
                        "clarification_needed"
                    ):
                        print("ai result data", ai_result)
                        return await self.execute_from_text_input(
                            user_input=user_input,
                            base_input=user_input,
                            step_id=ai_result.get("step_id"),
                        )
                elif "bs_workflow_runner" in ma_res and ma_res.get(
                    "bs_workflow_runner"
                ):
                    val = await self.ai_pre_gather_details(userinput=user_input)
                    if val is False:
                        # print("running self.execute")
                        return await self.execute()
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
                reset = await self.ai_reset_intent_handler(userinput=user_input)

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
                route = await self.ai_explain_workflow_steps(userinput=user_input)
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
            response_msg = ai_result.get("response_message") or ai_result.get(
                "clarification_message", ""
            )
            if response_msg:
                now = datetime.now()
                chat_entry = {
                    "id": self.generate_unique_id([e["id"] for e in self.chat_history]),
                    "date": now.isoformat(),
                    "input": user_input,
                    "output": response_msg,
                    "status": ai_result.get("log_status", "normal"),
                    "step_id": ai_result.get("step_id"),
                    "confirm_step": ai_result.get("confirm_step", False),
                }

                self.workflow_json.setdefault("chat", [])
                self.workflow_json.setdefault("chat_log", {})
                self.workflow_json.setdefault("last_ai_discovered", {})

                self.workflow_json["chat"].append(chat_entry)

                # Update last_ai_discovered if trigger_step exists
                if ai_result.get("trigger_step"):
                    self.workflow_json["last_ai_discovered"] = ai_result["trigger_step"]

                # Update chat summary
                new_summary = await self.get_chat_summarization()
                chat_log = self.workflow_json["chat_log"]
                chat_log["last_chat_summarized"] = len(self.workflow_json["chat"])
                chat_log["chat_summarization"] = new_summary

                self.saveworkflowtos3()

        except Exception as e:
            self.logger.warning(f"Failed to log chat entry: {e}")

        # Return AI result for frontend or further processing
        return ai_result

    async def execute_from_text_input(self, user_input: str, step_id, base_input=None):
        """
        Executes a workflow step or handles human conversation dynamically.
        Step selection is LOCKED if step_id is provided.
        AI is used ONLY for argument preparation.
        """

        base_ai_instruction = None
        locked_step_id = None

        # ------------------------------------------------------------------
        # Load AI prompt template
        # ------------------------------------------------------------------
        template_data = load_yaml_file(path=pathconfig.play_template)
        prompt_instructions = template_data.get("select_and_prepare_step", {}).get(
            "instructions", ""
        )

        if not isinstance(prompt_instructions, str):
            raise TypeError(
                "Invalid template structure: expected string for 'instructions'."
            )

        # ------------------------------------------------------------------
        # Ensure workflow JSON structure
        # ------------------------------------------------------------------
        self.workflow_json.setdefault("chat", [])
        self.workflow_json.setdefault("execution_logs", [])
        self.workflow_json.setdefault("pre_user_data", {})

        chats = self.workflow_json["chat"]

        # Generate chat id
        if chats:
            existing_ids = {entry["id"] for entry in chats}
            chid = self.generate_unique_id(existing_ids)
        else:
            chid = str(uuid.uuid4().int)[0:6]

        # ------------------------------------------------------------------
        # Determine previous execution context
        # ------------------------------------------------------------------
        if self.testing:
            previous_data = self.workflow_json.setdefault("testing", {})
        else:
            previous_data = self.workflow_json.setdefault("online", {})

        # ------------------------------------------------------------------
        # Resolve and LOCK step
        # ------------------------------------------------------------------
        pr_workflow = None

        if step_id is not None:
            # sanitize step_id
            if isinstance(step_id, str) and step_id.strip().isdigit():
                step_id = int(step_id)

            if not isinstance(step_id, int):
                raise ValueError("step_id must be an integer if provided")

            if not self.check_step_exists(step_id):
                raise ValueError(f"Step {step_id} does not exist in workflow")

            locked_step_id = step_id
            pr_workflow = self.steps[step_id]
            base_ai_instruction = pr_workflow.get("ai_instructions")

        else:
            # no step explicitly provided → allow AI to infer
            pr_workflow = self.workflow_json

        lastly_ai_attached = self.workflow_json.get("last_ai_discovered", {}) or {}
        user_made_arguments = self.workflow_json.get("pre_user_data", {})

        # ------------------------------------------------------------------
        # Prepare prompt
        # ------------------------------------------------------------------
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

        # ------------------------------------------------------------------
        # Default result
        # ------------------------------------------------------------------
        result = {
            "workflow_intent": False,
            "step_id": None,
            "message": "I could not understand that input.",
        }

        execution_status = "failed"
        workflow_intent = False

        # ------------------------------------------------------------------
        # Call AI (ARGUMENT PREPARATION ONLY)
        # ------------------------------------------------------------------
        print("user inputs", user_input, step_id)
        ai_result = await self.get_parsed_fireworks_response(
            prompt_text=prompt_text, temp=0.2
        )
        print("ai _ result", ai_result)

        # ------------------------------------------------------------------
        # Process AI response
        # ------------------------------------------------------------------
        if ai_result:
            workflow_intent = bool(ai_result.get("workflow_intent", False))
            message = ai_result.get("message", "")

            # 🔒 HARD STEP LOCK
            if locked_step_id is not None:
                ai_result["step_id"] = locked_step_id
                step_id = locked_step_id
            else:
                # only allow AI step if not locked
                try:
                    step_id = int(ai_result.get("step_id"))
                except (TypeError, ValueError):
                    step_id = None

            if workflow_intent and step_id and self.check_step_exists(step_id):
                result = await self._execute_step(step_id=step_id, ai_result=ai_result)
            else:
                execution_status = "success"
                result = {
                    "workflow_intent": False,
                    "step_id": None,
                    "emails": [],
                    "contacts": [],
                    "function_args": {},
                    "force": False,
                    "message": message
                    or "I can only help with workflow-related tasks.",
                }

        execution_status = result.get("execution_status", execution_status)

        # ------------------------------------------------------------------
        # Record chat
        # ------------------------------------------------------------------
        chat_entry = {
            "id": chid,
            "date": now.isoformat(),
            "input": base_input or user_input,
            "output": result.get("message"),
            "status": execution_status,
            "step_id": result.get("step_id") or step_id,
        }

        self.workflow_json["chat"].append(chat_entry)

        # Summarize chat
        new_summary = await self.get_chat_summarization()
        chat_log = self.workflow_json.setdefault("chat_log", {})
        chat_log["last_chat_summarized"] = len(self.workflow_json["chat"])
        chat_log["chat_summarization"] = new_summary

        # ------------------------------------------------------------------
        # Record testing / online execution
        # ------------------------------------------------------------------
        if execution_status == "success" and workflow_intent and result.get("step_id"):
            step_id = int(result["step_id"])
            step = self.steps[step_id]

            log_entry = {
                "title": step.get("title"),
                "date": now.isoformat(),
                "input": user_input,
                "output": result.get("message"),
                "status": execution_status,
            }

            target_section = "testing" if self.testing else "online"
            self.workflow_json.setdefault(target_section, {})
            self.workflow_json[target_section][str(step_id)] = log_entry

        # ------------------------------------------------------------------
        # Record execution logs
        # ------------------------------------------------------------------
        # self.workflow_json["execution_logs"].append(
        #     {
        #         "timestamp": now.isoformat(),
        #         "input": user_input,
        #         "output": result.get("message"),
        #         "status": execution_status,
        #         "step_id": result.get("step_id"),
        #     }
        # )

        self.workflow_json["last_ai_discovered"] = {}

        # ------------------------------------------------------------------
        # Persist workflow
        # ------------------------------------------------------------------
        self.saveworkflowtos3()

        return result

    async def update_steps_workflow(self, user_input: str):
        from playbook.routes import modify_instruction

        res = await modify_instruction(
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

    def _are_all_questions_answered(self):
        execution_data = self.previous_data

        for step_data in execution_data.values():
            outputs = step_data.get("output", [])
            if not isinstance(outputs, list):
                continue

            for q in outputs:
                if "answer" in q and q["answer"] is None:
                    return False

        return True

    async def answer_questions(self, answer: str, qid: str, chid: str):
        execution_data = self.previous_data
        chats = self.chat_history

        execution_updated = False
        last_step_id = None

        # -------------------------------------------------
        # 1. UPDATE EXECUTION DATA (SOURCE OF TRUTH)
        # -------------------------------------------------
        if isinstance(execution_data, dict):
            for step_id, step_data in execution_data.items():
                outputs = step_data.get("output", [])
                if not isinstance(outputs, list):
                    continue

                for q in outputs:
                    if q.get("id") == qid:
                        q["answer"] = answer
                        execution_updated = True
                        last_step_id = step_id
                        break

                if execution_updated:
                    break

        if not execution_updated:
            return {
                "status": "error",
                "message": f"Question ID '{qid}' not found",
                "qid": qid,
            }

        # -------------------------------------------------
        # 2. UPDATE CHAT HISTORY (SECONDARY)
        # -------------------------------------------------
        for chat in chats:
            if str(chat.get("id")) == str(chid):
                outputs = chat.get("output", [])
                if not isinstance(outputs, list):
                    continue

                for out in outputs:
                    if out.get("id") == qid:
                        out["answer"] = answer
                        break

        # -------------------------------------------------
        # 3. CHECK IF ALL QUESTIONS ANSWERED
        # -------------------------------------------------
        all_answered = self._are_all_questions_answered()

        message = (
            "Answer saved successfully."
            if not all_answered
            else "All questions have been answered. Workflow can now proceed."
        )

        if all_answered:
            self.chat_history.append(
                {
                    "id": uuid.uuid4().hex,
                    "date": now.isoformat(),
                    "input": f"answering ....",
                    "output": message,
                    "status": "success",
                    "step_id": last_step_id,
                }
            )

        # -------------------------------------------------
        # 4. PERSIST
        # -------------------------------------------------
        self.previous_data = execution_data
        self.chat_history = chats
        self.saveworkflowtos3()

        return {
            "status": "success",
            "all_questions_answered": all_answered,
            "message": message,
        }

    async def answer_questions_bulk(self, answers: list, chid: str):
        execution_data = self.previous_data
        chats = self.chat_history

        answer_map = {
            item.get("question_id"): item.get("answer")
            for item in answers
            if item.get("question_id") is not None
        }

        last_step_id = None

        # -------------------------------------------------
        # 1. UPDATE EXECUTION DATA
        # -------------------------------------------------
        for step_id, step_data in execution_data.items():
            outputs = step_data.get("output", [])
            if not isinstance(outputs, list):
                continue

            for q in outputs:
                qid = q.get("id")
                if qid in answer_map:
                    q["answer"] = answer_map[qid]
                    last_step_id = step_id

        # -------------------------------------------------
        # 2. UPDATE CHAT HISTORY
        # -------------------------------------------------
        for chat in chats:
            if str(chat.get("id")) == str(chid):
                outputs = chat.get("output", [])
                if not isinstance(outputs, list):
                    continue

                for out in outputs:
                    qid = out.get("id")
                    if qid in answer_map:
                        out["answer"] = answer_map[qid]

        # -------------------------------------------------
        # 3. CHECK IF ALL QUESTIONS ANSWERED
        # -------------------------------------------------
        all_answered = self._are_all_questions_answered()

        message = (
            "Answers saved successfully."
            if not all_answered
            else "All questions have been answered. Workflow can now proceed."
        )

        if all_answered:
            self.chat_history.append(
                {
                    "id": uuid.uuid4().hex,
                    "date": now.isoformat(),
                    "input": f"answering ....",
                    "output": message,
                    "status": "success",
                    "step_id": last_step_id,
                }
            )

        # -------------------------------------------------
        # 4. PERSIST
        # -------------------------------------------------
        self.previous_data = execution_data
        self.chat_history = chats
        self.saveworkflowtos3()

        return {
            "status": "success",
            "all_questions_answered": all_answered,
            "message": message,
        }
