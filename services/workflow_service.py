from collections import defaultdict
from datetime import datetime, timedelta
import json
import os, time
from typing import *
import re
from cust_helpers import pathconfig
from db.db_checkers import fetch_contacts_by_user, get_userinfo
from db.rds_db import connect_to_rds
from playbook.helperzz import save_playbook_to_s3
from utils.base_logger import get_logger
from utils.fireworkzz import get_evaluator_fireworks, get_fireworks_response
from utils.normal import can_reply_to_email, load_yaml_file, read_function_jsons
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
        self.previous_data = (
            self.workflow_json["testing"]
            if self.testing
            else self.workflow_json["online"]
        )
        self.logger = get_logger(__name__)
        self.ai_made_output = {}
        self.current_implemented_functions = read_function_jsons()

    def get_current_chats(self):
        allchats = self.workflow_json.get("chat", [])
        chat_log = self.workflow_json.get("chat_log", {})
        last_chat_check = chat_log.get("last_chat_summarized")
        last_summarization = chat_log.get("chat_summarization") or ""
        if last_chat_check:
            mixchats = allchats[-5:]
            return {"chat": mixchats, "chat_summarization": last_summarization}
        else:
            return {"chat": allchats, "chat_summarization": last_summarization}

    def generate_unique_id(self, existing_ids):
        while True:
            uid = str(uuid.uuid4().int)[0:6]
            if uid not in existing_ids:
                return uid

    def prompt_template_load(self):
        return load_yaml_file(path=pathconfig.play_template)

    def get_chat_summarization(self, chats_obj=None):
        template_data = self.prompt_template_load()

        chat_block = template_data.get("chat_summarization", {})
        prompt_instructions = chat_block.get("instructions")

        if not isinstance(prompt_instructions, str):
            raise TypeError(
                "Invalid template: chat_summarization.instructions must be a string"
            )
        if chats_obj is None:
            chats_obj = self.get_current_chats()
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
        print("res chat summarize", result)
        if result and "summary" in result:
            return result["summary"]
        return result

    def ai_input_intent_classifier(self, userinput):
        template_data = self.prompt_template_load()
        prompt_instructions = template_data.get("input_intent_classifier", {})
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
            .replace("{{chat}}", new_chat_json)
            .replace("{{chat_summarization}}", previous_summary or "")
            .strip()
        )
        result = self.get_parsed_fireworks_response(prompt_text)
        return result

    def ai_workflow_conversation_handler(self, userinput):
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

    def ai_detect_and_route_input(self, userinput):
        template_data = self.prompt_template_load()
        prompt_instructions = template_data.get("detect_and_route_input2", {})
        if not isinstance(prompt_instructions, str):
            raise TypeError(
                "Invalid template structure: expected string for 'instructions'."
            )
        chats_obj = self.get_current_chats()
        new_chat = chats_obj.get("chat", [])
        previous_summary = chats_obj.get("chat_summarization", "")
        previous_data = self.previous_data
        baseworkflow = self.workflow_json.get("workflow", {})

        # Serialize new chat messages
        new_chat_json = json.dumps(new_chat, ensure_ascii=False, indent=2)
        prompt_text = (
            prompt_instructions.replace("{{user_input}}", userinput)
            .replace("{{workflow_json}}", json.dumps(baseworkflow))
            .replace("{{previous_data}}", json.dumps(previous_data))
            .replace("{{current_chats}}", json.dumps(new_chat_json))
            .replace("{{previous_chat_summary}}", previous_summary)
        ).strip()
        newresultds = self.get_parsed_fireworks_response(prompt_text)
        print("res workflow", newresultds)
        return newresultds

    def ai_reset_intent_handler(self, userinput):
        template_data = self.prompt_template_load()
        custeps = self.steps

        # Map id -> title
        step_map = {str(step["id"]): step["title"] for _, step in custeps.items()}

        # Proper steptitles list for prompt
        steptitles = [{str(step["id"]): step["title"]} for _, step in custeps.items()]
        # print("step titles", steptitles)

        chats_obj = self.get_current_chats()
        new_chat = chats_obj.get("chat", [])
        previous_summary = chats_obj.get("chat_summarization", "")
        previous_data = self.previous_data
        # print("completed step ids", done_step_ids)

        # ✅ Build executed steps with titles
        done_steps_with_titles = {
            sid: step_map.get(str(sid), "") for sid in previous_data
        }
        # print("completed steps with titles", done_steps_with_titles)

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
        print("res reset", newresultds)
        return newresultds

    def get_parsed_fireworks_response(self, prompt_text, role="system"):
        """
        Get and parse Fireworks response.
        Retries once if the response is empty, invalid, or {}.
        """
        for attempt in range(2):  # attempt 0 = first, attempt 1 = retry once
            response_text = get_fireworks_response(prompt_text, role=role).strip()
            response_text = re.sub(
                r"^```(?:json)?\s*|\s*```$", "", response_text, flags=re.MULTILINE
            ).strip()

            if not response_text:
                print(f"[Retry {attempt+1}] Empty response from Fireworks.")
                time.sleep(0.3)
                continue
            # print("res text", response_text)

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
        print("[Error] Fireworks response invalid after one retry.")
        return {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.connection:
            self.connection.close()

    def get_attendees(self, attendees):
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
        allowed_keys = ["chat", "testing", "execution_logs", "online", "chat_log"]
        original_json = read_json_from_s3(self.wf_loc)

        for key in allowed_keys:
            if key in self.self.workflow_json:
                original_json[key] = self.workflow_json[key]

        # upload_any_file(
        #     file_path=json.dumps(original_json, indent=2),
        #     user_id=self.userid,
        #     file_name=self.filename,
        # )

        return save_playbook_to_s3(
            original_json,
            self.userid,
            "workflow updated successfully",
            self.filename,
        )

    def execute(self):
        current_step_id = self._get_first_step()
        if not current_step_id:
            self.logger.error("No valid start step found.")
            return

        visited = defaultdict(int)
        MAX_RETRIES_PER_STEP = 2

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

            # Skip step until all requirements are fulfilled
            if step.get("requirements_needed"):
                self.logger.warning(
                    f"Step '{step['title']}' requires inputs: {step['requirements_needed']}. Skipping until fulfilled."
                )
                break  # or continue to next, depending on workflow logic

            try:
                self.logger.info(f"Executing step: {step['title']} [{step['id']}]")
                result = self._execute_step(step)
                self.execution_log.append(
                    {
                        "step_id": step["id"],
                        "step_title": step["title"],
                        "result": result,
                    }
                )
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

    def _execute_step(self, step: Dict[str, Any]) -> Dict[str, Any]:
        step_type = step.get("type")

        # Handle communication steps
        if step_type == "communication":
            return self._handle_communication(step)
        elif step_type == "self-learn":
            return self._handle_self_learn(step)
        elif step_type == "navigation":
            return self._handle_navigation(step)
        else:
            raise ValueError(f"Unknown step type: {step_type}")

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
        result = get_fireworks_response(step["ai_instructions"])
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
            "automate": ("services.autopilot_service", "AutoMateService"),
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

        print("function triggering in process", func_name, args)

        # Prepare constructor arguments for each service
        if service_prefix == "google_meet":
            contacts = self.get_attendees(self.contacts)
            instance = service_class(
                access_token=self.userdetails["token"],
                user_email=self.userdetails.get("email"),
                contacts=contacts,
                userid=self.userid,
                testing=self.testing,
                workflow=self.workflow_json,
                wf_id=self.current_wf_id,
            )
            print("triggering google service")
        elif service_prefix == "gmail":
            instance = service_class(
                user_id=self.userid,
                connection=None,
                testing=self.testing,
                workflow=self.workflow_json,
                wf_id=self.current_wf_id,
            )
            print("triggering gmail service")
        elif service_prefix == "automate":
            instance = service_class(
                userid=self.userid,
                testing=self.testing,
                workflow=self.workflow_json,
                wf_id=self.current_wf_id,
            )
            print("triggering automate service")
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

            print("triggering twilio service")
        else:
            raise ValueError(f"Constructor not handled for service {service_prefix}")
        # attendees = args["contacts"] if args["contacts"] else None

        # # Replace contacts="all" with self.contacts
        # if not isinstance(attendees, list):
        #     args["contacts"] = self.get_attendees(attendees)
        # else:
        #     # If list contains "all" or "All", expand it
        #     if len(attendees) == 1 and any(a.lower() == "all" for a in attendees):
        #         args["contacts"] = self.get_attendees(attendees)

        # Get the method
        func = getattr(instance, method_name, None)
        if not func:
            raise ValueError(f"Method '{method_name}' not found in {class_name}")

        # Call the function with the provided args
        # print(method_name, instance, args)
        return func(**args)
        # return "ok"

    def check_input_tone(self, user_input: str):
        """
        Analyzes user input using the detect_and_route_input AI prompt.
        Determines if input is conversational, triggers workflow execution,
        or requests workflow modification/improvement.
        Logs all AI interactions into workflow_json["chat"].
        """

        # --- Load AI prompt ---
        template_data = load_yaml_file(path=pathconfig.play_template)
        prompt_instructions = template_data.get("detect_and_route_input", {}).get(
            "instructions", ""
        )
        if not isinstance(prompt_instructions, str):
            raise TypeError(
                "Invalid template structure: expected string for 'instructions'."
            )

        # --- Extract workflow metadata ---
        title = (
            self.workflow_json.get("input_data", {}).get("title", "your workflow")
            or "your workflow"
        )
        description = (
            self.workflow_json.get("input_data", {}).get("description", "")
            or "helping automate your process"
        )
        baseworkflow = self.workflow_json["workflow"]
        if "chat" in self.workflow_json:
            current_chats = self.workflow_json["chat"]
        else:
            current_chats = []
        # --- Log to chat ---
        # --- Assume you already have chat history loaded ---
        if current_chats:
            existing_ids = {entry["id"] for entry in current_chats}
            chid = self.generate_unique_id(existing_ids)
        else:
            chid = str(uuid.uuid4().int)[0:6]

        # --- Select context based on testing mode ---
        context_key = "testing" if self.testing else "online"
        if context_key not in self.workflow_json:
            self.workflow_json[context_key] = {}
        previous_data = self.workflow_json[context_key]

        # --- Build prompt text ---
        prompt_text = (
            prompt_instructions.replace("{{user_input}}", user_input)
            .replace("{{workflow_json}}", json.dumps(baseworkflow))
            .replace("{{workflow_title}}", title)
            .replace("{{workflow_description}}", description)
            .replace("{{previous_data}}", json.dumps(previous_data))
            .replace("{{current_chats}}", json.dumps(current_chats))
            .strip()
        )
        print("before ai trigger check input tone")

        ai_result = self.get_parsed_fireworks_response(prompt_text=prompt_text)
        print("test based ai result", ai_result)

        # --- Default fallback if AI failed ---
        if not isinstance(ai_result, dict) or not ai_result:
            ai_result = {
                "response_message": f"Let's continue working on the '{title}' workflow. {description}",
                "workflow_runner": False,
                "workflow_improv": False,
                "improv_input": None,
                "user_input_needed": "",
            }

        # --- Normalize keys ---
        response_message = ai_result.get("response_message", "").strip()
        workflow_runner = bool(ai_result.get("workflow_runner", False))
        workflow_improv = bool(ai_result.get("workflow_improv", False))
        improv_input = ai_result.get("improv_input", None)
        # --- Fallback message ---
        if not response_message:
            response_message = (
                f"Let's continue working on the '{title}' workflow. {description}"
            )
            workflow_runner = workflow_improv = False
            improv_input = None

        # --- Workflow execution ---
        if workflow_runner:
            self.logger.info(
                f"Detected workflow runner trigger for input: {user_input}"
            )
            return self.execute_from_text_input(
                user_input=response_message, base_input=user_input
            )

        # --- Workflow improvement ---
        if workflow_improv:
            self.logger.info(f"Detected workflow improvement request: {user_input}")
            improv_request = improv_input or user_input
            improv_result = self.update_steps_workflow(
                improv_request, workflow_json=self.workflow_json, user_id=self.userid
            )

            if isinstance(improv_result, dict) and "response_message" in improv_result:
                response_message = improv_result["response_message"]
            elif isinstance(improv_result, str):
                response_message = improv_result
            else:
                response_message = "The workflow update request was processed, but no response was returned."

        try:
            now = datetime.now()
            chat_entry = {
                "id": chid,
                "date": now.isoformat(),
                "input": user_input,
                "output": response_message,
                "status": (
                    "runner"
                    if workflow_runner
                    else "improv" if workflow_improv else "normal"
                ),
                "step_id": None,
            }

            if "chat" not in self.workflow_json or not isinstance(
                self.workflow_json["chat"], list
            ):
                self.workflow_json["chat"] = []
            self.workflow_json["chat"].append(chat_entry)
            self.saveworkflowtos3()
        except Exception as e:
            self.logger.warning(f"Failed to log chat entry: {e}")

        # --- Return structured output ---
        return {
            "response_message": response_message,
            "workflow_runner": workflow_runner,
            "workflow_improv": workflow_improv,
            "improv_input": improv_input,
        }

    def execute_from_text_input(self, user_input: str, base_input=None):
        """
        Executes a workflow step or handles human conversation dynamically.
        Tracks chat history, testing/online logs, execution logs, and saves workflow to S3.
        Skips execution if step already completed today unless `force=True`.
        """
        now = datetime.now()
        today_str = now.date().isoformat()

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

        # --- Prepare prompt with previous_data ---
        prompt_text = (
            prompt_instructions.replace("{{user_input}}", user_input)
            .replace("{{workflow_json}}", json.dumps(self.workflow_json))
            .replace("{{previous_data}}", json.dumps(previous_data))
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
            print("ai response", ai_result)

            step_id = ai_result.get("step_id")
            workflow_intent = ai_result.get("workflow_intent", False)
            message = ai_result.get("message", "")
            try:
                step_id = int(step_id)
            except (TypeError, ValueError):
                step_id = None
            # --- Handle reset actions from chat ---
            reset_action = ai_result.get("reset")
            if reset_action:
                if reset_action == "all":
                    target_section = "testing" if self.testing else "online"
                    self.workflow_json[target_section] = {}

                    message = (
                        "All testing data has been reset. You can now start testing again."
                        if self.testing
                        else "All online logs have been cleared."
                    )

                    result = {
                        "workflow_intent": True,
                        "reset": "all",
                        "message": message,
                    }

                    execution_status = "success"
                    execution_details = {"action": "reset_all"}

                elif reset_action == "id" and step_id is not None:
                    target_section = "testing" if self.testing else "online"
                    self.workflow_json[target_section].pop(str(step_id), None)

                    message = f"Step {step_id} testing data has been reset. You can now retest this step."
                    result = {
                        "workflow_intent": True,
                        "reset": "id",
                        "step_id": step_id,
                        "message": message,
                    }

                    execution_status = "success"
                    execution_details = {"action": "reset_step", "step_id": step_id}

                # --- Record chat entry for reset ---
                chat_entry = {
                    "id": chid,
                    "date": now.isoformat(),
                    "input": user_input,
                    "output": result["message"],
                    "status": execution_status,
                    "step_id": result.get("step_id") or step_id,
                }
                self.workflow_json["chat"].append(chat_entry)

                # --- Record in execution logs ---
                self.workflow_json["execution_logs"].append(
                    {
                        "timestamp": now.isoformat(),
                        "input": user_input,
                        "output": result["message"],
                        "status": execution_status,
                        "details": execution_details,
                        "step_id": result.get("step_id"),
                    }
                )

                # --- Save workflow ---
                self.saveworkflowtos3()
                return result

            if workflow_intent and step_id and self.check_step_exists(step_id):
                step = self.steps[step_id]
                self.current_wf_id = step_id
                function_args = ai_result.get("function_args", {})
                try:
                    if step.get("function_call"):
                        func_call = step.get("function_call", {})
                        func_name = func_call.get("function_name")
                        nfunction_args = func_call.get("arguments", {}) or {}
                        nfunction_args.update(function_args)

                        try:
                            execution_result = self._trigger_function(
                                func_name, nfunction_args
                            )
                        except Exception as e:
                            execution_result = {"error": str(e), "success": False}

                        # print("exe result:", execution_result)

                        # --- Determine if there was an error ---
                        raw_error = (
                            execution_result.get("error", "")
                            if isinstance(execution_result, dict)
                            else ""
                        )
                        is_http_error = "HttpError" in str(raw_error)
                        is_failed_flag = (
                            not execution_result.get("success", True)
                            if isinstance(execution_result, dict)
                            else False
                        )
                        has_error = bool(raw_error or is_failed_flag or is_http_error)

                        # --- Choose user-facing message ---
                        default_message = "Step executed successfully."

                        if has_error:
                            execution_status = "failed"
                            if is_http_error or "gmail" in func_name.lower():
                                message = (
                                    "There was a problem sending the email. "
                                    "Please check if the recipient address is valid and try again."
                                )
                            else:
                                # Extract return_str or fallback
                                if isinstance(execution_result, dict):
                                    message = (
                                        execution_result.get("return_str")
                                        or str(execution_result)
                                        or default_message
                                    )
                                else:
                                    message = str(execution_result) or default_message

                        else:
                            execution_status = "success"

                            # --- Handle different types and prioritize email_body if present ---
                            if execution_result is None:
                                message = default_message
                            elif isinstance(execution_result, dict):
                                if "email_body" in execution_result:
                                    message = execution_result[
                                        "email_body"
                                    ]  # prioritize email_body
                                else:
                                    message = (
                                        execution_result.get("return_str")
                                        or str(execution_result)
                                        or default_message
                                    )
                            elif isinstance(execution_result, (list, tuple, set)):
                                message = (
                                    ", ".join(map(str, execution_result))
                                    or default_message
                                )
                            elif isinstance(execution_result, (int, float, bool)):
                                message = str(execution_result)
                            elif isinstance(execution_result, str):
                                message = execution_result or default_message
                            else:
                                message = str(execution_result) or default_message

                        # --- Always log full technical details internally ---
                        execution_details = execution_result
                        # print("Execution details:", execution_details)
                        # print("User message:", message)

                    else:
                        self._handle_self_learn(step)
                        execution_status = "success"
                        execution_details = {"type": "self_learn"}
                        message = default_message

                    # --- Final structured result ---
                    result = {
                        "workflow_intent": True,
                        "step_id": step_id,
                        "message": message,
                        "execution_status": execution_status,
                        "execution_details": execution_details,
                    }

                except Exception as e:
                    # --- Handle fallback ---
                    fallback = self._find_fallback(step)
                    if fallback:
                        try:
                            self._handle_self_learn(fallback)
                            execution_status = "fallback"
                            execution_details = {"error": str(e)}
                            message = "Fallback step executed successfully."
                            result = {
                                "workflow_intent": True,
                                "step_id": fallback["id"],
                                "message": message,
                            }
                        except Exception:
                            execution_status = "failed"
                            execution_details = {"error": str(e)}
                            message = "problem in testing the step"
                            result = {
                                "workflow_intent": False,
                                "step_id": None,
                                "message": message,
                            }
                    else:
                        execution_status = "failed"
                        execution_details = {"error": str(e)}
                        message = "problem in testing the step"
                        result = {
                            "workflow_intent": False,
                            "step_id": None,
                            "message": message,
                        }
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
        if not already_done:
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
                        "details": execution_details,
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
                    "details": execution_details,
                    "step_id": result.get("step_id"),
                }
            )

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
