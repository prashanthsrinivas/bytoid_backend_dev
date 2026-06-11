"""On-demand grounded AI "how to tighten" recommendations (provider-aware).

The deterministic findings are the source of truth; the LLM only phrases
remediation for findings the engine produced — it can never invent one. The
prompt builds from a closed finding_id set and post-validation drops anything not
in it. Severity/effort/impact/rank are owned by code (reproducible). Async +
S3-persisted so the frontend polls (Bedrock can exceed the request timeout).
"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timezone

from utils.base_logger import get_logger
from cspm_core.schema import EFFORT_ORDER, SEV_INFO, SEVERITY_WEIGHTS

logger = get_logger(__name__)
_LLM_BUDGET_CHARS = 60_000


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _impact(severity):
    w = SEVERITY_WEIGHTS.get(severity, 0)
    return "high" if w >= 65 else "medium" if w >= 30 else "low"


def _material(snapshot):
    f = [x for x in (snapshot.get("findings") or []) if x.get("severity") != SEV_INFO]
    f.sort(key=lambda x: -SEVERITY_WEIGHTS.get(x.get("severity"), 0))
    return f


def _compact(provider, f):
    d = f.get("supporting_details", {}) or {}
    return {"finding_id": f.get("finding_id", ""), "rule_id": f.get("rule_id", ""),
            "rule_label": provider.rule_label(f.get("rule_id", "")), "severity": f.get("severity", ""),
            "scope_id": d.get("scope_id", ""), "region": d.get("region", ""),
            "entity_id": d.get("entity_id", ""), "summary": f.get("finding_summary", "")}


def _within_budget(items):
    while items and len(json.dumps(items)) > _LLM_BUDGET_CHARS:
        items.pop()
    return items


def _prompt(provider, compact):
    return (
        f"You are a senior {provider.label} cloud security engineer. Below is a JSON array of "
        "CONFIRMED posture findings from a deterministic audit. Produce concrete remediation.\n\n"
        "STRICT RULES:\n1. Recommend ONLY for the finding_id values in the input. Never invent "
        "resources, rules, or findings.\n2. Every recommendation MUST reference exactly one finding_id "
        "from the input.\n3. If you cannot map a recommendation to a provided finding_id, omit it.\n\n"
        "Return ONLY valid JSON:\n{\n  \"executive_tightening_summary\": \"<2-4 sentences grounded in the findings>\",\n"
        "  \"recommendations\": [{\"finding_id\": \"<id>\", \"recommended_action\": \"<text>\"}],\n"
        "  \"ranking_rationale\": \"<1-2 sentences>\"\n}\n\n"
        f"FINDINGS:\n{json.dumps(compact)}"
    )


def _merge_and_rank(provider, material, llm_actions):
    items = []
    for f in material:
        rid = f.get("rule_id", "")
        fid = f.get("finding_id", "")
        d = f.get("supporting_details", {}) or {}
        action = (llm_actions.get(fid) or "").strip() or provider.remediation_for(rid)
        items.append({"finding_id": fid, "rule_id": rid, "rule_label": provider.rule_label(rid),
                      "severity": f.get("severity", ""), "scope_id": d.get("scope_id", ""),
                      "region": d.get("region", ""), "entity_id": d.get("entity_id", ""),
                      "recommended_action": action, "effort": provider.effort_for(rid),
                      "impact": _impact(f.get("severity", ""))})
    items.sort(key=lambda x: (-SEVERITY_WEIGHTS.get(x["severity"], 0),
                              EFFORT_ORDER.index(x["effort"]) if x["effort"] in EFFORT_ORDER else 9))
    for i, it in enumerate(items, 1):
        it["rank"] = i
    return items


async def generate_recommendations(provider, user_id, snapshot) -> dict:
    material = _material(snapshot)
    if not material:
        return {"status": "success", "ai_used": False,
                "executive_tightening_summary": "No material findings to remediate.",
                "per_finding": [], "ranking_rationale": ""}
    compact = _within_budget([_compact(provider, f) for f in material])
    sent_ids = {c["finding_id"] for c in compact}
    llm_actions, ai_used, note = {}, False, None
    summary, rationale = "", ""
    try:
        from credits_route.route import Credits
        from db.rds_db import connect_to_rds
        from utils.fireworkzz import extract_json_safe, get_fireworks_response2

        db = connect_to_rds()
        try:
            text = await get_fireworks_response2(user_id=user_id, user_message=_prompt(provider, compact),
                                                 role="user", credits=Credits(db), temp=0.1)
        finally:
            try:
                db.close()
            except Exception:
                logger.debug("recommend db.close failed", exc_info=True)
        if text == "INSUFFICIENT":
            return {"status": "insufficient_credits", "message": "Not enough AI credits."}
        if isinstance(text, str) and text.startswith("BLOCKED_BY_GUARDRAIL"):
            return {"status": "blocked", "message": text}
        parsed = extract_json_safe(text) if text else None
        if isinstance(parsed, dict):
            ai_used = True
            for rec in parsed.get("recommendations") or []:
                fid = (rec or {}).get("finding_id", "")
                if fid in sent_ids:
                    llm_actions[fid] = (rec.get("recommended_action") or "").strip()
            summary = (parsed.get("executive_tightening_summary") or "").strip()
            rationale = (parsed.get("ranking_rationale") or "").strip()
        else:
            note = "AI response could not be parsed; using deterministic remediation."
    except Exception:
        logger.warning("%s AI recommendation failed; deterministic fallback", provider.key, exc_info=True)
        note = "AI unavailable; using deterministic remediation."

    per_finding = _merge_and_rank(provider, material, llm_actions)
    if not summary:
        crit = (snapshot.get("counts") or {}).get("by_severity", {}).get("critical", 0)
        high = (snapshot.get("counts") or {}).get("by_severity", {}).get("high", 0)
        summary = f"{len(per_finding)} findings require tightening ({crit} critical, {high} high)."
    if not rationale:
        rationale = "Ranked by severity, then remediation effort (quick wins first)."
    return {"status": "success", "ai_used": ai_used, "note": note,
            "executive_tightening_summary": summary, "per_finding": per_finding,
            "ranking_rationale": rationale, "findings_considered": len(material),
            "findings_sent_to_ai": len(compact)}


async def generate_and_store(provider, service, user_id, audit_id, scan_id, snapshot) -> dict:
    result = await generate_recommendations(provider, user_id, snapshot)
    result["scan_id"] = scan_id
    result["generated_at"] = _now()
    service.storage.save_recommendation(user_id, audit_id, scan_id, result)
    return result


def launch_generation(provider, service, user_id, audit_id, scan_id, snapshot) -> None:
    from cspm_core.helpers import release_rec_inflight

    def _target():
        try:
            asyncio.run(generate_and_store(provider, service, user_id, audit_id, scan_id, snapshot))
        except Exception:
            logger.warning("%s recommendation generation failed for %s", provider.key, scan_id, exc_info=True)
            try:
                service.storage.save_recommendation(user_id, audit_id, scan_id,
                    {"status": "error", "scan_id": scan_id, "message": "Generation failed.", "generated_at": _now()})
            except Exception:
                logger.debug("failed to persist recommendation error", exc_info=True)
        finally:
            try:
                asyncio.run(release_rec_inflight(provider.redis_namespace, f"{audit_id}:{scan_id}"))
            except Exception:
                logger.debug("release_rec_inflight failed", exc_info=True)

    threading.Thread(target=_target, name=f"{provider.key}-rec-{scan_id[:8]}", daemon=True).start()
