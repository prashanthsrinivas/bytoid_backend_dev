"""§4h — ``AutoMateService`` AI methods (batch) with a mocked LLM.

Covers the text-returning methods (``generate_ai_content``, ``review_content``,
``generate_chat_reply``, ``generate_email_reply``) and the JSON-parsing methods
(``generate_form_schema``, ``evaluate_answers``): happy path + the
malformed/empty-output fallback that must not crash.
"""

from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.workflow_playbook import _wf_pb_stubs as stubs

stubs.bootstrap_sut()

import services.automate_service as au  # noqa: E402

pytestmark = pytest.mark.unit


def _svc():
    s = object.__new__(au.AutoMateService)
    s.userid = "u1"
    s.credits = MagicMock()
    s.connection = MagicMock()
    s.inputdata = {}
    s.workflow = {}
    s.current_step_data = {"ai_instructions": "do the thing"}
    return s


def _run(coro):
    return asyncio.run(coro)


# ── text-returning methods ────────────────────────────────────────────────────

def test_generate_email_reply_returns_stripped_text():
    with patch.object(au, "get_fireworks_response2", AsyncMock(return_value="  Hi there  ")):
        out = _run(_svc().generate_email_reply("original message"))
    assert out == "Hi there"


def test_generate_chat_reply_wraps_in_return_str():
    with patch.object(au, "get_fireworks_response2", AsyncMock(return_value="hello!")):
        out = _run(_svc().generate_chat_reply("hey"))
    assert out == {"return_str": "hello!"}


def test_generate_ai_content_wraps_in_return_str():
    with patch.object(au, "get_fireworks_response2", AsyncMock(return_value="generated body")):
        out = _run(_svc().generate_ai_content("write something"))
    assert out == {"return_str": "generated body"}


def test_review_content_wraps_in_return_str():
    with patch.object(au, "get_fireworks_response2", AsyncMock(return_value="looks good")):
        out = _run(_svc().review_content("my content"))
    assert out == {"return_str": "looks good"}


# ── JSON-parsing methods ──────────────────────────────────────────────────────

def test_generate_form_schema_parses_json():
    with patch.object(au, "get_fireworks_response2", AsyncMock(return_value='{"fields": []}')):
        out = _run(_svc().generate_form_schema("collect info"))
    assert out == {"form": {"fields": []}}


def test_generate_form_schema_empty_response_error():
    with patch.object(au, "get_fireworks_response2", AsyncMock(return_value="")):
        out = _run(_svc().generate_form_schema("collect info"))
    assert out == {"error": "AI returned empty response"}


def test_generate_form_schema_invalid_json_error():
    with patch.object(au, "get_fireworks_response2", AsyncMock(return_value="not json")):
        out = _run(_svc().generate_form_schema("collect info"))
    assert out["error"] == "AI returned invalid JSON"


def test_evaluate_answers_parses_json():
    with patch.object(au, "get_fireworks_response2", AsyncMock(return_value='{"score": 5}')):
        out = _run(_svc().evaluate_answers([{"id": "q1", "answer": "a"}]))
    assert out == {"score": 5}


def test_generate_questions_parses_list():
    with patch.object(au, "get_fireworks_response2",
                      AsyncMock(return_value='[{"q": "What?"}]')):
        out = _run(_svc().generate_questions("make questions"))
    assert out == {"questions": [{"q": "What?"}]}


def test_generate_questions_invalid_json_error():
    with patch.object(au, "get_fireworks_response2", AsyncMock(return_value="nope")):
        out = _run(_svc().generate_questions("make questions"))
    assert out["error"] == "Invalid response format from AI"


# ── search_knowledge_base (lazy LanceClient stubbed) ──────────────────────────

def _patch_lance(return_value):
    fake = types.ModuleType("agent_route.lance_agent")
    client = MagicMock()
    client.mixed_query_vector = AsyncMock(return_value=return_value)
    fake.LanceClient = MagicMock(return_value=client)
    fake.QueryInput = lambda **kw: kw
    return patch.dict(sys.modules, {"agent_route.lance_agent": fake})


def test_search_knowledge_base_returns_string_answer():
    with _patch_lance("the answer"):
        out = _run(_svc().search_knowledge_base("query"))
    assert out == "the answer"


def test_search_knowledge_base_no_answer_when_non_string():
    with _patch_lance(None):
        out = _run(_svc().search_knowledge_base("query"))
    assert out == "No answer found as per query"
