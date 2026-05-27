"""Unit tests for tests_routes/normalizers.py.

All normalizer functions are pure (stdlib only) — no stubs required.
"""

import json
import pytest

import tests_routes.normalizers as norm

T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-01T00:01:00+00:00"
CAT = "backend_unit"
RUN = "run-001"


# ── _status_from_summary ──────────────────────────────────────────────────────

@pytest.mark.unit
def test_status_from_summary_passed():
    assert norm._status_from_summary({"failed": 0, "errors": 0}) == "passed"

@pytest.mark.unit
def test_status_from_summary_failed_when_failures():
    assert norm._status_from_summary({"failed": 1, "errors": 0}) == "failed"

@pytest.mark.unit
def test_status_from_summary_failed_when_errors():
    assert norm._status_from_summary({"failed": 0, "errors": 2}) == "failed"

@pytest.mark.unit
def test_status_from_summary_failed_when_nonzero_rc_and_no_tests():
    assert norm._status_from_summary({"failed": 0, "errors": 0, "total": 0}, returncode=1) == "failed"

@pytest.mark.unit
def test_status_from_summary_passed_nonzero_rc_with_tests():
    assert norm._status_from_summary({"failed": 0, "errors": 0, "total": 5}, returncode=1) == "passed"


# ── _severity_to_outcome ──────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("sev,expected", [
    ("critical", "failed"),
    ("high",     "failed"),
    ("error",    "failed"),
    ("medium",   "error"),
    ("warning",  "error"),
    ("low",      "skipped"),
    ("info",     "skipped"),
    ("",         "skipped"),
    ("CRITICAL", "failed"),
    ("HIGH",     "failed"),
])
def test_severity_to_outcome(sev, expected):
    assert norm._severity_to_outcome(sev) == expected


# ── _sarif_level_to_severity ──────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("level,expected", [
    ("error",   "high"),
    ("warning", "medium"),
    ("note",    "low"),
    ("",        "medium"),
    ("unknown", "medium"),
])
def test_sarif_level_to_severity(level, expected):
    assert norm._sarif_level_to_severity(level) == expected


# ── _summary_from_findings ────────────────────────────────────────────────────

@pytest.mark.unit
def test_summary_from_findings_empty():
    s = norm._summary_from_findings([])
    assert s == {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 0}

@pytest.mark.unit
def test_summary_from_findings_mixed():
    findings = [
        {"outcome": "failed"},
        {"outcome": "failed"},
        {"outcome": "error"},
        {"outcome": "skipped"},
    ]
    s = norm._summary_from_findings(findings)
    assert s["total"] == 4
    assert s["failed"] == 2
    assert s["errors"] == 1
    assert s["skipped"] == 1
    assert s["passed"] == 0


# ── _status_from_findings ─────────────────────────────────────────────────────

@pytest.mark.unit
def test_status_from_findings_no_high():
    findings = [{"severity": "low"}, {"severity": "medium"}]
    assert norm._status_from_findings(findings, threshold="high") == "passed"

@pytest.mark.unit
def test_status_from_findings_high_present():
    findings = [{"severity": "high"}]
    assert norm._status_from_findings(findings, threshold="high") == "failed"

@pytest.mark.unit
def test_status_from_findings_critical_threshold():
    findings = [{"severity": "high"}]
    assert norm._status_from_findings(findings, threshold="critical") == "passed"


# ── parse_pytest_json ─────────────────────────────────────────────────────────

@pytest.mark.unit
def test_parse_pytest_json_none_returns_failed_empty():
    out = norm.parse_pytest_json(
        category=CAT, run_id=RUN, raw_json=None,
        started_at=T0, finished_at=T1, returncode=0,
    )
    assert out["status"] == "failed"
    assert out["summary"]["total"] == 0
    assert out["tests"] == []

@pytest.mark.unit
def test_parse_pytest_json_all_passed():
    raw = {
        "duration": 1.5,
        "summary": {"total": 3, "passed": 3, "failed": 0, "error": 0},
        "tests": [
            {"nodeid": "test_a", "outcome": "passed", "duration": 0.5, "call": {}},
            {"nodeid": "test_b", "outcome": "passed", "duration": 0.5, "call": {}},
            {"nodeid": "test_c", "outcome": "passed", "duration": 0.5, "call": {}},
        ],
    }
    out = norm.parse_pytest_json(
        category=CAT, run_id=RUN, raw_json=raw,
        started_at=T0, finished_at=T1, returncode=0,
    )
    assert out["status"] == "passed"
    assert out["summary"]["passed"] == 3
    assert out["summary"]["failed"] == 0
    assert len(out["tests"]) == 3
    assert out["tests"][0]["name"] == "test_a"

@pytest.mark.unit
def test_parse_pytest_json_with_failures():
    raw = {
        "duration": 2.0,
        "summary": {"total": 2, "passed": 1, "failed": 1, "error": 0},
        "tests": [
            {"nodeid": "test_ok", "outcome": "passed", "duration": 1.0, "call": {}},
            {"nodeid": "test_fail", "outcome": "failed", "duration": 1.0,
             "call": {"longrepr": "AssertionError: expected True got False"}},
        ],
    }
    out = norm.parse_pytest_json(
        category=CAT, run_id=RUN, raw_json=raw,
        started_at=T0, finished_at=T1, returncode=1,
    )
    assert out["status"] == "failed"
    assert out["tests"][1]["message"] == "AssertionError: expected True got False"

@pytest.mark.unit
def test_parse_pytest_json_error_key_maps_to_errors():
    raw = {
        "duration": 0.1,
        "summary": {"total": 1, "passed": 0, "failed": 0, "error": 1},
        "tests": [],
    }
    out = norm.parse_pytest_json(
        category=CAT, run_id=RUN, raw_json=raw,
        started_at=T0, finished_at=T1, returncode=1,
    )
    assert out["summary"]["errors"] == 1

@pytest.mark.unit
def test_parse_pytest_json_empty_dict_is_failed():
    out = norm.parse_pytest_json(
        category=CAT, run_id=RUN, raw_json={},
        started_at=T0, finished_at=T1, returncode=1,
    )
    assert out["status"] == "failed"

@pytest.mark.unit
def test_parse_pytest_json_preserves_category_and_run_id():
    out = norm.parse_pytest_json(
        category="backend_security_authz", run_id="my-run",
        raw_json={"summary": {"total": 0, "passed": 0, "failed": 0, "error": 0}, "tests": []},
        started_at=T0, finished_at=T1, returncode=0,
    )
    assert out["category"] == "backend_security_authz"
    assert out["run_id"] == "my-run"


# ── parse_bandit_json ─────────────────────────────────────────────────────────

def _bandit_raw(results):
    return json.dumps({"results": results, "metrics": {}})

@pytest.mark.unit
def test_parse_bandit_json_empty():
    out = norm.parse_bandit_json(
        category="backend_security_sast", run_id=RUN,
        raw_text="", started_at=T0, finished_at=T1, returncode=0,
    )
    assert out["summary"]["total"] == 0
    assert out["status"] == "passed"

@pytest.mark.unit
def test_parse_bandit_json_high_finding_is_failed():
    raw = _bandit_raw([{
        "test_id": "B605", "issue_severity": "HIGH",
        "issue_confidence": "HIGH", "issue_text": "subprocess call with shell=True",
        "filename": "app.py", "line_number": 10, "issue_cwe": {"id": 78},
    }])
    out = norm.parse_bandit_json(
        category="backend_security_sast", run_id=RUN,
        raw_text=raw, started_at=T0, finished_at=T1, returncode=1,
    )
    assert out["status"] == "failed"
    assert out["summary"]["failed"] == 1
    assert out["tests"][0]["severity"] == "high"
    assert out["tests"][0]["cwe"] == "CWE-78"

@pytest.mark.unit
def test_parse_bandit_json_medium_only_is_passed():
    raw = _bandit_raw([{
        "test_id": "B311", "issue_severity": "MEDIUM",
        "issue_confidence": "LOW", "issue_text": "random not suitable for security",
        "filename": "utils.py", "line_number": 5, "issue_cwe": {},
    }])
    out = norm.parse_bandit_json(
        category="backend_security_sast", run_id=RUN,
        raw_text=raw, started_at=T0, finished_at=T1, returncode=0,
    )
    assert out["status"] == "passed"
    assert out["summary"]["errors"] == 1

@pytest.mark.unit
def test_parse_bandit_json_nonzero_rc_no_results_is_failed():
    out = norm.parse_bandit_json(
        category="backend_security_sast", run_id=RUN,
        raw_text=_bandit_raw([]), started_at=T0, finished_at=T1, returncode=1,
    )
    assert out["status"] == "failed"

@pytest.mark.unit
def test_parse_bandit_json_metrics_structure():
    raw = _bandit_raw([{
        "test_id": "B608", "issue_severity": "HIGH",
        "issue_confidence": "HIGH", "issue_text": "SQL injection",
        "filename": "db.py", "line_number": 42, "issue_cwe": {"id": 89},
    }])
    out = norm.parse_bandit_json(
        category="backend_security_sast", run_id=RUN,
        raw_text=raw, started_at=T0, finished_at=T1, returncode=1,
    )
    assert out["metrics"]["tool"] == "bandit"
    assert "severity_breakdown" in out["metrics"]
    assert "cwe_breakdown" in out["metrics"]
    assert "owasp_breakdown" in out["metrics"]

@pytest.mark.unit
def test_parse_bandit_json_invalid_json_returns_empty():
    out = norm.parse_bandit_json(
        category="backend_security_sast", run_id=RUN,
        raw_text="not json", started_at=T0, finished_at=T1, returncode=0,
    )
    assert out["summary"]["total"] == 0


# ── parse_semgrep_sarif ───────────────────────────────────────────────────────

def _sarif(rules, results):
    return json.dumps({"runs": [{"tool": {"driver": {"rules": rules}}, "results": results}]})

@pytest.mark.unit
def test_parse_semgrep_sarif_empty():
    out = norm.parse_semgrep_sarif(
        category="backend_security_sast", run_id=RUN,
        raw_text="", started_at=T0, finished_at=T1, returncode=0,
    )
    assert out["summary"]["total"] == 0

@pytest.mark.unit
def test_parse_semgrep_sarif_error_level_finding():
    sarif = _sarif(
        rules=[{"id": "rule1", "shortDescription": {"text": "SQL injection"}}],
        results=[{
            "ruleId": "rule1",
            "level": "error",
            "message": {"text": "User input in SQL query"},
            "locations": [{"physicalLocation": {"artifactLocation": {"uri": "db.py"},
                                                "region": {"startLine": 10}}}],
        }],
    )
    out = norm.parse_semgrep_sarif(
        category="backend_security_sast", run_id=RUN,
        raw_text=sarif, started_at=T0, finished_at=T1, returncode=1,
    )
    assert out["summary"]["failed"] >= 1
    assert out["tests"][0]["outcome"] == "failed"

@pytest.mark.unit
def test_parse_semgrep_sarif_warning_level_is_error_outcome():
    sarif = _sarif(
        rules=[{"id": "r2", "shortDescription": {"text": "weak hash"}}],
        results=[{
            "ruleId": "r2",
            "level": "warning",
            "message": {"text": "MD5 usage"},
            "locations": [{"physicalLocation": {"artifactLocation": {"uri": "crypto.py"},
                                                "region": {"startLine": 5}}}],
        }],
    )
    out = norm.parse_semgrep_sarif(
        category="backend_security_sast", run_id=RUN,
        raw_text=sarif, started_at=T0, finished_at=T1, returncode=0,
    )
    assert out["tests"][0]["outcome"] == "error"


# ── parse_pip_audit_json ──────────────────────────────────────────────────────

@pytest.mark.unit
def test_parse_pip_audit_json_no_vulns():
    raw = json.dumps({"dependencies": [{"name": "flask", "version": "2.0.0", "vulns": []}]})
    out = norm.parse_pip_audit_json(
        category="backend_security_deps", run_id=RUN,
        raw_text=raw, started_at=T0, finished_at=T1, returncode=0,
    )
    assert out["summary"]["total"] == 0
    assert out["status"] == "passed"

@pytest.mark.unit
def test_parse_pip_audit_json_critical_vuln():
    raw = json.dumps({"dependencies": [{
        "name": "requests", "version": "2.20.0",
        "vulns": [{"id": "GHSA-abcd", "severity": "critical",
                   "fix_versions": ["2.28.0"], "description": "SSRF vulnerability"}],
    }]})
    out = norm.parse_pip_audit_json(
        category="backend_security_deps", run_id=RUN,
        raw_text=raw, started_at=T0, finished_at=T1, returncode=1,
    )
    assert out["summary"]["failed"] == 1
    assert out["status"] == "failed"
    assert "requests==2.20.0" in out["tests"][0]["name"]

@pytest.mark.unit
def test_parse_pip_audit_json_medium_vuln_doesnt_fail():
    raw = json.dumps({"dependencies": [{
        "name": "pillow", "version": "9.0.0",
        "vulns": [{"id": "GHSA-xxxx", "severity": "medium", "fix_versions": [], "description": ""}],
    }]})
    out = norm.parse_pip_audit_json(
        category="backend_security_deps", run_id=RUN,
        raw_text=raw, started_at=T0, finished_at=T1, returncode=0,
    )
    assert out["status"] == "passed"
    assert out["summary"]["errors"] == 1

@pytest.mark.unit
def test_parse_pip_audit_json_fix_versions_in_remediation():
    raw = json.dumps({"dependencies": [{
        "name": "cryptography", "version": "3.0.0",
        "vulns": [{"id": "GHSA-yyyy", "severity": "high",
                   "fix_versions": ["41.0.0"], "description": "Weak key generation"}],
    }]})
    out = norm.parse_pip_audit_json(
        category="backend_security_deps", run_id=RUN,
        raw_text=raw, started_at=T0, finished_at=T1, returncode=1,
    )
    assert "41.0.0" in out["tests"][0]["remediation"]

@pytest.mark.unit
def test_parse_pip_audit_json_vulnerable_packages_metric():
    raw = json.dumps({"dependencies": [
        {"name": "pkg-a", "version": "1.0", "vulns": [{"id": "G1", "severity": "high", "fix_versions": []}]},
        {"name": "pkg-b", "version": "2.0", "vulns": [{"id": "G2", "severity": "medium", "fix_versions": []}]},
    ]})
    out = norm.parse_pip_audit_json(
        category="backend_security_deps", run_id=RUN,
        raw_text=raw, started_at=T0, finished_at=T1, returncode=0,
    )
    assert set(out["metrics"]["vulnerable_packages"]) == {"pkg-a", "pkg-b"}


# ── parse_coverage_xml ────────────────────────────────────────────────────────

def _cov_xml(line_rate=0.85, branch_rate=0.75, classes=None):
    cls_xml = ""
    for fname, lr, br in (classes or []):
        cls_xml += f'<class filename="{fname}" line-rate="{lr}" branch-rate="{br}"></class>'
    return (
        f'<?xml version="1.0"?>'
        f'<coverage line-rate="{line_rate}" branch-rate="{branch_rate}">'
        f'<packages><package><classes>{cls_xml}</classes></package></packages>'
        f'</coverage>'
    )

@pytest.mark.unit
def test_parse_coverage_xml_empty_text():
    out = norm.parse_coverage_xml(
        category="backend_coverage", run_id=RUN,
        raw_text="", started_at=T0, finished_at=T1, returncode=0,
    )
    assert out["summary"]["total"] == 0
    assert out["status"] == "failed"

@pytest.mark.unit
def test_parse_coverage_xml_high_coverage_passed():
    out = norm.parse_coverage_xml(
        category="backend_coverage", run_id=RUN,
        raw_text=_cov_xml(0.90, 0.80, [("app.py", 0.90, 0.80)]),
        started_at=T0, finished_at=T1, returncode=0,
    )
    assert out["status"] == "passed"
    assert out["tests"][0]["outcome"] == "passed"

@pytest.mark.unit
def test_parse_coverage_xml_low_coverage_failed():
    out = norm.parse_coverage_xml(
        category="backend_coverage", run_id=RUN,
        raw_text=_cov_xml(0.50, 0.30, [("db.py", 0.50, 0.30)]),
        started_at=T0, finished_at=T1, returncode=0,
    )
    assert out["tests"][0]["outcome"] == "failed"

@pytest.mark.unit
def test_parse_coverage_xml_mid_coverage_is_error():
    out = norm.parse_coverage_xml(
        category="backend_coverage", run_id=RUN,
        raw_text=_cov_xml(0.70, 0.60, [("utils.py", 0.70, 0.60)]),
        started_at=T0, finished_at=T1, returncode=0,
    )
    assert out["tests"][0]["outcome"] == "error"

@pytest.mark.unit
def test_parse_coverage_xml_invalid_xml_is_graceful():
    out = norm.parse_coverage_xml(
        category="backend_coverage", run_id=RUN,
        raw_text="<bad xml", started_at=T0, finished_at=T1, returncode=0,
    )
    assert out["summary"]["total"] == 0

@pytest.mark.unit
def test_parse_coverage_xml_message_format():
    out = norm.parse_coverage_xml(
        category="backend_coverage", run_id=RUN,
        raw_text=_cov_xml(0.85, 0.75, [("routes.py", 0.85, 0.75)]),
        started_at=T0, finished_at=T1, returncode=0,
    )
    assert "line=85.0%" in out["tests"][0]["message"]
    assert "branch=75.0%" in out["tests"][0]["message"]


# ── parse_mutmut_results ──────────────────────────────────────────────────────

@pytest.mark.unit
def test_parse_mutmut_results_all_killed():
    text = "Killed \U0001f389 10\nSurvived \U0001f641 0\nTimed out ⏰ 0"
    out = norm.parse_mutmut_results(
        category="backend_mutation", run_id=RUN,
        raw_text=text, started_at=T0, finished_at=T1, returncode=0,
    )
    assert out["status"] == "passed"
    assert out["summary"]["passed"] == 10
    assert out["summary"]["failed"] == 0

@pytest.mark.unit
def test_parse_mutmut_results_some_survived():
    text = "Killed \U0001f389 8\nSurvived \U0001f641 2\nTimed out ⏰ 0"
    out = norm.parse_mutmut_results(
        category="backend_mutation", run_id=RUN,
        raw_text=text, started_at=T0, finished_at=T1, returncode=1,
    )
    assert out["status"] == "failed"
    assert out["summary"]["failed"] == 2
    assert out["summary"]["passed"] == 8
    score = out["metrics"]["mutation_score"]
    assert abs(score - 80.0) < 0.1

@pytest.mark.unit
def test_parse_mutmut_results_empty_text():
    out = norm.parse_mutmut_results(
        category="backend_mutation", run_id=RUN,
        raw_text="", started_at=T0, finished_at=T1, returncode=0,
    )
    assert out["summary"]["total"] == 0

@pytest.mark.unit
def test_parse_mutmut_results_mutation_score_100_when_all_killed():
    text = "Killed \U0001f389 5\nSurvived \U0001f641 0"
    out = norm.parse_mutmut_results(
        category="backend_mutation", run_id=RUN,
        raw_text=text, started_at=T0, finished_at=T1, returncode=0,
    )
    assert out["metrics"]["mutation_score"] == 100.0


# ── parse_safety_json ─────────────────────────────────────────────────────────

@pytest.mark.unit
def test_parse_safety_json_empty():
    out = norm.parse_safety_json(
        category="backend_security_deps", run_id=RUN,
        raw_text="{}", started_at=T0, finished_at=T1, returncode=0,
    )
    assert out["summary"]["total"] == 0

@pytest.mark.unit
def test_parse_safety_json_critical_vuln():
    raw = json.dumps({"vulnerabilities": [{
        "vulnerability_id": "54321", "package_name": "django",
        "analyzed_version": "3.0.0", "severity": "critical",
        "advisory": "Remote code execution", "more_info_url": "https://safety.db/54321",
    }]})
    out = norm.parse_safety_json(
        category="backend_security_deps", run_id=RUN,
        raw_text=raw, started_at=T0, finished_at=T1, returncode=1,
    )
    assert out["summary"]["failed"] == 1
    assert out["status"] == "failed"

@pytest.mark.unit
def test_parse_safety_json_scan_results_key():
    raw = json.dumps({"scan_results": {"vulnerabilities": [{
        "vulnerability_id": "99999", "package_name": "pyyaml",
        "analyzed_version": "5.0", "severity": "high",
        "advisory": "Arbitrary code execution via yaml.load",
    }]}})
    out = norm.parse_safety_json(
        category="backend_security_deps", run_id=RUN,
        raw_text=raw, started_at=T0, finished_at=T1, returncode=1,
    )
    assert out["summary"]["total"] == 1


# ── make_failed_payload ───────────────────────────────────────────────────────

@pytest.mark.unit
def test_make_failed_payload_shape():
    out = norm.make_failed_payload(
        category="backend_unit", run_id="x", error="boom",
        started_at=T0, finished_at=T1,
    )
    assert out["status"] == "failed"
    assert out["summary"]["errors"] >= 1
    assert out["category"] == "backend_unit"

@pytest.mark.unit
def test_make_failed_payload_contains_error():
    out = norm.make_failed_payload(
        category="backend_unit", run_id="x", error="timeout after 600s",
        started_at=T0, finished_at=T1,
    )
    assert "timeout after 600s" in out["error"]
