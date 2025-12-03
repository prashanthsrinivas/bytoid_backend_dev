import requests
import pytz, json, re
from datetime import datetime, timedelta
from db.rds_db import connect_to_rds, get_cursor


def remove_teams_block(html_content):
    """
    Removes the Microsoft Teams auto-generated meeting invitation block
    WITHOUT touching the user's actual description text.
    """

    # Matches the Teams auto-meeting block that starts with a line of underscores
    # and ends with "For organizer"
    pattern = r"_{5,}[\s\S]*?For organizer"

    cleaned = re.sub(pattern, "", html_content, flags=re.IGNORECASE).strip()
    return cleaned


def format_dt(dt, tz):
    return {
        "dateTime": dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "timeZone": tz,
    }


class MicrosoftGraphCalendarService:
    GRAPH_BASE = "https://graph.microsoft.com/v1.0"

    def __init__(self, userid: str):
        self.userid = userid
        self.conn = connect_to_rds()

        # Fetch Microsoft tokens from DB
        with get_cursor(self.conn) as cursor:
            cursor.execute(
                """
                SELECT client_id, client_secret,
                       token, refresh_token,
                       expiry, email
                FROM users WHERE user_id=%s
                """,
                (str(userid),),
            )
            row = cursor.fetchone()

        if not row:
            raise ValueError("Microsoft OAuth not connected")

        (
            self.client_id,
            self.client_secret,
            access_token,
            refresh_token,
            expiry,
            self.user_email,
        ) = row

        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expiry = self.safe_parse(expiry)
        self.user_timezone = "UTC"

        # Refresh if needed
        self.ensure_fresh_token()

        self.headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

        # Load user timezone
        tz_resp = requests.get(
            f"{self.GRAPH_BASE}/me/mailboxSettings", headers=self.headers
        ).json()
        self.user_timezone = tz_resp.get("timeZone", "UTC")

    # -------------------------------------------
    def safe_parse(self, value):
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except:
            return None

    def get_all_available_slots(
        self,
        attendees: list,
        preferred_date,
        start_time,
        end_time,
        duration_minutes: int,
        days_to_check: int = 3,
    ):
        """
        Microsoft version of Google get_all_available_slots().
        Same rules, same return format.
        Uses MS Graph getSchedule() (15-min free/busy).
        """
        import pytz
        from datetime import datetime, timedelta, time

        tz = pytz.timezone(self.user_timezone)
        now_local = datetime.now(tz)

        # -------------------------
        # Normalize attendees
        # -------------------------
        attendees = attendees or []
        attendees = list({a.lower(): a for a in attendees}.values())

        # -------------------------
        # Determine preferred date
        # -------------------------
        if not preferred_date:
            preferred_date_dt = now_local.date()
            date_is_future = False
        else:
            try:
                preferred_date_dt = datetime.fromisoformat(str(preferred_date)).date()
            except:
                preferred_date_dt = now_local.date()
            date_is_future = preferred_date_dt > now_local.date()

        if preferred_date_dt < now_local.date():
            return {
                "success": False,
                "reason": "No available slots in range (date is in past).",
            }

        # -------------------------
        # Determine start time
        # -------------------------
        def to_local_time(val):
            if not val:
                return None
            try:
                d = datetime.fromisoformat(val.replace("Z", "+00:00"))
            except:
                d = datetime.strptime(val, "%Y-%m-%d %H:%M")
            if d.tzinfo is None:
                return tz.localize(d)
            return d.astimezone(tz)

        if date_is_future:
            if not start_time:
                start_dt = tz.localize(datetime.combine(preferred_date_dt, time(10, 0)))
            else:
                start_dt = to_local_time(start_time)
        else:
            if not start_time:
                minute = ((now_local.minute // 15) + 1) * 15
                if minute == 60:
                    now_local += timedelta(hours=1)
                    minute = 0
                start_dt = now_local.replace(minute=minute, second=0, microsecond=0)
            else:
                start_dt = to_local_time(start_time)
                if start_dt < now_local:
                    minute = ((now_local.minute // 15) + 1) * 15
                    if minute == 60:
                        now_local += timedelta(hours=1)
                        minute = 0
                    start_dt = now_local.replace(minute=minute, second=0, microsecond=0)

        # -------------------------
        # Determine end time
        # -------------------------
        if date_is_future:
            if not end_time:
                end_dt = tz.localize(datetime.combine(preferred_date_dt, time(11, 0)))
            else:
                end_dt = to_local_time(end_time)
        else:
            if not end_time:
                end_dt = start_dt + timedelta(minutes=duration_minutes)
            else:
                end_dt = to_local_time(end_time)

        # -------------------------
        # Convert to UTC
        # -------------------------
        base_start_utc = start_dt.astimezone(pytz.UTC)
        base_end_utc = end_dt.astimezone(pytz.UTC)

        # -------------------------
        # Loop through date range
        # -------------------------
        available_slots = []
        loop_start = max(now_local.date(), preferred_date_dt)

        for day_offset in range(days_to_check):
            day = loop_start + timedelta(days=day_offset)

            day_start = base_start_utc.replace(
                year=day.year, month=day.month, day=day.day
            )
            day_end = base_end_utc.replace(year=day.year, month=day.month, day=day.day)

            # -------------------------
            # Call Microsoft getSchedule()
            # -------------------------
            body = {
                "schedules": attendees,
                "startTime": {
                    "dateTime": day_start.strftime("%Y-%m-%dT%H:%M:%S"),
                    "timeZone": "UTC",
                },
                "endTime": {
                    "dateTime": day_end.strftime("%Y-%m-%dT%H:%M:%S"),
                    "timeZone": "UTC",
                },
                "availabilityViewInterval": 15,
            }

            resp = requests.post(
                f"{self.GRAPH_BASE}/me/calendar/getSchedule",
                headers=self.headers,
                json=body,
            ).json()

            # Gather busy times
            busy_periods = []
            for schedule in resp.get("value", []):
                for item in schedule.get("scheduleItems", []):
                    busy_periods.append(
                        (
                            datetime.fromisoformat(item["start"]["dateTime"] + "Z"),
                            datetime.fromisoformat(item["end"]["dateTime"] + "Z"),
                        )
                    )

            busy_periods.sort()

            # -------------------------
            # Check every 15-minute slot
            # -------------------------
            step = timedelta(minutes=15)
            slot_length = timedelta(minutes=duration_minutes)

            slot_start = day_start
            while slot_start + slot_length <= day_end:
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

                slot_start += step

        return available_slots

    # -------------------------------------------
    def ensure_fresh_token(self):
        # If token is near expiry (within 2 min), refresh it
        if not self.expiry or datetime.utcnow() >= self.expiry - timedelta(minutes=2):
            # print("🔄 Refreshing Microsoft token...")

            scopes = "User.Read Mail.Send Mail.ReadWrite Calendars.ReadWrite OnlineMeetings.ReadWrite Chat.ReadWrite"

            resp = requests.post(
                "https://login.microsoftonline.com/common/oauth2/v2.0/token",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "refresh_token",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": self.refresh_token,
                    "scope": scopes,
                },
            )

            data = resp.json()
            ##print("Data on refresh:", data)

            if "access_token" not in data:
                raise ValueError(f"MS token refresh failed: {data}")

            # Update memory
            self.access_token = data["access_token"]
            self.refresh_token = data.get("refresh_token", self.refresh_token)
            self.expiry = datetime.utcnow() + timedelta(
                seconds=data.get("expires_in", 3600)
            )

            # Save to DB
            with get_cursor(self.conn) as cursor:
                cursor.execute(
                    """
                    UPDATE users SET
                        token=%s,
                        refresh_token=%s,
                        expiry=%s
                    WHERE user_id=%s
                    """,
                    (self.access_token, self.refresh_token, self.expiry, self.userid),
                )
            self.conn.commit()

    def view_all_events(
        self, holidays=None, from_date=None, to_date=None, max_results=1000
    ):
        now = datetime.utcnow()

        if not from_date:
            from_date = (now - timedelta(days=365)).isoformat() + "Z"
        if not to_date:
            to_date = (now + timedelta(days=365)).isoformat() + "Z"

        # Fetch calendars
        cal_resp = requests.get(
            f"{self.GRAPH_BASE}/me/calendars",
            headers=self.headers,
        )

        if cal_resp.status_code != 200:
            return {"success": False, "error": cal_resp.text}

        calendars = cal_resp.json().get("value", [])
        all_events = []

        for cal in calendars:
            cal_id = cal["id"]
            cal_name = cal.get("name", cal_id)

            events_resp = requests.get(
                f"{self.GRAPH_BASE}/me/calendars/{cal_id}/calendarView",
                headers=self.headers,
                params={
                    "startDateTime": from_date,
                    "endDateTime": to_date,
                    "$top": max_results,
                    "$orderby": "start/dateTime",
                },
            )

            if events_resp.status_code != 200:
                continue

            events = events_resp.json().get("value", [])

            for ev in events:

                # Teams link
                teams_link = None
                if ev.get("onlineMeeting"):
                    teams_link = ev["onlineMeeting"].get("joinUrl")

                # 🔥 Status mapping here
                status = (
                    "cancelled"
                    if ev.get("isCancelled")
                    else (
                        "tentative" if ev.get("showAs") == "tentative" else "confirmed"
                    )
                )

                all_events.append(
                    {
                        "calendar_id": cal_id,
                        "calendar_name": cal_name,
                        "event_id": ev.get("id"),
                        "summary": ev.get("subject"),
                        "start": ev.get("start"),
                        "end": ev.get("end"),
                        "attendees": ev.get("attendees", []),
                        "hangoutLink": teams_link,
                        "location": ev.get("location", {}).get("displayName"),
                        "description": ev.get("bodyPreview"),
                        "status": status,
                    }
                )

        return {
            "success": True,
            "from": from_date,
            "to": to_date,
            "count": len(all_events),
            "events": all_events,
        }

    # -------------------------------------------
    # CREATE EVENT
    # -------------------------------------------
    def create_calendar_event(
        self, title, start_dt, end_dt, attendees, description, location, meet=False
    ):

        # Ensure datetime → ISO 8601 string
        if hasattr(start_dt, "isoformat"):
            start_dt = start_dt.isoformat()

        if hasattr(end_dt, "isoformat"):
            end_dt = end_dt.isoformat()

        event_body = {
            "subject": title,
            "start": {"dateTime": start_dt, "timeZone": self.user_timezone},
            "end": {"dateTime": end_dt, "timeZone": self.user_timezone},
            "body": {"contentType": "HTML", "content": description or ""},
            "location": {"displayName": location or ""},
            "attendees": [
                {"emailAddress": {"address": x}, "type": "required"} for x in attendees
            ],
        }

        if meet:
            event_body["isOnlineMeeting"] = True
            event_body["onlineMeetingProvider"] = "teamsForBusiness"

        resp = requests.post(
            f"{self.GRAPH_BASE}/me/events",
            headers=self.headers,
            json=event_body,
        )

        if resp.status_code not in (200, 201):
            return {"success": False, "error": resp.text}

        updated = resp.json()

        return {
            "event_id": updated.get("id"),
            "summary": updated.get("subject"),
            "start": updated.get("start"),
            "end": updated.get("end"),
            "attendees": updated.get("attendees", []),
            "hangoutLink": (
                updated.get("onlineMeeting", {}).get("joinUrl")
                if updated.get("isOnlineMeeting")
                else None
            ),
            "location": updated.get("location", {}).get("displayName"),
            "description": updated.get("body", {}).get("content"),
            "status": (
                "cancelled"
                if updated.get("isCancelled")
                else (
                    "tentative" if updated.get("showAs") == "tentative" else "confirmed"
                )
            ),
        }

    # -------------------------------------------
    # UPDATE EVENT
    # -------------------------------------------
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
    ):
        """
        Update Microsoft calendar event (Teams-compatible but cleaned like Google).
        """

        # ---------------------------------------
        # STEP 1: Ensure token is fresh
        # ---------------------------------------
        self.ensure_fresh_token()

        # ---------------------------------------
        # STEP 2: Fetch existing event (needed for description)
        # ---------------------------------------
        existing_resp = requests.get(
            f"{self.GRAPH_BASE}/me/events/{event_id}",
            headers=self.headers,
        )

        if existing_resp.status_code != 200:
            return {
                "success": False,
                "error": f"Failed to get existing event: {existing_resp.text}",
            }

        existing = existing_resp.json()

        old_desc = existing.get("body", {}).get("content", "") or ""

        # ---------------------------------------
        # STEP 3: Build update PATCH body
        # ---------------------------------------
        body = {}

        if title:
            body["subject"] = title

        if start_dt:
            body["start"] = format_dt(start_dt, self.user_timezone)

        if end_dt:
            body["end"] = format_dt(end_dt, self.user_timezone)

        if location:
            body["location"] = {"displayName": location}

        # ----------------------------
        # Attendees
        # ----------------------------
        if attendees is not None:
            body["attendees"] = [
                {"emailAddress": {"address": email}, "type": "required"}
                for email in attendees
            ]

        # ----------------------------
        # Teams Meeting (like googlemeet=True)
        # ----------------------------
        if googlemeet:
            body["isOnlineMeeting"] = True
            body["onlineMeetingProvider"] = "teamsForBusiness"
        else:
            # Explicitly DISABLE Teams meeting
            body["isOnlineMeeting"] = False
            body["onlineMeetingProvider"] = None
            body["onlineMeeting"] = None

        # ----------------------------
        # DESCRIPTION (user + cleanup logic)
        # ----------------------------
        if description is not None:
            # User passed a new description → use it directly
            clean_desc = remove_teams_block(description)
        else:
            # Keep user's existing text, but remove Teams block
            clean_desc = remove_teams_block(old_desc)

        body["body"] = {
            "contentType": "HTML",
            "content": clean_desc,
        }

        # DEBUG
        # print("🟦 Final PATCH body sent to Microsoft:", json.dumps(body, indent=2))

        # ---------------------------------------
        # STEP 4: PATCH request
        # ---------------------------------------
        resp = requests.patch(
            f"{self.GRAPH_BASE}/me/events/{event_id}",
            headers=self.headers,
            json=body,
        )

        if resp.status_code not in (200, 202):
            # print("❌ Microsoft update failed:", resp.text)
            return {"success": False, "error": resp.text}

        updated = resp.json()

        # ---------------------------------------
        # STEP 5: Normalize response
        # ---------------------------------------
        return {
            "success": True,
            "event_id": updated.get("id"),
            "summary": updated.get("subject"),
            "start": updated.get("start"),
            "end": updated.get("end"),
            "attendees": updated.get("attendees", []),
            "hangoutLink": (
                updated.get("onlineMeeting", {}).get("joinUrl")
                if updated.get("isOnlineMeeting")
                else None
            ),
            "location": updated.get("location", {}).get("displayName"),
            "description": updated.get("body", {}).get("content"),
            "status": (
                "cancelled"
                if updated.get("isCancelled")
                else (
                    "tentative" if updated.get("showAs") == "tentative" else "confirmed"
                )
            ),
        }

    # -------------------------------------------
    # DELETE EVENT
    # -------------------------------------------
    def delete_calendar_event(self, event_id):
        resp = requests.delete(
            f"{self.GRAPH_BASE}/me/events/{event_id}",
            headers=self.headers,
        )
        if resp.status_code not in (204,):
            return {"success": False, "error": resp.text}

        return {"success": True}
