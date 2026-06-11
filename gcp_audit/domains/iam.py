"""IAM domain — public bindings, primitive roles, stale service-account keys.

Pure analyzers evaluate a parsed Cloud Resource Manager IAM policy and the
service-account keys list. Key-age uses an injected ``now`` so the analyzer stays
deterministic and unit-testable.
"""

from __future__ import annotations

from datetime import datetime, timezone

from utils.base_logger import get_logger
from cspm_core.normalize import make_domain_finding
from gcp_audit.metadata import RULE_META

logger = get_logger(__name__)

_PUBLIC = {"allusers", "allauthenticatedusers"}
_PRIMITIVE = {"roles/owner", "roles/editor"}


def analyze_iam_policy(policy, project_id, project_name="") -> list:
    findings = []
    for binding in (policy or {}).get("bindings", []) or []:
        role = binding.get("role", "")
        members = binding.get("members", []) or []
        public = [m for m in members if m.lower() in _PUBLIC]
        if public:
            findings.append(make_domain_finding(
                rule_meta=RULE_META, rule_id="GCP_IAM_PUBLIC_MEMBER", severity="critical",
                finding_summary=f"IAM role {role} is granted to {', '.join(public)}",
                scope_id=project_id, scope_name=project_name, entity_type="iam_binding",
                entity_id=role, entity_name=role, source="gcp", details={"role": role, "members": public}))
        if role in _PRIMITIVE:
            users = [m for m in members if m.startswith("user:")]
            if users:
                findings.append(make_domain_finding(
                    rule_meta=RULE_META, rule_id="GCP_IAM_PRIMITIVE_ROLE", severity="medium",
                    finding_summary=f"Primitive role {role} is granted to {len(users)} user(s)",
                    scope_id=project_id, scope_name=project_name, entity_type="iam_binding",
                    entity_id=role, entity_name=role, source="gcp", details={"role": role, "members": users}))
    return findings


def analyze_sa_keys(keys, service_account_email, project_id, project_name="", now=None, max_age_days=90) -> list:
    now = now or datetime.now(timezone.utc)
    findings = []
    for key in keys or []:
        if key.get("keyType") != "USER_MANAGED":
            continue
        valid_after = key.get("validAfterTime")
        if not valid_after:
            continue
        try:
            created = datetime.fromisoformat(str(valid_after).replace("Z", "+00:00"))
        except ValueError:
            continue
        age = (now - created).days
        if age > max_age_days:
            key_id = (key.get("name", "") or "").split("/")[-1]
            findings.append(make_domain_finding(
                rule_meta=RULE_META, rule_id="GCP_SA_USER_MANAGED_KEY_STALE", severity="medium",
                finding_summary=f"Service-account key for {service_account_email} is {age} days old (> {max_age_days})",
                scope_id=project_id, scope_name=project_name, entity_type="sa_key",
                entity_id=key.get("name") or key_id, entity_name=service_account_email, source="gcp",
                details={"age_days": age, "service_account": service_account_email}))
    return findings


def collect(creds, project_id, project_name="") -> list:
    from gcp_audit.rest import CRM_V1, IAM_V1, get, list_items, post
    findings = []
    policy = post(creds, f"{CRM_V1}/projects/{project_id}:getIamPolicy",
                  {"options": {"requestedPolicyVersion": 3}})
    findings += analyze_iam_policy(policy, project_id, project_name)
    try:
        accounts = list_items(creds, f"{IAM_V1}/projects/{project_id}/serviceAccounts", item_key="accounts")
    except Exception:
        accounts = []
    for sa in accounts:
        email = sa.get("email", "")
        if not email:
            continue
        try:
            data = get(creds, f"{IAM_V1}/projects/{project_id}/serviceAccounts/{email}/keys")
            findings += analyze_sa_keys(data.get("keys", []), email, project_id, project_name)
        except Exception:
            logger.debug("gcp SA key fetch failed for %s", email, exc_info=True)
            continue
    return findings
