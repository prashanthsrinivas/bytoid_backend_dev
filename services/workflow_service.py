from collections import defaultdict
from datetime import datetime, timedelta
import json
from typing import *
import re
from cust_helpers import pathconfig
from db.db_checkers import fetch_contacts_by_user, get_userinfo
from db.rds_db import connect_to_rds
from utils.base_logger import get_logger
from utils.fireworkzz import get_fireworks_response
from utils.normal import load_yaml_file
from utils.s3_utils import read_json_from_s3


class WorkflowRunnerV2:
    def __init__(self, userid: str, filename: str, contacts=None):
        self.userid = userid
        self.filename = filename
        self.connection = connect_to_rds()
        self.wf_loc = f"{userid}/workflow/{filename}"
        self.workflow_json = read_json_from_s3(self.wf_loc)
        self.userdetails = get_userinfo(self.userid)
        self.contacts = contacts or fetch_contacts_by_user(self.userid)

        # Correctly load steps from workflow['steps'] instead of top-level steps
        workflow_steps = self.workflow_json.get("workflow", {}).get("steps", [])
        self.steps = {step["id"]: step for step in workflow_steps}

        self.input_data = self.workflow_json.get("input_data", {})
        self.execution_log: list[dict] = []
        self.logger = get_logger(__name__)
        self.ai_made_output = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.connection:
            self.connection.close()

    def check_step_exists(self, step_id):
        return str(step_id) in self.steps

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
            instance = service_class(
                access_token=self.userdetails["token"],
                user_email=self.userdetails.get("email"),
                contacts=self.contacts,
            )
        elif service_prefix == "gmail":
            instance = service_class(
                user_id=self.userid,
                connection=None,  # or pass self.conn if available
            )
        elif service_prefix == "automate":
            instance = service_class(userid=self.userid)
        elif service_prefix == "twilio":
            # Ensure Twilio credentials are in input_data
            instance = service_class(
                account_sid=self.input_data.get("twilio_account_sid"),
                auth_token=self.input_data.get("twilio_auth_token"),
                from_whatsapp_number=self.input_data.get("twilio_whatsapp_number"),
                from_sms_number=self.input_data.get("twilio_sms_number"),
                from_call_number=self.input_data.get("twilio_call_number"),
            )
        else:
            raise ValueError(f"Constructor not handled for service {service_prefix}")

        # Replace contacts="all" with self.contacts
        if "contacts" in args and args["contacts"] == "all":
            args["contacts"] = self.contacts

        # Get the method
        func = getattr(instance, method_name, None)
        if not func:
            raise ValueError(f"Method '{method_name}' not found in {class_name}")

        # Call the function with the provided args
        return func(**args)

    def execute_from_text_input(self, user_input: str):
        """
        Executes a workflow step or handles human conversation dynamically using a unified prompt.
        Returns only 'message', 'step_id', and 'workflow_intent'.
        Handles errors, fallbacks, and non-existing steps safely.
        """
        # Load prompt
        template_data = load_yaml_file(path=pathconfig.play_template)
        prompt_instructions = template_data.get("select_and_prepare_step", {}).get(
            "instructions", ""
        )

        if not isinstance(prompt_instructions, str):
            raise TypeError(
                f"Invalid template structure: expected string for 'instructions', got {type(prompt_instructions)}"
            )

        # Prepare prompt
        prompt_text = prompt_instructions.replace("{{user_input}}", user_input).replace(
            "{{workflow_json}}", json.dumps(self.workflow_json)
        )

        # Call AI
        response_text = get_fireworks_response(prompt_text, role="system").strip()
        response_text = re.sub(
            r"^```(?:json)?\s*|\s*```$", "", response_text, flags=re.MULTILINE
        ).strip()

        # Default result if AI fails
        result = {
            "workflow_intent": False,
            "step_id": None,
            "message": "I could not understand that input.",
        }

        if response_text:
            try:
                ai_result = json.loads(response_text)
            except Exception:
                ai_result = {}
            print("ai result", ai_result)

            # Ensure required keys
            step_id = ai_result.get("step_id")
            workflow_intent = ai_result.get("workflow_intent", False)
            message = ai_result.get("message", "")

            # Validate workflow_intent: step must exist
            if workflow_intent and step_id and self.check_step_exists(step_id):
                step = self.steps[step_id]

                # Prepare function_args
                function_args = ai_result.get("function_args", {})

                try:
                    if step.get("function_call"):
                        func_call = step.get("function_call", {})
                        func_name = func_call.get("function_name")
                        nfunction_args = func_call.get("arguments", {}) or {}
                        nfunction_args.update(function_args)
                        print("before trigger", func_name)

                        execution_result = self._trigger_function(
                            func_name, nfunction_args
                        )
                        self.execution_log.append(
                            {
                                "step_id": step["id"],
                                "step_title": step["title"],
                                "result": execution_result,
                            }
                        )

                        message = (
                            execution_result
                            if isinstance(execution_result, str)
                            else "Step executed successfully."
                        )
                    else:
                        # Handle self-learn step
                        self._handle_self_learn(step)
                        message = "Step executed successfully."

                    result = {
                        "workflow_intent": True,
                        "step_id": step["id"],
                        "message": message,
                    }
                    print(self.execution_log)
                    return result

                except Exception as e:
                    # Try fallback if available
                    fallback = self._find_fallback(step)
                    if fallback:
                        try:
                            self._handle_self_learn(fallback)
                            message = "Fallback step executed successfully."
                            result = {
                                "workflow_intent": True,
                                "step_id": fallback["id"],
                                "message": message,
                            }
                            print(self.execution_log, e)
                            return result
                        except Exception:
                            pass
                    # If no fallback or error persists
                    result = {
                        "workflow_intent": False,
                        "step_id": None,
                        "message": "Execution failed and no fallback available.",
                    }
                    print(self.execution_log, e)
                    return result
            else:
                # Step does not exist or workflow_intent false
                message = (
                    message
                    or "I could not find a step matching your request in this workflow."
                )
                result = {"workflow_intent": False, "step_id": None, "message": message}

        return result
