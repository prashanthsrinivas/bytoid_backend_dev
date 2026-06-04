"""§4g (AI batch) — ``WorkflowRunnerV2`` intent/routing AI methods, mocked LLM.

These all build a prompt from PLAY_TEMPLATE + instance state and delegate to
``get_parsed_fireworks_response``. We patch that wrapper with a rich sentinel and
patch ``PLAY_TEMPLATE`` with the keys each method reads, then assert the method
runs and returns the documented type (contract/smoke).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.workflow_playbook import _wf_pb_stubs as stubs

stubs.bootstrap_sut()

import services.workflow_service as ws  # noqa: E402

pytestmark = pytest.mark.unit

# String templates (str.replace ignores unknown placeholders) + the two dict-shaped
# ones accessed via .get("instructions").
_STR_KEYS = [
    "detect_trigger_type", "detect_current_step", "decision_type_check",
    "gather_workflow_missing_inputs", "detect_and_route_input",
    "step_clarification_prompt", "execution_helper", "reset_intent_handler",
    "wf_conversation", "input_intent_classifier", "workflow_conversation_handler",
]
_TMPL = {k: "{{user_input}}" for k in _STR_KEYS}
_TMPL["explain_workflow"] = {"instructions": "{{user_input}}"}
_TMPL["chat_summarization"] = {"instructions": "{{user_input}}"}

# Rich result so key-extracting methods (result["step_id"], result["summary"], …)
# all find what they need.
_SENTINEL = {
    "step_id": "1", "summary": "a summary", "reply": "hi", "trigger_type": "manual",
    "intent": "workflow", "bs_wf_single_runner": False, "required_inputs": [],
    "decision": "yes", "explanation": "because", "route": "execute",
}


def _runner():
    r = object.__new__(ws.WorkflowRunnerV2)
    r.userid = "u1"
    r.credits = MagicMock()
    r.logger = MagicMock()
    r.previous_data = {}
    r.input_data = {}
    _step = {"id": "1", "title": "Step One", "objective": "do x", "function_call": {}}
    r.steps = {"1": _step}
    r.contacts = []
    r.chat_history = []
    r.workflow_json = {"workflow": {"steps": [_step]}, "pre_user_data": {}}
    r.step_order = {"1": 0}
    r.wf_loc = "loc/wf"
    r.on_loc = "loc/on"
    r.execution_id = None
    r.testing = True
    r.get_parsed_fireworks_response = AsyncMock(return_value=dict(_SENTINEL))
    r.get_eval_parsed_fireworks_response = AsyncMock(return_value=dict(_SENTINEL))
    return r


def _call(method_name, *args):
    r = _runner()
    with patch.object(ws, "PLAY_TEMPLATE", _TMPL):
        return asyncio.run(getattr(r, method_name)("do the thing", *args))


@pytest.mark.parametrize("method", [
    "ai_detect_trigger_type",
    "ai_detect_current_step",
    "ai_explain_workflow_steps",
    "ai_reset_intent_handler",
])
def test_ai_method_runs_and_returns(method):
    out = _call(method)
    assert out is not None        # ran end-to-end, returned a value (no crash)


def test_ai_decision_check_with_step_id():
    r = _runner()
    r.input_data = "{}"          # decision builder string-replaces input_data
    with patch.object(ws, "PLAY_TEMPLATE", _TMPL):
        out = asyncio.run(r.ai_decision_Check("do the thing", "1"))
    assert out is not None


def test_ai_execute_helper_runs():
    r = _runner()
    r.workflow_json["pre_user_data"] = {}
    with patch.object(ws, "PLAY_TEMPLATE", _TMPL):
        out = asyncio.run(r.ai_execute_helper("do the thing", [], "instructions"))
    assert out is not None


def test_ai_detect_and_route_input_runs():
    r = _runner()
    with patch.object(ws, "PLAY_TEMPLATE", _TMPL):
        out = asyncio.run(r.ai_detect_and_route_input("do the thing"))
    assert out is not None


def test_ai_pre_gather_details_runs():
    r = _runner()
    r.saveworkflowtos3 = MagicMock()       # skip the S3 persistence path
    with patch.object(ws, "PLAY_TEMPLATE", _TMPL):
        out = asyncio.run(r.ai_pre_gather_details("do the thing"))
    assert out is not None or out is False        # ran (may return False)


def test_make_workflow_conversation_returns_dict():
    r = _runner()
    r.saveworkflowtos3 = MagicMock()
    with patch.object(ws, "PLAY_TEMPLATE", _TMPL), \
         patch.object(ws, "read_json_from_s3", return_value={}):
        out = asyncio.run(r.make_workflow_conversation("hi"))
    assert isinstance(out, dict)


def test_ai_scheudle_step_no_scheduler_returns_none():
    import sys
    import types as _types
    r = _runner()
    # the method imports services.scheduler_service (pulls a broken pandas in this
    # env); stub it, then the no-scheduler step short-circuits to None.
    fake_sched = _types.ModuleType("services.scheduler_service")
    fake_sched.SchedulerService = MagicMock()
    with patch.dict(sys.modules, {"services.scheduler_service": fake_sched}):
        assert asyncio.run(r.ai_scheudle_step("1", {"is_scheduler": None})) is None


def test_fetchusersocialandtimezone_defaults_utc():
    r = _runner()
    r.connection = MagicMock()
    with patch.object(ws, "fetch_user_Social", return_value="other"):
        social, tz = r.fetchusersocialandtimezone()
    assert social == "other" and tz == "UTC"


def test_get_chat_summarization_returns_summary():
    r = _runner()
    with patch.object(ws, "PLAY_TEMPLATE", _TMPL):
        out = asyncio.run(r.get_chat_summarization())
    assert out == "a summary"     # result["summary"]


def test_ai_detect_current_step_extracts_step_id():
    out = _call("ai_detect_current_step")
    assert out == "1"             # result["step_id"]
