"""Containers / Kubernetes domain (EKS, region-scoped).

Covers the AWS-API-reachable EKS control-plane posture: public API endpoint,
public endpoint open to 0.0.0.0/0, secrets envelope encryption, and control-plane
logging. In-cluster checks (RBAC, privileged pods, exposed dashboards) require
Kubernetes API access (kubeconfig / in-cluster credentials) and are out of scope
for the AWS-credential collector.
"""

from __future__ import annotations

from contextlib import suppress

from sg_audit.analysis.normalize import make_domain_finding
from sg_audit.metadata import (
    EKS_NO_CONTROL_PLANE_LOGGING,
    EKS_NO_SECRETS_ENCRYPTION,
    EKS_PUBLIC_ENDPOINT,
    EKS_PUBLIC_ENDPOINT_WORLD,
)
from sg_audit.schema import DOMAIN_CONTAINERS

DOMAIN = DOMAIN_CONTAINERS
SCOPE = "region"
_SOURCE = "eks"


def analyze_cluster(account_id, region, cluster) -> list[dict]:
    """``cluster`` = the EKS DescribeCluster ``cluster`` dict."""
    out = []
    name = cluster.get("name", "")

    def f(rule_id, severity, summary):
        return make_domain_finding(
            rule_id=rule_id, severity=severity, finding_summary=summary,
            account_id=account_id, region=region, entity_type="eks_cluster",
            entity_id=name, entity_name=name, source=_SOURCE)

    vpc = cluster.get("resourcesVpcConfig", {}) or {}
    if vpc.get("endpointPublicAccess"):
        cidrs = vpc.get("publicAccessCidrs", []) or []
        if "0.0.0.0/0" in cidrs or not cidrs:
            out.append(f(EKS_PUBLIC_ENDPOINT_WORLD, "high",
                        f"EKS cluster '{name}' public API endpoint is open to 0.0.0.0/0"))
        else:
            out.append(f(EKS_PUBLIC_ENDPOINT, "medium",
                        f"EKS cluster '{name}' API endpoint is publicly accessible"))

    enc = cluster.get("encryptionConfig") or []
    has_secrets_enc = any("secrets" in (e.get("resources") or []) for e in enc)
    if not has_secrets_enc:
        out.append(f(EKS_NO_SECRETS_ENCRYPTION, "medium",
                    f"EKS cluster '{name}' does not have secrets envelope encryption configured"))

    logging_cfg = (cluster.get("logging", {}) or {}).get("clusterLogging", []) or []
    enabled_types = {t for c in logging_cfg if c.get("enabled") for t in c.get("types", [])}
    if not enabled_types:
        out.append(f(EKS_NO_CONTROL_PLANE_LOGGING, "low",
                    f"EKS cluster '{name}' has no control-plane logging enabled"))
    return out


def collect(session, account_id: str, account_name: str, region: str, regions=None) -> list[dict]:
    eks = session.client("eks", region_name=region)
    out = []
    names = []
    with suppress(Exception):
        for page in eks.get_paginator("list_clusters").paginate():
            names.extend(page.get("clusters", []))
    for name in names:
        with suppress(Exception):
            cluster = eks.describe_cluster(name=name).get("cluster", {})
            out += analyze_cluster(account_id, region, cluster)
    return out
