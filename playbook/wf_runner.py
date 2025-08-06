import uuid
import logging
from typing import List, Dict, Any, Optional
from gmail_route.gmail_service import GmailService
from utils.fireworkzz import get_fireworks_response
from utils.s3_utils import read_json_from_s3
from collections import defaultdict
from db.db_checkers import get_userinfo, fetch_contacts_by_user
import re


class WorkflowRunner:
    def __init__(self, userid: str, filename: str, contacts=None):
        self.userid = userid
        self.filename = filename
        self.wf_loc = f"{userid}/workflow/{filename}"
        self.workflow_json = read_json_from_s3(self.wf_loc)
        self.userdetails = get_userinfo(self.userid)
        self.maxRetries = 2
        self.workflow = self.workflow_json.get("workflow", {})
        self.steps = {step["id"]: step for step in self.workflow.get("steps", [])}
        self.input_data = self.workflow_json.get("input_data", {})
        self.clarification_answers = {
            item["quote"]: item["answer"]
            for item in self.workflow_json.get("clarification_answers", [])
        }
        self.contacts = contacts or fetch_contacts_by_user(self.userid)
        self.execution_log: List[Dict[str, Any]] = []
        self.logger = logging.getLogger(f"WorkflowRunner-{userid}-{filename}")
        self.logger.setLevel(logging.INFO)
        self.meetingDetails = None
        if (
            not self.logger.handlers
        ):  # Prevent adding multiple handlers in debug mode or multiple runs
            handler = logging.StreamHandler()
            formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)

    def execute(self):
        start_step_id = self._get_first_step()
        if not start_step_id:
            self.logger.error("No valid start step found.")
            return

        current_step_id = start_step_id
        visited = defaultdict(int)
        MAX_RETRIES_PER_STEP = self.maxRetries

        while current_step_id:
            visited[current_step_id] += 1

            if visited[current_step_id] > MAX_RETRIES_PER_STEP:
                self.logger.warning(
                    f"Infinite loop detected at step {current_step_id}. Max retries exceeded."
                )
                break

            step = self.steps.get(current_step_id)
            if not step:
                self.logger.error(f"Step ID {current_step_id} not found.")
                break

            try:
                self.logger.info(f"Executing step: {step['title']} [{step['id']}]")
                step_result = self._execute_step(step)
                self.execution_log.append(
                    {
                        "step_id": step["id"],
                        "step_title": step["title"],
                        "result": step_result,
                    }
                )
                current_step_id = step_result.get("next_step")
            except Exception as e:
                self.logger.error(f"Error executing step {step['id']}: {e}")
                fallback = self._find_fallback_for_step(step)
                if fallback:
                    self.logger.warning(
                        f"Switching to fallback step: {fallback['title']} [{fallback['id']}]"
                    )
                    current_step_id = fallback["id"]
                else:
                    self.logger.error(
                        f"No fallback step defined for step {step['id']}. Ending execution."
                    )
                    break

    def _get_first_step(self) -> Optional[str]:
        # First step is usually the one not referenced by any `next_step`
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

        if step_type == "communication":
            return self._handle_communication(step)
        elif step_type == "navigation":
            return self._handle_navigation(step)
        elif step_type == "self-learn":
            return self._handle_self_learn(step)
        else:
            raise ValueError(f"Unknown step type: {step_type}")

    def _handle_communication(self, step: Dict[str, Any]) -> Dict[str, Any]:
        allowed_modes = [
            "Direct Message",
            "Scheduled",
            "Multichannel",
            "Automated message",
        ]

        communication_mode = step.get("communication_mode")
        channels = step.get("channels", [])

        if communication_mode not in allowed_modes:
            self.logger.error(
                f"Invalid communication_mode: '{communication_mode}'. Must be one of {allowed_modes}"
            )
            raise ValueError("Invalid communication_mode")

        ai_output = f"[COMMUNICATION] via {channels} ({communication_mode}) - {step['ai_instructions']}"
        self.logger.info(ai_output)

        # 👉 Route to appropriate handlers
        if "calendar_type" in step and step["calendar_type"] == "Google Calendar":
            # Call your Google Meet creation handler
            print("calling befor meet method")
            return self._handleGoogleMeet(step)

        elif "Gmail" in channels:
            # Call your Gmail invitation handler
            print("calling befor Gmail method")
            return self._handleGoogleMeetMail(step)

        # If it's a decision point, handle separately
        if step.get("decision_point"):
            print(f"Condition type: {step.get('decision_type', 'N/A')}")
            return self._handle_decision(step, ai_output)

        return {"output": ai_output, "next_step": step.get("next_step")}

    def _handle_navigation(self, step: Dict[str, Any]) -> Dict[str, Any]:
        # Simulate opening a page
        ai_output = f"[NAVIGATION] Go to {step.get('page_url')}"
        self.logger.info(ai_output)
        return {"output": ai_output, "next_step": step.get("next_step")}

    def _handle_self_learn(self, step: Dict[str, Any]) -> Dict[str, Any]:
        ai_output = f"[SELF-LEARN] {step['ai_instructions']}"
        self.logger.info(ai_output)

        # Pre-fill contacts if relevant
        objective = step.get("objective", "").lower()
        if re.search(
            r"\b(create|prepare|fetch|get)\b.*\b(contact list|contacts)\b", objective
        ):
            if len(self.contacts) < 1:
                self.contacts = fetch_contacts_by_user(self.userid)
            return {"output": ai_output, "next_step": step.get("next_step")}

        # Handle decision-based logic
        if step.get("decision_point"):
            return self._handle_decision(step, ai_output)

        # Non-decision self-learn step
        return {"output": ai_output, "next_step": step.get("next_step")}

    def _handle_decision(self, step: Dict[str, Any], ai_output: str) -> Dict[str, Any]:
        # Placeholder: simulate a decision outcome (first condition as default for now)
        conditions = step.get("condition", [])
        next_steps = step.get("next_step", [])

        # You could plug in actual AI/ML logic here later
        selected_index = 0
        selected_condition = conditions[selected_index] if conditions else "default"
        selected_next_step = next_steps[selected_index] if next_steps else None

        self.logger.info(
            f"[DECISION] Condition '{selected_condition}' matched → {selected_next_step}"
        )
        return {
            "output": f"{ai_output}\nDecision: {selected_condition}",
            "next_step": selected_next_step,
        }

    def _find_fallback_for_step(
        self, original_step: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        # Fallbacks are often separate steps not connected directly
        fallback_candidates = [
            step
            for step in self.steps.values()
            if "fallback" in step.get("title", "").lower()
            or "fallback" in step.get("objective", "").lower()
        ]
        for step in fallback_candidates:
            if step["id"] != original_step["id"]:
                return step
        return None

    def _handleGoogleMeet(self, step):
        from .services.meet_Service import GoogleMeetService
        from datetime import datetime

        print("called meet thing")

        email = self.userdetails["email"]
        token = self.userdetails["token"]
        scheduled_options = self.input_data.get("scheduled_options", {})

        # Set fallback/defaults
        start_time = scheduled_options.get("startTime", "09:00")
        end_time = scheduled_options.get("endTime", "10:00")
        start_date = scheduled_options.get("startDate", None)
        timezone = scheduled_options.get("timezone", "Asia/Kolkata")

        # Combine date and time
        if start_date:
            full_start = f"{start_date}T{start_time}:00"
            full_end = f"{start_date}T{end_time}:00"
        else:
            today = datetime.now().strftime("%Y-%m-%d")
            full_start = f"{today}T{start_time}:00"
            full_end = f"{today}T{end_time}:00"

        summary = f"Meeting Scheduled By {email} on {full_start}"
        service = GoogleMeetService(access_token=token, user_email=email)
        contacts = [item.get("email") for item in self.contacts if "email" in item]

        try:
            response = service.create_meeting(
                summary=summary,
                start_time=full_start,
                end_time=full_end,
                attendees=contacts,
                timezone=timezone,
            )
            self.logger.info(f"[GoogleMeet Created] {response}")
            self.meetingDetails = response
            return {
                "status": "success",
                "output": f"Meeting scheduled successfully: {response.get('hangoutLink')}",
                "next_step": step.get("next_step"),
            }
        except Exception as e:
            self.logger.error(f"[GoogleMeet Error] {e}")
            return {"status": "error", "message": str(e)}

    def _handleGoogleMeetMail(self, step):
        from gmail_route.gmail_service import GmailService
        from .helperzz import generate_meeting_email_body

        print("called meet mail thing")

        details = self.meetingDetails
        user_email = self.userdetails["email"]
        contacts = [item.get("email") for item in self.contacts if "email" in item]

        subject = "Meeting Scheduled"
        body = (
            generate_meeting_email_body(details, self.userdetails)
            or f"""
        Hi,

        A meeting has been scheduled.

        Summary: {details.get('summary')}
        Date & Time: {details.get('start_time')} to {details.get('end_time')} ({details.get('timezone')})
        Meeting Link: {details.get('hangoutLink', 'Link not available')}

        Regards,
        """
        )

        try:
            service = GmailService(self.userid)
            sent = service.send_Meet_mail(
                to_email=user_email, bcc_list=contacts, subject=subject, body=body
            )
            self.logger.info(f"Email sent successfully: {sent['id']}")
            return {
                "status": "success",
                "output": f"Email sent to {user_email} and bcc to {len(contacts)} people.",
                "next_step": step.get("next_step"),
            }
        except Exception as e:
            self.logger.error(f"Failed to send email: {e}")
            return {"status": "error", "message": str(e)}

    def get_execution_log(self) -> List[Dict[str, Any]]:
        return self.execution_log
