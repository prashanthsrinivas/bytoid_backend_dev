"""Network domain — Network Security Group inbound exposure analysis.

Pure ``analyze_nsgs`` evaluates parsed ARM NSG payloads; ``collect`` fetches them.
World-open == source is ``*`` / ``0.0.0.0/0`` / ``Internet`` / ``::/0`` on an
inbound Allow rule. IPv6 ``::/0`` is treated identically to ``0.0.0.0/0``.
"""

from __future__ import annotations

from azure_audit.metadata import RULE_META
from cspm_core.normalize import make_domain_finding

_NSG_API = "2023-09-01"
_WORLD = {"*", "0.0.0.0/0", "internet", "::/0"}
_ADMIN_PORTS = {22, 3389, 5985, 5986, 23}
_DB_PORTS = {1433, 3306, 5432, 1521, 27017, 6379, 5984, 9042, 11211}
_PROBE_PORTS = _ADMIN_PORTS | _DB_PORTS


def _sources(props) -> list:
    out = []
    if props.get("sourceAddressPrefix"):
        out.append(props["sourceAddressPrefix"])
    out += props.get("sourceAddressPrefixes") or []
    return out


def _is_world(sources) -> bool:
    return any(str(x).strip().lower() in _WORLD for x in sources)


def _port_strings(props) -> list:
    out = []
    if props.get("destinationPortRange"):
        out.append(props["destinationPortRange"])
    out += props.get("destinationPortRanges") or []
    return out


def _expand_ports(port_strs):
    """Return (probed_ports_hit:set, all_ports:bool). Ranges are tested only for
    membership of the admin/db probe set (we don't enumerate huge ranges)."""
    hit, all_ports = set(), False
    for raw in port_strs:
        p = str(raw).strip()
        if p == "*":
            all_ports = True
        elif "-" in p:
            try:
                lo, hi = (int(x) for x in p.split("-", 1))
            except ValueError:
                continue
            if lo <= 0 and hi >= 65535:
                all_ports = True
            else:
                hit |= {port for port in _PROBE_PORTS if lo <= port <= hi}
        else:
            try:
                hit.add(int(p))
            except ValueError:
                continue
    return hit, all_ports


def analyze_nsgs(nsgs, subscription_id, subscription_name="", region="") -> list:
    findings = []
    for nsg in nsgs or []:
        props = nsg.get("properties", {}) or {}
        name = nsg.get("name", "")
        loc = nsg.get("location", region)
        nsg_id = nsg.get("id") or name
        for rule in props.get("securityRules", []) or []:
            rp = rule.get("properties", {}) or {}
            if rp.get("direction") != "Inbound" or rp.get("access") != "Allow":
                continue
            if not _is_world(_sources(rp)):
                continue
            ports, all_ports = _expand_ports(_port_strings(rp))
            rname = rule.get("name", "")
            if all_ports:
                rid, sev = "AZ_NSG_ALL_PORTS_WORLD", "critical"
                summ = f"NSG '{name}' rule '{rname}' allows all ports from the Internet"
            elif ports & _ADMIN_PORTS:
                rid, sev = "AZ_NSG_ADMIN_WORLD_OPEN", "critical"
                summ = f"NSG '{name}' allows admin port(s) {sorted(ports & _ADMIN_PORTS)} from the Internet"
            elif ports & _DB_PORTS:
                rid, sev = "AZ_NSG_DB_WORLD_OPEN", "critical"
                summ = f"NSG '{name}' allows database port(s) {sorted(ports & _DB_PORTS)} from the Internet"
            elif ports:
                rid, sev = "AZ_NSG_SENSITIVE_WORLD_OPEN", "high"
                summ = f"NSG '{name}' allows port(s) {sorted(ports)} from the Internet"
            else:
                continue
            findings.append(make_domain_finding(
                rule_meta=RULE_META, rule_id=rid, severity=sev, finding_summary=summ,
                scope_id=subscription_id, scope_name=subscription_name, region=loc,
                entity_type="nsg", entity_id=nsg_id, entity_name=name, source="azure",
                details={"rule_name": rname, "ports": sorted(ports) if ports else "*",
                         "sources": _sources(rp), "protocol": rp.get("protocol", "")}))
    return findings


def collect(creds, subscription_id, subscription_name="") -> list:
    from azure_audit.rest import arm_list
    nsgs = arm_list(creds, f"/subscriptions/{subscription_id}/providers/Microsoft.Network/networkSecurityGroups", _NSG_API)
    return analyze_nsgs(nsgs, subscription_id, subscription_name)
