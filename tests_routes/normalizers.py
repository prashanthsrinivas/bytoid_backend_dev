"""Convert pytest-json-report and locust --json output into the canonical
test-result payload consumed by the frontend dashboard."""

import json
from datetime import datetime, timezone
from typing import Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _status_from_summary(summary: dict, returncode: Optional[int] = None) -> str:
    if summary.get("failed", 0) > 0 or summary.get("errors", 0) > 0:
        return "failed"
    if returncode is not None and returncode != 0 and summary.get("total", 0) == 0:
        return "failed"
    return "passed"


def parse_pytest_json(
    *,
    category: str,
    run_id: str,
    raw_json: Optional[dict],
    started_at: str,
    finished_at: str,
    returncode: int,
    stdout_tail: str = "",
    stderr_tail: str = "",
) -> dict:
    """Normalize pytest-json-report output."""
    if not raw_json:
        return {
            "category": category,
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": _duration(started_at, finished_at),
            "summary": {
                "total": 0,
                "passed": 0,
                "failed": 0,
                "skipped": 0,
                "errors": 0,
            },
            "status": "failed",
            "tests": [],
            "metrics": None,
            "stdout_tail": stdout_tail[-2000:] if stdout_tail else "",
            "stderr_tail": stderr_tail[-2000:] if stderr_tail else "",
        }

    raw_summary = raw_json.get("summary", {}) or {}
    summary = {
        "total": raw_summary.get("total", 0),
        "passed": raw_summary.get("passed", 0),
        "failed": raw_summary.get("failed", 0),
        "skipped": raw_summary.get("skipped", 0),
        "errors": raw_summary.get("error", 0),
    }
    tests = []
    for t in raw_json.get("tests", []) or []:
        call = t.get("call") or {}
        tests.append(
            {
                "name": t.get("nodeid"),
                "outcome": t.get("outcome"),
                "duration": round(t.get("duration", 0) or 0, 4),
                "message": call.get("longrepr"),
            }
        )

    return {
        "category": category,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": round(raw_json.get("duration", 0) or 0, 3),
        "summary": summary,
        "status": _status_from_summary(summary, returncode),
        "tests": tests,
        "metrics": None,
    }


def parse_locust_json(
    *,
    category: str,
    run_id: str,
    raw_text: str,
    started_at: str,
    finished_at: str,
    returncode: int,
    num_users: Optional[int] = None,
    stdout_tail: str = "",
    stderr_tail: str = "",
) -> dict:
    """Normalize Locust --json output (a JSON array of per-endpoint stats)."""
    try:
        stats = json.loads(raw_text) if raw_text else []
    except json.JSONDecodeError:
        stats = []

    tests = []
    total_reqs = 0
    total_fails = 0
    weighted_p50 = 0.0
    weighted_p95 = 0.0
    weighted_p99 = 0.0
    total_rps = 0.0

    for row in stats:
        name = row.get("name") or row.get("method", "")
        method = row.get("method", "")
        reqs = row.get("num_requests", 0) or 0
        fails = row.get("num_failures", 0) or 0
        p50 = row.get("median_response_time", 0) or 0
        # locust exposes percentiles in different shapes across versions; fall back gracefully.
        p95 = row.get("response_time_percentile_0.95")
        if p95 is None:
            p95 = row.get("ninety_fifth_response_time", 0) or 0
        p99 = row.get("response_time_percentile_0.99")
        if p99 is None:
            p99 = row.get("ninety_ninth_response_time", 0) or 0
        rps = row.get("current_rps") or row.get("total_rps", 0) or 0

        if name == "Aggregated" or method == "":
            total_reqs += reqs
            total_fails += fails
            total_rps = rps or total_rps
            weighted_p50 = p50
            weighted_p95 = p95
            weighted_p99 = p99
            continue

        tests.append(
            {
                "name": f"{method} {name}".strip(),
                "outcome": "failed" if fails > 0 else "passed",
                "duration": round((p95 or 0) / 1000.0, 4),
                "message": None,
                "metrics": {
                    "requests": reqs,
                    "failures": fails,
                    "p50": p50,
                    "p95": p95,
                    "p99": p99,
                    "rps": rps,
                },
            }
        )

    if total_reqs == 0:
        for row in stats:
            total_reqs += row.get("num_requests", 0) or 0
            total_fails += row.get("num_failures", 0) or 0

    failure_rate = (total_fails / total_reqs) if total_reqs else 0
    summary = {
        "total": len(tests),
        "passed": sum(1 for t in tests if t["outcome"] == "passed"),
        "failed": sum(1 for t in tests if t["outcome"] == "failed"),
        "skipped": 0,
        "errors": 0,
    }
    status = "passed" if failure_rate < 0.05 and returncode == 0 else "failed"

    return {
        "category": category,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": _duration(started_at, finished_at),
        "summary": summary,
        "status": status,
        "tests": tests,
        "metrics": {
            "requests": total_reqs,
            "failures": total_fails,
            "failure_rate": round(failure_rate, 4),
            "rps": round(total_rps, 2),
            "p50": weighted_p50,
            "p95": weighted_p95,
            "p99": weighted_p99,
            "num_users": num_users,
        },
        "stdout_tail": stdout_tail[-2000:] if stdout_tail else "",
        "stderr_tail": stderr_tail[-2000:] if stderr_tail else "",
    }


def make_failed_payload(
    *,
    category: str,
    run_id: str,
    started_at: str,
    finished_at: str,
    error: str,
    stdout_tail: str = "",
    stderr_tail: str = "",
) -> dict:
    """For when the runner couldn't start (binary missing, timeout, exception)."""
    return {
        "category": category,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": _duration(started_at, finished_at),
        "summary": {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 1},
        "status": "failed",
        "tests": [],
        "metrics": None,
        "error": error,
        "stdout_tail": stdout_tail[-2000:] if stdout_tail else "",
        "stderr_tail": stderr_tail[-2000:] if stderr_tail else "",
    }


def _duration(started_at: str, finished_at: str) -> float:
    try:
        s = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        f = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
        return round((f - s).total_seconds(), 3)
    except (ValueError, AttributeError):
        return 0.0


def utcnow_iso() -> str:
    return _now_iso()


# ─────────────────────────────────────────────────────────────────────────
# Phase 1 — security-scanner normalizers.
#
# Every scanner emits its own format; these functions converge them to the
# same canonical payload the dashboard already renders. Conventions:
#
#   summary.failed  = HIGH + CRITICAL findings
#   summary.errors  = MEDIUM findings
#   summary.skipped = LOW + INFO findings (so the dashboard's "passed"
#                     bucket actually means clean, not "low-severity only")
#   summary.total   = sum of the three
#   summary.passed  = 0  (every row is a finding; "passed" is reserved for
#                     the case of zero findings, in which case status==passed)
#
#   tests[*].outcome = "failed" for HIGH/CRITICAL, "error" for MEDIUM,
#                     "skipped" for LOW/INFO. The dashboard's TestOutcome
#                     widening already covers these values.
#
#   tests[*].message = "<CWE> | <OWASP> | <remediation>\n<full message>"
#
#   metrics.<tool>.severity_breakdown, .cwe_breakdown, .owasp_breakdown
# ─────────────────────────────────────────────────────────────────────────


def _severity_to_outcome(severity: str) -> str:
    s = (severity or "").lower()
    if s in {"critical", "high", "error"}:
        return "failed"
    if s in {"medium", "warning"}:
        return "error"
    return "skipped"


def _summary_from_findings(findings: list[dict]) -> dict:
    high = sum(1 for f in findings if f["outcome"] == "failed")
    med = sum(1 for f in findings if f["outcome"] == "error")
    low = sum(1 for f in findings if f["outcome"] == "skipped")
    return {
        "total": high + med + low,
        "passed": 0,
        "failed": high,
        "skipped": low,
        "errors": med,
    }


def _status_from_findings(findings: list[dict], threshold: str = "high") -> str:
    """Return 'passed' if no finding at or above `threshold`, else 'failed'."""
    order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    cut = order.get(threshold, 2)
    for f in findings:
        sev = (f.get("severity") or "").lower()
        if order.get(sev, 0) >= cut:
            return "failed"
    return "passed"


def _bucket_breakdown(findings: list[dict], key: str) -> dict:
    out: dict[str, int] = {}
    for f in findings:
        v = f.get(key) or "unknown"
        out[v] = out.get(v, 0) + 1
    return out


def _format_message(finding: dict) -> str:
    cwe = finding.get("cwe") or "CWE-unknown"
    owasp = finding.get("owasp") or "OWASP-unknown"
    rem = finding.get("remediation") or ""
    body = finding.get("body") or ""
    return f"{cwe} | {owasp} | {rem}\n{body}".strip()


def _sarif_level_to_severity(level: str) -> str:
    """SARIF uses 'error'/'warning'/'note'; map to our severity scale."""
    lvl = (level or "warning").lower()
    if lvl == "error":
        return "high"
    if lvl == "warning":
        return "medium"
    if lvl == "note":
        return "low"
    return "medium"


# ── Bandit ────────────────────────────────────────────────────────────────


_BANDIT_CWE = {
    # Bandit's `issue_cwe.id` maps to a CWE number; we still normalize a few
    # of the most common ones in case the field is absent in some versions.
    "B101": "CWE-703",
    "B102": "CWE-78",
    "B103": "CWE-732",
    "B104": "CWE-200",
    "B105": "CWE-259",
    "B106": "CWE-259",
    "B107": "CWE-259",
    "B201": "CWE-489",
    "B301": "CWE-502",
    "B302": "CWE-502",
    "B303": "CWE-327",
    "B304": "CWE-327",
    "B305": "CWE-327",
    "B306": "CWE-377",
    "B307": "CWE-78",
    "B308": "CWE-79",
    "B311": "CWE-330",
    "B321": "CWE-78",
    "B322": "CWE-326",
    "B324": "CWE-327",
    "B325": "CWE-377",
    "B401": "CWE-78",
    "B403": "CWE-502",
    "B405": "CWE-611",
    "B501": "CWE-295",
    "B502": "CWE-295",
    "B503": "CWE-295",
    "B504": "CWE-295",
    "B505": "CWE-326",
    "B506": "CWE-20",
    "B601": "CWE-78",
    "B602": "CWE-78",
    "B603": "CWE-78",
    "B604": "CWE-78",
    "B605": "CWE-78",
    "B606": "CWE-78",
    "B607": "CWE-78",
    "B608": "CWE-89",
    "B609": "CWE-78",
    "B610": "CWE-89",
    "B611": "CWE-89",
}


def parse_bandit_json(
    *,
    category: str,
    run_id: str,
    raw_text: str,
    started_at: str,
    finished_at: str,
    returncode: int,
) -> dict:
    """Bandit `-f json` output → canonical payload."""
    try:
        raw = json.loads(raw_text) if raw_text else {}
    except json.JSONDecodeError:
        raw = {}

    findings: list[dict] = []
    for result in raw.get("results", []) or []:
        test_id = result.get("test_id") or ""
        sev = (result.get("issue_severity") or "").lower()
        outcome = _severity_to_outcome(sev)
        cwe_obj = result.get("issue_cwe") or {}
        cwe = f"CWE-{cwe_obj.get('id')}" if cwe_obj.get("id") else _BANDIT_CWE.get(test_id, "CWE-unknown")
        owasp = result.get("issue_owasp") or _owasp_from_cwe(cwe)
        body = result.get("issue_text") or ""
        loc = f"{result.get('filename')}:{result.get('line_number')}"
        finding = {
            "name": f"{test_id} {loc}".strip(),
            "outcome": outcome,
            "duration": 0.0,
            "severity": sev,
            "cwe": cwe,
            "owasp": owasp,
            "remediation": result.get("issue_confidence", "") and f"confidence: {result.get('issue_confidence')}",
            "body": body,
            "test_id": test_id,
            "path": loc,
        }
        finding["message"] = _format_message(finding)
        findings.append(finding)

    summary = _summary_from_findings(findings)
    status = _status_from_findings(findings, threshold="high")
    if returncode != 0 and summary["total"] == 0:
        status = "failed"

    return {
        "category": category,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": _duration(started_at, finished_at),
        "summary": summary,
        "status": status,
        "tests": findings,
        "metrics": {
            "tool": "bandit",
            "severity_breakdown": _bucket_breakdown(findings, "severity"),
            "cwe_breakdown": _bucket_breakdown(findings, "cwe"),
            "owasp_breakdown": _bucket_breakdown(findings, "owasp"),
        },
    }


# ── Semgrep ───────────────────────────────────────────────────────────────


def parse_semgrep_sarif(
    *,
    category: str,
    run_id: str,
    raw_text: str,
    started_at: str,
    finished_at: str,
    returncode: int,
) -> dict:
    """Semgrep SARIF output → canonical payload."""
    return _parse_generic_sarif(
        tool="semgrep",
        category=category,
        run_id=run_id,
        raw_text=raw_text,
        started_at=started_at,
        finished_at=finished_at,
        returncode=returncode,
    )


# ── Gitleaks ──────────────────────────────────────────────────────────────


def parse_gitleaks_sarif(
    *,
    category: str,
    run_id: str,
    raw_text: str,
    started_at: str,
    finished_at: str,
    returncode: int,
) -> dict:
    """Gitleaks SARIF output → canonical payload."""
    return _parse_generic_sarif(
        tool="gitleaks",
        category=category,
        run_id=run_id,
        raw_text=raw_text,
        started_at=started_at,
        finished_at=finished_at,
        returncode=returncode,
        # Every gitleaks hit is a leak; treat all as HIGH regardless of SARIF level.
        force_severity="high",
    )


# ── pip-audit ─────────────────────────────────────────────────────────────


def parse_pip_audit_json(
    *,
    category: str,
    run_id: str,
    raw_text: str,
    started_at: str,
    finished_at: str,
    returncode: int,
) -> dict:
    """pip-audit `-f json` output → canonical payload."""
    try:
        raw = json.loads(raw_text) if raw_text else {}
    except json.JSONDecodeError:
        raw = {}

    findings: list[dict] = []
    deps = raw.get("dependencies") or raw.get("vulnerabilities") or []
    for dep in deps:
        # The pip-audit JSON schema lists each vulnerable dep with a list of vulns.
        for vuln in dep.get("vulns") or dep.get("vulnerabilities") or []:
            vid = vuln.get("id") or "UNKNOWN"
            sev_field = vuln.get("severity") or vuln.get("rating") or ""
            sev = sev_field.lower() if isinstance(sev_field, str) else "medium"
            if sev not in {"critical", "high", "medium", "low", "info"}:
                sev = "medium"
            cwe = "CWE-1035"  # "Using Components with Known Vulnerabilities"
            owasp = "A06:2021-Vulnerable and Outdated Components"
            pkg = dep.get("name", "")
            ver = dep.get("version", "")
            fix = ", ".join(vuln.get("fix_versions") or [])
            finding = {
                "name": f"{vid} {pkg}=={ver}",
                "outcome": _severity_to_outcome(sev),
                "duration": 0.0,
                "severity": sev,
                "cwe": cwe,
                "owasp": owasp,
                "remediation": f"Upgrade to: {fix}" if fix else "No fixed version available; consider replacing the dependency.",
                "body": vuln.get("description", ""),
                "test_id": vid,
                "path": f"{pkg}=={ver}",
            }
            finding["message"] = _format_message(finding)
            findings.append(finding)

    summary = _summary_from_findings(findings)
    status = _status_from_findings(findings, threshold="critical")
    return {
        "category": category,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": _duration(started_at, finished_at),
        "summary": summary,
        "status": status,
        "tests": findings,
        "metrics": {
            "tool": "pip-audit",
            "severity_breakdown": _bucket_breakdown(findings, "severity"),
            "vulnerable_packages": sorted({f["path"].split("==")[0] for f in findings}),
        },
    }


# ── Safety (community edition) ────────────────────────────────────────────


def parse_safety_json(
    *,
    category: str,
    run_id: str,
    raw_text: str,
    started_at: str,
    finished_at: str,
    returncode: int,
) -> dict:
    """Safety scan JSON output → canonical payload."""
    try:
        raw = json.loads(raw_text) if raw_text else {}
    except json.JSONDecodeError:
        raw = {}

    findings: list[dict] = []
    # Safety v3 emits a list under 'vulnerabilities' or 'scan_results'.
    vulns = (
        raw.get("vulnerabilities")
        or raw.get("scan_results", {}).get("vulnerabilities")
        or raw.get("results")
        or []
    )
    for v in vulns:
        sev_field = v.get("severity") or v.get("cvss_severity") or ""
        sev = sev_field.lower() if isinstance(sev_field, str) else "medium"
        if sev not in {"critical", "high", "medium", "low", "info"}:
            sev = "medium"
        vid = v.get("vulnerability_id") or v.get("id") or "UNKNOWN"
        pkg = v.get("package_name") or v.get("package") or ""
        ver = v.get("analyzed_version") or v.get("installed_version") or ""
        finding = {
            "name": f"{vid} {pkg}=={ver}",
            "outcome": _severity_to_outcome(sev),
            "duration": 0.0,
            "severity": sev,
            "cwe": "CWE-1035",
            "owasp": "A06:2021-Vulnerable and Outdated Components",
            "remediation": v.get("more_info_url") or "See vendor advisory.",
            "body": v.get("advisory") or v.get("description") or "",
            "test_id": vid,
            "path": f"{pkg}=={ver}",
        }
        finding["message"] = _format_message(finding)
        findings.append(finding)

    return {
        "category": category,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": _duration(started_at, finished_at),
        "summary": _summary_from_findings(findings),
        "status": _status_from_findings(findings, threshold="critical"),
        "tests": findings,
        "metrics": {
            "tool": "safety",
            "severity_breakdown": _bucket_breakdown(findings, "severity"),
        },
    }


# ── Coverage (pytest-cov XML) ─────────────────────────────────────────────


def parse_coverage_xml(
    *,
    category: str,
    run_id: str,
    raw_text: str,
    started_at: str,
    finished_at: str,
    returncode: int,
) -> dict:
    """Cobertura-style coverage.xml → canonical payload.

    Each Python file becomes one row in `tests[]`. `summary` totals lines
    measured/covered/missed across the codebase. `metrics` carries
    aggregate line + branch coverage percentages.
    """
    import xml.etree.ElementTree as ET  # noqa: PLC0415  (lazy import; XML rarely needed)

    files: list[dict] = []
    overall_line_rate: Optional[float] = None
    overall_branch_rate: Optional[float] = None

    if raw_text:
        try:
            root = ET.fromstring(raw_text)
            overall_line_rate = float(root.attrib.get("line-rate", "0") or 0)
            overall_branch_rate = float(root.attrib.get("branch-rate", "0") or 0)
            for cls in root.iter("class"):
                fname = cls.attrib.get("filename", "")
                line_rate = float(cls.attrib.get("line-rate", "0") or 0)
                branch_rate = float(cls.attrib.get("branch-rate", "0") or 0)
                # Below 60% line coverage flags as failed; 60-80% as error; >=80% as passed.
                if line_rate < 0.60:
                    outcome = "failed"
                elif line_rate < 0.80:
                    outcome = "error"
                else:
                    outcome = "passed"
                files.append(
                    {
                        "name": fname,
                        "outcome": outcome,
                        "duration": 0.0,
                        "message": f"line={line_rate * 100:.1f}% branch={branch_rate * 100:.1f}%",
                    }
                )
        except ET.ParseError:
            pass

    summary = {
        "total": len(files),
        "passed": sum(1 for f in files if f["outcome"] == "passed"),
        "failed": sum(1 for f in files if f["outcome"] == "failed"),
        "skipped": 0,
        "errors": sum(1 for f in files if f["outcome"] == "error"),
    }
    status = "passed" if summary["failed"] == 0 and overall_line_rate and overall_line_rate >= 0.60 else "failed"

    return {
        "category": category,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": _duration(started_at, finished_at),
        "summary": summary,
        "status": status,
        "tests": files,
        "metrics": {
            "tool": "pytest-cov",
            "line_rate": round((overall_line_rate or 0) * 100, 2),
            "branch_rate": round((overall_branch_rate or 0) * 100, 2),
        },
    }


# ── Generic SARIF parser (shared by Semgrep, Gitleaks, Bandit-SARIF) ──────


def _parse_generic_sarif(
    *,
    tool: str,
    category: str,
    run_id: str,
    raw_text: str,
    started_at: str,
    finished_at: str,
    returncode: int,
    force_severity: Optional[str] = None,
) -> dict:
    try:
        raw = json.loads(raw_text) if raw_text else {}
    except json.JSONDecodeError:
        raw = {}

    findings: list[dict] = []
    for run in raw.get("runs", []) or []:
        # Build a rule_id → rule-metadata index for richer findings.
        rules_index: dict[str, dict] = {}
        for rule in (run.get("tool", {}).get("driver", {}).get("rules") or []):
            rid = rule.get("id") or ""
            if rid:
                rules_index[rid] = rule

        for res in run.get("results") or []:
            rule_id = res.get("ruleId") or ""
            rule = rules_index.get(rule_id, {})
            level = res.get("level") or rule.get("defaultConfiguration", {}).get("level") or "warning"
            sev = force_severity or _sarif_level_to_severity(level)
            outcome = _severity_to_outcome(sev)

            # Location.
            loc = ""
            try:
                phys = res["locations"][0]["physicalLocation"]
                uri = phys["artifactLocation"]["uri"]
                line = phys.get("region", {}).get("startLine", "")
                loc = f"{uri}:{line}".rstrip(":")
            except (KeyError, IndexError, TypeError):
                pass

            # CWE / OWASP from the rule's `properties` if present, else fall back.
            tags = (rule.get("properties") or {}).get("tags") or []
            cwe = next((t for t in tags if isinstance(t, str) and t.upper().startswith("CWE-")), "CWE-unknown")
            owasp = next((t for t in tags if isinstance(t, str) and t.upper().startswith("A0")), _owasp_from_cwe(cwe))

            body = (
                res.get("message", {}).get("text")
                or rule.get("shortDescription", {}).get("text")
                or rule.get("fullDescription", {}).get("text")
                or ""
            )
            rem = (
                rule.get("help", {}).get("text")
                or rule.get("helpUri", "")
                or ""
            )

            finding = {
                "name": f"{rule_id} {loc}".strip(),
                "outcome": outcome,
                "duration": 0.0,
                "severity": sev,
                "cwe": cwe,
                "owasp": owasp,
                "remediation": rem,
                "body": body,
                "test_id": rule_id,
                "path": loc,
            }
            finding["message"] = _format_message(finding)
            findings.append(finding)

    summary = _summary_from_findings(findings)
    status = _status_from_findings(findings, threshold="high")
    if returncode != 0 and summary["total"] == 0:
        # A non-zero exit with no parsed findings indicates a tool failure.
        status = "failed"

    return {
        "category": category,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": _duration(started_at, finished_at),
        "summary": summary,
        "status": status,
        "tests": findings,
        "metrics": {
            "tool": tool,
            "severity_breakdown": _bucket_breakdown(findings, "severity"),
            "cwe_breakdown": _bucket_breakdown(findings, "cwe"),
            "owasp_breakdown": _bucket_breakdown(findings, "owasp"),
        },
    }


# ── Phase 2 normalizers ───────────────────────────────────────────────────
#
# mypy   — `mypy --output=json` emits JSON Lines (one object per diagnostic).
# pylint — `pylint --output-format=json` emits a JSON array.
# ruff   — `ruff check --output-format=sarif` emits SARIF (same as Semgrep).
# ─────────────────────────────────────────────────────────────────────────


def parse_mypy_json(
    *,
    category: str,
    run_id: str,
    raw_text: str,
    started_at: str,
    finished_at: str,
    returncode: int,
) -> dict:
    """mypy `--output=json` JSONL → canonical payload.

    Each line is one diagnostic:
      {"file": "...", "line": N, "column": N, "severity": "error"|"warning"|"note",
       "message": "...", "code": "..."}
    """
    findings: list[dict] = []
    for line in (raw_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            diag = json.loads(line)
        except json.JSONDecodeError:
            continue
        sev_raw = (diag.get("severity") or "note").lower()
        sev = {"error": "high", "warning": "medium", "note": "low"}.get(sev_raw, "low")
        outcome = _severity_to_outcome(sev)
        loc = f"{diag.get('file', '')}:{diag.get('line', '')}".rstrip(":")
        code = diag.get("code") or "mypy"
        finding = {
            "name": f"{code} {loc}".strip(),
            "outcome": outcome,
            "duration": 0.0,
            "severity": sev,
            "cwe": "CWE-unknown",
            "owasp": "OWASP-unmapped",
            "remediation": f"mypy code: {code}",
            "body": diag.get("message") or "",
            "test_id": code,
            "path": loc,
        }
        finding["message"] = _format_message(finding)
        findings.append(finding)

    summary = _summary_from_findings(findings)
    status = _status_from_findings(findings, threshold="high")
    if returncode != 0 and summary["total"] == 0:
        status = "failed"

    return {
        "category": category,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": _duration(started_at, finished_at),
        "summary": summary,
        "status": status,
        "tests": findings,
        "metrics": {
            "tool": "mypy",
            "severity_breakdown": _bucket_breakdown(findings, "severity"),
            "code_breakdown": _bucket_breakdown(findings, "test_id"),
        },
    }


def parse_pylint_json(
    *,
    category: str,
    run_id: str,
    raw_text: str,
    started_at: str,
    finished_at: str,
    returncode: int,
) -> dict:
    """pylint `--output-format=json` → canonical payload.

    Each item:
      {"type": "error"|"warning"|"refactor"|"convention"|"fatal",
       "module": "...", "path": "...", "line": N, "symbol": "...",
       "message": "...", "message-id": "E0001"}
    """
    try:
        items = json.loads(raw_text) if raw_text and raw_text.strip() else []
        if not isinstance(items, list):
            items = []
    except json.JSONDecodeError:
        items = []

    _pylint_sev = {
        "fatal": "critical",
        "error": "high",
        "warning": "medium",
        "refactor": "low",
        "convention": "low",
    }

    findings: list[dict] = []
    for item in items:
        typ = (item.get("type") or "convention").lower()
        sev = _pylint_sev.get(typ, "low")
        outcome = _severity_to_outcome(sev)
        path = item.get("path") or item.get("module") or ""
        line = item.get("line") or ""
        loc = f"{path}:{line}".rstrip(":")
        msg_id = item.get("message-id") or item.get("messageId") or "pylint"
        finding = {
            "name": f"{msg_id} {loc}".strip(),
            "outcome": outcome,
            "duration": 0.0,
            "severity": sev,
            "cwe": "CWE-unknown",
            "owasp": "OWASP-unmapped",
            "remediation": f"pylint symbol: {item.get('symbol', msg_id)}",
            "body": item.get("message") or "",
            "test_id": msg_id,
            "path": loc,
        }
        finding["message"] = _format_message(finding)
        findings.append(finding)

    summary = _summary_from_findings(findings)
    status = _status_from_findings(findings, threshold="high")
    if returncode not in {0, 4} and summary["total"] == 0:
        # pylint exits 4 on "usage error"; 1/2/4 are warning/error/fatal bitmask.
        status = "failed"

    return {
        "category": category,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": _duration(started_at, finished_at),
        "summary": summary,
        "status": status,
        "tests": findings,
        "metrics": {
            "tool": "pylint",
            "severity_breakdown": _bucket_breakdown(findings, "severity"),
            "symbol_breakdown": _bucket_breakdown(findings, "test_id"),
        },
    }


def parse_ruff_sarif(
    *,
    category: str,
    run_id: str,
    raw_text: str,
    started_at: str,
    finished_at: str,
    returncode: int,
) -> dict:
    """ruff `--output-format=sarif` → canonical payload (reuses SARIF parser)."""
    return _parse_generic_sarif(
        tool="ruff",
        category=category,
        run_id=run_id,
        raw_text=raw_text,
        started_at=started_at,
        finished_at=finished_at,
        returncode=returncode,
    )


def parse_mutmut_results(
    *,
    category: str,
    run_id: str,
    raw_text: str,
    started_at: str,
    finished_at: str,
    returncode: int,
) -> dict:
    """Parse `mutmut results` text output into the canonical payload.

    mutmut results output looks like:
        Timed out ⏰ 3
        Suspicious 🤔 1
        Killed 🎉 42
        Survived 🙁 5

    killed   → passed (tests caught the mutation — good)
    survived → failed (tests missed the mutation — bad)
    timed out + suspicious → errors
    """
    killed = 0
    survived = 0
    timed_out = 0
    suspicious = 0

    for line in (raw_text or "").splitlines():
        line = line.strip()
        # Strip emoji and extra whitespace for robust parsing
        clean = line.encode("ascii", "ignore").decode().strip()
        # e.g. "Killed  42" after emoji removal
        lower = clean.lower()
        parts = lower.split()
        if not parts:
            continue
        try:
            count = int(parts[-1])
        except (ValueError, IndexError):
            continue
        if "killed" in lower:
            killed = count
        elif "survived" in lower:
            survived = count
        elif "timed out" in lower or "timeout" in lower:
            timed_out = count
        elif "suspicious" in lower:
            suspicious = count

    total = killed + survived + timed_out + suspicious
    summary = {
        "total": total,
        "passed": killed,
        "failed": survived,
        "skipped": 0,
        "errors": timed_out + suspicious,
    }

    # Build one test row per survived mutant (we don't have individual IDs from text output)
    tests: list[dict] = []
    if survived > 0:
        tests.append({
            "name": f"mutmut: {survived} mutant(s) survived",
            "outcome": "failed",
            "duration": 0.0,
            "severity": "medium",
            "message": (
                f"{survived} mutant(s) survived — tests did not catch these mutations. "
                f"Run `mutmut show <id>` to inspect. Killed: {killed}, "
                f"Timed out: {timed_out}, Suspicious: {suspicious}."
            ),
        })
    if killed > 0:
        tests.append({
            "name": f"mutmut: {killed} mutant(s) killed",
            "outcome": "passed",
            "duration": 0.0,
            "severity": "info",
            "message": f"{killed} mutant(s) killed by the test suite.",
        })
    if timed_out + suspicious > 0:
        tests.append({
            "name": f"mutmut: {timed_out + suspicious} mutant(s) timed out / suspicious",
            "outcome": "failed",
            "duration": 0.0,
            "severity": "low",
            "message": (
                f"Timed out: {timed_out}, Suspicious: {suspicious}. "
                "These may indicate infinite loops introduced by mutations."
            ),
        })

    status = "passed" if survived == 0 and total > 0 else ("failed" if survived > 0 else "never_run")
    if returncode != 0 and total == 0:
        status = "failed"

    return {
        "category": category,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": _duration(started_at, finished_at),
        "summary": summary,
        "status": status,
        "tests": tests,
        "metrics": {
            "tool": "mutmut",
            "killed": killed,
            "survived": survived,
            "timed_out": timed_out,
            "suspicious": suspicious,
            "mutation_score": round(killed / total * 100, 1) if total > 0 else 0.0,
        },
    }


# ── CWE → OWASP fallback map (best-effort) ────────────────────────────────


_CWE_TO_OWASP = {
    "CWE-20":  "A03:2021-Injection",
    "CWE-22":  "A01:2021-Broken Access Control",
    "CWE-78":  "A03:2021-Injection",
    "CWE-79":  "A03:2021-Injection",
    "CWE-89":  "A03:2021-Injection",
    "CWE-94":  "A03:2021-Injection",
    "CWE-200": "A01:2021-Broken Access Control",
    "CWE-259": "A07:2021-Identification and Authentication Failures",
    "CWE-287": "A07:2021-Identification and Authentication Failures",
    "CWE-295": "A02:2021-Cryptographic Failures",
    "CWE-311": "A02:2021-Cryptographic Failures",
    "CWE-326": "A02:2021-Cryptographic Failures",
    "CWE-327": "A02:2021-Cryptographic Failures",
    "CWE-330": "A02:2021-Cryptographic Failures",
    "CWE-352": "A01:2021-Broken Access Control",
    "CWE-377": "A04:2021-Insecure Design",
    "CWE-434": "A04:2021-Insecure Design",
    "CWE-489": "A05:2021-Security Misconfiguration",
    "CWE-502": "A08:2021-Software and Data Integrity Failures",
    "CWE-611": "A05:2021-Security Misconfiguration",
    "CWE-732": "A01:2021-Broken Access Control",
    "CWE-778": "A09:2021-Security Logging and Monitoring Failures",
    "CWE-798": "A07:2021-Identification and Authentication Failures",
    "CWE-862": "A01:2021-Broken Access Control",
    "CWE-863": "A01:2021-Broken Access Control",
    "CWE-918": "A10:2021-Server-Side Request Forgery",
    "CWE-1035": "A06:2021-Vulnerable and Outdated Components",
}


def _owasp_from_cwe(cwe: str) -> str:
    return _CWE_TO_OWASP.get(cwe, "OWASP-unmapped")
