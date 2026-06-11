"""Data domain — Storage accounts, SQL servers, Key Vaults.

Pure analyzers evaluate parsed ARM payloads for public exposure, weak transport,
shared-key auth, and missing encryption/purge-protection. ``collect`` fetches the
three resource types and runs them.
"""

from __future__ import annotations

from azure_audit.metadata import RULE_META
from cspm_core.normalize import make_domain_finding

_STORAGE_API = "2023-01-01"
_SQL_API = "2022-05-01-preview"
_KV_API = "2023-02-01"


def _f(rule_id, severity, summary, scope_id, scope_name, etype, eid, ename, region="", details=None):
    return make_domain_finding(
        rule_meta=RULE_META, rule_id=rule_id, severity=severity, finding_summary=summary,
        scope_id=scope_id, scope_name=scope_name, region=region, entity_type=etype,
        entity_id=eid, entity_name=ename, source="azure", details=details or {})


def analyze_storage_accounts(accounts, subscription_id, subscription_name="") -> list:
    findings = []
    for acct in accounts or []:
        props = acct.get("properties", {}) or {}
        name = acct.get("name", "")
        loc = acct.get("location", "")
        aid = acct.get("id") or name
        if props.get("allowBlobPublicAccess") is True:
            findings.append(_f("AZ_STORAGE_PUBLIC_BLOB", "high",
                               f"Storage account '{name}' allows public blob access",
                               subscription_id, subscription_name, "storage_account", aid, name, loc))
        if props.get("supportsHttpsTrafficOnly") is False:
            findings.append(_f("AZ_STORAGE_NO_HTTPS", "medium",
                               f"Storage account '{name}' does not enforce HTTPS-only",
                               subscription_id, subscription_name, "storage_account", aid, name, loc))
        if props.get("allowSharedKeyAccess") is True:
            findings.append(_f("AZ_STORAGE_SHARED_KEY", "medium",
                               f"Storage account '{name}' permits shared-key access",
                               subscription_id, subscription_name, "storage_account", aid, name, loc))
        if (props.get("networkAcls", {}) or {}).get("defaultAction") == "Allow":
            findings.append(_f("AZ_STORAGE_PUBLIC_NETWORK", "medium",
                               f"Storage account '{name}' network default action is Allow",
                               subscription_id, subscription_name, "storage_account", aid, name, loc))
    return findings


def analyze_sql_servers(servers, subscription_id, subscription_name="") -> list:
    findings = []
    for srv in servers or []:
        props = srv.get("properties", {}) or {}
        name = srv.get("name", "")
        loc = srv.get("location", "")
        sid = srv.get("id") or name
        if props.get("publicNetworkAccess") == "Enabled":
            findings.append(_f("AZ_SQL_PUBLIC_NETWORK", "high",
                               f"SQL server '{name}' allows public network access",
                               subscription_id, subscription_name, "sql_server", sid, name, loc))
    return findings


def analyze_key_vaults(vaults, subscription_id, subscription_name="") -> list:
    findings = []
    for kv in vaults or []:
        props = kv.get("properties", {}) or {}
        name = kv.get("name", "")
        loc = kv.get("location", "")
        vid = kv.get("id") or name
        if props.get("enablePurgeProtection") is not True:
            findings.append(_f("AZ_KEYVAULT_NO_PURGE_PROTECTION", "medium",
                               f"Key Vault '{name}' has purge protection disabled",
                               subscription_id, subscription_name, "key_vault", vid, name, loc))
        public = props.get("publicNetworkAccess") == "Enabled" or \
            (props.get("networkAcls", {}) or {}).get("defaultAction") == "Allow"
        if public:
            findings.append(_f("AZ_KEYVAULT_PUBLIC_NETWORK", "medium",
                               f"Key Vault '{name}' allows public network access",
                               subscription_id, subscription_name, "key_vault", vid, name, loc))
    return findings


def collect(creds, subscription_id, subscription_name="") -> list:
    from azure_audit.rest import arm_list
    sub = f"/subscriptions/{subscription_id}/providers"
    findings = []
    findings += analyze_storage_accounts(
        arm_list(creds, f"{sub}/Microsoft.Storage/storageAccounts", _STORAGE_API), subscription_id, subscription_name)
    findings += analyze_sql_servers(
        arm_list(creds, f"{sub}/Microsoft.Sql/servers", _SQL_API), subscription_id, subscription_name)
    findings += analyze_key_vaults(
        arm_list(creds, f"{sub}/Microsoft.KeyVault/vaults", _KV_API), subscription_id, subscription_name)
    return findings
