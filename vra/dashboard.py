"""Vendor Intelligence Dashboard model — pure aggregation over a snapshot.

Builds the executive + analyst + evidence views the dashboard renders, from the
assessment record, the latest snapshot, and the trend series. No I/O here; the
route reads from S3 and hands the data in. The same snapshot powers both the
executive cards and the granular drill-down (one experience, per the spec).
"""

from __future__ import annotations

from vra.config import VRA_DASHBOARD_BASE_URL
from vra.evidence import evidence_by_category, snapshot_to_evidence
from vra.report_inputs import build_analysis_context, key_observations, risk_rating, trend
from vra.schema import (
    CATEGORIES,
    CATEGORY_LABELS,
    SEV_CRITICAL,
    SEV_HIGH,
    SEV_INFO,
    SEV_LOW,
    SEV_MEDIUM,
    SEVERITY_ORDER,
)

# Categories surfaced as their own dashboard panels (others still appear in
# the category summary + drill-down).
_PANEL_CATEGORIES = ("corporate", "security", "compliance", "reputation")


def dashboard_url(assessment_id: str) -> str:
    """Public URL of the live dashboard for an assessment (or '' if unset)."""
    base = (VRA_DASHBOARD_BASE_URL or "").rstrip("/")
    return f"{base}/vra/dashboard/{assessment_id}" if base else ""


def _worst_severity(findings: list[dict]) -> str:
    worst = SEV_INFO
    for f in findings:
        sev = f.get("severity", SEV_INFO)
        if SEVERITY_ORDER.index(sev) > SEVERITY_ORDER.index(worst):
            worst = sev
    return worst


def _compliance_coverage(findings: list[dict]) -> dict:
    certs: list[str] = []
    has_security_txt = False
    has_trust_center = False
    for f in findings:
        et = f.get("evidence_type")
        if et == "certification_mention":
            certs.extend(f.get("supporting_details", {}).get("certifications", []))
        elif et == "security_txt" and not f.get("risk_indicators"):
            has_security_txt = True
        elif et == "trust_center":
            has_trust_center = True
    return {
        "certifications": sorted(set(certs)),
        "has_security_txt": has_security_txt,
        "has_trust_center": has_trust_center,
    }


def build_dashboard(
    record: dict,
    snapshot: dict | None,
    trend_points: list[dict] | None = None,
    *,
    prior_scores: list[float] | None = None,
) -> dict:
    """Assemble the full dashboard model. ``snapshot`` may be None (never scanned)."""
    assessment_id = record.get("assessment_id", "")
    vendor_name = record.get("vendor_name", "")
    trend_points = trend_points or []

    if not snapshot:
        return {
            "assessment_id": assessment_id,
            "dashboard_url": dashboard_url(assessment_id),
            "executive_summary": {
                "vendor_name": vendor_name,
                "scan_state": record.get("scan_state", "pending"),
                "last_scan_date": None,
                "overall_risk_rating": "Unknown",
                "total_findings": 0,
            },
            "scanned": False,
        }

    findings = snapshot.get("findings") or []
    counts = snapshot.get("counts") or {}
    by_sev = counts.get("by_severity") or {}
    grouped = evidence_by_category(snapshot)
    score = snapshot.get("risk_score", 0.0)

    return {
        "assessment_id": assessment_id,
        "dashboard_url": dashboard_url(assessment_id),
        "scanned": True,
        # ---- Executive Summary ----
        "executive_summary": {
            "vendor_name": vendor_name,
            "overall_risk_rating": risk_rating(snapshot),
            "risk_trend": trend(prior_scores, score)["direction"],
            "last_scan_date": snapshot.get("scanned_at"),
            "total_findings": counts.get("total", len(findings)),
            "critical_findings": by_sev.get(SEV_CRITICAL, 0),
            "high_findings": by_sev.get(SEV_HIGH, 0),
            "medium_findings": by_sev.get(SEV_MEDIUM, 0),
            "low_findings": by_sev.get(SEV_LOW, 0),
        },
        # ---- Risk Overview Visualizations ----
        "risk_overview": {
            "risk_score": score,
            "risk_rating": risk_rating(snapshot),
            "trend_chart": trend_points,  # [{scanned_at, risk_score}, ...]
            "finding_distribution": {s: by_sev.get(s, 0) for s in SEVERITY_ORDER},
            "compliance_coverage": _compliance_coverage(findings),
            "category_summary": [
                {
                    "category": cat,
                    "label": CATEGORY_LABELS.get(cat, cat),
                    "count": len(grouped.get(cat, [])),
                    "worst_severity": _worst_severity(grouped.get(cat, [])),
                }
                for cat in CATEGORIES
            ],
        },
        # ---- Category panels (executive view) ----
        "categories": {
            cat: {
                "label": CATEGORY_LABELS.get(cat, cat),
                "count": len(grouped.get(cat, [])),
                "records": grouped.get(cat, []),
            }
            for cat in _PANEL_CATEGORIES
        },
        # ---- Granular evidence drill-down (same experience) ----
        "evidence": snapshot_to_evidence(snapshot),
        "key_observations": key_observations(snapshot),
        "analysis": build_analysis_context(snapshot, prior_scores),
    }
