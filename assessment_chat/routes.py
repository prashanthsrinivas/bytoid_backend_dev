"""Assessment chat — HTTP blueprint (Phase 1, core).

Endpoints (all prefixed ``/assessment-chat``):
  POST /thread               get-or-create the thread for a workflow context
  GET  /messages             paginated, translated to the caller's language
  POST /send                 post a message (WebSocket fan-out to participants)
  POST /language             set the caller's per-conversation reading language
  GET  /participants         list active participants
  GET  /eligible-users       org/SAML members addable to the thread
  POST /participants/add     add an org member or external/assessee email
  POST /participants/remove  deactivate a participant
  GET  /languages            supported languages + AI disclaimer text

Caller identity follows the repo convention: ``user_id`` (query or body) parsed
with ``parse_composite_user_id``. Realtime push reuses the shared ``ws_service``;
it is strictly best-effort — the GET /messages poll is the source of truth.
"""

from flask import Blueprint, jsonify, request

from utils.app_configs import IS_DEV
from utils.base_logger import get_logger
from utils.normal import parse_composite_user_id

from assessment_chat.schema import (
    SUPPORTED_LANGS,
    VISIBILITY_ALL,
    VISIBILITY_INTERNAL,
    bootstrap_schema,
)
from assessment_chat.service import (
    ChatError,
    add_participant,
    get_or_create_thread,
    list_messages,
    list_org_users,
    list_participants,
    post_message,
    push_new_message,
    remove_participant,
    set_language,
)
from assessment_chat.translation import AI_DISCLAIMER, LANG_NAMES

logger = get_logger(__name__, log_level="DEBUG" if IS_DEV else "INFO")
assessment_chat_bp = Blueprint("assessment_chat", __name__, url_prefix="/assessment-chat")

try:
    bootstrap_schema()
except Exception as _bs_exc:  # pragma: no cover - boot-time best effort
    logger.warning("assessment_chat schema bootstrap failed: %s", _bs_exc)


# ── Helpers ────────────────────────────────────────────────────────────────────


def _caller():
    """Resolve the acting user_id from query string or JSON body."""
    raw = request.args.get("user_id") or (request.get_json(silent=True) or {}).get("user_id")
    if not raw:
        return None
    _logged_in, user_id = parse_composite_user_id(raw)
    return user_id


def _err(exc: Exception):
    status = getattr(exc, "status", 400) if isinstance(exc, ChatError) else 500
    if status == 500:
        logger.exception("assessment_chat error: %s", exc)
        return jsonify({"error": "internal server error"}), 500
    return jsonify({"error": str(exc)}), status


# ── Endpoints ────────────────────────────────────────────────────────────────────


@assessment_chat_bp.route("/languages", methods=["GET"])
def languages():
    """Supported reading languages + the AI-translation disclaimer text."""
    return jsonify({
        "languages": [{"code": c, "name": LANG_NAMES.get(c, c)} for c in SUPPORTED_LANGS],
        "disclaimer": AI_DISCLAIMER,
    }), 200


@assessment_chat_bp.route("/thread", methods=["POST"])
def thread():
    """Get-or-create the chat thread for an assessment context.

    The conversation is anchored to ``(context_type, context_id)`` — typically
    the assessment doc ('runbook'/<runbook_id>) or an intake run
    ('intake'/<run_id>). A review workflow is OPTIONAL: the chat is available
    before "Send for review"; when a ``document_workflow`` exists (found via
    ``workflow_id`` or ``doc_type``+``doc_id``) its reviewer/approver parties are
    seeded automatically.

    Body: {user_id, context_type?, context_id (or workflow_id or doc_id),
           workflow_id?, doc_type?, doc_id?, email_subject?}
    """
    user_id = _caller()
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
    body = request.get_json(silent=True) or {}
    # Stable anchor: explicit context_id, else doc_id, else workflow_id.
    context_id = body.get("context_id") or body.get("doc_id") or body.get("workflow_id")
    if not context_id:
        return jsonify({"error": "context_id, doc_id or workflow_id is required"}), 400
    # Default the context type: 'runbook' when anchored on a doc, else 'workflow'.
    default_type = "runbook" if body.get("doc_id") else "workflow"
    try:
        t = get_or_create_thread(
            user_id,
            context_type=body.get("context_type") or default_type,
            context_id=context_id,
            workflow_id=body.get("workflow_id"),
            doc_type=body.get("doc_type"),
            doc_id=body.get("doc_id"),
            email_subject=body.get("email_subject"),
        )
    except Exception as exc:
        return _err(exc)
    return jsonify({"thread": t}), 200


@assessment_chat_bp.route("/messages", methods=["GET"])
def messages():
    """Paginated messages, translated into the caller's per-conversation language.

    Query: user_id, thread_id, page?, page_size?
    """
    user_id = _caller()
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
    thread_id = request.args.get("thread_id")
    if not thread_id:
        return jsonify({"error": "thread_id is required"}), 400
    try:
        page = int(request.args.get("page", 1))
        page_size = min(int(request.args.get("page_size", 50)), 200)
    except (TypeError, ValueError):
        page, page_size = 1, 50
    try:
        rows, total = list_messages(thread_id, user_id, page=page, page_size=page_size)
    except Exception as exc:
        return _err(exc)
    return jsonify({"messages": rows, "total": total, "page": page, "page_size": page_size}), 200


@assessment_chat_bp.route("/send", methods=["POST"])
def send():
    """Post a message. Body: {user_id, thread_id, text, lang?, visibility?}."""
    user_id = _caller()
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
    body = request.get_json(silent=True) or {}
    thread_id = body.get("thread_id")
    if not thread_id:
        return jsonify({"error": "thread_id is required"}), 400
    visibility = body.get("visibility", VISIBILITY_ALL)
    if visibility not in (VISIBILITY_ALL, VISIBILITY_INTERNAL):
        visibility = VISIBILITY_ALL
    try:
        msg = post_message(
            thread_id, user_id, body.get("text", ""),
            lang=body.get("lang"), visibility=visibility,
        )
    except Exception as exc:
        return _err(exc)

    # Realtime fan-out to in-app participants (best-effort).
    push_new_message(thread_id, msg)
    # Mirror outbound to email-bridged participants (best-effort; never blocks
    # the response on failure). Internal-visibility messages are not bridged.
    try:
        from assessment_chat.email_bridge import deliver_for_thread
        deliver_for_thread(thread_id, msg, user_id)
    except Exception:
        logger.debug("email bridge outbound skipped", exc_info=IS_DEV)
    return jsonify({"message": msg}), 201


@assessment_chat_bp.route("/language", methods=["POST"])
def language():
    """Set the caller's reading language for this thread. Body: {user_id, thread_id, lang}."""
    user_id = _caller()
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
    body = request.get_json(silent=True) or {}
    thread_id = body.get("thread_id")
    lang = body.get("lang")
    if not thread_id or not lang:
        return jsonify({"error": "thread_id and lang are required"}), 400
    try:
        code = set_language(thread_id, user_id, lang)
    except Exception as exc:
        return _err(exc)
    return jsonify({"preferred_lang": code}), 200


@assessment_chat_bp.route("/participants", methods=["GET"])
def participants():
    """List active participants. Query: user_id, thread_id."""
    user_id = _caller()
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
    thread_id = request.args.get("thread_id")
    if not thread_id:
        return jsonify({"error": "thread_id is required"}), 400
    try:
        # Authorization: caller must be a participant to view the roster.
        from assessment_chat.service import _require_participant
        _require_participant(thread_id, user_id)
        rows = list_participants(thread_id)
    except Exception as exc:
        return _err(exc)
    public = [
        {
            "participant_id": p["participant_id"],
            "user_id": p.get("user_id"),
            "email": p.get("email"),
            "role": p.get("role"),
            "side": p.get("side"),
            "preferred_lang": p.get("preferred_lang"),
            "is_external": bool(p.get("is_external")),
            "email_bridge": bool(p.get("email_bridge")),
        }
        for p in rows
    ]
    return jsonify({"participants": public}), 200


@assessment_chat_bp.route("/eligible-users", methods=["GET"])
def eligible_users():
    """Org/SAML members the caller could add. Query: user_id, thread_id?.

    Returns org members, excluding those already active on the thread.
    """
    user_id = _caller()
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
    try:
        members = list_org_users(user_id)
    except Exception as exc:
        return _err(exc)
    existing_ids = set()
    thread_id = request.args.get("thread_id")
    if thread_id:
        try:
            existing_ids = {p.get("user_id") for p in list_participants(thread_id) if p.get("user_id")}
        except Exception:
            existing_ids = set()
    users = [m for uid, m in members.items() if uid not in existing_ids]
    return jsonify({"users": users}), 200


@assessment_chat_bp.route("/participants/add", methods=["POST"])
def participants_add():
    """Add a participant.

    Body: {user_id, thread_id, target_user_id? , email?, side?, role?, email_bridge?}
    """
    user_id = _caller()
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
    body = request.get_json(silent=True) or {}
    thread_id = body.get("thread_id")
    if not thread_id:
        return jsonify({"error": "thread_id is required"}), 400
    try:
        added = add_participant(
            thread_id, user_id,
            target_user_id=body.get("target_user_id"),
            email=body.get("email"),
            side=body.get("side"),
            role=body.get("role", "added"),
            email_bridge=bool(body.get("email_bridge")),
        )
    except Exception as exc:
        return _err(exc)
    return jsonify({"participant": added}), 201


@assessment_chat_bp.route("/participants/remove", methods=["POST"])
def participants_remove():
    """Deactivate a participant. Body: {user_id, thread_id, participant_id}."""
    user_id = _caller()
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
    body = request.get_json(silent=True) or {}
    thread_id = body.get("thread_id")
    participant_id = body.get("participant_id")
    if not thread_id or not participant_id:
        return jsonify({"error": "thread_id and participant_id are required"}), 400
    try:
        remove_participant(thread_id, user_id, participant_id)
    except Exception as exc:
        return _err(exc)
    return jsonify({"ok": True}), 200


@assessment_chat_bp.route("/email/inbound", methods=["POST"])
def email_inbound():
    """Ingest an inbound email reply into its bridged chat thread.

    Intended for the mail poller / webhook. Matches the reply to a bridged
    thread (by provider thread id, or sender + subject fallback), dedupes, and
    posts it as a ``source='email'`` message. Returns the created message, or
    ``{ingested: false}`` when the email is not part of a bridged conversation.

    Body: {provider, from_email, subject, body, email_thread_id, email_message_id}
    """
    body = request.get_json(silent=True) or {}
    from assessment_chat.email_bridge import ingest_inbound_email

    msg = ingest_inbound_email(
        provider=body.get("provider", "gmail"),
        from_email=body.get("from_email"),
        subject=body.get("subject"),
        body=body.get("body"),
        email_thread_id=body.get("email_thread_id"),
        email_message_id=body.get("email_message_id"),
    )
    if not msg:
        return jsonify({"ingested": False}), 200
    return jsonify({"ingested": True, "message": msg}), 201


@assessment_chat_bp.route("/call/start", methods=["POST"])
def call_start():
    """Start an audio call (Google Meet) from the thread.

    Body: {user_id, thread_id, title?, duration_minutes?}
    """
    user_id = _caller()
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
    body = request.get_json(silent=True) or {}
    thread_id = body.get("thread_id")
    if not thread_id:
        return jsonify({"error": "thread_id is required"}), 400
    try:
        from assessment_chat.audio import start_call
        result = start_call(
            thread_id, user_id,
            title=body.get("title"),
            duration_minutes=int(body.get("duration_minutes") or 60),
        )
    except Exception as exc:
        return _err(exc)
    return jsonify(result), 201


@assessment_chat_bp.route("/call/end", methods=["POST"])
def call_end():
    """End a call and produce meeting notes from a recording or transcript.

    Body: {user_id, call_id, transcript? | audio_path? | audio_s3_key?}
    """
    user_id = _caller()
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
    body = request.get_json(silent=True) or {}
    call_id = body.get("call_id")
    if not call_id:
        return jsonify({"error": "call_id is required"}), 400
    try:
        from assessment_chat.audio import end_call
        result = end_call(
            call_id, user_id,
            transcript=body.get("transcript"),
            audio_path=body.get("audio_path"),
            audio_s3_key=body.get("audio_s3_key"),
        )
    except Exception as exc:
        return _err(exc)

    # Auto-file the Minutes of Meeting into notes (best-effort). Requires an
    # internal-side actor; if the ender is assessee-side this no-ops and the UI
    # still shows the MoM for an internal user to file manually.
    result["notes_filed"] = False
    try:
        if (result.get("summary") or "").strip():
            from assessment_chat.audio import summary_to_notes
            filed = summary_to_notes(call_id, user_id, target="both")
            result["notes_filed"] = True
            result["notes_message"] = filed.get("message")
    except Exception as exc:
        logger.debug("auto-file MoM skipped: %s", exc)
    return jsonify(result), 200


@assessment_chat_bp.route("/call/transcribe-chunk", methods=["POST"])
def call_transcribe_chunk():
    """Transcribe one live mic chunk and broadcast it to the thread.

    multipart/form-data: {user_id, call_id, lang?, client_ts?, audio}
    """
    raw_uid = request.form.get("user_id")
    user_id = parse_composite_user_id(raw_uid)[1] if raw_uid else None
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
    call_id = request.form.get("call_id")
    audio = request.files.get("audio")
    if not call_id or audio is None:
        return jsonify({"error": "call_id and audio are required"}), 400

    import os
    import tempfile

    suffix = os.path.splitext(audio.filename or "")[1] or ".webm"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            audio.save(tmp.name)
            tmp_path = tmp.name
        from assessment_chat.audio import transcribe_chunk
        result = transcribe_chunk(
            call_id, user_id, tmp_path,
            lang=request.form.get("lang"),
            client_ts=int(request.form.get("client_ts") or 0),
        )
    except Exception as exc:
        return _err(exc)
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    return jsonify(result), 200


@assessment_chat_bp.route("/call/ice", methods=["GET"])
def call_ice():
    """ICE servers for the in-house WebRTC call. STUN is always returned; TURN is
    included when configured (required for reliable cross-network connectivity)."""
    import os

    ice = [{"urls": os.getenv("STUN_URL", "stun:stun.l.google.com:19302")}]
    turn_url = os.getenv("TURN_URL")
    if turn_url:
        server = {"urls": turn_url}
        if os.getenv("TURN_USERNAME"):
            server["username"] = os.getenv("TURN_USERNAME")
        if os.getenv("TURN_CREDENTIAL"):
            server["credential"] = os.getenv("TURN_CREDENTIAL")
        ice.append(server)
    return jsonify({"ice_servers": ice}), 200


@assessment_chat_bp.route("/call/summary-to-notes", methods=["POST"])
def call_summary_to_notes():
    """File a call's summary into the chat and/or workflow notes.

    Body: {user_id, call_id, target? ('chat'|'workflow'|'both'), visibility?}
    """
    user_id = _caller()
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
    body = request.get_json(silent=True) or {}
    call_id = body.get("call_id")
    if not call_id:
        return jsonify({"error": "call_id is required"}), 400
    try:
        from assessment_chat.audio import summary_to_notes
        result = summary_to_notes(
            call_id, user_id,
            target=body.get("target", "both"),
            visibility=body.get("visibility", "internal"),
        )
    except Exception as exc:
        return _err(exc)
    return jsonify(result), 200
