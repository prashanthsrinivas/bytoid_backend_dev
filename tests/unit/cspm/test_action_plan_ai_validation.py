"""Unit — the code-owned gate for AI-drafted CLI commands."""

from __future__ import annotations

import pytest

from cspm_core.action_plan import validate_draft_command

BLOB = ("394711685916 ca-central-1 sg-0123456789abcdef0 test-sg vpc-00000001 "
        "0.0.0.0/0 tcp 22 i-007759a63defdc275 my-bucket-1")


def test_accepts_grounded_single_command():
    assert validate_draft_command(
        "aws ec2 revoke-security-group-ingress --group-id sg-0123456789abcdef0 "
        "--protocol tcp --port 22 --cidr 0.0.0.0/0 --region ca-central-1",
        "aws", BLOB)


@pytest.mark.parametrize("cmd", [
    "az storage account update --name x",          # wrong tool
    "ec2 revoke-security-group-ingress",           # missing tool prefix
    "aws ec2 describe-instances; rm -rf /",        # chaining
    "aws s3 ls | grep secret",                     # pipe
    "aws s3 cp s3://b file > out.txt",             # redirect
    "aws ssm send-command `whoami`",               # backticks
    "aws ssm send-command $(whoami)",              # subshell
    "aws ec2 describe-instances\naws s3 ls",       # multi-line
    "",                                            # empty
    None,                                          # not a string
    "aws " + "x" * 600,                            # too long
])
def test_rejects_malformed_or_dangerous(cmd):
    assert not validate_draft_command(cmd, "aws", BLOB)


def test_rejects_foreign_resource_ids():
    assert not validate_draft_command(
        "aws ec2 revoke-security-group-ingress --group-id sg-9999999999attacker",
        "aws", BLOB)


def test_allows_ids_from_findings_only():
    ok = "aws ec2 modify-instance-metadata-options --instance-id i-007759a63defdc275 --http-tokens required"
    assert validate_draft_command(ok, "aws", BLOB)
    assert not validate_draft_command(ok.replace("i-007759a63defdc275", "i-deadbeef99999999"),
                                      "aws", BLOB)


def test_flags_are_not_id_checked():
    # flag tokens (starting with "-") may contain digits without being grounded
    assert validate_draft_command("aws s3api put-public-access-block --bucket my-bucket-1",
                                  "aws", BLOB)


def test_gcloud_and_az_prefixes_respected():
    assert validate_draft_command("gcloud compute firewall-rules update fw --disabled",
                                  "gcloud", "fw")
    assert not validate_draft_command("gcloud compute firewall-rules update fw --disabled",
                                      "az", "fw")
