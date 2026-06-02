"""Assessment chat — bidirectional email bridge (Phase 2).

Outbound: when a message is posted, mirror it by email to every active
participant flagged ``email_bridge``. Each recipient gets the body translated
into their per-conversation language, with the original text and the AI
disclaimer appended. ``internal``-visibility messages are NEVER emailed.

Inbound: the existing Gmail/Outlook fetch path (and the ``/email/inbound``
endpoint) call :func:`ingest_inbound_email`. A reply whose provider thread id
matches a bridged ``chat_thread`` (or, as a fallback, whose sender is a known
external participant and whose normalized subject matches) is ingested as a
``source='email'`` chat message and pushed to in-app participants.

Sending reuses the existing wrappers verbatim:
  * Gmail  — ``gmail_route.routes.send_mail(user_id, to, subject, body)``
  * Outlook — ``microsoft_route.routes.outlook_send_mail(user_id, to, subject,
              body, thread_id=...)``
Provider is detected from the thread owner's connected mailbox.
"""

import re
import uuid

import pymysql.cursors

from db.rds_db import connect_to_rds
from utils.base_logger import get_logger

from assessment_chat.schema import DEFAULT_LANG, VISIBILITY_INTERNAL
from assessment_chat.service import (
    get_thread,
    list_participants,
    push_new_message,
)
from assessment_chat.translation import (
    AI_DISCLAIMER,
    LANG_NAMES,
    normalize_lang,
    translate_message,
)

logger = get_logger(__name__)

_RE_PREFIX = re.compile(r"^\s*((re|fwd|fw)\s*:\s*)+", re.IGNORECASE)
# Common quoted-reply separators — used to trim history off an inbound body.
_QUOTE_MARKERS = (
    "-----Original Message-----",
    "________________________________",
    "From:",
)
_ON_WROTE = re.compile(r"\nOn .{0,120}\bwrote:\s*\n", re.IGNORECASE)


def _normalize_subject(subject: str | None) -> str:
    return _RE_PREFIX.sub("", (subject or "").strip()).strip().lower()


def _default_subject(thread: dict) -> str:
    parts = [p for p in (thread.get("doc_type"), thread.get("doc_id")) if p]
    suffix = (" — " + " ".join(parts)) if parts else ""
    return f"[Bytoid Assessment]{suffix}"


# ── Provider detection ─────────────────────────────────────────────────────────


def detect_provider(user_id: str) -> tuple[str | None, str | None]:
    """Return (``'gmail'`` | ``'outlook'`` | None, from_email) for a user.

    Prefers Google creds stored directly on the users row, then an active
    ``integrations`` row (``platform`` 'google' / 'microsoft').
    """
    if not user_id:
        return None, None
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT token, email FROM users WHERE user_id=%s LIMIT 1", (user_id,)
            )
            u = cur.fetchone() or {}
            cur.execute(
                "SELECT platform, email FROM integrations "
                "WHERE (user_id=%s OR primary_user_id_fk=%s) AND status='active'",
                (user_id, user_id),
            )
            ints = cur.fetchall() or []
    finally:
        conn.close()

    platforms = {r.get("platform") for r in ints}
    if u.get("token"):
        return "gmail", u.get("email")
    if "google" in platforms:
        return "gmail", next((r["email"] for r in ints if r.get("platform") == "google"), u.get("email"))
    if "microsoft" in platforms:
        return "outlook", next((r["email"] for r in ints if r.get("platform") == "microsoft"), None)
    return None, u.get("email")


# ── Outbound ───────────────────────────────────────────────────────────────────


def _compose_body(message: dict, recipient_lang: str, sender_label: str) -> str:
    """Build the email body: translated text + original + AI disclaimer footer."""
    target = normalize_lang(recipient_lang or DEFAULT_LANG)
    tr = translate_message(
        message_id=message["message_id"],
        original_text=message["original_text"],
        original_lang=message.get("original_lang", DEFAULT_LANG),
        target_lang=target,
        requester_user_id=message.get("sender_user_id") or "system",
    )
    lines = [tr["text"], "", f"— {sender_label} (via Bytoid Assessment)"]
    if tr["ai_translated"]:
        src_name = LANG_NAMES.get(tr["original_lang"], tr["original_lang"])
        lines += [
            "",
            f"Original ({src_name}):",
            tr["original_text"],
            "",
            f"[{AI_DISCLAIMER}]",
        ]
    lines += ["", "Reply to this email to respond in the assessment conversation."]
    return "\n".join(lines)


def _persist_thread_ref(thread_id: str, provider: str, subject: str, email_thread_id: str | None) -> None:
    conn = connect_to_rds()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE chat_thread SET email_provider=%s, "
                "email_subject=COALESCE(email_subject,%s), "
                "email_thread_id=COALESCE(email_thread_id,%s) "
                "WHERE thread_id=%s",
                (provider, subject, email_thread_id, thread_id),
            )
        conn.commit()
    finally:
        conn.close()


def _update_last_msgid(thread_id: str, msgid: str | None) -> None:
    """Record the most recent RFC Message-ID so the next outbound can thread
    via In-Reply-To. No-op when ``msgid`` is falsy."""
    if not msgid:
        return
    conn = connect_to_rds()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE chat_thread SET email_last_msgid=%s WHERE thread_id=%s",
                (msgid, thread_id),
            )
        conn.commit()
    finally:
        conn.close()


def _rfc_from_gmail_return(ret, owner_id: str) -> str | None:
    """Gmail wrappers return ``f"{user_id}_{rfc_message_id}"``. We know the
    owner id, so strip that exact prefix to recover the RFC Message-ID."""
    if not isinstance(ret, str):
        return None
    prefix = f"{owner_id}_"
    return ret[len(prefix):] if ret.startswith(prefix) else None


def deliver_message(thread: dict, message: dict, sender_user_id: str) -> int:
    """Mirror a posted message out to email-bridged participants.

    Returns the number of emails sent. Never raises — best-effort.
    """
    if message.get("visibility") == VISIBILITY_INTERNAL:
        return 0  # internal messages are never bridged

    thread_id = thread["thread_id"]
    try:
        participants = list_participants(thread_id)
    except Exception:
        return 0
    targets = [
        p for p in participants
        if p.get("email_bridge") and p.get("email") and p.get("user_id") != sender_user_id
    ]
    if not targets:
        return 0

    owner_id = thread.get("created_by")
    provider, _from_email = detect_provider(owner_id)
    if not provider:
        logger.info("email bridge: thread owner %s has no connected mailbox", owner_id)
        return 0

    subject = thread.get("email_subject") or _default_subject(thread)
    sender_label = message.get("sender_email") or message.get("sender_user_id") or "Assessment team"

    # Track the live thread refs across recipients in this call so the first
    # send seeds the thread id / message id the later sends thread onto.
    email_thread_id = thread.get("email_thread_id")
    email_last_msgid = thread.get("email_last_msgid")

    sent = 0
    for p in targets:
        body = _compose_body(message, p.get("preferred_lang"), str(sender_label))
        try:
            if provider == "gmail":
                if email_thread_id and email_last_msgid:
                    # Reply into the existing Gmail thread (proper In-Reply-To).
                    from gmail_route.routes import gmail_reply
                    res = gmail_reply(
                        owner_id, p["email"], subject,
                        thread_id=email_thread_id, body_text=body,
                        in_reply_to=email_last_msgid,
                    )
                    rfc = _rfc_from_gmail_return(res, owner_id)
                    if rfc:
                        sent += 1
                        email_last_msgid = rfc
                        _update_last_msgid(thread_id, rfc)
                    else:
                        logger.warning("gmail bridge reply failed to %s: %s", p.get("email"), res)
                else:
                    # First message — start the thread, then seed its RFC id so
                    # subsequent messages thread via In-Reply-To.
                    from gmail_route.routes import send_mail
                    res = send_mail(owner_id, p["email"], subject, body)
                    if isinstance(res, dict) and res.get("status") == "success":
                        sent += 1
                        email_thread_id = res.get("thread_id") or email_thread_id
                        _persist_thread_ref(thread_id, provider, subject, email_thread_id)
                        rfc = _seed_gmail_rfc(owner_id, res.get("message_id"))
                        if rfc:
                            email_last_msgid = rfc
                            _update_last_msgid(thread_id, rfc)
                    else:
                        logger.warning("gmail bridge send failed to %s: %s", p.get("email"), res)
            else:
                from microsoft_route.routes import outlook_send_mail
                res = outlook_send_mail(
                    owner_id, p["email"], subject, body,
                    thread_id=email_thread_id, in_reply_to=email_last_msgid,
                )
                if isinstance(res, dict) and res.get("status") != "failed":
                    sent += 1
                    email_thread_id = res.get("thread_id") or email_thread_id
                    _persist_thread_ref(thread_id, provider, subject, email_thread_id)
                    new_rfc = res.get("internet_message_id")
                    if new_rfc:
                        email_last_msgid = new_rfc
                        _update_last_msgid(thread_id, new_rfc)
                else:
                    logger.warning("outlook bridge send failed to %s: %s", p.get("email"), res)
        except Exception as exc:
            logger.warning("email bridge send failed to %s: %s", p.get("email"), exc)
    return sent


def _seed_gmail_rfc(owner_id: str, message_id: str | None) -> str | None:
    """After a first Gmail send, recover the sent message's RFC Message-ID.

    ``send_mail`` returns ``message_id=f"{user_id}_{gmail_api_id}"``; we know
    the owner id, so we strip it to get the API id, then look up the RFC header
    via the existing ``get_message_id`` helper. Best-effort — returns None on
    any failure (threading simply falls back to a fresh send next time).
    """
    if not message_id:
        return None
    prefix = f"{owner_id}_"
    api_id = message_id[len(prefix):] if message_id.startswith(prefix) else None
    if not api_id:
        return None
    try:
        from gmail_route.routes import get_message_id
        from services.gmail_service import GmailService

        gs = GmailService(owner_id)
        return get_message_id(gs.service, "me", api_id)
    except Exception as exc:
        logger.debug("gmail RFC id seed failed: %s", exc)
        return None


# ── Inbound ────────────────────────────────────────────────────────────────────


def _trim_quoted(body: str) -> str:
    """Best-effort strip of quoted reply history from an inbound email body."""
    if not body:
        return ""
    text = body
    m = _ON_WROTE.search(text)
    if m:
        text = text[: m.start()]
    for marker in _QUOTE_MARKERS:
        idx = text.find(marker)
        if idx > 0:
            text = text[:idx]
    # Drop trailing quoted ('>') lines.
    kept = []
    for line in text.splitlines():
        if line.lstrip().startswith(">"):
            break
        kept.append(line)
    return "\n".join(kept).strip() or body.strip()


def _find_bridged_thread(email_thread_id: str | None, from_email: str | None, subject: str | None) -> dict | None:
    """Locate the chat thread a reply belongs to.

    Primary: provider thread id == ``chat_thread.email_thread_id``.
    Fallback: sender is an active external participant of a single thread whose
    normalized subject matches the inbound (handles new-thread replies).
    """
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            if email_thread_id:
                cur.execute(
                    "SELECT * FROM chat_thread WHERE email_thread_id=%s LIMIT 1",
                    (email_thread_id,),
                )
                row = cur.fetchone()
                if row:
                    return dict(row)
            if from_email:
                norm = _normalize_subject(subject)
                cur.execute(
                    """SELECT t.* FROM chat_thread t
                       JOIN chat_participant p ON p.thread_id=t.thread_id
                       WHERE p.active=1 AND p.is_external=1
                         AND LOWER(p.email)=LOWER(%s)
                         AND t.email_subject IS NOT NULL""",
                    (from_email,),
                )
                candidates = [dict(r) for r in cur.fetchall()]
                matches = [c for c in candidates if _normalize_subject(c.get("email_subject")) == norm]
                if len(matches) == 1:
                    return matches[0]
    finally:
        conn.close()
    return None


def _already_ingested(email_message_id: str | None) -> bool:
    if not email_message_id:
        return False
    conn = connect_to_rds()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM chat_message WHERE email_message_id=%s LIMIT 1",
                (email_message_id,),
            )
            return cur.fetchone() is not None
    finally:
        conn.close()


def ingest_inbound_email(
    *,
    provider: str,
    from_email: str | None,
    subject: str | None,
    body: str | None,
    email_thread_id: str | None,
    email_message_id: str | None,
) -> dict | None:
    """Ingest an inbound email reply into its bridged chat thread.

    Returns the created message dict (and pushes it to in-app participants), or
    None if the email isn't part of a bridged conversation / is a duplicate.
    Never raises — safe to call from the mail fetch path.
    """
    try:
        if _already_ingested(email_message_id):
            return None
        thread = _find_bridged_thread(email_thread_id, from_email, subject)
        if not thread:
            return None

        thread_id = thread["thread_id"]
        # Map sender email → an active participant (to attribute + pick language).
        sender_user_id = None
        original_lang = DEFAULT_LANG
        for p in list_participants(thread_id):
            if p.get("email") and from_email and p["email"].lower() == from_email.lower():
                sender_user_id = p.get("user_id")
                original_lang = normalize_lang(p.get("preferred_lang") or DEFAULT_LANG)
                break

        text = _trim_quoted(body or "")
        if not text:
            return None

        message_id = str(uuid.uuid4())
        conn = connect_to_rds()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(
                    """INSERT INTO chat_message
                       (message_id, thread_id, sender_user_id, sender_email,
                        original_text, original_lang, visibility, source, email_message_id)
                       VALUES (%s,%s,%s,%s,%s,%s,'all','email',%s)""",
                    (message_id, thread_id, sender_user_id, from_email,
                     text, original_lang, email_message_id),
                )
                cur.execute(
                    "SELECT created_at FROM chat_message WHERE message_id=%s", (message_id,)
                )
                created = cur.fetchone() or {}
            conn.commit()
        finally:
            conn.close()

        # Remember the sender's Message-ID so our next outbound threads onto it
        # via In-Reply-To (email_message_id is the RFC id for both providers).
        _update_last_msgid(thread_id, email_message_id)

        message = {
            "message_id": message_id,
            "thread_id": thread_id,
            "sender_user_id": sender_user_id,
            "sender_email": from_email,
            "original_text": text,
            "original_lang": original_lang,
            "visibility": "all",
            "source": "email",
            "created_at": created.get("created_at"),
        }
        push_new_message(thread_id, message)
        logger.info("email bridge: ingested inbound reply into thread %s", thread_id)
        return message
    except Exception as exc:
        logger.warning("email bridge ingest failed: %s", exc)
        return None


def deliver_for_thread(thread_id: str, message: dict, sender_user_id: str) -> int:
    """Convenience wrapper: load the thread then deliver. Never raises."""
    try:
        thread = get_thread(thread_id)
    except Exception:
        return 0
    return deliver_message(thread, message, sender_user_id)
