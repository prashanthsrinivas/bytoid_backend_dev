"""Assemble the AI-assisted risk-analysis inputs for a VRA.

Pure functions that turn a snapshot (+ prior snapshots) into the structured,
fully-traceable context the report's OSINT section and the AI risk analysis
consume: an executive summary scaffold, key risk observations, a risk-rating
recommendation, evidence-based conclusions, and a trend. Every observation
carries its ``source_url`` so conclusions are traceable back to evidence — the
non-negotiable requirement for this module.

No LLM call happens here (that is the credit-gated runbook engine's job); this
prepares its grounded, citable input.
"""

from __future__ import annotations

from vra.evidence import snapshot_to_evidence
from vra.schema import (
    CATEGORY_LABELS,
    SEV_CRITICAL,
    SEV_HIGH,
    SEV_MEDIUM,
)

# 0-100 snapshot-score -> rating band (distinct from the runbook's authoritative
# Impact x Likelihood rating; this is the OSINT-only posture indicator).
_RATING_BANDS = ((75, "Critical"), (50, "High"), (20, "Medium"), (0, "Low"))


def risk_rating(snapshot: dict) -> str:
    score = snapshot.get("risk_score", 0.0) or 0.0
    # A single critical finding floors the rating at High regardless of mean.
    by_sev = (snapshot.get("counts") or {}).get("by_severity") or {}
    if by_sev.get(SEV_CRITICAL):
        return "Critical"
    for threshold, label in _RATING_BANDS:
        if score >= threshold:
            return label
    return "Low"


def key_observations(snapshot: dict, limit: int = 12) -> list[dict]:
    """High/critical findings, most-severe first, each with its source URL."""
    order = {SEV_CRITICAL: 0, SEV_HIGH: 1, SEV_MEDIUM: 2}
    sig = [
        f for f in (snapshot.get("findings") or [])
        if f.get("severity") in (SEV_CRITICAL, SEV_HIGH, SEV_MEDIUM)
    ]
    sig.sort(key=lambda f: order.get(f.get("severity"), 9))
    return [
        {
            "summary": f.get("finding_summary", ""),
            "severity": f.get("severity"),
            "category": f.get("category"),
            "category_label": CATEGORY_LABELS.get(f.get("category", ""), ""),
            "source_url": f.get("source_url", ""),
            "risk_indicators": f.get("risk_indicators", []),
        }
        for f in sig[:limit]
    ]


def trend(prior_scores: list[float] | None, current_score: float) -> dict:
    """Direction vs. the previous scan. prior_scores oldest->newest."""
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
    """Grounded, citable context for the AI risk analysis + report OSINT section."""
    counts = snapshot.get("counts") or {}
    evidence = snapshot_to_evidence(snapshot)
    observations = key_observations(snapshot)
    return {
        "vendor_name": snapshot.get("vendor_name", ""),
        "vendor_domain": snapshot.get("vendor_domain", ""),
        "scanned_at": snapshot.get("scanned_at", ""),
        "risk_rating": risk_rating(snapshot),
        "snapshot_risk_score": snapshot.get("risk_score", 0.0),
        "counts": counts,
        "key_observations": observations,
        "trend": trend(prior_scores, snapshot.get("risk_score", 0.0)),
        "evidence_summary": {
            "total_findings": evidence["total_findings"],
            "evidence_artifacts": evidence["evidence_artifacts"],
            "collected_window": evidence["collected_window"],
        },
        # Flat list of (claim -> citation) so every conclusion is traceable.
        "traceability": [
            {"finding": o["summary"], "source_url": o["source_url"]}
            for o in observations
            if o["source_url"]
        ],
    }


def render_context_markdown(context: dict) -> str:
    """Render the context as a grounded markdown brief for the report/LLM."""
    lines = [
        f"## OSINT Intelligence Assessment — {context.get('vendor_name', 'Vendor')}",
        "",
        f"- **Overall OSINT risk rating:** {context.get('risk_rating')} "
        f"(score {context.get('snapshot_risk_score')})",
        f"- **Risk trend:** {context.get('trend', {}).get('direction')}",
        f"- **Last scan:** {context.get('scanned_at')}",
        f"- **Findings:** {context.get('evidence_summary', {}).get('total_findings', 0)} "
        f"({context.get('evidence_summary', {}).get('evidence_artifacts', 0)} cited artifacts)",
        "",
        "### Key Risk Observations",
    ]
    obs = context.get("key_observations") or []
    if not obs:
        lines.append("- No significant (medium+) OSINT findings.")
    for o in obs:
        cite = f" [source]({o['source_url']})" if o.get("source_url") else ""
        lines.append(f"- **[{o.get('severity', '').upper()}]** {o.get('summary', '')}{cite}")
    lines += ["", "_All conclusions above are traceable to the cited collected evidence._"]
    return "\n".join(lines)
