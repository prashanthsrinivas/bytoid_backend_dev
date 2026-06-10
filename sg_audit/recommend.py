"""On-demand AI "how to tighten" recommendations — grounded + faithfulness-bounded.

The deterministic rule engine is the source of truth. This module asks Bedrock
ONLY to phrase remediation for findings the engine already produced; it can never
invent a security group, rule, or finding. Faithfulness is enforced two ways:

  1. The prompt is built solely from the persisted findings (a closed
     ``finding_id`` set) with an explicit instruction to map every recommendation
     to one of those ids.
  2. Post-validation drops any recommendation whose ``finding_id`` is not in the
     input set — the prompt asks, the validator guarantees.

Severity, effort, impact, and rank are owned by code (deterministic, reproducible);
the LLM only supplies the human-readable ``recommended_action`` text. The single
Bedrock entry point ``get_fireworks_response2`` already enforces input/output
guardrails + the AI-credit gate + usage metering.
"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timezone

from utils.base_logger import get_logger
from sg_audit import config as sg_config
from sg_audit.schema import (
    EFFORT_ORDER,
    RULE_LABELS,
    SEVERITY_WEIGHTS,
    SEV_INFO,
)

logger = get_logger(__name__)

# Deterministic remediation template per rule (faithful best practice). Used as a
# fallback when the LLM is unavailable/omits a finding, and as grounding context.
REMEDIATION_HINTS = {
    "SG_ADMIN_WORLD_INGRESS": "Remove the 0.0.0.0/0 (and ::/0) rule; restrict the admin port to a bastion host, VPN, or corporate CIDR — ideally use SSM Session Manager instead of public SSH/RDP.",
    "SG_DB_WORLD_INGRESS": "Remove internet exposure of the database port; allow access only from the application tier's security group or a private CIDR.",
    "SG_CACHE_WORLD_INGRESS": "Remove internet exposure of the cache port; restrict to the application security group within the VPC. Redis/Memcached are frequently unauthenticated.",
    "SG_ALL_PORTS_WORLD": "Replace the all-ports/all-protocols rule with explicit least-privilege rules for only the ports the workload needs.",
    "SG_WIDE_RANGE_WORLD": "Narrow the port range to the specific ports required and scope the source away from 0.0.0.0/0.",
    "SG_SENSITIVE_NON_ADMIN_WORLD": "Restrict the sensitive service port to known internal sources; put the service behind a load balancer or VPN rather than exposing it directly.",
    "SG_NON_WORLD_ADMIN_OPEN_WIDE": "Tighten the source CIDR for the admin port to specific known addresses (a /32 or small range) instead of a wide public block.",
    "SG_DEFAULT_SG_HAS_RULES": "Remove all CIDR-based rules from the default security group (CIS 1.4 / 5.4); the default SG should deny all and never be used directly.",
    "SG_DEFAULT_SG_IN_USE": "Move resources off the default security group onto purpose-built least-privilege groups, then strip the default SG's rules.",
    "SG_BROAD_INTERNAL_CIDR": "Narrow the internal source range to the specific subnet/security group that needs access instead of a broad /8-/16 block.",
    "SG_BROAD_EGRESS_ALL": "Replace the allow-all egress rule with explicit destinations/ports the workload requires to limit data-exfiltration paths on compromise.",
    "SG_UNUSED": "Delete the unused security group to reduce sprawl and the chance of it being attached with stale permissive rules.",
    "SG_ICMP_WORLD": "Restrict ICMP to internal ranges or remove it; allow only the specific ICMP types needed (e.g. path MTU discovery).",
    "SG_MISSING_RULE_DESCRIPTION": "Add a description to each rule documenting its purpose and owner to support future audits and safe cleanup.",
}

# Effort to remediate, per rule (drives deterministic ranking; lower surfaces first).
_EFFORT_BY_RULE = {
    "SG_ADMIN_WORLD_INGRESS": "low",
    "SG_DB_WORLD_INGRESS": "low",
    "SG_CACHE_WORLD_INGRESS": "low",
    "SG_ALL_PORTS_WORLD": "medium",
    "SG_WIDE_RANGE_WORLD": "medium",
    "SG_SENSITIVE_NON_ADMIN_WORLD": "low",
    "SG_NON_WORLD_ADMIN_OPEN_WIDE": "low",
    "SG_DEFAULT_SG_HAS_RULES": "low",
    "SG_DEFAULT_SG_IN_USE": "high",
    "SG_BROAD_INTERNAL_CIDR": "medium",
    "SG_BROAD_EGRESS_ALL": "medium",
    "SG_UNUSED": "low",
    "SG_ICMP_WORLD": "low",
    "SG_MISSING_RULE_DESCRIPTION": "low",
}


def _impact_from_severity(severity: str) -> str:
    w = SEVERITY_WEIGHTS.get(severity, 0)
    if w >= 65:
        return "high"
    if w >= 30:
        return "medium"
    return "low"


def _material_findings(snapshot: dict) -> list[dict]:
    """Findings worth recommending on, worst-first (skip pure info)."""
    findings = [f for f in (snapshot.get("findings") or []) if f.get("severity") != SEV_INFO]
    findings.sort(key=lambda f: -SEVERITY_WEIGHTS.get(f.get("severity"), 0))
    return findings


def _compact(f: dict) -> dict:
    d = f.get("supporting_details", {}) or {}
    return {
        "finding_id": f.get("finding_id", ""),
        "rule_id": f.get("rule_id", ""),
        "rule_label": RULE_LABELS.get(f.get("rule_id", ""), ""),
        "severity": f.get("severity", ""),
        "account_id": d.get("account_id", ""),
        "region": d.get("region", ""),
        "group_id": d.get("group_id", ""),
        "summary": f.get("finding_summary", ""),
    }


def _findings_within_budget(findings: list[dict], budget_chars: int) -> list[dict]:
    """Serialize compact findings, trimming the tail to fit the char budget."""
    compact = [_compact(f) for f in findings]
    while compact and len(json.dumps(compact)) > budget_chars:
        compact.pop()  # findings are worst-first, so we drop least-severe last
    return compact


def _build_prompt(compact: list[dict]) -> str:
    return (
        "You are a senior AWS security engineer. Below is a JSON array of CONFIRMED "
        "security-group findings from a deterministic audit. Produce concrete remediation "
        "guidance to tighten security.\n\n"
        "STRICT RULES:\n"
        "1. Recommend ONLY for the finding_id values in the input. Do NOT invent security "
        "groups, rules, ports, accounts, or findings.\n"
        "2. Every item in `recommendations` MUST reference exactly one finding_id from the input.\n"
        "3. If you cannot map a recommendation to a provided finding_id, omit it.\n"
        "4. Keep each recommended_action specific and actionable (what rule to remove/narrow, "
        "and to what scope).\n\n"
        "Return ONLY valid JSON of this exact shape:\n"
        "{\n"
        '  "executive_tightening_summary": "<2-4 sentences grounded in the findings>",\n'
        '  "recommendations": [{"finding_id": "<id>", "recommended_action": "<text>"}],\n'
        '  "ranking_rationale": "<1-2 sentences on how to prioritize>"\n'
        "}\n\n"
        f"FINDINGS:\n{json.dumps(compact)}"
    )


def _merge_and_rank(material: list[dict], llm_actions: dict[str, str]) -> list[dict]:
    """Build the final per_finding list: action from LLM (or deterministic), code-ranked."""
    items = []
    for f in material:
        rid = f.get("rule_id", "")
        fid = f.get("finding_id", "")
        d = f.get("supporting_details", {}) or {}
        action = (llm_actions.get(fid) or "").strip() or REMEDIATION_HINTS.get(rid, "")
        items.append({
            "finding_id": fid,
            "rule_id": rid,
            "rule_label": RULE_LABELS.get(rid, ""),
            "severity": f.get("severity", ""),
            "account_id": d.get("account_id", ""),
            "region": d.get("region", ""),
            "group_id": d.get("group_id", ""),
            "recommended_action": action,
            "effort": _EFFORT_BY_RULE.get(rid, "medium"),
            "impact": _impact_from_severity(f.get("severity", "")),
        })
    # Deterministic ranking: highest severity first, then lowest effort.
    items.sort(key=lambda x: (
        -SEVERITY_WEIGHTS.get(x["severity"], 0),
        EFFORT_ORDER.index(x["effort"]) if x["effort"] in EFFORT_ORDER else 9,
    ))
    for i, item in enumerate(items, start=1):
        item["rank"] = i
    return items


async def generate_recommendations(user_id: str, snapshot: dict) -> dict:
    """Produce grounded AI tightening recommendations for a posture snapshot.

    Never raises. Returns a status dict; on insufficient credits / guardrail block
    the deterministic findings still stand (the caller can show them without AI).
    """
    material = _material_findings(snapshot)
    if not material:
        return {
            "status": "success",
            "ai_used": False,
            "executive_tightening_summary": "No material (medium or higher) findings to remediate.",
            "per_finding": [],
            "ranking_rationale": "",
        }

    compact = _findings_within_budget(material, sg_config.SG_LLM_BUDGET_CHARS)
    sent_ids = {c["finding_id"] for c in compact}
    prompt = _build_prompt(compact)

    llm_actions: dict[str, str] = {}
    ai_used = False
    note = None
    try:
        from credits_route.route import Credits
        from db.rds_db import connect_to_rds
        from utils.fireworkzz import extract_json_safe, get_fireworks_response2

        db = connect_to_rds()
        try:
            credits = Credits(db)
            text = await get_fireworks_response2(
                user_id=user_id, user_message=prompt, role="user", credits=credits, temp=0.1
            )
        finally:
            try:
                db.close()
            except Exception:
                logger.debug("recommend db.close failed", exc_info=True)

        if text == "INSUFFICIENT":
            return {"status": "insufficient_credits",
                    "message": "Not enough AI credits to generate recommendations."}
        if isinstance(text, str) and text.startswith("BLOCKED_BY_GUARDRAIL"):
            return {"status": "blocked", "message": text}

        parsed = extract_json_safe(text) if text else None
        if isinstance(parsed, dict):
            ai_used = True
            for rec in parsed.get("recommendations") or []:
                fid = (rec or {}).get("finding_id", "")
                # Faithfulness: only accept recommendations for findings we sent.
                if fid in sent_ids:
                    llm_actions[fid] = (rec.get("recommended_action") or "").strip()
            summary = (parsed.get("executive_tightening_summary") or "").strip()
            rationale = (parsed.get("ranking_rationale") or "").strip()
        else:
            note = "AI response could not be parsed; using deterministic remediation."
            summary, rationale = "", ""
    except Exception:
        logger.warning("SG-audit AI recommendation failed; using deterministic fallback", exc_info=True)
        note = "AI unavailable; using deterministic remediation."
        summary, rationale = "", ""

    dropped = 0  # recommendations referencing unknown finding_ids were ignored above
    per_finding = _merge_and_rank(material, llm_actions)

    if not summary:
        crit = (snapshot.get("counts") or {}).get("by_severity", {}).get("critical", 0)
        high = (snapshot.get("counts") or {}).get("by_severity", {}).get("high", 0)
        summary = (
            f"{len(per_finding)} security-group findings require tightening "
            f"({crit} critical, {high} high). Prioritize removing internet exposure of "
            "administrative and data ports."
        )
    if not rationale:
        rationale = "Ranked by severity, then by remediation effort (quick wins first)."

    return {
        "status": "success",
        "ai_used": ai_used,
        "note": note,
        "executive_tightening_summary": summary,
        "per_finding": per_finding,
        "ranking_rationale": rationale,
        "findings_considered": len(material),
        "findings_sent_to_ai": len(compact),
        "recommendations_dropped": dropped,
    }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def generate_and_store(user_id: str, audit_id: str, scan_id: str, snapshot: dict, service=None) -> dict:
    """Generate recommendations and persist them to S3 keyed by scan_id."""
    from sg_audit.service import SgAuditService

    service = service or SgAuditService()
    result = await generate_recommendations(user_id, snapshot)
    result["scan_id"] = scan_id
    result["generated_at"] = _utc_now_iso()
    service.storage.save_recommendation(user_id, audit_id, scan_id, result)
    return result


def launch_generation(user_id: str, audit_id: str, scan_id: str, snapshot: dict) -> None:
    """Run recommendation generation in a background thread (Bedrock can exceed
    the API-gateway/worker request timeout), persisting the result to S3 for the
    frontend to poll. Releases the in-flight lock when done.
    """
    from sg_audit.helpers import release_rec_inflight

    def _target():
        try:
            asyncio.run(generate_and_store(user_id, audit_id, scan_id, snapshot))
        except Exception:
            logger.warning("SG-audit recommendation generation failed for %s", scan_id, exc_info=True)
            try:
                from sg_audit.service import SgAuditService

                SgAuditService().storage.save_recommendation(
                    user_id, audit_id, scan_id,
                    {"status": "error", "scan_id": scan_id,
                     "message": "Recommendation generation failed.", "generated_at": _utc_now_iso()},
                )
            except Exception:
                logger.debug("failed to persist recommendation error marker", exc_info=True)
        finally:
            try:
                asyncio.run(release_rec_inflight(f"{audit_id}:{scan_id}"))
            except Exception:
                logger.debug("release_rec_inflight failed", exc_info=True)

    threading.Thread(target=_target, name=f"sg-rec-{scan_id[:8]}", daemon=True).start()
