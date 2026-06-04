"""§4g (AI) — ``WorkflowRunnerV2`` LLM wrappers / conversation, mocked LLM.

``get_parsed_fireworks_response`` / ``get_eval_parsed_fireworks_response`` (parse
+ fence-strip + retry → {}), ``ai_conversation_handler`` (delegates to the parsed
wrapper), and ``check_input_tone`` (intent routing with sub-methods stubbed).
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
    r.userid = "u1"
    r.credits = MagicMock()
    for k, v in attrs.items():
        setattr(r, k, v)
    return r


def _run(coro):
    return asyncio.run(coro)


# ── get_parsed_fireworks_response ─────────────────────────────────────────────

def test_get_parsed_fireworks_response_valid_json():
    r = _runner()
    with patch.object(ws, "get_fireworks_response2", AsyncMock(return_value='{"a": 1}')):
        assert _run(r.get_parsed_fireworks_response("p")) == {"a": 1}


def test_get_parsed_fireworks_response_strips_fence():
    r = _runner()
    with patch.object(ws, "get_fireworks_response2",
                      AsyncMock(return_value='```json\n{"a": 1}\n```')):
        assert _run(r.get_parsed_fireworks_response("p")) == {"a": 1}


def test_get_parsed_fireworks_response_invalid_returns_empty():
    r = _runner()
    with patch.object(ws, "get_fireworks_response2", AsyncMock(return_value="not json")):
        assert _run(r.get_parsed_fireworks_response("p")) == {}


# ── get_eval_parsed_fireworks_response ────────────────────────────────────────

def test_get_eval_parsed_fireworks_response_valid_json():
    r = _runner()
    with patch.object(ws, "get_evaluator_fireworks", AsyncMock(return_value='{"b": 2}')):
        assert _run(r.get_eval_parsed_fireworks_response("p")) == {"b": 2}


# ── ai_conversation_handler ───────────────────────────────────────────────────

def test_ai_conversation_handler_returns_reply():
    r = _runner(workflow_json={})
    r.get_parsed_fireworks_response = AsyncMock(return_value={"reply": "hello back"})
    with patch.object(ws, "PLAY_TEMPLATE",
                      {"workflow_conversation_handler": "{{user_input}}"}):
        assert _run(r.ai_conversation_handler("hi")) == "hello back"


# ── check_input_tone (normal-conversation branch) ─────────────────────────────

def test_check_input_tone_normal_conversation():
    r = _runner(chat_history=[], logger=MagicMock())
    r.ai_input_intent_classifier = AsyncMock(return_value={"intent": "normal_conversation"})
    r.ai_conversation_handler = AsyncMock(return_value="friendly reply")
    r.savechatcheck = AsyncMock()        # persistence side-effect, stubbed out
    out = _run(r.check_input_tone("how are you?"))
    assert out["response_message"] == "friendly reply"
    assert out["log_status"] == "normal"
    assert out["wf_single_runner"] is False
