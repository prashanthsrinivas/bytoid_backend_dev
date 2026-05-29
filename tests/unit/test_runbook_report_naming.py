"""Unit tests for runbook report naming helpers in runbook/report_naming.py.

The module is dependency-light: pure helpers plus one lazily-imported LLM call
(utils.fireworkzz.get_fireworks_response), which the tests patch. Importing it
does not drag in the rest of the runbook infra, so no global module stubbing is
needed (which keeps the rest of the suite uncontaminated).
"""

import asyncio

import pytest

from runbook import report_naming as rn


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── _extract_first_paragraph ─────────────────────────────────────────────────

@pytest.mark.unit
def test_extract_first_paragraph_strips_html_and_skips_short_blocks():
    merged = {
        "blocks": [
            {"micro_blocks": [{"html": "<h2>Title</h2>"}]},  # too short, skipped
            {"micro_blocks": [
                {"html": "<p>This assessment covers the <b>Acme Billing</b> "
                         "system and its data flows.</p>"}
            ]},
        ]
    }
    text = rn._extract_first_paragraph(merged)
    assert "Acme Billing" in text
    assert "<" not in text  # tags stripped


@pytest.mark.unit
@pytest.mark.parametrize("merged", [{}, {"blocks": []}, {"blocks": [{}]}, None])
def test_extract_first_paragraph_empty(merged):
    assert rn._extract_first_paragraph(merged) == ""


# ── _clean_descriptor ────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("raw,expected", [
    ('"Acme Billing"', "Acme Billing"),
    ("Acme Billing System\nextra line", "Acme Billing System"),
    ("one two three four five six", "one two three four"),  # capped at 4
    ("INSUFFICIENT", ""),
    ("", ""),
    (None, ""),
])
def test_clean_descriptor(raw, expected):
    assert rn._clean_descriptor(raw) == expected


# ── build_report_name ────────────────────────────────────────────────────────

def _patch_llm(monkeypatch, fn):
    """Patch the lazily-imported get_fireworks_response with a fake coroutine."""
    import sys
    import types

    mod = types.ModuleType("utils.fireworkzz")
    mod.get_fireworks_response = fn
    monkeypatch.setitem(sys.modules, "utils.fireworkzz", mod)


@pytest.mark.unit
def test_build_report_name_uses_ai_descriptor(monkeypatch):
    merged = {"blocks": [{"micro_blocks": [
        {"html": "<p>This privacy assessment evaluates the Acme Billing "
                 "platform end to end.</p>"}
    ]}]}

    async def fake_llm(msg, role, credits, user_id):
        return "Acme Billing"

    _patch_llm(monkeypatch, fake_llm)
    name = _run(rn.build_report_name("PIA", merged, credits=None, user_id="u1"))
    assert name == "PIA — Acme Billing"


@pytest.mark.unit
def test_build_report_name_falls_back_when_no_content():
    # No usable paragraph → timestamped fallback, still prefixed by runbook name.
    name = _run(rn.build_report_name("PIA", {"blocks": []}, credits=None, user_id="u1"))
    assert name.startswith("PIA — ")
    assert name != "PIA — "  # has a timestamp suffix


@pytest.mark.unit
def test_build_report_name_falls_back_on_insufficient_credits(monkeypatch):
    merged = {"blocks": [{"micro_blocks": [
        {"html": "<p>This assessment reviews the Acme data pipeline thoroughly.</p>"}
    ]}]}

    async def insufficient(msg, role, credits, user_id):
        return "INSUFFICIENT"

    _patch_llm(monkeypatch, insufficient)
    name = _run(rn.build_report_name("PIA", merged, credits=None, user_id="u1"))
    assert name.startswith("PIA — ")
    assert "INSUFFICIENT" not in name
