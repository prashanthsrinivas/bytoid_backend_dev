"""Posture scoring + rollups over a snapshot's findings.

Pure aggregation (stdlib only). The snapshot already carries the top-level
``risk_score``/``posture_score`` (computed by ``normalize.build_snapshot``);
this module derives the per-account / per-region / per-SG rollups the dashboard
renders, scoring each level with the SAME weighted formula so cards compare
directly.
"""

from __future__ import annotations

from sg_audit.analysis.normalize import (
    category_counts,
    risk_score,
    rule_counts,
    severity_counts,
)
from sg_audit.schema import SEV_CRITICAL, SEV_INFO, SEVERITY_ORDER, rating_for


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
