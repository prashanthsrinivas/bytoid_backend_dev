"""Assessment chat — external meeting links (Google Meet / Microsoft Teams).

The "Call" control in a chat thread creates a meeting on the caller's calendar
and posts the join link into the conversation — either instantly or scheduled
for a chosen time. Provider is chosen by the caller's login:

  * Google login    → Google Meet  (services.meet_service.GoogleMeetService)
  * Microsoft login  → Teams        (services.microsoft_calender_service)

Recording/transcription/notes are handled natively by Meet/Teams. There is no
in-house audio/transcription in the app.
"""

import uuid
from datetime import datetime, timedelta, timezone

from db.rds_db import connect_to_rds
from utils.base_logger import get_logger

from assessment_chat.service import (
    ChatError,
    get_thread,
    list_participants,
    post_message,
    push_new_message,
)

logger = get_logger(__name__)

DEFAULT_CALL_MINUTES = 30


def _require_participant(thread_id: str, user_id: str) -> dict:
    from assessment_chat.service import _require_participant as _rp
    return _rp(thread_id, user_id)


def _parse_dt(value: str) -> datetime:
    """Parse an ISO-8601 datetime to an aware UTC datetime."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _create_meeting(host_user_id, provider, title, start, end, attendees, description):
    """Create the external meeting on the host's calendar. Returns
    (join_url, event_id, provider_tag)."""
    if provider == "microsoft":
        from services.microsoft_calender_service import MicrosoftGraphCalendarService

        svc = MicrosoftGraphCalendarService(host_user_id)
        res = svc.create_calendar_event(
            title, start, end, attendees, description, location="", meet=True,
        )
        if not isinstance(res, dict) or not res.get("hangoutLink"):
            raise ChatError(f"could not create the Teams meeting: {(res or {}).get('error', 'unknown error')}")
        return res["hangoutLink"], res.get("event_id"), "teams"

    # default: Google Meet
    from services.meet_service import GoogleMeetService

    meet = GoogleMeetService(userid=host_user_id)
    res = meet.createbasemeet(
        summary=title,
        start_time=start.isoformat(),
        end_time=end.isoformat(),
        attendees=attendees,
        description=description,
        timezone="UTC",
    )
    if not isinstance(res, dict) or not res.get("success"):
        raise ChatError(f"could not create the Google Meet: {(res or {}).get('error', 'unknown error')}")
    return res.get("meet_link"), res.get("event_id"), "google_meet"


def _resolve_provider(provider_hint: str | None) -> str:
    """Normalize the caller's provider ('google' | 'microsoft'). Falls back to the
    session's oauth_provider, then to Google."""
    p = (provider_hint or "").strip().lower()
    if p in ("google", "microsoft"):
        return p
    try:
        from flask import session
        p = (session.get("oauth_provider") or "").strip().lower()
    except Exception:
        p = ""
    if p in ("google", "microsoft"):
        return p
    return "google"


def start_call(
    thread_id: str,
    actor_user_id: str,
    *,
    provider: str | None = None,
    start_time: str | None = None,
    duration_minutes: int = DEFAULT_CALL_MINUTES,
    title: str | None = None,
    host_user_id: str | None = None,
) -> dict:
    """Create an external meeting (Google Meet or Teams) and announce it in the
    conversation. Instant when ``start_time`` is omitted; otherwise scheduled.
    The host (default: caller) must have the matching account connected — they
    own the calendar event and the meeting; attendees are invited natively.
    """
    _require_participant(thread_id, actor_user_id)
    thread = get_thread(thread_id)
    host = host_user_id or actor_user_id
    provider = _resolve_provider(provider)

    attendees = [p["email"] for p in list_participants(thread_id) if p.get("email")]
    scheduled = bool(start_time)
    start = _parse_dt(start_time) if start_time else datetime.now(timezone.utc)
    end = start + timedelta(minutes=max(int(duration_minutes or DEFAULT_CALL_MINUTES), 1))
    summary = title or (
        f"Assessment call — {thread.get('doc_type') or 'review'} {thread.get('doc_id') or ''}".strip()
    )
    description = "Meeting started from the Bytoid assessment conversation."

    join_url, event_id, provider_tag = _create_meeting(
        host, provider, summary, start, end, attendees, description,
    )
    if not join_url:
        raise ChatError("could not create the meeting (no join link returned)")

    call_id = str(uuid.uuid4())
    conn = connect_to_rds()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO chat_call_session
                   (call_id, thread_id, started_by, provider, join_url, event_id, status)
                   VALUES (%s,%s,%s,%s,%s,%s,'active')""",
                (call_id, thread_id, actor_user_id, provider_tag, join_url, event_id),
            )
        conn.commit()
    finally:
        conn.close()

    if scheduled:
        when = start.strftime("%b %d, %Y %H:%M UTC")
        text = f"📅 Meeting scheduled for {when}. Join: {join_url}"
    else:
        text = f"📞 Audio call started. Join: {join_url}"
    msg = post_message(
        thread_id, actor_user_id, text, lang="en", visibility="all", source="system",
    )
    msg["call_id"] = call_id
    push_new_message(thread_id, msg)

    return {
        "call_id": call_id,
        "thread_id": thread_id,
        "provider": provider_tag,
        "join_url": join_url,
        "scheduled": scheduled,
        "status": "active",
    }
