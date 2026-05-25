import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import random
import os, time
from typing import *
import re
from cust_helpers import pathconfig
from db.db_checkers import fetch_contacts_by_user, fetch_user_Social, get_userinfo
from db.rds_db import connect_to_rds
from playbook.helperzz import save_execution_playbook_to_s3, save_playbook_to_s3
from utils.base_logger import get_logger
from utils.app_configs import IS_DEV
from utils.fireworkzz import (
    get_fireworks_response,
    get_fireworks_response2,
    get_evaluator_fireworks,
    get_think_bedrock_vision_image,
)
from utils.normal import (
    can_reply_to_email,
    load_yaml_file,
    read_function_jsons,
    read_function_jsons2,
)
from utils.s3_utils import read_json_from_s3, attach_CLDFRNT_url
from dotenv import load_dotenv
import copy, uuid, traceback

load_dotenv()

PLAY_TEMPLATE = load_yaml_file(path=pathconfig.play_template)

now = datetime.now()


def base_name(filename):
    name_without_ext = os.path.splitext(filename)[0]

    # Always take first 8 characters (playbook ID)
    return name_without_ext[:8]


class WorkflowRunnerV2:
    def __init__(
        self,
        userid: str = None,
        filename: str = None,
        workflowJson=None,
        contacts=None,
        testing=False,
        on_update=None,
        execution_id=None,
        execution_unique_key=None,
        db=None,
        credits=None,
        user_id: str = None,
    ):
        self.userid = userid or user_id
        self.filename = filename
        self.credits = credits
        self.execution_id = execution_id
        self.connection = db or connect_to_rds()
        self.basename = base_name(filename)
        self.wf_loc = f"{userid}/workflow/{self.basename}/{filename}"
        self.on_loc = f"{userid}/workflow/{self.basename}/{execution_id}.json"
        base_workflow = workflowJson or read_json_from_s3(self.wf_loc)
        self.workflow_json = copy.deepcopy(base_workflow)
        self.userdetails = get_userinfo(self.userid)
        self.contacts = contacts or fetch_contacts_by_user(self.userid)
        self.testing = testing
        self.current_wf_id = None
        self.execution_unique_key = execution_unique_key
        # Correctly load steps from workflow['steps'] instead of top-level steps
        workflow_steps = self.workflow_json.get("workflow", {}).get("steps", [])
        self.steps = {step["id"]: step for step in workflow_steps}
        self.step_order = {step["id"]: idx for idx, step in enumerate(workflow_steps)}
        self.input_data = self.workflow_json.get("input_data", {})
        self.chat_history = self.workflow_json.get("chat", [])
        self.chat_log = self.workflow_json.get("chat_log", {})
        self.execution_log: list[dict] = []
        self.previous_data = self.get_current_execution_data()
        self.logger = get_logger(__name__, log_level="DEBUG" if IS_DEV else "INFO")
        self.ai_made_output = {}
        self.current_implemented_functions = read_function_jsons()
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.execution_date = self.started_at.split("T")[0]
        self.on_update = on_update
        if not self.testing:
            self.workflow_json["input_data"]["contacts"] = self.contacts

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
        else:
            if self.execution_id and not self.testing:
                exec_json = read_json_from_s3(self.on_loc) or {}
                return exec_json
        # return original_json

    def fetchusersocialandtimezone(self):
        if not self.connection:
            self.connection = connect_to_rds()
        main_user_account_type = fetch_user_Social(
            user_id=self.userid, connection=self.connection
        )
        timezone = None
        if main_user_account_type == "google":
            from services.meet_service import GoogleMeetService

            timezone = GoogleMeetService.get_user_timezone()
        elif main_user_account_type == "microsoft":
            from services.microsoft_calender_service import (
                MicrosoftGraphCalendarService,
            )

            timezone = MicrosoftGraphCalendarService.get_user_timezone()
        else:
            timezone = "UTC"
        return main_user_account_type, timezone

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

    def _get_next_uncompleted_step(self):
        """
        Environment-agnostic:
        Supports both testing format:
        { "1": {...}, "2": {...} }
        and online format:
        { "steps": { "1": {...}, "2": {...} } }
        """
        execution_data = self.previous_data or {}
        # print("execution data", execution_data)

        # 🔹 Normalize completed steps
        if "steps" in execution_data and isinstance(execution_data["steps"], dict):
            completed_steps = execution_data["steps"]  # online
        else:
            completed_steps = execution_data  # testing

        completed_ids = set(str(k) for k in completed_steps.keys())
        # print("completed ids", completed_ids)

        # 🔹 Always respect workflow order (use insertion order for UUID ids)
        ordered_steps = sorted(
            self.steps.values(),
            key=lambda s: self.step_order.get(s.get("id"), 0),
        )
        # print("ordered steps", ordered_steps)

        for step in ordered_steps:
            step_id = str(step.get("id"))
            if step_id not in completed_ids:
                return step_id

        return None

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
            self.logger.error(
                "ai input intent classifier error: %s", e, exc_info=IS_DEV
            )

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
                baseworkflow = self.get_step_data(last_step)
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
        # print("res workflow detector", newresultds)
        return newresultds

    def update_statuscount(self, count, status):
        workflow_json = self.workflow_json or {}
        autotest = workflow_json.get("autotest", {})

        # ✅ use incoming values
        if count is not None:
            autotest["count"] = count

        if status is not None:
            autotest["status"] = status

        workflow_json["autotest"] = autotest
        self.workflow_json = workflow_json

        return self.saveworkflowtos3()

    async def autocheckerworkflow(self):
        template_data = PLAY_TEMPLATE
        prompt_instructions = template_data.get("autoworkflow_initiator")

        if not isinstance(prompt_instructions, str):
            raise TypeError(
                "Invalid template structure: expected string for 'autoworkflow_initiator'."
            )

        # =========================
        # Unified execution context
        # =========================
        execution_data = self.previous_data or {}
        workflow_json = self.workflow_json or {}
        workflow = workflow_json.get("workflow", {})
        inputdata = self.input_data or {}
        steps = self.steps or {}

        # =========================
        # Detect next uncompleted step
        # =========================
        next_step_id = self._get_next_uncompleted_step()
        # print("next step", next_step_id)

        # =========================
        # ✅ ALL STEPS COMPLETED
        # =========================
        if not next_step_id or next_step_id not in steps:
            autotest = workflow_json.get("autotest", {})

            count = autotest.get("count", 0)
            status = autotest.get("status", False)

            if status and count > 0:
                val = count - 1
                autotest["count"] = val
                if val == 0:
                    autotest["status"] = False

                workflow_json["autotest"] = autotest

                self.saveworkflowtos3()

                if count > 1:
                    return "clear all steps for retry"
            # return "All steps in the workflow have been completed successfully."

        # =========================
        # Chat context (tone only)
        # =========================
        chats_obj = self.get_current_chats()
        chats = chats_obj.get("chat", [])[-5:]
        last_chat = chats[-1] if chats else {}

        # =========================
        # Step titles for phrasing
        # =========================
        steptitles = [
            {
                str(step["id"]): {
                    "title": step["title"],
                    "description": step["objective"],
                }
            }
            for _, step in steps.items()
        ]

        now = datetime.now().isoformat()

        # =========================
        # Prompt construction
        # =========================
        prompt_text = (
            prompt_instructions.replace("{{step_id}}", str(next_step_id))
            .replace(
                "{{workflow_json}}",
                json.dumps(workflow, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{previous_execution}}",
                json.dumps(execution_data, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{current_chats}}",
                json.dumps(chats, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{last_chat}}",
                json.dumps(last_chat, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{workflow_titles}}",
                json.dumps(steptitles, ensure_ascii=False, indent=2),
            )
            .replace("{{input_data.title}}", inputdata.get("title", ""))
            .replace("{{input_data.description}}", inputdata.get("description", ""))
            .replace("{{todays_datetime}}", now)
        ).strip()

        # =========================
        # LLM → human-style request
        # =========================
        chat_response = await get_fireworks_response(
            user_message=prompt_text,
            role="user",
            user_id=self.userid,
            credits=self.credits,
        )

        if isinstance(chat_response, dict):
            chat_response = chat_response.get("text", "")

        return chat_response.strip()

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
        # print("inside ai detect and route inpuut")
        detect_prompt = template_data.get("detect_and_route_input")
        clarification_prompt_template = template_data.get("step_clarification_prompt")

        if not isinstance(detect_prompt, str):
            raise TypeError(
                "Invalid template structure: 'detect_and_route_input' must be a string."
            )
        # print("here at 683")
        # Get current chat history
        chats_obj = self.get_current_chats()
        new_chat = chats_obj.get("chat", [])[-5:]
        last_chat = new_chat[-1] if new_chat else []
        # print("exttacted id", extracted_id, userinput)

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
        # print("before check on fucntion")

        # Build detect & route prompt
        def build_detect_prompt(u_input):
            # print("inside builde detect prompt")
            return (
                detect_prompt.replace("{{user_input}}", u_input)
                # .replace(
                #     "{{workflow_json}}", json.dumps(self.workflow_json.get("workflow"))
                # )
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

        # print("before calling parsed fireworks")
        # Call AI to detect step
        ai_result = await self.get_parsed_fireworks_response(
            build_detect_prompt(modinput)
        )
        # print("ai detect initial:", ai_result)
        if str(ai_result["step_id"]) != str(extracted_id):
            ai_result = await self.get_eval_parsed_fireworks_response(
                build_detect_prompt(modinput)
            )
            # print("ai_eval detect")
        # If AI detects a step but arguments missing, optionally use step_clarification_prompt
        if ai_result.get("clarification_needed") and clarification_prompt_template:
            step_id = ai_result.get("step_id")
            step_data = self.get_step_data(step_id)
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
            # print("clarification AI result:", clarification_result)
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
            baseworkflow = self.get_step_data(step_id)
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
        # print("res reset", newresultds)
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

    async def ai_scheudle_step(self, stepid, step):
        from services.scheduler_service import SchedulerService

        cfg = step.get("is_scheduler")
        if not cfg:
            return None

        # Prevent duplicate scheduling
        if step.get("scheduler_meta", {}).get("scheduled"):
            return None

        days = cfg.get("days")
        hours = cfg.get("hours")
        time_str = cfg.get("time")

        now = datetime.now()
        social, user_timezone = self.fetchusersocialandtimezone()

        # ---------------------------
        # Resolve run datetime
        # ---------------------------
        if time_str:
            hh, mm = map(int, time_str.split(":"))
            run_at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if run_at <= now:
                run_at += timedelta(days=1)
        else:
            run_at = now + timedelta(days=days or 0, hours=hours or 0)

        # ---------------------------
        # Schedule SINGLE step
        # ---------------------------
        result = SchedulerService.schedule_single_step(
            run_at=run_at,
            userid=self.userid,
            filename=self.filename,
            stepid=stepid,
            timezone=user_timezone,
        )

        # ---------------------------
        # Persist metadata
        # ---------------------------
        step.setdefault("scheduler_meta", {})
        step["scheduler_meta"].update(
            {
                "scheduled": True,
                "task_id": result["task_id"],
                "run_at_local": run_at.isoformat(),
                "run_at_utc": result["run_at_utc"],
            }
        )

        # # {
        # #   "days": <number | null>,
        # #   "hours": <number | null>,
        # #   "time": "<HH:MM | null>"
        # # }
        # pass
        self.saveworkflowtos3()
        return result

    async def get_parsed_fireworks_response(self, prompt_text, role="system", temp=0.3):
        """
        Get and parse Fireworks response.
        Retries once if the response is empty, invalid, or {}.
        """
        for attempt in range(2):
            response_text = await get_fireworks_response2(
                user_message=prompt_text,
                role=role,
                temp=temp,
                user_id=self.userid,
                credits=self.credits,
            )

            if not response_text:
                # print(f"[Retry {attempt+1}] Empty response from Fireworks. ")
                await asyncio.sleep(0.3)
                continue

            response_text = response_text.strip()
            response_text = re.sub(
                r"^```(?:json)?\s*|\s*```$", "", response_text, flags=re.MULTILINE
            ).strip()

            try:
                ai_result = json.loads(response_text)
                if not ai_result:
                    # print(f"[Retry {attempt+1}] Empty JSON object from Fireworks.")
                    await asyncio.sleep(0.3)
                    continue

                return ai_result  # ✅ Valid response

            except json.JSONDecodeError:
                # print(
                #     f"[Retry {attempt+1}] Failed to parse JSON response.{response_text}"
                # )

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
                user_message=prompt_text,
                role=role,
                temp=temp,
                user_id=self.userid,
                credits=self.credits,
            )

            if not response_text:
                # print(f"[Retry {attempt+1}] Empty response from Fireworks. ")
                await asyncio.sleep(0.3)
                continue

            response_text = response_text.strip()
            response_text = re.sub(
                r"^```(?:json)?\s*|\s*```$", "", response_text, flags=re.MULTILINE
            ).strip()

            try:
                ai_result = json.loads(response_text)
                if not ai_result:
                    # print(f"[Retry {attempt+1}] Empty JSON object from Fireworks.")
                    await asyncio.sleep(0.3)
                    continue

                return ai_result  # ✅ Valid response

            except json.JSONDecodeError:
                # print(
                #     f"[Retry {attempt+1}] Failed to parse JSON response.{response_text}"
                # )

                await asyncio.sleep(0.3)

                continue

        return {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.connection:
            try:
                if exc:
                    self.connection.rollback()
                else:
                    self.connection.commit()
            finally:
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
            elif str(step_id) in self.steps:
                return True
        except (ValueError, TypeError):
            pass

        return False

    def _trigger_runbook_owner(self, runbook_id: str) -> None:
        """Trigger the runbook task under self.userid with self.filename."""
        from utils.celery_base import create_playbook_runbook_task
        create_playbook_runbook_task.delay(self.userid, self.filename, runbook_id)

    def saveworkflowtos3(self, finished=None):
        unallowed_keys = {"input_data", "workflow"}

        original_json = read_json_from_s3(self.wf_loc) or {}
        # Update workflow metadata
        for key, value in self.workflow_json.items():
            if key not in unallowed_keys:
                original_json[key] = value

        if self.execution_id and not self.testing:
            original_json.setdefault("executions", {})
            executions_for_day = original_json["executions"].setdefault(
                self.execution_date, {}
            )
            # print("executions currently",executions_for_day)

            clf = os.getenv("CLOUDFRNT")
            basepathexec = f"{clf}/{self.on_loc}"

            # 🔥 ALWAYS CREATE NEW EXECUTION
            executions_for_day[self.execution_id] = {
                "execution_id": self.execution_id,
                "started_at": self.started_at,
                "execution_path": basepathexec,
                "execution_unique_key": self.execution_unique_key,
                "status": "running",
            }
            # print("new executions or present",executions_for_day)

            if finished:
                # print("executions are finished", finished)
                executions_for_day[self.execution_id]["status"] = "completed"

                current_schedule = original_json.get("current_schedule")
                if current_schedule:
                    original_json.setdefault("prev_schedules", []).append(
                        current_schedule
                    )

                original_json["current_schedule"] = None

        return save_playbook_to_s3(
            original_json,
            self.userid,
            "workflow updated successfully",
            self.filename,
        )

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

            step = self.get_step_data(current_step_id)
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
        if self.testing:
            chat_entry = {
                "id": self.generate_unique_id([e["id"] for e in self.chat_history]),
                "date": now.isoformat(),
                "input": userinput or "Workflow execution initiated",
                "output": "The process was executed successfully.",
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
        else:
            self.saveworkflowtos3(finished=True)
        return self.execution_log

    def get_execution_log(self) -> List[Dict[str, Any]]:
        return self.execution_log

    def append_execution_step_log(self, step_id, log_entry):
        if not self.execution_id or self.testing:
            return

        exec_json = read_json_from_s3(self.on_loc) or {}

        exec_json.setdefault("steps", {})
        exec_json["steps"][str(step_id)] = log_entry

        save_execution_playbook_to_s3(
            exec_json,
            self.userid,
            "execution step logged",
            self.on_loc,
        )

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

    def get_step_data(self, step_id):
        step = self.steps.get(str(step_id))
        if step is None:
            try:
                step = self.steps.get(int(step_id))
            except (ValueError, TypeError):
                pass
        return step

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
                if k in CONTACT_KEYS and not self.testing:
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

    def _find_step_by_ref(self, step_ref: str) -> Optional[Dict[str, Any]]:
        """
        Finds a step by reference number or ID.
        Tries direct ID match first, then by 1-based position in step list.
        """
        # Try direct ID match
        if str(step_ref) in self.steps:
            return self.steps[str(step_ref)]
        # Try by 1-based position in the step list
        try:
            idx = int(step_ref) - 1
            step_list = self.workflow_json.get("workflow", {}).get("steps", [])
            if 0 <= idx < len(step_list):
                return step_list[idx]
        except (ValueError, TypeError):
            pass
        return None

    def _build_dependency_blocked_response(
        self,
        target_step: dict,
        blocking_step: dict,
        required_fields: list,
        step_ref: str = None,
    ) -> dict:
        """
        Builds a user-facing response when a dependency step blocks execution.
        The user needs to provide required_fields for blocking_step before target_step can run.
        """
        target_title = (
            target_step.get("title", "this step") if target_step else "this step"
        )
        blocking_title = blocking_step.get("title", f"Step {step_ref}")

        fields_str = "\n".join(f"- {f}" for f in required_fields)

        message = (
            f"To execute **{target_title}**, I need to first complete **{blocking_title}**, "
            f"which requires the following from you:\n{fields_str}\n\n"
            f"Please provide these details and run **{blocking_title}** first."
        )

        self.logger.debug(
            "_build_dependency_blocked_response: target=%s, blocker=%s, fields=%s",
            target_step.get("id"),
            blocking_step.get("id"),
            required_fields,
        )

        return {
            "workflow_intent": False,
            "execution_status": "dependency_blocked",
            "step_id": blocking_step.get("id"),
            "message": message,
            "response_message": message,
            "log_status": "dependency_blocked",
            "clarification_needed": True,
            "dependency_info": {
                "target_step_id": target_step.get("id") if target_step else None,
                "blocking_step_id": blocking_step.get("id"),
                "blocking_step_title": blocking_title,
                "required_fields": required_fields,
            },
        }

    async def _resolve_placeholders(self, args: dict, current_step_id: str = None):
        """
        Resolves {{step_N.field_name}} template references in args.
        Walks dependency chain in ascending order (lowest step first).
        Returns tuple: (resolved_args, blocking_response_or_None)

        - If all resolved or already auto-executed → returns (resolved_args, None)
        - If a dependency step needs user input → returns (args, blocking_response)
        """
        self.logger.debug(
            "Entering _resolve_placeholders with args: %s", sorted(args.keys())
        )
        try:
            placeholder_pattern = re.compile(r"\{\{step_(\w+)\.(\w+)\}\}")
            resolved = dict(args)

            # Step 1: Collect all unresolved dependencies
            unresolved_deps = {}  # step_ref → [field_names]
            for key, value in args.items():
                if not isinstance(value, str):
                    continue
                for match in placeholder_pattern.finditer(value):
                    step_ref, field_name = match.group(1), match.group(2)

                    # Check if already resolved
                    already_resolved = False

                    # Try previous_data
                    execution_data = self.previous_data or {}
                    steps_data = execution_data.get("steps", execution_data)
                    step_entry = steps_data.get(str(step_ref), {})
                    details = step_entry.get("details", {})
                    if field_name in details:
                        already_resolved = True

                    # Try pre_user_data
                    if not already_resolved:
                        pud = self.workflow_json.get("pre_user_data", {})
                        if field_name in pud and pud[field_name] not in (
                            None,
                            "",
                            [],
                            {},
                        ):
                            already_resolved = True

                    # If not already resolved, add to unresolved deps
                    if not already_resolved:
                        unresolved_deps.setdefault(str(step_ref), []).append(field_name)

            if not unresolved_deps:
                self.logger.debug(
                    "_resolve_placeholders: all dependencies already resolved"
                )
                return resolved, None

            # Step 2: Sort step_refs in ascending order (handle both numeric and string IDs)
            def sort_key(ref):
                try:
                    return (0, int(ref))
                except ValueError:
                    return (1, ref)

            sorted_refs = sorted(unresolved_deps.keys(), key=sort_key)
            self.logger.debug(
                "_resolve_placeholders: unresolved deps in order: %s", sorted_refs
            )

            # Step 3: Walk dependencies from earliest to latest
            for step_ref in sorted_refs:
                dep_step = self._find_step_by_ref(step_ref)
                if not dep_step:
                    self.logger.warning(
                        "_resolve_placeholders: step ref %s not found", step_ref
                    )
                    continue

                needs = dep_step.get("requirements_needed") or []

                if needs:
                    # BLOCKED — this step needs user input
                    self.logger.warning(
                        "_resolve_placeholders: blocked by step %s which needs %s",
                        step_ref,
                        needs,
                    )
                    target_step = (
                        self.get_step_data(current_step_id) if current_step_id else None
                    )
                    blocking_response = self._build_dependency_blocked_response(
                        target_step=target_step,
                        blocking_step=dep_step,
                        required_fields=needs,
                        step_ref=step_ref,
                    )
                    return resolved, blocking_response

                # Auto-executable — run it
                try:
                    self.logger.info(
                        "_resolve_placeholders: auto-executing step %s (id=%s)",
                        step_ref,
                        dep_step.get("id"),
                    )
                    dep_result = await self._execute_step(
                        step_id=dep_step["id"], compl=True
                    )

                    # Check execution result for our fields
                    if dep_result:
                        details = dep_result.get("execution_details", {})
                        for field_name in unresolved_deps[step_ref]:
                            if field_name in details:
                                resolved[f"step_{step_ref}_{field_name}"] = details[
                                    field_name
                                ]

                except Exception as e:
                    self.logger.error(
                        "_resolve_placeholders: error auto-executing step %s: %s",
                        step_ref,
                        e,
                        exc_info=True,
                    )

            # Step 4: Final substitution pass
            for key, value in resolved.items():
                if not isinstance(value, str):
                    continue
                resolved_value = value
                for match in placeholder_pattern.finditer(value):
                    step_ref = match.group(1)
                    field_name = match.group(2)
                    placeholder_str = match.group(0)

                    resolved_val = None

                    # Check previous_data
                    execution_data = self.previous_data or {}
                    steps_data = execution_data.get("steps", execution_data)
                    step_entry = steps_data.get(str(step_ref), {})
                    details = step_entry.get("details", {})
                    if field_name in details:
                        resolved_val = details[field_name]
                        self.logger.debug(
                            "_resolve_placeholders: resolved %s from previous_data",
                            placeholder_str,
                        )

                    # Check pre_user_data
                    if resolved_val is None:
                        pud = self.workflow_json.get("pre_user_data", {})
                        if field_name in pud and pud[field_name] not in (
                            None,
                            "",
                            [],
                            {},
                        ):
                            resolved_val = pud[field_name]
                            self.logger.debug(
                                "_resolve_placeholders: resolved %s from pre_user_data",
                                placeholder_str,
                            )

                    # Perform substitution
                    if resolved_val is not None:
                        resolved_value = resolved_value.replace(
                            placeholder_str, str(resolved_val)
                        )
                    else:
                        self.logger.warning(
                            "_resolve_placeholders: could not resolve placeholder %s in arg '%s'",
                            placeholder_str,
                            key,
                        )

                resolved[key] = resolved_value

            self.logger.debug("_resolve_placeholders completed successfully")
            return resolved, None

        except Exception as e:
            self.logger.error("_resolve_placeholders error: %s", e, exc_info=True)
            raise

    async def _execute_step(
        self, step_id, ai_result=None, compl=False
    ) -> Dict[str, Any]:
        step = self.get_step_data(step_id)
        self.current_wf_id = step_id
        function_args = None
        if ai_result:
            function_args = ai_result.get("function_args", {})
        # print("in execute ", step)

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

                # Resolve any {{step_N.field_name}} references and check for dependency blocks
                nfunction_args, blocking_response = await self._resolve_placeholders(
                    nfunction_args, current_step_id=step_id
                )
                if blocking_response:
                    self.logger.warning(
                        "_execute_step: execution blocked by dependency - %s",
                        blocking_response.get("message", "")[:100],
                    )
                    return blocking_response

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
                        elif "form" in execution_result:
                            form_schema = execution_result["form"]

                            message = {"type": "form", "form_schema": form_schema}
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
            now = datetime.utcnow()
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

            if result.get("step_id"):
                step_id = result["step_id"]

                step = self.get_step_data(step_id)
                title = step.get("title")
                if self.testing:
                    log_entry = {
                        "title": title,
                        "date": now.isoformat(),
                        "input": "complete execution",
                        "output": result.get("message"),
                        "status": execution_status,
                        "details": execution_result or execution_details,
                    }
                else:
                    log_entry = {
                        "title": title,
                        "date": now.isoformat(),
                        "status": execution_status,
                        "details": execution_result or execution_details,
                    }

                # ----------------------------------------
                # TESTING → store inside workflow JSON
                # ----------------------------------------
                if self.testing:
                    chat_entry = {
                        "id": chid,
                        "date": now.isoformat(),
                        "input": f"running {title}",
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
                    self.workflow_json.setdefault("testing", {})
                    self.workflow_json["testing"][str(step_id)] = log_entry
                    self.saveworkflowtos3()

                # ----------------------------------------
                # ONLINE → store in execution file
                # ----------------------------------------
                else:
                    self.append_execution_step_log(step_id, log_entry)

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
            user_message=step["ai_instructions"],
            role="system",
            user_id=self.userid,
            credits=self.credits,
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
        if not self.connection:
            self.connection = connect_to_rds()

        # Prepare constructor arguments for each service
        if service_prefix == "google_meet":
            contacts = self.get_attendees(self.contacts)
            instance = service_class(
                contacts=contacts,
                userid=self.userid,
                testing=self.testing,
                workflow=self.workflow_json,
                wf_id=self.current_wf_id,
                connection=self.connection,
            )
        # print("triggering google service")
        elif service_prefix == "gmail":
            instance = service_class(
                user_id=self.userid,
                connection=self.connection,
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
                credits=self.credits,
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
        # print("step currently", step_id)

        # Call the function
        import inspect

        result = func(**args)

        if inspect.isawaitable(result):
            result = await result

        return result

        # return "ok"

    async def savechatcheck(self, ai_result, user_input):
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
                return chat_entry

        except Exception as e:
            self.logger.warning(f"Failed to log chat entry: {e}")
        return None

    async def check_input_tone(self, user_input: str):
        try:
            result = await self.ai_input_intent_classifier(userinput=user_input)
            ai_result = {}
            # print("result by input checker", result)

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
                    # print("convo ai_result", "phase 2 normal")

                elif intent == "workflow":
                    ma_res = await self.ai_detect_trigger_type(userinput=user_input)
                    # print("ma_res", ma_res)
                    if (
                        "bs_wf_single_runner" in ma_res
                        and ma_res["bs_wf_single_runner"]
                    ):
                        extracted_id = None
                        if "step_id" in ma_res:
                            extracted_id = ma_res["step_id"]
                            step = self.get_step_data(extracted_id)
                            if step:
                                function_call = step.get("function_call", {})
                                function_name = function_call.get("function_name")
                                if (
                                    function_name
                                    == "automate.assign_or_show_questions_from_file"
                                ):
                                    result = await self._execute_step(
                                        step_id=extracted_id, compl=True
                                    )
                                    if "questions" in result:
                                        return {
                                            "message": "Questions retrieved successfully.",
                                            "action": "step_complete",
                                        }
                                    else:
                                        return result
                            # print("step", step)
                            if step.get("decision_point"):
                                val = self.ai_decision_Check(
                                    userinput=user_input, extracted_id=extracted_id
                                )
                                if "next_step_id" in val:
                                    valres = val["response_text"]
                                    user_input = f"{valres} where {user_input}"
                                    extracted_id = val["next_step_id"]
                            if step.get("is_scheduler"):
                                schedule_result = await self.ai_scheudle_step(
                                    extracted_id, step
                                )

                                ai_result = {
                                    "response_message": (
                                        f"✅ Step **{step.get('title', extracted_id)}** has been scheduled.\n\n"
                                        if schedule_result
                                        else "⚠️ This step is already scheduled."
                                    ),
                                    "log_status": "workflow_scheduled",
                                    "step_id": extracted_id,
                                    "wf_single_runner": False,
                                    "confirm_step": False,
                                }
                                chat_entry = await self.savechatcheck(
                                    ai_result=ai_result, user_input=user_input
                                )
                                if chat_entry:
                                    ai_result["chat_entry"] = chat_entry

                                # IMPORTANT: hard stop downstream triggers
                                return ai_result

                        route = await self.ai_detect_and_route_input(
                            userinput=user_input, extracted_id=extracted_id
                        )
                        # print("base route workflow", route)
                        ai_result = {
                            "response_message": route.get("response_message", ""),
                            "wf_single_runner": bool(
                                route.get("wf_single_runner", False)
                            ),
                            "confirm_step": bool(route.get("confirm_step", False)),
                            "step_id": route.get("step_id"),
                            "log_status": "workflow",
                            "trigger_step": route.get("trigger_step", {}),
                            "clarification_needed": bool(
                                route.get("clarification_needed", False)
                            ),
                            "clarification_message": route.get(
                                "clarification_message", ""
                            ),
                        }
                        # -------------------------------
                        # Decide execution vs clarification
                        # -------------------------------
                        if ai_result.get("wf_single_runner") and not ai_result.get(
                            "clarification_message"
                        ):
                            # print("ai result data", ai_result)
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
                        improv_result = await self.update_steps_workflow(user_input)
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
                        "clarification_needed": reset.get(
                            "clarification_needed", False
                        ),
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
            chat_entry = await self.savechatcheck(
                ai_result=ai_result, user_input=user_input
            )
            if chat_entry:
                ai_result["chat_entry"] = chat_entry

            # Return AI result for frontend or further processing
            return ai_result
        except Exception as e:
            import traceback as _tb

            self.logger.error("Error in check_input_tone: %s\n%s", e, _tb.format_exc())
            return {
                "response_message": "Error processing option.",
                "wf_single_runner": False,
                "log_status": "error",
            }

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
            # normalize: keep int for legacy numeric IDs, string for UUID IDs
            if isinstance(step_id, str) and step_id.strip().isdigit():
                step_id = int(step_id)

            if not self.check_step_exists(step_id):
                raise ValueError(f"Step {step_id} does not exist in workflow")

            locked_step_id = step_id
            pr_workflow = self.get_step_data(step_id)
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
        # print("user inputs", user_input, step_id)
        ai_result = await self.get_parsed_fireworks_response(
            prompt_text=prompt_text, temp=0.2
        )
        # print("ai _ result", ai_result)

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
                step_id = ai_result.get("step_id")
            # print(step_id, type(step_id), self.check_step_exists(step_id))
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
        # print("result on execute on 2422", result)

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
            step_id = result["step_id"]
            step = self.get_step_data(step_id)

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

        self.workflow_json["last_ai_discovered"] = {}

        # ------------------------------------------------------------------
        # Persist workflow
        # ------------------------------------------------------------------
        self.saveworkflowtos3()

        result["chat_entry"] = chat_entry
        # Normalize key so the frontend can always read response_message
        result.setdefault("response_message", result.get("message", ""))
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

    def _question_answer_stats(self):
        execution_data = self.previous_data
        answered = 0
        total = 0

        for step_data in execution_data.values():
            outputs = step_data.get("output", [])
            if not isinstance(outputs, list):
                continue

            for q in outputs:
                if "user_answer" in q:
                    total += 1
                    ans = q.get("user_answer")

                    # ✅ Proper check
                    if ans is not None and str(ans).strip() != "":
                        answered += 1

        all_answered = total > 0 and answered == total

        return {
            "answered": answered,
            "total": total,
            "all_answered": all_answered,
        }

    async def answer_questions(self, answer: str, comment: str, qid: str, chid: str):
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
                        q["user_answer"] = answer
                        q["comment"] = comment
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

        cleared = False
        # -------------------------------------------------
        # 2. UPDATE CHAT HISTORY (SECONDARY)
        # -------------------------------------------------
        for chat in chats:
            if str(chat.get("id")) == str(chid):
                outputs = chat.get("output", [])
                step_id = chat.get("step_id")
                last_step_id = step_id
                if not isinstance(outputs, list):
                    continue

                for out in outputs:
                    if out.get("id") == qid:
                        out["user_answer"] = answer
                        out["comment"] = comment
                        if answer == "":
                            cleared = True
                        break
        # -------------------------------------------------
        # 3. CHECK IF ALL QUESTIONS ANSWERED
        # -------------------------------------------------
        # all_answered = self._question_answer_stats()
        stats = self._question_answer_stats()
        all_answered = stats["all_answered"]
        if cleared:
            all_answered = False

        message = (
            "Answer saved successfully."
            if not all_answered
            else "All questions have been answered."
        )

        if all_answered:
            self.chat_history.append(
                {
                    "id": uuid.uuid4().hex,
                    "date": now.isoformat(),
                    "input": "",
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

        # -------------------------------------------------
        # 5. TRIGGER RUNBOOK (after save, to avoid stale S3 read in worker)
        # -------------------------------------------------
        if all_answered:
            runbook_id = self.workflow_json.get("runbook_id")
            if runbook_id:
                self.logger.info("All questions answered, triggering runbook task")
                self._trigger_runbook_owner(runbook_id)

        return {
            "status": "success",
            "all_questions_answered": all_answered,
            "message": message,
        }

    async def update_form_field(self, field_id: str, answer, chid: str):

        execution_data = self.previous_data or {}
        chats = self.chat_history or []

        field_found = False

        # ------------------------------
        # UPDATE EXECUTION DATA
        # ------------------------------
        for step_data in execution_data.values():
            if not isinstance(step_data, dict):
                continue
            output = step_data.get("output") or {}
            if not isinstance(output, dict):
                continue
            form_schema = output.get("form_schema") or {}
            if not isinstance(form_schema, dict):
                continue
            fields = form_schema.get("fields") or []
            if not isinstance(fields, list):
                continue

            for field in fields:
                if not isinstance(field, dict):
                    continue
                if field.get("id") == field_id:
                    field["user_answer"] = answer
                    field_found = True
                    break

            if field_found:
                break

        if not field_found:
            return {"status": "error", "message": f"Field '{field_id}' not found"}

        # ------------------------------
        # UPDATE CHAT HISTORY
        # ------------------------------
        for chat in chats:

            if str(chat.get("id")) == str(chid):
                chat_output = chat.get("output") or {}
                if not isinstance(chat_output, dict):
                    chat_output = {}
                chat_form_schema = chat_output.get("form_schema") or {}
                if not isinstance(chat_form_schema, dict):
                    chat_form_schema = {}
                fields = chat_form_schema.get("fields") or []
                if not isinstance(fields, list):
                    fields = []

                for field in fields:

                    if field.get("id") == field_id:
                        field["answer"] = answer
                        break

                break

        # ------------------------------
        # CHECK COMPLETION
        # ------------------------------
        all_answered = self._are_all_required_fields_answered()

        message = "Answer saved."
        if all_answered:
            message = "Form completed successfully."

        # ------------------------------
        # SAVE
        # ------------------------------
        self.previous_data = execution_data
        self.chat_history = chats
        self.saveworkflowtos3()

        # ------------------------------
        # TRIGGER RUNBOOK (if linked)
        # ------------------------------
        if all_answered:
            runbook_id = self.workflow_json.get("runbook_id")
            if runbook_id:
                self.logger.info("Form completed, triggering runbook task")
                self._trigger_runbook_owner(runbook_id)

        return {
            "status": "success",
            "all_fields_answered": all_answered,
            "message": message,
        }

    def _are_all_required_fields_answered(self):

        execution_data = self.previous_data or {}

        for step_data in execution_data.values():
            if not isinstance(step_data, dict):
                continue
            output = step_data.get("output") or {}
            if not isinstance(output, dict):
                continue
            form_schema = output.get("form_schema") or {}
            if not isinstance(form_schema, dict):
                continue
            fields = form_schema.get("fields") or []
            if not isinstance(fields, list):
                continue

            for field in fields:
                if not isinstance(field, dict):
                    continue
                if field.get("required"):
                    answer = field.get("user_answer")
                    if answer in [None, ""]:
                        return False

        return True

    async def update_form_bulk(self, answers, chid: str):

        execution_data = self.previous_data or {}
        chats = self.chat_history or []
        workflow = self.workflow_json or {}

        # -------------------------------------------------
        # NORMALIZE ANSWERS
        # supports:
        # [{"field_id":"x","answer":"y"}]
        # [{"id":"x","answer":"y"}]
        # {"x":"y"}
        # -------------------------------------------------
        if isinstance(answers, list):

            normalized = {}

            for a in answers:

                fid = a.get("id") or a.get("field_id")

                if not fid:
                    continue

                val = a.get("answer") or a.get("user_answer")

                # convert boolean strings
                if isinstance(val, str) and val.lower() in ["true", "false"]:
                    val = val.lower() == "true"

                normalized[fid] = val

            answers = normalized

        updated_fields = []

        # -------------------------------------------------
        # FIND TARGET CHAT
        # -------------------------------------------------
        target_chat = None

        for chat in chats:
            if str(chat.get("id")) == str(chid):
                target_chat = chat
                break

        if not target_chat:
            return {"status": "error", "message": "Chat not found"}

        step_id = target_chat.get("step_id")

        # -------------------------------------------------
        # UPDATE EXECUTION DATA
        # -------------------------------------------------
        if step_id and step_id in execution_data:
            exec_step = execution_data[step_id]
            if isinstance(exec_step, dict):
                exec_output = exec_step.get("output") or {}
                if isinstance(exec_output, dict):
                    exec_form_schema = exec_output.get("form_schema") or {}
                    if isinstance(exec_form_schema, dict):
                        fields = exec_form_schema.get("fields") or []
                        if isinstance(fields, list):
                            for field in fields:
                                if not isinstance(field, dict):
                                    continue
                                fid = field.get("id")
                                if fid in answers:
                                    field["user_answer"] = answers[fid]
                                    updated_fields.append(fid)

        # -------------------------------------------------
        # UPDATE CHAT HISTORY
        # -------------------------------------------------
        chat_output = target_chat.get("output") or {}
        if not isinstance(chat_output, dict):
            chat_output = {}
        form_schema = chat_output.get("form_schema") or {}
        if not isinstance(form_schema, dict):
            form_schema = {}
        fields = form_schema.get("fields", [])
        if not isinstance(fields, list):
            fields = []

        for field in fields:
            if not isinstance(field, dict):
                continue
            fid = field.get("id")
            if fid in answers:
                field["user_answer"] = answers[fid]
                updated_fields.append(fid)
        # -------------------------------------------------
        # SAVE ANSWERS TO pre_user_data PER STEP
        # -------------------------------------------------

        existing_pre = workflow.get("pre_user_data")
        pre_user_data = existing_pre if isinstance(existing_pre, dict) else {}
        workflow["pre_user_data"] = pre_user_data

        step_key = str(step_id)

        # ensure step container exists
        existing_step = pre_user_data.get(step_key)
        step_inputs = existing_step if isinstance(existing_step, dict) else {}
        pre_user_data[step_key] = step_inputs

        for fid, val in answers.items():
            step_inputs[fid] = val

        pre_user_data[step_key] = step_inputs
        workflow["pre_user_data"] = pre_user_data

        # -------------------------------------------------
        # CHECK FORM COMPLETION
        # -------------------------------------------------
        form_completed = self._are_all_required_fields_answered()

        # -------------------------------------------------
        # SAVE STATE
        # -------------------------------------------------
        self.previous_data = execution_data
        self.chat_history = chats
        self.workflow_json = workflow

        self.saveworkflowtos3()

        # -------------------------------------------------
        # TRIGGER RUNBOOK (if linked)
        # -------------------------------------------------
        if form_completed:
            runbook_id = self.workflow_json.get("runbook_id")
            if runbook_id:
                self.logger.info("Form completed, triggering runbook task")
                self._trigger_runbook_owner(runbook_id)

        return {
            "status": "success",
            "updated_fields": updated_fields,
            "form_completed": form_completed,
            "saved_inputs": pre_user_data,
        }

    async def answer_questions_bulk(self, answers: list, chid: str):
        execution_data = self.previous_data
        chats = self.chat_history

        answer_map = {
            item.get("question_id"): item.get("user_answer")
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
                    q["user_answer"] = answer_map[qid]
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
                        out["user_answer"] = answer_map[qid]

        # -------------------------------------------------
        # 3. CHECK IF ALL QUESTIONS ANSWERED
        # -------------------------------------------------

        stats = self._question_answer_stats()

        all_answered = stats["all_answered"]
        answered = stats["answered"]
        total = stats["total"]

        message = (
            f"Responses have been saved successfully. "
            f"{answered} of {total} questions have been completed."
        )

        if all_answered:
            next_step_title = None

            if last_step_id and last_step_id in self.steps:
                current_step = self.get_step_data(last_step_id)

                # 1️⃣ Explicit next_step
                next_step_id = current_step.get("next_step")

                # 2️⃣ Fallback: implicit next step by insertion order
                if not next_step_id:
                    sorted_step_ids = sorted(
                        self.steps.keys(), key=lambda k: self.step_order.get(k, 0)
                    )
                    try:
                        current_index = sorted_step_ids.index(last_step_id)
                        if current_index + 1 < len(sorted_step_ids):
                            next_step_id = sorted_step_ids[current_index + 1]
                    except ValueError:
                        next_step_id = None

                # 3️⃣ Resolve title
                if next_step_id and next_step_id in self.steps:
                    st = self.get_step_data(next_step_id)
                    next_step_title = st.get("title")

            # -----------------------------
            # Build user-facing message
            # -----------------------------
            if next_step_title:
                message = (
                    "All required questions have been answered successfully. "
                    f"To continue, please initiate the next step: {next_step_title}."
                )
            else:
                message = (
                    "All required questions have been answered successfully. "
                    "The process has now been completed."
                )

            self.chat_history.append(
                {
                    "id": uuid.uuid4().hex,
                    "date": now.isoformat(),
                    "input": "Submitted responses to all pending questions",
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

        # -------------------------------------------------
        # 5. TRIGGER RUNBOOK (after save, to avoid stale S3 read in worker)
        # -------------------------------------------------
        if all_answered:
            runbook_id = self.workflow_json.get("runbook_id")
            if runbook_id:
                self.logger.info("All questions answered, triggering runbook task")
                self._trigger_runbook_owner(runbook_id)

        return {
            "status": "success",
            "all_questions_answered": all_answered,
            "message": message,
        }

    async def make_workflow_conversation(self, user_message=""):

        template_data = PLAY_TEMPLATE
        detect_prompt = template_data.get("wf_conversation")
        # print("ty")

        # -------------------------
        # Determine current step
        # -------------------------

        current_step = self._get_next_uncompleted_step()

        if not current_step:
            return {
                "message": "✅ This workflow is already completed.",
                "action": "workflow_complete",
                "options": [],
            }
        # -------------------------
        # Save chat
        # -------------------------

        chats = self.workflow_json.setdefault("chat", [])

        if chats:
            existing_ids = {entry["id"] for entry in chats}
            chid = self.generate_unique_id(existing_ids)
        else:
            chid = str(uuid.uuid4().int)[0:6]
        is_first_interaction = len(chats) == 0

        last_chat = chats[-1] if chats else None
        output_data = {}
        ai_result = {}

        step_data = self.get_step_data(current_step)
        function_call = step_data.get("function_call", {})
        function_name = function_call.get("function_name")
        if function_name == "automate.assign_or_show_questions_from_file":
            # print("dsadsad")
            assigned = self.workflow_json.get("assigned_questions")
            if assigned:
                result = await self._execute_step(step_id=current_step, compl=True)
                if (
                    "execution_details" in result
                    and result["execution_details"]["questions"]
                ):
                    return {
                        "message": "Questions retrieved successfully.",
                        "action": "step_complete",
                    }
            else:
                if is_first_interaction:
                    output_data = {
                        "message": "👋 Welcome!\n\n"
                        "Good to have u here.\n\n"
                        "For the process to run, no assigned questions were found.\n\n"
                        "Please upload or assign a questionnaire file."
                    }
                else:
                    output_data = {
                        "message": "For the process to run, no assigned questions were found.\n\n"
                        "Please upload or assign a questionnaire file."
                    }
        else:
            previous_results = self.previous_data or {}
            collected_inputs = self.workflow_json.get("pre_user_data", {})

            # -------------------------------------------------
            # WAIT FOR OPTION SELECTION
            # -------------------------------------------------

            if last_chat and str(last_chat.get("step_id")) == str(current_step):

                last_output = last_chat.get("output", {})
                options = last_output.get("options", [])

                if options and not user_message:

                    return {
                        "message": "⚠️ Please select one of the options above to continue.",
                        "options": options,
                        "action": "await_choice",
                        "step_id": current_step,
                    }

            if last_chat and str(last_chat.get("step_id")) == str(current_step):

                fields = (
                    last_chat.get("output", {}).get("form_schema", {}).get("fields", [])
                )

                if fields:
                    unanswered = [
                        f
                        for f in fields
                        if f.get("required")
                        and not (f.get("answer") or f.get("user_answer"))
                    ]

                    if unanswered:
                        return {
                            "message": "⚠️ Please complete the above form to continue.",
                            "action": "collect_inputs",
                            "inputs": fields,
                            "requires_input": True,
                            "step_id": current_step,
                        }
            # print("bes lasa a")
            # last assistant message
            last_assistant_output = last_chat.get("output") if last_chat else {}

            # ✅ Handle list
            if isinstance(last_assistant_output, list):
                last_assistant_output = (
                    last_assistant_output[0] if last_assistant_output else {}
                )

            # ✅ Handle string (VERY IMPORTANT)
            if isinstance(last_assistant_output, str):
                try:
                    last_assistant_output = json.loads(last_assistant_output)
                except Exception:
                    last_assistant_output = {}

            # ✅ Final safety
            if not isinstance(last_assistant_output, dict):
                last_assistant_output = {}

            # print("DEBUG OUTPUT:", last_assistant_output)

            last_options = last_assistant_output.get("options", [])

            # last 3 chat messages for context
            chat_context = chats[-3:] if chats else []
            # print("dsadasdas")

            prompt_text = (
                detect_prompt.replace("{{workflow_json}}", json.dumps(self.steps))
                .replace("{{current_step}}", str(current_step))
                .replace("{{step_data}}", json.dumps(step_data))
                .replace("{{previous_results}}", json.dumps(previous_results))
                .replace("{{collected_inputs}}", json.dumps(collected_inputs))
                .replace("{{user_message}}", user_message)
                .replace(
                    "{{last_options}}",
                    json.dumps(last_options),
                )
                .replace("{{last_assistant_output}}", json.dumps(last_assistant_output))
                .replace("{{chat_history}}", json.dumps(chat_context))
            )

            # -------------------------
            # Call AI
            # -------------------------

            ai_result = await self.get_parsed_fireworks_response(
                prompt_text=prompt_text, temp=0.2
            )

            if not ai_result:
                return {"message": "Something went wrong."}

            # -------------------------
            # Transform AI result
            # -------------------------
            if is_first_interaction:
                ai_result["message"] = (
                    "👋 Welcome!\n\n"
                    "Good to have u here. \n\n" + ai_result.get("message", "")
                )

            if ai_result.get("requires_input"):
                output_data = {
                    "message": ai_result.get("message"),
                    "form_schema": {"fields": ai_result.get("inputs", [])},
                    "options": ai_result.get("options", []),
                }
            else:
                output_data = {
                    "message": ai_result.get("message"),
                    "options": ai_result.get("options", []),
                }

        # -------------------------
        # Save chat entry
        # -------------------------

        chat_entry = {
            "id": chid,
            "date": now.isoformat(),
            "input": user_message,
            "output": output_data,
            "status": "conversation",
            "step_id": current_step,
        }

        chats.append(chat_entry)

        # -------------------------
        # Save workflow
        # -------------------------

        self.saveworkflowtos3()

        return output_data

    async def answer_ques_file_bk(
        self, extracted_files, step_id, file_keys, inp_links=None, inp_link_keys=None
    ):
        import re
        from config_evidences.evidence_helpers import get_only_evidence
        from db.lance_db_service import LanceDBServer

        if inp_links is None:
            inp_links = []
        if inp_link_keys is None:
            inp_link_keys = []

        assigned_ques = self.workflow_json.get("assigned_questions", [])
        if not assigned_ques:
            return {"error": "No assigned questions found"}

        execution_data = self.previous_data
        chats = self.chat_history

        # ===========================
        # GET ANSWERED QUESTION IDs
        # ===========================
        answered_qids = set()
        if isinstance(execution_data, dict):
            for s_id, step_data in execution_data.items():
                if step_id and str(s_id) != str(step_id):
                    continue
                for out in step_data.get("output", []):
                    if out.get("user_answer"):
                        answered_qids.add(out.get("id"))

        # ===========================
        # EARLY EXIT
        # ===========================
        if not extracted_files and not inp_links:
            return {"error": "No usable content found"}

        CHUNK_SIZE = 8000
        OVERLAP = 500

        def _make_chunks(text):
            result = []
            i = 0
            while i < len(text):
                result.append(text[i : i + CHUNK_SIZE])
                i += CHUNK_SIZE - OVERLAP
            return result

        def safe_json_load(text):
            text = text.strip()
            for start_char, end_char in [("[", "]"), ("{", "}")]:
                try:
                    s = text.find(start_char)
                    e = text.rfind(end_char)
                    if s != -1 and e != -1:
                        fragment = re.sub(r",\s*([}\]])", r"\1", text[s : e + 1])
                        return json.loads(fragment)
                except Exception:
                    pass
            return {} if "{" in text else []

        # ===========================
        # STEP 1: GET EVIDENCE CONFIGS  (combined_text no longer needed — files processed individually)
        # ===========================
        user_evidence = get_only_evidence(self.userid)
        # print("user structur evidence", user_evidence) # OK
        evidence_summary = json.dumps(
            [
                {
                    "id": e.get("id"),
                    "artifact": e.get("artifact"),
                    "type": e.get("type"),
                    "expectations": e.get("expectations"),
                }
                for e in user_evidence
            ],
            indent=2,
        )

        runbook_id = self.workflow_json.get("runbook_id", "")
        self.logger.debug("runbook_id: %s", runbook_id)
        runbook_evidence_config = []
        allowed_artifacts = set()
        disallowed_artifacts = set()

        if runbook_id:
            try:
                dbserver = LanceDBServer()
                runbook_list = await dbserver.get_runbook_by_id(self.userid, runbook_id)
                if runbook_list:
                    # self.logger.debug("runbook_list: %s", runbook_list)
                    runbook = runbook_list[0]
                    raw_config = runbook.get("runbook_evidence_config", "") or ""
                    # print(type(raw_config), raw_config)
                    if raw_config:
                        config_data = json.loads(raw_config)
                        if isinstance(config_data, list):
                            runbook_evidence_config = config_data
                        elif isinstance(config_data, dict):
                            runbook_evidence_config = config_data.get(
                                "configurations", []
                            )
            except Exception as e:
                self.logger.error("Runbook config fetch failed: %s", e, exc_info=IS_DEV)

        self.logger.debug("runbook evidence config: %s", runbook_evidence_config)
        for cfg in runbook_evidence_config:
            artifact = cfg.get("artifact", "")
            decision = (
                cfg.get("decision")
                if cfg.get("decision") is not None
                else cfg.get("Decision")
            )
            if decision is True:
                allowed_artifacts.add(artifact)
            elif decision is False:
                disallowed_artifacts.add(artifact)
        self.logger.debug("allowed artifacts: %s", allowed_artifacts)
        self.logger.debug("disallowed artifacts: %s", disallowed_artifacts)

        # ===========================
        # STEP 2: CATEGORIZE EVIDENCE
        # ===========================
        evidence_map = {}  # artifact -> {snippets: [], files: set()}

        # Build a doc-safe evidence summary that excludes image-only artifacts.
        # PDFs and other text files cannot satisfy evidence whose nature requires
        # an actual image (Screenshot, System screenshot, CCTV, etc.).
        _image_natures = {"Image", "Image/video", "Screenshot"}
        _image_only_artifacts = {
            e.get("artifact")
            for e in user_evidence
            if e.get("nature") in _image_natures
        }
        evidence_summary_docs = json.dumps(
            [
                {
                    "id": e.get("id"),
                    "artifact": e.get("artifact"),
                    "type": e.get("type"),
                    "expectations": e.get("expectations"),
                }
                for e in user_evidence
                if e.get("nature") not in _image_natures
            ],
            indent=2,
        )

        self.logger.debug("evidence summary length: %d", len(evidence_summary))
        self.logger.info("Starting evidence classification")

        cat_prompt_base = (
            "You are an evidence classification expert.\n\n"
            "KNOWN EVIDENCE TYPES:\n"
            + evidence_summary_docs
            + "\n\nDOCUMENT CHUNK:\n{chunk}\n\n"
            "Identify which evidence types from the list are present in this chunk. "
            "Extract relevant content snippets.\n\n"
            "Return ONLY valid JSON (no markdown):\n"
            '[{"artifact": "<artifact name>", "content": "<relevant snippet>",}]'
        )
        self.logger.info("Starting evidence classification (text chunks — per file)")

        # Process each file independently so findings are attributed to the correct file
        for f in extracted_files:
            content = f.get("content", "").strip()
            if not content:
                continue
            s3_key = f.get("s3_key", "")
            cf_url = attach_CLDFRNT_url(s3_key) if s3_key else f.get("filename", "")
            self.logger.debug(
                "Classifying file %s (%d chars)", cf_url or "?", len(content)
            )

            for chunk in _make_chunks(content):
                try:
                    prompt = cat_prompt_base.replace("{chunk}", chunk)
                    resp = await get_fireworks_response2(
                        user_message=prompt,
                        role="user",
                        temp=0.1,
                        user_id=self.userid,
                        credits=self.credits,
                    )
                    parsed = safe_json_load(resp)
                    items = (
                        parsed
                        if isinstance(parsed, list)
                        else parsed.get("found", []) if isinstance(parsed, dict) else []
                    )
                    for item in items:
                        artifact = item.get("artifact", "")
                        snippet = item.get("content", "")
                        if artifact and snippet:
                            entry = evidence_map.setdefault(
                                artifact, {"snippets": [], "files": set()}
                            )
                            entry["snippets"].append(snippet)
                            if cf_url:
                                entry["files"].add(cf_url)
                except Exception as e:
                    self.logger.error(
                        "Evidence categorization chunk failed: %s", e, exc_info=IS_DEV
                    )

        if inp_links:
            self.logger.info("INSIDE IMAGES EXTRACTION — %d image(s)", len(inp_links))
            for idx, data_uri in enumerate(inp_links):
                try:
                    self.logger.info("Processing image %d/%d", idx + 1, len(inp_links))
                    img_s3_key = inp_link_keys[idx] if idx < len(inp_link_keys) else ""
                    img_cf_url = attach_CLDFRNT_url(img_s3_key) if img_s3_key else ""
                    result = await get_think_bedrock_vision_image(
                        data_uri=data_uri,
                        evidence_summary=evidence_summary,
                        user_id=self.userid,
                        credits=self.credits,
                    )
                    if not result:
                        self.logger.warning("No result for image %d", idx + 1)
                        continue

                    meta = result.get("image_meta", {})
                    self.logger.info(
                        "Image %d meta — type=%s timestamps=%s log_entries=%d",
                        idx + 1,
                        meta.get("image_type", "unknown"),
                        meta.get("timestamps", []),
                        len(meta.get("log_entries", [])),
                    )

                    for item in result.get("found", []):
                        artifact = item.get("artifact", "")
                        content = item.get("content", "")
                        if artifact and content:
                            entry = evidence_map.setdefault(
                                artifact, {"snippets": [], "files": set()}
                            )
                            entry["snippets"].append(content)
                            if img_cf_url:
                                entry["files"].add(img_cf_url)
                except Exception as e:
                    self.logger.error(
                        "Vision categorization failed for image %d: %s",
                        idx + 1,
                        e,
                        exc_info=IS_DEV,
                    )

        # ===========================
        # STEP 3: EVALUATE CONTENT vs EXPECTATIONS, THEN SPLIT
        # ===========================
        artifact_expectations = {
            e.get("artifact"): e.get("expectations", "") for e in user_evidence
        }

        admissible_evidence = {}
        inadmissible_evidence = {}
        discarded_evidence = {}

        for artifact, data in evidence_map.items():
            expectations_str = artifact_expectations.get(artifact, "")
            content_passes = True
            rejection_reason = ""

            if expectations_str and data["snippets"]:
                snippets_text = "\n".join(data["snippets"][:5])
                eval_prompt = (
                    f"ARTIFACT TYPE: {artifact}\n"
                    f"EXPECTATIONS: {expectations_str}\n\n"
                    f"EVIDENCE CONTENT:\n{snippets_text[:3000]}\n\n"
                    "Does the evidence content actually satisfy these expectations? "
                    "Be strict — if the content is generic, unrelated, or missing key elements, it fails.\n"
                    'Return ONLY JSON: {"passes": true, "reason": ""} or {"passes": false, "reason": "<specific reason>"}'
                )
                try:
                    resp = await get_fireworks_response2(
                        user_message=eval_prompt,
                        role="user",
                        temp=0.0,
                        user_id=self.userid,
                        credits=self.credits,
                    )
                    eval_data = safe_json_load(resp)
                    if isinstance(eval_data, dict):
                        content_passes = eval_data.get("passes", True)
                        rejection_reason = eval_data.get("reason", "")
                except Exception as e:
                    self.logger.error(
                        "Expectations eval failed for %s: %s",
                        artifact,
                        e,
                        exc_info=IS_DEV,
                    )

            if not content_passes:
                inadmissible_evidence[artifact] = {**data, "reason": rejection_reason}
                continue

            if runbook_evidence_config:
                if artifact in allowed_artifacts:
                    admissible_evidence[artifact] = data
                elif artifact in disallowed_artifacts:
                    inadmissible_evidence[artifact] = {
                        **data,
                        "reason": "Artifact type not allowed by runbook configuration.",
                    }
                else:
                    discarded_evidence[artifact] = data
            else:
                # No runbook config → everything found is admissible
                admissible_evidence[artifact] = data

        self.logger.info("Admissible evidence: %s", list(admissible_evidence.keys()))
        self.logger.info(
            "Inadmissible evidence: %s", list(inadmissible_evidence.keys())
        )
        self.logger.debug("Discarded evidence: %s", list(discarded_evidence.keys()))
        # ===========================
        # STEP 4: ANSWER QUESTIONS
        # ===========================
        answers_map = {}
        total_updated = 0

        def persist_partial(new_answers):
            updated = 0
            for qid, ans_data in new_answers.items():
                answer = ans_data["answer"] if isinstance(ans_data, dict) else ans_data
                answered_by = (
                    ans_data.get("answered_by_evidence")
                    if isinstance(ans_data, dict)
                    else None
                )

                if isinstance(execution_data, dict):
                    for s_id, step_data in execution_data.items():
                        if step_id and str(s_id) != str(step_id):
                            continue
                        for out in step_data.get("output", []):
                            if out.get("id") == qid and not out.get("user_answer"):
                                out["user_answer"] = answer
                                if answered_by:
                                    out["answered_by_evidence"] = answered_by
                                updated += 1
                                break

                for chat in chats:
                    if step_id and str(chat.get("step_id")) != str(step_id):
                        continue
                    for out in chat.get("output", []):
                        if out.get("id") == qid and not out.get("user_answer"):
                            out["user_answer"] = answer
                            if answered_by:
                                out["answered_by_evidence"] = answered_by
                            break

            self.previous_data = execution_data
            self.chat_history = chats
            self.saveworkflowtos3()
            return updated

        for q in [q for q in assigned_ques if q.get("id") not in answered_qids]:
            qid = q.get("id")
            evidence_required = q.get("evidence_required", [])

            relevant_evidence = (
                {
                    k: admissible_evidence[k]
                    for k in evidence_required
                    if k in admissible_evidence
                }
                if evidence_required
                else admissible_evidence
            )
            if not relevant_evidence:
                continue

            evidence_context = "\n\n".join(
                f"[EVIDENCE TYPE: {art}]\n" + "\n".join(data["snippets"][:5])
                for art, data in relevant_evidence.items()
            )
            evidence_sources = [
                {
                    "filename": fname,
                    "typeof_evidence": art,
                    "summary_subject_matter": (
                        data["snippets"][0][:200] if data["snippets"] else ""
                    ),
                }
                for art, data in relevant_evidence.items()
                for fname in data["files"]
            ]

            for ev_chunk in _make_chunks(evidence_context):
                if qid in answered_qids:
                    break
                try:
                    prompt = (
                        "You are a STRICT QUESTION ANSWERING ENGINE.\n"
                        "Answer ONLY from EVIDENCE CONTEXT. NO hallucination. NO guessing.\n\n"
                        f"EVIDENCE CONTEXT:\n{ev_chunk}\n\n"
                        f"QUESTION ID: {qid}\n"
                        f"Question: {q.get('question', '')}\n"
                        f"Options: {json.dumps(q.get('options', {}), ensure_ascii=False)}\n\n"
                        'Return ONLY JSON: {"id": "...", "user_answer": "..." OR null}'
                    )
                    resp = await get_fireworks_response2(
                        user_message=prompt,
                        role="user",
                        temp=0.0,
                        user_id=self.userid,
                        credits=self.credits,
                    )
                    parsed = safe_json_load(resp)
                    if isinstance(parsed, list):
                        parsed = parsed[0] if parsed else {}
                    answer = (
                        parsed.get("user_answer") if isinstance(parsed, dict) else None
                    )

                    if answer and answer not in [None, "", "null", "N/A"]:
                        answers_map[qid] = {
                            "answer": str(answer).strip(),
                            "answered_by_evidence": evidence_sources,
                        }
                        answered_qids.add(qid)
                        total_updated += persist_partial({qid: answers_map[qid]})
                        break
                except Exception as e:
                    self.logger.error(
                        "Question %s answering failed: %s", qid, e, exc_info=IS_DEV
                    )

        # ===========================
        # STEP 5: EVIDENCE-BASED QUESTIONS
        # ===========================
        evidence_based_questions = self.workflow_json.get(
            "evidence_based_questions", []
        )
        existing_ev_qids = {q.get("id") for q in evidence_based_questions}
        new_ev_questions = []
        ev_q_counter = len(evidence_based_questions) + 1

        config_to_check = (
            runbook_evidence_config if runbook_evidence_config else user_evidence
        )
        for ev_cfg in config_to_check:
            artifact = ev_cfg.get("artifact", "")
            decision = (
                ev_cfg.get("decision")
                if ev_cfg.get("decision") is not None
                else ev_cfg.get("Decision")
            )
            if runbook_evidence_config and decision is False:
                continue
            expectations_str = ev_cfg.get("expectations", "")
            if (
                not expectations_str
                or not artifact
                or artifact not in admissible_evidence
            ):
                continue

            expectation_points = [
                p.strip() for p in expectations_str.split(";") if p.strip()
            ]
            snippets_text = "\n".join(admissible_evidence[artifact]["snippets"][:3])

            for point in expectation_points:
                check_prompt = (
                    f"ARTIFACT: {artifact}\n"
                    f"EXPECTATION POINT: {point}\n\n"
                    f"EVIDENCE:\n{snippets_text[:3000]}\n\n"
                    "Does the evidence content satisfy this specific expectation point?\n"
                    'Return ONLY JSON: {"met": true} or {"met": false}'
                )
                try:
                    check_resp = await get_fireworks_response2(
                        user_message=check_prompt,
                        role="user",
                        temp=0.0,
                        user_id=self.userid,
                        credits=self.credits,
                    )
                    check_data = safe_json_load(check_resp)
                    met = (
                        check_data.get("met", True)
                        if isinstance(check_data, dict)
                        else True
                    )
                    if not met:
                        new_qid = f"evidence_{ev_q_counter}"
                        if new_qid not in existing_ev_qids:
                            gen_prompt = (
                                f"ARTIFACT TYPE: {artifact}\n"
                                f"MISSING EXPECTATION: {point}\n\n"
                                "The uploaded evidence does NOT satisfy the above expectation.\n"
                                "Generate:\n"
                                "1. A clear, specific question asking the user to address this gap.\n"
                                "2. An 'information' field (1-2 sentences) explaining WHY this question "
                                "is being asked and what evidence or detail the user should provide.\n\n"
                                'Return ONLY JSON: {"question": "...", "information": "..."}'
                            )
                            try:
                                gen_resp = await get_fireworks_response2(
                                    user_message=gen_prompt,
                                    role="user",
                                    temp=0.4,
                                    user_id=self.userid,
                                    credits=self.credits,
                                )
                                gen_data = safe_json_load(gen_resp) or {}
                            except Exception:
                                gen_data = {}
                            ai_question = (
                                gen_data.get("question")
                                or f"Please provide evidence for: {point}"
                            )
                            ai_information = gen_data.get("information") or (
                                f"This question is raised because the '{artifact}' evidence did not satisfy: {point}."
                            )
                            # new_ev_questions.append(
                            #     {
                            #         "id": new_qid,
                            #         "section": ev_cfg.get("type", "Evidence"),
                            #         "subsection": artifact,
                            #         "question": ai_question,
                            #         "information": ai_information,
                            #         "options": {
                            #             "A": "Provide a verbal / text answer",
                            #             "B": "Upload new evidence",
                            #             "C": "Discard — not applicable",
                            #         },
                            #         "discard_options": ["C"],
                            #         "upload_options": ["B"],
                            #         "text_options": ["A"],
                            #         "user_answer": None,
                            #         "comment": None,
                            #         "evidence_artifact": artifact,
                            #         "missing_expectation": point,
                            #     }
                            # )
                            new_ev_questions.append(
                                {
                                    "id": new_qid,
                                    "section": ev_cfg.get("type", "Evidence"),
                                    "subsection": artifact,
                                    "question": ai_question,
                                    "information": ai_information,
                                    "options": {
                                        "A": "Provide a verbal / text answer",
                                        "B": "Upload new evidence",
                                        # "C": "Discard — not applicable",
                                    },
                                    # "discard_options": ["C"],
                                    "upload_options": ["B"],
                                    "text_options": ["A"],
                                    "user_answer": None,
                                    "comment": None,
                                    "evidence_artifact": artifact,
                                    "missing_expectation": point,
                                }
                            )
                            ev_q_counter += 1
                except Exception as e:
                    self.logger.error(
                        "Expectation check failed for %s / %s: %s",
                        artifact,
                        point,
                        e,
                        exc_info=IS_DEV,
                    )

        if new_ev_questions:
            evidence_based_questions.extend(new_ev_questions)
            self.workflow_json["evidence_based_questions"] = evidence_based_questions

        # ===========================
        # SAVE EVIDENCE OVERVIEW + FILE REFS
        # ===========================
        cf_urls = [attach_CLDFRNT_url(k) for k in file_keys if k]

        current_urls = self.workflow_json.get("evidences_ques", [])
        current_urls.extend(cf_urls)
        self.workflow_json["evidences_ques"] = current_urls

        self.workflow_json["evidence_overview"] = {
            "admissible": [
                {
                    "artifact": k,
                    "files": list(v["files"]),
                    "summary": v["snippets"][0] if v["snippets"] else "",
                }
                for k, v in admissible_evidence.items()
            ],
            "inadmissible": [
                {
                    "artifact": k,
                    "files": list(v["files"]),
                    "summary": v.get("reason", ""),
                }
                for k, v in inadmissible_evidence.items()
            ],
            "discarded": [
                {"artifact": k, "files": []} for k, v in discarded_evidence.items()
            ],
        }
        self.saveworkflowtos3()

        # Trigger runbook task after save when the questionnaire is fully answered.
        if self._question_answer_stats()["all_answered"]:
            runbook_id = self.workflow_json.get("runbook_id")
            if runbook_id:
                self.logger.info("All questions answered, triggering runbook task")
                self._trigger_runbook_owner(runbook_id)

        remaining = [q for q in assigned_ques if q.get("id") not in answered_qids]
        questions_needing_evidence = [
            {
                "id": q["id"],
                "question_number": q.get("question_number"),
                "question": q["question"],
                "evidence_required": q["evidence_required"],
            }
            for q in remaining
            if q.get("evidence_required")
        ]
        self.logger.info(
            "Evidence pipeline complete: updated=%d  answered=%d remaining=%d ev_questions=%d admissible=%s",
            total_updated,
            len(answers_map),
            len(remaining),
            len(new_ev_questions),
            list(admissible_evidence.keys()),
        )
        return {
            "status": "success",
            "updated_answers": total_updated,
            "total_questions": len(assigned_ques),
            "answered_now": len(answers_map),
            "remaining_unanswered": len(remaining),
            "evidence_based_questions_added": len(new_ev_questions),
            "admissible_evidence_types": list(admissible_evidence.keys()),
            "inadmissible_evidence_types": list(inadmissible_evidence.keys()),
            "questions_needing_evidence": questions_needing_evidence,
        }

    def answer_evidence_question(
        self, qid: str, user_answer, comment=None, evidence_url=None
    ):
        evidence_based_questions = self.workflow_json.get(
            "evidence_based_questions", []
        )
        target = None
        for q in evidence_based_questions:
            if q.get("id") == qid:
                target = q
                break

        if not target:
            return {
                "status": "error",
                "message": f"Evidence question '{qid}' not found",
            }

        discard_options = target.get("discard_options", [])
        upload_options = target.get("upload_options", [])
        text_options = target.get("text_options", [])

        if user_answer in discard_options:
            self.workflow_json["evidence_based_questions"] = [
                q for q in evidence_based_questions if q.get("id") != qid
            ]
            answer_type = "discarded"
        elif user_answer in upload_options:
            target["user_answer"] = user_answer
            target["answer_type"] = "upload"
            if evidence_url:
                current_urls = self.workflow_json.get("evidences_ques", [])
                current_urls.append(attach_CLDFRNT_url(evidence_url))
                self.workflow_json["evidences_ques"] = current_urls
            if comment is not None:
                target["comment"] = comment
            answer_type = "upload"
        elif user_answer in text_options:
            if not comment or not str(comment).strip():
                return {
                    "status": "error",
                    "message": "A text answer requires a non-empty comment.",
                }
            target["user_answer"] = user_answer
            target["answer_type"] = "text"
            target["comment"] = str(comment).strip()
            answer_type = "text"
        else:
            # Legacy / unknown option — store as-is
            target["user_answer"] = user_answer
            target["answer_type"] = "unknown"
            if comment is not None:
                target["comment"] = comment
            answer_type = "unknown"

        save_playbook_to_s3(
            self.workflow_json,
            self.userid,
            "evidence question answered",
            self.workflow_json["filename"],
        )

        return {
            "status": "success",
            "qid": qid,
            "answer_type": answer_type,
        }

    def edit_assigned_question(self, qid: str, new_question: str):
        workflow = self.workflow_json

        if not new_question or not new_question.strip():
            return {
                "status": "error",
                "message": "Question text cannot be empty",
            }

        assigned_questions = workflow.get("assigned_questions", [])
        updated = False

        for q in assigned_questions:
            if q.get("id") == qid:
                q["question"] = new_question.strip()
                updated = True
                break

        if not updated:
            return {
                "status": "error",
                "message": f"Question ID '{qid}' not found",
            }
        self.workflow_json["assigned_questions"] = assigned_questions

        save_playbook_to_s3(
            self.workflow_json,
            self.userid,
            "question updated",
            self.workflow_json["filename"],
        )

        return {
            "status": "success",
            "message": "Question updated successfully",
            "qid": qid,
        }

    def delete_assigned_question(self, qid: str):
        workflow = self.workflow_json

        assigned_questions = workflow.get("assigned_questions", [])
        new_questions = [q for q in assigned_questions if q.get("id") != qid]

        if len(new_questions) == len(assigned_questions):
            return {
                "status": "error",
                "message": f"Question ID '{qid}' not found in assigned_questions",
            }

        # workflow["assigned_questions"] = new_questions
        self.workflow_json["assigned_questions"] = new_questions

        save_playbook_to_s3(
            self.workflow_json,
            self.userid,
            "workflow updated successfully",
            self.workflow_json["filename"],
        )
        return {
            "status": "success",
            "message": "Question deleted successfully",
            "qid": qid,
        }

    def morph_question(
        self,
        qid: str,
        new_question: str,
        morph_type: str,
        new_options: dict = None,
    ):
        workflow = self.workflow_json

        # ----------------------------
        # VALIDATION
        # ----------------------------
        if not new_question or not new_question.strip():
            return {
                "status": "error",
                "message": "Question text cannot be empty",
            }

        if morph_type not in ["text_to_option", "option_to_text", "update_only"]:
            return {
                "status": "error",
                "message": "Invalid morph_type. Allowed: text_to_option, option_to_text, update_only",
            }

        assigned_questions = workflow.get("assigned_questions", [])
        updated = False

        for q in assigned_questions:
            if q.get("id") == qid:

                # ----------------------------
                # ALWAYS UPDATE QUESTION TEXT
                # ----------------------------
                q["question"] = new_question.strip()

                # ----------------------------
                # MORPH LOGIC
                # ----------------------------
                if morph_type == "text_to_option":
                    if not new_options or not isinstance(new_options, dict):
                        return {
                            "status": "error",
                            "message": "new_options must be provided for text_to_option",
                        }

                    q["options"] = new_options

                elif morph_type == "option_to_text":
                    # Remove options completely
                    q["options"] = {}

                updated = True
                break

        if not updated:
            return {
                "status": "error",
                "message": f"Question ID '{qid}' not found",
            }

        # ----------------------------
        # SAVE BACK TO S3
        # ----------------------------
        self.workflow_json["assigned_questions"] = assigned_questions

        save_playbook_to_s3(
            self.workflow_json,
            self.userid,
            "question morphed",
            self.workflow_json["filename"],
        )

        return {
            "status": "success",
            "message": "Question morphed successfully",
            "qid": qid,
            "morph_type": morph_type,
        }

    def assign_evidence_required(self, qid: str, evidences_required: list):
        workflow = self.workflow_json
        assigned_questions = workflow.get("assigned_questions", [])
        updated = False

        for q in assigned_questions:
            if q.get("id") == qid:
                q["evidence_required"] = evidences_required
                updated = True
                break

        if not updated:
            return {
                "status": "error",
                "message": f"Question ID '{qid}' not found",
            }

        self.workflow_json["assigned_questions"] = assigned_questions

        save_playbook_to_s3(
            self.workflow_json,
            self.userid,
            "evidence assigned",
            self.workflow_json["filename"],
        )

        return {
            "status": "success",
            "message": "Evidence requirements assigned successfully",
            "qid": qid,
            "evidence_required": evidences_required,
        }
