"""§4g — ``WorkflowRunnerV2`` Q&A / evidence mutators.

``edit_assigned_question``, ``delete_assigned_question``, ``morph_question``,
``assign_evidence_required``, ``answer_evidence_question``. Each mutates
``workflow_json`` and persists via ``save_playbook_to_s3`` (patched). Covers the
not-found / validation error branches and the happy path.
"""

from __future__ import annotations

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
    with patch.object(ws, "save_playbook_to_s3", MagicMock()):
        yield


# ── edit_assigned_question ────────────────────────────────────────────────────

def test_edit_assigned_question_empty_rejected():
    r = _runner()
    assert r.edit_assigned_question("q1", "  ")["status"] == "error"


def test_edit_assigned_question_not_found():
    r = _runner(assigned_questions=[{"id": "q1", "question": "old"}])
    assert r.edit_assigned_question("zzz", "new")["status"] == "error"


def test_edit_assigned_question_success():
    r = _runner(assigned_questions=[{"id": "q1", "question": "old"}])
    out = r.edit_assigned_question("q1", " new text ")
    assert out["status"] == "success"
    assert r.workflow_json["assigned_questions"][0]["question"] == "new text"


# ── delete_assigned_question ──────────────────────────────────────────────────

def test_delete_assigned_question_not_found():
    r = _runner(assigned_questions=[{"id": "q1"}])
    assert r.delete_assigned_question("zzz")["status"] == "error"


def test_delete_assigned_question_success():
    r = _runner(assigned_questions=[{"id": "q1"}, {"id": "q2"}])
    out = r.delete_assigned_question("q1")
    assert out["status"] == "success"
    assert [q["id"] for q in r.workflow_json["assigned_questions"]] == ["q2"]


# ── morph_question ────────────────────────────────────────────────────────────

def test_morph_question_invalid_type():
    r = _runner(assigned_questions=[{"id": "q1", "question": "x"}])
    assert r.morph_question("q1", "new", "bogus")["status"] == "error"


def test_morph_question_text_to_option_requires_options():
    r = _runner(assigned_questions=[{"id": "q1", "question": "x"}])
    out = r.morph_question("q1", "new", "text_to_option", new_options=None)
    assert out["status"] == "error"


def test_morph_question_success_option_to_text_clears_options():
    r = _runner(assigned_questions=[{"id": "q1", "question": "x", "options": {"a": 1}}])
    out = r.morph_question("q1", "new q", "option_to_text")
    assert out["status"] == "success"
    assert r.workflow_json["assigned_questions"][0]["options"] == {}


# ── assign_evidence_required ──────────────────────────────────────────────────

def test_assign_evidence_required_sets_field():
    r = _runner(assigned_questions=[{"id": "q1"}])
    r.assign_evidence_required("q1", ["screenshot", "log"])
    assert r.workflow_json["assigned_questions"][0]["evidence_required"] == ["screenshot", "log"]


# ── answer_evidence_question ──────────────────────────────────────────────────

def test_answer_evidence_question_not_found():
    r = _runner(evidence_based_questions=[{"id": "e1"}])
    assert r.answer_evidence_question("zzz", "yes")["status"] == "error"


def test_answer_evidence_question_discard_removes_it():
    r = _runner(evidence_based_questions=[
        {"id": "e1", "discard_options": ["no"], "upload_options": [], "text_options": []}])
    r.answer_evidence_question("e1", "no")
    assert r.workflow_json["evidence_based_questions"] == []
