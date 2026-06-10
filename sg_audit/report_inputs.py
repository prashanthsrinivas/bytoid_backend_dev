"""Assemble the grounded, fully-traceable analysis inputs for an SG audit.

Pure functions that turn a snapshot (+ prior snapshots) into the structured
context the report's "Security Group Posture" section and the AI recommender
consume: a posture rating, the key risk drivers, positive/negative signals, and
a trend. Every driver carries its account/region/group_id/rule_id so conclusions
are traceable back to a specific security group rule — the non-negotiable
faithfulness requirement.

No LLM call happens here; this prepares the recommender's grounded input.
"""

from __future__ import annotations

from sg_audit.schema import (
    CATEGORY_LABELS,
    RULE_LABELS,
    SEV_CRITICAL,
    SEV_HIGH,
    SEV_MEDIUM,
    rating_for,
)

_SEV_ORDER = {SEV_CRITICAL: 0, SEV_HIGH: 1, SEV_MEDIUM: 2}


def posture_rating(snapshot: dict) -> str:
    by_sev = (snapshot.get("counts") or {}).get("by_severity") or {}
    return rating_for(snapshot.get("risk_score", 0.0) or 0.0, has_critical=by_sev.get(SEV_CRITICAL, 0) > 0)


def _driver(f: dict) -> dict:
    d = f.get("supporting_details", {}) or {}
    return {
        "finding_id": f.get("finding_id", ""),
        "rule_id": f.get("rule_id", ""),
        "rule_label": RULE_LABELS.get(f.get("rule_id", ""), ""),
        "severity": f.get("severity"),
        "summary": f.get("finding_summary", ""),
        "category": f.get("category"),
        "category_label": CATEGORY_LABELS.get(f.get("category", ""), ""),
        "account_id": d.get("account_id", ""),
        "region": d.get("region", ""),
        "group_id": d.get("group_id", ""),
        "service": d.get("service", ""),
        "cidr": d.get("cidr", ""),
        "port": d.get("port"),
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
    """Materially good posture signals derived from the absence of risk."""
    by_rule = (snapshot.get("counts") or {}).get("by_rule") or {}
    sig: list[str] = []
    if not by_rule.get("SG_ADMIN_WORLD_INGRESS"):
        sig.append("No administrative ports (SSH/RDP) exposed to the internet")
    if not by_rule.get("SG_DB_WORLD_INGRESS") and not by_rule.get("SG_CACHE_WORLD_INGRESS"):
        sig.append("No databases or caches exposed to the internet")
    if not by_rule.get("SG_ALL_PORTS_WORLD"):
        sig.append("No security group opens all ports to the internet")
    if not by_rule.get("SG_DEFAULT_SG_HAS_RULES"):
        sig.append("Default security groups carry no CIDR-based rules")
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
            {
                "finding_id": d["finding_id"],
                "rule_id": d["rule_id"],
                "location": f"{d['account_id']}/{d['region']}/{d['group_id']}",
            }
            for d in drivers
        ],
    }


def render_context_markdown(context: dict) -> str:
    """Render the context as a grounded markdown brief for the report/LLM."""
    lines = [
        f"## AWS Security Group Posture — {context.get('posture_rating', 'Unknown')}",
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
        loc = f"{d['account_id']}/{d['region']}/{d['group_id']}"
        lines.append(
            f"- **[{(d.get('severity') or '').upper()}]** {d.get('summary', '')} "
            f"`({d.get('rule_id')} @ {loc})`"
        )

    pos = context.get("positive_signals") or []
    if pos:
        lines += ["", "### Positive Signals"]
        lines += [f"- {p}" for p in pos]

    lines += ["", "_All conclusions above are traceable to the cited security group rules._"]
    return "\n".join(lines)
