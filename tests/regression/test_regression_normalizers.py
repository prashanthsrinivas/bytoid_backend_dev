"""Regression tests for tests_routes/normalizers.py output contracts.

These tests lock the public shape of every normalizer so that refactoring
can never silently drop a field the frontend depends on.
"""

import json
import pytest
import tests_routes.normalizers as norm

T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-01T00:01:00+00:00"


# ── Canonical payload shape (shared contract) ─────────────────────────────────

REQUIRED_TOP_LEVEL = {"category", "run_id", "started_at", "finished_at",
                      "duration_seconds", "summary", "status", "tests"}

REQUIRED_SUMMARY_KEYS = {"total", "passed", "failed", "skipped", "errors"}

VALID_STATUSES = {"passed", "failed", "error"}


def _assert_canonical_shape(payload: dict, category: str, run_id: str):
    for key in REQUIRED_TOP_LEVEL:
        assert key in payload, f"Payload missing required key: {key!r}"
    assert payload["category"] == category
    assert payload["run_id"] == run_id
    assert isinstance(payload["duration_seconds"], (int, float))
    assert payload["status"] in ("passed", "failed")
    summary = payload["summary"]
    for k in REQUIRED_SUMMARY_KEYS:
        assert k in summary, f"summary missing key: {k!r}"
    assert isinstance(payload["tests"], list)


# ── parse_pytest_json ─────────────────────────────────────────────────────────

@pytest.mark.regression
def test_regression_parse_pytest_json_shape_with_data():
    raw = {"duration": 1.0, "summary": {"total": 1, "passed": 1, "failed": 0, "error": 0},
           "tests": [{"nodeid": "t", "outcome": "passed", "duration": 1.0, "call": {}}]}
    out = norm.parse_pytest_json(category="backend_unit", run_id="r1",
                                 raw_json=raw, started_at=T0, finished_at=T1, returncode=0)
    _assert_canonical_shape(out, "backend_unit", "r1")

@pytest.mark.regression
def test_regression_parse_pytest_json_shape_empty():
    out = norm.parse_pytest_json(category="backend_unit", run_id="r1",
                                 raw_json=None, started_at=T0, finished_at=T1, returncode=0)
    _assert_canonical_shape(out, "backend_unit", "r1")

@pytest.mark.regression
def test_regression_parse_pytest_test_items_have_required_keys():
    raw = {"duration": 1.0, "summary": {"total": 1, "passed": 1, "failed": 0, "error": 0},
           "tests": [{"nodeid": "test_foo::bar", "outcome": "passed", "duration": 0.1,
                       "call": {"longrepr": None}}]}
    out = norm.parse_pytest_json(category="c", run_id="r",
                                 raw_json=raw, started_at=T0, finished_at=T1, returncode=0)
    t = out["tests"][0]
    assert "name" in t and "outcome" in t and "duration" in t and "message" in t

@pytest.mark.regression
def test_regression_parse_pytest_summary_errors_key_not_error():
    """The canonical summary uses 'errors' (not 'error') as the key."""
    raw = {"duration": 0.5, "summary": {"total": 1, "passed": 0, "failed": 0, "error": 1},
           "tests": []}
    out = norm.parse_pytest_json(category="c", run_id="r",
                                 raw_json=raw, started_at=T0, finished_at=T1, returncode=1)
    assert "errors" in out["summary"]
    assert "error" not in out["summary"]


# ── parse_bandit_json ─────────────────────────────────────────────────────────

@pytest.mark.regression
def test_regression_parse_bandit_json_shape():
    raw = json.dumps({"results": [], "metrics": {}})
    out = norm.parse_bandit_json(category="backend_security_sast", run_id="b1",
                                 raw_text=raw, started_at=T0, finished_at=T1, returncode=0)
    _assert_canonical_shape(out, "backend_security_sast", "b1")
    assert "metrics" in out
    assert out["metrics"]["tool"] == "bandit"

@pytest.mark.regression
def test_regression_bandit_finding_has_security_fields():
    raw = json.dumps({"results": [{
        "test_id": "B605", "issue_severity": "HIGH", "issue_confidence": "HIGH",
        "issue_text": "shell=True", "filename": "f.py", "line_number": 1,
        "issue_cwe": {"id": 78},
    }], "metrics": {}})
    out = norm.parse_bandit_json(category="backend_security_sast", run_id="b1",
                                 raw_text=raw, started_at=T0, finished_at=T1, returncode=1)
    t = out["tests"][0]
    for field in ("name", "outcome", "severity", "cwe", "owasp", "message", "path"):
        assert field in t, f"Finding missing field: {field!r}"

@pytest.mark.regression
def test_regression_bandit_metrics_breakdown_keys():
    raw = json.dumps({"results": [], "metrics": {}})
    out = norm.parse_bandit_json(category="c", run_id="r",
                                 raw_text=raw, started_at=T0, finished_at=T1, returncode=0)
    for key in ("severity_breakdown", "cwe_breakdown", "owasp_breakdown"):
        assert key in out["metrics"], f"metrics missing: {key!r}"


# ── parse_semgrep_sarif ───────────────────────────────────────────────────────

@pytest.mark.regression
def test_regression_parse_semgrep_shape_empty():
    out = norm.parse_semgrep_sarif(category="backend_security_sast", run_id="s1",
                                   raw_text="", started_at=T0, finished_at=T1, returncode=0)
    _assert_canonical_shape(out, "backend_security_sast", "s1")

@pytest.mark.regression
def test_regression_semgrep_finding_has_path_field():
    sarif = json.dumps({"runs": [{"tool": {"driver": {"rules": [
        {"id": "r1", "shortDescription": {"text": "Bad pattern"}}
    ]}}, "results": [{
        "ruleId": "r1", "level": "error",
        "message": {"text": "Issue"},
        "locations": [{"physicalLocation": {"artifactLocation": {"uri": "app.py"},
                                            "region": {"startLine": 5}}}],
    }]}]})
    out = norm.parse_semgrep_sarif(category="c", run_id="r",
                                   raw_text=sarif, started_at=T0, finished_at=T1, returncode=1)
    assert "path" in out["tests"][0]
    assert "app.py" in out["tests"][0]["path"]


# ── parse_coverage_xml ────────────────────────────────────────────────────────

@pytest.mark.regression
def test_regression_parse_coverage_shape():
    xml = ('<?xml version="1.0"?><coverage line-rate="0.85" branch-rate="0.75">'
           '<packages><package><classes>'
           '<class filename="app.py" line-rate="0.85" branch-rate="0.75"></class>'
           '</classes></package></packages></coverage>')
    out = norm.parse_coverage_xml(category="backend_coverage", run_id="c1",
                                  raw_text=xml, started_at=T0, finished_at=T1, returncode=0)
    _assert_canonical_shape(out, "backend_coverage", "c1")
    assert out["metrics"]["tool"] == "pytest-cov"
    assert "line_rate" in out["metrics"]
    assert "branch_rate" in out["metrics"]

@pytest.mark.regression
def test_regression_coverage_file_entry_has_message_with_percentages():
    xml = ('<?xml version="1.0"?><coverage line-rate="0.90" branch-rate="0.80">'
           '<packages><package><classes>'
           '<class filename="svc.py" line-rate="0.90" branch-rate="0.80"></class>'
           '</classes></package></packages></coverage>')
    out = norm.parse_coverage_xml(category="backend_coverage", run_id="c1",
                                  raw_text=xml, started_at=T0, finished_at=T1, returncode=0)
    msg = out["tests"][0]["message"]
    assert "%" in msg


# ── parse_pip_audit_json ──────────────────────────────────────────────────────

@pytest.mark.regression
def test_regression_pip_audit_shape():
    out = norm.parse_pip_audit_json(category="backend_security_deps", run_id="p1",
                                    raw_text="{}", started_at=T0, finished_at=T1, returncode=0)
    _assert_canonical_shape(out, "backend_security_deps", "p1")
    assert out["metrics"]["tool"] == "pip-audit"
    assert "vulnerable_packages" in out["metrics"]


# ── parse_mutmut_results ──────────────────────────────────────────────────────

@pytest.mark.regression
def test_regression_mutmut_shape():
    text = "Killed \U0001f389 5\nSurvived \U0001f641 0"
    out = norm.parse_mutmut_results(category="backend_mutation", run_id="m1",
                                    raw_text=text, started_at=T0, finished_at=T1, returncode=0)
    _assert_canonical_shape(out, "backend_mutation", "m1")
    assert "mutation_score" in out["metrics"]

@pytest.mark.regression
def test_regression_mutmut_score_is_float():
    text = "Killed \U0001f389 7\nSurvived \U0001f641 3"
    out = norm.parse_mutmut_results(category="backend_mutation", run_id="m1",
                                    raw_text=text, started_at=T0, finished_at=T1, returncode=1)
    assert isinstance(out["metrics"]["mutation_score"], float)
    assert 0.0 <= out["metrics"]["mutation_score"] <= 100.0


# ── make_failed_payload ───────────────────────────────────────────────────────

@pytest.mark.regression
def test_regression_make_failed_payload_shape():
    out = norm.make_failed_payload(category="backend_unit", run_id="x",
                                   error="runner crash",
                                   started_at=T0, finished_at=T1)
    _assert_canonical_shape(out, "backend_unit", "x")
    assert out["status"] == "failed"


# ── _status_from_summary invariants ──────────────────────────────────────────

@pytest.mark.regression
def test_regression_status_from_summary_never_returns_unknown():
    for f in (0, 1, 5):
        for e in (0, 1):
            result = norm._status_from_summary({"failed": f, "errors": e, "total": f + e})
            assert result in ("passed", "failed")


# ── _severity_to_outcome invariants ──────────────────────────────────────────

@pytest.mark.regression
def test_regression_severity_to_outcome_returns_only_valid_values():
    for sev in ("critical", "high", "medium", "low", "info", "warning", "error", "unknown", ""):
        result = norm._severity_to_outcome(sev)
        assert result in ("failed", "error", "skipped"), f"Unexpected outcome for {sev!r}: {result!r}"
