"""Security Posture Dashboard model — pure aggregation (provider-aware)."""

from __future__ import annotations

from cspm_core.compliance import all_frameworks, coverage_for
from cspm_core.report_inputs import build_analysis_context
from cspm_core.schema import SEVERITY_ORDER
from cspm_core.score import (
    executive_rollup,
    global_posture,
    per_domain,
    per_entity,
    per_scope,
)


def build_dashboard(provider, record, snapshot, trend_points=None, *, prior_scores=None) -> dict:
    audit_id = record.get("audit_id", "")
    trend_points = trend_points or []
    if not snapshot:
        return {
            "audit_id": audit_id, "name": record.get("name", ""), "scanned": False,
            "executive_summary": {"name": record.get("name", ""), "scan_state": record.get("scan_state", "pending"),
                                  "last_scan_date": None, "rating": "Unknown", "posture_score": None, "total_findings": 0},
        }
    exec_roll = executive_rollup(snapshot, provider)
    findings = snapshot.get("findings") or []
    collector_status = snapshot.get("collector_status") or {}
    return {
        "audit_id": audit_id, "name": record.get("name", ""), "provider": provider.key, "scanned": True,
        "executive_summary": {
            "name": record.get("name", ""), "scan_state": record.get("scan_state", "complete"),
            "last_scan_date": snapshot.get("scanned_at"), "rating": exec_roll["rating"],
            "posture_score": exec_roll["posture_score"], "risk_score": exec_roll["risk_score"],
            "risk_trend": build_analysis_context(provider, snapshot, prior_scores)["trend"]["direction"],
            "total_findings": exec_roll["total_findings"],
            "critical_findings": exec_roll["by_severity"].get("critical", 0),
            "high_findings": exec_roll["by_severity"].get("high", 0),
            "medium_findings": exec_roll["by_severity"].get("medium", 0),
            "low_findings": exec_roll["by_severity"].get("low", 0),
            "scopes_scanned": exec_roll["scopes_scanned"], "scopes_with_critical": exec_roll["scopes_with_critical"],
        },
        "risk_overview": {
            "posture_score": exec_roll["posture_score"], "risk_score": exec_roll["risk_score"],
            "rating": exec_roll["rating"], "trend_chart": trend_points,
            "finding_distribution": {s: exec_roll["by_severity"].get(s, 0) for s in SEVERITY_ORDER},
            "by_category": exec_roll["by_category"], "by_rule": exec_roll["by_rule"],
        },
        "global": global_posture(snapshot, provider),
        "per_domain": per_domain(findings, provider),
        "compliance": coverage_for(snapshot, provider, "CIS"),
        "compliance_frameworks": all_frameworks(snapshot, provider),
        "per_scope": per_scope(findings, collector_status),
        "per_entity": per_entity(findings),
        "collector_status": collector_status,
        "analysis": build_analysis_context(provider, snapshot, prior_scores),
    }
