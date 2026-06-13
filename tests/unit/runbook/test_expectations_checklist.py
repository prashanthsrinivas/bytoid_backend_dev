"""Phase 4 — structured verification-expectations checklist.

The report (and intake overview) must show each declared expectation with a
met/not-met verdict (green tick / red cross). Core logic is the pure reconciler
``build_expectations_checklist``; this file proves it produces exactly one entry
per declared expectation and degrades safely on malformed LLM output.
"""

from __future__ import annotations

import pytest

# Dependency-free module — imports without runbook.helper's heavy chain.
from runbook.evidence_overview import build_expectations_checklist

pytestmark = pytest.mark.unit


def test_one_entry_per_expectation():
    expectations = "approved; version controlled; owner"
    llm = [
        {"expectation": "approved", "met": True, "reason": "signed"},
        {"expectation": "version controlled", "met": False, "reason": "no version"},
        {"expectation": "owner", "met": True, "reason": "CISO"},
    ]
    out = build_expectations_checklist(expectations, llm)
    assert [c["expectation"] for c in out] == ["approved", "version controlled", "owner"]
    assert [c["met"] for c in out] == [True, False, True]


def test_unevaluated_expectations_marked_not_met():
    expectations = "approved; owner; periodic review"
    llm = [{"expectation": "approved", "met": True, "reason": "ok"}]
    out = build_expectations_checklist(expectations, llm)
    assert len(out) == 3
    assert out[1] == {"expectation": "owner", "met": False, "reason": "Not evaluated"}
    assert out[2]["met"] is False


def test_extra_llm_items_dropped():
    expectations = "approved"
    llm = [
        {"expectation": "approved", "met": True, "reason": "x"},
        {"expectation": "something else entirely", "met": True, "reason": "noise"},
    ]
    out = build_expectations_checklist(expectations, llm)
    assert len(out) == 1
    assert out[0]["expectation"] == "approved"


def test_met_coerced_to_bool():
    out = build_expectations_checklist("approved", [{"expectation": "approved", "met": "yes"}])
    assert out[0]["met"] is True  # truthy string → True
    out2 = build_expectations_checklist("approved", [{"expectation": "approved", "met": 0}])
    assert out2[0]["met"] is False


def test_empty_expectations_returns_empty():
    assert build_expectations_checklist("", [{"expectation": "x", "met": True}]) == []
    assert build_expectations_checklist(None, None) == []


def test_malformed_llm_items_degrade():
    # non-list, dicts missing keys, None entries → never raise
    out = build_expectations_checklist("approved; owner", "not a list")
    assert len(out) == 2
    assert all(c["met"] is False for c in out)
    out2 = build_expectations_checklist("approved", [None, {"no_expectation": 1}])
    assert out2[0]["met"] is False


def test_prefix_match_tolerates_minor_drift():
    # LLM rephrases slightly / truncates — prefix match still reconciles.
    expectations = "approved by management"
    llm = [{"expectation": "approved by management and reviewed", "met": True, "reason": "ok"}]
    out = build_expectations_checklist(expectations, llm)
    assert out[0]["met"] is True


def test_case_and_whitespace_insensitive():
    out = build_expectations_checklist(
        "Approved", [{"expectation": "  approved  ", "met": True, "reason": "x"}]
    )
    assert out[0]["met"] is True
