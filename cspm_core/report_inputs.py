"""Grounded analysis context + executive report (provider-aware, no LLM)."""

from __future__ import annotations

from cspm_core.schema import CATEGORY_LABELS, SEV_CRITICAL, SEV_HIGH, SEV_MEDIUM, rating_for

_SEV_ORDER = {SEV_CRITICAL: 0, SEV_HIGH: 1, SEV_MEDIUM: 2}


def posture_rating(snapshot) -> str:
    by_sev = (snapshot.get("counts") or {}).get("by_severity") or {}
    return rating_for(snapshot.get("risk_score", 0.0) or 0.0, has_critical=by_sev.get(SEV_CRITICAL, 0) > 0)


def _location(d):
    return f"{d.get('scope_id', '')}/{d.get('region') or 'global'}/{d.get('entity_id', '')}"


def key_risk_drivers(provider, snapshot, limit=20):
    sig = [f for f in (snapshot.get("findings") or []) if f.get("severity") in (SEV_CRITICAL, SEV_HIGH, SEV_MEDIUM)]
    sig.sort(key=lambda f: _SEV_ORDER.get(f.get("severity"), 9))
    out = []
    for f in sig[:limit]:
        d = f.get("supporting_details", {}) or {}
        out.append({
            "finding_id": f.get("finding_id", ""), "rule_id": f.get("rule_id", ""),
            "rule_label": provider.rule_label(f.get("rule_id", "")), "severity": f.get("severity"),
            "summary": f.get("finding_summary", ""), "domain": f.get("domain", ""),
            "category_label": CATEGORY_LABELS.get(f.get("category", ""), ""), "location": _location(d),
        })
    return out


def trend(prior_scores, current_score):
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


def build_analysis_context(provider, snapshot, prior_scores=None) -> dict:
    drivers = key_risk_drivers(provider, snapshot)
    return {
        "scanned_at": snapshot.get("scanned_at", ""), "posture_rating": posture_rating(snapshot),
        "risk_score": snapshot.get("risk_score", 0.0), "posture_score": snapshot.get("posture_score", 0.0),
        "counts": snapshot.get("counts") or {}, "scopes_scanned": snapshot.get("scopes_scanned", []),
        "collector_status": snapshot.get("collector_status", {}), "key_risk_drivers": drivers,
        "trend": trend(prior_scores, snapshot.get("risk_score", 0.0)),
        "traceability": [{"finding_id": d["finding_id"], "rule_id": d["rule_id"], "location": d["location"]} for d in drivers],
    }


def build_report(provider, snapshot, record=None, prior_scores=None) -> str:
    from cspm_core import score
    from cspm_core.compliance import all_frameworks

    record = record or {}
    gp = score.global_posture(snapshot, provider)
    by_sev = gp.get("by_severity", {})
    name = record.get("name") or f"{provider.label} Cloud Security Posture"
    lines = [
        f"# {provider.label} Security Posture Report — {name}", "",
        f"- **Overall posture:** {gp.get('rating')} ({round(gp.get('overall_posture_score', 0))}/100, risk {gp.get('overall_risk_score')})",
        f"- **Findings:** {gp.get('total_findings', 0)} (critical {by_sev.get('critical', 0)}, high {by_sev.get('high', 0)}, medium {by_sev.get('medium', 0)}, low {by_sev.get('low', 0)})",
        f"- **Trend:** {trend(prior_scores, snapshot.get('risk_score', 0.0)).get('direction')}",
        f"- **{provider.scope_label.title()}s scanned:** {len(gp.get('scopes_scanned') or [])}",
        f"- **Last audit:** {snapshot.get('scanned_at')}", "", "## Risk by Domain",
    ]
    for dom in gp.get("risk_by_domain", []):
        s = dom.get("by_severity", {})
        lines.append(f"- **{dom.get('label')}** — {dom.get('rating')} ({round(dom.get('posture_score', 0))}/100): "
                     f"C {s.get('critical', 0)} · H {s.get('high', 0)} · M {s.get('medium', 0)} · L {s.get('low', 0)}")
    if not gp.get("risk_by_domain"):
        lines.append("- No findings.")
    lines += ["", "## Compliance Coverage"]
    for cov in all_frameworks(snapshot, provider):
        lines.append(f"- **{cov.get('framework_label', cov.get('framework'))}**: {cov.get('coverage_pct')}% "
                     f"({cov.get('passing')}/{cov.get('evaluated')} evaluated; {cov.get('failing')} failing)")
    lines += ["", "## Top Risks"]
    top = gp.get("top_10_critical", [])
    if not top:
        lines.append("- No critical/high risks identified.")
    for t in top:
        lines.append(f"- **[{(t.get('severity') or '').upper()}]** {t.get('summary') or t.get('rule_label')} "
                     f"`({t.get('rule_id')} @ {t.get('scope_id')}/{t.get('region') or 'global'}/{t.get('entity_id')})`")
    lines += ["", "## Remediation Priority Queue (top 10)"]
    for it in (gp.get("remediation_priority_queue", []) or [])[:10]:
        lines.append(f"{it.get('rank')}. **[{(it.get('severity') or '').upper()}]** {it.get('rule_label')} — "
                     f"{it.get('entity_name') or it.get('entity_id')} ({it.get('scope_id')}) · effort {it.get('effort')} · _{it.get('remediation')}_")
    lines += ["", "_All findings are deterministic and trace to the cited resources; no conclusion is extrapolated._"]
    return "\n".join(lines)
