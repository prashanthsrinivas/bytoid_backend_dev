"""Compute domain — Compute Engine instance posture.

Pure ``analyze_instances`` evaluates parsed instance payloads (from the aggregated
list) for external IPs, OS Login, default-SA full scope, and Shielded VM.
"""

from __future__ import annotations

from cspm_core.normalize import make_domain_finding
from gcp_audit.metadata import RULE_META

_FULL_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


def _f(rule_id, severity, summary, project_id, project_name, eid, ename, region, details=None):
    return make_domain_finding(
        rule_meta=RULE_META, rule_id=rule_id, severity=severity, finding_summary=summary,
        scope_id=project_id, scope_name=project_name, region=region, entity_type="instance",
        entity_id=eid, entity_name=ename, source="gcp", details=details or {})


def _has_external_ip(inst) -> bool:
    for nic in inst.get("networkInterfaces", []) or []:
        for ac in nic.get("accessConfigs", []) or []:
            if ac.get("natIP") or ac.get("type") == "ONE_TO_ONE_NAT":
                return True
    return False


def _os_login_enabled(inst) -> bool:
    for item in (inst.get("metadata", {}) or {}).get("items", []) or []:
        if item.get("key") == "enable-oslogin":
            return str(item.get("value", "")).lower() in ("true", "1")
    return False


def _uses_default_sa_full_scope(inst) -> bool:
    for sa in inst.get("serviceAccounts", []) or []:
        if sa.get("email", "").endswith("-compute@developer.gserviceaccount.com") \
                and _FULL_SCOPE in (sa.get("scopes") or []):
            return True
    return False


def analyze_instances(instances, project_id, project_name="") -> list:
    findings = []
    for inst in instances or []:
        name = inst.get("name", "")
        iid = inst.get("id") or name
        zone = (inst.get("zone", "") or "").split("/")[-1]
        if _has_external_ip(inst):
            findings.append(_f("GCP_INSTANCE_PUBLIC_IP", "medium",
                               f"Instance '{name}' has an external IP", project_id, project_name, iid, name, zone))
        if not _os_login_enabled(inst):
            findings.append(_f("GCP_OS_LOGIN_DISABLED", "medium",
                               f"Instance '{name}' does not have OS Login enabled",
                               project_id, project_name, iid, name, zone))
        if _uses_default_sa_full_scope(inst):
            findings.append(_f("GCP_DEFAULT_SA_FULL_SCOPE", "high",
                               f"Instance '{name}' uses the default service account with full cloud-platform scope",
                               project_id, project_name, iid, name, zone))
        shielded = inst.get("shieldedInstanceConfig", {}) or {}
        if not (shielded.get("enableSecureBoot") and shielded.get("enableVtpm")
                and shielded.get("enableIntegrityMonitoring")):
            findings.append(_f("GCP_SHIELDED_VM_OFF", "low",
                               f"Instance '{name}' does not have Shielded VM fully enabled",
                               project_id, project_name, iid, name, zone))
    return findings


def collect(creds, project_id, project_name="") -> list:
    from gcp_audit.rest import COMPUTE_V1, get
    url = f"{COMPUTE_V1}/projects/{project_id}/aggregated/instances"
    instances, params = [], {}
    while True:
        data = get(creds, url, params)
        for _zone, block in (data.get("items", {}) or {}).items():
            instances += (block or {}).get("instances", []) or []
        tok = data.get("nextPageToken")
        if not tok:
            break
        params["pageToken"] = tok
    return analyze_instances(instances, project_id, project_name)
