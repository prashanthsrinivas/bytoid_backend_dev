import traceback

from db.db_checkers import fetch_contacts_by_user
from db.rds_db import connect_to_rds, get_cursor
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from google.auth.transport.requests import Request
import pytz
from datetime import datetime, timedelta, time
from typing import Union, List
import os, re
from utils.normal import can_reply_to_email, convert_human_date, convert_human_time
from dotenv import load_dotenv
from utils.g_scopes import g_basescopes
from utils.base_logger import get_logger

load_dotenv()
logger = get_logger(__name__)


class GoogleMeetService:
    def __init__(
        self,
        userid: str,
        contacts=None,
        testing=False,
        workflow=None,
        wf_id=None,
        connection=None,
        integrations=None,
    ):
        """
        access_token: OAuth 2.0 access token with Calendar, Drive scope
        user_email: authenticated user's email address
        """
        # print("GoogleMeetService initialized")
        self.conn = connection or connect_to_rds()
        self.userid = userid
        self.contacts = contacts or fetch_contacts_by_user(self.userid)
        source_table = None
        with get_cursor(self.conn) as cursor:
            cursor.execute(
                """
                            SELECT client_id, client_secret,
                                token, refresh_token,
                                expiry, email
                            FROM users
                            WHERE user_id = %s
                        """,
                (str(userid),),
            )

            row = cursor.fetchone()
            if row and all(
                [row[0], row[1], row[3]]
            ):  # client_id, secret, refresh_token exist
                source_table = "users"
            else:

                cursor.execute(
                    """
                    SELECT client_id,client_secret,
                               access_token,refresh_token,expiry,email
                               FROM integrations WHERE primary_user_id_fk=%s AND platform = 'google'
                               """,
                    (str(userid),),
                )
                row = cursor.fetchone()
                if row and all([row[0], row[1], row[2]]):
                    source_table = "integrations"

            if not row:
                raise ValueError(f"No Gmail credentials found for user {userid}")
        client_id, client_secret, access_token, refresh_token, expiry, user_email = row
        # expiryed = datetime.fromisoformat(expiry) if isinstance(expiry, str) else expiry
        if isinstance(expiry, str):
            if expiry.startswith("0000"):
                expiryed = None
            else:
                try:
                    expiryed = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                except Exception:
                    expiryed = None
        else:
            expiryed = expiry
        self.user_email = user_email

        self.creds = Credentials(
            token=access_token,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=g_basescopes,
            expiry=expiryed,
        )
        if self.creds.expired and self.creds.refresh_token:
            try:
                # This call uses the refresh_token to get a new access token
                self.creds.refresh(
                    Request()
                )  # You need to import google.auth.transport.requests.Request
                # print(f"✅ Token refreshed successfully for user {userid}")

                # 4. CRITICAL STEP: Save the NEW tokens and expiry back to the database
                with get_cursor(self.conn) as cursor:
                    if source_table == "users":
                        cursor.execute(
                            """
                            UPDATE users
                            SET token = %s, expiry = %s 
                            WHERE user_id = %s
                            """,
                            (self.creds.token, self.creds.expiry, str(userid)),
                        )
                    elif source_table == "integrations":
                        cursor.execute(
                            """
                            UPDATE integrations
                            SET access_token=%s,
                                refresh_token=%s,
                                expiry=%s
                            WHERE primary_user_id_fk=%s
                            AND platform='google'
                        """,
                            (
                                self.creds.token,
                                self.creds.refresh_token,
                                self.creds.expiry,
                                str(userid),
                            ),
                        )
                self.conn.commit()

            except Exception as e:
                # Token refresh failed (e.g., refresh token revoked)
                # print(f"❌ Token refresh failed for user {userid}: {e}")
                logger.info("❌ Token refresh failed for user %s: %s", userid, e)
                raise ValueError(
                    f"Token refresh failed. User must re-authenticate: {e}"
                )

        self.conn.close()
        self.calendar_service = build("calendar", "v3", credentials=self.creds)
        self.drive_service = build("drive", "v3", credentials=self.creds)
        calendar_info = (
            self.calendar_service.calendars().get(calendarId="primary").execute()
        )
        self.organizer_tz = calendar_info.get("timeZone", "UTC")
        self.testing = testing
        self.workflow = workflow
        self.current_wf_id = wf_id

    # Build credentials object

    def get_attendees_or_contacts(self, attendees):
        """
        Returns a list of attendees.
        Handles testing mode logic to allow flexible test email usage.
        Supports string, list, or list of dict input.
        """

        def normalize_attendees(attendees):
            """Ensure attendees is always a clean list of valid email strings."""
            if isinstance(attendees, str):
                attendees = [attendees.strip()] if attendees.strip() else []
            elif isinstance(attendees, list):
                normalized = []
                for item in attendees:
                    if isinstance(item, str) and "@" in item:
                        normalized.append(item.strip())
                    elif isinstance(item, dict):
                        email = item.get("email", "").strip()
                        if email and "@" in email:
                            normalized.append(email)
                attendees = normalized
            else:
                attendees = []
            return attendees

        def is_valid_attendee_list(emails):
            """Check if it's a non-empty list of replyable emails."""
            if not emails:
                return False
            for email in emails:
                if "@" not in email:
                    return False
            return True

        attendees = normalize_attendees(attendees)

        # ---------------- TESTING MODE ----------------
        if self.testing:
            main_test_mail = os.getenv("TEST_EMAIL")
            secondary_mail = os.getenv("TEST_EMAIL2")

            # print("🧩 Testing mode ON")
            # print("attendees:", attendees, type(attendees))

            # ✅ If user manually provided valid replyable emails, use them
            if is_valid_attendee_list(attendees):
                # print("✅ validated attendees:", attendees)
                return attendees

            # 🚨 Otherwise use default testing logic
            # print("⚠️ Using default test emails")
            if self.user_email == main_test_mail:
                return [secondary_mail]
            else:
                return [main_test_mail]

        # ---------------- NORMAL MODE ----------------
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

        # ✅ "all" keyword support
        if (
            len(attendees) == 1
            and isinstance(attendees[0], str)
            and attendees[0].lower() == "all"
        ):
            return contact_email_list()

        elif any(a.lower() == "all" for a in attendees if isinstance(a, str)):
            return contact_email_list()

        # ✅ If valid emails provided, return filtered list
        if is_valid_attendee_list(attendees):
            return list(dict.fromkeys(attendees))  # deduplicate

        # ✅ Fallback: empty (no valid attendees)
        return []

    def _tz_abbrev(self, dt: datetime):
        """Return timezone abbreviation like IST, UTC, CET based on timezone name."""
        try:
            import zoneinfo

            tz = zoneinfo.ZoneInfo(self.organizer_tz)
            return dt.astimezone(tz).strftime("%Z")
        except:
            return self.organizer_tz  # fallback raw timezone string

    def get_user_timezone(self):
        return self.organizer_tz

    # ----------------------------
    # SLOT FINDER
    # ----------------------------

    def get_all_available_slots(
        self,
        attendees: list,
        preferred_date,
        start_time,
        end_time,
        duration_minutes: int = 60,
        days_to_check: int = 3,
        timezone: str = None,
    ):
        """
        Return available slots for attendees.

        RULES:
        - Future date: use exactly as given
        - Same-day date: adjust if time is missing/past
        - Default slot: 10:00–11:00 if time missing
        - Step = 15 minutes
        - All comparisons done in UTC
        """

        try:
            effective_tz = timezone or self.organizer_tz or "UTC"
            tz = pytz.timezone(effective_tz)
            now_local = datetime.now(tz)

            # -------------------------
            # Normalize attendees
            # -------------------------
            if not isinstance(attendees, list):
                attendees = self.get_attendees_or_contacts(attendees)
            elif len(attendees) == 1 and str(attendees[0]).lower() == "all":
                attendees = self.get_attendees_or_contacts(attendees)

            # -------------------------
            # Parse preferred date
            # -------------------------
            if not preferred_date:
                preferred_date_dt = now_local.date()
                date_is_future = False
            else:
                converted_date = convert_human_date(preferred_date, tz_str=effective_tz)
                # print("converted date", converted_date)
                if not converted_date:
                    preferred_date_dt = now_local.date()
                    date_is_future = False
                else:
                    preferred_date_dt = converted_date.date()
                    date_is_future = preferred_date_dt > now_local.date()

            # Reject past date
            if preferred_date_dt < now_local.date():
                return {"success": False, "reason": "Preferred date is in the past."}

            # -------------------------
            # Determine start datetime
            # -------------------------
            if not start_time:
                start_dt = datetime.combine(preferred_date_dt, time(10, 0))
                start_dt = tz.localize(start_dt)
            else:
                start_dt = convert_human_time(start_time, tz_str=effective_tz)

                if start_dt.tzinfo is None:
                    start_dt = tz.localize(start_dt)

                # IMPORTANT FIX → attach preferred date
                start_dt = start_dt.replace(
                    year=preferred_date_dt.year,
                    month=preferred_date_dt.month,
                    day=preferred_date_dt.day,
                )

            # Same-day correction
            if preferred_date_dt == now_local.date() and start_dt < now_local:
                minute = ((now_local.minute // 15) + 1) * 15
                if minute == 60:
                    now_local += timedelta(hours=1)
                    minute = 0

                start_dt = now_local.replace(minute=minute, second=0, microsecond=0)

            # -------------------------
            # Duration
            # -------------------------
            try:
                duration_minutes = int(duration_minutes)
            except:
                duration_minutes = 60

            # -------------------------
            # Determine end datetime
            # -------------------------
            if not end_time:
                end_dt = start_dt + timedelta(minutes=duration_minutes)
            else:
                end_dt = convert_human_time(end_time, tz_str=effective_tz)

                if end_dt.tzinfo is None:
                    end_dt = tz.localize(end_dt)

                # IMPORTANT FIX → attach preferred date
                end_dt = end_dt.replace(
                    year=preferred_date_dt.year,
                    month=preferred_date_dt.month,
                    day=preferred_date_dt.day,
                )

                if end_dt <= start_dt:
                    end_dt = start_dt + timedelta(minutes=duration_minutes)

            # -------------------------
            # Convert to UTC
            # -------------------------
            date_start_utc = start_dt.astimezone(pytz.UTC)
            date_end_utc = end_dt.astimezone(pytz.UTC)

            available_slots = []

            # Ensure we start from future
            loop_start_date = max(now_local.date(), preferred_date_dt)

            # -------------------------
            # Search days forward
            # -------------------------
            for day_offset in range(days_to_check):

                current_date = loop_start_date + timedelta(days=day_offset)

                check_start_utc = date_start_utc.replace(
                    year=current_date.year,
                    month=current_date.month,
                    day=current_date.day,
                )

                check_end_utc = date_end_utc.replace(
                    year=current_date.year,
                    month=current_date.month,
                    day=current_date.day,
                )

                body = {
                    "timeMin": check_start_utc.isoformat(),
                    "timeMax": check_end_utc.isoformat(),
                    "timeZone": "UTC",
                    "items": [{"id": email} for email in attendees],
                }

                freebusy = self.calendar_service.freebusy().query(body=body).execute()

                busy_periods = []
                for cal in freebusy["calendars"].values():
                    busy_periods.extend(cal.get("busy", []))

                busy_periods = sorted(
                    [
                        (
                            datetime.fromisoformat(p["start"]).astimezone(pytz.UTC),
                            datetime.fromisoformat(p["end"]).astimezone(pytz.UTC),
                        )
                        for p in busy_periods
                    ]
                )

                slot_start = check_start_utc
                slot_length = timedelta(minutes=duration_minutes)

                while slot_start + slot_length <= check_end_utc:

                    slot_end = slot_start + slot_length

                    overlap = any(
                        not (slot_end <= bstart or slot_start >= bend)
                        for bstart, bend in busy_periods
                    )

                    if not overlap:
                        available_slots.append(
                            {
                                "start": slot_start.isoformat(),
                                "end": slot_end.isoformat(),
                                "startDate": slot_start.date().isoformat(),
                            }
                        )

                    slot_start += timedelta(minutes=15)

            return available_slots

        except Exception as e:
            logger.info("Error in get_all_available_slots: %s", e)
            return []

    # ----------------------------
    # MEETING SCHEDULER
    # ----------------------------
    def schedule_meeting_on_first_available(
        self,
        summary: str,
        attendees: list,
        preferred_date: str,
        start_time: str,
        end_time: str,
        duration_minutes: int = 60,
        description: str = None,
        timezone: str = None,
    ):
        """Find first free slot and schedule a meeting."""
        try:
            # print("started schedule_meeting_on_first_available")
            nattendees = self.get_attendees_or_contacts(attendees)
            # print("attendes1", nattendees)
            slots = self.get_all_available_slots(
                attendees=nattendees,
                preferred_date=preferred_date,
                start_time=start_time,
                end_time=end_time,
                duration_minutes=duration_minutes,
                timezone=timezone,
            )

            if not slots:
                return {"success": False, "reason": "No available slots in range."}

            first_slot = slots[0]
            # print("first slot", first_slot)
            created = self.createbasemeet(
                summary=summary,
                start_time=first_slot["start"],
                end_time=first_slot["end"],
                attendees=nattendees,
                description=description,
                timezone=timezone,
            )
            # print("created", created)

            return created
        except Exception as e:
            logger.info("Error in schedule_meeting_on_first_available: %s", repr(e))
            traceback.print_exc()
            return {"success": False, "error": str(e)}

    # ----------------------------
    # MEETING CREATOR BASE
    # ----------------------------
    def createbasemeet(
        self,
        summary: str,
        start_time: str,
        end_time: str,
        attendees: list = None,
        description: str = None,
        timezone: str = None,
    ):
        """Create a Google Calendar event with a Google Meet link."""

        import traceback
        import pytz
        from datetime import datetime

        # print("got into createbasemeet")

        try:
            # --------------------------------------------------
            # 1. Normalize attendees
            # --------------------------------------------------
            if not attendees:
                attendees = []

            if not isinstance(attendees, list):
                attendees = self.get_attendees_or_contacts(attendees)

            if len(attendees) == 1 and str(attendees[0]).lower() == "all":
                attendees = self.get_attendees_or_contacts(attendees)

            attendees = [{"email": str(email).strip()} for email in attendees if email]

            # --------------------------------------------------
            # 2. Timezone handling
            # --------------------------------------------------
            timezonez = timezone or self.organizer_tz or "UTC"
            tz = pytz.timezone(timezonez)

            # Convert incoming ISO times to timezone-aware datetimes
            start_dt = datetime.fromisoformat(start_time)
            end_dt = datetime.fromisoformat(end_time)

            if start_dt.tzinfo is None:
                start_dt = tz.localize(start_dt)
            else:
                start_dt = start_dt.astimezone(tz)

            if end_dt.tzinfo is None:
                end_dt = tz.localize(end_dt)
            else:
                end_dt = end_dt.astimezone(tz)

            start_time = start_dt.isoformat()
            end_time = end_dt.isoformat()

            # --------------------------------------------------
            # 3. Build event
            # --------------------------------------------------
            event = {
                "summary": summary,
                "start": {"dateTime": start_time, "timeZone": timezonez},
                "end": {"dateTime": end_time, "timeZone": timezonez},
                "attendees": attendees,
                "conferenceData": {
                    "createRequest": {
                        "requestId": f"meet-{datetime.now().timestamp()}",
                        "conferenceSolutionKey": {"type": "hangoutsMeet"},
                    }
                },
            }

            if self.user_email:
                event["organizer"] = {"email": self.user_email}

            if description:
                event["description"] = description

            # print("Sending event to Google Calendar:", event)

            # --------------------------------------------------
            # 4. Create event
            # --------------------------------------------------
            created_event = (
                self.calendar_service.events()
                .insert(
                    calendarId="primary",
                    body=event,
                    conferenceDataVersion=1,
                    sendUpdates="all",
                )
                .execute()
            )

            if not created_event:
                return {"success": False, "error": "Event was not created."}

            # --------------------------------------------------
            # 5. Safe datetime parsing
            # --------------------------------------------------
            start_val = created_event["start"].get("dateTime") or created_event[
                "start"
            ].get("date")
            end_val = created_event["end"].get("dateTime") or created_event["end"].get(
                "date"
            )

            start_dt = datetime.fromisoformat(start_val)
            end_dt = datetime.fromisoformat(end_val)

            tz_abbrev = self._tz_abbrev(start_dt)

            start_str = start_dt.strftime("%B %d, %Y at %I:%M %p")
            end_str = end_dt.strftime("%I:%M %p")

            attendees_list = ", ".join(
                a.get("email", "") for a in created_event.get("attendees", [])
            )

            return_str = (
                f"Meeting '{created_event.get('summary')}' is scheduled on "
                f"{start_str} {tz_abbrev} to {end_str} {tz_abbrev}. "
                f"Attendees: {attendees_list}. "
                f"Meet link: {created_event.get('hangoutLink')}"
            )

            return {
                "event_id": created_event.get("id"),
                "summary": created_event.get("summary"),
                "meet_link": created_event.get("hangoutLink"),
                "start_time": created_event["start"],
                "end_time": created_event["end"],
                "attendees": created_event.get("attendees", []),
                "return_str": return_str,
                "success": True,
            }

        except Exception as e:
            logger.info("error on create base: %s", repr(e))
            # traceback.print_exc()
            return {
                "success": False,
                "error": str(e),
            }

    def update_meeting(
        self,
        event_id: str,
        summary: str = None,
        start_time: str = None,
        end_time: str = None,
        attendees: list = None,
        description: str = None,
        timezone: str = None,
        preferred_date=None,
    ):
        """
        Update a Google Calendar event.
        Checks if meeting is active before updating.
        Only updates fields provided.
        """
        try:
            # print("update_meeting called", start_time, end_time)

            # Fetch event details first
            event = (
                self.calendar_service.events()
                .get(calendarId="primary", eventId=event_id)
                .execute()
            )

            # --- Check if event is cancelled/deleted ---
            if not event or event.get("status") == "cancelled":
                summary_safe = (
                    event.get("summary", "Unnamed Meeting") if event else "Unknown"
                )
                return {
                    "updated": False,
                    "return_str": f"The meeting '{summary_safe}' has already been cancelled or deleted. No updates were made.",
                }

            def is_iso_datetime(value: str) -> bool:
                if not value or not isinstance(value, str):
                    return False
                return bool(re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", value))

            # Safe handling
            # if start_time and end_time:
            #     if is_iso_datetime(start_time) and is_iso_datetime(end_time):
            #         start_time_std, end_time_std = start_time, end_time
            #     else:
            #         start_time_std = convert_human_time(start_time)
            #         end_time_std = convert_human_time(end_time)
            # else:
            #     start_time_std = end_time_std = None

            if start_time and end_time:
                if is_iso_datetime(start_time) and is_iso_datetime(end_time):
                    start_dt = datetime.fromisoformat(start_time)
                    end_dt = datetime.fromisoformat(end_time)

                else:
                    start_dt = convert_human_time(start_time)
                    end_dt = convert_human_time(end_time)

                # -----------------------------------
                # APPLY PREFERRED DATE IF PROVIDED
                # -----------------------------------
                if preferred_date:

                    pref_date = convert_human_date(preferred_date)

                    if pref_date:

                        start_dt = start_dt.replace(
                            year=pref_date.year,
                            month=pref_date.month,
                            day=pref_date.day,
                        )

                        end_dt = end_dt.replace(
                            year=pref_date.year,
                            month=pref_date.month,
                            day=pref_date.day,
                        )

                # -----------------------------------
                # SHIFT IF IN PAST
                # -----------------------------------
                now = (
                    datetime.now(start_dt.tzinfo) if start_dt.tzinfo else datetime.now()
                )

                if end_dt < now and not preferred_date:
                    delta = timedelta(days=1)
                    start_dt += delta
                    end_dt += delta

                # -----------------------------------
                # FINAL ISO FORMAT
                # -----------------------------------
                start_time_std = start_dt.isoformat()
                end_time_std = end_dt.isoformat()

            else:
                start_time_std = end_time_std = None

            # Convert human-friendly times
            # start_time_std = convert_human_time(start_time)
            # end_time_std = convert_human_time(end_time)
            timezone = timezone or self.organizer_tz

            # Normalize attendees
            attendees = self.get_attendees_or_contacts(attendees)
            normalized_attendees = []
            if attendees is not None:
                for a in attendees:
                    if isinstance(a, dict):
                        email = a.get("email")
                        if isinstance(email, dict):
                            email = email.get("email")
                        if isinstance(email, str):
                            normalized_attendees.append({"email": email})
                    elif isinstance(a, str):
                        normalized_attendees.append({"email": a})

            # Apply updates only to provided fields
            if summary:
                event["summary"] = summary
            if start_time_std and end_time_std:
                event["start"] = {"dateTime": start_time_std, "timeZone": timezone}
                event["end"] = {"dateTime": end_time_std, "timeZone": timezone}
            if normalized_attendees:
                event["attendees"] = normalized_attendees
            if description:
                event["description"] = description
            # print("start and end", event["start"], event["end"])

            # Perform the update
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

            # Build user-friendly return string
            start_dt = datetime.fromisoformat(updated_event["start"]["dateTime"])
            end_dt = datetime.fromisoformat(updated_event["end"]["dateTime"])

            tz_abbrev = self._tz_abbrev(start_dt)

            start_str = start_dt.strftime("%B %d, %Y at %I:%M %p")
            end_str = end_dt.strftime("%I:%M %p")

            attendees_list = ", ".join(
                a["email"] for a in updated_event.get("attendees", [])
            )
            meet_link = updated_event.get("hangoutLink", "No Meet link available")

            return_str = (
                f"Meeting '{updated_event.get('summary')}' was successfully updated. "
                f"It is scheduled on {start_str} {tz_abbrev} to {end_str} {tz_abbrev} "
                f"with attendees: {attendees_list}. "
                f"Meet link: {meet_link}"
            )
            return {
                "updated": True,
                "summary": updated_event.get("summary"),
                "return_str": return_str,
            }

        except HttpError as e:
            if e.resp.status in [404, 410] or "Resource has been deleted" in str(e):
                return {
                    "updated": False,
                    "return_str": "This meeting has already been deleted or is no longer active.",
                }
            return {
                "updated": False,
                "error": str(e),
                "return_str": "Failed to update meeting.",
            }

        except Exception as e:
            return {
                "updated": False,
                "error": str(e),
                "return_str": "Unexpected error while updating meeting.",
            }

    def delete_meeting(self, event_id: str):
        """
        Delete a Google Calendar event by ID.
        Handles already-deleted or missing events gracefully.
        Returns a clean summary-based message.
        """
        try:
            # Try to fetch event details first (for better message)
            event = (
                self.calendar_service.events()
                .get(calendarId="primary", eventId=event_id)
                .execute()
            )

            summary = event.get("summary", "Unnamed Meeting")
            start = event.get("start", {}).get("dateTime")
            start_str = (
                datetime.fromisoformat(start).strftime("%B %d, %Y at %I:%M %p")
                if start
                else "Unknown time"
            )

            # Attempt deletion
            self.calendar_service.events().delete(
                calendarId="primary", eventId=event_id
            ).execute()

            return_str = f"Meeting '{summary}' scheduled on {start_str} has been deleted successfully."
            return {"deleted": True, "return_str": return_str}

        except HttpError as e:
            # 410 = Gone (already deleted)
            if e.resp.status == 410 or "Resource has been deleted" in str(e):
                # Try to extract summary if event lookup failed before
                summary = locals().get("summary", "Unnamed Meeting")
                return_str = f"Meeting '{summary}' has already been deleted earlier."
                return {"deleted": True, "return_str": return_str}

            # 404 = Not found (invalid or missing)
            elif e.resp.status == 404:
                summary = locals().get("summary", "Unnamed Meeting")
                return_str = f"No meeting found for '{summary}'. It may have been removed already."
                return {"deleted": False, "return_str": return_str}

            # Other Google API errors
            return {
                "deleted": False,
                "return_str": f"Failed to delete meeting. ({str(e)})",
            }

        except Exception as e:
            # Generic fallback for unexpected issues
            return {"deleted": False, "return_str": f"Error deleting meeting: {str(e)}"}

    def is_meeting_slot_available(
        self,
        attendees: list,
        preferred_date: str,
        start_time: str,
        end_time: str,
        duration_minutes: int = 60,
        days_to_check: int = 3,
        timezone: str = None,
    ) -> bool:
        """
        Returns True if at least one available slot exists for the meeting across attendees.
        Else returns False.
        """
        timezone = timezone or self.organizer_tz
        tz = pytz.timezone(timezone)
        date_start = datetime.strptime(
            f"{preferred_date} {start_time}", "%Y-%m-%d %H:%M"
        )
        date_end = datetime.strptime(f"{preferred_date} {end_time}", "%Y-%m-%d %H:%M")
        if not isinstance(attendees, list):
            attendees = self.get_attendees_or_contacts(attendees)
        else:
            # If list contains "all" or "All", expand it
            if len(attendees) == 1 and any(a.lower() == "all" for a in attendees):
                attendees = self.get_attendees_or_contacts(attendees)

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
        duration_minutes: int = 60,
        days_to_check: int = 3,
        timezone: str = None,
    ):
        """
        Returns the first available time slot (start, end) for a meeting across attendees.
        Returns None if no available slot found.
        """

        # Convert human-friendly inputs
        preferred_date_std = convert_human_date(preferred_date)
        start_time_std = convert_human_time(start_time)
        end_time_std = convert_human_time(end_time)
        timezone = timezone or self.organizer_tz

        tz = pytz.timezone(timezone)
        date_start = datetime.strptime(
            f"{preferred_date_std} {start_time_std}", "%Y-%m-%d %H:%M"
        )
        date_end = datetime.strptime(
            f"{preferred_date_std} {end_time_std}", "%Y-%m-%d %H:%M"
        )
        if not isinstance(attendees, list):
            attendees = self.get_attendees_or_contacts(attendees)
        else:
            # If list contains "all" or "All", expand it
            if len(attendees) == 1 and any(a.lower() == "all" for a in attendees):
                attendees = self.get_attendees_or_contacts(attendees)

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

    # ----------------------------
    # MEETING CREATOR which skips weekdays
    # ----------------------------
    def create_meeting(
        self,
        summary: str,
        attendees: Union[List[str], str],  # list of emails or "all"
        preferred_date: str = None,  # "YYYY-MM-DD"
        start_time: str = None,  # "HH:MM"
        end_time: str = None,  # "HH:MM"
        duration_minutes: int = 60,
        description: str = None,
        timezone: str = None,
    ):
        """
        Schedules a meeting with human-readable date and time support.
        - Dates: today, tomorrow, 12-10, 12-jan, 10 days from now, next sunday
        - Times: 9am, lunch time, breakfast, quarter to five, sunrise, sunset
        - Skips weekends automatically
        """
        # print("starting of create meeting")
        timezone = timezone or self.organizer_tz
        tz = pytz.timezone(timezone)
        now = datetime.now(tz)

        attendees = self.get_attendees_or_contacts(attendees)
        if not attendees:
            return {"success": False, "reason": "No valid attendees available."}

        # Base date (skip weekends)
        base_date = now
        while base_date.weekday() >= 5:
            base_date += timedelta(days=1)
        # print("bef preffered data", type(preferred_date), preferred_date)

        # --- Convert human-readable date and time ---
        if preferred_date:
            base_date = convert_human_date(
                preferred_date, base_date=base_date, tz_str=timezone
            )
            # print("base date human parsed", base_date)
            if not base_date:
                return {
                    "success": False,
                    "reason": f"Could not parse date: {preferred_date}",
                }

        if start_time:
            check_start_dt = convert_human_time(
                start_time, base_date=base_date, tz_str=timezone
            )
            # print("base time human parsed", check_start_dt)
            if not check_start_dt:
                return {
                    "success": False,
                    "reason": f"Could not parse start time: {start_time}",
                }
        else:
            check_start_dt = base_date.replace(
                hour=9, minute=0, second=0, microsecond=0
            )
            check_start_dt = tz.localize(check_start_dt)

        if end_time:
            check_end_dt = convert_human_time(
                end_time,
                base_date=base_date,
                tz_str=timezone,
            )
            # print("base time end human parsed", check_end_dt)
            if not check_end_dt:
                return {
                    "success": False,
                    "reason": f"Could not parse end time: {end_time}",
                }
        else:
            check_end_dt = check_start_dt + timedelta(minutes=duration_minutes)

        # Ensure start < end
        if check_end_dt <= check_start_dt:
            check_end_dt = check_start_dt + timedelta(minutes=duration_minutes)

        # Skip weekends if start falls on weekend
        while check_start_dt.weekday() >= 5:
            check_start_dt += timedelta(days=1)
            check_start_dt = check_start_dt.replace(hour=9, minute=0)
            check_end_dt = check_start_dt + timedelta(minutes=duration_minutes)

        # print("start dt", check_start_dt)
        # print("end dt", check_end_dt)

        # Convert to strings for API
        check_start = check_start_dt.strftime("%Y-%m-%d %H:%M")
        check_end = check_end_dt.strftime("%Y-%m-%d %H:%M")

        # print("Meeting creation with attendees:", attendees)
        # print("Start:", check_start, "End:", check_end)

        # Find first available slot
        first_slot = self.get_first_available_slot(
            attendees=attendees,
            preferred_date=check_start_dt.date().isoformat(),
            start_time=check_start,
            end_time=check_end,
            duration_minutes=duration_minutes,
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
        )
        if "error" in created or None in created:
            return {"error": "meeting cant be created"}

        return {"success": True, "meeting": created}

    def view_all_events(
        self,
        max_results=2500,
        holidays=False,
        from_date=None,
        to_date=None,
    ):
        """
        Fetch Google Calendar events across all calendars.

        Args:
            max_results   : Max events fetched per page
            show_holidays :
                False = exclude holiday calendars (default)
                True  = include holiday calendars + user calendars (both)
            from_date     : Optional start date (ISO date or datetime string)
            to_date       : Optional end date   (ISO date or datetime string)
        """

        # print("got into viewallevents")

        # ------------ Date Range Logic ------------
        now = datetime.utcnow()

        default_from = now - timedelta(days=365)
        default_to = now + timedelta(days=365)

        # Parse from_date
        if from_date:
            try:
                from_dt = datetime.fromisoformat(from_date.replace("Z", "+00:00"))
            except:
                from_dt = datetime.strptime(from_date, "%Y-%m-%d")
        else:
            from_dt = default_from

        # Parse to_date
        if to_date:
            try:
                to_dt = datetime.fromisoformat(to_date.replace("Z", "+00:00"))
            except:
                to_dt = datetime.strptime(to_date, "%Y-%m-%d")
        else:
            to_dt = default_to

        # Convert to RFC3339
        time_min = from_dt.isoformat() + "Z"
        time_max = to_dt.isoformat() + "Z"

        # print(f"Fetching events from {time_min} to {time_max}")

        # ------------ Fetch all calendars ------------
        try:
            calendars = (
                self.calendar_service.calendarList().list().execute().get("items", [])
            )
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to fetch calendar list: {str(e)}",
            }

        all_events = []

        # ------------ Loop Through Calendars ------------
        for cal in calendars:
            cal_id = cal["id"].lower()
            cal_name = cal.get("summary", cal_id)

            # Detect holiday calendars
            is_holiday_calendar = (
                "holiday" in cal_id
                or "holidays" in cal_id
                or cal_name.lower().startswith("holidays")
            )

            # FINAL REQUIRED LOGIC:
            # -------------------------------------------------
            # If show_holidays = False → exclude holiday calendars
            # If show_holidays = True  → include ALL calendars
            # -------------------------------------------------
            if not holidays and is_holiday_calendar:
                continue
            # (When show_holidays=True → do not filter anything)

            # print(f"Fetching events from calendar: {cal_name}")

            page_token = None

            try:
                while True:
                    result = (
                        self.calendar_service.events()
                        .list(
                            calendarId=cal["id"],
                            singleEvents=True,
                            orderBy="startTime",
                            timeMin=time_min,
                            timeMax=time_max,
                            maxResults=max_results,
                            pageToken=page_token,
                            # 👇 ADD THIS
                            showDeleted=True,
                        )
                        .execute()
                    )

                    items = result.get("items", [])
                    # print(f"  Got {len(items)} events")

                    for ev in items:
                        all_events.append(
                            {
                                "calendar_id": cal["id"],
                                "calendar_name": cal_name,
                                "event_id": ev.get("id"),
                                "summary": ev.get("summary"),
                                "start": ev.get("start"),
                                "end": ev.get("end"),
                                "attendees": ev.get("attendees", []),
                                "hangoutLink": ev.get("hangoutLink"),
                                "location": ev.get("location"),
                                "description": ev.get("description"),
                                "status": ev.get("status"),
                            }
                        )

                    page_token = result.get("nextPageToken")
                    if not page_token:
                        break

            except Exception as e:
                # print(f"Error in calendar {cal['id']}: {e}")
                continue

        # ------------ Return Result ------------
        return {
            "success": True,
            "from": time_min,
            "to": time_max,
            "count": len(all_events),
            "events": all_events,
        }

    def create_calendar_event(
        self,
        title,
        start_dt,
        end_dt,
        attendees=None,
        description=None,
        location=None,
        meet=False,
        timezone=None,
    ):
        attendees = attendees or []

        # Resolve timezone ONLY from argument or organizer
        effective_tz = timezone or self.organizer_tz

        event_body = {
            "summary": title,
            "description": description,
            "location": location,
            "start": {
                "dateTime": start_dt.isoformat(),
                "timeZone": effective_tz,
            },
            "end": {
                "dateTime": end_dt.isoformat(),
                "timeZone": effective_tz,
            },
            "attendees": [{"email": a} for a in attendees],
        }

        if meet:
            event_body["conferenceData"] = {
                "createRequest": {
                    "requestId": f"c-meet-{datetime.now().timestamp()}",
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            }

        created_event = (
            self.calendar_service.events()
            .insert(
                calendarId="primary",
                body=event_body,
                conferenceDataVersion=1 if meet else 0,
            )
            .execute()
        )

        return {
            "success": True,
            "id": created_event.get("id"),
            "hangoutLink": created_event.get("hangoutLink"),
            "summary": created_event.get("summary"),
            "start": created_event.get("start"),
            "end": created_event.get("end"),
            "attendees": created_event.get("attendees"),
            "conferenceData": created_event.get("conferenceData"),
            "return_str": (
                f"Meeting '{created_event.get('summary')}' scheduled successfully. "
                f"{'Google Meet link created.' if meet else 'No meeting link added.'}"
            ),
        }

    def update_calendar_event(
        self,
        event_id,
        title=None,
        start_dt=None,
        end_dt=None,
        attendees=None,
        description=None,
        location=None,
        googlemeet=False,
        timezone=None,
    ):
        try:
            event = (
                self.calendar_service.events()
                .get(calendarId="primary", eventId=event_id)
                .execute()
            )

            # Simple fields
            if title:
                event["summary"] = title
            if description:
                event["description"] = description
            if location:
                event["location"] = location
            if attendees:
                event["attendees"] = [{"email": a} for a in attendees]

            # Time update (timezone ONLY from argument or organizer)
            if start_dt and end_dt:
                effective_tz = timezone or self.organizer_tz

                event["start"] = {
                    "dateTime": start_dt.isoformat(),
                    "timeZone": effective_tz,
                }
                event["end"] = {
                    "dateTime": end_dt.isoformat(),
                    "timeZone": effective_tz,
                }

            # Google Meet handling
            if googlemeet:
                event["conferenceData"] = {
                    "createRequest": {
                        "requestId": f"c-meet-{datetime.now().timestamp()}",
                        "conferenceSolutionKey": {"type": "hangoutsMeet"},
                    }
                }
            else:
                event.pop("conferenceData", None)

            updated = (
                self.calendar_service.events()
                .patch(
                    calendarId="primary",
                    eventId=event_id,
                    body=event,
                    conferenceDataVersion=1 if googlemeet else 0,
                )
                .execute()
            )

            return {
                "success": True,
                "event_id": updated.get("id"),
                "summary": updated.get("summary"),
                "hangoutLink": updated.get("hangoutLink"),
                "start": updated.get("start"),
                "end": updated.get("end"),
                "attendees": updated.get("attendees"),
                "return_str": (
                    f"Meeting '{updated.get('summary')}' updated successfully. "
                    f"{'Google Meet link updated.' if googlemeet else 'No meeting link attached.'}"
                ),
            }

        except Exception as e:
            return {"success": False, "error": str(e)}

    def delete_calendar_event(self, event_id):
        try:
            self.calendar_service.events().delete(
                calendarId="primary", eventId=event_id
            ).execute()

            return {
                "success": True,
                "event_id": event_id,
                "return_str": "Meeting was successfully cancelled and removed from the calendar.",
            }

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "return_str": "Failed to cancel the meeting due to an error.",
            }
