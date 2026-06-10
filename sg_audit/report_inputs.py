"""Grounded, fully-traceable analysis inputs + executive report for an audit.

Pure functions that turn a snapshot (+ prior snapshots) into the structured
context the AI recommender consumes and into a multi-domain executive report.
Every driver carries its account/region/entity/rule_id so conclusions are
traceable back to a specific resource — the non-negotiable faithfulness
requirement. No LLM call happens here.
"""

from __future__ import annotations

from sg_audit import metadata
from sg_audit.schema import (
    CATEGORY_LABELS,
    SEV_CRITICAL,
    SEV_HIGH,
    SEV_MEDIUM,
    rating_for,
)

_SEV_ORDER = {SEV_CRITICAL: 0, SEV_HIGH: 1, SEV_MEDIUM: 2}


def posture_rating(snapshot: dict) -> str:
    by_sev = (snapshot.get("counts") or {}).get("by_severity") or {}
    return rating_for(snapshot.get("risk_score", 0.0) or 0.0, has_critical=by_sev.get(SEV_CRITICAL, 0) > 0)


def _entity_id(d: dict) -> str:
    return d.get("entity_id") or d.get("group_id") or ""


def _location(d: dict) -> str:
    return f"{d.get('account_id', '')}/{d.get('region') or 'global'}/{_entity_id(d)}"


def _driver(f: dict) -> dict:
    d = f.get("supporting_details", {}) or {}
    rid = f.get("rule_id", "")
    return {
        "finding_id": f.get("finding_id", ""),
        "rule_id": rid,
        "rule_label": metadata.rule_label(rid),
        "severity": f.get("severity"),
        "summary": f.get("finding_summary", ""),
        "domain": f.get("domain", ""),
        "category": f.get("category"),
        "category_label": CATEGORY_LABELS.get(f.get("category", ""), ""),
        "account_id": d.get("account_id", ""),
        "region": d.get("region", ""),
        "entity_type": d.get("entity_type", ""),
        "entity_id": _entity_id(d),
        "location": _location(d),
    }


def key_risk_drivers(snapshot: dict, limit: int = 20) -> list[dict]:
    """Top findings driving the score, most severe first, each fully cited."""
    sig = [f for f in (snapshot.get("findings") or [])
           if f.get("severity") in (SEV_CRITICAL, SEV_HIGH, SEV_MEDIUM)]
    sig.sort(key=lambda f: _SEV_ORDER.get(f.get("severity"), 9))
    return [_driver(f) for f in sig[:limit]]


def negative_signals(snapshot: dict, limit: int = 10) -> list[dict]:
    return key_risk_drivers(snapshot, limit)


def positive_signals(snapshot: dict) -> list[str]:
    """Materially good posture signals derived from the absence of key risks."""
    by_rule = (snapshot.get("counts") or {}).get("by_rule") or {}
    sig: list[str] = []
    if not by_rule.get("SG_ADMIN_WORLD_INGRESS") and not by_rule.get("EC2_OPEN_MGMT_PORT"):
        sig.append("No administrative ports (SSH/RDP) exposed to the internet")
    if not by_rule.get("SG_DB_WORLD_INGRESS") and not by_rule.get("RDS_PUBLIC"):
        sig.append("No databases exposed to the internet")
    if not by_rule.get("S3_PUBLIC_ACL") and not by_rule.get("S3_PUBLIC_POLICY"):
        sig.append("No publicly-readable S3 buckets detected")
    if not by_rule.get("IAM_ROOT_NO_MFA") and not by_rule.get("IAM_ROOT_HAS_KEYS"):
        sig.append("Root account has MFA and no active access keys")
    if not by_rule.get("LOG_NO_CLOUDTRAIL"):
        sig.append("CloudTrail logging is enabled")
    return sig


def category_summary(snapshot: dict) -> list[dict]:
    by_cat = (snapshot.get("counts") or {}).get("by_category") or {}
    return [
        {"category": c, "label": CATEGORY_LABELS.get(c, c), "count": n}
        for c, n in by_cat.items()
    ]


def trend(prior_scores: list[float] | None, current_score: float) -> dict:
    """Direction vs. the previous scan (prior_scores oldest->newest, risk_score)."""
    prev = prior_scores[-1] if prior_scores else None
    if prev is None:
        direction = "baseline"
    elif current_score > prev + 1:
        direction = "worsening"
    elif current_score < prev - 1:
        direction = "improving"
    else:
        direction = "stable"
    return {"direction": direction, "previous_score": prev, "current_score": current_score}


def build_analysis_context(snapshot: dict, prior_scores: list[float] | None = None) -> dict:
    """Grounded, citable context for the AI recommender + report section."""
    counts = snapshot.get("counts") or {}
    rating = posture_rating(snapshot)
    drivers = key_risk_drivers(snapshot)
    return {
        "audit_id": snapshot.get("audit_id", ""),
        "scanned_at": snapshot.get("scanned_at", ""),
        "posture_rating": rating,
        "risk_score": snapshot.get("risk_score", 0.0),
        "posture_score": snapshot.get("posture_score", 0.0),
        "counts": counts,
        "accounts_scanned": snapshot.get("accounts_scanned", []),
        "collector_status": snapshot.get("collector_status", {}),
        "category_summary": category_summary(snapshot),
        "key_risk_drivers": drivers,
        "positive_signals": positive_signals(snapshot),
        "negative_signals": negative_signals(snapshot),
        "trend": trend(prior_scores, snapshot.get("risk_score", 0.0)),
        # Flat (finding -> location) list so every conclusion is traceable.
        "traceability": [
            {"finding_id": d["finding_id"], "rule_id": d["rule_id"], "location": d["location"]}
            for d in drivers
        ],
    }


def render_context_markdown(context: dict) -> str:
    """Render the analysis context as a grounded markdown brief."""
    lines = [
        f"## AWS Cloud Security Posture — {context.get('posture_rating', 'Unknown')}",
        "",
        f"- **Posture score:** {context.get('posture_score')} / 100 "
        f"(risk score {context.get('risk_score')})",
        f"- **Trend:** {context.get('trend', {}).get('direction')}",
        f"- **Last audit:** {context.get('scanned_at')}",
        f"- **Accounts scanned:** {len(context.get('accounts_scanned') or [])}",
        "",
        "### Key Risk Drivers",
    ]
    drivers = context.get("key_risk_drivers") or []
    if not drivers:
        lines.append("- No material (medium+) risk drivers identified.")
    for d in drivers:
        lines.append(
            f"- **[{(d.get('severity') or '').upper()}]** {d.get('summary', '')} "
            f"`({d.get('rule_id')} @ {d.get('location')})`"
        )

    pos = context.get("positive_signals") or []
    if pos:
        lines += ["", "### Positive Signals"]
        lines += [f"- {p}" for p in pos]

    lines += ["", "_All conclusions above are traceable to the cited resources._"]
    return "\n".join(lines)


def build_report(snapshot: dict, record: dict | None = None, prior_scores: list[float] | None = None) -> str:
    """Grounded multi-domain executive report (markdown) for the latest scan."""
    from sg_audit.analysis import score
    from sg_audit.compliance import all_frameworks

    record = record or {}
    gp = score.global_posture(snapshot)
    by_sev = gp.get("by_severity", {})
    name = record.get("name") or "Cloud Security Posture"
    lines = [
        f"# Cloud Security Posture Report — {name}",
        "",
        f"- **Overall posture:** {gp.get('rating')} ({round(gp.get('overall_posture_score', 0))}/100, "
        f"risk {gp.get('overall_risk_score')})",
        f"- **Findings:** {gp.get('total_findings', 0)} "
        f"(critical {by_sev.get('critical', 0)}, high {by_sev.get('high', 0)}, "
        f"medium {by_sev.get('medium', 0)}, low {by_sev.get('low', 0)})",
        f"- **Trend:** {trend(prior_scores, snapshot.get('risk_score', 0.0)).get('direction')}",
        f"- **Accounts scanned:** {len(gp.get('accounts_scanned') or [])}",
        f"- **Last audit:** {snapshot.get('scanned_at')}",
        "",
        "## Risk by Domain",
    ]
    for dom in gp.get("risk_by_domain", []):
        s = dom.get("by_severity", {})
        lines.append(
            f"- **{dom.get('label')}** — {dom.get('rating')} ({round(dom.get('posture_score', 0))}/100): "
            f"C {s.get('critical', 0)} · H {s.get('high', 0)} · M {s.get('medium', 0)} · L {s.get('low', 0)}"
        )
    if not gp.get("risk_by_domain"):
        lines.append("- No findings.")

    lines += ["", "## Compliance Coverage"]
    for cov in all_frameworks(snapshot):
        lines.append(
            f"- **{cov.get('framework_label', cov.get('framework'))}**: {cov.get('coverage_pct')}% "
            f"({cov.get('passing')}/{cov.get('evaluated')} evaluated controls; {cov.get('failing')} failing)"
        )

    lines += ["", "## Top Risks"]
    top = gp.get("top_10_critical", [])
    if not top:
        lines.append("- No critical/high risks identified.")
    for t in top:
        lines.append(
            f"- **[{(t.get('severity') or '').upper()}]** {t.get('summary') or t.get('rule_label')} "
            f"`({t.get('rule_id')} @ {t.get('account_id')}/{t.get('region') or 'global'}/{t.get('entity_id')})`"
        )

    lines += ["", "## Remediation Priority Queue (top 10)"]
    for it in (gp.get("remediation_priority_queue", []) or [])[:10]:
        lines.append(
            f"{it.get('rank')}. **[{(it.get('severity') or '').upper()}]** {it.get('rule_label')} — "
            f"{it.get('entity_name') or it.get('entity_id')} "
            f"({it.get('account_id')}/{it.get('region') or 'global'}) · effort {it.get('effort')} · "
            f"_{it.get('remediation')}_"
        )

    lines += ["", "_All findings are deterministic and trace to the cited resources; "
              "no conclusion is extrapolated beyond the collected evidence._"]
    return "\n".join(lines)
