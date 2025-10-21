from multiprocessing import get_logger
import uuid
from typing import List, Dict, Any, Optional
from cust_helpers import pathconfig
from services.gmail_service import GmailService
from services.meet_service import GoogleMeetService
from utils.fireworkzz import get_fireworks_response
from utils.normal import load_yaml_file
from utils.s3_utils import read_json_from_s3
from collections import defaultdict
from db.db_checkers import get_userinfo, fetch_contacts_by_user
import re
from datetime import datetime, timedelta, timezone
import json
import pytz
from dateutil import parser


def normalize_scheduled_options(meeting_details: dict) -> dict:
    # Handle 'tomorrow' or relative date
    date_str = meeting_details.get("date", "")
    if date_str.lower() == "tomorrow":
        start_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        try:
            start_date = parser.parse(date_str).strftime("%Y-%m-%d")
        except Exception:
            start_date = datetime.now().strftime("%Y-%m-%d")  # fallback

    # Handle time
    try:
        time_obj = parser.parse(meeting_details.get("time", "09:00"))
        start_time = time_obj.strftime("%H:%M")
        end_time = (time_obj + timedelta(hours=1)).strftime("%H:%M")
    except Exception:
        start_time = "09:00"
        end_time = "10:00"

    return {
        "startTime": start_time,
        "endTime": end_time,
        "startDate": start_date,
        "timezone": "Asia/Kolkata",  # or dynamic if needed
    }


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
        self.logger = get_logger(__name__)
        self.meetingDetails = None
        self.ai_made_output = {}

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

    def _execute_single_step(self, step: Dict[str, Any]) -> Dict[str, Any]:
        return self._execute_step(step)

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

        # Check if it's a contact preparation step
        objective = step.get("objective", "").lower()
        if re.search(
            r"\b(create|prepare|fetch|get)\b.*\b(contact list|contacts)\b", objective
        ):
            if len(self.contacts) < 1:
                self.contacts = fetch_contacts_by_user(self.userid)
            return {
                "output": {"contacts": self.contacts},
                "next_step": step.get("next_step"),
            }
            # Handle special logic for checking availability
        elif "availability" in objective and "google calendar" in objective:
            self.logger.info("Checking availability using Google Calendar")
            availability = self._handleavailability_google()

            if step.get("decision_point"):
                if availability:
                    selected_next_step = step["next_step"][0]  # "Time slot available"
                    condition_msg = "Time slot available"

                    # Optionally update scheduled_options for future steps
                    self.input_data["scheduled_options"] = {
                        "startTime": availability["startTime"][-8:-3],  # Extract HH:MM
                        "endTime": availability["endTime"][-8:-3],
                        "startDate": availability["startDate"],
                        "timezone": self.input_data.get("scheduled_options", {}).get(
                            "timezone", "Asia/Kolkata"
                        ),
                    }

                else:
                    selected_next_step = step["next_step"][
                        1
                    ]  # "Time slot not available"
                    condition_msg = "Time slot not available"

                return {
                    "output": f"[AVAILABILITY] {availability or 'No slot found'}\nDecision: {condition_msg}",
                    "next_step": selected_next_step,
                }

            return {
                "output": f"{availability or 'No slot found'}",
                "next_step": step.get("next_step"),
            }
        self.logger.info("Triggering AI prompt for meeting/email generation")
        promptloc = load_yaml_file(path=pathconfig.play_template)
        prompttempl = promptloc.get("self-learn-context", "")

        inputprompt = (
            prompttempl.replace("{{input_data}}", json.dumps(self.input_data, indent=2))
            .replace("{{stepData}}", json.dumps(step, indent=2))
            .replace("{{userData}}", json.dumps(self.userdetails, indent=2))
            .replace(
                "{{clarificationData}}",
                json.dumps(self.clarification_answers, indent=2),
            )
        )

        content = get_fireworks_response(inputprompt, role="system")

        # Step 1: Normalize the output
        if hasattr(content, "text"):
            content_str = content.text
        elif isinstance(content, dict):
            content_str = content.get("message", "")
        else:
            content_str = str(content)

        # Step 2: Parse JSON from markdown or raw body
        match = re.search(r"```(?:json)?\s*(.*?)```", content_str, re.DOTALL)
        if match:
            cleaned_json = match.group(1).strip()
            try:
                parsed_output = json.loads(cleaned_json)
            except Exception as e:
                self.logger.error(f"Error parsing JSON from code block: {e}")
                parsed_output = {"message": cleaned_json}
        else:
            if (
                content_str.strip().lower().startswith("<!doctype html>")
                or "<html>" in content_str.lower()
            ):
                parsed_output = {"html_email_body": content_str.strip()}
            else:
                parsed_output = {"message": content_str.strip()}

        self.logger.warning(f"main parsed output -> {parsed_output}")
        if (
            "meeting_details" in parsed_output
            and "scheduled_options" not in self.input_data
        ):
            self.input_data = {
                "scheduled_options": normalize_scheduled_options(
                    parsed_output.get("meeting_details", {})
                )
            }

        # Save result
        self.ai_made_output[step["id"]] = parsed_output

        # Decision handling
        if step.get("decision_point"):
            return self._handle_decision(step, parsed_output)

        return {"output": parsed_output, "next_step": step.get("next_step")}

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

    def _handleGoogleMeet(self, step: Dict[str, Any]) -> Dict[str, Any]:

        try:
            # Get token/email
            token = self.userdetails["token"]
            email = self.userdetails["email"]
            meetData = self.input_data
            scheduled_options = meetData.get("scheduled_options", {})

            # Use dateutil.parser to handle timezones gracefully
            now = datetime.now(timezone.utc)
            timezone_str = scheduled_options.get("timezone", "Asia/Kolkata")

            # Handle time parsing for start time
            start_time_str = scheduled_options.get("startTime")
            start_date = scheduled_options.get("startDate") or now.strftime("%Y-%m-%d")

            if start_time_str:
                # Use parser for robustness
                start_datetime = parser.parse(f"{start_date} {start_time_str}")
            else:
                # If no startTime, round to next full hour and make it timezone-aware
                next_hour = (now + timedelta(hours=1)).replace(
                    minute=0, second=0, microsecond=0
                )
                start_datetime = next_hour

            # Handle time parsing for end time
            end_time_str = scheduled_options.get("endTime")
            if end_time_str:
                # Use parser for robustness
                end_datetime = parser.parse(f"{start_date} {end_time_str}")
            else:
                # If no endTime, set it one hour after start_datetime
                end_datetime = start_datetime + timedelta(hours=1)

            self.logger.info(
                f"[GoogleMeet] Start: {start_datetime}, End: {end_datetime}, Timezone: {timezone_str}"
            )

            # Collect basic info
            topic = meetData.get(
                "topic",
                f"Meeting Scheduled By {email} on {start_datetime.strftime('%Y-%m-%d %H:%M')}",
            )
            attendees = [item["email"] for item in self.contacts if "email" in item]

            # Init service
            service = GoogleMeetService(access_token=token, user_email=email)

            # Create event
            # --- FIX: Convert datetime objects to ISO format strings for JSON serialization ---
            eventData = service.create_meeting(
                summary=topic,
                description=f"Auto-created meeting for {topic}",
                attendees=attendees,
                start_time=start_datetime.isoformat(),
                end_time=end_datetime.isoformat(),
                timezone=timezone_str,
            )

            self.meetingDetails = eventData

            self.logger.info(f"[GoogleMeet Created] {eventData}")
            return {
                "status": "success",
                "output": f"Google Meet created successfully: {eventData.get('meet_link')}",
                "next_step": step.get("next_step"),
            }

        except Exception as e:
            self.logger.error(f"[GoogleMeet Error] {e}")
            # The original error is a TypeError, but if another error occurs, this will catch it.
            return {"output": f"[GoogleMeet Error] {e}"}

    def _handleGoogleMeetMail(self, step):
        from .helperzz import generate_meeting_email_body

        print("called meet mail thing")

        details = self.meetingDetails
        user_email = self.userdetails["email"]
        contacts = [item["email"] for item in self.contacts if "email" in item]

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

    def _handleavailability_google(self):
        from datetime import datetime

        # Extract user info
        email = self.userdetails["email"]
        token = self.userdetails["token"]

        # Extract contact emails
        contacts = [item.get("email") for item in self.contacts if "email" in item]

        # Fallback/defaults
        scheduled_options = self.input_data.get("scheduled_options", {}) or {}
        start_time = scheduled_options.get("startTime", "10:00")
        end_time = scheduled_options.get("endTime", "11:00")
        start_date = scheduled_options.get("startDate")
        timezone = scheduled_options.get("timezone", "Asia/Kolkata")
        duration_minutes = int(scheduled_options.get("duration_minutes", 30))
        days_to_check = int(scheduled_options.get("days_to_check", 3))

        # Use today's date if not provided
        preferred_date = start_date or datetime.now().strftime("%Y-%m-%d")

        # Initialize service
        service = GoogleMeetService(access_token=token, user_email=email)

        # Call to get available slot
        available_slot = service.get_first_available_slot(
            attendees=contacts,
            preferred_date=preferred_date,
            start_time=start_time,
            end_time=end_time,
            duration_minutes=duration_minutes,
            timezone=timezone,
            days_to_check=days_to_check,
        )

        return available_slot

    def get_execution_log(self) -> List[Dict[str, Any]]:
        return self.execution_log
