"""Phase 2 — server-side enforcement of evidence ``responsePolicy``.

``evidence_only`` (the default) blocks free-text answers to evidence questions;
``text_fallback_allowed`` keeps the legacy behavior. Covers the evidence-based
question path (``answer_evidence_question``), the assigned-question paths
(``answer_questions`` / ``answer_questions_bulk``), and the generation-side
option set.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from tests.workflow_playbook import _wf_pb_stubs as stubs

stubs.bootstrap_sut()

import services.workflow_service as ws  # noqa: E402

pytestmark = pytest.mark.unit


def _runner(**wf):
    r = object.__new__(ws.WorkflowRunnerV2)
    r.userid = "u1"
    base = {"filename": "f.json", "assigned_questions": [], "evidence_based_questions": []}
    base.update(wf)
    r.workflow_json = base
    return r


@pytest.fixture(autouse=True)
def _no_s3():
    with patch.object(ws, "save_playbook_to_s3", MagicMock()), \
         patch.object(ws.WorkflowRunnerV2, "saveworkflowtos3", MagicMock(), create=True):
        yield


def _policy_map(mapping):
    return patch(
        "config_evidences.evidence_helpers.get_response_policy_map",
        return_value=mapping,
    )


# ── generation-side option set ────────────────────────────────────────────────

def test_evidence_question_options_evidence_only_drops_text():
    options, meta = ws._evidence_question_options("evidence_only")
    assert meta["text_options"] == []
    assert "A" not in options  # the verbal/text option is gone
    assert meta["upload_options"] == ["B"]


def test_evidence_question_options_text_fallback_keeps_text():
    options, meta = ws._evidence_question_options("text_fallback_allowed")
    assert meta["text_options"] == ["A"]
    assert "A" in options and "B" in options


# ── answer_evidence_question ───────────────────────────────────────────────────

def test_evidence_question_text_rejected_when_evidence_only():
    r = _runner(evidence_based_questions=[{
        "id": "evidence_1", "evidence_artifact": "Policies",
        "response_policy": "evidence_only",
        "options": {"B": "Upload new evidence"},
        "upload_options": ["B"], "text_options": ["A"]}])
    # 'A' is a (stale) text option; policy must block it
    out = r.answer_evidence_question("evidence_1", "A", comment="some text")
    assert out["status"] == "error"
    assert out["policy"] == "evidence_only"
    assert out["required_artifact"] == "Policies"


def test_evidence_question_upload_accepted_when_evidence_only():
    r = _runner(evidence_based_questions=[{
        "id": "evidence_1", "evidence_artifact": "Policies",
        "response_policy": "evidence_only",
        "options": {"B": "Upload new evidence"},
        "upload_options": ["B"], "text_options": []}])
    out = r.answer_evidence_question("evidence_1", "B", evidence_url="key/p.pdf")
    assert out["status"] == "success"
    assert out["answer_type"] == "upload"


def test_evidence_question_text_accepted_when_fallback():
    r = _runner(evidence_based_questions=[{
        "id": "evidence_1", "evidence_artifact": "Policies",
        "response_policy": "text_fallback_allowed",
        "options": {"A": "Provide a verbal / text answer", "B": "Upload new evidence"},
        "upload_options": ["B"], "text_options": ["A"]}])
    out = r.answer_evidence_question("evidence_1", "A", comment="explanation")
    assert out["status"] == "success"
    assert out["answer_type"] == "text"


def test_evidence_question_policy_from_config_when_unstamped():
    # No response_policy on the question → fall back to the live config map.
    r = _runner(evidence_based_questions=[{
        "id": "evidence_1", "evidence_artifact": "Policies",
        "options": {"A": "Provide a verbal / text answer", "B": "Upload new evidence"},
        "upload_options": ["B"], "text_options": ["A"]}])
    with _policy_map({"Policies": "evidence_only"}):
        out = r.answer_evidence_question("evidence_1", "A", comment="text")
    assert out["status"] == "error"


# ── answer_questions / _enforce_response_policy ────────────────────────────────

def _qa_workflow(evidence_required, admissible_artifacts):
    return {
        "filename": "f.json",
        "assigned_questions": [{"id": "q1", "evidence_required": evidence_required}],
        "evidence_overview": {
            "admissible": [{"artifact": a} for a in admissible_artifacts],
            "inadmissible": [], "discarded": [],
        },
    }


def test_assigned_text_answer_blocked_evidence_only():
    r = _runner(**_qa_workflow(["Policies"], []))
    with _policy_map({"Policies": "evidence_only"}):
        err = r._enforce_response_policy("q1", "Yes we comply")
    assert err is not None
    assert err["required_artifact"] == "Policies"


def test_assigned_text_answer_allowed_when_evidence_satisfied():
    r = _runner(**_qa_workflow(["Policies"], ["Policies"]))
    with _policy_map({"Policies": "evidence_only"}):
        assert r._enforce_response_policy("q1", "Yes") is None


def test_assigned_text_answer_allowed_when_fallback():
    r = _runner(**_qa_workflow(["Policies"], []))
    with _policy_map({"Policies": "text_fallback_allowed"}):
        assert r._enforce_response_policy("q1", "Yes") is None


def test_assigned_no_evidence_required_unaffected():
    r = _runner(**_qa_workflow([], []))
    with _policy_map({}):
        assert r._enforce_response_policy("q1", "Yes") is None


def test_assigned_clearing_answer_always_allowed():
    r = _runner(**_qa_workflow(["Policies"], []))
    with _policy_map({"Policies": "evidence_only"}):
        assert r._enforce_response_policy("q1", "") is None


def test_answer_questions_returns_policy_error():
    wf = _qa_workflow(["Policies"], [])
    r = _runner(**wf)
    r.previous_data = {"s1": {"output": [{"id": "q1"}]}}
    r.chat_history = []
    with _policy_map({"Policies": "evidence_only"}):
        out = asyncio.run(r.answer_questions("Yes", "c", "q1", "chid"))
    assert out["status"] == "error"
    assert out["policy"] == "evidence_only"


def test_answer_questions_bulk_partitions_rejected():
    wf = {
        "filename": "f.json",
        "assigned_questions": [
            {"id": "q1", "evidence_required": ["Policies"]},
            {"id": "q2", "evidence_required": []},
        ],
        "evidence_overview": {"admissible": [], "inadmissible": [], "discarded": []},
    }
    r = _runner(**wf)
    r.previous_data = {"s1": {"output": [{"id": "q1"}, {"id": "q2"}]}}
    r.chat_history = []
    r.steps = {}
    r.step_order = {}
    with _policy_map({"Policies": "evidence_only"}), \
         patch.object(r, "_question_answer_stats",
                      return_value={"all_answered": False, "answered": 1, "total": 2}):
        out = asyncio.run(r.answer_questions_bulk(
            [{"question_id": "q1", "user_answer": "Yes"},
             {"question_id": "q2", "user_answer": "No"}], "chid"))
    assert out["status"] == "success"
    assert "rejected" in out and len(out["rejected"]) == 1
    assert out["rejected"][0]["qid"] == "q1"
    # q2 (no evidence required) was applied
    assert r.previous_data["s1"]["output"][1]["user_answer"] == "No"
    assert r.previous_data["s1"]["output"][0].get("user_answer") != "Yes"
