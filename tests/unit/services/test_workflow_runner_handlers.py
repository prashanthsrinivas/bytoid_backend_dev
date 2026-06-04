"""§4g — ``WorkflowRunnerV2`` step handlers (``_handle_*``).

Communication (no function-call branch), navigation, and self-learn (LLM mocked).
Each returns ``{"output": ..., "next_step": ...}``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.workflow_playbook import _wf_pb_stubs as stubs

stubs.bootstrap_sut()

import services.workflow_service as ws  # noqa: E402

pytestmark = pytest.mark.unit


def _runner(**attrs):
    r = object.__new__(ws.WorkflowRunnerV2)
    r.logger = MagicMock()
    for k, v in attrs.items():
        setattr(r, k, v)
    return r


def test_handle_navigation():
    r = _runner()
    out = r._handle_navigation({"page_url": "/dash", "next_step": "2"})
    assert out == {"output": "[NAVIGATION] Go to /dash", "next_step": "2"}


def test_handle_communication_no_function_call():
    r = _runner()
    out = r._handle_communication(
        {"ai_instructions": "send the email", "id": "1", "next_step": None})
    assert out == {"output": "[COMMUNICATION] send the email", "next_step": None}


def test_handle_self_learn_uses_llm():
    r = _runner(ai_made_output={}, userid="u1", credits=MagicMock())
    with patch.object(ws, "get_fireworks_response", AsyncMock(return_value="learned it")):
        out = asyncio.run(r._handle_self_learn(
            {"ai_instructions": "learn", "id": "1", "next_step": None}))
    assert out == {"output": "learned it", "next_step": None}
    assert r.ai_made_output["1"] == "learned it"


# ── _trigger_function (validation error paths) ────────────────────────────────

def test_trigger_function_invalid_name_format():
    r = _runner()
    with pytest.raises(ValueError):
        asyncio.run(r._trigger_function("1", "noseparator", {}))


def test_trigger_function_unknown_service_prefix():
    r = _runner()
    with pytest.raises(ValueError):
        asyncio.run(r._trigger_function("1", "unknown.method", {}))


# ── _trigger_runbook_owner ────────────────────────────────────────────────────

def test_trigger_runbook_owner_enqueues_task():
    import sys
    import types as _types
    r = _runner(userid="u1", filename="f.json")
    fake_celery = _types.ModuleType("utils.celery_base")
    task = MagicMock()
    fake_celery.create_playbook_runbook_task = task
    with patch.dict(sys.modules, {"utils.celery_base": fake_celery}):
        r._trigger_runbook_owner("rb-1")
    task.delay.assert_called_once_with("u1", "f.json", "rb-1")
