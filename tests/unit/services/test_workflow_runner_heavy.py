"""§4g — ``WorkflowRunnerV2`` heavy orchestrators (coverable paths).

Each is exercised via its simplest end-to-end path with collaborators mocked:
``_execute_step`` (self-learn branch), ``update_steps_workflow`` (delegates to
modify_instruction).
"""

from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.workflow_playbook import _wf_pb_stubs as stubs

stubs.bootstrap_sut()

import services.workflow_service as ws  # noqa: E402

pytestmark = pytest.mark.unit


def _runner(**attrs):
    r = object.__new__(ws.WorkflowRunnerV2)
    r.logger = MagicMock()
    r.userid = "u1"
    r.credits = MagicMock()
    for k, v in attrs.items():
        setattr(r, k, v)
    return r


# ── _execute_step (self-learn branch: step with no function_call) ─────────────

def test_execute_step_self_learn_path():
    step = {"id": "1", "ai_instructions": "summarize", "next_step": None}
    r = _runner(steps={"1": step}, ai_made_output={})
    with patch.object(ws, "get_fireworks_response", AsyncMock(return_value="learned")):
        out = asyncio.run(r._execute_step("1"))
    assert out["execution_status"] == "success"
    assert out["step_id"] == "1"
    assert out["workflow_intent"] is True


# ── update_steps_workflow (delegates to playbook.routes.modify_instruction) ───

def _patch_modify(return_value):
    fake = types.ModuleType("playbook.routes")
    fake.modify_instruction = AsyncMock(return_value=return_value)
    return patch.dict(sys.modules, {"playbook.routes": fake})


def test_update_steps_workflow_success():
    r = _runner(filename="f.json")
    with _patch_modify(True):
        assert asyncio.run(r.update_steps_workflow("rename step 1")) == "workflow updated."


def test_update_steps_workflow_noop_returns_none():
    r = _runner(filename="f.json")
    with _patch_modify(False):
        assert asyncio.run(r.update_steps_workflow("nothing")) is None


# ── _extract_context_for_step (small data → returned unchanged, no AI) ────────

def test_extract_context_for_step_small_data_passthrough():
    r = _runner()
    field_data = {"a": "small value", "b": [1, 2, 3]}
    out = asyncio.run(r._extract_context_for_step(field_data, {"id": "1"}))
    assert out == field_data        # under char budget → unchanged, no LLM call


# ── answer_ques_file_bk (no-assigned-questions early exit) ────────────────────

def test_answer_ques_file_bk_no_assigned_questions():
    r = _runner(workflow_json={}, previous_data={}, chat_history=[])
    # the method imports config_evidences.evidence_helpers + db.lance_db_service
    # at call time; stub both so the early-exit branch is reached.
    fake_ev = types.ModuleType("config_evidences.evidence_helpers")
    fake_ev.get_only_evidence = MagicMock()
    fake_lance = types.ModuleType("db.lance_db_service")
    fake_lance.LanceDBServer = MagicMock()
    with patch.dict(sys.modules, {"config_evidences.evidence_helpers": fake_ev,
                                  "db.lance_db_service": fake_lance}):
        out = asyncio.run(r.answer_ques_file_bk([], "1", []))
    assert out == {"error": "No assigned questions found"}


# ── autocheckerworkflow (all-steps-done retry exit) ───────────────────────────

def test_autocheckerworkflow_retry_exit():
    r = _runner(steps={}, step_order={}, previous_data={}, input_data={},
                workflow_json={"autotest": {"status": True, "count": 2}})
    r.saveworkflowtos3 = MagicMock()
    with patch.object(ws, "PLAY_TEMPLATE", {"autoworkflow_initiator": "x"}):
        out = asyncio.run(r.autocheckerworkflow())
    assert out == "clear all steps for retry"
    assert r.saveworkflowtos3.called


# ── execute_from_text_input (locked non-existent step → ValueError) ───────────

def test_execute_from_text_input_unknown_step_raises():
    r = _runner(testing=True, workflow_json={}, steps={})
    tmpl = {"select_and_prepare_step": {"instructions": "{{user_input}}"}}
    with patch.object(ws, "load_yaml_file", return_value=tmpl), \
         pytest.raises(ValueError):
        asyncio.run(r.execute_from_text_input("do it", "99"))
