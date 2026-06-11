"""Gated, dry-run-by-default GCP remediation fixers.

Each fixer has signature ``fix(creds, finding, dry_run) -> (action_text, performed)``.
On ``dry_run=True`` it only describes the change. Real writes are guarded upstream
by ``cspm_core.autoremediate`` (provider enabled + approved workflow + explicit
non-dry-run).
"""

from __future__ import annotations


def _details(finding):
    return finding.get("supporting_details", {}) or {}


def _project(finding):
    return _details(finding).get("scope_id", "")


def fix_bucket_public(creds, finding, dry_run):
    name = _details(finding).get("entity_name", "")
    action = f"Remove allUsers / allAuthenticatedUsers from bucket {name} IAM policy"
    if dry_run:
        return action, False
    from gcp_audit.rest import STORAGE_V1, get, post
    policy = get(creds, f"{STORAGE_V1}/b/{name}/iam")
    public = {"allusers", "allauthenticatedusers"}
    new_bindings = []
    for b in policy.get("bindings", []) or []:
        members = [m for m in (b.get("members", []) or []) if m.lower() not in public]
        if members:
            new_bindings.append({**b, "members": members})
    policy["bindings"] = new_bindings
    post(creds, f"{STORAGE_V1}/b/{name}/iam", policy)
    return action, True


def fix_firewall_disable(creds, finding, dry_run):
    name = _details(finding).get("entity_name", "")
    project = _project(finding)
    action = f"Disable firewall rule {name} in project {project}"
    if dry_run:
        return action, False
    import requests

    from gcp_audit.rest import COMPUTE_V1
    url = f"{COMPUTE_V1}/projects/{project}/global/firewalls/{name}"
    resp = requests.patch(url, headers={"Authorization": f"Bearer {creds['access_token']}",
                                        "Content-Type": "application/json"},
                          json={"disabled": True}, timeout=30)
    resp.raise_for_status()
    return action, True


FIXERS = {
    "GCP_BUCKET_PUBLIC": fix_bucket_public,
    "GCP_FW_ADMIN_WORLD_OPEN": fix_firewall_disable,
    "GCP_FW_DB_WORLD_OPEN": fix_firewall_disable,
    "GCP_FW_ALL_PORTS_WORLD": fix_firewall_disable,
}
