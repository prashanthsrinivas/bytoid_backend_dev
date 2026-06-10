"""IAM domain collector — identity & access posture (account-scoped).

Pure ``analyze_*`` functions (no boto3) + a ``collect`` that fetches the
credential report, the account authorization details, and the password policy.
Findings cover: admin/wildcard access, missing MFA (user + root), root access
keys, unused credentials, stale access keys, weak password policy, and
cross-account trust risks.
"""

from __future__ import annotations

import csv
import io
import json
import time
from contextlib import suppress
from datetime import datetime, timezone
from urllib.parse import unquote

from sg_audit.analysis.normalize import make_domain_finding
from sg_audit.metadata import (
    IAM_ADMIN_ACCESS,
    IAM_CROSS_ACCOUNT_TRUST_WILDCARD,
    IAM_GITHUB_OIDC_TRUST_WILDCARD,
    IAM_NO_PASSWORD_POLICY,
    IAM_ROOT_HAS_KEYS,
    IAM_ROOT_NO_MFA,
    IAM_STALE_ACCESS_KEY,
    IAM_UNUSED_CREDENTIAL,
    IAM_USER_NO_MFA,
    IAM_WILDCARD_POLICY,
)
from sg_audit.schema import DOMAIN_IAM

DOMAIN = DOMAIN_IAM
SCOPE = "account"

_STALE_DAYS = 90
_SOURCE = "iam"


# ── helpers ──────────────────────────────────────────────────────────────────

def _days_since(value: str) -> float | None:
    """Days since an ISO8601 timestamp; None for N/A / no_information / unparsable."""
    if not value or value in ("N/A", "no_information", "not_supported"):
        return None
    try:
        s = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
    except ValueError:
        return None


def _truthy(v) -> bool:
    return str(v).lower() == "true"


def _policy_doc(raw) -> dict:
    """Return a policy document dict from a dict or URL-encoded JSON string."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(unquote(raw))
        except (ValueError, TypeError):
            return {}
    return {}


def _as_list(v) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _stmt_is_wildcard(stmt: dict) -> bool:
    if stmt.get("Effect") != "Allow":
        return False
    actions = [str(a) for a in _as_list(stmt.get("Action"))]
    resources = [str(r) for r in _as_list(stmt.get("Resource"))]
    return "*" in actions and "*" in resources


def _github_sub_restricted(condition: dict) -> bool:
    """True if the trust condition restricts the GitHub Actions OIDC `sub` claim
    to something specific (not absent and not a bare wildcard)."""
    for operator in (condition or {}).values():
        if not isinstance(operator, dict):
            continue
        for key, val in operator.items():
            if str(key).endswith(":sub"):
                for v in _as_list(val):
                    sv = str(v)
                    if sv and sv not in ("*", "repo:*", "repo:*:*"):
                        return True
    return False


# ── pure analyzers ────────────────────────────────────────────────────────────

def analyze_credential_report(rows: list[dict], account_id: str) -> list[dict]:
    out = []
    for row in rows or []:
        user = row.get("user", "")
        is_root = user == "<root_account>"
        mfa = _truthy(row.get("mfa_active"))
        pw_enabled = _truthy(row.get("password_enabled"))

        if is_root:
            if not mfa:
                out.append(make_domain_finding(
                    rule_id=IAM_ROOT_NO_MFA, severity="critical",
                    finding_summary="Root account does not have MFA enabled",
                    account_id=account_id, entity_type="root", entity_id="root", entity_name="root",
                    source=_SOURCE))
            if _truthy(row.get("access_key_1_active")) or _truthy(row.get("access_key_2_active")):
                out.append(make_domain_finding(
                    rule_id=IAM_ROOT_HAS_KEYS, severity="critical",
                    finding_summary="Root account has active access keys",
                    account_id=account_id, entity_type="root", entity_id="root", entity_name="root",
                    source=_SOURCE))
            continue

        if pw_enabled and not mfa:
            out.append(make_domain_finding(
                rule_id=IAM_USER_NO_MFA, severity="high",
                finding_summary=f"IAM user '{user}' has console access without MFA",
                account_id=account_id, entity_type="user", entity_id=user, entity_name=user,
                source=_SOURCE))

        # Unused console credential (password enabled, no recent use).
        pw_last = _days_since(row.get("password_last_used", ""))
        if pw_enabled and (pw_last is None or pw_last > _STALE_DAYS):
            out.append(make_domain_finding(
                rule_id=IAM_UNUSED_CREDENTIAL, severity="medium",
                finding_summary=f"IAM user '{user}' console password unused for >{_STALE_DAYS} days",
                account_id=account_id, entity_type="user", entity_id=user, entity_name=user,
                source=_SOURCE, details={"days_since_use": pw_last}))

        # Stale / unused access keys.
        for n in ("1", "2"):
            if not _truthy(row.get(f"access_key_{n}_active")):
                continue
            rotated = _days_since(row.get(f"access_key_{n}_last_rotated", ""))
            used = _days_since(row.get(f"access_key_{n}_last_used_date", ""))
            if rotated is not None and rotated > _STALE_DAYS:
                out.append(make_domain_finding(
                    rule_id=IAM_STALE_ACCESS_KEY, severity="medium",
                    finding_summary=f"IAM user '{user}' access key {n} not rotated for >{_STALE_DAYS} days",
                    account_id=account_id, entity_type="user", entity_id=user, entity_name=user,
                    source=_SOURCE, details={"key": n, "days_since_rotation": rotated}))
            elif used is not None and used > _STALE_DAYS:
                out.append(make_domain_finding(
                    rule_id=IAM_UNUSED_CREDENTIAL, severity="medium",
                    finding_summary=f"IAM user '{user}' access key {n} unused for >{_STALE_DAYS} days",
                    account_id=account_id, entity_type="user", entity_id=user, entity_name=user,
                    source=_SOURCE, details={"key": n, "days_since_use": used}))
    return out


def analyze_authorization_details(aad: dict, account_id: str) -> list[dict]:
    out = []

    def _admin_or_wildcard(entity_type, name, attached, inline):
        for p in attached or []:
            if p.get("PolicyName") == "AdministratorAccess":
                out.append(make_domain_finding(
                    rule_id=IAM_ADMIN_ACCESS, severity="high",
                    finding_summary=f"IAM {entity_type} '{name}' has AdministratorAccess attached",
                    account_id=account_id, entity_type=entity_type, entity_id=name, entity_name=name,
                    source=_SOURCE))
        for ip in inline or []:
            doc = _policy_doc(ip.get("PolicyDocument"))
            if any(_stmt_is_wildcard(s) for s in _as_list(doc.get("Statement"))):
                out.append(make_domain_finding(
                    rule_id=IAM_WILDCARD_POLICY, severity="high",
                    finding_summary=f"IAM {entity_type} '{name}' has an inline policy granting *:*",
                    account_id=account_id, entity_type=entity_type, entity_id=name, entity_name=name,
                    source=_SOURCE))

    for u in aad.get("UserDetailList", []) or []:
        _admin_or_wildcard("user", u.get("UserName", ""), u.get("AttachedManagedPolicies"), u.get("UserPolicyList"))
    for r in aad.get("RoleDetailList", []) or []:
        name = r.get("RoleName", "")
        _admin_or_wildcard("role", name, r.get("AttachedManagedPolicies"), r.get("RolePolicyList"))
        # Cross-account / wildcard trust.
        trust = _policy_doc(r.get("AssumeRolePolicyDocument"))
        for stmt in _as_list(trust.get("Statement")):
            if stmt.get("Effect") != "Allow":
                continue
            principals = stmt.get("Principal", {})
            aws_principals = _as_list(principals.get("AWS") if isinstance(principals, dict) else principals)
            has_condition = bool(stmt.get("Condition"))
            risky = any(
                p == "*" or (account_id and f":{account_id}:" not in str(p) and str(p).startswith("arn:aws:iam::"))
                for p in aws_principals
            )
            if risky and not has_condition:
                out.append(make_domain_finding(
                    rule_id=IAM_CROSS_ACCOUNT_TRUST_WILDCARD, severity="high",
                    finding_summary=f"IAM role '{name}' trusts an external/wildcard principal without a condition",
                    account_id=account_id, entity_type="role", entity_id=name, entity_name=name,
                    source=_SOURCE, details={"principals": aws_principals}))

            # GitHub Actions OIDC trust without a repo/sub restriction.
            federated = _as_list(principals.get("Federated") if isinstance(principals, dict) else None)
            if any("token.actions.githubusercontent.com" in str(p) for p in federated):
                if not _github_sub_restricted(stmt.get("Condition") or {}):
                    out.append(make_domain_finding(
                        rule_id=IAM_GITHUB_OIDC_TRUST_WILDCARD, severity="high",
                        finding_summary=f"IAM role '{name}' trusts GitHub Actions OIDC without a repo/sub condition",
                        account_id=account_id, entity_type="role", entity_id=name, entity_name=name,
                        source=_SOURCE))

    # Customer-managed (local) policies that grant *:*.
    for pol in aad.get("Policies", []) or []:
        default = pol.get("DefaultVersionId")
        for ver in pol.get("PolicyVersionList", []) or []:
            if ver.get("IsDefaultVersion") or ver.get("VersionId") == default:
                doc = _policy_doc(ver.get("Document"))
                if any(_stmt_is_wildcard(s) for s in _as_list(doc.get("Statement"))):
                    pname = pol.get("PolicyName", "")
                    out.append(make_domain_finding(
                        rule_id=IAM_WILDCARD_POLICY, severity="high",
                        finding_summary=f"Customer-managed policy '{pname}' grants Action:* on Resource:*",
                        account_id=account_id, entity_type="policy", entity_id=pname, entity_name=pname,
                        source=_SOURCE))
    return out


def analyze_password_policy(policy: dict | None, account_id: str) -> list[dict]:
    weak = policy is None or int((policy or {}).get("MinimumPasswordLength", 0) or 0) < 14
    if not weak:
        return []
    reason = "no account password policy is set" if policy is None else "minimum length < 14"
    return [make_domain_finding(
        rule_id=IAM_NO_PASSWORD_POLICY, severity="medium",
        finding_summary=f"Account password policy is weak ({reason})",
        account_id=account_id, entity_type="account", entity_id=account_id, entity_name="password_policy",
        source=_SOURCE)]


# ── boto3 collect ──────────────────────────────────────────────────────────────

def _credential_report_rows(iam) -> list[dict]:
    # Generate then fetch; the report may be a moment behind.
    for _ in range(4):
        with suppress(Exception):
            iam.generate_credential_report()
        try:
            content = iam.get_credential_report()["Content"]
            return list(csv.DictReader(io.StringIO(content.decode("utf-8"))))
        except Exception:
            time.sleep(2)
    return []


def collect(session, account_id: str, account_name: str, region: str, regions=None) -> list[dict]:
    iam = session.client("iam")
    findings: list[dict] = []

    findings += analyze_credential_report(_credential_report_rows(iam), account_id)

    aad: dict = {}
    paginator = iam.get_paginator("get_account_authorization_details")
    for page in paginator.paginate():
        for key in ("UserDetailList", "RoleDetailList", "Policies", "GroupDetailList"):
            aad.setdefault(key, []).extend(page.get(key, []))
    findings += analyze_authorization_details(aad, account_id)

    try:
        policy = iam.get_account_password_policy().get("PasswordPolicy")
    except Exception:
        policy = None
    findings += analyze_password_policy(policy, account_id)

    return findings
