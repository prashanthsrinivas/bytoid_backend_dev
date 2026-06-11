"""Network domain — VPC firewall ingress exposure + default network.

Pure ``analyze_firewalls`` / ``analyze_networks`` evaluate parsed Compute API
payloads. World-open == ``sourceRanges`` contains ``0.0.0.0/0`` on an enabled
INGRESS allow rule.
"""

from __future__ import annotations

from cspm_core.normalize import make_domain_finding
from gcp_audit.metadata import RULE_META

_WORLD = "0.0.0.0/0"
_ADMIN_PORTS = {22, 3389, 23, 5985, 5986}
_DB_PORTS = {1433, 3306, 5432, 1521, 27017, 6379, 11211, 9042}
_PROBE = _ADMIN_PORTS | _DB_PORTS


def _ports_from_allowed(allowed):
    """Return (probed_ports_hit:set, all_ports:bool) from a firewall ``allowed`` list."""
    hit, all_ports = set(), False
    for entry in allowed or []:
        proto = (entry.get("IPProtocol") or "").lower()
        if proto == "all":
            all_ports = True
            continue
        if proto not in ("tcp", "udp"):
            continue  # icmp/esp/etc. carry no port exposure for this check
        ports = entry.get("ports")
        if not ports:
            all_ports = True  # tcp/udp with no ports == every port
            continue
        for raw in ports:
            p = str(raw)
            if "-" in p:
                try:
                    lo, hi = (int(x) for x in p.split("-", 1))
                except ValueError:
                    continue
                if lo <= 0 and hi >= 65535:
                    all_ports = True
                else:
                    hit |= {x for x in _PROBE if lo <= x <= hi}
            else:
                try:
                    hit.add(int(p))
                except ValueError:
                    continue
    return hit, all_ports


def analyze_firewalls(firewalls, project_id, project_name="") -> list:
    findings = []
    for fw in firewalls or []:
        if fw.get("disabled"):
            continue
        if (fw.get("direction") or "INGRESS") != "INGRESS":
            continue
        if _WORLD not in (fw.get("sourceRanges") or []):
            continue
        if not fw.get("allowed"):
            continue  # deny rules carry 'denied', not 'allowed'
        hit, all_ports = _ports_from_allowed(fw.get("allowed"))
        name = fw.get("name", "")
        fid = fw.get("id") or name
        if all_ports:
            rid, sev = "GCP_FW_ALL_PORTS_WORLD", "critical"
            summ = f"Firewall '{name}' allows all ports from 0.0.0.0/0"
        elif hit & _ADMIN_PORTS:
            rid, sev = "GCP_FW_ADMIN_WORLD_OPEN", "critical"
            summ = f"Firewall '{name}' allows admin port(s) {sorted(hit & _ADMIN_PORTS)} from 0.0.0.0/0"
        elif hit & _DB_PORTS:
            rid, sev = "GCP_FW_DB_WORLD_OPEN", "critical"
            summ = f"Firewall '{name}' allows database port(s) {sorted(hit & _DB_PORTS)} from 0.0.0.0/0"
        elif hit:
            rid, sev = "GCP_FW_SENSITIVE_WORLD_OPEN", "high"
            summ = f"Firewall '{name}' allows port(s) {sorted(hit)} from 0.0.0.0/0"
        else:
            continue
        findings.append(make_domain_finding(
            rule_meta=RULE_META, rule_id=rid, severity=sev, finding_summary=summ,
            scope_id=project_id, scope_name=project_name, entity_type="firewall",
            entity_id=fid, entity_name=name, source="gcp",
            details={"source_ranges": fw.get("sourceRanges"), "allowed": fw.get("allowed"),
                     "network": fw.get("network", "")}))
    return findings


def analyze_networks(networks, project_id, project_name="") -> list:
    findings = []
    for net in networks or []:
        if net.get("name") == "default":
            findings.append(make_domain_finding(
                rule_meta=RULE_META, rule_id="GCP_DEFAULT_NETWORK", severity="medium",
                finding_summary=f"Default network present in project {project_name or project_id}",
                scope_id=project_id, scope_name=project_name, entity_type="network",
                entity_id=net.get("id") or "default", entity_name="default", source="gcp"))
    return findings


def collect(creds, project_id, project_name="") -> list:
    from gcp_audit.rest import COMPUTE_V1, list_items
    findings = []
    findings += analyze_firewalls(
        list_items(creds, f"{COMPUTE_V1}/projects/{project_id}/global/firewalls"), project_id, project_name)
    findings += analyze_networks(
        list_items(creds, f"{COMPUTE_V1}/projects/{project_id}/global/networks"), project_id, project_name)
    return findings
