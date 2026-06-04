"""§4g (cont.) — more ``WorkflowRunnerV2`` sync methods.

``update_statuscount`` (mutates autotest + persists), ``_question_answer_stats``,
``_are_all_required_fields_answered``, ``_build_dependency_blocked_response``.
Built via ``object.__new__``; persistence/logging are stubbed on the instance.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.workflow_playbook import _wf_pb_stubs as stubs

stubs.bootstrap_sut()

import services.workflow_service as ws  # noqa: E402

pytestmark = pytest.mark.unit


def _runner(**attrs):
    r = object.__new__(ws.WorkflowRunnerV2)
    for k, v in attrs.items():
        setattr(r, k, v)
    return r


# ── update_statuscount ────────────────────────────────────────────────────────

def test_update_statuscount_sets_autotest_and_persists():
    r = _runner(workflow_json={})
    r.saveworkflowtos3 = MagicMock(return_value="saved")
    out = r.update_statuscount(7, "running")
    assert out == "saved"
    assert r.workflow_json["autotest"] == {"count": 7, "status": "running"}
    assert r.saveworkflowtos3.called


def test_update_statuscount_ignores_none_values():
    r = _runner(workflow_json={"autotest": {"count": 1, "status": "old"}})
    r.saveworkflowtos3 = MagicMock(return_value=None)
    r.update_statuscount(None, "done")
    assert r.workflow_json["autotest"] == {"count": 1, "status": "done"}


# ── _question_answer_stats ────────────────────────────────────────────────────

def test_question_answer_stats_counts_answered():
    r = _runner(previous_data={
        "s1": {"output": [{"user_answer": "yes"}, {"user_answer": ""},
                          {"no_answer_key": 1}]},
        "s2": {"output": "not-a-list"},
    })
    assert r._question_answer_stats() == {"answered": 1, "total": 2, "all_answered": False}


def test_question_answer_stats_all_answered():
    r = _runner(previous_data={"s1": {"output": [{"user_answer": "a"},
                                                 {"user_answer": "b"}]}})
    assert r._question_answer_stats() == {"answered": 2, "total": 2, "all_answered": True}


def test_question_answer_stats_empty():
    assert _runner(previous_data={})._question_answer_stats() == {
        "answered": 0, "total": 0, "all_answered": False}


# ── _are_all_required_fields_answered ─────────────────────────────────────────

def _wf_with_field(required, user_answer):
    return {"s1": {"output": {"form_schema": {"fields": [
        {"required": required, "user_answer": user_answer}]}}}}


def test_required_field_answered_true():
    r = _runner(previous_data=_wf_with_field(True, "filled"))
    assert r._are_all_required_fields_answered() is True


@pytest.mark.parametrize("blank", [None, ""])
def test_required_field_unanswered_false(blank):
    r = _runner(previous_data=_wf_with_field(True, blank))
    assert r._are_all_required_fields_answered() is False


def test_optional_field_blank_is_ok():
    r = _runner(previous_data=_wf_with_field(False, None))
    assert r._are_all_required_fields_answered() is True


def test_no_fields_is_ok():
    assert _runner(previous_data={})._are_all_required_fields_answered() is True


# ── _build_dependency_blocked_response ────────────────────────────────────────

def test_build_dependency_blocked_response_shape_and_message():
    r = _runner(logger=MagicMock())
    out = r._build_dependency_blocked_response(
        target_step={"id": "t", "title": "Target Step"},
        blocking_step={"id": "b", "title": "Blocker Step"},
        required_fields=["name", "email"],
        step_ref="2",
    )
    assert out["execution_status"] == "dependency_blocked"
    assert out["step_id"] == "b"
    assert out["clarification_needed"] is True
    assert out["workflow_intent"] is False
    for token in ("Target Step", "Blocker Step", "name", "email"):
        assert token in out["message"]


# ── get_current_execution_data ────────────────────────────────────────────────

def test_get_current_execution_data_testing_mode():
    r = _runner(workflow_json={"testing": {"x": 1}}, testing=True)
    assert r.get_current_execution_data() == {"x": 1}


def test_get_current_execution_data_execution_mode_reads_s3():
    r = _runner(workflow_json={}, testing=False, execution_id="e1", on_loc="loc/x")
    with patch.object(ws, "read_json_from_s3", return_value={"y": 2}):
        assert r.get_current_execution_data() == {"y": 2}


def test_get_current_execution_data_neither_returns_none():
    r = _runner(workflow_json={}, testing=False, execution_id=None)
    assert r.get_current_execution_data() is None


# ── get_execution_log ─────────────────────────────────────────────────────────

def test_get_execution_log_returns_attribute():
    r = _runner(execution_log=[{"step": "1"}])
    assert r.get_execution_log() == [{"step": "1"}]


# ── append_execution_step_log ─────────────────────────────────────────────────

def test_append_execution_step_log_noop_in_testing():
    r = _runner(execution_id=None, testing=False)
    with patch.object(ws, "save_execution_playbook_to_s3") as save:
        assert r.append_execution_step_log("1", {"log": 1}) is None
        assert not save.called


def test_append_execution_step_log_writes_step():
    r = _runner(execution_id="e1", testing=False, on_loc="loc/x", userid="u1")
    with patch.object(ws, "read_json_from_s3", return_value={}), \
         patch.object(ws, "save_execution_playbook_to_s3") as save:
        r.append_execution_step_log("3", {"log": "done"})
    assert save.called
    saved_json = save.call_args.args[0]
    assert saved_json["steps"]["3"] == {"log": "done"}


# ── _find_fallback ────────────────────────────────────────────────────────────

def test_find_fallback_returns_other_fallback_step():
    steps = {
        "1": {"id": "1", "title": "Main step"},
        "2": {"id": "2", "title": "Fallback handler", "objective": ""},
    }
    r = _runner(steps=steps)
    assert r._find_fallback({"id": "1"}) == {"id": "2", "title": "Fallback handler",
                                             "objective": ""}


def test_find_fallback_excludes_self():
    steps = {"2": {"id": "2", "title": "fallback"}}
    r = _runner(steps=steps)
    assert r._find_fallback({"id": "2"}) is None


def test_find_fallback_none_when_absent():
    r = _runner(steps={"1": {"id": "1", "title": "normal", "objective": "do"}})
    assert r._find_fallback({"id": "1"}) is None


# ── get_current_chats ─────────────────────────────────────────────────────────

def test_get_current_chats_no_chat_log():
    r = _runner(workflow_json={"chat": [{"m": 1}]})
    assert r.get_current_chats() == {"chat": [{"m": 1}], "chat_summarization": ""}


def test_get_current_chats_with_summary_returns_tail():
    chats = [{"m": i} for i in range(15)]
    r = _runner(workflow_json={
        "chat": chats,
        "chat_log": {"last_chat_summarized": "yes", "chat_summarization": "summary"},
    })
    out = r.get_current_chats()
    assert out["chat_summarization"] == "summary"
    assert out["chat"] == chats[-10:]          # last 10 only


# ── get_attendees ─────────────────────────────────────────────────────────────

def test_get_attendees_all_uses_contacts():
    r = _runner(testing=False, contacts=[{"email": "a@x.com"}])
    with patch.object(ws, "can_reply_to_email", return_value=True):
        assert r.get_attendees("all") == ["a@x.com"]


def test_get_attendees_dict_input():
    r = _runner(testing=False)
    with patch.object(ws, "can_reply_to_email", return_value=True):
        assert r.get_attendees({"email": "b@x.com"}) == ["b@x.com"]


def test_get_attendees_string_email():
    r = _runner(testing=False)
    with patch.object(ws, "can_reply_to_email", return_value=True):
        assert r.get_attendees("c@x.com") == ["c@x.com"]


def test_get_attendees_rejects_invalid_string():
    r = _runner(testing=False)
    with patch.object(ws, "can_reply_to_email", return_value=False):
        assert r.get_attendees("not-an-email") == []


# ── handle_workflow_reset ─────────────────────────────────────────────────────

def test_handle_workflow_reset_clarification():
    r = _runner()
    out = r.handle_workflow_reset(
        {"clarification_needed": True, "response_message": "which step?"}, "reset")
    assert out["clarification_needed"] is True
    assert out["log_status"] == "clarification"


def test_handle_workflow_reset_unclear():
    r = _runner()
    out = r.handle_workflow_reset({}, "reset something")
    assert out["clarification_needed"] is True
    assert "unclear" in out["response_message"].lower()


# ── storeargument_results ─────────────────────────────────────────────────────

def test_storeargument_results_stores_meaningful_value():
    r = _runner(workflow_json={"pre_user_data": {}})
    r.storeargument_results({"city": "NYC"})
    assert r.workflow_json["pre_user_data"].get("city") == "NYC"


def test_storeargument_results_skips_reserved_keys():
    r = _runner(workflow_json={"pre_user_data": {}})
    r.storeargument_results({"status": "ok", "message": "hi", "success": True})
    pud = r.workflow_json["pre_user_data"]
    assert "status" not in pud and "message" not in pud and "success" not in pud


def test_storeargument_results_skips_question_content():
    r = _runner(workflow_json={"pre_user_data": {}})
    r.storeargument_results({"question": "What?", "answer": "A"})
    assert r.workflow_json["pre_user_data"] == {}


# ── _resolve_placeholders ─────────────────────────────────────────────────────

def test_resolve_placeholders_no_placeholders_passthrough():
    import asyncio
    r = _runner(logger=MagicMock(), previous_data={}, workflow_json={})
    resolved, blocking = asyncio.run(r._resolve_placeholders({"a": "plain", "n": 5}))
    assert resolved == {"a": "plain", "n": 5}
    assert blocking is None


def test_resolve_placeholders_already_resolved_via_pre_user_data():
    import asyncio
    r = _runner(logger=MagicMock(), previous_data={},
                workflow_json={"pre_user_data": {"name": "Bob"}})
    _resolved, blocking = asyncio.run(
        r._resolve_placeholders({"greeting": "{{step_1.name}}"}))
    assert blocking is None        # dependency already satisfied → not blocked
