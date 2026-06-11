"""Identity domain — Entra ID / Azure RBAC analysis.

Flags Owner assignments at subscription scope, custom roles granting wildcard
actions, and lingering classic administrators. Pure analyzers take parsed ARM
``roleAssignments`` / ``roleDefinitions`` / ``classicAdministrators`` payloads.
"""

from __future__ import annotations

from azure_audit.metadata import RULE_META
from cspm_core.normalize import make_domain_finding

_RA_API = "2022-04-01"
_RD_API = "2022-04-01"
_CLASSIC_API = "2015-07-01"
# Well-known Owner built-in role definition GUID.
_OWNER_GUID = "8e3af657-a8ff-443c-a75c-2fe8c4bcb635"


def analyze_role_assignments(assignments, subscription_id, subscription_name="") -> list:
    findings = []
    sub_scope = f"/subscriptions/{subscription_id}".lower()
    for ra in assignments or []:
        props = ra.get("properties", {}) or {}
        rdid = (props.get("roleDefinitionId") or "").lower()
        scope = (props.get("scope") or "").lower()
        if _OWNER_GUID in rdid and (scope == sub_scope or scope == "" or scope == "/"):
            pid = props.get("principalId", "")
            findings.append(make_domain_finding(
                rule_meta=RULE_META, rule_id="AZ_RBAC_SUBSCRIPTION_OWNER", severity="high",
                finding_summary=f"Principal {pid} ({props.get('principalType', 'Unknown')}) holds Owner at subscription scope",
                scope_id=subscription_id, scope_name=subscription_name,
                entity_type="role_assignment", entity_id=ra.get("name") or ra.get("id") or pid,
                entity_name=pid, source="azure",
                details={"principal_type": props.get("principalType"), "scope": props.get("scope")}))
    return findings


def analyze_role_definitions(definitions, subscription_id, subscription_name="") -> list:
    findings = []
    for rd in definitions or []:
        props = rd.get("properties", {}) or {}
        if (props.get("type") or props.get("roleType")) != "CustomRole":
            continue
        actions = []
        for perm in props.get("permissions", []) or []:
            actions += perm.get("actions", []) or []
        if "*" in actions:
            name = props.get("roleName", "") or rd.get("name", "")
            findings.append(make_domain_finding(
                rule_meta=RULE_META, rule_id="AZ_RBAC_CUSTOM_WILDCARD_ROLE", severity="high",
                finding_summary=f"Custom role '{name}' grants wildcard (*) actions",
                scope_id=subscription_id, scope_name=subscription_name,
                entity_type="role_definition", entity_id=rd.get("id") or name, entity_name=name,
                source="azure", details={"actions": actions}))
    return findings


def analyze_classic_admins(admins, subscription_id, subscription_name="") -> list:
    findings = []
    for adm in admins or []:
        props = adm.get("properties", {}) or {}
        email = props.get("emailAddress", "") or adm.get("name", "")
        findings.append(make_domain_finding(
            rule_meta=RULE_META, rule_id="AZ_CLASSIC_ADMIN_PRESENT", severity="medium",
            finding_summary=f"Classic administrator '{email}' ({props.get('role', '')}) still assigned",
            scope_id=subscription_id, scope_name=subscription_name,
            entity_type="classic_admin", entity_id=adm.get("id") or email, entity_name=email,
            source="azure", details={"role": props.get("role")}))
    return findings


def collect(creds, subscription_id, subscription_name="") -> list:
    from azure_audit.rest import arm_list
    base = f"/subscriptions/{subscription_id}/providers/Microsoft.Authorization"
    findings = []
    findings += analyze_role_assignments(
        arm_list(creds, f"{base}/roleAssignments", _RA_API), subscription_id, subscription_name)
    findings += analyze_role_definitions(
        arm_list(creds, f"{base}/roleDefinitions", _RD_API), subscription_id, subscription_name)
    findings += analyze_classic_admins(
        arm_list(creds, f"{base}/classicAdministrators", _CLASSIC_API), subscription_id, subscription_name)
    return findings
