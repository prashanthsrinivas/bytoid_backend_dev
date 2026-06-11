"""Gated, dry-run-by-default Azure remediation fixers.

Each fixer has signature ``fix(creds, finding, dry_run) -> (action_text, performed)``.
On ``dry_run=True`` it only describes the change. Real writes are ARM PATCHes
guarded upstream by ``cspm_core.autoremediate`` (provider enabled + approved
workflow + explicit non-dry-run). The ARM resource id is the finding's entity_id.
"""

from __future__ import annotations

_STORAGE_API = "2023-01-01"
_NSG_API = "2023-09-01"


def _resource_id(finding) -> str:
    return finding.get("supporting_details", {}).get("entity_id", "") or finding.get("entity_id", "")


def fix_storage_public_blob(creds, finding, dry_run):
    rid = _resource_id(finding)
    action = f"Set allowBlobPublicAccess=false on storage account {rid}"
    if dry_run:
        return action, False
    from azure_audit.rest import arm_patch
    arm_patch(creds, rid, _STORAGE_API, {"properties": {"allowBlobPublicAccess": False}})
    return action, True


def fix_storage_https_only(creds, finding, dry_run):
    rid = _resource_id(finding)
    action = f"Enable supportsHttpsTrafficOnly=true on storage account {rid}"
    if dry_run:
        return action, False
    from azure_audit.rest import arm_patch
    arm_patch(creds, rid, _STORAGE_API, {"properties": {"supportsHttpsTrafficOnly": True}})
    return action, True


def fix_storage_default_deny(creds, finding, dry_run):
    rid = _resource_id(finding)
    action = f"Set networkAcls.defaultAction=Deny on storage account {rid}"
    if dry_run:
        return action, False
    from azure_audit.rest import arm_patch
    arm_patch(creds, rid, _STORAGE_API, {"properties": {"networkAcls": {"defaultAction": "Deny"}}})
    return action, True


FIXERS = {
    "AZ_STORAGE_PUBLIC_BLOB": fix_storage_public_blob,
    "AZ_STORAGE_NO_HTTPS": fix_storage_https_only,
    "AZ_STORAGE_PUBLIC_NETWORK": fix_storage_default_deny,
}
