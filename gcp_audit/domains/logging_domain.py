"""Logging & Monitoring domain — audit-log config + log sinks.

``analyze_audit_configs`` flags a project whose IAM policy ``auditConfigs`` do not
cover ``allServices`` with the full set of log types; ``analyze_sinks`` flags a
project with no log sink. Named ``logging_domain`` to avoid shadowing stdlib.
"""

from __future__ import annotations

from cspm_core.normalize import make_domain_finding
from gcp_audit.metadata import RULE_META

_NEEDED_LOG_TYPES = {"DATA_READ", "DATA_WRITE", "ADMIN_READ"}


def analyze_audit_configs(policy, project_id, project_name="") -> list:
    configs = (policy or {}).get("auditConfigs", []) or []
    all_svc = next((c for c in configs if c.get("service") == "allServices"), None)
    have = {lc.get("logType") for lc in (all_svc or {}).get("auditLogConfigs", []) or []}
    if all_svc and _NEEDED_LOG_TYPES <= have:
        return []
    return [make_domain_finding(
        rule_meta=RULE_META, rule_id="GCP_AUDIT_LOGGING_INCOMPLETE", severity="medium",
        finding_summary=f"Project {project_name or project_id} does not enable audit logging for all services",
        scope_id=project_id, scope_name=project_name, entity_type="project",
        entity_id=project_id, entity_name=project_name or project_id, source="gcp",
        details={"configured_log_types": sorted(have)})]


def analyze_sinks(sinks, project_id, project_name="") -> list:
    if sinks:
        return []
    return [make_domain_finding(
        rule_meta=RULE_META, rule_id="GCP_NO_LOG_SINK", severity="low",
        finding_summary=f"Project {project_name or project_id} has no logging sink configured",
        scope_id=project_id, scope_name=project_name, entity_type="project",
        entity_id=project_id, entity_name=project_name or project_id, source="gcp")]


def collect(creds, project_id, project_name="") -> list:
    from gcp_audit.rest import CRM_V1, LOGGING_V2, list_items, post
    findings = []
    policy = post(creds, f"{CRM_V1}/projects/{project_id}:getIamPolicy",
                  {"options": {"requestedPolicyVersion": 3}})
    findings += analyze_audit_configs(policy, project_id, project_name)
    sinks = list_items(creds, f"{LOGGING_V2}/projects/{project_id}/sinks", item_key="sinks")
    findings += analyze_sinks(sinks, project_id, project_name)
    return findings
