"""Posture scoring + rollups (pure aggregation over a snapshot's findings).

Provider-parameterized: labels/metadata come from the ``Provider`` so the same
math serves every cloud. Mirrors ``sg_audit/analysis/score.py`` with ``scope_id``
(subscription/project) in place of AWS's account_id.
"""

from __future__ import annotations

from cspm_core.normalize import (
    category_counts,
    domain_counts,
    risk_score,
    rule_counts,
    severity_counts,
)
from cspm_core.schema import (
    SEV_CRITICAL,
    SEV_HIGH,
    SEV_INFO,
    SEVERITY_ORDER,
    SEVERITY_WEIGHTS,
    rating_for,
)

_EASE_BONUS = {"low": 100, "medium": 60, "high": 30}


def worst_severity(findings) -> str:
    worst, wi = SEV_INFO, 0
    for f in findings:
        sev = f.get("severity", SEV_INFO)
        i = SEVERITY_ORDER.index(sev) if sev in SEVERITY_ORDER else 0
        if i > wi:
            wi, worst = i, sev
    return worst


def _scored_block(findings) -> dict:
    by_sev = severity_counts(findings)
    score = risk_score(findings)
    return {
        "risk_score": score, "posture_score": round(100.0 - score, 1),
        "rating": rating_for(score, has_critical=by_sev.get(SEV_CRITICAL, 0) > 0),
        "worst_severity": worst_severity(findings), "total": len(findings), "by_severity": by_sev,
    }


def _group_by(findings, *keys):
    grouped: dict = {}
    for f in findings:
        d = f.get("supporting_details", {}) or {}
        k = tuple(d.get(key, "") for key in keys)
        grouped.setdefault(k, []).append(f)
    return grouped


def per_domain(findings, provider) -> list:
    grouped: dict = {}
    for f in findings:
        grouped.setdefault(f.get("domain") or "hygiene", []).append(f)
    rows = [
        {"domain": dom, "label": provider.domain_labels.get(dom, dom), **_scored_block(items)}
        for dom, items in grouped.items()
    ]
    rows.sort(key=lambda r: r["risk_score"], reverse=True)
    return rows


def per_scope(findings, collector_status=None) -> list:
    status = collector_status or {}
    rows = []
    for (scope_id, scope_name), items in _group_by(findings, "scope_id", "scope_name").items():
        sstat = {k: v for k, v in status.items() if k == scope_id or str(k).startswith(f"{scope_id}:")}
        rows.append({"scope_id": scope_id, "scope_name": scope_name or scope_id,
                     "collector_status": sstat, **_scored_block(items)})
    rows.sort(key=lambda r: r["risk_score"], reverse=True)
    return rows


def per_entity(findings) -> list:
    grouped = _group_by(findings, "entity_type", "entity_id", "entity_name", "scope_id", "region")
    rows = []
    for (etype, eid, ename, scope_id, region), items in grouped.items():
        rows.append({
            "domain": items[0].get("domain") if items else "",
            "entity_type": etype, "entity_id": eid, "entity_name": ename or eid,
            "scope_id": scope_id, "region": region, "findings": items, **_scored_block(items),
        })
    rows.sort(key=lambda r: r["risk_score"], reverse=True)
    return rows


def priority_score(finding, provider) -> float:
    rid = finding.get("rule_id", "")
    sev_w = SEVERITY_WEIGHTS.get(finding.get("severity", SEV_INFO), 0)
    expl, blast = provider.priority_inputs(rid)
    risk = (expl + blast) / 6.0 * 100.0
    ease = _EASE_BONUS.get(provider.effort_for(rid), 60)
    return round(0.5 * sev_w + 0.3 * risk + 0.2 * ease, 1)


def _queue_item(finding, rank, provider) -> dict:
    rid = finding.get("rule_id", "")
    d = finding.get("supporting_details", {}) or {}
    return {
        "rank": rank, "priority": priority_score(finding, provider),
        "finding_id": finding.get("finding_id", ""), "domain": finding.get("domain", ""),
        "rule_id": rid, "rule_label": provider.rule_label(rid), "severity": finding.get("severity"),
        "effort": provider.effort_for(rid), "entity_type": d.get("entity_type", ""),
        "entity_id": d.get("entity_id", ""), "entity_name": d.get("entity_name", d.get("entity_id", "")),
        "scope_id": d.get("scope_id", ""), "region": d.get("region", ""),
        "summary": finding.get("finding_summary", ""), "remediation": provider.remediation_for(rid),
        "cis": provider.cis_for(rid),
    }


def remediation_priority_queue(findings, provider, limit=50) -> list:
    ranked = sorted(findings, key=lambda f: priority_score(f, provider), reverse=True)
    return [_queue_item(f, i + 1, provider) for i, f in enumerate(ranked[:limit])]


def top_critical(findings, provider, limit=10) -> list:
    sig = [f for f in findings if f.get("severity") in (SEV_CRITICAL, SEV_HIGH)]
    sig.sort(key=lambda f: priority_score(f, provider), reverse=True)
    return [_queue_item(f, i + 1, provider) for i, f in enumerate(sig[:limit])]


def executive_rollup(snapshot, provider) -> dict:
    findings = snapshot.get("findings") or []
    by_sev = severity_counts(findings)
    score = snapshot.get("risk_score", risk_score(findings))
    scopes = {f.get("supporting_details", {}).get("scope_id", "") for f in findings}
    scopes.discard("")
    crit_scopes = {f.get("supporting_details", {}).get("scope_id", "")
                   for f in findings if f.get("severity") == SEV_CRITICAL}
    crit_scopes.discard("")
    return {
        "risk_score": score,
        "posture_score": snapshot.get("posture_score", round(100.0 - (score or 0.0), 1)),
        "rating": rating_for(score, has_critical=by_sev.get(SEV_CRITICAL, 0) > 0),
        "worst_severity": worst_severity(findings), "total_findings": len(findings),
        "by_severity": by_sev, "by_category": category_counts(findings),
        "by_rule": rule_counts(findings),
        "scopes_scanned": len(snapshot.get("scopes_scanned") or list(scopes)),
        "scopes_with_findings": len(scopes), "scopes_with_critical": len(crit_scopes),
    }


def global_posture(snapshot, provider) -> dict:
    findings = snapshot.get("findings") or []
    by_sev = severity_counts(findings)
    score = snapshot.get("risk_score", risk_score(findings))
    return {
        "overall_risk_score": score,
        "overall_posture_score": snapshot.get("posture_score", round(100.0 - (score or 0.0), 1)),
        "rating": rating_for(score, has_critical=by_sev.get(SEV_CRITICAL, 0) > 0),
        "total_findings": len(findings), "by_severity": by_sev, "by_domain": domain_counts(findings),
        "risk_by_domain": per_domain(findings, provider),
        "top_10_critical": top_critical(findings, provider, 10),
        "remediation_priority_queue": remediation_priority_queue(findings, provider, 50),
        "scanned_at": snapshot.get("scanned_at"), "scopes_scanned": snapshot.get("scopes_scanned", []),
    }
