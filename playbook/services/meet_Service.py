from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import pytz
from datetime import datetime, timedelta


class GoogleMeetService:
    def __init__(self, access_token: str, user_email: str):
        """
        access_token: OAuth 2.0 access token with Calendar, Drive scope
        user_email: authenticated user's email address
        """
        self.creds = Credentials(token=access_token)
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

    def create_meeting(
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
                # sendUpdates="all",
            )
            .execute()
        )

        return {
            "event_id": created_event.get("id"),
            "summary": created_event.get("summary"),
            "meet_link": created_event.get("hangoutLink"),
            "start": created_event["start"],
            "end": created_event["end"],
            "attendees": created_event.get("attendees", []),
        }

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
                        "start": slot_start.isoformat(),
                        "end": slot_end.isoformat(),
                        "date": slot_start.date().isoformat(),
                    }

                slot_start += timedelta(minutes=15)

        return None  # ❌ No valid slot found


# Example usage
# if __name__ == "__main__":
#     ACCESS_TOKEN = "ya29.A0AS3H6Nya02eQL6lnW-So6Il9NZkV7C4jNFM95TGZtgCtKiRgKOsHwcqZaAlkOxHwLGeY6wTDebrwqYk01-J4s7Eu5jFFz-qUBgonk3KI8g8zFIadbrxPdrP5zoX4VTlOgrjVlwyMZhHZ_fSlQfmysusAY0INP30FfJCfPKwW4ElDZUKRwVv_mU8yACltXq-px1N7FiMaCgYKARASARQSFQHGX2MiUrkfSvRB4JxWMqncV9wTOQ0206"
#     USER_EMAIL = "mahender.kurikyala11@gmail.com"

#     service = GoogleMeetService(access_token=ACCESS_TOKEN, user_email=USER_EMAIL)

#     #     # Create a Google Meet event
#     # result = service.create_meeting(
#     #     summary="Project Sync-up",
#     #     description="""
#     # <div style="font-family: Arial, sans-serif; color: #333;">
#     #     <div style="text-align: center;">
#     #         <img src="https://yourcompany.com/logo.png" alt="Company Logo" width="120"/>
#     #         <h2 style="margin-top: 10px;">Bytoid Technologies</h2>
#     #         <p style="font-size: 16px;">🚀 Let's align and innovate!</p>
#     #     </div>
#     #     <hr style="border: none; border-top: 1px solid #ccc;"/>
#     #     <h3>📋 Agenda</h3>
#     #     <ul>
#     #         <li>Progress updates from all teams</li>
#     #         <li>Discussion on upcoming deadlines</li>
#     #         <li>Address blockers and assign action items</li>
#     #     </ul>
#     #     <p>📅 <strong>Date:</strong> August 6, 2025<br/>
#     #     🕒 <strong>Time:</strong> 7:00 PM - 8:30 PM IST</p>
#     #     <p>🌐 <strong>Meeting Platform:</strong> Google Meet</p>
#     #     <hr/>
#     #     <p style="font-size: 12px; color: #555;">
#     #         For assistance, contact <a href="mailto:support@bytoid.com">support@bytoid.com</a>
#     #     </p>
#     # </div>
#     # """,
#     #     start_time="2025-08-06T19:00:00+05:30",
#     #     end_time="2025-08-06T20:30:00+05:30",
#     #     attendees=["riyagijo2@gmail.com", "service@bytoid.ca"],
#     # )
#     # print(result)
#     #     # print("Meet link:", result["meet_link"])
#     #     # id = "tts-wmpf-jqh"
#     #     updated = service.update_meeting(
#     #         event_id="q3j9lg97uhf0bvk7jcf41vufeo",
#     #         summary="Updated Meeting",
#     #         start_time="2025-08-05T17:00:00",
#     #         end_time="2025-08-05T17:40:00",
#     #     )
#     #     print("Updated:", updated)
#     # event = service.get_event_by_meet_link("https://meet.google.com/tts-wmpf-jqh")
#     # print(event)
#     # slots = service.get_all_available_slots(
#     #     attendees=["riyagijo2@gmail.com", "service@bytoid.ca"],
#     #     preferred_date="2025-08-06",
#     #     start_time="14:00",
#     #     end_time="18:00",
#     #     duration_minutes=30,
#     # )
#     # print("Available Slots:", slots)
#     # is_available = service.is_meeting_slot_available(
#     #     attendees=["user1@example.com", "user2@example.com"],
#     #     preferred_date="2025-08-06",
#     #     start_time="10:00",
#     #     end_time="17:00",
#     #     duration_minutes=30,
#     # )

#     # if is_available:
#     #     print("✅ At least one valid slot is available.")
#     # else:
#     #     print("❌ No common time available.")
#     # slot = service.get_first_available_slot(
#     #     attendees=["user1@example.com", "user2@example.com"],
#     #     preferred_date="2025-08-06",
#     #     start_time="10:00",
#     #     end_time="17:00",
#     #     duration_minutes=30,
#     # )

#     # if slot:
#     #     print("✅ First available slot found:")
#     #     print("Start:", slot["start"])
#     #     print("End:", slot["end"])
#     #     print("Date:", slot["date"])
#     # else:
#     #     print("❌ No common slot available in next 3 days.")
#     res = service.delete_meeting("m4pb7pv4cpl8kcakc29vsie8n0")
#     print(res)
