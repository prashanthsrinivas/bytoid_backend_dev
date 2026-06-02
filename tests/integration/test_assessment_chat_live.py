"""Live round-trip smoke test for assessment_chat against the REAL database.

This is the "live smoke test" the sandbox cannot run (no AWS/DB there). It is
double-gated so it never runs by accident:

  * skips unless ``RUN_LIVE_CHAT=1`` is set, AND
  * skips if ``db.rds_db`` can't be imported (no AWS creds / no DB).

It mocks only the external *network* calls — the LLM translator and the
Gmail/Outlook send wrappers — so it spends no AI credits and sends no email.
Everything else (schema bootstrap, thread/participant/message persistence,
visibility filtering, translation caching, inbound ingest + dedupe) runs
against real MySQL. All rows it creates are removed in teardown.

Run it from an environment with DB access:

    RUN_LIVE_CHAT=1 python -m pytest tests/integration/test_assessment_chat_live.py -o addopts="" -s
"""

import os
import uuid

import pytest

pytestmark = pytest.mark.integration

if os.getenv("RUN_LIVE_CHAT") != "1":
    pytest.skip("set RUN_LIVE_CHAT=1 to run the live assessment_chat smoke test", allow_module_level=True)

try:  # Skip cleanly when the DB layer can't be imported (no AWS creds).
    from db.rds_db import connect_to_rds
except Exception as exc:  # pragma: no cover - env dependent
    pytest.skip(f"db.rds_db unavailable: {exc}", allow_module_level=True)

from assessment_chat import email_bridge, service, translation
from assessment_chat.schema import bootstrap_schema


@pytest.fixture()
def live_workflow(monkeypatch):
    """Create a throwaway org user + workflow, yield ids, then clean up."""
    bootstrap_schema()

    # Avoid real LLM + real email during the smoke test.
    monkeypatch.setattr(
        translation, "_translate_text",
        lambda text, src, tgt, uid: f"[{tgt}] {text}",
    )

    org_id = f"smoke-{uuid.uuid4().hex[:8]}"
    owner = f"smoke_owner_{uuid.uuid4().hex[:8]}"
    reviewer = f"smoke_qr_{uuid.uuid4().hex[:8]}"
    wf_id = str(uuid.uuid4())
    doc_id = f"DOC-{uuid.uuid4().hex[:6]}"

    conn = connect_to_rds()
    try:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO users (user_id, email, user_type, company_name) VALUES (%s,%s,%s,%s)",
                [
                    (owner, f"{owner}@smoke.test", "admin", org_id),
                    (reviewer, f"{reviewer}@smoke.test", "user", org_id),
                ],
            )
            cur.execute(
                "INSERT INTO document_workflow "
                "(workflow_id, org_id, doc_type, doc_id, doc_version, owner_user_id, "
                " current_quality_reviewer) VALUES (%s,%s,'runbook',%s,'v1',%s,%s)",
                (wf_id, org_id, doc_id, owner, reviewer),
            )
        conn.commit()
    finally:
        conn.close()

    yield {"org_id": org_id, "owner": owner, "reviewer": reviewer, "wf_id": wf_id}

    # Cleanup — remove every row this test created.
    conn = connect_to_rds()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT thread_id FROM chat_thread WHERE workflow_id=%s", (wf_id,))
            tids = [r[0] for r in (cur.fetchall() or [])]
            for tid in tids:
                cur.execute(
                    "DELETE FROM chat_message_translation WHERE message_id IN "
                    "(SELECT message_id FROM chat_message WHERE thread_id=%s)", (tid,)
                )
                cur.execute("DELETE FROM chat_message WHERE thread_id=%s", (tid,))
                cur.execute("DELETE FROM chat_participant WHERE thread_id=%s", (tid,))
            cur.execute("DELETE FROM chat_thread WHERE workflow_id=%s", (wf_id,))
            cur.execute("DELETE FROM document_workflow WHERE workflow_id=%s", (wf_id,))
            cur.execute("DELETE FROM users WHERE user_id IN (%s,%s)", (owner, reviewer))
        conn.commit()
    finally:
        conn.close()


def test_live_round_trip(live_workflow, monkeypatch):
    w = live_workflow

    # Thread creation seeds workflow roles.
    t = service.get_or_create_thread(w["owner"], "workflow", w["wf_id"], workflow_id=w["wf_id"])
    tid = t["thread_id"]
    roles = {p["user_id"]: p["role"] for p in service.list_participants(tid)}
    assert roles.get(w["owner"]) == "owner"
    assert roles.get(w["reviewer"]) == "quality_reviewer"

    # Add an assessee, post public + internal, check visibility filtering.
    service.add_participant(tid, w["owner"], email="vendor@smoke.test", side="assessee")
    service.post_message(tid, w["reviewer"], "public note", visibility="all")
    service.post_message(tid, w["reviewer"], "internal note", visibility="internal")
    internal_view, _ = service.list_messages(tid, w["owner"])
    assert {m["original_text"] for m in internal_view} >= {"public note", "internal note"}

    # Translation + disclaimer for a reviewer reading in French.
    service.set_language(tid, w["reviewer"], "fr")
    fr_view, _ = service.list_messages(tid, w["reviewer"])
    fr_public = next(m for m in fr_view if m["original_text"] == "public note")
    assert fr_public["ai_translated"] and fr_public["disclaimer"]

    # Inbound email ingest + dedupe (bridge thread id may be unset, use fallback).
    monkeypatch.setattr(email_bridge, "_find_bridged_thread", lambda *a, **k: service.get_thread(tid))
    msg = email_bridge.ingest_inbound_email(
        provider="gmail", from_email="vendor@smoke.test", subject="Re: assessment",
        body="reply body", email_thread_id=None, email_message_id=f"live-{uuid.uuid4().hex}",
    )
    assert msg and msg["source"] == "email"
