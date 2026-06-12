"""gcloud CLI command templates for action points (display/copy only — NEVER executed).

Mirrors gcp_audit/fixers.py: one builder per fixer-backed rule, returning
single-line `gcloud …` commands grounded in the finding's supporting_details.
"""

from __future__ import annotations


def _bucket_block_public(finding) -> list:
    sd = finding.get("supporting_details", {}) or {}
    bucket = sd.get("entity_name", "") or sd.get("entity_id", "")
    if not bucket:
        return []
    return [f"gcloud storage buckets update gs://{bucket} --public-access-prevention"]


def _firewall_disable(finding) -> list:
    sd = finding.get("supporting_details", {}) or {}
    name, project = sd.get("entity_name", ""), sd.get("scope_id", "")
    if not name:
        return []
    cmd = f"gcloud compute firewall-rules update {name} --disabled"
    if project:
        cmd += f" --project {project}"
    return [cmd]


CLI_BUILDERS = {
    "GCP_BUCKET_PUBLIC": _bucket_block_public,
    "GCP_FW_ADMIN_WORLD_OPEN": _firewall_disable,
    "GCP_FW_DB_WORLD_OPEN": _firewall_disable,
    "GCP_FW_ALL_PORTS_WORLD": _firewall_disable,
}
