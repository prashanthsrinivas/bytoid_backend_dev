"""Approval-gated auto-remediation executor (opt-in, dry-run by default).

SAFETY MODEL (all required to perform a real AWS write):
  1. ``SG_AUTO_REMEDIATE_ENABLED`` must be set (off by default).
  2. The finding must have a remediation workflow that reached/passed approval
     (``workflow_route`` state in {approval, published}).
  3. The caller must explicitly request ``dry_run=False`` (default is dry-run,
     which only returns the planned action).
  4. Writes use a SEPARATE assumed role (``SG_AUTO_REMEDIATE_ROLE_NAME``), never
     the read-only audit role.

Only a small set of reversible/idempotent fixers are supported; anything else
returns ``unsupported`` and stays manual.
"""

from __future__ import annotations

from datetime import datetime, timezone

from utils.base_logger import get_logger
from sg_audit import config as sg_config
from sg_audit.service import SgAuditService

logger = get_logger(__name__)
_APPROVED_STATES = {"approval", "published"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── fixers: (session, finding, dry_run) -> (action_text, performed) ──────────

def _fix_s3_block_public_access(session, finding, dry_run):
    d = finding.get("supporting_details", {})
    bucket = d.get("entity_id")
    action = f"Enable S3 Block Public Access (all four settings) on bucket '{bucket}'"
    if dry_run:
        return action, False
    s3 = session.client("s3")
    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True,
            "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
        },
    )
    return action, True


def _fix_ec2_imdsv2(session, finding, dry_run):
    d = finding.get("supporting_details", {})
    iid, region = d.get("entity_id"), d.get("region")
    action = f"Require IMDSv2 (HttpTokens=required) on instance '{iid}'"
    if dry_run:
        return action, False
    ec2 = session.client("ec2", region_name=region)
    ec2.modify_instance_metadata_options(InstanceId=iid, HttpTokens="required", HttpEndpoint="enabled")
    return action, True


def _fix_sg_revoke_world(session, finding, dry_run):
    d = finding.get("supporting_details", {})
    gid = d.get("group_id") or d.get("entity_id")
    region, cidr = d.get("region"), d.get("cidr")
    proto = {"all": "-1", "tcp": "tcp", "udp": "udp"}.get(str(d.get("protocol")), str(d.get("protocol") or "-1"))
    action = f"Revoke ingress {cidr} {proto}:{d.get('from_port')}-{d.get('to_port')} on security group '{gid}'"
    if dry_run:
        return action, False
    perm = {"IpProtocol": proto}
    if d.get("from_port") is not None:
        perm["FromPort"] = int(d["from_port"])
        perm["ToPort"] = int(d["to_port"])
    if ":" in str(cidr):
        perm["Ipv6Ranges"] = [{"CidrIpv6": cidr}]
    else:
        perm["IpRanges"] = [{"CidrIp": cidr}]
    ec2 = session.client("ec2", region_name=region)
    ec2.revoke_security_group_ingress(GroupId=gid, IpPermissions=[perm])
    return action, True


def _fix_rds_snapshot_private(session, finding, dry_run):
    d = finding.get("supporting_details", {})
    sid, region = d.get("entity_id"), d.get("region")
    action = f"Remove public 'all' restore attribute from RDS snapshot '{sid}'"
    if dry_run:
        return action, False
    rds = session.client("rds", region_name=region)
    rds.modify_db_snapshot_attribute(DBSnapshotIdentifier=sid, AttributeName="restore", ValuesToRemove=["all"])
    return action, True


def _fix_s3_default_encryption(session, finding, dry_run):
    d = finding.get("supporting_details", {})
    bucket = d.get("entity_id")
    action = f"Enable default SSE-S3 (AES256) encryption on bucket '{bucket}'"
    if dry_run:
        return action, False
    s3 = session.client("s3")
    s3.put_bucket_encryption(
        Bucket=bucket,
        ServerSideEncryptionConfiguration={"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]},
    )
    return action, True


def _fix_rds_private(session, finding, dry_run):
    d = finding.get("supporting_details", {})
    db_id, region = d.get("entity_id"), d.get("region")
    action = f"Disable public accessibility on RDS instance '{db_id}' (applied immediately)"
    if dry_run:
        return action, False
    rds = session.client("rds", region_name=region)
    rds.modify_db_instance(DBInstanceIdentifier=db_id, PubliclyAccessible=False, ApplyImmediately=True)
    return action, True


FIXERS = {
    "S3_PUBLIC_ACL": _fix_s3_block_public_access,
    "S3_PUBLIC_POLICY": _fix_s3_block_public_access,
    "S3_NO_PUBLIC_ACCESS_BLOCK": _fix_s3_block_public_access,
    "S3_NO_ENCRYPTION": _fix_s3_default_encryption,
    "CICD_TFSTATE_PUBLIC": _fix_s3_block_public_access,
    "EC2_IMDSV1_ENABLED": _fix_ec2_imdsv2,
    "SG_ADMIN_WORLD_INGRESS": _fix_sg_revoke_world,
    "SG_DB_WORLD_INGRESS": _fix_sg_revoke_world,
    "SG_CACHE_WORLD_INGRESS": _fix_sg_revoke_world,
    "SG_ALL_PORTS_WORLD": _fix_sg_revoke_world,
    "SG_SENSITIVE_NON_ADMIN_WORLD": _fix_sg_revoke_world,
    "RDS_SNAPSHOT_PUBLIC": _fix_rds_snapshot_private,
    "RDS_PUBLIC": _fix_rds_private,
}


def supported(rule_id: str) -> bool:
    return rule_id in FIXERS


def _is_approved(user_id, audit_id, finding_id, service) -> bool:
    link = service.storage.get_remediation_links(user_id, audit_id).get(finding_id)
    if not link or not link.get("workflow_id"):
        return False
    try:
        from workflow_route.state_machine import get_workflow

        return get_workflow(link["workflow_id"]).get("state") in _APPROVED_STATES
    except Exception:
        logger.debug("approval-state lookup failed", exc_info=True)
        return False


def _write_session(user_id, account_id):
    """Assume the SEPARATE write-scoped remediation role in the target account."""
    import boto3

    row = _base_creds(user_id)
    if not row:
        return None
    base = boto3.session.Session(
        aws_access_key_id=row["aws_access_key_id"],
        aws_secret_access_key=row["aws_secret_access_key"],
        aws_session_token=row.get("aws_session_token"),
        region_name=row.get("aws_region") or sg_config.AWS_REGION,
    )
    if account_id and account_id == row.get("aws_account_id"):
        return base  # connected account — use base creds directly
    sts = base.client("sts")
    arn = f"arn:aws:iam::{account_id}:role/{sg_config.SG_AUTO_REMEDIATE_ROLE_NAME}"
    creds = sts.assume_role(RoleArn=arn, RoleSessionName="bytoid-sg-remediate")["Credentials"]
    return boto3.session.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


def _base_creds(user_id):
    import pymysql

    from db.rds_db import connect_to_rds
    conn = None
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                """SELECT aws_access_key_id, aws_secret_access_key, aws_session_token,
                          aws_region, aws_account_id FROM aws_saml_sessions
                   WHERE user_id=%s AND expires_at > NOW() LIMIT 1""",
                (user_id,),
            )
            return cur.fetchone()
    except Exception:
        logger.warning("auto-remediate base creds lookup failed", exc_info=True)
        return None
    finally:
        if conn:
            conn.close()


def execute_remediation(user_id, audit_id, finding, *, dry_run=None, service=None) -> dict:
    """Execute (or plan) the fix for one finding. Never raises.

    Returns a status dict: disabled | unsupported | not_approved | planned |
    executed | error.
    """
    service = service or SgAuditService()
    rule_id = finding.get("rule_id", "")
    finding_id = finding.get("finding_id", "")
    if dry_run is None:
        dry_run = sg_config.SG_AUTO_REMEDIATE_DRY_RUN

    if not sg_config.auto_remediate_enabled():
        return {"status": "disabled", "reason": "auto-remediation is turned off"}
    fixer = FIXERS.get(rule_id)
    if not fixer:
        return {"status": "unsupported", "rule_id": rule_id}
    if not _is_approved(user_id, audit_id, finding_id, service):
        return {"status": "not_approved", "reason": "no approved remediation workflow for this finding"}

    account_id = finding.get("supporting_details", {}).get("account_id")
    try:
        if dry_run:
            action, _ = fixer(None, finding, True)
            return {"status": "planned", "dry_run": True, "action": action, "finding_id": finding_id}
        session = _write_session(user_id, account_id)
        if session is None:
            return {"status": "error", "message": "could not obtain write credentials"}
        action, performed = fixer(session, finding, False)
    except Exception as exc:
        logger.warning("auto-remediation failed for %s: %s", finding_id, exc, exc_info=True)
        return {"status": "error", "message": str(exc), "finding_id": finding_id}

    # Record execution on the remediation link (best-effort).
    try:
        links = service.storage.get_remediation_links(user_id, audit_id)
        link = links.get(finding_id, {"finding_id": finding_id})
        link.update({"executed_at": _now(), "executed_action": action, "executed": performed})
        service.storage.save_remediation_link(user_id, audit_id, finding_id, link)
    except Exception:
        logger.debug("failed to record execution", exc_info=True)

    logger.info("Auto-remediated %s: %s", finding_id, action)
    return {"status": "executed", "dry_run": False, "action": action, "finding_id": finding_id}
