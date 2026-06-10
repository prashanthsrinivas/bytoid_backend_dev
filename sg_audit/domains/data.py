"""Data & Storage domain collector (account-scoped, sweeps regions for RDS/Secrets).

S3 is a global namespace (collected once); RDS instances/snapshots and Secrets
Manager secrets are regional and swept across the enabled regions. Findings:
public buckets/policies, missing Block Public Access, unencrypted storage,
publicly accessible / unencrypted RDS, public snapshots, secrets without rotation.
"""

from __future__ import annotations

from contextlib import suppress

from sg_audit.analysis.normalize import make_domain_finding
from sg_audit.metadata import (
    RDS_PUBLIC,
    RDS_SNAPSHOT_PUBLIC,
    RDS_UNENCRYPTED,
    S3_NO_ENCRYPTION,
    S3_NO_PUBLIC_ACCESS_BLOCK,
    S3_PUBLIC_ACL,
    S3_PUBLIC_POLICY,
    SECRET_NO_ROTATION,
)
from sg_audit.schema import DOMAIN_DATA

DOMAIN = DOMAIN_DATA
SCOPE = "account"
_SOURCE = "s3/rds/secretsmanager"

_PUBLIC_ACL_URIS = (
    "http://acs.amazonaws.com/groups/global/AllUsers",
    "http://acs.amazonaws.com/groups/global/AuthenticatedUsers",
)


# ── pure analyzers ────────────────────────────────────────────────────────────

def analyze_bucket(account_id, bucket, region, acl, pab, policy_status, encryption) -> list[dict]:
    """One bucket's facts -> findings. ``acl``/``pab``/etc. are the raw AWS dicts
    (or None when the call failed/was denied)."""
    out = []

    def f(rule_id, severity, summary, details=None):
        return make_domain_finding(
            rule_id=rule_id, severity=severity, finding_summary=summary,
            account_id=account_id, region=region or "",
            entity_type="s3_bucket", entity_id=bucket, entity_name=bucket,
            source=_SOURCE, details=details or {})

    if acl is not None:
        for grant in acl.get("Grants", []) or []:
            uri = (grant.get("Grantee", {}) or {}).get("URI", "")
            if uri in _PUBLIC_ACL_URIS:
                out.append(f(S3_PUBLIC_ACL, "critical",
                            f"S3 bucket '{bucket}' ACL grants public access ({grant.get('Permission')})",
                            {"grantee": uri}))
                break
    if policy_status is not None and (policy_status.get("PolicyStatus", {}) or {}).get("IsPublic"):
        out.append(f(S3_PUBLIC_POLICY, "critical", f"S3 bucket '{bucket}' policy allows public access"))

    cfg = (pab or {}).get("PublicAccessBlockConfiguration") if pab is not None else None
    if pab is None or not cfg or not all([
        cfg.get("BlockPublicAcls"), cfg.get("IgnorePublicAcls"),
        cfg.get("BlockPublicPolicy"), cfg.get("RestrictPublicBuckets"),
    ]):
        out.append(f(S3_NO_PUBLIC_ACCESS_BLOCK, "high",
                    f"S3 bucket '{bucket}' does not have full Block Public Access enabled"))

    if encryption is None:
        out.append(f(S3_NO_ENCRYPTION, "medium", f"S3 bucket '{bucket}' has no default encryption"))
    return out


def analyze_rds_instance(account_id, region, db) -> list[dict]:
    out = []
    name = db.get("DBInstanceIdentifier", "")

    def f(rule_id, severity, summary):
        return make_domain_finding(
            rule_id=rule_id, severity=severity, finding_summary=summary,
            account_id=account_id, region=region, entity_type="rds_instance",
            entity_id=name, entity_name=name, source=_SOURCE)

    if db.get("PubliclyAccessible"):
        out.append(f(RDS_PUBLIC, "critical", f"RDS instance '{name}' is publicly accessible"))
    if not db.get("StorageEncrypted"):
        out.append(f(RDS_UNENCRYPTED, "high", f"RDS instance '{name}' storage is not encrypted"))
    return out


def analyze_rds_snapshot_public(account_id, region, snapshot_id, attrs) -> list[dict]:
    for a in (attrs or {}).get("DBSnapshotAttributes", []) or []:
        if a.get("AttributeName") == "restore" and "all" in (a.get("AttributeValues") or []):
            return [make_domain_finding(
                rule_id=RDS_SNAPSHOT_PUBLIC, severity="critical",
                finding_summary=f"RDS snapshot '{snapshot_id}' is publicly shared",
                account_id=account_id, region=region, entity_type="rds_snapshot",
                entity_id=snapshot_id, entity_name=snapshot_id, source=_SOURCE)]
    return []


def analyze_secret(account_id, region, secret) -> list[dict]:
    if secret.get("RotationEnabled"):
        return []
    name = secret.get("Name", secret.get("ARN", ""))
    return [make_domain_finding(
        rule_id=SECRET_NO_ROTATION, severity="low",
        finding_summary=f"Secret '{name}' does not have rotation enabled",
        account_id=account_id, region=region, entity_type="secret",
        entity_id=name, entity_name=name, source=_SOURCE)]


# ── boto3 collect ──────────────────────────────────────────────────────────────

def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _collect_s3(session, account_id) -> list[dict]:
    s3 = session.client("s3")
    out = []
    try:
        buckets = s3.list_buckets().get("Buckets", [])
    except Exception:
        return out
    for b in buckets:
        name = b.get("Name")
        try:
            loc = s3.get_bucket_location(Bucket=name).get("LocationConstraint") or "us-east-1"
        except Exception:
            loc = ""
        acl = _safe(lambda n=name: s3.get_bucket_acl(Bucket=n))
        pab = _safe(lambda n=name: s3.get_public_access_block(Bucket=n))
        pol_status = _safe(lambda n=name: s3.get_bucket_policy_status(Bucket=n))
        enc = _safe(lambda n=name: s3.get_bucket_encryption(Bucket=n))
        out += analyze_bucket(account_id, name, loc, acl, pab, pol_status, enc)
    return out


def _collect_rds_secrets(session, account_id, regions) -> list[dict]:
    out = []
    for region in regions or []:
        rds = session.client("rds", region_name=region)
        with suppress(Exception):
            for page in rds.get_paginator("describe_db_instances").paginate():
                for db in page.get("DBInstances", []):
                    out += analyze_rds_instance(account_id, region, db)
        with suppress(Exception):
            for page in rds.get_paginator("describe_db_snapshots").paginate(SnapshotType="manual"):
                for snap in page.get("DBSnapshots", []):
                    sid = snap.get("DBSnapshotIdentifier")
                    attrs = _safe(lambda s=sid, c=rds: c.describe_db_snapshot_attributes(
                        DBSnapshotIdentifier=s).get("DBSnapshotAttributesResult"))
                    out += analyze_rds_snapshot_public(account_id, region, sid, attrs)
        sm = session.client("secretsmanager", region_name=region)
        with suppress(Exception):
            for page in sm.get_paginator("list_secrets").paginate():
                for secret in page.get("SecretList", []):
                    out += analyze_secret(account_id, region, secret)
    return out


def collect(session, account_id: str, account_name: str, region: str, regions=None) -> list[dict]:
    regions = regions or [region]
    return _collect_s3(session, account_id) + _collect_rds_secrets(session, account_id, regions)
