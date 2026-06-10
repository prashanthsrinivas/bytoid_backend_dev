"""Cross-account Security Group collection (runs inside the Lambda).

Pure boto3 + the dependency-light ``sg_audit.analysis`` engine — no app/DB/KMS
imports. Per-account and per-region work is isolated in try/except so a single
account or region failure lands in ``collector_status`` and degrades to a partial
snapshot; it never aborts the whole audit.
"""

from __future__ import annotations

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from sg_audit.analysis.normalize import build_snapshot
from sg_audit.analysis.rules import analyze_account_region

# Adaptive retries soak up EC2 Describe throttling when fanning across regions.
_BOTO_CFG = Config(retries={"max_attempts": 5, "mode": "adaptive"})

_ROLE_SESSION_NAME = "bytoid-sg-audit"


def _base_session(base_credentials: dict):
    return boto3.session.Session(
        aws_access_key_id=base_credentials.get("access_key_id"),
        aws_secret_access_key=base_credentials.get("secret_access_key"),
        aws_session_token=base_credentials.get("session_token"),
        region_name=base_credentials.get("region") or "us-east-1",
    )


def _discover_accounts(session) -> list[dict]:
    """List ACTIVE accounts in the org via organizations:ListAccounts (paginated)."""
    org = session.client("organizations", config=_BOTO_CFG)
    accounts = []
    paginator = org.get_paginator("list_accounts")
    for page in paginator.paginate():
        for acct in page.get("Accounts", []):
            if acct.get("Status") == "ACTIVE":
                accounts.append({"id": acct.get("Id", ""), "name": acct.get("Name", "")})
    return accounts


def _assume(session, account_id: str, role_name: str, external_id: str):
    """Assume the member-account audit role; returns a scoped boto3 Session."""
    sts = session.client("sts", config=_BOTO_CFG)
    role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
    kwargs = {"RoleArn": role_arn, "RoleSessionName": _ROLE_SESSION_NAME}
    if external_id:
        kwargs["ExternalId"] = external_id
    resp = sts.assume_role(**kwargs)
    c = resp["Credentials"]
    return boto3.session.Session(
        aws_access_key_id=c["AccessKeyId"],
        aws_secret_access_key=c["SecretAccessKey"],
        aws_session_token=c["SessionToken"],
    )


def _enabled_regions(session, default_region: str) -> list[str]:
    try:
        ec2 = session.client("ec2", region_name=default_region, config=_BOTO_CFG)
        resp = ec2.describe_regions(AllRegions=False)
        return [r["RegionName"] for r in resp.get("Regions", [])]
    except Exception:
        # Fall back to the single base region rather than nothing.
        return [default_region]


def _describe_security_groups(session, region: str) -> list[dict]:
    ec2 = session.client("ec2", region_name=region, config=_BOTO_CFG)
    out = []
    paginator = ec2.get_paginator("describe_security_groups")
    for page in paginator.paginate():
        out.extend(page.get("SecurityGroups", []))
    return out


def _describe_eni_usage(session, region: str) -> dict | None:
    """group_id -> attachment count, or None if ENI data can't be fetched."""
    try:
        ec2 = session.client("ec2", region_name=region, config=_BOTO_CFG)
        usage: dict[str, int] = {}
        paginator = ec2.get_paginator("describe_network_interfaces")
        for page in paginator.paginate():
            for eni in page.get("NetworkInterfaces", []):
                for grp in eni.get("Groups", []):
                    gid = grp.get("GroupId")
                    if gid:
                        usage[gid] = usage.get(gid, 0) + 1
        return usage
    except Exception:
        return None


def _err(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return f"error: {exc.response.get('Error', {}).get('Code', 'ClientError')}"
    return f"error: {type(exc).__name__}"


def run_collection(
    *,
    scan_id: str,
    audit_id: str,
    scope: dict,
    external_id: str,
    base_credentials: dict,
    management_account_id: str = "",
) -> dict:
    """Run a full cross-account SG audit and return the (unsigned) snapshot.

    Never raises: every failure mode is captured in ``collector_status`` and the
    snapshot is still produced (possibly with zero findings).
    """
    collector_status: dict[str, str] = {}
    findings: list[dict] = []
    accounts_scanned: list[str] = []

    scope = scope or {}
    role_name = scope.get("role_name") or "BytoidSecurityAuditRole"
    regions_filter = scope.get("regions") or []
    base_region = base_credentials.get("region") or "us-east-1"

    session = _base_session(base_credentials)

    # --- Resolve target accounts --------------------------------------------
    name_by_id: dict[str, str] = {}
    targets: list[str] = list(scope.get("account_ids") or [])
    if not targets and scope.get("discover"):
        try:
            discovered = _discover_accounts(session)
            targets = [a["id"] for a in discovered if a["id"]]
            name_by_id = {a["id"]: a["name"] for a in discovered}
        except Exception as exc:
            collector_status["_discovery"] = _err(exc)

    if not targets:
        if "_discovery" not in collector_status:
            collector_status["_discovery"] = "error: no_accounts"
        snapshot = build_snapshot(
            scan_id=scan_id, audit_id=audit_id, findings=[],
            accounts_scanned=[], collector_status=collector_status, scope=scope,
        )
        snapshot["fatal"] = True
        return snapshot

    # --- Per-account fan-out -------------------------------------------------
    for account_id in targets:
        try:
            acct_session = _assume(session, account_id, role_name, external_id)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("ExpiredToken", "ExpiredTokenException"):
                # Base credentials expired mid-run: nothing else will succeed.
                collector_status["_base"] = "error: expired_token"
                break
            collector_status[account_id] = _err(exc)
            continue
        except Exception as exc:
            collector_status[account_id] = _err(exc)
            continue

        regions = regions_filter or _enabled_regions(acct_session, base_region)
        account_name = name_by_id.get(account_id, "")
        account_ok = False

        for region in regions:
            try:
                sgs = _describe_security_groups(acct_session, region)
                eni_usage = _describe_eni_usage(acct_session, region)
                findings += analyze_account_region(
                    account_id=account_id,
                    account_name=account_name,
                    region=region,
                    security_groups=sgs,
                    eni_sg_usage=eni_usage,
                )
                collector_status[f"{account_id}:{region}"] = "ok"
                account_ok = True
            except Exception as exc:
                collector_status[f"{account_id}:{region}"] = _err(exc)

        collector_status[account_id] = "ok" if account_ok else "error: no_regions"
        if account_ok:
            accounts_scanned.append(account_id)

    return build_snapshot(
        scan_id=scan_id,
        audit_id=audit_id,
        findings=findings,
        accounts_scanned=accounts_scanned,
        collector_status=collector_status,
        scope=scope,
    )
