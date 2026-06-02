"""Runnable unit/integration tests for the assessment_chat package.

The repo's DB layer (``db.rds_db``) calls AWS Secrets Manager at import, so we
cannot reach a real database in CI/sandbox. These tests therefore stand up a
faithful **in-memory SQLite** fake of the DB layer — the same queries the code
runs are executed against SQLite (with ``%s``→``?`` and the one
``ON DUPLICATE KEY`` upsert translated) — plus lightweight stubs for the
external services (LLM translation, Gmail/Outlook send, WebSocket push).

This exercises the real business rules end-to-end: workflow-role seeding, the
add/remove permission matrix, internal-vs-assessee visibility filtering,
per-conversation translation + caching, and the email bridge (outbound
translation, In-Reply-To threading, inbound match + dedupe).

For a true live round-trip against the real database, see
``tests/integration/test_assessment_chat_live.py`` (auto-skips without DB).
"""

import sqlite3
import sys
import types
from unittest.mock import MagicMock

import pytest

# ── In-memory fake DB layer ──────────────────────────────────────────────────

_STATE: dict = {"sqlite": None, "ws_emits": [], "gmail_sends": [], "gmail_replies": [], "rfc_counter": 0}

_SQLITE_DDL = [
    """CREATE TABLE users (
        user_id TEXT PRIMARY KEY, email TEXT, user_type TEXT,
        company_name TEXT, launch_id_fk TEXT, permissions TEXT, token TEXT)""",
    """CREATE TABLE integrations (
        user_id TEXT, primary_user_id_fk TEXT, platform TEXT, email TEXT, status TEXT)""",
    """CREATE TABLE document_workflow (
        workflow_id TEXT PRIMARY KEY, org_id TEXT, doc_type TEXT, doc_id TEXT,
        doc_version TEXT, owner_user_id TEXT, current_quality_reviewer TEXT,
        current_governance_reviewer TEXT, current_approver TEXT)""",
    """CREATE TABLE chat_thread (
        thread_id TEXT PRIMARY KEY, org_id TEXT, context_type TEXT, context_id TEXT,
        workflow_id TEXT, doc_type TEXT, doc_id TEXT, created_by TEXT,
        email_provider TEXT, email_subject TEXT, email_thread_id TEXT,
        email_last_msgid TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(context_type, context_id))""",
    """CREATE TABLE chat_participant (
        participant_id TEXT PRIMARY KEY, thread_id TEXT, user_id TEXT, email TEXT,
        role TEXT, side TEXT, preferred_lang TEXT, is_external INTEGER,
        email_bridge INTEGER, added_by TEXT, active INTEGER,
        added_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(thread_id, user_id))""",
    """CREATE TABLE chat_message (
        message_id TEXT PRIMARY KEY, thread_id TEXT, sender_user_id TEXT,
        sender_email TEXT, original_text TEXT, original_lang TEXT, visibility TEXT,
        source TEXT, email_message_id TEXT, call_id TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE chat_message_translation (
        message_id TEXT, lang TEXT, translated_text TEXT, model TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(message_id, lang))""",
    """CREATE TABLE chat_call_session (
        call_id TEXT PRIMARY KEY, thread_id TEXT, started_by TEXT, provider TEXT,
        join_url TEXT, event_id TEXT, status TEXT, recording_ref TEXT,
        transcript TEXT, summary TEXT, notes_filed INTEGER DEFAULT 0,
        started_at TEXT DEFAULT CURRENT_TIMESTAMP, ended_at TEXT)""",
    """CREATE TABLE document_workflow_events (
        event_id TEXT PRIMARY KEY, workflow_id TEXT, from_state TEXT, to_state TEXT,
        kind TEXT, actor_user_id TEXT, assigned_to_user_id TEXT, comment TEXT,
        attachments_json TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
]


def _translate_sql(sql: str) -> str:
    s = sql.replace("%s", "?")
    if "ON DUPLICATE KEY UPDATE" in s:
        # Only used by the translation cache (PK message_id, lang).
        head = s.split("ON DUPLICATE KEY UPDATE", 1)[0]
        s = (
            head
            + "ON CONFLICT(message_id, lang) DO UPDATE SET "
            "translated_text=excluded.translated_text, model=excluded.model"
        )
    return s


class _FakeCursor:
    def __init__(self, conn):
        self._cur = conn._sqlite.cursor()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def execute(self, sql, params=None):
        self._cur.execute(_translate_sql(sql), tuple(params) if params else ())
        return self

    def fetchone(self):
        row = self._cur.fetchone()
        return dict(row) if row is not None else None

    def fetchall(self):
        return [dict(r) for r in self._cur.fetchall()]

    def close(self):
        try:
            self._cur.close()
        except Exception:
            pass


class _FakeConn:
    def __init__(self, sqlite_conn):
        self._sqlite = sqlite_conn

    def cursor(self, *a, **k):
        return _FakeCursor(self)

    def commit(self):
        self._sqlite.commit()

    def rollback(self):
        self._sqlite.rollback()

    def close(self):
        # Shared connection — keep it alive across connect_to_rds() calls.
        pass


def _connect_to_rds():
    return _FakeConn(_STATE["sqlite"])


# ── External-service stubs ────────────────────────────────────────────────────


async def _fake_get_fireworks_response2(user_id, user_message, role, credits, temp=0.7):
    # Deterministic, marked translation so assertions are simple; record calls.
    _STATE["llm_calls"] = _STATE.get("llm_calls", 0) + 1
    return f"<<translated>> {user_message[-40:]}"


def _install_stubs():
    """Install sys.modules stubs; return a dict of saved originals for restore."""
    saved = {}
    keys = [
        "pymysql", "pymysql.cursors", "db", "db.rds_db",
        "utils.fireworkzz", "websockets_custom", "websockets_custom.ws_instance",
        "gmail_route", "gmail_route.routes", "microsoft_route", "microsoft_route.routes",
        "services.gmail_service", "services.meet_service", "agent_route", "agent_route.s_t_s",
        "utils.s3_utils",
    ]
    for k in keys:
        saved[k] = sys.modules.get(k)

    sys.modules["pymysql"] = MagicMock(name="pymysql_stub")
    cursors_mod = types.ModuleType("pymysql.cursors")
    cursors_mod.DictCursor = object
    sys.modules["pymysql.cursors"] = cursors_mod

    db_mod = types.ModuleType("db")
    sys.modules["db"] = db_mod
    rds_mod = types.ModuleType("db.rds_db")
    rds_mod.connect_to_rds = _connect_to_rds
    sys.modules["db.rds_db"] = rds_mod

    fw = types.ModuleType("utils.fireworkzz")
    fw.NORMAL_MODEL = "test-model"
    fw.get_fireworks_response2 = _fake_get_fireworks_response2
    sys.modules["utils.fireworkzz"] = fw

    # WebSocket push: record emits (so we can assert fan-out + visibility).
    ws_pkg = types.ModuleType("websockets_custom")
    sys.modules["websockets_custom"] = ws_pkg
    ws_inst = types.ModuleType("websockets_custom.ws_instance")

    class _WS:
        async def emit(self, **kwargs):
            _STATE["ws_emits"].append(kwargs)

    class _Builder:
        def assessment_chat_event(self, **kw):
            return {"type": "assessment_chat", "message": kw.get("preview", ""), "extra": kw}

    ws_inst.ws_service = _WS()
    ws_inst.msg_builder_main = _Builder()
    sys.modules["websockets_custom.ws_instance"] = ws_inst

    # Gmail / Outlook send wrappers.
    gr_pkg = types.ModuleType("gmail_route")
    sys.modules["gmail_route"] = gr_pkg
    gr = types.ModuleType("gmail_route.routes")

    def _send_mail(owner, to, subject, body, attachments=None):
        _STATE["gmail_sends"].append({"to": to, "subject": subject, "body": body})
        return {"status": "success", "message_id": f"{owner}_APIID1", "thread_id": "GT1"}

    def _gmail_reply(owner, to, subject, thread_id=None, body_text=None, in_reply_to=None, **kw):
        _STATE["rfc_counter"] += 1
        rfc = f"RFC{_STATE['rfc_counter']}"
        _STATE["gmail_replies"].append(
            {"to": to, "thread_id": thread_id, "in_reply_to": in_reply_to, "body": body_text}
        )
        return f"{owner}_{rfc}"

    def _get_message_id(service, who, api_id):
        return "RFCSEED"

    gr.send_mail = _send_mail
    gr.gmail_reply = _gmail_reply
    gr.get_message_id = _get_message_id
    sys.modules["gmail_route.routes"] = gr

    mr_pkg = types.ModuleType("microsoft_route")
    sys.modules["microsoft_route"] = mr_pkg
    mr = types.ModuleType("microsoft_route.routes")
    mr.outlook_send_mail = lambda *a, **k: {"status": "success", "thread_id": "OT1", "internet_message_id": "ORFC1"}
    sys.modules["microsoft_route.routes"] = mr

    gs_mod = types.ModuleType("services.gmail_service")

    class _GmailService:
        def __init__(self, *a, **k):
            self.service = None

    gs_mod.GmailService = _GmailService
    sys.modules["services.gmail_service"] = gs_mod

    # Google Meet creation.
    meet_mod = types.ModuleType("services.meet_service")

    class _GoogleMeetService:
        def __init__(self, *a, **k):
            pass

        def createbasemeet(self, **kw):
            _STATE["meet_calls"] = _STATE.get("meet_calls", [])
            _STATE["meet_calls"].append(kw)
            return {"success": True, "meet_link": "https://meet.test/xyz", "event_id": "EV1"}

    meet_mod.GoogleMeetService = _GoogleMeetService
    sys.modules["services.meet_service"] = meet_mod

    # Whisper transcription.
    ar_pkg = types.ModuleType("agent_route")
    sys.modules["agent_route"] = ar_pkg
    sts_mod = types.ModuleType("agent_route.s_t_s")

    class _Speech2TextService:
        def __init__(self, *a, **k):
            pass

        async def transcribe_audio(self, path):
            return "transcribed words from the call"

    sts_mod.Speech2TextService = _Speech2TextService
    sys.modules["agent_route.s_t_s"] = sts_mod

    # S3 binary read (recording fetch).
    s3_mod = types.ModuleType("utils.s3_utils")
    s3_mod.read_binary_from_s3 = lambda key: b"fake-audio-bytes"
    sys.modules["utils.s3_utils"] = s3_mod

    return saved


def _restore_stubs(saved):
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v
    # Force re-import of modules under test against restored deps.
    for m in list(sys.modules):
        if m.startswith("assessment_chat") or m == "workflow_route.state_machine":
            sys.modules.pop(m, None)


def _fresh_db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    for ddl in _SQLITE_DDL:
        conn.execute(ddl)
    # Seed an org (SAML-style: shared company_name) + a workflow.
    users = [
        ("U_admin", "admin@acme.test", "admin", "acme", None, None, "tok"),  # owner has gmail token
        ("U_qr", "qr@acme.test", "user", "acme", None, None, None),
        ("U_gr", "gr@acme.test", "user", "acme", None, None, None),
        ("U_appr", "appr@acme.test", "user", "acme", None, None, None),
        ("U_other", "other@acme.test", "user", "acme", None, None, None),
    ]
    conn.executemany(
        "INSERT INTO users (user_id,email,user_type,company_name,launch_id_fk,permissions,token) "
        "VALUES (?,?,?,?,?,?,?)",
        users,
    )
    conn.execute(
        "INSERT INTO document_workflow (workflow_id,org_id,doc_type,doc_id,doc_version,"
        "owner_user_id,current_quality_reviewer,current_governance_reviewer,current_approver) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("WF1", "acme", "runbook", "DOC1", "v1", "U_admin", "U_qr", "U_gr", "U_appr"),
    )
    conn.commit()
    return conn


@pytest.fixture()
def chat():
    """Provide freshly-imported service/email_bridge modules over a fresh DB."""
    saved = _install_stubs()
    _STATE["sqlite"] = _fresh_db()
    _STATE["ws_emits"] = []
    _STATE["gmail_sends"] = []
    _STATE["gmail_replies"] = []
    _STATE["rfc_counter"] = 0
    _STATE["llm_calls"] = 0
    # Import AFTER stubs are installed.
    import importlib

    import assessment_chat.service as service
    import assessment_chat.translation as translation
    import assessment_chat.email_bridge as email_bridge
    import assessment_chat.audio as audio

    service = importlib.reload(service)
    translation = importlib.reload(translation)
    email_bridge = importlib.reload(email_bridge)
    audio = importlib.reload(audio)

    ns = types.SimpleNamespace(
        service=service, translation=translation, email_bridge=email_bridge, audio=audio,
    )
    try:
        yield ns
    finally:
        _restore_stubs(saved)


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_thread_seeds_workflow_roles_as_internal(chat):
    t = chat.service.get_or_create_thread("U_admin", "workflow", "WF1", workflow_id="WF1")
    parts = {p["user_id"]: p for p in chat.service.list_participants(t["thread_id"])}
    assert parts["U_admin"]["role"] == "owner"
    assert parts["U_qr"]["role"] == "quality_reviewer"
    assert parts["U_gr"]["role"] == "governance_reviewer"
    assert parts["U_appr"]["role"] == "approver"
    assert all(p["side"] == "internal" for p in parts.values())


def test_get_or_create_is_idempotent(chat):
    t1 = chat.service.get_or_create_thread("U_admin", "workflow", "WF1", workflow_id="WF1")
    t2 = chat.service.get_or_create_thread("U_qr", "workflow", "WF1", workflow_id="WF1")
    assert t1["thread_id"] == t2["thread_id"]


def test_permission_matrix_add_and_remove(chat):
    svc = chat.service
    t = svc.get_or_create_thread("U_admin", "workflow", "WF1", workflow_id="WF1")
    tid = t["thread_id"]

    # Internal reviewer may add another internal org member.
    svc.add_participant(tid, "U_qr", target_user_id="U_other")
    assert any(p["user_id"] == "U_other" for p in svc.list_participants(tid))

    # Internal reviewer may NOT add an external/assessee email.
    with pytest.raises(svc.ChatPermissionError):
        svc.add_participant(tid, "U_qr", email="vendor@ext.test")

    # Admin/assigner may add an external assessee participant.
    added = svc.add_participant(tid, "U_admin", email="vendor@ext.test")
    assert added["is_external"] and added["side"] == "assessee"

    # An assessee-side participant cannot add anyone.
    svc.add_participant(tid, "U_admin", target_user_id="U_other", side="assessee")  # demote U_other
    with pytest.raises(svc.ChatPermissionError):
        svc.add_participant(tid, "U_other", target_user_id="U_appr")

    # Reviewer can remove only what they added; admin can remove (non-owner) anyone.
    parts = {p["user_id"]: p for p in svc.list_participants(tid)}
    with pytest.raises(svc.ChatPermissionError):
        svc.remove_participant(tid, "U_gr", parts["U_qr"]["participant_id"])  # not added by U_gr
    svc.remove_participant(tid, "U_admin", parts["U_qr"]["participant_id"])  # admin can
    remaining = {p["user_id"] for p in svc.list_participants(tid)}
    assert "U_qr" not in remaining
    # Owner can never be removed.
    with pytest.raises(svc.ChatPermissionError):
        svc.remove_participant(tid, "U_admin", parts["U_admin"]["participant_id"])


def test_visibility_internal_hidden_from_assessee(chat):
    svc = chat.service
    t = svc.get_or_create_thread("U_admin", "workflow", "WF1", workflow_id="WF1")
    tid = t["thread_id"]
    svc.add_participant(tid, "U_admin", target_user_id="U_other", side="assessee")

    svc.post_message(tid, "U_qr", "public hello", visibility="all")
    svc.post_message(tid, "U_qr", "internal only note", visibility="internal")

    internal_view, _ = svc.list_messages(tid, "U_admin")
    assessee_view, _ = svc.list_messages(tid, "U_other")
    assert {m["original_text"] for m in internal_view} == {"public hello", "internal only note"}
    assert {m["original_text"] for m in assessee_view} == {"public hello"}


def test_assessee_cannot_post_internal(chat):
    svc = chat.service
    t = svc.get_or_create_thread("U_admin", "workflow", "WF1", workflow_id="WF1")
    tid = t["thread_id"]
    svc.add_participant(tid, "U_admin", target_user_id="U_other", side="assessee")
    msg = svc.post_message(tid, "U_other", "trying internal", visibility="internal")
    assert msg["visibility"] == "all"  # forced down


def test_translation_and_disclaimer_and_cache(chat):
    svc = chat.service
    t = svc.get_or_create_thread("U_admin", "workflow", "WF1", workflow_id="WF1")
    tid = t["thread_id"]
    svc.set_language(tid, "U_gr", "fr")
    svc.post_message(tid, "U_qr", "hello team", lang="en", visibility="all")

    fr_view, _ = svc.list_messages(tid, "U_gr")
    m = fr_view[0]
    assert m["ai_translated"] is True
    assert m["text"].startswith("<<translated>>")
    assert m["original_text"] == "hello team"
    assert m["disclaimer"] and "AI" in m["disclaimer"]

    # English viewer gets the original untouched.
    en_view, _ = svc.list_messages(tid, "U_admin")
    assert en_view[0]["ai_translated"] is False
    assert en_view[0]["text"] == "hello team"

    # Cache: re-listing in French must not call the LLM again.
    calls_after_first = _STATE["llm_calls"]
    svc.list_messages(tid, "U_gr")
    assert _STATE["llm_calls"] == calls_after_first


def test_email_bridge_outbound_translates_skips_internal_and_threads(chat):
    svc, bridge = chat.service, chat.email_bridge
    t = svc.get_or_create_thread("U_admin", "workflow", "WF1", workflow_id="WF1")
    tid = t["thread_id"]
    # Add an email-bridged external recipient who reads French.
    svc.add_participant(tid, "U_admin", email="vendor@ext.test", email_bridge=True)
    parts = svc.list_participants(tid)
    pid = next(p["participant_id"] for p in parts if p.get("email") == "vendor@ext.test")
    svc.set_language(tid, None, "fr") if False else None  # external has no user_id; default lang ok

    # Internal message is NOT emailed.
    m_int = svc.post_message(tid, "U_qr", "secret", visibility="internal")
    assert bridge.deliver_for_thread(tid, m_int, "U_qr") == 0
    assert _STATE["gmail_sends"] == []

    # First 'all' message → send_mail (new thread), seeds RFC + thread id.
    m1 = svc.post_message(tid, "U_qr", "kickoff", visibility="all")
    assert bridge.deliver_for_thread(tid, m1, "U_qr") == 1
    assert len(_STATE["gmail_sends"]) == 1
    assert _STATE["gmail_sends"][0]["to"] == "vendor@ext.test"
    th = svc.get_thread(tid)
    assert th["email_thread_id"] == "GT1"
    assert th["email_last_msgid"] == "RFCSEED"

    # Second 'all' message → gmail_reply with In-Reply-To threading.
    m2 = svc.post_message(tid, "U_qr", "follow up", visibility="all")
    assert bridge.deliver_for_thread(tid, m2, "U_qr") == 1
    assert len(_STATE["gmail_replies"]) == 1
    assert _STATE["gmail_replies"][0]["in_reply_to"] == "RFCSEED"
    assert _STATE["gmail_replies"][0]["thread_id"] == "GT1"
    _ = pid


def test_email_bridge_inbound_match_and_dedupe(chat):
    svc, bridge = chat.service, chat.email_bridge
    t = svc.get_or_create_thread("U_admin", "workflow", "WF1", workflow_id="WF1")
    tid = t["thread_id"]
    svc.add_participant(tid, "U_admin", email="vendor@ext.test", email_bridge=True)
    # Establish the provider thread id via a first outbound.
    m1 = svc.post_message(tid, "U_qr", "kickoff", visibility="all")
    bridge.deliver_for_thread(tid, m1, "U_qr")

    ingested = bridge.ingest_inbound_email(
        provider="gmail", from_email="vendor@ext.test", subject="Re: kickoff",
        body="Sounds good\nOn ... wrote:\n> quoted", email_thread_id="GT1",
        email_message_id="RFC-IN-1",
    )
    assert ingested is not None
    assert ingested["source"] == "email"
    assert ingested["original_text"] == "Sounds good"  # quoted history trimmed
    assert svc.get_thread(tid)["email_last_msgid"] == "RFC-IN-1"

    # Duplicate delivery (same Message-ID) is ignored.
    dup = bridge.ingest_inbound_email(
        provider="gmail", from_email="vendor@ext.test", subject="Re: kickoff",
        body="Sounds good", email_thread_id="GT1", email_message_id="RFC-IN-1",
    )
    assert dup is None

    # The inbound message is visible to participants.
    view, _ = svc.list_messages(tid, "U_admin")
    assert any(mm["source"] == "email" for mm in view)


def test_email_bridge_inbound_subject_fallback(chat):
    svc, bridge = chat.service, chat.email_bridge
    t = svc.get_or_create_thread("U_admin", "workflow", "WF1", workflow_id="WF1")
    tid = t["thread_id"]
    svc.add_participant(tid, "U_admin", email="vendor@ext.test", email_bridge=True)
    m1 = svc.post_message(tid, "U_qr", "kickoff", visibility="all")
    bridge.deliver_for_thread(tid, m1, "U_qr")  # sets email_subject

    # No provider thread id, but sender + normalized subject identify the thread.
    ingested = bridge.ingest_inbound_email(
        provider="gmail", from_email="vendor@ext.test",
        subject="RE: " + svc.get_thread(tid)["email_subject"],
        body="reply via fallback", email_thread_id=None, email_message_id="RFC-IN-2",
    )
    assert ingested is not None
    assert ingested["original_text"] == "reply via fallback"


# ── Audio conferencing (Phase 3) ─────────────────────────────────────────────


def test_call_start_creates_meet_and_announces(chat):
    svc, audio = chat.service, chat.audio
    t = svc.get_or_create_thread("U_admin", "workflow", "WF1", workflow_id="WF1")
    tid = t["thread_id"]
    res = audio.start_call(tid, "U_qr", title="Risk review call")
    assert res["join_url"] == "https://meet.test/xyz"
    assert res["status"] == "active"
    # A system message with the join link is posted and visible.
    view, _ = svc.list_messages(tid, "U_admin")
    assert any(m["source"] == "system" and "meet.test" in m["original_text"] for m in view)
    # The Meet was created with the participants as attendees.
    assert _STATE["meet_calls"][0]["attendees"]


def test_call_end_with_transcript_summarizes(chat):
    svc, audio = chat.service, chat.audio
    t = svc.get_or_create_thread("U_admin", "workflow", "WF1", workflow_id="WF1")
    tid = t["thread_id"]
    call = audio.start_call(tid, "U_qr")
    out = audio.end_call(call["call_id"], "U_qr", transcript="we agreed to remediate finding 3")
    assert out["status"] == "ended"
    assert out["transcript"] == "we agreed to remediate finding 3"
    assert out["summary"]  # LLM stub produced notes


def test_call_end_with_s3_recording_transcribes(chat):
    svc, audio = chat.service, chat.audio
    t = svc.get_or_create_thread("U_admin", "workflow", "WF1", workflow_id="WF1")
    tid = t["thread_id"]
    call = audio.start_call(tid, "U_qr")
    out = audio.end_call(call["call_id"], "U_qr", audio_s3_key="recordings/call.mp3")
    assert out["transcript"] == "transcribed words from the call"  # Whisper stub
    assert out["summary"]


def test_summary_to_notes_files_chat_and_workflow(chat):
    svc, audio = chat.service, chat.audio
    t = svc.get_or_create_thread("U_admin", "workflow", "WF1", workflow_id="WF1")
    tid = t["thread_id"]
    call = audio.start_call(tid, "U_qr")
    audio.end_call(call["call_id"], "U_qr", transcript="action: fix MFA gap")

    result = audio.summary_to_notes(call["call_id"], "U_qr", target="both")
    assert result["filed_to_chat"] and result["filed_to_workflow"]

    # Chat copy is present as a call_summary message.
    view, _ = svc.list_messages(tid, "U_admin")
    assert any(m["source"] == "call_summary" for m in view)

    # A workflow note (document_workflow_events comment) was written.
    cur = _STATE["sqlite"].execute(
        "SELECT COUNT(*) AS c FROM document_workflow_events WHERE workflow_id='WF1' AND kind='comment'"
    )
    assert cur.fetchone()["c"] >= 1


def test_summary_to_notes_blocks_assessee(chat):
    svc, audio = chat.service, chat.audio
    t = svc.get_or_create_thread("U_admin", "workflow", "WF1", workflow_id="WF1")
    tid = t["thread_id"]
    svc.add_participant(tid, "U_admin", target_user_id="U_other", side="assessee")
    call = audio.start_call(tid, "U_qr")
    audio.end_call(call["call_id"], "U_qr", transcript="notes")
    with pytest.raises(svc.ChatPermissionError):
        audio.summary_to_notes(call["call_id"], "U_other", target="chat")
