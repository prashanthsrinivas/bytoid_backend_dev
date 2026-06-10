"""Posture scoring + rollups over a snapshot's findings.

Pure aggregation (stdlib only). The snapshot already carries the top-level
``risk_score``/``posture_score`` (computed by ``normalize.build_snapshot``);
this module derives the per-account / per-region / per-SG rollups the dashboard
renders, scoring each level with the SAME weighted formula so cards compare
directly.
"""

from __future__ import annotations

from sg_audit import metadata
from sg_audit.analysis.normalize import (
    category_counts,
    domain_counts,
    risk_score,
    rule_counts,
    severity_counts,
)
from sg_audit.schema import (
    DOMAIN_LABELS,
    DOMAIN_SECURITY_GROUPS,
    SEV_CRITICAL,
    SEV_HIGH,
    SEV_INFO,
    SEVERITY_ORDER,
    SEVERITY_WEIGHTS,
    rating_for,
)


def worst_severity(findings: list[dict]) -> str:
    worst = SEV_INFO
    wi = 0
    for f in findings:
        sev = f.get("severity", SEV_INFO)
        i = SEVERITY_ORDER.index(sev) if sev in SEVERITY_ORDER else 0
        if i > wi:
            wi, worst = i, sev
    return worst


def _scored_block(findings: list[dict]) -> dict:
    by_sev = severity_counts(findings)
    score = risk_score(findings)
    return {
        "risk_score": score,
        "posture_score": round(100.0 - score, 1),
        "rating": rating_for(score, has_critical=by_sev.get(SEV_CRITICAL, 0) > 0),
        "worst_severity": worst_severity(findings),
        "total": len(findings),
        "by_severity": by_sev,
    }


def _group_by(findings: list[dict], *keys: str) -> dict:
    grouped: dict[tuple, list] = {}
    for f in findings:
        d = f.get("supporting_details", {}) or {}
        k = tuple(d.get(key, "") for key in keys)
        grouped.setdefault(k, []).append(f)
    return grouped


def per_account(findings: list[dict], collector_status: dict | None = None) -> list[dict]:
    """One scored row per account, newest-worst first, with collector status."""
    status = collector_status or {}
    rows = []
    for (account_id, account_name), items in _group_by(findings, "account_id", "account_name").items():
        # Aggregate any per-region status lines that belong to this account.
        acct_status = {
            k: v for k, v in status.items()
            if k == account_id or str(k).startswith(f"{account_id}:")
        }
        rows.append({
            "account_id": account_id,
            "account_name": account_name,
            "collector_status": acct_status,
            **_scored_block(items),
        })
    rows.sort(key=lambda r: r["risk_score"], reverse=True)
    return rows


def per_region(findings: list[dict]) -> list[dict]:
    rows = []
    for (account_id, region), items in _group_by(findings, "account_id", "region").items():
        rows.append({
            "account_id": account_id,
            "region": region,
            **_scored_block(items),
        })
    rows.sort(key=lambda r: r["risk_score"], reverse=True)
    return rows


def per_security_group(findings: list[dict]) -> list[dict]:
    """One row per SG (the unit of remediation), with its findings attached."""
    rows = []
    grouped = _group_by(findings, "account_id", "region", "group_id", "group_name", "vpc_id")
    for (account_id, region, group_id, group_name, vpc_id), items in grouped.items():
        in_use = next((i["supporting_details"].get("in_use") for i in items
                       if i["supporting_details"].get("in_use") is not None), None)
        rows.append({
            "account_id": account_id,
            "region": region,
            "group_id": group_id,
            "group_name": group_name,
            "vpc_id": vpc_id,
            "in_use": in_use,
            "findings": items,
            **_scored_block(items),
        })
    rows.sort(key=lambda r: r["risk_score"], reverse=True)
    return rows


def executive_rollup(snapshot: dict) -> dict:
    """Top-line posture numbers for the dashboard's executive cards."""
    findings = snapshot.get("findings") or []
    by_sev = severity_counts(findings)
    score = snapshot.get("risk_score", risk_score(findings))
    accounts = {f.get("supporting_details", {}).get("account_id", "") for f in findings}
    accounts.discard("")
    accounts_with_critical = {
        f.get("supporting_details", {}).get("account_id", "")
        for f in findings if f.get("severity") == SEV_CRITICAL
    }
    accounts_with_critical.discard("")
    return {
        "risk_score": score,
        "posture_score": snapshot.get("posture_score", round(100.0 - (score or 0.0), 1)),
        "rating": rating_for(score, has_critical=by_sev.get(SEV_CRITICAL, 0) > 0),
        "worst_severity": worst_severity(findings),
        "total_findings": len(findings),
        "by_severity": by_sev,
        "by_category": category_counts(findings),
        "by_rule": rule_counts(findings),
        "accounts_scanned": len(snapshot.get("accounts_scanned") or list(accounts)),
        "accounts_with_findings": len(accounts),
        "accounts_with_critical": len(accounts_with_critical),
    }


# ── Multi-domain: per-domain / per-entity rollups ───────────────────────────

def per_domain(findings: list[dict]) -> list[dict]:
    """One scored row per domain (for the global risk-by-domain breakdown)."""
    grouped: dict[str, list] = {}
    for f in findings:
        grouped.setdefault(f.get("domain") or DOMAIN_SECURITY_GROUPS, []).append(f)
    rows = [
        {"domain": dom, "label": DOMAIN_LABELS.get(dom, dom), **_scored_block(items)}
        for dom, items in grouped.items()
    ]
    rows.sort(key=lambda r: r["risk_score"], reverse=True)
    return rows


def per_entity(findings: list[dict]) -> list[dict]:
    """One scored row per entity (domain-agnostic drill-down unit).

    Groups by (domain, entity_type, entity_id) and attaches the entity's
    findings — the generic equivalent of ``per_security_group`` for other domains.
    """
    grouped = _group_by(findings, "entity_type", "entity_id", "entity_name", "account_id", "region")
    rows = []
    for (entity_type, entity_id, entity_name, account_id, region), items in grouped.items():
        rows.append({
            "domain": items[0].get("domain") if items else "",
            "entity_type": entity_type,
            "entity_id": entity_id,
            "entity_name": entity_name or entity_id,
            "account_id": account_id,
            "region": region,
            "findings": items,
            **_scored_block(items),
        })
    rows.sort(key=lambda r: r["risk_score"], reverse=True)
    return rows


# ── Remediation priority queue ──────────────────────────────────────────────

_EASE_BONUS = {"low": 100, "medium": 60, "high": 30}  # lower effort = quicker win


def priority_score(finding: dict) -> float:
    """0-100 remediation priority from severity x exploitability x blast x ease.

    Deterministic and reproducible (no LLM): blends the finding's authoritative
    severity weight (50%), the exploitability+blast_radius risk from RULE_META
    (30%), and ease-of-fix (20%) so quick high-impact wins rank first.
    """
    rid = finding.get("rule_id", "")
    sev_w = SEVERITY_WEIGHTS.get(finding.get("severity", SEV_INFO), 0)
    expl, blast = metadata.priority_inputs(rid)
    risk = (expl + blast) / 6.0 * 100.0  # 0-100
    ease = _EASE_BONUS.get(metadata.effort_for(rid), 60)
    return round(0.5 * sev_w + 0.3 * risk + 0.2 * ease, 1)


def _queue_item(finding: dict, rank: int) -> dict:
    rid = finding.get("rule_id", "")
    d = finding.get("supporting_details", {}) or {}
    return {
        "rank": rank,
        "priority": priority_score(finding),
        "finding_id": finding.get("finding_id", ""),
        "domain": finding.get("domain", ""),
        "rule_id": rid,
        "rule_label": metadata.rule_label(rid),
        "severity": finding.get("severity"),
        "effort": metadata.effort_for(rid),
        "entity_type": d.get("entity_type", ""),
        "entity_id": d.get("entity_id", ""),
        "entity_name": d.get("entity_name", d.get("entity_id", "")),
        "account_id": d.get("account_id", ""),
        "region": d.get("region", ""),
        "summary": finding.get("finding_summary", ""),
        "remediation": metadata.remediation_for(rid),
        "cis": metadata.cis_for(rid),
    }


def remediation_priority_queue(findings: list[dict], limit: int = 50) -> list[dict]:
    """Findings ranked by exploitability x blast radius x ease of fix."""
    ranked = sorted(findings, key=priority_score, reverse=True)
    return [_queue_item(f, i + 1) for i, f in enumerate(ranked[:limit])]


def top_critical(findings: list[dict], limit: int = 10) -> list[dict]:
    """Top critical/high findings across all domains, by priority score."""
    sig = [f for f in findings if f.get("severity") in (SEV_CRITICAL, SEV_HIGH)]
    sig.sort(key=priority_score, reverse=True)
    return [_queue_item(f, i + 1) for i, f in enumerate(sig[:limit])]


# ── Global posture (cross-domain) ───────────────────────────────────────────

def global_posture(snapshot: dict) -> dict:
    """Overall account posture: global score + risk-by-domain + top-10 + queue."""
    findings = snapshot.get("findings") or []
    by_sev = severity_counts(findings)
    score = snapshot.get("risk_score", risk_score(findings))
    return {
        "overall_risk_score": score,
        "overall_posture_score": snapshot.get("posture_score", round(100.0 - (score or 0.0), 1)),
        "rating": rating_for(score, has_critical=by_sev.get(SEV_CRITICAL, 0) > 0),
        "total_findings": len(findings),
        "by_severity": by_sev,
        "by_domain": domain_counts(findings),
        "risk_by_domain": per_domain(findings),
        "top_10_critical": top_critical(findings, 10),
        "remediation_priority_queue": remediation_priority_queue(findings, 50),
        "scanned_at": snapshot.get("scanned_at"),
        "accounts_scanned": snapshot.get("accounts_scanned", []),
    }
