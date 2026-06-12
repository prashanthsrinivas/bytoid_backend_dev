"""Azure CLI command templates for action points (display/copy only — NEVER executed).

Mirrors azure_audit/fixers.py: one builder per fixer-backed rule, returning
single-line `az …` commands grounded in the finding's supporting_details.
"""

from __future__ import annotations

import re

_RG_RE = re.compile(r"/resourceGroups/([^/]+)/", re.IGNORECASE)


def _storage_update(finding, flags: str) -> list:
    sd = finding.get("supporting_details", {}) or {}
    name = sd.get("entity_name", "")
    if not name:
        return []
    cmd = f"az storage account update --name {name}"
    rg = _RG_RE.search(sd.get("entity_id", "") or "")
    if rg:
        cmd += f" --resource-group {rg.group(1)}"
    sub = sd.get("scope_id", "")
    if sub:
        cmd += f" --subscription {sub}"
    return [cmd + f" {flags}"]


CLI_BUILDERS = {
    "AZ_STORAGE_PUBLIC_BLOB": lambda f: _storage_update(f, "--allow-blob-public-access false"),
    "AZ_STORAGE_NO_HTTPS": lambda f: _storage_update(f, "--https-only true"),
    "AZ_STORAGE_PUBLIC_NETWORK": lambda f: _storage_update(f, "--default-action Deny"),
}
