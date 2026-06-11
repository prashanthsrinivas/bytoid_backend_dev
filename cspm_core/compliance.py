"""Compliance coverage — CIS (per cloud) + SOC 2 + ISO 27001 (shared).

CIS controls come from each provider's ``rule_meta[*].cis`` (the cloud's own CIS
benchmark, labelled by ``provider.cis_label``/``cis_families``). SOC 2 / ISO are
derived from a finding's category via shared, cloud-agnostic maps. Honest framing:
"coverage of the controls Bytoid evaluates", not full-framework attestation.
"""

from __future__ import annotations

FRAMEWORK_CIS = "CIS"
FRAMEWORK_SOC2 = "SOC2"
FRAMEWORK_ISO = "ISO27001"
FRAMEWORKS = (FRAMEWORK_CIS, FRAMEWORK_SOC2, FRAMEWORK_ISO)

SOC2_FAMILIES = {"CC6": "Logical & Physical Access", "CC7": "System Operations", "CC8": "Change Management"}
ISO_FAMILIES = {"A.8": "Asset Management", "A.9": "Access Control", "A.10": "Cryptography",
                "A.12": "Operations Security", "A.13": "Communications Security"}

_CATEGORY_SOC2 = {
    "identity": ["CC6.1", "CC6.2", "CC6.3"], "access_control": ["CC6.1", "CC6.6"],
    "network_exposure": ["CC6.6"], "public_access": ["CC6.6", "CC6.7"],
    "data_exposure": ["CC6.6", "CC6.7"], "encryption": ["CC6.7"], "egress": ["CC6.6"],
    "logging": ["CC7.2"], "monitoring": ["CC7.1", "CC7.2"], "patch_management": ["CC7.1"], "hygiene": ["CC6.1"],
}
_CATEGORY_ISO = {
    "identity": ["A.9.2", "A.9.4"], "access_control": ["A.9.1", "A.9.4"],
    "network_exposure": ["A.13.1"], "public_access": ["A.13.1", "A.9.4"],
    "data_exposure": ["A.8.2", "A.13.1"], "encryption": ["A.10.1"], "egress": ["A.13.1"],
    "logging": ["A.12.4"], "monitoring": ["A.12.4"], "patch_management": ["A.12.6"], "hygiene": ["A.9.2"],
}


def _cis_control_rules(provider) -> dict:
    index: dict = {}
    for rid, m in provider.rule_meta.items():
        for control in m.get("cis", []) or []:
            index.setdefault(control, []).append(rid)
    return index


def _iso_family(control: str) -> str:
    parts = str(control).split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else control


def _build(framework, framework_label, control_to_keys, key_fn, family_fn, families, findings) -> dict:
    failing_keys = {key_fn(f) for f in findings}
    controls, family_roll, passing = [], {}, 0
    for control in sorted(control_to_keys):
        keys = control_to_keys[control]
        hits = [f for f in findings if key_fn(f) in keys]
        is_failing = bool(hits) and bool(set(keys) & failing_keys)
        if not is_failing:
            passing += 1
        fam = family_fn(control)
        controls.append({"control": control, "family": fam, "family_label": families.get(fam, "Other"),
                         "status": "fail" if is_failing else "pass", "finding_count": len(hits)})
        roll = family_roll.setdefault(fam, {"evaluated": 0, "failing": 0})
        roll["evaluated"] += 1
        if is_failing:
            roll["failing"] += 1
    total = len(control_to_keys)
    heatmap = [
        {"family": fam, "label": families.get(fam, "Other"), "evaluated": v["evaluated"],
         "failing": v["failing"], "passing": v["evaluated"] - v["failing"],
         "coverage_pct": round(100.0 * (v["evaluated"] - v["failing"]) / v["evaluated"], 1) if v["evaluated"] else 100.0}
        for fam, v in sorted(family_roll.items())
    ]
    return {
        "framework": framework, "framework_label": framework_label, "evaluated": total,
        "passing": passing, "failing": total - passing,
        "coverage_pct": round(100.0 * passing / total, 1) if total else 100.0,
        "failing_controls": [c for c in controls if c["status"] == "fail"],
        "controls": controls, "heatmap": heatmap,
    }


def coverage_for(snapshot, provider, framework=FRAMEWORK_CIS) -> dict:
    findings = snapshot.get("findings") or []
    if framework == FRAMEWORK_CIS:
        ctk = {c: set(rules) for c, rules in _cis_control_rules(provider).items()}
        return _build(FRAMEWORK_CIS, provider.cis_label, ctk, lambda f: f.get("rule_id"),
                      lambda c: str(c).split(".", 1)[0], provider.cis_families, findings)
    cat_map = _CATEGORY_SOC2 if framework == FRAMEWORK_SOC2 else _CATEGORY_ISO
    families = SOC2_FAMILIES if framework == FRAMEWORK_SOC2 else ISO_FAMILIES
    family_fn = (lambda c: str(c).split(".", 1)[0]) if framework == FRAMEWORK_SOC2 else _iso_family
    label = "SOC 2 (Trust Services Criteria)" if framework == FRAMEWORK_SOC2 else "ISO/IEC 27001 Annex A"
    control_to_cats: dict = {}
    for cat, controls in cat_map.items():
        for c in controls:
            control_to_cats.setdefault(c, set()).add(cat)
    return _build(framework, label, control_to_cats, lambda f: f.get("category"), family_fn, families, findings)


def all_frameworks(snapshot, provider) -> list:
    return [coverage_for(snapshot, provider, fw) for fw in FRAMEWORKS]
