"""CI/CD & DevOps domain (AWS-native, account-scoped).

Covers what is reachable with AWS credentials: CodeBuild projects with secrets in
plaintext environment variables or privileged mode, and publicly-exposed
Terraform state buckets (state holds secrets + full resource detail). GitHub/repo
secret scanning, pipeline tokens in external CI, and raw Terraform-state contents
require source/CI integrations that are out of scope for the AWS collector.
"""

from __future__ import annotations

import re
from contextlib import suppress

from sg_audit.analysis.normalize import make_domain_finding
from sg_audit.metadata import (
    CICD_CODEBUILD_PLAINTEXT_SECRET,
    CICD_CODEBUILD_PRIVILEGED,
    CICD_TFSTATE_PUBLIC,
)
from sg_audit.schema import DOMAIN_DEVOPS

DOMAIN = DOMAIN_DEVOPS
SCOPE = "account"
_SOURCE = "codebuild/s3"

_SECRET_NAME = re.compile(
    r"(SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|PRIVATE[_-]?KEY|API[_-]?KEY|SECRET[_-]?KEY|ACCESS[_-]?KEY)",
    re.IGNORECASE,
)
_TFSTATE_NAME = re.compile(r"(tfstate|terraform[-_]?state)", re.IGNORECASE)
_PUBLIC_ACL_URIS = (
    "http://acs.amazonaws.com/groups/global/AllUsers",
    "http://acs.amazonaws.com/groups/global/AuthenticatedUsers",
)


# ── pure analyzers ────────────────────────────────────────────────────────────

def analyze_codebuild_project(account_id, region, project) -> list[dict]:
    out = []
    name = project.get("name", "")
    env = project.get("environment", {}) or {}

    def f(rule_id, severity, summary, details=None):
        return make_domain_finding(
            rule_id=rule_id, severity=severity, finding_summary=summary,
            account_id=account_id, region=region, entity_type="codebuild_project",
            entity_id=name, entity_name=name, source=_SOURCE, details=details or {})

    secret_vars = [
        v.get("name") for v in env.get("environmentVariables", []) or []
        if str(v.get("type", "PLAINTEXT")) == "PLAINTEXT" and _SECRET_NAME.search(str(v.get("name", "")))
    ]
    if secret_vars:
        out.append(f(CICD_CODEBUILD_PLAINTEXT_SECRET, "high",
                    f"CodeBuild project '{name}' has secret-like plaintext env vars: {', '.join(secret_vars)}",
                    {"variables": secret_vars}))
    if env.get("privilegedMode"):
        out.append(f(CICD_CODEBUILD_PRIVILEGED, "medium",
                    f"CodeBuild project '{name}' runs in privileged mode"))
    return out


def analyze_tfstate_bucket(account_id, bucket, is_public) -> list[dict]:
    if not is_public:
        return []
    return [make_domain_finding(
        rule_id=CICD_TFSTATE_PUBLIC, severity="critical",
        finding_summary=f"Terraform state bucket '{bucket}' is publicly accessible",
        account_id=account_id, entity_type="s3_bucket", entity_id=bucket, entity_name=bucket,
        source=_SOURCE)]


# ── boto3 collect ──────────────────────────────────────────────────────────────

def _chunk(xs, n):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def _collect_codebuild(session, account_id, regions) -> list[dict]:
    out = []
    for region in regions or []:
        with suppress(Exception):
            cb = session.client("codebuild", region_name=region)
            names = []
            for page in cb.get_paginator("list_projects").paginate():
                names.extend(page.get("projects", []))
            for batch in _chunk(names, 100):
                with suppress(Exception):
                    for proj in cb.batch_get_projects(names=batch).get("projects", []):
                        out += analyze_codebuild_project(account_id, region, proj)
    return out


def _bucket_is_public(s3, bucket) -> bool:
    with suppress(Exception):
        if (s3.get_bucket_policy_status(Bucket=bucket).get("PolicyStatus", {}) or {}).get("IsPublic"):
            return True
    with suppress(Exception):
        for grant in s3.get_bucket_acl(Bucket=bucket).get("Grants", []) or []:
            if (grant.get("Grantee", {}) or {}).get("URI") in _PUBLIC_ACL_URIS:
                return True
    # Missing/incomplete Block Public Access => treat as exposed for tfstate.
    with suppress(Exception):
        cfg = s3.get_public_access_block(Bucket=bucket).get("PublicAccessBlockConfiguration", {})
        if not all([cfg.get("BlockPublicAcls"), cfg.get("IgnorePublicAcls"),
                    cfg.get("BlockPublicPolicy"), cfg.get("RestrictPublicBuckets")]):
            return True
        return False
    return True  # no PAB at all


def _collect_tfstate(session, account_id) -> list[dict]:
    out = []
    with suppress(Exception):
        s3 = session.client("s3")
        for b in s3.list_buckets().get("Buckets", []):
            name = b.get("Name", "")
            if _TFSTATE_NAME.search(name):
                out += analyze_tfstate_bucket(account_id, name, _bucket_is_public(s3, name))
    return out


def collect(session, account_id: str, account_name: str, region: str, regions=None) -> list[dict]:
    regions = regions or [region]
    return _collect_codebuild(session, account_id, regions) + _collect_tfstate(session, account_id)
