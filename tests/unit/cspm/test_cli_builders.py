"""Unit — CLI command templates: right tool, single line, grounded in the finding."""

from __future__ import annotations

import pytest

from tests.unit.cspm.conftest import sg_finding


def _assert_command(cmd, tool, *must_contain):
    assert cmd.startswith(f"{tool} ")
    assert "\n" not in cmd
    for needle in must_contain:
        assert needle in cmd, f"{needle!r} missing from {cmd!r}"


# ── AWS (sg_audit) ────────────────────────────────────────────────────────────

def test_every_aws_builder_is_grounded():
    from sg_audit.cli_commands import CLI_BUILDERS

    samples = {
        "SG_ADMIN_WORLD_INGRESS": sg_finding(),
        "SG_DB_WORLD_INGRESS": sg_finding("SG_DB_WORLD_INGRESS", from_port=3306, to_port=3306),
        "SG_CACHE_WORLD_INGRESS": sg_finding("SG_CACHE_WORLD_INGRESS", from_port=6379, to_port=6379),
        "SG_ALL_PORTS_WORLD": sg_finding("SG_ALL_PORTS_WORLD", protocol="all",
                                         from_port=0, to_port=65535),
        "SG_SENSITIVE_NON_ADMIN_WORLD": sg_finding("SG_SENSITIVE_NON_ADMIN_WORLD",
                                                   from_port=9200, to_port=9200),
        "S3_PUBLIC_ACL": sg_finding("S3_PUBLIC_ACL", entity_type="s3_bucket",
                                    entity_id="my-bucket-1", group_id=""),
        "S3_PUBLIC_POLICY": sg_finding("S3_PUBLIC_POLICY", entity_type="s3_bucket",
                                       entity_id="my-bucket-1", group_id=""),
        "S3_NO_PUBLIC_ACCESS_BLOCK": sg_finding("S3_NO_PUBLIC_ACCESS_BLOCK",
                                                entity_type="s3_bucket",
                                                entity_id="my-bucket-1", group_id=""),
        "CICD_TFSTATE_PUBLIC": sg_finding("CICD_TFSTATE_PUBLIC", entity_type="s3_bucket",
                                          entity_id="tfstate-bucket-1", group_id=""),
        "S3_NO_ENCRYPTION": sg_finding("S3_NO_ENCRYPTION", entity_type="s3_bucket",
                                       entity_id="my-bucket-1", group_id=""),
        "EC2_IMDSV1_ENABLED": sg_finding("EC2_IMDSV1_ENABLED", entity_type="ec2_instance",
                                         entity_id="i-007759a63defdc275", group_id=""),
        "RDS_PUBLIC": sg_finding("RDS_PUBLIC", entity_type="rds_instance",
                                 entity_id="prod-db-1", group_id=""),
        "RDS_SNAPSHOT_PUBLIC": sg_finding("RDS_SNAPSHOT_PUBLIC", entity_type="rds_snapshot",
                                          entity_id="snap-1a2b3c4d", group_id=""),
    }
    assert set(samples) == set(CLI_BUILDERS), "every registered rule must have a sample"
    for rule_id, finding in samples.items():
        cmds = CLI_BUILDERS[rule_id](finding)
        assert cmds, rule_id
        eid = finding["supporting_details"]["entity_id"]
        for cmd in cmds:
            _assert_command(cmd, "aws", eid)


def test_sg_revoke_embeds_rule_parameters():
    from sg_audit.cli_commands import CLI_BUILDERS

    cmd = CLI_BUILDERS["SG_ADMIN_WORLD_INGRESS"](sg_finding())[0]
    _assert_command(cmd, "aws", "revoke-security-group-ingress",
                    "sg-0123456789abcdef0", "--protocol tcp", "--port 22",
                    "--cidr 0.0.0.0/0", "--region ca-central-1")


def test_sg_revoke_all_protocols_omits_port():
    from sg_audit.cli_commands import CLI_BUILDERS

    cmd = CLI_BUILDERS["SG_ALL_PORTS_WORLD"](
        sg_finding("SG_ALL_PORTS_WORLD", protocol="all", from_port=0, to_port=65535))[0]
    assert "--protocol all" in cmd
    assert "--port" not in cmd


def test_builder_returns_empty_without_identifiers():
    from sg_audit.cli_commands import CLI_BUILDERS

    bare = sg_finding("S3_PUBLIC_ACL", entity_type="s3_bucket", entity_id="", group_id="")
    bare["supporting_details"]["entity_id"] = ""
    assert CLI_BUILDERS["S3_PUBLIC_ACL"](bare) == []


def test_uncovered_rule_has_no_builder():
    from sg_audit.cli_commands import CLI_BUILDERS

    assert "IAM_ROOT_NO_MFA" not in CLI_BUILDERS
    assert CLI_BUILDERS.get("LOG_NO_CLOUDTRAIL") is None


# ── Azure ─────────────────────────────────────────────────────────────────────

def _azure_storage_finding(rule_id):
    return {"rule_id": rule_id, "supporting_details": {
        "scope_id": "sub-123", "scope_name": "Prod", "region": "eastus",
        "entity_type": "storage_account", "entity_name": "prodstore1",
        "entity_id": "/subscriptions/sub-123/resourceGroups/rg-data/providers/"
                     "Microsoft.Storage/storageAccounts/prodstore1"}}


@pytest.mark.parametrize("rule_id,flag", [
    ("AZ_STORAGE_PUBLIC_BLOB", "--allow-blob-public-access false"),
    ("AZ_STORAGE_NO_HTTPS", "--https-only true"),
    ("AZ_STORAGE_PUBLIC_NETWORK", "--default-action Deny"),
])
def test_azure_builders(rule_id, flag):
    from azure_audit.cli_commands import CLI_BUILDERS

    cmd = CLI_BUILDERS[rule_id](_azure_storage_finding(rule_id))[0]
    _assert_command(cmd, "az", "--name prodstore1", "--resource-group rg-data",
                    "--subscription sub-123", flag)


# ── GCP ───────────────────────────────────────────────────────────────────────

def test_gcp_bucket_builder():
    from gcp_audit.cli_commands import CLI_BUILDERS

    f = {"rule_id": "GCP_BUCKET_PUBLIC", "supporting_details": {
        "scope_id": "proj-1", "entity_type": "gcs_bucket",
        "entity_id": "b-1", "entity_name": "public-bucket-1"}}
    cmd = CLI_BUILDERS["GCP_BUCKET_PUBLIC"](f)[0]
    _assert_command(cmd, "gcloud", "gs://public-bucket-1", "--public-access-prevention")


@pytest.mark.parametrize("rule_id", ["GCP_FW_ADMIN_WORLD_OPEN", "GCP_FW_DB_WORLD_OPEN",
                                     "GCP_FW_ALL_PORTS_WORLD"])
def test_gcp_firewall_builders(rule_id):
    from gcp_audit.cli_commands import CLI_BUILDERS

    f = {"rule_id": rule_id, "supporting_details": {
        "scope_id": "proj-1", "entity_type": "firewall",
        "entity_id": "fw-123", "entity_name": "default-allow-ssh"}}
    cmd = CLI_BUILDERS[rule_id](f)[0]
    _assert_command(cmd, "gcloud", "firewall-rules update default-allow-ssh",
                    "--disabled", "--project proj-1")
