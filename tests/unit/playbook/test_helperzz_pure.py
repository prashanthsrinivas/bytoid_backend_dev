"""Phase 1 — pure-function unit tests for ``playbook/helperzz.py`` (§4e).

Covers the no-I/O parsers/helpers: ``base_name``, ``clean_yaml_block``,
``normalize_input``, ``extract_questions``, ``clean_json_block``,
``normalize_contacts``, ``extract_json_from_llm_output``,
``cheap_internal_data_hint``, ``generate_meeting_email_body``,
``returninsructdata``. Each gets valid + edge cases. Assertions reflect the
*actual* current behavior of the source.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

# Bring up the shared stub finder before importing the SUT (helperzz pulls
# AWS/LLM/google heavies transitively at import time).
from tests.workflow_playbook import _wf_pb_stubs as stubs

stubs.bootstrap_sut()

import playbook.helperzz as h  # noqa: E402

pytestmark = pytest.mark.unit


# ── base_name ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("filename,expected", [
    ("abcdefghij.yaml", "abcdefgh"),   # first 8 of stem
    ("ab.txt", "ab"),
    ("noext", "noext"),
    ("12345678901234.json", "12345678"),
    ("", ""),
    ("a.b.c.yaml", "a.b.c"),           # splitext drops only last ext
])
def test_base_name(filename, expected):
    assert h.base_name(filename) == expected


# ── clean_yaml_block ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("```yaml\nfoo: 1\n```", "foo: 1"),
    ("```yml\nbar: 2\n```", "bar: 2"),
    ("```\nbaz: 3\n```", "baz: 3"),
    ("no fence here", "no fence here"),
    ("   spaced   ", "spaced"),
])
def test_clean_yaml_block(text, expected):
    assert h.clean_yaml_block(text) == expected


# ── normalize_input ───────────────────────────────────────────────────────────

def test_normalize_input_renames_keys():
    out = h.normalize_input(
        {"trigger_input_list": [1, 2], "triggermode": "auto", "keep": "x"}
    )
    assert out == {"trigger_input": [1, 2], "trigger_mode": "auto", "keep": "x"}


def test_normalize_input_does_not_mutate_original():
    src = {"trigger_input_list": [1]}
    h.normalize_input(src)
    assert "trigger_input_list" in src and "trigger_input" not in src


def test_normalize_input_empty():
    assert h.normalize_input({}) == {}


# ── extract_questions ─────────────────────────────────────────────────────────

def test_extract_questions_filters_to_questions():
    text = "What is this?\n- How now?\nrandom statement line\nIs it ok?"
    assert h.extract_questions(text) == ["What is this?", "How now?", "Is it ok?"]


@pytest.mark.parametrize("text", ["", "   ", "just a statement."])
def test_extract_questions_none_match(text):
    assert h.extract_questions(text) == []


def test_extract_questions_strips_bullets():
    assert h.extract_questions("• Can we proceed?") == ["Can we proceed?"]


# ── clean_json_block ──────────────────────────────────────────────────────────

def test_clean_json_block_dict_roundtrips_to_json():
    assert json.loads(h.clean_json_block({"a": 1})) == {"a": 1}


def test_clean_json_block_strips_json_fence():
    assert h.clean_json_block('```json\n{"a": 1}\n```') == '{"a": 1}'


def test_clean_json_block_plain_passthrough():
    assert h.clean_json_block("  plain text  ") == "plain text"


# ── extract_json_from_llm_output ──────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ('```json\n{"a": 1}\n```', '{"a": 1}'),
    ('```\n{"a": 1}\n```', '{"a": 1}'),
    ('{"a": 1}', '{"a": 1}'),
    ('  {"a": 1}  ', '{"a": 1}'),
])
def test_extract_json_from_llm_output(raw, expected):
    assert h.extract_json_from_llm_output(raw) == expected


# ── normalize_contacts ────────────────────────────────────────────────────────

@pytest.mark.parametrize("contacts,expected", [
    ("all", "All"),
    ("All", "All"),
    ("  aLL ", "All"),
    ("bob@example.com", "bob@example.com"),
    (["all"], "All"),
    (["All"], "All"),
    (["a@x.com", "b@x.com"], ["a@x.com", "b@x.com"]),
    ([], []),
    (123, "All"),          # fallback for unexpected type
    (None, "All"),
])
def test_normalize_contacts(contacts, expected):
    assert h.normalize_contacts(contacts) == expected


# ── cheap_internal_data_hint ──────────────────────────────────────────────────

def test_cheap_internal_data_hint_positive():
    assert h.cheap_internal_data_hint(
        {"name": "check inventory levels", "description": "", "trigger_input": []}
    ) is True


def test_cheap_internal_data_hint_negative():
    assert h.cheap_internal_data_hint(
        {"name": "say hello", "description": "greet the user", "trigger_input": []}
    ) is False


def test_cheap_internal_data_hint_uses_trigger_input():
    assert h.cheap_internal_data_hint(
        {"name": "x", "description": "y", "trigger_input": ["pull the sales report"]}
    ) is True


# ── generate_meeting_email_body ───────────────────────────────────────────────

def test_generate_meeting_email_body_defaults_safe():
    out = h.generate_meeting_email_body({}, {})
    assert isinstance(out, str)
    assert "there" in out                      # default first name
    assert "Link not available" in out         # default hangout link


def test_generate_meeting_email_body_includes_summary_and_name():
    out = h.generate_meeting_email_body(
        {"summary": "Kickoff", "hangoutLink": "https://meet/x"},
        {"first_name": "Ada", "BusinessName": "Acme"},
    )
    assert "Ada" in out and "Kickoff" in out and "Acme" in out


# ── returninsructdata ─────────────────────────────────────────────────────────

def test_returninsructdata_shape_and_contact_normalization():
    out = h.returninsructdata(
        {"title": "T", "description": "D", "contacts": ["all"],
         "communication_channels": ["email", "email", "sms"]}
    )
    assert out["name"] == "T" and out["description"] == "D"
    assert out["selected_contacts"] == "All"
    assert sorted(out["communication_mode"]) == ["email", "sms"]   # de-duped
    assert out["user_timezone"] == "UTC"


def test_returninsructdata_tolerates_missing_fields():
    out = h.returninsructdata({})
    # missing contacts → normalize_contacts([]) → [] (empty list, not "All")
    assert out["name"] == "" and out["selected_contacts"] == []
    assert out["communication_mode"] == []


# ── format_step_data ──────────────────────────────────────────────────────────

def test_format_step_data_core_fields_and_defaults():
    out = h.format_step_data({"id": "1", "title": "T", "stepType": "action",
                              "objective": "do"})
    assert out["id"] == "1" and out["title"] == "T"
    assert out["type"] == "action" and out["objective"] == "do"
    assert out["decision_point"] is False        # always present, boolean
    assert out["requirements_needed"] == []       # defaulted
    assert out["is_scheduler"] is None            # always present


def test_format_step_data_skips_empty_and_none():
    out = h.format_step_data({"id": "", "title": None, "objective": "keep"})
    assert "id" not in out and "title" not in out
    assert out["objective"] == "keep"


def test_format_step_data_decision_point():
    out = h.format_step_data({"isDecisionPoint": True, "decisionType": "binary"})
    assert out["decision_point"] is True and out["decision_type"] == "binary"


def test_format_step_data_next_step_precedence():
    out = h.format_step_data({"nextStepIds": ["2"], "next_step": "9"})
    assert out["next_step"] == ["2"]              # nextStepIds wins


def test_format_step_data_communication_extras():
    out = h.format_step_data({"stepType": "communication",
                              "communicationMode": "email",
                              "selectedIntegrations": ["gmail"]})
    assert out["type"] == "communication"
    assert out["communication_mode"] == "email"
    assert out["channels"] == ["gmail"]


def test_format_step_data_navigation_page_url():
    out = h.format_step_data({"stepType": "navigation", "pageUrl": "/dash"})
    assert out["page_url"] == "/dash"


# ── replace_section ───────────────────────────────────────────────────────────

_PROMPT = "## Intro\nold intro\n## Body\nkeep me\n"


def test_replace_section_replaces_content():
    out = h.replace_section(_PROMPT, "Intro", "new intro")
    assert "new intro" in out
    assert "old intro" not in out
    assert "## Body\nkeep me" in out          # other sections preserved


def test_replace_section_removes_when_empty():
    out = h.replace_section(_PROMPT, "Intro", "")
    assert "old intro" not in out and "Intro" not in out
    assert "## Body\nkeep me" in out


def test_replace_section_missing_raises():
    with pytest.raises(ValueError):
        h.replace_section(_PROMPT, "Nonexistent", "x")


# ── returnconfigandpath (success path) ────────────────────────────────────────

def test_returnconfigandpath_success():
    with patch.object(h, "get_subagent_by_userid", return_value="sub-1"), \
         patch.object(h, "check_subagent_by_playbook", return_value=("pb-1", "cfg/path")):
        assert h.returnconfigandpath("u1") == ("pb-1", "cfg/path", "sub-1")
