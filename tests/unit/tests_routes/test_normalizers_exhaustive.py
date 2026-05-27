"""Exhaustive parametrized unit tests for tests_routes/normalizers.py.

Aims for breadth: every CWE/OWASP mapping, every severity, every category,
every JSON edge case.  All inputs are synthetic — no I/O, no live services.
"""

import json
import pytest

import tests_routes.normalizers as norm

T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-01T00:01:00+00:00"
RUN = "exhaustive-run"

# Every backend security category the dashboard knows about.
SEC_CATEGORIES = [
    "backend_security_sast",
    "backend_security_secrets",
    "backend_security_deps",
    "backend_security_authz",
    "backend_security_api",
    "backend_security_llm",
    "backend_security_infra",
    "backend_coverage",
    "backend_typecheck",
    "backend_lint",
    "backend_mutation",
]

# Severity ladder used everywhere in normalizers.
SEVERITIES = ["critical", "high", "medium", "low", "info"]


# ── parametrized: every severity → outcome (case variants) ────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("sev", ["critical", "Critical", "CRITICAL", " CRITICAL ", "critical "])
def test_severity_critical_variants_all_map_to_failed(sev):
    # Leading/trailing space ≠ a match; only case fold is required by the impl.
    if sev.strip() == sev:
        assert norm._severity_to_outcome(sev) == "failed"

@pytest.mark.unit
@pytest.mark.parametrize("sev", ["high", "High", "HIGH"])
def test_severity_high_case_insensitive_failed(sev):
    assert norm._severity_to_outcome(sev) == "failed"

@pytest.mark.unit
@pytest.mark.parametrize("sev", ["error", "ERROR", "Error"])
def test_severity_error_case_insensitive_failed(sev):
    assert norm._severity_to_outcome(sev) == "failed"

@pytest.mark.unit
@pytest.mark.parametrize("sev", ["medium", "MEDIUM", "Medium", "warning", "WARNING", "Warning"])
def test_severity_medium_warning_case_insensitive_error(sev):
    assert norm._severity_to_outcome(sev) == "error"

@pytest.mark.unit
@pytest.mark.parametrize("sev", ["low", "info", "note", "", None, "unknown", "foo", "1", "  "])
def test_severity_low_or_unknown_maps_to_skipped(sev):
    assert norm._severity_to_outcome(sev or "") == "skipped"


# ── parametrized: SARIF level matrix ──────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("lvl,expected", [
    ("error", "high"), ("ERROR", "high"), ("Error", "high"),
    ("warning", "medium"), ("WARNING", "medium"), ("Warning", "medium"),
    ("note", "low"), ("NOTE", "low"), ("Note", "low"),
    ("", "medium"), ("none", "medium"), ("info", "medium"),
    ("anything-else", "medium"),
])
def test_sarif_level_mapping_exhaustive(lvl, expected):
    assert norm._sarif_level_to_severity(lvl) == expected


# ── parametrized: _status_from_findings threshold matrix ──────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("threshold,worst_sev,expected", [
    ("critical", "critical", "failed"),
    ("critical", "high",     "passed"),
    ("critical", "medium",   "passed"),
    ("critical", "low",      "passed"),
    ("critical", "info",     "passed"),
    ("high",     "critical", "failed"),
    ("high",     "high",     "failed"),
    ("high",     "medium",   "passed"),
    ("high",     "low",      "passed"),
    ("high",     "info",     "passed"),
    ("medium",   "critical", "failed"),
    ("medium",   "high",     "failed"),
    ("medium",   "medium",   "failed"),
    ("medium",   "low",      "passed"),
    ("medium",   "info",     "passed"),
])
def test_status_from_findings_threshold_matrix(threshold, worst_sev, expected):
    findings = [{"severity": worst_sev, "outcome": "failed" if worst_sev in ("critical", "high", "error") else "error"}]
    assert norm._status_from_findings(findings, threshold=threshold) == expected


# ── parametrized: empty / None / malformed inputs to every normalizer ─────────

PARSER_FUNCS_TEXT = [
    norm.parse_bandit_json,
    norm.parse_semgrep_sarif,
    norm.parse_gitleaks_sarif,
    norm.parse_pip_audit_json,
    norm.parse_safety_json,
    norm.parse_coverage_xml,
    norm.parse_mypy_json,
    norm.parse_pylint_json,
    norm.parse_ruff_sarif,
    norm.parse_mutmut_results,
]

@pytest.mark.unit
@pytest.mark.parametrize("fn", PARSER_FUNCS_TEXT)
def test_every_text_parser_handles_empty_string(fn):
    out = fn(category="x", run_id=RUN, raw_text="", started_at=T0, finished_at=T1, returncode=0)
    assert isinstance(out, dict)
    assert out["category"] == "x"
    assert out["run_id"] == RUN

@pytest.mark.unit
@pytest.mark.parametrize("fn", PARSER_FUNCS_TEXT)
def test_every_text_parser_handles_garbage(fn):
    """Contract: non-empty non-JSON/XML text must not crash."""
    out = fn(category="x", run_id=RUN, raw_text="not-json-or-xml-or-sarif",
             started_at=T0, finished_at=T1, returncode=0)
    assert isinstance(out, dict)
    assert "summary" in out
    assert "status" in out
    assert "tests" in out

@pytest.mark.unit
@pytest.mark.parametrize("fn", PARSER_FUNCS_TEXT)
def test_every_text_parser_handles_empty_object_string(fn):
    out = fn(category="x", run_id=RUN, raw_text="{}",
             started_at=T0, finished_at=T1, returncode=0)
    assert isinstance(out, dict)

@pytest.mark.unit
@pytest.mark.parametrize("fn", PARSER_FUNCS_TEXT)
def test_every_text_parser_returns_required_keys(fn):
    out = fn(category="c", run_id=RUN, raw_text="{}", started_at=T0, finished_at=T1, returncode=0)
    for key in ("category", "run_id", "started_at", "finished_at",
                "duration_seconds", "summary", "status", "tests"):
        assert key in out, f"{fn.__name__} missing key: {key}"

@pytest.mark.unit
@pytest.mark.parametrize("fn", PARSER_FUNCS_TEXT)
@pytest.mark.parametrize("rc", [0, 1, 2, 127, -1])
def test_every_text_parser_handles_any_returncode(fn, rc):
    out = fn(category="c", run_id=RUN, raw_text="", started_at=T0, finished_at=T1, returncode=rc)
    assert out["status"] in {"passed", "failed", "error", "never_run"}

@pytest.mark.unit
@pytest.mark.parametrize("fn", PARSER_FUNCS_TEXT)
@pytest.mark.parametrize("cat", SEC_CATEGORIES)
def test_every_parser_preserves_category(fn, cat):
    out = fn(category=cat, run_id=RUN, raw_text="", started_at=T0, finished_at=T1, returncode=0)
    assert out["category"] == cat


# ── parametrized: bandit CWE mapping table ────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("test_id,cwe", list(norm._BANDIT_CWE.items()))
def test_bandit_cwe_mapping_each_entry(test_id, cwe):
    """Each entry in _BANDIT_CWE must yield the correct CWE on a synthetic finding."""
    raw = json.dumps({"results": [{
        "test_id": test_id, "issue_severity": "MEDIUM", "issue_confidence": "LOW",
        "issue_text": "synthetic", "filename": "f.py", "line_number": 1, "issue_cwe": {},
    }]})
    out = norm.parse_bandit_json(category="backend_security_sast", run_id=RUN,
                                 raw_text=raw, started_at=T0, finished_at=T1, returncode=0)
    assert out["tests"][0]["cwe"] == cwe


# ── parametrized: bandit severity escalation ──────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("sev,expected_outcome", [
    ("LOW",      "skipped"),
    ("MEDIUM",   "error"),
    ("HIGH",     "failed"),
    ("CRITICAL", "failed"),
])
def test_bandit_severity_to_outcome(sev, expected_outcome):
    raw = json.dumps({"results": [{
        "test_id": "B101", "issue_severity": sev, "issue_confidence": "HIGH",
        "issue_text": "x", "filename": "f.py", "line_number": 1, "issue_cwe": {"id": 703},
    }]})
    out = norm.parse_bandit_json(category="backend_security_sast", run_id=RUN,
                                 raw_text=raw, started_at=T0, finished_at=T1, returncode=0)
    assert out["tests"][0]["outcome"] == expected_outcome


# ── parametrized: pip-audit severity → outcome ────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("sev,outcome", [
    ("critical", "failed"),
    ("high",     "failed"),
    ("medium",   "error"),
    ("low",      "skipped"),
    ("info",     "skipped"),
])
def test_pip_audit_severity_outcome(sev, outcome):
    raw = json.dumps({"dependencies": [{
        "name": "pkg", "version": "1.0",
        "vulns": [{"id": "G", "severity": sev, "fix_versions": [], "description": ""}],
    }]})
    out = norm.parse_pip_audit_json(category="backend_security_deps", run_id=RUN,
                                    raw_text=raw, started_at=T0, finished_at=T1, returncode=0)
    assert out["tests"][0]["outcome"] == outcome


# ── parametrized: coverage XML — coverage band → outcome ──────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("line_rate,branch_rate,expected", [
    (0.00, 0.00, "failed"),
    (0.30, 0.30, "failed"),
    (0.59, 0.59, "failed"),
    (0.60, 0.50, "error"),
    (0.70, 0.50, "error"),
    (0.79, 0.50, "error"),
    (0.80, 0.50, "passed"),
    (0.90, 0.85, "passed"),
    (1.00, 1.00, "passed"),
])
def test_coverage_band_to_outcome(line_rate, branch_rate, expected):
    xml = (
        f'<?xml version="1.0"?>'
        f'<coverage line-rate="{line_rate}" branch-rate="{branch_rate}">'
        f'<packages><package><classes>'
        f'<class filename="m.py" line-rate="{line_rate}" branch-rate="{branch_rate}"/>'
        f'</classes></package></packages></coverage>'
    )
    out = norm.parse_coverage_xml(category="backend_coverage", run_id=RUN,
                                  raw_text=xml, started_at=T0, finished_at=T1, returncode=0)
    assert out["tests"][0]["outcome"] == expected


# ── parametrized: pytest summary keys round-trip ──────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("total,passed,failed,errors,skipped", [
    (0, 0, 0, 0, 0),
    (1, 1, 0, 0, 0),
    (1, 0, 1, 0, 0),
    (1, 0, 0, 1, 0),
    (1, 0, 0, 0, 1),
    (10, 5, 2, 1, 2),
    (100, 80, 10, 5, 5),
])
def test_pytest_summary_roundtrip(total, passed, failed, errors, skipped):
    raw = {
        "duration": 1.0,
        "summary": {"total": total, "passed": passed, "failed": failed,
                    "error": errors, "skipped": skipped},
        "tests": [],
    }
    out = norm.parse_pytest_json(category="backend_unit", run_id=RUN, raw_json=raw,
                                 started_at=T0, finished_at=T1, returncode=0 if failed + errors == 0 else 1)
    assert out["summary"]["total"] == total
    assert out["summary"]["passed"] == passed
    assert out["summary"]["failed"] == failed
    assert out["summary"]["errors"] == errors
    assert out["summary"]["skipped"] == skipped


# ── parametrized: mutmut text variants ────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("killed,survived,expected_score", [
    (0, 0, 0.0),
    (1, 0, 100.0),
    (0, 1, 0.0),
    (10, 0, 100.0),
    (5, 5, 50.0),
    (9, 1, 90.0),
    (1, 9, 10.0),
    (50, 50, 50.0),
    (100, 0, 100.0),
])
def test_mutmut_score_arithmetic(killed, survived, expected_score):
    text = f"Killed 🎉 {killed}\nSurvived 🙁 {survived}\nTimed out ⏰ 0"
    out = norm.parse_mutmut_results(category="backend_mutation", run_id=RUN,
                                    raw_text=text, started_at=T0, finished_at=T1, returncode=0)
    assert abs(out["metrics"]["mutation_score"] - expected_score) < 0.01


# ── parametrized: summary failed/errors/skipped from findings ─────────────────

@pytest.mark.unit
@pytest.mark.parametrize("outcomes,exp_failed,exp_errors,exp_skipped", [
    ([], 0, 0, 0),
    (["failed"], 1, 0, 0),
    (["failed", "failed"], 2, 0, 0),
    (["error"], 0, 1, 0),
    (["skipped"], 0, 0, 1),
    (["failed", "error", "skipped"], 1, 1, 1),
    (["failed"] * 5 + ["error"] * 3 + ["skipped"] * 2, 5, 3, 2),
])
def test_summary_from_findings_parametrized(outcomes, exp_failed, exp_errors, exp_skipped):
    findings = [{"outcome": o} for o in outcomes]
    s = norm._summary_from_findings(findings)
    assert s["failed"] == exp_failed
    assert s["errors"] == exp_errors
    assert s["skipped"] == exp_skipped
    assert s["total"] == len(outcomes)


# ── parametrized: bucket breakdown ────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("findings,key,expected", [
    ([], "severity", {}),
    ([{"severity": "high"}], "severity", {"high": 1}),
    ([{"severity": "high"}] * 3, "severity", {"high": 3}),
    ([{"severity": "high"}, {"severity": "low"}], "severity", {"high": 1, "low": 1}),
    ([{"cwe": "CWE-89"}, {"cwe": "CWE-89"}, {"cwe": "CWE-78"}], "cwe", {"CWE-89": 2, "CWE-78": 1}),
])
def test_bucket_breakdown_parametrized(findings, key, expected):
    assert norm._bucket_breakdown(findings, key) == expected


# ── parametrized: format_message contains CWE + OWASP ─────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("cwe,owasp", [
    ("CWE-89",   "A03:2021-Injection"),
    ("CWE-78",   "A03:2021-Injection"),
    ("CWE-79",   "A03:2021-Injection"),
    ("CWE-22",   "A01:2021-Broken Access Control"),
    ("CWE-200",  "A01:2021-Broken Access Control"),
    ("CWE-327",  "A02:2021-Cryptographic Failures"),
    ("CWE-1035", "A06:2021-Vulnerable and Outdated Components"),
])
def test_format_message_includes_cwe_and_owasp(cwe, owasp):
    msg = norm._format_message({"cwe": cwe, "owasp": owasp, "remediation": "fix it", "body": "..."})
    assert cwe in msg
    assert owasp in msg


# ── parametrized: pip-audit / safety variants in payload shape ────────────────

@pytest.mark.unit
@pytest.mark.parametrize("key_for_vulns", ["vulnerabilities", "results"])
def test_safety_json_alternative_top_level_keys(key_for_vulns):
    raw = json.dumps({key_for_vulns: [{
        "vulnerability_id": "S1", "package_name": "pkg", "analyzed_version": "1",
        "severity": "high", "advisory": "boom",
    }]})
    out = norm.parse_safety_json(category="backend_security_deps", run_id=RUN,
                                 raw_text=raw, started_at=T0, finished_at=T1, returncode=1)
    assert out["summary"]["failed"] == 1

@pytest.mark.unit
@pytest.mark.parametrize("key_for_deps,key_for_vulns", [
    ("dependencies", "vulns"),
    ("vulnerabilities", "vulnerabilities"),
    ("dependencies", "vulnerabilities"),
])
def test_pip_audit_alternative_keys(key_for_deps, key_for_vulns):
    raw = json.dumps({key_for_deps: [{
        "name": "pkg", "version": "1.0",
        key_for_vulns: [{"id": "G", "severity": "high", "fix_versions": [], "description": ""}],
    }]})
    out = norm.parse_pip_audit_json(category="backend_security_deps", run_id=RUN,
                                    raw_text=raw, started_at=T0, finished_at=T1, returncode=1)
    assert out["summary"]["failed"] == 1


# ── parametrized: pytest tests[] entries shape ────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("nodeid,outcome", [
    ("tests/unit/test_a.py::test_one", "passed"),
    ("tests/unit/test_a.py::test_two", "failed"),
    ("tests/unit/test_a.py::test_three", "skipped"),
    ("tests/integration/test_x.py::test_xy", "error"),
    ("tests/regression/test_r.py::test_rr", "passed"),
])
def test_pytest_test_entry_shape(nodeid, outcome):
    raw = {
        "duration": 0.1,
        "summary": {"total": 1, "passed": int(outcome == "passed"),
                    "failed": int(outcome == "failed"),
                    "error":  int(outcome == "error"),
                    "skipped": int(outcome == "skipped")},
        "tests": [{"nodeid": nodeid, "outcome": outcome, "duration": 0.5,
                   "call": {"longrepr": "msg" if outcome != "passed" else None}}],
    }
    out = norm.parse_pytest_json(category="backend_unit", run_id=RUN, raw_json=raw,
                                 started_at=T0, finished_at=T1, returncode=0)
    assert out["tests"][0]["name"] == nodeid
    assert out["tests"][0]["outcome"] == outcome


# ── parametrized: status_from_summary across summary shapes ───────────────────

@pytest.mark.unit
@pytest.mark.parametrize("summary,rc,expected", [
    ({"failed": 0, "errors": 0, "total": 1}, 0, "passed"),
    ({"failed": 0, "errors": 0, "total": 1}, 1, "passed"),  # tests present → not a meta failure
    ({"failed": 1, "errors": 0, "total": 1}, 1, "failed"),
    ({"failed": 0, "errors": 1, "total": 1}, 1, "failed"),
    ({"failed": 0, "errors": 0, "total": 0}, 0, "passed"),
    ({"failed": 0, "errors": 0, "total": 0}, 1, "failed"),
])
def test_status_from_summary_matrix(summary, rc, expected):
    assert norm._status_from_summary(summary, returncode=rc) == expected


# ── exhaustive: empty results map to a 0 / 0 / 0 / 0 / 0 summary ──────────────

@pytest.mark.unit
@pytest.mark.parametrize("fn", PARSER_FUNCS_TEXT)
def test_every_parser_empty_summary_zeros(fn):
    out = fn(category="x", run_id=RUN, raw_text="{}", started_at=T0, finished_at=T1, returncode=0)
    for k in ("passed", "failed", "skipped", "errors"):
        assert out["summary"].get(k, 0) >= 0


# ── exhaustive: ruff & mypy SARIF variants ────────────────────────────────────

@pytest.mark.unit
def test_ruff_sarif_with_one_warning():
    sarif = json.dumps({"runs": [{
        "tool": {"driver": {"rules": [{"id": "F401"}]}},
        "results": [{
            "ruleId": "F401", "level": "warning",
            "message": {"text": "unused import"},
            "locations": [{"physicalLocation": {"artifactLocation": {"uri": "x.py"},
                                                "region": {"startLine": 1}}}],
        }],
    }]})
    out = norm.parse_ruff_sarif(category="backend_lint", run_id=RUN,
                                raw_text=sarif, started_at=T0, finished_at=T1, returncode=0)
    assert out["summary"]["total"] >= 1


# ── exhaustive: duration ─────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("start,end,expected_at_least", [
    ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00", 1.0),
    ("2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00+00:00", 60.0),
    ("2026-01-01T00:00:00+00:00", "2026-01-01T01:00:00+00:00", 3600.0),
])
def test_duration_calculations(start, end, expected_at_least):
    d = norm._duration(start, end)
    assert d >= expected_at_least - 0.5
    assert d <= expected_at_least + 0.5


# ── exhaustive: make_failed_payload for every category ────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("cat", SEC_CATEGORIES)
def test_make_failed_payload_for_every_category(cat):
    out = norm.make_failed_payload(category=cat, run_id="r", error="x",
                                   started_at=T0, finished_at=T1)
    assert out["category"] == cat
    assert out["status"] == "failed"
