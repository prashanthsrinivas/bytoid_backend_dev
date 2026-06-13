"""Grounded "overall security posture" brief for the selected cloud providers.

Single source of truth consumed by (a) the workflow cloud auto-fill (so the AI
sees what is *correctly configured* — positive aspects — not just findings) and
(b) the Security Posture UI surfaces (CSPM dashboard, auto-fill dialog, the
assessment page).

Everything here is deterministic — positives come from actually-passing
compliance controls and real severity counts, negatives from real findings. No
LLM call and nothing extrapolated, so the brief stays faithful to the scan.
"""

from __future__ import annotations

from utils.base_logger import get_logger
from playbook.cloud_autofill import POSTURE_PROVIDERS, _storage_for

logger = get_logger(__name__)

PROVIDER_LABELS = {"aws": "AWS", "azure": "Azure", "gcp": "GCP"}


def _provider_obj(provider_key: str):
    """The cspm_core ``Provider`` for Azure/GCP; AWS (sg_audit) has none."""
    if provider_key == "azure":
        from azure_audit.provider import AZURE_PROVIDER

        return AZURE_PROVIDER
    if provider_key == "gcp":
        from gcp_audit.provider import GCP_PROVIDER

        return GCP_PROVIDER
    return None


def _analysis(provider_key: str, snapshot: dict):
    """Return ``(context, compliance, drivers, base_positives)`` for a provider,
    routing AWS → sg_audit and Azure/GCP → cspm_core (both no-LLM)."""
    if provider_key == "aws":
        from sg_audit import report_inputs as ri
        from sg_audit.compliance import all_frameworks

        ctx = ri.build_analysis_context(snapshot)
        return (
            ctx,
            all_frameworks(snapshot),
            ctx.get("key_risk_drivers", []),
            list(ctx.get("positive_signals") or []),
        )

    from cspm_core import report_inputs as ri
    from cspm_core.compliance import all_frameworks

    pobj = _provider_obj(provider_key)
    ctx = ri.build_analysis_context(pobj, snapshot)
    return ctx, all_frameworks(snapshot, pobj), ctx.get("key_risk_drivers", []), []


def _derive_positives(snapshot: dict, compliance: list, base_positives: list) -> list:
    """Grounded positive aspects: provider-specific signals (AWS) + absence of
    high-severity findings + passing compliance coverage (all providers)."""
    pos = list(base_positives or [])
    by_sev = (snapshot.get("counts") or {}).get("by_severity") or {}
    if not by_sev.get("critical"):
        pos.append("No critical-severity findings in the latest scan")
    if not by_sev.get("high"):
        pos.append("No high-severity findings in the latest scan")
    for cov in compliance or []:
        passing = cov.get("passing", 0)
        evaluated = cov.get("evaluated", 0)
        if evaluated and passing:
            label = cov.get("framework_label") or cov.get("framework")
            pos.append(
                f"{label}: {passing}/{evaluated} controls passing "
                f"({cov.get('coverage_pct')}%)"
            )
    return pos


def _driver_line(d: dict) -> str:
    sev = (d.get("severity") or "").upper()
    loc = d.get("location") or ""
    label = d.get("rule_label") or d.get("rule_id") or ""
    summary = d.get("summary") or label
    return f"[{sev}] {summary} ({label} @ {loc})" if loc else f"[{sev}] {summary} ({label})"


def _slim_compliance(compliance: list) -> list:
    return [
        {
            "framework": c.get("framework"),
            "framework_label": c.get("framework_label") or c.get("framework"),
            "coverage_pct": c.get("coverage_pct"),
            "passing": c.get("passing"),
            "evaluated": c.get("evaluated"),
            "failing": c.get("failing"),
        }
        for c in compliance or []
    ]


def _render_markdown(label, ctx, positives, negatives, compliance) -> str:
    lines = [
        f"## {label} Security Posture — {ctx.get('posture_rating', 'Unknown')}",
        "",
        f"- Posture score: {ctx.get('posture_score')} / 100 "
        f"(risk score {ctx.get('risk_score')})",
        f"- Last scan: {ctx.get('scanned_at')}",
        "",
        "### Positive Aspects (what is correctly configured)",
    ]
    if positives:
        lines += [f"- {p}" for p in positives]
    else:
        lines.append("- None identified")

    lines += ["", "### Key Risk Findings"]
    if negatives:
        lines += [f"- {n}" for n in negatives]
    else:
        lines.append("- No material (medium+) findings")

    lines += ["", "### Compliance Coverage"]
    if compliance:
        for c in compliance:
            lines.append(
                f"- {c.get('framework_label')}: {c.get('coverage_pct')}% "
                f"({c.get('passing')}/{c.get('evaluated')} passing; "
                f"{c.get('failing')} failing)"
            )
    else:
        lines.append("- No compliance mapping available")

    return "\n".join(lines)


def _resolve_audit_id(storage, user_id: str, audit_id):
    """Caller's audit_id, or the newest audit that actually has a snapshot."""
    if audit_id:
        return audit_id
    audits = storage.list_audits(user_id) or []
    return next(
        (
            a.get("audit_id")
            for a in audits
            if a.get("audit_id") and storage.list_snapshot_index(user_id, a["audit_id"])
        ),
        None,
    )


def build_posture_brief(user_id: str, selections) -> dict:
    """Build the grounded posture brief for ``selections``.

    ``selections`` is a list of ``{"provider": "aws|azure|gcp", "audit_id"?: str}``.
    Returns ``{"providers": [per-provider dict...], "brief_text": "<combined md>"}``.
    Each provider is isolated — one failure never blanks the others.
    """
    providers_out = []
    brief_parts = []
    for sel in selections or []:
        provider_key = (sel.get("provider") or "").lower()
        if provider_key not in POSTURE_PROVIDERS:
            continue
        label = PROVIDER_LABELS.get(provider_key, provider_key.upper())
        try:
            storage = _storage_for(provider_key)
            audit_id = _resolve_audit_id(storage, user_id, sel.get("audit_id"))
            snapshot = storage.get_latest_snapshot(user_id, audit_id) if audit_id else None
            if not snapshot:
                providers_out.append(
                    {"provider": provider_key, "label": label, "available": False,
                     "error": "no posture snapshot"}
                )
                continue

            ctx, compliance, drivers, base_pos = _analysis(provider_key, snapshot)
            positives = _derive_positives(snapshot, compliance, base_pos)
            negatives = [_driver_line(d) for d in drivers]
            slim_compliance = _slim_compliance(compliance)
            markdown = _render_markdown(label, ctx, positives, negatives, slim_compliance)

            providers_out.append(
                {
                    "provider": provider_key,
                    "label": label,
                    "available": True,
                    "audit_id": audit_id,
                    "scanned_at": ctx.get("scanned_at"),
                    "rating": ctx.get("posture_rating"),
                    "posture_score": ctx.get("posture_score"),
                    "risk_score": ctx.get("risk_score"),
                    "positives": positives,
                    "negatives": negatives,
                    "compliance": slim_compliance,
                    "markdown": markdown,
                }
            )
            brief_parts.append(markdown)
        except Exception as e:
            logger.warning("posture brief failed for %s: %s", provider_key, e)
            providers_out.append(
                {"provider": provider_key, "label": label, "available": False,
                 "error": str(e)}
            )

    return {"providers": providers_out, "brief_text": "\n\n".join(brief_parts)}
