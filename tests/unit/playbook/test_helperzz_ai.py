"""§4e (AI) — helperzz async AI helpers with a mocked LLM.

``needs_internal_data`` (cheap regex gate → LLM authoritative) and
``minimize_functions`` (LLM → JSON → function expansion). The Fireworks call is
mocked; assertions cover the parse, the malformed-output fallback, and the
regex-shortcut (no-LLM) path.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.workflow_playbook import _wf_pb_stubs as stubs

stubs.bootstrap_sut()

import playbook.helperzz as h  # noqa: E402

pytestmark = pytest.mark.unit

_CREDITS = MagicMock()


# ── needs_internal_data ───────────────────────────────────────────────────────

def test_needs_internal_data_regex_shortcut_no_llm():
    llm = AsyncMock()
    tmpl = {"detect_internal_data_dependency": "{instruction_input_as_json}"}
    with patch.object(h, "get_fireworks_response2", llm):
        out = asyncio.run(h.needs_internal_data(
            {"name": "say hello", "description": "greet", "trigger_input": []},
            tmpl, "u1", _CREDITS))
    assert out is False
    assert not llm.await_count        # regex gate short-circuited the LLM


def test_needs_internal_data_llm_true():
    llm = AsyncMock(return_value='{"needs_internal_data": true}')
    tmpl = {"detect_internal_data_dependency": "{instruction_input_as_json}"}
    with patch.object(h, "get_fireworks_response2", llm):
        out = asyncio.run(h.needs_internal_data(
            {"name": "pull the inventory report", "description": "", "trigger_input": []},
            tmpl, "u1", _CREDITS))
    assert out is True


def test_needs_internal_data_malformed_llm_returns_not_possible():
    llm = AsyncMock(return_value="not json at all")
    tmpl = {"detect_internal_data_dependency": "{instruction_input_as_json}"}
    with patch.object(h, "get_fireworks_response2", llm):
        out = asyncio.run(h.needs_internal_data(
            {"name": "check stock levels", "description": "", "trigger_input": []},
            tmpl, "u1", _CREDITS))
    assert isinstance(out, dict) and out["status"] == "Not possible"


# ── minimize_functions ────────────────────────────────────────────────────────

_TMPL = {"functions_checker":
         "{instruction_input_as_json}{all_function_details}{Actucal_user_socaial}"}


def test_minimize_functions_malformed_json():
    llm = AsyncMock(return_value="```json\nnot-json\n```")
    with patch.object(h, "get_fireworks_response2", llm):
        out = asyncio.run(h.minimize_functions({"x": 1}, _TMPL, {}, "google", "u1", _CREDITS))
    assert out["status"] == "Not possible"


def test_minimize_functions_not_possible_returns_none_pair():
    llm = AsyncMock(return_value='{"status": "Not possible"}')
    with patch.object(h, "get_fireworks_response2", llm):
        out = asyncio.run(h.minimize_functions({"x": 1}, _TMPL, {}, "google", "u1", _CREDITS))
    assert out == (None, None)


def test_minimize_functions_no_required_returns_all():
    fns = {"a": {"status": "active"}}
    llm = AsyncMock(return_value='{"required_functions": []}')
    with patch.object(h, "get_fireworks_response2", llm):
        out = asyncio.run(h.minimize_functions({"x": 1}, _TMPL, fns, "google", "u1", _CREDITS))
    assert out == (fns, None)


# ── check_doc_context_needed ──────────────────────────────────────────────────

_CTX_TMPL = {"checklanceneeded": "static prompt"}    # format ignores extra kwargs


def test_check_doc_context_needed_list_of_strings():
    inp = {"name": "n", "description": "d", "trigger_input": []}
    with patch.object(h, "get_fireworks_response2", AsyncMock(return_value='["Q1?", "Q2?"]')):
        out = asyncio.run(h.check_doc_context_needed(inp, _CTX_TMPL, "u1", _CREDITS))
    assert out == ["Q1?", "Q2?"]


def test_check_doc_context_needed_list_of_dicts():
    inp = {"name": "n", "description": "d", "trigger_input": []}
    with patch.object(h, "get_fireworks_response2",
                      AsyncMock(return_value='[{"question": "Q1?"}]')):
        out = asyncio.run(h.check_doc_context_needed(inp, _CTX_TMPL, "u1", _CREDITS))
    assert out == ["Q1?"]


def test_check_doc_context_needed_prose_fallback_to_extract():
    inp = {"name": "n", "description": "d", "trigger_input": []}
    with patch.object(h, "get_fireworks_response2",
                      AsyncMock(return_value="What is the scope?")):
        out = asyncio.run(h.check_doc_context_needed(inp, _CTX_TMPL, "u1", _CREDITS))
    assert out == ["What is the scope?"]      # extract_questions fallback


# ── evallogic ─────────────────────────────────────────────────────────────────

def test_evallogic_keeps_related_with_usecase():
    tmpl = {"context_workflow_validator_batch": "t"}
    batch = [{"query": "How do I X?"}]
    llm = AsyncMock(return_value='[{"related": true, "has_usecase_details": true, '
                                 '"explanation": "relevant"}]')
    with patch.object(h, "evaluator_context_llama", llm):
        out = asyncio.run(h.evallogic(tmpl, batch, "u1", _CREDITS))
    assert out == [{"User": "How do I X?", "Ai Response": "relevant"}]


def test_evallogic_drops_unrelated():
    tmpl = {"context_workflow_validator_batch": "t"}
    batch = [{"query": "q"}]
    llm = AsyncMock(return_value='[{"related": false, "has_usecase_details": false}]')
    with patch.object(h, "evaluator_context_llama", llm):
        out = asyncio.run(h.evallogic(tmpl, batch, "u1", _CREDITS))
    assert out == []


# ── triggeraicontextfinder (no-questions shortcut) ────────────────────────────

def test_triggeraicontextfinder_no_questions_returns_empty():
    inp = {"name": "n", "description": "d", "trigger_input": []}
    with patch.object(h, "check_doc_context_needed", AsyncMock(return_value=[])):
        out = asyncio.run(h.triggeraicontextfinder(inp, "u1", {}, [], _CREDITS))
    assert out == []

