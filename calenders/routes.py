from db.db_checkers import fetch_user_Social
from db.rds_db import connect_to_rds
from flask import Blueprint, request
from services.meet_service import GoogleMeetService
from datetime import datetime
import pytz
from utils.base_logger import get_logger
from services.microsoft_calender_service import MicrosoftGraphCalendarService
from umail_helper.mails_process import get_integration_users
from utils.normal import sanitize_value, strip_html

from utils.permission_required import permission_required_body
logger = get_logger(__name__)
calenders_bp = Blueprint("calender", __name__)


@calenders_bp.route("/check-user-events", methods=["POST"])
@permission_required_body("calendar.view.confirmed")
def get_all_user_events():
    try:
        body = request.json or {}
        # print("body", body)

        userid = body.get("userid")
        holidays_param = body.get("holidays", False)
        from_date_raw = body.get("from_date", None)
        to_date_raw = body.get("to_date", None)

        if not userid:
            return {"success": False, "message": "userid required"}, 400

        # Normalize holidays boolean
        if isinstance(holidays_param, str):
            holidays = holidays_param.lower() in ["true", "1", "yes"]
        else:
            holidays = bool(holidays_param)

        # ---- SAFE DATETIME PARSER ----
        def parse_iso(dt_str):
            if not dt_str:
                return None
            if dt_str.startswith("0000"):
                return None
            try:
                if dt_str.endswith("Z"):
                    dt_str = dt_str[:-1] + "+00:00"
                    if dt_str.year < 1:
                        return None
                return datetime.fromisoformat(dt_str)
            except:
                try:
                    return datetime.strptime(dt_str, "%Y-%m-%d")
                except:
                    raise ValueError(f"Invalid datetime format: {dt_str}")

        from_date = parse_iso(from_date_raw) if from_date_raw else None
        to_date = parse_iso(to_date_raw) if to_date_raw else None
        service = None
        # print(type(userid), userid)

        val = fetch_user_Social(user_id=userid)
        logger.info("Platform detected:%s", val)
        connection = connect_to_rds()
        integrations = get_integration_users(userid, connection)
        # print("val receved", val)

        # Initialize service
        if val == "google":
            # print("into google service")
            service = GoogleMeetService(userid=userid, integrations=integrations)
        elif val == "microsoft" or val == "saml":
            # print("into microsoft")
            service = MicrosoftGraphCalendarService(userid=userid)

        # Fetch events
        result = service.view_all_events(
            holidays=holidays,
            from_date=from_date.isoformat() if from_date else None,
            to_date=to_date.isoformat() if to_date else None,
        )

        return result, 200

    except Exception as e:
        # print("Error in get_all_user_events:", e)
        return {"success": False, "error": str(e)}, 500


@calenders_bp.route("/create-user-event", methods=["POST"])
@permission_required_body("calendar.create")
def create_user_event():
    try:
        body = request.json or {}

        userid = body.get("userid")
        title = body.get("title")
        start_time_raw = body.get("start_time")
        end_time_raw = body.get("end_time")
        attendees = body.get("attendees", [])
        description = body.get("description")
        location = body.get("location")
        googlemeet = body.get("googlemeet", False)

        if not userid:
            return {"success": False, "message": "userid required"}, 400
        if not start_time_raw or not end_time_raw:
            return {
                "success": False,
                "message": "start_time and end_time required",
            }, 400

        # Normalize boolean
        if isinstance(googlemeet, str):
            googlemeet = googlemeet.lower() in ["true", "1", "yes"]

        # Init service (loads user timezone automatically)
        val = fetch_user_Social(user_id=userid)
        # Initialize service
        if val == "google":
            service = GoogleMeetService(userid=userid)
            organizer_tz = pytz.timezone(service.organizer_tz)
        elif val == "microsoft":
            service = MicrosoftGraphCalendarService(userid=userid)
            organizer_tz = pytz.timezone(service.user_timezone)

        # Time parsing
        def parse_time(dt):
            # "2025-11-30T18:30:00.000Z" → convert to organizer TZ
            try:
                if dt.endswith("Z"):
                    dt = dt[:-1] + "+00:00"
                d = datetime.fromisoformat(dt)
            except:
                d = datetime.strptime(dt, "%Y-%m-%d %H:%M")

            # If naive, apply organizer tz
            if d.tzinfo is None:
                return organizer_tz.localize(d)
            return d.astimezone(organizer_tz)

        start_dt = parse_time(start_time_raw)
        end_dt = parse_time(end_time_raw)

        # Create event
        result = service.create_calendar_event(
            title=title,
            start_dt=start_dt,
            end_dt=end_dt,
            attendees=attendees,
            description=description,
            location=location,
            meet=googlemeet,
        )

        return {"success": True, "event": result}, 200

    except Exception as e:
        # print("Error in create_user_event:", e)
        return {"success": False, "error": str(e)}, 500


@calenders_bp.route("/update-user-event", methods=["POST"])
@permission_required_body("calendar.create")
def update_user_event():
    try:
        body = request.json or {}

        userid = body.get("userid")
        event_id = body.get("event_id")

        if not userid or not event_id:
            return {"success": False, "message": "userid and event_id required"}, 400

        title = body.get("title")
        start_time_raw = body.get("start_time")
        end_time_raw = body.get("end_time")
        attendees = body.get("attendees", [])
        description = body.get("description")
        location = body.get("location")
        googlemeet = body.get("googlemeet", False)

        if isinstance(googlemeet, str):
            googlemeet = googlemeet.lower() in ["true", "1", "yes"]

        val = fetch_user_Social(user_id=userid)

        if val == "google":
            service = GoogleMeetService(userid=userid)
            organizer_tz = pytz.timezone(service.organizer_tz)
        elif val == "microsoft":
            service = MicrosoftGraphCalendarService(userid=userid)
            organizer_tz = pytz.timezone(service.user_timezone)
        else:
            return {"success": False, "message": "Unsupported provider"}, 400

        def parse_dt(dt):
            if not dt:
                return None
            try:
                if dt.endswith("Z"):
                    dt = dt[:-1] + "+00:00"
                d = datetime.fromisoformat(dt)
            except Exception:
                d = datetime.strptime(dt, "%Y-%m-%d %H:%M")
            if d.tzinfo is None:
                return organizer_tz.localize(d)
            return d.astimezone(organizer_tz)

        start_dt = parse_dt(start_time_raw)
        end_dt = parse_dt(end_time_raw)

        updated_event = service.update_calendar_event(
            event_id=event_id,
            title=title,
            start_dt=start_dt,
            end_dt=end_dt,
            attendees=attendees,
            description=description,
            location=location,
            googlemeet=googlemeet,
        )

        # 🔥 CRITICAL: sanitize response here
        safe_event = sanitize_value(strip_html(updated_event))

        return {"success": True, "event": safe_event}, 200

    except Exception:
        return {"success": False, "error": "Internal server error"}, 500


@calenders_bp.route("/delete-user-event", methods=["POST"])
@permission_required_body("calendar.create")
def delete_user_event():
    try:
        body = request.json or {}

        userid = body.get("userid")
        event_id = body.get("event_id")

        if not userid or not event_id:
            return {"success": False, "message": "userid and event_id required"}, 400

        val = fetch_user_Social(user_id=userid)

        if val == "google":
            service = GoogleMeetService(userid=userid)
        elif val == "microsoft":
            service = MicrosoftGraphCalendarService(userid=userid)
        else:
            return {"success": False, "message": "Unsupported provider"}, 400

        result = service.delete_calendar_event(event_id)

        # 🔥 sanitize result too
        safe_result = sanitize_value(result)

        return {"success": True, "result": safe_result}, 200

    except Exception:
        return {"success": False, "error": "Internal server error"}, 500
