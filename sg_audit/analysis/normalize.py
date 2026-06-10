"""Shared SG-audit finding contract — the app <-> Lambda interface.

Both the Lambda collector and the app callback bind to this shape, so it is the
one place the schema is defined and validated. A *finding* is one flagged
security-group condition; a *snapshot* is one audit's worth of findings plus
derived counts and a 0-100 risk score. Dependency-free (stdlib only) so it
vendors cleanly into the Lambda.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sg_audit import metadata
from sg_audit.schema import (
    CATEGORIES,
    DOMAIN_SECURITY_GROUPS,
    SEV_INFO,
    SEVERITY_ORDER,
    SEVERITY_WEIGHTS,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_finding_id(
    account_id: str,
    group_id: str,
    rule_id: str,
    cidr: str,
    protocol: str,
    from_port,
    to_port,
    port="",
) -> str:
    """Stable, deterministic id for a finding (drives AI faithfulness checks)."""
    return f"{account_id}:{group_id}:{rule_id}:{cidr}:{protocol}:{from_port}-{to_port}:{port}"


def make_finding(
    *,
    finding_id: str,
    category: str,
    rule_id: str,
    finding_summary: str,
    severity: str = SEV_INFO,
    supporting_details: dict | None = None,
    evidence_type: str = "sg_rule",
    source: str = "ec2:DescribeSecurityGroups",
    domain: str | None = None,
    collected_at: str | None = None,
) -> dict:
    """Build one validated, normalized finding record.

    Raises ``ValueError`` on an unknown category or severity so a malformed
    analyzer fails loudly in tests rather than silently producing junk. ``domain``
    defaults from the rule metadata (Security Groups when unknown), so existing
    SG findings are auto-tagged without changing the SG rule engine.
    """
    if category not in CATEGORIES:
        raise ValueError(f"Unknown category: {category!r}")
    if severity not in SEVERITY_ORDER:
        raise ValueError(f"Unknown severity: {severity!r}")
    resolved_domain = domain or metadata.domain_for(rule_id, DOMAIN_SECURITY_GROUPS)
    sd = dict(supporting_details or {})
    # Backfill the standardized entity fields for SG findings (which carry
    # group_id/group_name) so every domain exposes a consistent entity model.
    if not sd.get("entity_id") and sd.get("group_id"):
        sd.setdefault("entity_type", "security_group")
        sd["entity_id"] = sd.get("group_id")
        sd.setdefault("entity_name", sd.get("group_name", ""))
    return {
        "finding_id": str(finding_id),
        "domain": resolved_domain,
        "category": category,
        "rule_id": str(rule_id),
        "evidence_type": str(evidence_type or "").strip(),
        "source": str(source or "").strip(),
        "finding_summary": str(finding_summary or "").strip(),
        "supporting_details": sd,
        # risk_indicators carries the rule_id (parity with VRA's finding shape).
        "risk_indicators": [str(rule_id)],
        "severity": severity,
        "collected_at": collected_at or _utc_now_iso(),
    }


def make_domain_finding(
    *,
    rule_id: str,
    severity: str,
    finding_summary: str,
    account_id: str,
    entity_type: str,
    entity_id: str,
    entity_name: str = "",
    region: str = "",
    source: str = "",
    details: dict | None = None,
) -> dict:
    """Convenience builder for the non-SG domains.

    Pulls domain + category from ``metadata.RULE_META`` so domain modules only
    declare what they observe. Produces the same finding shape as ``make_finding``.
    """
    m = metadata.meta(rule_id)
    sd = {
        "account_id": account_id,
        "region": region or "",
        "entity_type": entity_type,
        "entity_id": entity_id,
        "entity_name": entity_name or entity_id,
        "rule_id": rule_id,
        **(details or {}),
    }
    fid = f"{account_id}:{m.get('domain', '')}:{rule_id}:{entity_id}"
    return make_finding(
        finding_id=fid,
        category=m.get("category", "hygiene"),
        rule_id=rule_id,
        severity=severity,
        finding_summary=finding_summary,
        supporting_details=sd,
        evidence_type=f"{m.get('domain', 'posture')}_finding",
        source=source or f"aws:{m.get('domain', '')}",
        domain=m.get("domain"),
    )


def severity_counts(findings: list[dict]) -> dict:
    counts = {sev: 0 for sev in SEVERITY_ORDER}
    for f in findings:
        sev = f.get("severity", SEV_INFO)
        if sev in counts:
            counts[sev] += 1
    return counts


def category_counts(findings: list[dict]) -> dict:
    counts = {cat: 0 for cat in CATEGORIES}
    for f in findings:
        cat = f.get("category")
        if cat in counts:
            counts[cat] += 1
    return counts


def rule_counts(findings: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for f in findings:
        rid = f.get("rule_id", "")
        if rid:
            counts[rid] = counts.get(rid, 0) + 1
    return counts


def domain_counts(findings: list[dict]) -> dict:
    """Per-domain {total, by_severity} (only domains with findings present)."""
    out: dict[str, dict] = {}
    for f in findings:
        dom = f.get("domain") or DOMAIN_SECURITY_GROUPS
        if dom not in out:
            out[dom] = {"total": 0, "by_severity": {sev: 0 for sev in SEVERITY_ORDER}}
        out[dom]["total"] += 1
        sev = f.get("severity", SEV_INFO)
        if sev in out[dom]["by_severity"]:
            out[dom]["by_severity"][sev] += 1
    return out


def risk_score(findings: list[dict]) -> float:
    """0-100 risk score from severity-weighted findings (higher = worse).

    Blends the worst finding (70%) with the mean severity weight (30%) so one
    critical dominates but breadth still moves the needle. Empty -> 0.0.
    """
    if not findings:
        return 0.0
    weights = [SEVERITY_WEIGHTS.get(f.get("severity", SEV_INFO), 0) for f in findings]
    worst = max(weights)
    mean = sum(weights) / len(weights)
    return round(0.7 * worst + 0.3 * mean, 1)


def build_snapshot(
    *,
    scan_id: str,
    audit_id: str,
    findings: list[dict],
    accounts_scanned: list[str] | None = None,
    collector_status: dict | None = None,
    scope: dict | None = None,
    scanned_at: str | None = None,
) -> dict:
    """Assemble the full audit snapshot the Lambda posts to the callback."""
    clean = list(findings or [])
    score = risk_score(clean)
    return {
        "scan_id": scan_id,
        "audit_id": audit_id,
        "scanned_at": scanned_at or _utc_now_iso(),
        "findings": clean,
        "accounts_scanned": accounts_scanned or [],
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
        # Per-account / per-region ok|error map; lets the dashboard show
        # partial-scan state without ever failing the whole run.
        "collector_status": collector_status or {},
    }


REQUIRED_FINDING_FIELDS = (
    "finding_id",
    "category",
    "rule_id",
    "evidence_type",
    "source",
    "finding_summary",
    "severity",
    "collected_at",
)


def validate_finding(finding: dict) -> bool:
    """True if ``finding`` is structurally valid (used by the callback)."""
    if not isinstance(finding, dict):
        return False
    if any(field not in finding for field in REQUIRED_FINDING_FIELDS):
        return False
    if finding.get("category") not in CATEGORIES:
        return False
    if finding.get("severity") not in SEVERITY_ORDER:
        return False
    return True
