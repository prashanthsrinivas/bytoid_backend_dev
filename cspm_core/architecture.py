"""Architecture View model — pure derivation from the latest posture snapshot.

Builds a scope → region → entity graph from findings' supporting_details (no new
cloud collection): every asset that has findings becomes a node carrying its
findings (with severities), severity counts and max severity, so the canvas can
badge each asset and deep-link into the per-finding drill-down. Suppressed
findings are included but flagged. Relation edges are derived where details link
resources (entity → security group via group_id, entity → VPC via vpc_id).
"""

from __future__ import annotations

from cspm_core.finding_detail import _load_sidecar
from cspm_core.schema import SEVERITY_WEIGHTS

_SEVERITIES = ("critical", "high", "medium", "low", "info")


def _empty_counts() -> dict:
    return {s: 0 for s in _SEVERITIES}


def build_architecture(ctx, user_id, audit_id, snapshot) -> dict:
    suppressed = set(_load_sidecar(ctx, user_id, audit_id, "suppressions"))
    scopes: dict = {}
    entity_index: dict = {}  # entity_id -> (scope_id, region) for relation edges
    relations: list = []
    totals = {"scopes": 0, "regions": 0, "entities": 0, "findings": 0,
              "by_severity": _empty_counts()}

    for f in snapshot.get("findings") or []:
        sd = f.get("supporting_details", {}) or {}
        scope_id = sd.get("account_id") or sd.get("scope_id") or "unknown"
        scope_name = sd.get("account_name") or sd.get("scope_name") or ""
        region = sd.get("region") or "global"
        entity_id = sd.get("entity_id") or sd.get("group_id") or f.get("finding_id", "")
        sev = f.get("severity", "info")
        m = ctx.meta(f.get("rule_id", "")) or {}

        scope = scopes.setdefault(scope_id, {"id": scope_id, "name": scope_name, "regions": {}})
        if scope_name and not scope["name"]:
            scope["name"] = scope_name
        reg = scope["regions"].setdefault(region, {"name": region, "entities": {}})
        ent = reg["entities"].setdefault(entity_id, {
            "entity_id": entity_id,
            "entity_name": sd.get("entity_name") or sd.get("group_name") or entity_id,
            "entity_type": sd.get("entity_type") or "resource",
            "findings": [], "counts_by_severity": _empty_counts(), "max_severity": "info",
        })
        ent["findings"].append({
            "finding_id": f.get("finding_id", ""), "severity": sev,
            "rule_id": f.get("rule_id", ""), "rule_label": m.get("label", f.get("rule_id", "")),
            "summary": f.get("finding_summary", ""),
            "suppressed": f.get("finding_id") in suppressed,
        })
        ent["counts_by_severity"][sev] = ent["counts_by_severity"].get(sev, 0) + 1
        if SEVERITY_WEIGHTS.get(sev, 0) > SEVERITY_WEIGHTS.get(ent["max_severity"], 0):
            ent["max_severity"] = sev
        totals["findings"] += 1
        totals["by_severity"][sev] = totals["by_severity"].get(sev, 0) + 1
        entity_index[entity_id] = (scope_id, region)

        for rel_key, label in (("group_id", "security group"), ("vpc_id", "vpc")):
            target = sd.get(rel_key)
            if target and target != entity_id:
                relations.append({"source": entity_id, "target": target, "label": label})

    # keep only relations whose both ends exist as entities, dedup
    seen = set()
    kept = []
    for r in relations:
        key = (r["source"], r["target"])
        if key in seen or r["target"] not in entity_index or r["source"] not in entity_index:
            continue
        seen.add(key)
        kept.append(r)

    scope_rows = []
    for scope in scopes.values():
        regions = []
        for reg in scope["regions"].values():
            entities = sorted(reg["entities"].values(),
                              key=lambda e: -SEVERITY_WEIGHTS.get(e["max_severity"], 0))
            for e in entities:
                e["findings"].sort(key=lambda x: -SEVERITY_WEIGHTS.get(x["severity"], 0))
            regions.append({"name": reg["name"], "entities": entities})
            totals["entities"] += len(entities)
        regions.sort(key=lambda r: r["name"])
        totals["regions"] += len(regions)
        scope_rows.append({"id": scope["id"], "name": scope["name"], "regions": regions})
    scope_rows.sort(key=lambda s: s["id"])
    totals["scopes"] = len(scope_rows)

    return {"scopes": scope_rows, "relations": kept, "totals": totals}


def architecture_payload(ctx, user_id, audit_id):
    snap = ctx.get_snapshot(user_id, audit_id, None)
    if not snap:
        return {"status": "error", "message": "No scan found"}, 404
    model = build_architecture(ctx, user_id, audit_id, snap)
    return {"status": "success", "scan_id": snap.get("scan_id"),
            "scanned_at": snap.get("scanned_at"), **model}, 200
