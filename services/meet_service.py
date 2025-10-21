from db.db_checkers import fetch_contacts_by_user
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import pytz
from datetime import datetime, timedelta
from typing import Union, List

from utils.normal import can_reply_to_email


class GoogleMeetService:
    def __init__(self, access_token: str, user_email: str, contacts=None):
        """
        access_token: OAuth 2.0 access token with Calendar, Drive scope
        user_email: authenticated user's email address
        """
        self.creds = Credentials(token=access_token)
        self.contacts = contacts or fetch_contacts_by_user(self.userid)
        self.calendar_service = build("calendar", "v3", credentials=self.creds)
        self.drive_service = build("drive", "v3", credentials=self.creds)
        self.user_email = user_email

    def get_all_available_slots(
        self,
        attendees: list,
        preferred_date: str,
        start_time: str,
        end_time: str,
        duration_minutes: int,
        timezone: str = "Asia/Kolkata",
        days_to_check: int = 5,
    ):
        """
        Get all available time slots across attendees using FreeBusy API.
        Checks preferred date + next `days_to_check` days.
        Returns a list of slot dicts with start and end times.
        """
        tz = pytz.timezone(timezone)
        date_start = datetime.strptime(
            f"{preferred_date} {start_time}", "%Y-%m-%d %H:%M"
        )
        date_end = datetime.strptime(f"{preferred_date} {end_time}", "%Y-%m-%d %H:%M")
        all_slots = []

        for day_offset in range(days_to_check):
            current_start = tz.localize(date_start + timedelta(days=day_offset))
            current_end = tz.localize(date_end + timedelta(days=day_offset))

            body = {
                "timeMin": current_start.isoformat(),
                "timeMax": current_end.isoformat(),
                "timeZone": timezone,
                "items": [{"id": email} for email in attendees],
            }

            freebusy_result = (
                self.calendar_service.freebusy().query(body=body).execute()
            )
            busy_periods = []

            for calendar in freebusy_result["calendars"].values():
                busy_periods.extend(calendar.get("busy", []))

            busy_periods = sorted(
                [
                    (
                        datetime.fromisoformat(p["start"]),
                        datetime.fromisoformat(p["end"]),
                    )
                    for p in busy_periods
                ]
            )

            # Generate candidate slots
            slot_start = current_start
            slot_delta = timedelta(minutes=duration_minutes)

            while slot_start + slot_delta <= current_end:
                slot_end = slot_start + slot_delta

                # Check for overlap with any busy period
                overlap = any(
                    not (slot_end <= busy_start or slot_start >= busy_end)
                    for (busy_start, busy_end) in busy_periods
                )

                if not overlap:
                    all_slots.append(
                        {
                            "start": slot_start.isoformat(),
                            "end": slot_end.isoformat(),
                            "date": slot_start.strftime("%Y-%m-%d"),
                        }
                    )

                slot_start += timedelta(minutes=15)

        return all_slots

    def schedule_meeting_on_first_available(
        self,
        summary: str,
        attendees: list,
        preferred_date: str,
        start_time: str,
        end_time: str,
        duration_minutes: int,
        description: str = None,
        timezone: str = "Asia/Kolkata",
    ):
        """
        Finds the first available slot and schedules a meeting.
        Returns meeting details or error.
        """
        slots = self.get_all_available_slots(
            attendees=attendees,
            preferred_date=preferred_date,
            start_time=start_time,
            end_time=end_time,
            duration_minutes=duration_minutes,
            timezone=timezone,
        )

        if not slots:
            return {"success": False, "reason": "No available slots in range."}

        first_slot = slots[0]

        created = self.create_meeting(
            summary=summary,
            start_time=first_slot["start"],
            end_time=first_slot["end"],
            attendees=attendees,
            description=description,
            timezone=timezone,
        )

        return {"success": True, "meeting": created}

    def createbasemeet(
        self,
        summary: str,
        start_time: str,
        end_time: str,
        attendees: list = None,
        timezone: str = "Asia/Kolkata",
        description: str = None,  # ✅ Added parameter
    ):
        """
        Create a Google Calendar event with a Meet link.
        start_time, end_time: ISO format ('YYYY-MM-DDTHH:MM:SS')
        attendees: list of emails
        """
        attendees = attendees or []

        event = {
            "summary": summary,
            "start": {
                "dateTime": start_time,
                "timeZone": timezone,
            },
            "end": {
                "dateTime": end_time,
                "timeZone": timezone,
            },
            "attendees": [{"email": email} for email in attendees],
            "conferenceData": {
                "createRequest": {
                    "requestId": f"meet-{datetime.now().timestamp()}",
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            },
            "organizer": {"email": self.user_email},
        }

        if description:
            event["description"] = description  # ✅ Add custom message

        created_event = (
            self.calendar_service.events()
            .insert(
                calendarId="primary",
                body=event,
                conferenceDataVersion=1,
            )
            .execute()
        )

        return {
            "event_id": created_event.get("id"),
            "summary": created_event.get("summary"),
            "meet_link": created_event.get("hangoutLink"),
            "start_time": created_event["start"],
            "end_time": created_event["end"],
            "attendees": created_event.get("attendees", []),
        }

    def update_meeting(
        self,
        event_id: str,
        summary: str = None,
        start_time: str = None,
        end_time: str = None,
        attendees: list = None,
        description: str = None,
        timezone: str = "Asia/Kolkata",
    ):
        """
        Update a Google Calendar event.
        Only fields provided will be updated.
        """
        try:
            event = (
                self.calendar_service.events()
                .get(calendarId="primary", eventId=event_id)
                .execute()
            )

            if summary:
                event["summary"] = summary
            if start_time and end_time:
                event["start"] = {"dateTime": start_time, "timeZone": timezone}
                event["end"] = {"dateTime": end_time, "timeZone": timezone}
            if attendees is not None:
                event["attendees"] = [{"email": email} for email in attendees]
            if description:
                event["description"] = description

            updated_event = (
                self.calendar_service.events()
                .update(
                    calendarId="primary",
                    eventId=event_id,
                    body=event,
                    conferenceDataVersion=1,
                    sendUpdates="all",
                )
                .execute()
            )

            return {
                "event_id": updated_event["id"],
                "summary": updated_event.get("summary"),
                "updated": True,
            }

        except Exception as e:
            return {"error": str(e), "updated": False}

    def delete_meeting(self, event_id: str):
        """
        Delete a Google Calendar event by ID.
        """
        try:
            self.calendar_service.events().delete(
                calendarId="primary", eventId=event_id
            ).execute()
            return {"deleted": True, "event_id": event_id}
        except Exception as e:
            return {"deleted": False, "error": str(e)}

    def is_meeting_slot_available(
        self,
        attendees: list,
        preferred_date: str,
        start_time: str,
        end_time: str,
        duration_minutes: int,
        timezone: str = "Asia/Kolkata",
        days_to_check: int = 3,
    ) -> bool:
        """
        Returns True if at least one available slot exists for the meeting across attendees.
        Else returns False.
        """
        tz = pytz.timezone(timezone)
        date_start = datetime.strptime(
            f"{preferred_date} {start_time}", "%Y-%m-%d %H:%M"
        )
        date_end = datetime.strptime(f"{preferred_date} {end_time}", "%Y-%m-%d %H:%M")

        for day_offset in range(days_to_check):
            current_start = tz.localize(date_start + timedelta(days=day_offset))
            current_end = tz.localize(date_end + timedelta(days=day_offset))

            body = {
                "timeMin": current_start.isoformat(),
                "timeMax": current_end.isoformat(),
                "timeZone": timezone,
                "items": [{"id": email} for email in attendees],
            }

            freebusy_result = (
                self.calendar_service.freebusy().query(body=body).execute()
            )
            busy_periods = []

            for calendar in freebusy_result["calendars"].values():
                busy_periods.extend(calendar.get("busy", []))

            busy_periods = sorted(
                [
                    (
                        datetime.fromisoformat(p["start"]),
                        datetime.fromisoformat(p["end"]),
                    )
                    for p in busy_periods
                ]
            )

            slot_start = current_start
            slot_delta = timedelta(minutes=duration_minutes)

            while slot_start + slot_delta <= current_end:
                slot_end = slot_start + slot_delta

                # Check overlap
                overlap = any(
                    not (slot_end <= busy_start or slot_start >= busy_end)
                    for (busy_start, busy_end) in busy_periods
                )

                if not overlap:
                    return True  # ✅ Found a free slot

                slot_start += timedelta(minutes=15)

        return False  # ❌ No valid slot found

    def get_event_by_meet_link(self, meet_link: str):
        """
        Search upcoming events and find the one with the matching Meet link.
        """
        now = datetime.utcnow().isoformat() + "Z"  # 'Z' means UTC time
        events_result = (
            self.calendar_service.events()
            .list(
                calendarId="primary",
                timeMin=now,
                maxResults=100,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )

        events = events_result.get("items", [])

        for event in events:
            if event.get("hangoutLink") == meet_link:
                return {
                    "event_id": event["id"],
                    "summary": event.get("summary"),
                    "start": event["start"],
                    "end": event["end"],
                    "attendees": event.get("attendees", []),
                    "hangoutLink": event["hangoutLink"],
                }

        return {"error": "No event found with the given Meet link."}

    def get_first_available_slot(
        self,
        attendees: list,
        preferred_date: str,
        start_time: str,
        end_time: str,
        duration_minutes: int,
        timezone: str = "Asia/Kolkata",
        days_to_check: int = 3,
    ):
        """
        Returns the first available time slot (start, end) for a meeting across attendees.
        Returns None if no available slot found.
        """
        tz = pytz.timezone(timezone)
        date_start = datetime.strptime(
            f"{preferred_date} {start_time}", "%Y-%m-%d %H:%M"
        )
        date_end = datetime.strptime(f"{preferred_date} {end_time}", "%Y-%m-%d %H:%M")

        for day_offset in range(days_to_check):
            current_start = tz.localize(date_start + timedelta(days=day_offset))
            current_end = tz.localize(date_end + timedelta(days=day_offset))

            body = {
                "timeMin": current_start.isoformat(),
                "timeMax": current_end.isoformat(),
                "timeZone": timezone,
                "items": [{"id": email} for email in attendees],
            }

            freebusy_result = (
                self.calendar_service.freebusy().query(body=body).execute()
            )
            busy_periods = []

            for calendar in freebusy_result["calendars"].values():
                busy_periods.extend(calendar.get("busy", []))

            busy_periods = sorted(
                [
                    (
                        datetime.fromisoformat(p["start"]).astimezone(tz),
                        datetime.fromisoformat(p["end"]).astimezone(tz),
                    )
                    for p in busy_periods
                ]
            )

            slot_start = current_start
            slot_delta = timedelta(minutes=duration_minutes)

            while slot_start + slot_delta <= current_end:
                slot_end = slot_start + slot_delta

                # Check overlap with any busy time
                overlap = any(
                    not (slot_end <= busy_start or slot_start >= busy_end)
                    for (busy_start, busy_end) in busy_periods
                )

                if not overlap:
                    return {
                        "startTime": slot_start.isoformat(),
                        "endTime": slot_end.isoformat(),
                        "startDate": slot_start.date().isoformat(),
                    }

                slot_start += timedelta(minutes=15)

        return None  # ❌ No valid slot found

    def upload_to_drive(self, file_path: str, mime_type: str, folder_id: str = None):
        """
        Upload a file to Google Drive.
        """
        file_metadata = {"name": file_path.split("/")[-1]}
        if folder_id:
            file_metadata["parents"] = [folder_id]

        media = MediaFileUpload(file_path, mimetype=mime_type)
        file = (
            self.drive_service.files()
            .create(body=file_metadata, media_body=media, fields="id, webViewLink")
            .execute()
        )
        return file

    def list_drive_files(self, page_size=10):
        """
        List recent Drive files.
        """
        results = (
            self.drive_service.files()
            .list(pageSize=page_size, fields="nextPageToken, files(id, name, mimeType)")
            .execute()
        )
        return results.get("files", [])

    def create_meeting(
        self,
        summary: str,
        attendees: Union[List[str], str],  # list of emails or "all"
        preferred_date: str = None,  # "YYYY-MM-DD"
        start_time: str = None,  # "HH:MM"
        end_time: str = None,  # "HH:MM"
        duration_minutes: int = 60,
        description: str = None,
        timezone: str = "Asia/Kolkata",
    ):
        """
        Schedules a meeting with robust handling of missing inputs:
        - date + start/end
        - only start or end time
        - no date/time -> pick earliest available slot
        - skips weekends
        - filters attendees using can_reply_to_email
        """
        tz = pytz.timezone(timezone)
        now = datetime.now(tz)

        # Prepare attendees list
        if attendees == "all":
            attendees_list = []
            if hasattr(self, "contacts") and isinstance(self.contacts, list):
                for i in self.contacts:
                    if can_reply_to_email(i):
                        attendees_list.append(i)
            attendees = attendees_list

        if not attendees:
            return {"success": False, "reason": "No valid attendees available."}

        # Pick next weekday for base date
        base_date = now
        while base_date.weekday() >= 5:  # skip Sat/Sun
            base_date += timedelta(days=1)

        # Determine start and end datetimes
        if preferred_date and start_time and end_time:
            # Full date + time provided
            check_start_dt = datetime.strptime(
                f"{preferred_date} {start_time}", "%Y-%m-%d %H:%M"
            )
            check_end_dt = datetime.strptime(
                f"{preferred_date} {end_time}", "%Y-%m-%d %H:%M"
            )
        elif start_time and not end_time:
            # Only start time provided
            check_start_dt = base_date.replace(
                hour=int(start_time.split(":")[0]),
                minute=int(start_time.split(":")[1]),
                second=0,
                microsecond=0,
            )
            check_end_dt = check_start_dt + timedelta(minutes=duration_minutes)
        elif end_time and not start_time:
            # Only end time provided
            check_end_dt = base_date.replace(
                hour=int(end_time.split(":")[0]),
                minute=int(end_time.split(":")[1]),
                second=0,
                microsecond=0,
            )
            check_start_dt = check_end_dt - timedelta(minutes=duration_minutes)
        else:
            # No date/time provided
            check_start_dt = now + timedelta(
                minutes=15 - now.minute % 15
            )  # round to next 15 min
            check_end_dt = check_start_dt + timedelta(minutes=duration_minutes)

        # Skip weekends if calculated start is on weekend
        while check_start_dt.weekday() >= 5:
            check_start_dt += timedelta(days=1)
            check_start_dt = check_start_dt.replace(hour=9, minute=0)
            check_end_dt = check_start_dt + timedelta(minutes=duration_minutes)

        # Localize datetimes
        check_start_dt = tz.localize(check_start_dt)
        check_end_dt = tz.localize(check_end_dt)

        # Convert to full datetime strings
        check_start = check_start_dt.strftime("%Y-%m-%d %H:%M")
        check_end = check_end_dt.strftime("%Y-%m-%d %H:%M")

        print("Meeting creation with attendees:", attendees)
        print("Start:", check_start, "End:", check_end)

        # Find first available slot
        first_slot = self.get_first_available_slot(
            attendees=attendees,
            preferred_date=check_start_dt.date().isoformat(),
            start_time=check_start,
            end_time=check_end,
            duration_minutes=duration_minutes,
            timezone=timezone,
            days_to_check=10,
        )

        if not first_slot:
            return {"success": False, "reason": "No available slots found."}

        created = self.createbasemeet(
            summary=summary,
            start_time=first_slot["startTime"],
            end_time=first_slot["endTime"],
            attendees=attendees,
            description=description,
            timezone=timezone,
        )

        return {"success": True, "meeting": created}
