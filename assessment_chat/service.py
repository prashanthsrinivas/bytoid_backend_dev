"""Assessment chat — core service logic.

Threads are anchored to a review workflow (``context_type='workflow'``,
``context_id=<workflow_id>``) and seed their participants from the
``document_workflow`` role columns (owner / quality / governance / approver).

Two cross-cutting rules are enforced here, not at the route layer, so every
caller path obeys them:

  * **Visibility** — a message with ``visibility='internal'`` is invisible to
    any participant whose ``side='assessee'`` (read path filters it; senders on
    the assessee side cannot create internal messages).
  * **Add/remove permissions** —
        admin/assigner (owner): add internal + assessee/external, remove anyone
        quality/governance reviewer, approver (internal): add internal only,
                                                          remove only what they added
        assessee participant: cannot add or remove, cannot post internal
"""

import uuid

import pymysql.cursors

from db.rds_db import connect_to_rds
from utils.base_logger import get_logger
from workflow_route.state_machine import get_user_org_id

from assessment_chat.schema import (
    DEFAULT_LANG,
    SIDE_ASSESSEE,
    SIDE_INTERNAL,
    SUPPORTED_LANGS,
    VISIBILITY_ALL,
    VISIBILITY_INTERNAL,
)
from assessment_chat.translation import normalize_lang, translate_message

logger = get_logger(__name__)


# ── Errors (routes map these to HTTP status codes) ────────────────────────────


class ChatError(Exception):
    status = 400


class ChatNotFoundError(ChatError):
    status = 404


class ChatPermissionError(ChatError):
    status = 403


class ChatValidationError(ChatError):
    status = 400


# ── Role / side classification ────────────────────────────────────────────────

# document_workflow role columns → (chat role, side). All workflow parties are
# internal staff; the assessee/vendor is never a workflow column and is added
# explicitly with side='assessee'.
_WORKFLOW_ROLE_COLS = [
    ("owner_user_id", "owner"),
    ("current_quality_reviewer", "quality_reviewer"),
    ("current_governance_reviewer", "governance_reviewer"),
    ("current_approver", "approver"),
]

# Roles that carry admin/assigner power on the thread (can add assessee/external
# participants and remove anyone).
_ADMIN_ROLES = {"owner", "admin"}


# ── User / org helpers ─────────────────────────────────────────────────────────


def _user_row(user_id: str) -> dict | None:
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT user_id, email, user_type, company_name, launch_id_fk, permissions "
                "FROM users WHERE user_id=%s LIMIT 1",
                (user_id,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def list_org_users(user_id: str) -> dict[str, dict]:
    """Return {user_id: {user_id, email, user_type}} for every member of the
    caller's org — including SAML/SSO-provisioned users.

    Mirrors ``workflow_route.routes.get_assignable_users``: SAML orgs share a
    ``company_name``; non-SAML orgs track membership via the root admin's
    ``permissions.shared``/``permissions.invites`` lists. The caller (and the
    resolved root admin) are always included.
    """
    import json

    caller = _user_row(user_id)
    if not caller:
        return {}

    out: dict[str, dict] = {}

    def _add(uid, email, utype):
        if uid and uid not in out:
            out[uid] = {"user_id": uid, "email": email, "user_type": utype}

    company_name = (caller.get("company_name") or "").strip()
    launch_id = (caller.get("launch_id_fk") or "").strip()

    conn = connect_to_rds()
    try:
        if company_name:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(
                    "SELECT user_id, email, user_type FROM users WHERE company_name=%s",
                    (company_name,),
                )
                for r in cur.fetchall():
                    _add(r["user_id"], r.get("email"), r.get("user_type"))
        else:
            admin_id = launch_id
            if not admin_id and caller.get("user_type") != "admin":
                try:
                    cperms = json.loads(caller["permissions"]) if caller.get("permissions") else {}
                    invited_by = cperms.get("invited_by", "")
                    if invited_by:
                        with conn.cursor(pymysql.cursors.DictCursor) as cur:
                            cur.execute(
                                "SELECT user_id FROM users WHERE email=%s AND user_type='admin' LIMIT 1",
                                (invited_by,),
                            )
                            ref = cur.fetchone()
                            if ref:
                                admin_id = ref["user_id"]
                except (json.JSONDecodeError, TypeError):
                    pass
            if not admin_id:
                admin_id = user_id

            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(
                    "SELECT user_id, email, user_type, permissions FROM users WHERE user_id=%s LIMIT 1",
                    (admin_id,),
                )
                admin_row = cur.fetchone()

            admin_perms = {}
            if admin_row and admin_row.get("permissions"):
                try:
                    admin_perms = json.loads(admin_row["permissions"])
                    if not isinstance(admin_perms, dict):
                        admin_perms = {}
                except (ValueError, TypeError):
                    admin_perms = {}

            email_set = set()
            for entry in admin_perms.get("shared", []):
                if entry.get("email") and entry.get("status") != "revoked":
                    email_set.add(entry["email"].lower())
            for entry in admin_perms.get("invites", []):
                if entry.get("email") and entry.get("status") not in ("revoked", "pending"):
                    email_set.add(entry["email"].lower())

            if email_set:
                with conn.cursor(pymysql.cursors.DictCursor) as cur:
                    placeholders = ",".join(["%s"] * len(email_set))
                    cur.execute(
                        f"SELECT user_id, email, user_type FROM users WHERE email IN ({placeholders})",
                        tuple(email_set),
                    )
                    for r in cur.fetchall():
                        _add(r["user_id"], r.get("email"), r.get("user_type"))

            if admin_row and admin_row.get("user_type") == "admin":
                _add(admin_row["user_id"], admin_row.get("email"), "admin")
    finally:
        conn.close()

    # Always include the caller.
    _add(caller["user_id"], caller.get("email"), caller.get("user_type"))
    return out


def _emails_for(user_ids: set[str]) -> dict[str, str]:
    ids = {u for u in user_ids if u}
    if not ids:
        return {}
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            placeholders = ",".join(["%s"] * len(ids))
            cur.execute(
                f"SELECT user_id, email FROM users WHERE user_id IN ({placeholders})",
                tuple(ids),
            )
            return {r["user_id"]: r.get("email") for r in cur.fetchall()}
    finally:
        conn.close()


# ── Thread + participant reads ──────────────────────────────────────────────────


def get_thread(thread_id: str) -> dict:
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT * FROM chat_thread WHERE thread_id=%s", (thread_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        raise ChatNotFoundError("thread not found")
    return dict(row)


def get_participant(thread_id: str, user_id: str) -> dict | None:
    """Return the active participant row for ``user_id`` on the thread, or None."""
    if not user_id:
        return None
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM chat_participant "
                "WHERE thread_id=%s AND user_id=%s AND active=1 LIMIT 1",
                (thread_id, user_id),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def _require_participant(thread_id: str, user_id: str) -> dict:
    p = get_participant(thread_id, user_id)
    if not p:
        raise ChatPermissionError("you are not a participant of this conversation")
    return p


def list_participants(thread_id: str, include_inactive: bool = False) -> list[dict]:
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            q = "SELECT * FROM chat_participant WHERE thread_id=%s"
            if not include_inactive:
                q += " AND active=1"
            q += " ORDER BY added_at ASC"
            cur.execute(q, (thread_id,))
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return rows


# ── Thread creation + seeding ────────────────────────────────────────────────────


def _workflow_row(workflow_id: str) -> dict | None:
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM document_workflow WHERE workflow_id=%s", (workflow_id,)
            )
            row = cur.fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def _insert_participant(
    cur,
    thread_id: str,
    *,
    user_id: str | None,
    email: str | None,
    role: str,
    side: str,
    added_by: str | None,
    is_external: bool = False,
    email_bridge: bool = False,
) -> str:
    """Insert a participant if not already present; return participant_id.

    Re-activates a previously removed member rather than duplicating. user_id
    members dedupe on (thread_id, user_id); external members dedupe on
    (thread_id, lower(email)).
    """
    if user_id:
        cur.execute(
            "SELECT participant_id FROM chat_participant WHERE thread_id=%s AND user_id=%s",
            (thread_id, user_id),
        )
    else:
        cur.execute(
            "SELECT participant_id FROM chat_participant "
            "WHERE thread_id=%s AND user_id IS NULL AND LOWER(email)=LOWER(%s)",
            (thread_id, email or ""),
        )
    existing = cur.fetchone()
    if existing:
        pid = existing["participant_id"] if isinstance(existing, dict) else existing[0]
        cur.execute(
            "UPDATE chat_participant SET active=1, role=%s, side=%s, "
            "is_external=%s, email_bridge=%s WHERE participant_id=%s",
            (role, side, 1 if is_external else 0, 1 if email_bridge else 0, pid),
        )
        return pid

    pid = str(uuid.uuid4())
    cur.execute(
        """INSERT INTO chat_participant
           (participant_id, thread_id, user_id, email, role, side,
            preferred_lang, is_external, email_bridge, added_by, active)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1)""",
        (
            pid, thread_id, user_id, email, role, side,
            DEFAULT_LANG, 1 if is_external else 0, 1 if email_bridge else 0, added_by,
        ),
    )
    return pid


def get_or_create_thread(
    user_id: str,
    context_type: str,
    context_id: str,
    *,
    workflow_id: str | None = None,
    doc_type: str | None = None,
    doc_id: str | None = None,
    email_subject: str | None = None,
) -> dict:
    """Fetch the thread for a context, creating + seeding it on first access.

    The caller must be a member of the workflow's org (or a workflow party).
    On creation the workflow's owner/reviewers/approver are seeded as internal
    participants; the caller is added if not already among them.
    """
    if context_type != "workflow":
        raise ChatValidationError("only context_type='workflow' is supported")
    wf_id = workflow_id or context_id
    if not wf_id:
        raise ChatValidationError("workflow_id (context_id) is required")

    # Existing thread → ensure caller may see it, then return.
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM chat_thread WHERE context_type=%s AND context_id=%s",
                (context_type, context_id),
            )
            existing = cur.fetchone()
    finally:
        conn.close()

    wf = _workflow_row(wf_id)
    if not wf:
        raise ChatNotFoundError("workflow not found")

    org_id = wf.get("org_id") or get_user_org_id(user_id)
    org_users = list_org_users(user_id)
    workflow_party_ids = {wf.get(col) for col, _ in _WORKFLOW_ROLE_COLS if wf.get(col)}
    caller_is_member = user_id in org_users or user_id in workflow_party_ids
    if not caller_is_member:
        raise ChatPermissionError("you do not have access to this assessment")

    if existing:
        thread = dict(existing)
        # Make sure an authorized caller who isn't yet a participant gets a seat.
        if not get_participant(thread["thread_id"], user_id):
            _ensure_caller_participant(thread["thread_id"], user_id, wf, org_users)
        return thread

    # Create + seed.
    thread_id = str(uuid.uuid4())
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                """INSERT INTO chat_thread
                   (thread_id, org_id, context_type, context_id, workflow_id,
                    doc_type, doc_id, created_by, email_subject)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    thread_id, org_id, context_type, context_id, wf_id,
                    doc_type or wf.get("doc_type"), doc_id or wf.get("doc_id"),
                    user_id, email_subject,
                ),
            )
            # Seed workflow parties (internal). First role per user wins.
            seeded: set[str] = set()
            for col, role in _WORKFLOW_ROLE_COLS:
                uid = wf.get(col)
                if uid and uid not in seeded:
                    seeded.add(uid)
                    _insert_participant(
                        cur, thread_id,
                        user_id=uid, email=org_users.get(uid, {}).get("email"),
                        role=role, side=SIDE_INTERNAL, added_by=user_id,
                    )
            # Caller, if not already seeded.
            if user_id not in seeded:
                _insert_participant(
                    cur, thread_id,
                    user_id=user_id, email=org_users.get(user_id, {}).get("email"),
                    role="added", side=SIDE_INTERNAL, added_by=user_id,
                )
        conn.commit()
    finally:
        conn.close()

    return get_thread(thread_id)


def _ensure_caller_participant(thread_id: str, user_id: str, wf: dict, org_users: dict) -> None:
    """Add an authorized caller (workflow party or org member) as a participant."""
    role = "added"
    for col, r in _WORKFLOW_ROLE_COLS:
        if wf.get(col) == user_id:
            role = r
            break
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            _insert_participant(
                cur, thread_id,
                user_id=user_id, email=org_users.get(user_id, {}).get("email"),
                role=role, side=SIDE_INTERNAL, added_by=user_id,
            )
        conn.commit()
    finally:
        conn.close()


# ── Permission helpers ───────────────────────────────────────────────────────────


def _is_admin_actor(participant: dict, org_users: dict, user_id: str) -> bool:
    if participant.get("role") in _ADMIN_ROLES:
        return True
    return org_users.get(user_id, {}).get("user_type") == "admin"


# ── Participant management ───────────────────────────────────────────────────────


def add_participant(
    thread_id: str,
    actor_user_id: str,
    *,
    target_user_id: str | None = None,
    email: str | None = None,
    side: str | None = None,
    role: str = "added",
    email_bridge: bool = False,
) -> dict:
    """Add an org member (by user_id) or an external/assessee member (by email).

    Permission matrix:
      - internal participant  → may add internal org members
      - admin/assigner (owner or user_type=admin) → may also add assessee/external
    """
    actor = _require_participant(thread_id, actor_user_id)
    if actor.get("side") != SIDE_INTERNAL:
        raise ChatPermissionError("assessee participants cannot add members")

    org_users = list_org_users(actor_user_id)
    actor_is_admin = _is_admin_actor(actor, org_users, actor_user_id)

    # Internal org-member add.
    if target_user_id:
        member = org_users.get(target_user_id)
        if not member:
            raise ChatValidationError("target user is not a member of your organization")
        resolved_side = (side or SIDE_INTERNAL)
        if resolved_side == SIDE_ASSESSEE and not actor_is_admin:
            raise ChatPermissionError("only an admin/assigner can add assessee-side participants")
        conn = connect_to_rds()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                pid = _insert_participant(
                    cur, thread_id,
                    user_id=target_user_id, email=member.get("email"),
                    role=role, side=resolved_side, added_by=actor_user_id,
                )
            conn.commit()
        finally:
            conn.close()
        return {"participant_id": pid, "user_id": target_user_id, "email": member.get("email"),
                "side": resolved_side, "role": role}

    # External / assessee add by email — admin/assigner only.
    if email:
        if not actor_is_admin:
            raise ChatPermissionError("only an admin/assigner can add external participants")
        resolved_side = (side or SIDE_ASSESSEE)
        conn = connect_to_rds()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                pid = _insert_participant(
                    cur, thread_id,
                    user_id=None, email=email.strip().lower(),
                    role=role, side=resolved_side, added_by=actor_user_id,
                    is_external=True, email_bridge=email_bridge,
                )
            conn.commit()
        finally:
            conn.close()
        return {"participant_id": pid, "user_id": None, "email": email.strip().lower(),
                "side": resolved_side, "role": role, "is_external": True}

    raise ChatValidationError("provide target_user_id or email")


def remove_participant(thread_id: str, actor_user_id: str, participant_id: str) -> None:
    """Deactivate a participant. Admin/assigner may remove anyone (except the
    owner); others may remove only members they added."""
    actor = _require_participant(thread_id, actor_user_id)
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM chat_participant WHERE participant_id=%s AND thread_id=%s",
                (participant_id, thread_id),
            )
            target = cur.fetchone()
            if not target:
                raise ChatNotFoundError("participant not found")
            if target.get("role") == "owner":
                raise ChatPermissionError("the owner cannot be removed")

            org_users = list_org_users(actor_user_id)
            actor_is_admin = _is_admin_actor(actor, org_users, actor_user_id)
            if not actor_is_admin and target.get("added_by") != actor_user_id:
                raise ChatPermissionError("you can only remove participants you added")

            cur.execute(
                "UPDATE chat_participant SET active=0 WHERE participant_id=%s",
                (participant_id,),
            )
        conn.commit()
    finally:
        conn.close()


def set_language(thread_id: str, user_id: str, lang: str) -> str:
    """Set the caller's per-conversation reading language. Returns stored code."""
    _require_participant(thread_id, user_id)
    code = normalize_lang(lang)
    if code not in SUPPORTED_LANGS:
        raise ChatValidationError(f"language must be one of {SUPPORTED_LANGS}")
    conn = connect_to_rds()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE chat_participant SET preferred_lang=%s "
                "WHERE thread_id=%s AND user_id=%s AND active=1",
                (code, thread_id, user_id),
            )
        conn.commit()
    finally:
        conn.close()
    return code


# ── Messaging ──────────────────────────────────────────────────────────────────


def post_message(
    thread_id: str,
    sender_user_id: str,
    text: str,
    *,
    lang: str | None = None,
    visibility: str = VISIBILITY_ALL,
    source: str = "chat",
) -> dict:
    """Persist a message. Assessee-side senders are forced to 'all' visibility;
    only internal participants may post 'internal' messages."""
    actor = _require_participant(thread_id, sender_user_id)
    body = (text or "").strip()
    if not body:
        raise ChatValidationError("message text is required")

    vis = visibility if visibility in (VISIBILITY_ALL, VISIBILITY_INTERNAL) else VISIBILITY_ALL
    if actor.get("side") != SIDE_INTERNAL:
        vis = VISIBILITY_ALL  # assessee can never post internal-only

    original_lang = normalize_lang(lang or actor.get("preferred_lang") or DEFAULT_LANG)
    message_id = str(uuid.uuid4())
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                """INSERT INTO chat_message
                   (message_id, thread_id, sender_user_id, original_text,
                    original_lang, visibility, source)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (message_id, thread_id, sender_user_id, body, original_lang, vis, source),
            )
            cur.execute(
                "SELECT created_at FROM chat_message WHERE message_id=%s", (message_id,)
            )
            created = cur.fetchone() or {}
        conn.commit()
    finally:
        conn.close()

    return {
        "message_id": message_id,
        "thread_id": thread_id,
        "sender_user_id": sender_user_id,
        "original_text": body,
        "original_lang": original_lang,
        "visibility": vis,
        "source": source,
        "created_at": created.get("created_at"),
    }


def list_messages(
    thread_id: str,
    viewer_user_id: str,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict], int]:
    """Return messages visible to the viewer, translated to their preferred
    language. Assessee-side viewers never receive 'internal' messages."""
    viewer = _require_participant(thread_id, viewer_user_id)
    target_lang = normalize_lang(viewer.get("preferred_lang") or DEFAULT_LANG)
    is_assessee = viewer.get("side") == SIDE_ASSESSEE

    where = "thread_id=%s"
    params: list = [thread_id]
    if is_assessee:
        where += " AND visibility=%s"
        params.append(VISIBILITY_ALL)

    offset = (max(page, 1) - 1) * page_size
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(f"SELECT COUNT(*) AS cnt FROM chat_message WHERE {where}", params)
            total = cur.fetchone()["cnt"]
            cur.execute(
                f"SELECT * FROM chat_message WHERE {where} "
                f"ORDER BY created_at ASC LIMIT %s OFFSET %s",
                (*params, page_size, offset),
            )
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    emails = _emails_for({r.get("sender_user_id") for r in rows})

    out: list[dict] = []
    for r in rows:
        tr = translate_message(
            message_id=r["message_id"],
            original_text=r["original_text"],
            original_lang=r["original_lang"],
            target_lang=target_lang,
            requester_user_id=viewer_user_id,
        )
        out.append({
            "message_id": r["message_id"],
            "thread_id": r["thread_id"],
            "sender_user_id": r.get("sender_user_id"),
            "sender_email": r.get("sender_email") or emails.get(r.get("sender_user_id")),
            "visibility": r.get("visibility"),
            "source": r.get("source"),
            "created_at": r.get("created_at"),
            "text": tr["text"],
            "original_text": tr["original_text"],
            "original_lang": tr["original_lang"],
            "target_lang": tr["target_lang"],
            "ai_translated": tr["ai_translated"],
            "disclaimer": tr["disclaimer"],
        })
    return out, total


# ── Realtime push (shared by routes + email bridge) ──────────────────────────────


def push_new_message(thread_id: str, message: dict) -> None:
    """Best-effort WebSocket fan-out of a new message to in-app participants.

    Visibility is enforced here too: an ``internal`` message is never pushed to
    assessee-side participants, and the sender is skipped. Carries identifiers
    only — clients re-fetch ``/messages`` so per-viewer translation + visibility
    are applied server-side. Never raises.
    """
    try:
        from websockets_custom.ws_instance import msg_builder_main, ws_service
    except Exception:
        return

    visibility = message.get("visibility", VISIBILITY_ALL)
    sender = message.get("sender_user_id")
    try:
        participants = list_participants(thread_id)
    except Exception:
        return

    recipients = []
    for p in participants:
        uid = p.get("user_id")
        if not uid or uid == sender:
            continue
        if visibility == VISIBILITY_INTERNAL and p.get("side") != SIDE_INTERNAL:
            continue
        recipients.append(uid)
    if not recipients:
        return

    evt = msg_builder_main.assessment_chat_event(
        thread_id=thread_id,
        message_id=message.get("message_id"),
        sender_user_id=sender,
        event="message",
        preview=(message.get("original_text") or "")[:120],
    )

    import asyncio

    async def _run():
        for uid in recipients:
            try:
                await ws_service.emit(
                    user_id=uid,
                    message=evt["message"],
                    scope="global",
                    msg_type=evt["type"],
                    feature="assessment_chat",
                    extra=evt["extra"],
                )
            except Exception:
                pass

    try:
        asyncio.run(_run())
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_run())
        finally:
            loop.close()
    except Exception:
        pass
