"""Map Cloud Security Posture tables to Trackers + runbook Responses & Evidence.

One place that turns any dashboard table (findings / per_account / per_domain /
compliance / priority_queue / remediations) into:
  * tracker **columns + rows** (consumed by tab_tracker via append_to_tracker), and
  * canonical **evidence records** (the vra/evidence.py "Responses & Evidence"
    shape: source / source_url / collection_date / evidence_type / category /
    finding_summary / supporting_details / risk_indicators / severity).

Pure ``build_table`` (snapshot in, table dict out) is unit-testable; ``load_and_build``
adds the S3 load. The actual tracker/runbook writes live in the routes /
runbook_evidence module.
"""

from __future__ import annotations

from sg_audit import config as sg_config, metadata
from sg_audit.analysis import score
from sg_audit.compliance import FRAMEWORKS, coverage_for
from sg_audit.schema import CATEGORY_LABELS, DOMAIN_LABELS, SEVERITY_ORDER

TABLES = ("findings", "per_account", "per_domain", "compliance", "priority_queue", "remediations")

_SEV_ENUM = list(SEVERITY_ORDER)
_RATING_ENUM = ["Low", "Medium", "High", "Critical", "Unknown"]


def _dash_url(audit_id: str) -> str:
    base = (sg_config.SG_DASHBOARD_BASE_URL or "").rstrip("/")
    return f"{base}/security-group-audit/{audit_id}" if base else ""


def _ev(audit_id, *, evidence_type, category, summary, details, indicators, severity, collected_at):
    return {
        "source": "sg_audit",
        "source_url": _dash_url(audit_id),
        "collection_date": collected_at or "",
        "evidence_type": evidence_type,
        "category": category,
        "category_label": CATEGORY_LABELS.get(category, ""),
        "finding_summary": summary,
        "supporting_details": details or {},
        "risk_indicators": indicators or [],
        "severity": severity or "info",
    }


# ── per-table builders: snapshot -> {name, columns, rows, evidence} ───────────

def _b_findings(snapshot, audit_id, **_):
    findings = snapshot.get("findings") or []
    columns = [
        {"name": "Severity", "type": "select", "enum": _SEV_ENUM},
        {"name": "Domain", "type": "text"},
        {"name": "Rule", "type": "text"},
        {"name": "Account", "type": "text"},
        {"name": "Region", "type": "text"},
        {"name": "Entity", "type": "text"},
        {"name": "Finding", "type": "text"},
    ]
    rows, evidence = [], []
    for f in findings:
        d = f.get("supporting_details", {}) or {}
        rid = f.get("rule_id", "")
        rows.append({
            "Severity": f.get("severity", "info"),
            "Domain": DOMAIN_LABELS.get(f.get("domain", ""), f.get("domain", "")),
            "Rule": metadata.rule_label(rid),
            "Account": d.get("account_id", ""),
            "Region": d.get("region") or "global",
            "Entity": d.get("entity_name") or d.get("entity_id", ""),
            "Finding": f.get("finding_summary", ""),
        })
        evidence.append(_ev(
            audit_id, evidence_type=f.get("domain", "posture"), category=f.get("category", ""),
            summary=f.get("finding_summary", ""), details=d, indicators=f.get("risk_indicators", []),
            severity=f.get("severity"), collected_at=f.get("collected_at", snapshot.get("scanned_at")),
        ))
    return {"name": "Posture Findings", "columns": columns, "rows": rows, "evidence": evidence}


def _b_per_account(snapshot, audit_id, **_):
    rows_src = score.per_account(snapshot.get("findings") or [], snapshot.get("collector_status"))
    columns = [
        {"name": "Account", "type": "text"},
        {"name": "Posture", "type": "number"},
        {"name": "Rating", "type": "select", "enum": _RATING_ENUM},
        {"name": "Critical", "type": "number"},
        {"name": "High", "type": "number"},
        {"name": "Findings", "type": "number"},
    ]
    rows, evidence = [], []
    for r in rows_src:
        sev = r.get("by_severity", {})
        rows.append({
            "Account": r.get("account_name") or r.get("account_id", ""),
            "Posture": r.get("posture_score", 0), "Rating": r.get("rating", "Low"),
            "Critical": sev.get("critical", 0), "High": sev.get("high", 0), "Findings": r.get("total", 0),
        })
        evidence.append(_ev(
            audit_id, evidence_type="account_posture", category="access_control",
            summary=f"Account {r.get('account_id')} posture {r.get('posture_score')}/100 ({r.get('rating')})",
            details=r, indicators=[], severity=r.get("worst_severity", "info"),
            collected_at=snapshot.get("scanned_at"),
        ))
    return {"name": "Posture by Account", "columns": columns, "rows": rows, "evidence": evidence}


def _b_per_domain(snapshot, audit_id, **_):
    rows_src = score.per_domain(snapshot.get("findings") or [])
    columns = [
        {"name": "Domain", "type": "text"},
        {"name": "Posture", "type": "number"},
        {"name": "Rating", "type": "select", "enum": _RATING_ENUM},
        {"name": "Critical", "type": "number"},
        {"name": "High", "type": "number"},
        {"name": "Findings", "type": "number"},
    ]
    rows, evidence = [], []
    for r in rows_src:
        sev = r.get("by_severity", {})
        rows.append({
            "Domain": r.get("label", r.get("domain", "")), "Posture": r.get("posture_score", 0),
            "Rating": r.get("rating", "Low"), "Critical": sev.get("critical", 0),
            "High": sev.get("high", 0), "Findings": r.get("total", 0),
        })
        evidence.append(_ev(
            audit_id, evidence_type="domain_posture", category="access_control",
            summary=f"{r.get('label')} posture {r.get('posture_score')}/100 ({r.get('rating')})",
            details=r, indicators=[], severity=r.get("worst_severity", "info"),
            collected_at=snapshot.get("scanned_at"),
        ))
    return {"name": "Posture by Domain", "columns": columns, "rows": rows, "evidence": evidence}


def _b_compliance(snapshot, audit_id, framework=None, **_):
    fw = framework if framework in FRAMEWORKS else "CIS"
    cov = coverage_for(snapshot, fw)
    columns = [
        {"name": "Control", "type": "text"},
        {"name": "Family", "type": "text"},
        {"name": "Status", "type": "select", "enum": ["pass", "fail"]},
        {"name": "Findings", "type": "number"},
    ]
    rows, evidence = [], []
    for c in cov.get("controls", []):
        rows.append({
            "Control": c.get("control", ""), "Family": c.get("family_label", ""),
            "Status": c.get("status", "pass"), "Findings": c.get("finding_count", 0),
        })
        if c.get("status") == "fail":
            evidence.append(_ev(
                audit_id, evidence_type=f"{fw.lower()}_control", category="monitoring",
                summary=f"{fw} control {c.get('control')} ({c.get('family_label')}) failing: "
                        f"{c.get('finding_count')} finding(s)",
                details=c, indicators=[c.get("control", "")], severity="medium",
                collected_at=snapshot.get("scanned_at"),
            ))
    return {"name": f"{cov.get('framework_label', fw)} Coverage", "columns": columns,
            "rows": rows, "evidence": evidence}


def _b_priority_queue(snapshot, audit_id, **_):
    q = score.remediation_priority_queue(snapshot.get("findings") or [], 200)
    columns = [
        {"name": "Rank", "type": "number"},
        {"name": "Priority", "type": "number"},
        {"name": "Severity", "type": "select", "enum": _SEV_ENUM},
        {"name": "Rule", "type": "text"},
        {"name": "Entity", "type": "text"},
        {"name": "Account", "type": "text"},
        {"name": "Effort", "type": "select", "enum": ["low", "medium", "high"]},
        {"name": "Recommended Fix", "type": "text"},
    ]
    rows, evidence = [], []
    for it in q:
        rows.append({
            "Rank": it.get("rank", 0), "Priority": it.get("priority", 0),
            "Severity": it.get("severity", "info"), "Rule": it.get("rule_label", ""),
            "Entity": it.get("entity_name") or it.get("entity_id", ""), "Account": it.get("account_id", ""),
            "Effort": it.get("effort", "medium"), "Recommended Fix": it.get("remediation", ""),
        })
        evidence.append(_ev(
            audit_id, evidence_type="remediation_priority", category="access_control",
            summary=f"#{it.get('rank')} {it.get('rule_label')} — {it.get('remediation')}",
            details=it, indicators=it.get("cis", []), severity=it.get("severity"),
            collected_at=snapshot.get("scanned_at"),
        ))
    return {"name": "Remediation Priority Queue", "columns": columns, "rows": rows, "evidence": evidence}


def _b_remediations(snapshot, audit_id, remediation_links=None, **_):
    links = list((remediation_links or {}).values())
    columns = [
        {"name": "Finding", "type": "text"},
        {"name": "Rule", "type": "text"},
        {"name": "Approver", "type": "text"},
        {"name": "State", "type": "text"},
        {"name": "Requested", "type": "date"},
    ]
    rows, evidence = [], []
    for link in links:
        rows.append({
            "Finding": link.get("summary", ""), "Rule": link.get("rule_label", ""),
            "Approver": link.get("approver_email") or link.get("approver", ""),
            "State": (link.get("state", "") or "").replace("_", " "),
            "Requested": (link.get("requested_at", "") or "")[:10],
        })
        evidence.append(_ev(
            audit_id, evidence_type="remediation_approval", category="monitoring",
            summary=f"Remediation '{link.get('rule_label')}' — {link.get('state')}",
            details=link, indicators=[], severity="info", collected_at=link.get("requested_at"),
        ))
    return {"name": "Remediation Approvals", "columns": columns, "rows": rows, "evidence": evidence}


_BUILDERS = {
    "findings": _b_findings,
    "per_account": _b_per_account,
    "per_domain": _b_per_domain,
    "compliance": _b_compliance,
    "priority_queue": _b_priority_queue,
    "remediations": _b_remediations,
}


def build_table(snapshot: dict, table: str, *, framework: str | None = None, remediation_links: dict | None = None) -> dict | None:
    """Pure: snapshot -> {name, columns, rows, evidence} for one table."""
    builder = _BUILDERS.get(table)
    if not builder:
        return None
    audit_id = snapshot.get("audit_id", "")
    return builder(snapshot, audit_id, framework=framework, remediation_links=remediation_links)


def load_and_build(user_id: str, audit_id: str, table: str, framework: str | None = None) -> dict | None:
    """Load the latest snapshot (+ remediation links) and build the table."""
    from sg_audit.service import SgAuditService

    service = SgAuditService()
    snapshot = service.storage.get_latest_snapshot(user_id, audit_id)
    if not snapshot:
        return None
    snapshot.setdefault("audit_id", audit_id)
    links = service.storage.get_remediation_links(user_id, audit_id) if table == "remediations" else None
    return build_table(snapshot, table, framework=framework, remediation_links=links)
