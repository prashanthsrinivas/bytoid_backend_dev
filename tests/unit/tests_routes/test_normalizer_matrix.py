"""Massively parametrized matrix tests: every parser × every category × every
severity × every returncode → no crashes, valid envelope shape.
"""

import json
import pytest

import tests_routes.normalizers as norm
from tests_routes.categories import ALL_CATEGORIES

T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-01T00:01:00+00:00"

PARSERS = [
    ("bandit",   norm.parse_bandit_json),
    ("semgrep",  norm.parse_semgrep_sarif),
    ("gitleaks", norm.parse_gitleaks_sarif),
    ("pipaudit", norm.parse_pip_audit_json),
    ("safety",   norm.parse_safety_json),
    ("coverage", norm.parse_coverage_xml),
    ("mypy",     norm.parse_mypy_json),
    ("pylint",   norm.parse_pylint_json),
    ("ruff",     norm.parse_ruff_sarif),
    ("mutmut",   norm.parse_mutmut_results),
]

CATEGORIES = list(ALL_CATEGORIES.keys())
RETURNCODES = [0, 1]


# ── matrix: parser × category × rc, expecting no crash ───────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("name,fn", PARSERS)
@pytest.mark.parametrize("category", CATEGORIES)
@pytest.mark.parametrize("rc", RETURNCODES)
def test_parser_category_rc_matrix_envelope(name, fn, category, rc):
    """Every parser, every category, returncode 0/1: returns a valid envelope."""
    out = fn(category=category, run_id="m-run", raw_text="{}",
             started_at=T0, finished_at=T1, returncode=rc)
    assert out["category"] == category
    assert "summary" in out and isinstance(out["summary"], dict)
    assert "status" in out
    assert "tests" in out and isinstance(out["tests"], list)


# ── matrix: parser × category × rc, raw_text="" ──────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("name,fn", PARSERS)
@pytest.mark.parametrize("category", CATEGORIES)
def test_parser_category_empty_text(name, fn, category):
    out = fn(category=category, run_id="m-run", raw_text="",
             started_at=T0, finished_at=T1, returncode=0)
    assert out["category"] == category


# ── matrix: bandit severities × test_id mapping ──────────────────────────────

BANDIT_TEST_IDS = list(norm._BANDIT_CWE.keys())
BANDIT_SEVS = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]

@pytest.mark.unit
@pytest.mark.parametrize("test_id", BANDIT_TEST_IDS)
@pytest.mark.parametrize("sev", BANDIT_SEVS)
def test_bandit_test_id_sev_matrix(test_id, sev):
    raw = json.dumps({"results": [{
        "test_id": test_id, "issue_severity": sev,
        "issue_confidence": "HIGH", "issue_text": "x",
        "filename": "f.py", "line_number": 1, "issue_cwe": {},
    }]})
    out = norm.parse_bandit_json(
        category="backend_security_sast", run_id="r",
        raw_text=raw, started_at=T0, finished_at=T1, returncode=0,
    )
    assert out["tests"][0]["cwe"] == norm._BANDIT_CWE[test_id]
    expected_outcome = {"LOW": "skipped", "MEDIUM": "error",
                        "HIGH": "failed", "CRITICAL": "failed"}[sev]
    assert out["tests"][0]["outcome"] == expected_outcome


# ── matrix: every pip-audit severity × fix_versions shape ────────────────────

PIP_SEVS = ["critical", "high", "medium", "low", "info"]
FIX_VERSIONS = [[], ["1.0.0"], ["1.0.0", "2.0.0"], ["latest"]]

@pytest.mark.unit
@pytest.mark.parametrize("sev", PIP_SEVS)
@pytest.mark.parametrize("fix", FIX_VERSIONS)
def test_pip_audit_sev_fix_matrix(sev, fix):
    raw = json.dumps({"dependencies": [{
        "name": "pkg", "version": "0.1",
        "vulns": [{"id": "G", "severity": sev, "fix_versions": fix, "description": ""}],
    }]})
    out = norm.parse_pip_audit_json(
        category="backend_security_deps", run_id="r",
        raw_text=raw, started_at=T0, finished_at=T1, returncode=0,
    )
    assert len(out["tests"]) == 1
    if fix:
        for v in fix:
            assert v in out["tests"][0]["remediation"]


# ── matrix: SARIF level × tool ────────────────────────────────────────────────

SARIF_TOOLS = [
    ("semgrep", norm.parse_semgrep_sarif),
    ("gitleaks", norm.parse_gitleaks_sarif),
    ("ruff", norm.parse_ruff_sarif),
]
SARIF_LEVELS = ["error", "warning", "note"]

@pytest.mark.unit
@pytest.mark.parametrize("tool,fn", SARIF_TOOLS)
@pytest.mark.parametrize("level", SARIF_LEVELS)
def test_sarif_tool_level_matrix(tool, fn, level):
    sarif = json.dumps({"runs": [{
        "tool": {"driver": {"rules": [{"id": "r1"}]}},
        "results": [{
            "ruleId": "r1", "level": level,
            "message": {"text": "x"},
            "locations": [{"physicalLocation": {
                "artifactLocation": {"uri": "f.py"},
                "region": {"startLine": 1},
            }}],
        }],
    }]})
    out = fn(category="backend_security_sast", run_id="r",
             raw_text=sarif, started_at=T0, finished_at=T1, returncode=0)
    assert out["summary"]["total"] >= 1


# ── matrix: coverage XML at every line-rate × branch-rate combo ──────────────

LINE_RATES = [0.0, 0.1, 0.3, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
BRANCH_RATES = [0.0, 0.3, 0.6, 0.8, 1.0]

@pytest.mark.unit
@pytest.mark.parametrize("lr", LINE_RATES)
@pytest.mark.parametrize("br", BRANCH_RATES)
def test_coverage_lr_br_matrix(lr, br):
    xml = (
        f'<?xml version="1.0"?>'
        f'<coverage line-rate="{lr}" branch-rate="{br}">'
        f'<packages><package><classes>'
        f'<class filename="f.py" line-rate="{lr}" branch-rate="{br}"/>'
        f'</classes></package></packages></coverage>'
    )
    out = norm.parse_coverage_xml(
        category="backend_coverage", run_id="r",
        raw_text=xml, started_at=T0, finished_at=T1, returncode=0,
    )
    if lr < 0.60:
        assert out["tests"][0]["outcome"] == "failed"
    elif lr < 0.80:
        assert out["tests"][0]["outcome"] == "error"
    else:
        assert out["tests"][0]["outcome"] == "passed"


# ── matrix: mutmut killed × survived combinations ────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("killed", [0, 1, 5, 10, 50, 100])
@pytest.mark.parametrize("survived", [0, 1, 5, 10])
def test_mutmut_matrix(killed, survived):
    text = f"Killed 🎉 {killed}\nSurvived 🙁 {survived}\nTimed out ⏰ 0"
    out = norm.parse_mutmut_results(
        category="backend_mutation", run_id="r",
        raw_text=text, started_at=T0, finished_at=T1, returncode=0,
    )
    assert out["summary"]["passed"] == killed
    assert out["summary"]["failed"] == survived
    if killed + survived > 0:
        expected_score = round(killed / (killed + survived) * 100, 2)
        assert abs(out["metrics"]["mutation_score"] - expected_score) < 0.5
