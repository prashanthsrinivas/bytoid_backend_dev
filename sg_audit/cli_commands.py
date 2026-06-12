"""AWS CLI command templates for action points (display/copy only — NEVER executed).

One builder per fixer-backed rule (mirrors sg_audit/autoremediate.py FIXERS).
Each builder takes one finding and returns a list of single-line `aws …`
commands grounded entirely in the finding's supporting_details. Anything not
covered here is advisory-only in the action plan (manual steps, or a validated
AI-drafted command).
"""

from __future__ import annotations


def _region_flag(sd: dict) -> str:
    region = sd.get("region", "")
    return f" --region {region}" if region else ""


def _sg_revoke(finding) -> list:
    sd = finding.get("supporting_details", {}) or {}
    gid, cidr = sd.get("group_id", ""), sd.get("cidr", "")
    if not gid or not cidr:
        return []
    proto = str(sd.get("protocol") or "all")
    cmd = f"aws ec2 revoke-security-group-ingress --group-id {gid}"
    if proto in ("all", "-1"):
        cmd += " --protocol all"
    else:
        cmd += f" --protocol {proto}"
        fp, tp = sd.get("from_port"), sd.get("to_port")
        if fp is not None:
            cmd += f" --port {fp}" if (tp is None or tp == fp) else f" --port {fp}-{tp}"
    return [cmd + f" --cidr {cidr}" + _region_flag(sd)]


def _s3_block_public(finding) -> list:
    sd = finding.get("supporting_details", {}) or {}
    bucket = sd.get("entity_id", "")
    if not bucket:
        return []
    return [f"aws s3api put-public-access-block --bucket {bucket} "
            "--public-access-block-configuration "
            "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"]


def _s3_default_encryption(finding) -> list:
    sd = finding.get("supporting_details", {}) or {}
    bucket = sd.get("entity_id", "")
    if not bucket:
        return []
    return [f"aws s3api put-bucket-encryption --bucket {bucket} "
            "--server-side-encryption-configuration "
            "'{\"Rules\":[{\"ApplyServerSideEncryptionByDefault\":{\"SSEAlgorithm\":\"AES256\"}}]}'"]


def _ec2_imdsv2(finding) -> list:
    sd = finding.get("supporting_details", {}) or {}
    iid = sd.get("entity_id", "")
    if not iid:
        return []
    return [f"aws ec2 modify-instance-metadata-options --instance-id {iid} "
            f"--http-tokens required --http-endpoint enabled{_region_flag(sd)}"]


def _rds_private(finding) -> list:
    sd = finding.get("supporting_details", {}) or {}
    db = sd.get("entity_id", "")
    if not db:
        return []
    return [f"aws rds modify-db-instance --db-instance-identifier {db} "
            f"--no-publicly-accessible --apply-immediately{_region_flag(sd)}"]


def _rds_snapshot_private(finding) -> list:
    sd = finding.get("supporting_details", {}) or {}
    snap = sd.get("entity_id", "")
    if not snap:
        return []
    return [f"aws rds modify-db-snapshot-attribute --db-snapshot-identifier {snap} "
            f"--attribute-name restore --values-to-remove all{_region_flag(sd)}"]


CLI_BUILDERS = {
    "SG_ADMIN_WORLD_INGRESS": _sg_revoke,
    "SG_DB_WORLD_INGRESS": _sg_revoke,
    "SG_CACHE_WORLD_INGRESS": _sg_revoke,
    "SG_ALL_PORTS_WORLD": _sg_revoke,
    "SG_SENSITIVE_NON_ADMIN_WORLD": _sg_revoke,
    "S3_PUBLIC_ACL": _s3_block_public,
    "S3_PUBLIC_POLICY": _s3_block_public,
    "S3_NO_PUBLIC_ACCESS_BLOCK": _s3_block_public,
    "CICD_TFSTATE_PUBLIC": _s3_block_public,
    "S3_NO_ENCRYPTION": _s3_default_encryption,
    "EC2_IMDSV1_ENABLED": _ec2_imdsv2,
    "RDS_PUBLIC": _rds_private,
    "RDS_SNAPSHOT_PUBLIC": _rds_snapshot_private,
}
