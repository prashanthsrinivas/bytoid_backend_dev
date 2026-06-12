"""The ``AZURE_PROVIDER`` descriptor — Azure specifics plugged into ``cspm_core``.

Credentials reuse the existing ``azure_integration`` login: the IdP config
(tenant/client/secret) mints a fresh ARM-scoped client-credentials token (the
Graph token can't read ARM), and the active Graph session token is passed through
for any Graph reads. Scopes are the org's subscriptions enumerated via ARM.
"""

from __future__ import annotations

import requests

from utils.base_logger import get_logger
from azure_audit.config import auto_remediate_enabled
from azure_audit.cli_commands import CLI_BUILDERS
from azure_audit.fixers import FIXERS
from azure_audit.metadata import CIS_FAMILIES, CIS_LABEL, DOMAIN_LABELS, DOMAINS, RULE_META
from cspm_core.provider import Provider

logger = get_logger(__name__)

PERMS = {
    "create": "azure_audit.audit.create",
    "findings_read": "azure_audit.findings.read",
    "dashboard_read": "azure_audit.dashboard.read",
    "recommend": "azure_audit.recommend.generate",
    "remediation": "azure_audit.remediation.request",
    "action_plan_generate": "azure_audit.action_plan.generate",
    "action_plan_edit": "azure_audit.action_plan.edit",
    "action_plan_request": "azure_audit.action_plan.request",
}

_ARM_SCOPE = "https://management.azure.com/.default"
_SUBSCRIPTIONS_API = "2022-12-01"


def _mint_arm_token(idp_cfg):
    token_url = f"https://login.microsoftonline.com/{idp_cfg['tenant_id']}/oauth2/v2.0/token"
    try:
        resp = requests.post(token_url, data={
            "grant_type": "client_credentials", "client_id": idp_cfg["client_id"],
            "client_secret": idp_cfg["client_secret"], "scope": _ARM_SCOPE}, timeout=20)
        if not resp.ok:
            logger.warning("azure ARM token mint failed: %s", resp.status_code)
            return None
        return resp.json().get("access_token")
    except Exception:
        logger.warning("azure ARM token mint error", exc_info=True)
        return None


def resolve_credentials(user_id):
    """Mint an ARM token from the stored Azure IdP config; attach the Graph
    session token if present. Returns None when Azure isn't connected."""
    from azure_integration.helpers import _get_active_azure_session, _get_azure_idp_config

    idp = _get_azure_idp_config(user_id)
    if not idp or not idp.get("tenant_id") or not idp.get("client_id"):
        return None
    arm_token = _mint_arm_token(idp)
    if not arm_token:
        return None
    session = _get_active_azure_session(user_id)
    return {"arm_token": arm_token, "graph_token": (session or {}).get("access_token"),
            "tenant_id": idp.get("tenant_id")}


def enumerate_scopes(creds, scope=None):
    """List enabled subscriptions, filtered to the audit's scope_ids if set."""
    from azure_audit.rest import arm_list

    wanted = set((scope or {}).get("scope_ids") or [])
    out = []
    for sub in arm_list(creds, "/subscriptions", _SUBSCRIPTIONS_API):
        sid = sub.get("subscriptionId")
        if not sid:
            continue
        if sub.get("state") and sub["state"] != "Enabled":
            continue
        if wanted and sid not in wanted:
            continue
        out.append({"id": sid, "name": sub.get("displayName") or sid})
    return out


def collect(creds, scope, domains=None):
    """Run the enabled domain collectors for one subscription. Returns
    ``(findings, status)`` where status is keyed ``{sub_id}:{domain}``."""
    from azure_audit.domains import DOMAIN_COLLECTORS

    sid = scope.get("id", "")
    sname = scope.get("name") or sid
    domains = domains or list(DOMAINS)
    findings, status = [], {}
    for d in domains:
        fn = DOMAIN_COLLECTORS.get(d)
        if not fn:
            continue
        try:
            findings += fn(creds, sid, sname) or []
            status[f"{sid}:{d}"] = "ok"
        except Exception as exc:
            logger.warning("azure %s collect failed for %s: %s", d, sid, exc)
            status[f"{sid}:{d}"] = f"error: {type(exc).__name__}"
    return findings, status


AZURE_PROVIDER = Provider(
    key="azure", label="Azure", route_prefix="azure-audit", s3_namespace="azure_audit",
    redis_namespace="azure_audit", domains=DOMAINS, domain_labels=DOMAIN_LABELS,
    rule_meta=RULE_META, cis_label=CIS_LABEL, cis_families=CIS_FAMILIES, perms=PERMS,
    resolve_credentials=resolve_credentials, enumerate_scopes=enumerate_scopes, collect=collect,
    fixers=FIXERS, auto_remediate_enabled=auto_remediate_enabled,
    cli_tool="az", cli_builders=CLI_BUILDERS, scope_label="subscription",
    default_audit_name="Azure Cloud Security Posture Audit",
    default_role_hint="Connect Azure via the Azure Integration (SAML). The app mints a read-only ARM token; "
                      "ensure the app registration has at least Reader on the target subscriptions.")
