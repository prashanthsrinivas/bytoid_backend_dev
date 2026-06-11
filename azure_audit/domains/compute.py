"""Compute domain — Virtual Machines + Public IP exposure.

Pure analyzers evaluate parsed ARM VM payloads (disk encryption / unmanaged disks)
and Public IP address resources (in-use direct exposure). ``collect`` fetches both.
"""

from __future__ import annotations

from azure_audit.metadata import RULE_META
from cspm_core.normalize import make_domain_finding

_VM_API = "2023-07-01"
_PIP_API = "2023-09-01"


def _f(rule_id, severity, summary, scope_id, scope_name, etype, eid, ename, region="", details=None):
    return make_domain_finding(
        rule_meta=RULE_META, rule_id=rule_id, severity=severity, finding_summary=summary,
        scope_id=scope_id, scope_name=scope_name, region=region, entity_type=etype,
        entity_id=eid, entity_name=ename, source="azure", details=details or {})


def analyze_vms(vms, subscription_id, subscription_name="") -> list:
    findings = []
    for vm in vms or []:
        props = vm.get("properties", {}) or {}
        name = vm.get("name", "")
        loc = vm.get("location", "")
        vid = vm.get("id") or name
        os_disk = (props.get("storageProfile", {}) or {}).get("osDisk", {}) or {}
        if "managedDisk" not in os_disk and os_disk.get("vhd"):
            findings.append(_f("AZ_VM_UNMANAGED_DISK", "low",
                               f"VM '{name}' uses unmanaged (page-blob) disks",
                               subscription_id, subscription_name, "virtual_machine", vid, name, loc))
        encrypted = bool(os_disk.get("encryptionSettings")) or \
            bool((props.get("securityProfile", {}) or {}).get("encryptionAtHost"))
        if not encrypted:
            findings.append(_f("AZ_VM_NO_DISK_ENCRYPTION", "medium",
                               f"VM '{name}' has no disk encryption / encryption-at-host enabled",
                               subscription_id, subscription_name, "virtual_machine", vid, name, loc))
    return findings


def analyze_public_ips(public_ips, subscription_id, subscription_name="") -> list:
    findings = []
    for pip in public_ips or []:
        props = pip.get("properties", {}) or {}
        if not props.get("ipConfiguration"):
            continue  # unattached IPs are not an active exposure
        name = pip.get("name", "")
        loc = pip.get("location", "")
        addr = props.get("ipAddress", "")
        findings.append(_f("AZ_PUBLIC_IP_IN_USE", "medium",
                           f"Public IP '{name}' ({addr or 'allocated'}) is attached to a resource",
                           subscription_id, subscription_name, "public_ip", pip.get("id") or name, name, loc,
                           details={"ip_address": addr}))
    return findings


def collect(creds, subscription_id, subscription_name="") -> list:
    from azure_audit.rest import arm_list
    sub = f"/subscriptions/{subscription_id}/providers"
    findings = []
    findings += analyze_vms(
        arm_list(creds, f"{sub}/Microsoft.Compute/virtualMachines", _VM_API), subscription_id, subscription_name)
    findings += analyze_public_ips(
        arm_list(creds, f"{sub}/Microsoft.Network/publicIPAddresses", _PIP_API), subscription_id, subscription_name)
    return findings
