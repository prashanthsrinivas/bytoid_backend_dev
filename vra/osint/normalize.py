"""Shared OSINT finding contract — the app <-> Lambda interface.

Both the Lambda collector and the app callback bind to this shape, so it is the
one place the schema is defined and validated. A *finding* maps 1:1 to a runbook
evidence record (Source / Collection date / Evidence type / Finding summary /
Supporting details / Risk indicators). A *snapshot* is one scan's worth of
findings plus derived counts and a 0-100 trend score.

Dependency-free (stdlib only) so it vendors cleanly into the Lambda.
"""

from __future__ import annotations

from datetime import datetime, timezone

from vra.schema import (
    CATEGORIES,
    SEVERITY_ORDER,
    SEVERITY_WEIGHTS,
    SEV_INFO,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_finding(
    *,
    category: str,
    evidence_type: str,
    source: str,
    finding_summary: str,
    source_url: str = "",
    supporting_details: dict | None = None,
    risk_indicators: list[str] | None = None,
    severity: str = SEV_INFO,
    collected_at: str | None = None,
) -> dict:
    """Build one validated, normalized finding record.

    Raises ``ValueError`` on an unknown category or severity so a malformed
    collector fails loudly in tests rather than silently producing junk.
    """
    if category not in CATEGORIES:
        raise ValueError(f"Unknown category: {category!r}")
    if severity not in SEVERITY_ORDER:
        raise ValueError(f"Unknown severity: {severity!r}")
    return {
        "category": category,
        "evidence_type": str(evidence_type or "").strip(),
        "source": str(source or "").strip(),
        "source_url": str(source_url or "").strip(),
        "finding_summary": str(finding_summary or "").strip(),
        "supporting_details": supporting_details or {},
        "risk_indicators": [str(r).strip() for r in (risk_indicators or []) if str(r).strip()],
        "severity": severity,
        "collected_at": collected_at or _utc_now_iso(),
    }


def severity_counts(findings: list[dict]) -> dict:
    """Count findings per severity label (all labels present, zero-filled)."""
    counts = {sev: 0 for sev in SEVERITY_ORDER}
    for f in findings:
        sev = f.get("severity", SEV_INFO)
        if sev in counts:
            counts[sev] += 1
    return counts


def category_counts(findings: list[dict]) -> dict:
    """Count findings per intelligence category (all categories zero-filled)."""
    counts = {cat: 0 for cat in CATEGORIES}
    for f in findings:
        cat = f.get("category")
        if cat in counts:
            counts[cat] += 1
    return counts


def snapshot_risk_score(findings: list[dict]) -> float:
    """Derive a 0-100 trend score from severity-weighted findings.

    Distinct from the runbook's authoritative Impact x Likelihood rating; used
    only for the dashboard trend line. Empty -> 0.0. The score blends the worst
    finding (70%) with the mean severity weight (30%) so one critical dominates
    but breadth still moves the needle.
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
    assessment_id: str,
    vendor_name: str,
    vendor_domain: str,
    findings: list[dict],
    collector_status: dict | None = None,
    scanned_at: str | None = None,
) -> dict:
    """Assemble the full scan snapshot the Lambda posts to the callback."""
    clean = list(findings or [])
    return {
        "scan_id": scan_id,
        "assessment_id": assessment_id,
        "vendor_name": vendor_name,
        "vendor_domain": vendor_domain,
        "scanned_at": scanned_at or _utc_now_iso(),
        "findings": clean,
        "counts": {
            "total": len(clean),
            "by_severity": severity_counts(clean),
            "by_category": category_counts(clean),
        },
        "risk_score": snapshot_risk_score(clean),
        # Per-collector ok/error map; lets the dashboard show partial-scan state
        # without ever failing the whole run.
        "collector_status": collector_status or {},
    }


# Fields a finding must contain to be accepted by the callback (defense in depth
# against a malformed/old Lambda payload).
REQUIRED_FINDING_FIELDS = (
    "category",
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
