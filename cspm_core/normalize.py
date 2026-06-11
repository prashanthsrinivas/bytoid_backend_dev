"""Shared finding/snapshot contract for every cloud.

A *finding* is one flagged condition; a *snapshot* is one audit run's findings +
derived counts + a 0-100 risk score. ``scope_id``/``scope_name`` generalize AWS's
account_id (a subscription for Azure, a project for GCP). Domain collectors call
``make_domain_finding`` with their provider's ``rule_meta`` so domain/category are
pulled from one place. Dependency-free (stdlib only).
"""

from __future__ import annotations

from datetime import datetime, timezone

from cspm_core.schema import CATEGORIES, SEV_INFO, SEVERITY_ORDER, SEVERITY_WEIGHTS


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_finding(
    *, finding_id, domain, category, rule_id, finding_summary, severity=SEV_INFO,
    supporting_details=None, evidence_type="posture_finding", source="", collected_at=None,
) -> dict:
    if category not in CATEGORIES:
        raise ValueError(f"Unknown category: {category!r}")
    if severity not in SEVERITY_ORDER:
        raise ValueError(f"Unknown severity: {severity!r}")
    return {
        "finding_id": str(finding_id),
        "domain": domain,
        "category": category,
        "rule_id": str(rule_id),
        "evidence_type": str(evidence_type or "").strip(),
        "source": str(source or "").strip(),
        "finding_summary": str(finding_summary or "").strip(),
        "supporting_details": dict(supporting_details or {}),
        "risk_indicators": [str(rule_id)],
        "severity": severity,
        "collected_at": collected_at or _utc_now_iso(),
    }


def make_domain_finding(
    *, rule_meta, rule_id, severity, finding_summary, scope_id, scope_name="", region="",
    entity_type, entity_id, entity_name="", source="", details=None,
) -> dict:
    """Build a finding, pulling domain + category from ``rule_meta[rule_id]``."""
    m = rule_meta.get(rule_id, {})
    sd = {
        "scope_id": scope_id,
        "scope_name": scope_name or scope_id,
        "region": region or "",
        "entity_type": entity_type,
        "entity_id": entity_id,
        "entity_name": entity_name or entity_id,
        "rule_id": rule_id,
        **(details or {}),
    }
    fid = f"{scope_id}:{m.get('domain', '')}:{rule_id}:{entity_id}"
    return make_finding(
        finding_id=fid, domain=m.get("domain", "hygiene"), category=m.get("category", "hygiene"),
        rule_id=rule_id, severity=severity, finding_summary=finding_summary,
        supporting_details=sd, evidence_type=f"{m.get('domain', 'posture')}_finding", source=source,
    )


def severity_counts(findings):
    counts = {sev: 0 for sev in SEVERITY_ORDER}
    for f in findings:
        sev = f.get("severity", SEV_INFO)
        if sev in counts:
            counts[sev] += 1
    return counts


def category_counts(findings):
    counts = {c: 0 for c in CATEGORIES}
    for f in findings:
        if f.get("category") in counts:
            counts[f["category"]] += 1
    return counts


def rule_counts(findings):
    counts: dict = {}
    for f in findings:
        rid = f.get("rule_id", "")
        if rid:
            counts[rid] = counts.get(rid, 0) + 1
    return counts


def domain_counts(findings):
    out: dict = {}
    for f in findings:
        dom = f.get("domain") or "hygiene"
        if dom not in out:
            out[dom] = {"total": 0, "by_severity": {s: 0 for s in SEVERITY_ORDER}}
        out[dom]["total"] += 1
        sev = f.get("severity", SEV_INFO)
        if sev in out[dom]["by_severity"]:
            out[dom]["by_severity"][sev] += 1
    return out


def risk_score(findings) -> float:
    if not findings:
        return 0.0
    weights = [SEVERITY_WEIGHTS.get(f.get("severity", SEV_INFO), 0) for f in findings]
    return round(0.7 * max(weights) + 0.3 * (sum(weights) / len(weights)), 1)


def build_snapshot(*, scan_id, audit_id, findings, scopes_scanned=None,
                   collector_status=None, scope=None, scanned_at=None) -> dict:
    clean = list(findings or [])
    score = risk_score(clean)
    return {
        "scan_id": scan_id,
        "audit_id": audit_id,
        "scanned_at": scanned_at or _utc_now_iso(),
        "findings": clean,
        "scopes_scanned": scopes_scanned or [],
        "scope": scope or {},
        "counts": {
            "total": len(clean),
            "by_severity": severity_counts(clean),
            "by_category": category_counts(clean),
            "by_rule": rule_counts(clean),
            "by_domain": domain_counts(clean),
        },
        "risk_score": score,
        "posture_score": round(100.0 - score, 1),
        "collector_status": collector_status or {},
    }


REQUIRED_FINDING_FIELDS = (
    "finding_id", "domain", "category", "rule_id", "evidence_type", "source",
    "finding_summary", "severity", "collected_at",
)


def validate_finding(finding) -> bool:
    if not isinstance(finding, dict):
        return False
    if any(field not in finding for field in REQUIRED_FINDING_FIELDS):
        return False
    if finding.get("category") not in CATEGORIES:
        return False
    if finding.get("severity") not in SEVERITY_ORDER:
        return False
    return True
