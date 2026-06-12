"""The ``GCP_PROVIDER`` descriptor — GCP specifics plugged into ``cspm_core``.

Credentials reuse the existing ``gcp_integration`` login: the stored service-account
JSON key mints a fresh ``cloud-platform`` Bearer. Scopes are the org's projects
(via Cloud Resource Manager when an ``organization_id`` is in the audit scope,
else the configured project).
"""

from __future__ import annotations

from utils.base_logger import get_logger
from cspm_core.provider import Provider
from gcp_audit.config import auto_remediate_enabled
from gcp_audit.cli_commands import CLI_BUILDERS
from gcp_audit.fixers import FIXERS
from gcp_audit.metadata import CIS_FAMILIES, CIS_LABEL, DOMAIN_LABELS, DOMAINS, RULE_META

logger = get_logger(__name__)

PERMS = {
    "create": "gcp_audit.audit.create",
    "findings_read": "gcp_audit.findings.read",
    "dashboard_read": "gcp_audit.dashboard.read",
    "recommend": "gcp_audit.recommend.generate",
    "remediation": "gcp_audit.remediation.request",
    "action_plan_generate": "gcp_audit.action_plan.generate",
    "action_plan_edit": "gcp_audit.action_plan.edit",
    "action_plan_request": "gcp_audit.action_plan.request",
}


def resolve_credentials(user_id):
    """Mint a fresh cloud-platform token from the stored GCP service-account key.
    Returns None when GCP isn't connected."""
    from gcp_integration.helpers import _fetch_service_account_token, _get_gcp_config

    cfg = _get_gcp_config(user_id)
    if not cfg or not cfg.get("service_account_key"):
        return None
    token, err = _fetch_service_account_token(cfg)
    if err or not token or not token.get("access_token"):
        return None
    return {"access_token": token["access_token"], "project_id": cfg.get("project_id")}


def enumerate_scopes(creds, scope=None):
    """List ACTIVE projects under the audit's organization_id, else fall back to
    the configured project. Filtered to scope_ids if set."""
    from gcp_audit.rest import CRM_V1, list_items

    scope = scope or {}
    wanted = set(scope.get("scope_ids") or [])
    org = scope.get("organization_id")
    projects = []
    if org:
        org_id = str(org).split("/")[-1]
        for p in list_items(creds, f"{CRM_V1}/projects", item_key="projects"):
            parent = p.get("parent", {}) or {}
            if parent.get("type") == "organization" and str(parent.get("id")) == org_id \
                    and p.get("lifecycleState", "ACTIVE") == "ACTIVE":
                projects.append({"id": p.get("projectId"), "name": p.get("name") or p.get("projectId")})
    if not projects:
        pid = creds.get("project_id")
        if pid:
            projects = [{"id": pid, "name": pid}]
    if wanted:
        projects = [p for p in projects if p["id"] in wanted]
    return projects


def collect(creds, scope, domains=None):
    """Run the enabled domain collectors for one project. Returns
    ``(findings, status)`` keyed ``{project_id}:{domain}``."""
    from gcp_audit.domains import DOMAIN_COLLECTORS

    pid = scope.get("id", "")
    pname = scope.get("name") or pid
    domains = domains or list(DOMAINS)
    findings, status = [], {}
    for d in domains:
        fn = DOMAIN_COLLECTORS.get(d)
        if not fn:
            continue
        try:
            findings += fn(creds, pid, pname) or []
            status[f"{pid}:{d}"] = "ok"
        except Exception as exc:
            logger.warning("gcp %s collect failed for %s: %s", d, pid, exc)
            status[f"{pid}:{d}"] = f"error: {type(exc).__name__}"
    return findings, status


GCP_PROVIDER = Provider(
    key="gcp", label="GCP", route_prefix="gcp-audit", s3_namespace="gcp_audit",
    redis_namespace="gcp_audit", domains=DOMAINS, domain_labels=DOMAIN_LABELS,
    rule_meta=RULE_META, cis_label=CIS_LABEL, cis_families=CIS_FAMILIES, perms=PERMS,
    resolve_credentials=resolve_credentials, enumerate_scopes=enumerate_scopes, collect=collect,
    fixers=FIXERS, auto_remediate_enabled=auto_remediate_enabled,
    cli_tool="gcloud", cli_builders=CLI_BUILDERS, scope_label="project",
    default_audit_name="GCP Cloud Security Posture Audit",
    default_role_hint="Connect GCP via the GCP Integration (service-account key). Grant the service account "
                      "Viewer + Security Reviewer on the target org/projects. Set organization_id to scan org-wide.")
