"""Map CSPM tables → Trackers + runbook Responses & Evidence (provider-aware).

Turns any dashboard table (findings / per_scope / per_domain / compliance /
priority_queue / remediations) into tracker columns+rows and canonical evidence
records (the vra/evidence.py shape). Pure ``build_table``; ``load_and_build`` adds
the S3 load. ``scope`` is a subscription (Azure) or project (GCP).
"""

from __future__ import annotations

from cspm_core import score
from cspm_core.compliance import FRAMEWORKS, coverage_for
from cspm_core.schema import CATEGORY_LABELS, SEVERITY_ORDER

TABLES = ("findings", "per_scope", "per_domain", "compliance", "priority_queue", "remediations")

_SEV_ENUM = list(SEVERITY_ORDER)
_RATING_ENUM = ["Low", "Medium", "High", "Critical", "Unknown"]


def _ev(provider, *, evidence_type, category, summary, details, indicators, severity, collected_at):
    return {
        "source": provider.key, "source_url": "", "collection_date": collected_at or "",
        "evidence_type": evidence_type, "category": category,
        "category_label": CATEGORY_LABELS.get(category, ""), "finding_summary": summary,
        "supporting_details": details or {}, "risk_indicators": indicators or [], "severity": severity or "info",
    }


def _b_findings(provider, snapshot, **_):
    columns = [
        {"name": "Severity", "type": "select", "enum": _SEV_ENUM}, {"name": "Domain", "type": "text"},
        {"name": "Rule", "type": "text"}, {"name": provider.scope_label.title(), "type": "text"},
        {"name": "Region", "type": "text"}, {"name": "Entity", "type": "text"}, {"name": "Finding", "type": "text"},
    ]
    rows, evidence = [], []
    for f in snapshot.get("findings") or []:
        d = f.get("supporting_details", {}) or {}
        rid = f.get("rule_id", "")
        rows.append({
            "Severity": f.get("severity", "info"),
            "Domain": provider.domain_labels.get(f.get("domain", ""), f.get("domain", "")),
            "Rule": provider.rule_label(rid), provider.scope_label.title(): d.get("scope_name") or d.get("scope_id", ""),
            "Region": d.get("region") or "global", "Entity": d.get("entity_name") or d.get("entity_id", ""),
            "Finding": f.get("finding_summary", ""),
        })
        evidence.append(_ev(provider, evidence_type=f.get("domain", "posture"), category=f.get("category", ""),
                            summary=f.get("finding_summary", ""), details=d, indicators=f.get("risk_indicators", []),
                            severity=f.get("severity"), collected_at=f.get("collected_at", snapshot.get("scanned_at"))))
    return {"name": "Posture Findings", "columns": columns, "rows": rows, "evidence": evidence}


def _b_per_scope(provider, snapshot, **_):
    rows_src = score.per_scope(snapshot.get("findings") or [], snapshot.get("collector_status"))
    label = provider.scope_label.title()
    columns = [{"name": label, "type": "text"}, {"name": "Posture", "type": "number"},
               {"name": "Rating", "type": "select", "enum": _RATING_ENUM}, {"name": "Critical", "type": "number"},
               {"name": "High", "type": "number"}, {"name": "Findings", "type": "number"}]
    rows, evidence = [], []
    for r in rows_src:
        sev = r.get("by_severity", {})
        rows.append({label: r.get("scope_name") or r.get("scope_id", ""), "Posture": r.get("posture_score", 0),
                     "Rating": r.get("rating", "Low"), "Critical": sev.get("critical", 0),
                     "High": sev.get("high", 0), "Findings": r.get("total", 0)})
        evidence.append(_ev(provider, evidence_type="scope_posture", category="access_control",
                            summary=f"{label} {r.get('scope_id')} posture {r.get('posture_score')}/100 ({r.get('rating')})",
                            details=r, indicators=[], severity=r.get("worst_severity", "info"),
                            collected_at=snapshot.get("scanned_at")))
    return {"name": f"Posture by {label}", "columns": columns, "rows": rows, "evidence": evidence}


def _b_per_domain(provider, snapshot, **_):
    rows_src = score.per_domain(snapshot.get("findings") or [], provider)
    columns = [{"name": "Domain", "type": "text"}, {"name": "Posture", "type": "number"},
               {"name": "Rating", "type": "select", "enum": _RATING_ENUM}, {"name": "Critical", "type": "number"},
               {"name": "High", "type": "number"}, {"name": "Findings", "type": "number"}]
    rows, evidence = [], []
    for r in rows_src:
        sev = r.get("by_severity", {})
        rows.append({"Domain": r.get("label", r.get("domain", "")), "Posture": r.get("posture_score", 0),
                     "Rating": r.get("rating", "Low"), "Critical": sev.get("critical", 0),
                     "High": sev.get("high", 0), "Findings": r.get("total", 0)})
        evidence.append(_ev(provider, evidence_type="domain_posture", category="access_control",
                            summary=f"{r.get('label')} posture {r.get('posture_score')}/100 ({r.get('rating')})",
                            details=r, indicators=[], severity=r.get("worst_severity", "info"),
                            collected_at=snapshot.get("scanned_at")))
    return {"name": "Posture by Domain", "columns": columns, "rows": rows, "evidence": evidence}


def _b_compliance(provider, snapshot, framework=None, **_):
    fw = framework if framework in FRAMEWORKS else "CIS"
    cov = coverage_for(snapshot, provider, fw)
    columns = [{"name": "Control", "type": "text"}, {"name": "Family", "type": "text"},
               {"name": "Status", "type": "select", "enum": ["pass", "fail"]}, {"name": "Findings", "type": "number"}]
    rows, evidence = [], []
    for c in cov.get("controls", []):
        rows.append({"Control": c.get("control", ""), "Family": c.get("family_label", ""),
                     "Status": c.get("status", "pass"), "Findings": c.get("finding_count", 0)})
        if c.get("status") == "fail":
            evidence.append(_ev(provider, evidence_type=f"{fw.lower()}_control", category="monitoring",
                                summary=f"{fw} control {c.get('control')} ({c.get('family_label')}) failing: {c.get('finding_count')} finding(s)",
                                details=c, indicators=[c.get("control", "")], severity="medium",
                                collected_at=snapshot.get("scanned_at")))
    return {"name": f"{cov.get('framework_label', fw)} Coverage", "columns": columns, "rows": rows, "evidence": evidence}


def _b_priority_queue(provider, snapshot, **_):
    q = score.remediation_priority_queue(snapshot.get("findings") or [], provider, 200)
    columns = [{"name": "Rank", "type": "number"}, {"name": "Priority", "type": "number"},
               {"name": "Severity", "type": "select", "enum": _SEV_ENUM}, {"name": "Rule", "type": "text"},
               {"name": "Entity", "type": "text"}, {"name": provider.scope_label.title(), "type": "text"},
               {"name": "Effort", "type": "select", "enum": ["low", "medium", "high"]}, {"name": "Recommended Fix", "type": "text"}]
    rows, evidence = [], []
    for it in q:
        rows.append({"Rank": it.get("rank", 0), "Priority": it.get("priority", 0), "Severity": it.get("severity", "info"),
                     "Rule": it.get("rule_label", ""), "Entity": it.get("entity_name") or it.get("entity_id", ""),
                     provider.scope_label.title(): it.get("scope_id", ""), "Effort": it.get("effort", "medium"),
                     "Recommended Fix": it.get("remediation", "")})
        evidence.append(_ev(provider, evidence_type="remediation_priority", category="access_control",
                            summary=f"#{it.get('rank')} {it.get('rule_label')} — {it.get('remediation')}",
                            details=it, indicators=it.get("cis", []), severity=it.get("severity"),
                            collected_at=snapshot.get("scanned_at")))
    return {"name": "Remediation Priority Queue", "columns": columns, "rows": rows, "evidence": evidence}


def _b_remediations(provider, snapshot, remediation_links=None, **_):
    columns = [{"name": "Finding", "type": "text"}, {"name": "Rule", "type": "text"}, {"name": "Approver", "type": "text"},
               {"name": "State", "type": "text"}, {"name": "Requested", "type": "date"}]
    rows, evidence = [], []
    for link in (remediation_links or {}).values():
        rows.append({"Finding": link.get("summary", ""), "Rule": link.get("rule_label", ""),
                     "Approver": link.get("approver_email") or link.get("approver", ""),
                     "State": (link.get("state", "") or "").replace("_", " "),
                     "Requested": (link.get("requested_at", "") or "")[:10]})
        evidence.append(_ev(provider, evidence_type="remediation_approval", category="monitoring",
                            summary=f"Remediation '{link.get('rule_label')}' — {link.get('state')}",
                            details=link, indicators=[], severity="info", collected_at=link.get("requested_at")))
    return {"name": "Remediation Approvals", "columns": columns, "rows": rows, "evidence": evidence}


_BUILDERS = {"findings": _b_findings, "per_scope": _b_per_scope, "per_domain": _b_per_domain,
             "compliance": _b_compliance, "priority_queue": _b_priority_queue, "remediations": _b_remediations}


def build_table(provider, snapshot, table, *, framework=None, remediation_links=None) -> dict | None:
    builder = _BUILDERS.get(table)
    if not builder:
        return None
    return builder(provider, snapshot, framework=framework, remediation_links=remediation_links)


def load_and_build(provider, user_id, audit_id, table, framework=None) -> dict | None:
    from cspm_core.service import CspmService

    service = CspmService(provider)
    snapshot = service.storage.get_latest_snapshot(user_id, audit_id)
    if not snapshot:
        return None
    snapshot.setdefault("audit_id", audit_id)
    links = service.storage.get_remediation_links(user_id, audit_id) if table == "remediations" else None
    return build_table(provider, snapshot, table, framework=framework, remediation_links=links)
