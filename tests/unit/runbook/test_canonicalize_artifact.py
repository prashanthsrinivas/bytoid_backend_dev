"""Artifact-name canonicalization for the evidence auto-fill matcher.

The Intake Workflow "Upload Files to Auto-fill" pipeline classifies each file to
one artifact via an LLM, then compares that name with EXACT string equality at
every downstream step (runbook allow/deny, ``evidence_required`` intersection,
expectations lookup). An LLM that paraphrases the config's ``"Policies"`` /
``"Screenshot"`` would be silently dropped -> "0 questions fulfilled".
``canonicalize_artifact_name`` snaps a free-form name back to a known config
name (or None), so this proves the realistic drift cases map correctly and junk
maps to None.
"""

from __future__ import annotations

import pytest

# Dependency-free module — imports without the heavy workflow_service chain.
from runbook.evidence_overview import canonicalize_artifact_name

pytestmark = pytest.mark.unit


# Representative slice of config_evidences/evidence_default.json artifact names.
KNOWN = [
    "Policies",
    "SOP / procedure",
    "Runbook / playbook",
    "System log file",
    "Screenshot",
    "Scan output",
    "Ticket export",
    "Email approval",
]
CFG = [{"artifact": n} for n in KNOWN]


@pytest.mark.parametrize(
    "raw,expected",
    [
        # exact / case / whitespace
        ("Policies", "Policies"),
        ("policies", "Policies"),
        ("  POLICIES  ", "Policies"),
        # pluralization / singular
        ("Policy", "Policies"),
        ("Screenshots", "Screenshot"),
        # punctuation drift
        ("SOP/procedure", "SOP / procedure"),
        ("sop  procedure", "SOP / procedure"),
        # extra qualifier words (containment + fuzzy)
        ("system screenshot", "Screenshot"),
        ("screenshot of the access dashboard", "Screenshot"),
        ("system log", "System log file"),
        ("system log export", "System log file"),
        ("runbook", "Runbook / playbook"),
        ("email approval record", "Email approval"),
    ],
)
def test_drift_maps_to_canonical(raw, expected):
    assert canonicalize_artifact_name(raw, CFG) == expected


@pytest.mark.parametrize("raw", ["", "   ", None, "a", "it", "completely unrelated thing xyz"])
def test_no_confident_match_returns_none(raw):
    assert canonicalize_artifact_name(raw, CFG) is None


def test_accepts_bare_string_list():
    # known_artifacts may be plain strings, not just config dicts.
    assert canonicalize_artifact_name("Policy", KNOWN) == "Policies"


def test_empty_known_list_returns_none():
    assert canonicalize_artifact_name("Policies", []) is None
    assert canonicalize_artifact_name("Policies", None) is None


def test_longest_specific_key_wins():
    # "system log file" must beat a hypothetical short generic key on containment.
    cfg = [{"artifact": "Log"}, {"artifact": "System log file"}]
    assert canonicalize_artifact_name("system log file export", cfg) == "System log file"
