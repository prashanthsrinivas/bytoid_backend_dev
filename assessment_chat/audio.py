"""Assessment chat — audio conferencing (Phase 3).

A chat thread can spin up an audio call (Google Meet via the existing
``GoogleMeetService``). After the call, a recording is transcribed with the
existing Whisper service (``agent_route.s_t_s.Speech2TextService``) and
summarized into meeting notes (reusing the Bedrock wrapper). The summary can
be filed back into the chat (``source='call_summary'``) and/or the workflow
notes feed (``workflow_route.state_machine.add_comment``).

External calls (Meet creation, transcription, summarization) are driven
synchronously from the request thread via a fresh event loop — the same
pattern the rest of the package uses for ``get_fireworks_response2``.
"""

import asyncio
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

import pymysql.cursors

from db.rds_db import connect_to_rds
from utils.base_logger import get_logger
from utils.fireworkzz import get_fireworks_response2

from assessment_chat.schema import SIDE_INTERNAL
from assessment_chat.service import (
    ChatError,
    ChatNotFoundError,
    ChatPermissionError,
    ChatValidationError,
    get_thread,
    list_participants,
    post_message,
    push_new_message,
)

logger = get_logger(__name__)

DEFAULT_CALL_MINUTES = 60
_MAX_TRANSCRIPT_CHARS = 120_000  # guard the single-shot summarization prompt


def _run(coro):
    """Drive a coroutine to completion from a sync request thread."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _require_participant(thread_id: str, user_id: str) -> dict:
    from assessment_chat.service import _require_participant as _rp
    return _rp(thread_id, user_id)


def _get_call(call_id: str) -> dict:
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT * FROM chat_call_session WHERE call_id=%s", (call_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        raise ChatNotFoundError("call session not found")
    return dict(row)


# ── Start a call ──────────────────────────────────────────────────────────────


def start_call(
    thread_id: str,
    actor_user_id: str,
    *,
    title: str | None = None,
    duration_minutes: int = DEFAULT_CALL_MINUTES,
) -> dict:
    """Create a Google Meet for the thread and announce it in the conversation.

    The meeting is hosted by the thread owner (the account with Google
    connected); all active participants with an email are invited. A system
    chat message carrying the join link is posted, pushed to in-app
    participants, and emailed to bridged participants.
    """
    _require_participant(thread_id, actor_user_id)

    # In-house audio call: no external provider / join link. Participants connect
    # peer-to-peer (WebRTC) with signaling relayed over the websocket; the live
    # transcript is built from per-participant mic chunks (see transcribe_chunk).
    call_id = str(uuid.uuid4())
    conn = connect_to_rds()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO chat_call_session
                   (call_id, thread_id, started_by, provider, join_url, event_id, status)
                   VALUES (%s,%s,%s,'inhouse',NULL,NULL,'active')""",
                (call_id, thread_id, actor_user_id),
            )
        conn.commit()
    finally:
        conn.close()

    # Announce in the conversation so other participants can join in-app.
    msg = post_message(
        thread_id, actor_user_id,
        "📞 Audio call started in the conversation.",
        lang="en", visibility="all", source="system",
    )
    msg["call_id"] = call_id
    push_new_message(thread_id, msg)

    return {
        "call_id": call_id,
        "thread_id": thread_id,
        "provider": "inhouse",
        "status": "active",
        "message": msg,
    }


# ── Live transcript: per-participant mic chunks → Whisper → append-only ───────


def broadcast_call_signal(
    thread_id: str,
    from_user: str,
    *,
    kind: str,
    data: dict | None = None,
    to_user: str | None = None,
    call_id: str | None = None,
) -> None:
    """Relay a call control/signaling event over the websocket.

    ``to_user`` set → targeted (WebRTC offer/answer/ice). Otherwise broadcast to
    every other active participant of the thread (presence: join/leave). Pure
    fan-out; never raises.
    """
    try:
        from websockets_custom.ws_instance import ws_service
    except Exception:
        return

    extra = {
        "thread_id": thread_id,
        "call_id": call_id,
        "from_user": from_user,
        "kind": kind,
        "data": data or {},
        "event": kind,
    }

    if to_user:
        targets = [to_user]
    else:
        try:
            targets = [
                p["user_id"] for p in list_participants(thread_id)
                if p.get("user_id") and p.get("user_id") != from_user
            ]
        except Exception:
            return

    async def _run_emit():
        for uid in targets:
            try:
                await ws_service.emit(
                    user_id=uid, message="", scope="user",
                    msg_type="call_signal", feature="call_signal", extra=extra,
                )
            except Exception:
                pass

    _run(_run_emit())


def transcribe_chunk(
    call_id: str,
    actor_user_id: str,
    audio_path: str,
    *,
    lang: str | None = None,
    client_ts: int = 0,
) -> dict:
    """Transcribe one mic chunk (Whisper), store it as a transcript segment, and
    broadcast it live to the thread. Append-only: never mutates a shared blob, so
    concurrent chunks from multiple speakers can't race or scramble ordering."""
    call = _get_call(call_id)
    thread_id = call["thread_id"]
    _require_participant(thread_id, actor_user_id)

    from agent_route.s_t_s import Speech2TextService
    stt = Speech2TextService(actor_user_id)
    text = (_run(stt.transcribe_audio(audio_path)) or "").strip()
    if not text:
        return {"call_id": call_id, "text": ""}

    segment_id = str(uuid.uuid4())
    src_lang = (lang or "en").split("-")[0].lower()
    conn = connect_to_rds()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO chat_call_segment
                   (segment_id, call_id, sender_user_id, lang, text, client_ts)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (segment_id, call_id, actor_user_id, src_lang, text, int(client_ts or 0)),
            )
        conn.commit()
    finally:
        conn.close()

    # Live fan-out — translate each segment into EACH viewer's reading language so
    # the transcript shows in their language (en/fr/es/ja/zh). Cached per
    # (segment_id, lang) so re-emits/re-reads are cheap.
    try:
        from websockets_custom.ws_instance import ws_service
        from assessment_chat.translation import translate_message, normalize_lang
        recips = [p for p in list_participants(thread_id) if p.get("user_id")]

        async def _run_emit():
            for p in recips:
                uid = p["user_id"]
                tgt = normalize_lang(p.get("preferred_lang") or "en")
                try:
                    tr = translate_message(segment_id, text, src_lang, tgt, actor_user_id)
                    shown, ai = tr["text"], tr["ai_translated"]
                except Exception:
                    shown, ai = text, False
                extra = {
                    "thread_id": thread_id, "call_id": call_id, "segment_id": segment_id,
                    "sender_user_id": actor_user_id, "text": shown, "original_text": text,
                    "lang": tgt, "ai_translated": ai, "client_ts": int(client_ts or 0),
                    "event": "transcript",
                }
                try:
                    await ws_service.emit(
                        user_id=uid, message=shown, scope="user",
                        msg_type="call_transcript", feature="call_transcript", extra=extra,
                    )
                except Exception:
                    pass

        _run(_run_emit())
    except Exception:
        logger.debug("call transcript broadcast skipped", exc_info=False)

    return {"call_id": call_id, "segment_id": segment_id, "text": text, "lang": src_lang}


def _assemble_segments(call_id: str) -> str:
    """Build the full transcript from append-only segments, ORDER BY client_ts so
    out-of-order / concurrent arrivals never scramble the sentences."""
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT sender_user_id, text FROM chat_call_segment "
                "WHERE call_id=%s ORDER BY client_ts, created_at",
                (call_id,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    if not rows:
        return ""
    emails = {}
    try:
        from assessment_chat.service import _emails_for
        emails = _emails_for({r.get("sender_user_id") for r in rows})
    except Exception:
        pass
    lines = []
    for r in rows:
        who = emails.get(r.get("sender_user_id")) or r.get("sender_user_id") or "Participant"
        lines.append(f"{who}: {r['text']}")
    return "\n".join(lines)[:_MAX_TRANSCRIPT_CHARS]


# ── End a call: transcribe + summarize ───────────────────────────────────────


def _resolve_transcript(
    user_id: str,
    *,
    transcript: str | None,
    audio_path: str | None,
    audio_s3_key: str | None,
) -> tuple[str, str | None]:
    """Return (transcript_text, recording_ref). Transcribes via Whisper when a
    recording is supplied; falls back to a directly-provided transcript."""
    if transcript and transcript.strip():
        return transcript.strip(), audio_s3_key or audio_path

    tmp_path = None
    recording_ref = audio_s3_key or audio_path
    try:
        if audio_s3_key:
            from utils.s3_utils import read_binary_from_s3
            data = read_binary_from_s3(audio_s3_key)
            if not data:
                raise ChatValidationError("could not read recording from S3")
            suffix = os.path.splitext(audio_s3_key)[1] or ".mp3"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(data)
                tmp_path = tmp.name
            audio_path = tmp_path

        if not audio_path:
            raise ChatValidationError("provide transcript, audio_path, or audio_s3_key")

        from agent_route.s_t_s import Speech2TextService
        stt = Speech2TextService(user_id)
        text = _run(stt.transcribe_audio(audio_path))
        return (text or "").strip(), recording_ref
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _summarize_transcript(user_id: str, transcript: str) -> str:
    """Summarize a meeting transcript into structured notes via the LLM."""
    clipped = transcript[:_MAX_TRANSCRIPT_CHARS]
    truncated = len(transcript) > _MAX_TRANSCRIPT_CHARS
    prompt = (
        "You are taking minutes for a vendor/risk assessment call. Summarize the "
        "transcript below into concise meeting notes with these sections:\n"
        "1. Summary (2-4 sentences)\n2. Key points discussed\n"
        "3. Decisions\n4. Action items (owner — task)\n"
        "Use only what the transcript states; do not invent details.\n\n"
        f"Transcript:\n{clipped}"
    )
    out = _run(get_fireworks_response2(user_id, prompt, "user", None, 0.2))
    if not out or out == "INSUFFICIENT":
        # Fall back to the raw transcript so notes are never silently empty.
        out = transcript[:4000]
    if truncated:
        out += "\n\n[Note: transcript was truncated for summarization.]"
    return out


def end_call(
    call_id: str,
    actor_user_id: str,
    *,
    transcript: str | None = None,
    audio_path: str | None = None,
    audio_s3_key: str | None = None,
) -> dict:
    """Close a call: transcribe the recording (if given) and summarize it into
    meeting notes. Stores transcript + summary on the call session."""
    call = _get_call(call_id)
    _require_participant(call["thread_id"], actor_user_id)

    # Default path for in-house calls: assemble the live transcript from the
    # append-only segments (ordered). An explicitly-supplied transcript/recording
    # still wins (back-compat / external recordings).
    if not (transcript and transcript.strip()) and not audio_path and not audio_s3_key:
        text, recording_ref = _assemble_segments(call_id), None
    else:
        text, recording_ref = _resolve_transcript(
            actor_user_id, transcript=transcript, audio_path=audio_path, audio_s3_key=audio_s3_key,
        )
    summary = _summarize_transcript(actor_user_id, text) if text else ""

    conn = connect_to_rds()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE chat_call_session SET status='ended', ended_at=%s, "
                "recording_ref=%s, transcript=%s, summary=%s WHERE call_id=%s",
                (datetime.now(timezone.utc), recording_ref, text, summary, call_id),
            )
        conn.commit()
    finally:
        conn.close()

    # Tell peers the call is over so they tear down their connections.
    try:
        broadcast_call_signal(call["thread_id"], actor_user_id, kind="call_ended", call_id=call_id)
    except Exception:
        pass

    return {
        "call_id": call_id,
        "thread_id": call["thread_id"],
        "status": "ended",
        "transcript": text,
        "summary": summary,
    }


# ── File the summary into notes ──────────────────────────────────────────────


def summary_to_notes(
    call_id: str,
    actor_user_id: str,
    *,
    target: str = "both",
    visibility: str = "internal",
) -> dict:
    """Add the call summary to notes.

    ``target``: 'chat' (post into the conversation), 'workflow' (file into the
    workflow notes/comment feed), or 'both' (default). Filing into notes is an
    internal-staff action, so the actor must be an internal-side participant.
    """
    call = _get_call(call_id)
    thread_id = call["thread_id"]
    actor = _require_participant(thread_id, actor_user_id)
    if actor.get("side") != SIDE_INTERNAL:
        raise ChatPermissionError("only internal participants can file meeting notes")
    summary = (call.get("summary") or "").strip()
    if not summary:
        raise ChatValidationError("this call has no summary yet; end the call first")

    if target not in ("chat", "workflow", "both"):
        target = "both"

    chat_message = None
    if target in ("chat", "both"):
        chat_message = post_message(
            thread_id, actor_user_id,
            f"📝 Meeting notes (audio call):\n{summary}",
            lang="en", visibility=visibility, source="call_summary",
        )
        chat_message["call_id"] = call_id
        push_new_message(thread_id, chat_message)

    workflow_filed = False
    if target in ("workflow", "both"):
        thread = get_thread(thread_id)
        wf_id = thread.get("workflow_id")
        if wf_id:
            try:
                from workflow_route.state_machine import add_comment
                add_comment(wf_id, actor_user_id, f"Meeting notes from audio call {call_id}:\n{summary}")
                workflow_filed = True
            except Exception as exc:
                logger.warning("summary_to_notes: workflow add_comment failed: %s", exc)

    conn = connect_to_rds()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE chat_call_session SET notes_filed=1 WHERE call_id=%s", (call_id,)
            )
        conn.commit()
    finally:
        conn.close()

    return {
        "call_id": call_id,
        "filed_to_chat": chat_message is not None,
        "filed_to_workflow": workflow_filed,
        "message": chat_message,
    }
