"""Security Posture Dashboard model — pure aggregation over a snapshot.

Builds the executive + per-account + per-region + per-SG drill-down views the
dashboard renders, from the audit record, the latest snapshot, and the trend
series. No I/O here; the route reads from S3 and hands the data in.
"""

from __future__ import annotations

from sg_audit.analysis.score import (
    executive_rollup,
    global_posture,
    per_account,
    per_domain,
    per_entity,
    per_region,
    per_security_group,
)
from sg_audit.compliance import all_frameworks, compliance_coverage
from sg_audit.config import SG_DASHBOARD_BASE_URL
from sg_audit.report_inputs import build_analysis_context
from sg_audit.schema import SEVERITY_ORDER


def dashboard_url(audit_id: str) -> str:
    base = (SG_DASHBOARD_BASE_URL or "").rstrip("/")
    return f"{base}/security-group-audit/{audit_id}" if base else ""


def build_dashboard(
    record: dict,
    snapshot: dict | None,
    trend_points: list[dict] | None = None,
    *,
    prior_scores: list[float] | None = None,
) -> dict:
    """Assemble the full dashboard model. ``snapshot`` may be None (never scanned)."""
    audit_id = record.get("audit_id", "")
    trend_points = trend_points or []

    if not snapshot:
        return {
            "audit_id": audit_id,
            "name": record.get("name", ""),
            "dashboard_url": dashboard_url(audit_id),
            "scanned": False,
            "executive_summary": {
                "name": record.get("name", ""),
                "scan_state": record.get("scan_state", "pending"),
                "last_scan_date": None,
                "rating": "Unknown",
                "posture_score": None,
                "total_findings": 0,
            },
        }

    exec_roll = executive_rollup(snapshot)
    findings = snapshot.get("findings") or []
    collector_status = snapshot.get("collector_status") or {}

    return {
        "audit_id": audit_id,
        "name": record.get("name", ""),
        "dashboard_url": dashboard_url(audit_id),
        "scanned": True,
        # ---- Executive summary ----
        "executive_summary": {
            "name": record.get("name", ""),
            "scan_state": record.get("scan_state", "complete"),
            "last_scan_date": snapshot.get("scanned_at"),
            "rating": exec_roll["rating"],
            "posture_score": exec_roll["posture_score"],
            "risk_score": exec_roll["risk_score"],
            "risk_trend": build_analysis_context(snapshot, prior_scores)["trend"]["direction"],
            "total_findings": exec_roll["total_findings"],
            "critical_findings": exec_roll["by_severity"].get("critical", 0),
            "high_findings": exec_roll["by_severity"].get("high", 0),
            "medium_findings": exec_roll["by_severity"].get("medium", 0),
            "low_findings": exec_roll["by_severity"].get("low", 0),
            "accounts_scanned": exec_roll["accounts_scanned"],
            "accounts_with_critical": exec_roll["accounts_with_critical"],
        },
        # ---- Risk overview visualizations ----
        "risk_overview": {
            "posture_score": exec_roll["posture_score"],
            "risk_score": exec_roll["risk_score"],
            "rating": exec_roll["rating"],
            "trend_chart": trend_points,  # [{scanned_at, risk_score, posture_score}, ...]
            "finding_distribution": {s: exec_roll["by_severity"].get(s, 0) for s in SEVERITY_ORDER},
            "by_category": exec_roll["by_category"],
            "by_rule": exec_roll["by_rule"],
        },
        # ---- Global (cross-domain) posture ----
        "global": global_posture(snapshot),
        "per_domain": per_domain(findings),
        "compliance": compliance_coverage(snapshot),
        "compliance_frameworks": all_frameworks(snapshot),
        # ---- Drill-downs ----
        "per_account": per_account(findings, collector_status),
        "per_region": per_region(findings),
        "per_security_group": per_security_group(findings),
        "per_entity": per_entity(findings),
        # ---- Partial-scan transparency ----
        "collector_status": collector_status,
        "analysis": build_analysis_context(snapshot, prior_scores),
    }
