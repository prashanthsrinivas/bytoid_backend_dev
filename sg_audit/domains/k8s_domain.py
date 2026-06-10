"""In-cluster Kubernetes domain (EKS, region-scoped, opt-in + best-effort).

Connects to each EKS cluster's Kubernetes API (using an EKS bearer token derived
from the assumed AWS session) and flags cluster-admin RBAC to broad subjects,
privileged / host-namespace pods, and dashboards exposed via LoadBalancer
services. Requires ``SG_K8S_SCAN_ENABLED``, a reachable cluster endpoint, and
RBAC for the auditor; any failure degrades to no findings (recorded in
collector_status by the runner). EKS *control-plane* posture is covered
separately by the ``containers`` domain.

Named ``k8s_domain`` (module) but registered under the ``k8s`` domain key.
"""

from __future__ import annotations

import base64
import tempfile
from contextlib import suppress

from sg_audit import config as sg_config
from sg_audit.analysis.normalize import make_domain_finding
from sg_audit.metadata import (
    K8S_CLUSTER_ADMIN_BINDING,
    K8S_DASHBOARD_EXPOSED,
    K8S_HOST_NAMESPACE_POD,
    K8S_PRIVILEGED_POD,
)
from sg_audit.schema import DOMAIN_K8S

DOMAIN = DOMAIN_K8S
SCOPE = "region"
_SOURCE = "kubernetes"

_BROAD_SUBJECTS = {"system:authenticated", "system:anonymous", "system:unauthenticated", "*"}
_SKIP_NS = {"kube-system", "kube-node-lease", "kube-public"}


# ── pure analyzers ────────────────────────────────────────────────────────────

def _f(account_id, region, cluster, rule_id, severity, summary, details=None):
    return make_domain_finding(
        rule_id=rule_id, severity=severity, finding_summary=summary,
        account_id=account_id, region=region, entity_type="eks_cluster",
        entity_id=cluster, entity_name=cluster, source=_SOURCE, details=details or {})


def analyze_rolebindings(account_id, region, cluster, crbs) -> list[dict]:
    out = []
    for crb in crbs or []:
        if (crb.get("roleRef", {}) or {}).get("name") != "cluster-admin":
            continue
        for s in crb.get("subjects", []) or []:
            if s.get("name") in _BROAD_SUBJECTS:
                name = (crb.get("metadata", {}) or {}).get("name", "")
                out.append(_f(account_id, region, cluster, K8S_CLUSTER_ADMIN_BINDING, "high",
                              f"cluster-admin bound to '{s.get('name')}' via '{name}' on cluster {cluster}",
                              {"binding": name, "subject": s.get("name")}))
                break
    return out


def analyze_pods(account_id, region, cluster, pods) -> list[dict]:
    out = []
    for pod in pods or []:
        meta = pod.get("metadata", {}) or {}
        ns = meta.get("namespace", "")
        if ns in _SKIP_NS:
            continue
        spec = pod.get("spec", {}) or {}
        pname = meta.get("name", "")
        if any((c.get("securityContext", {}) or {}).get("privileged") for c in spec.get("containers", []) or []):
            out.append(_f(account_id, region, cluster, K8S_PRIVILEGED_POD, "high",
                          f"Privileged container in pod {ns}/{pname} (cluster {cluster})",
                          {"namespace": ns, "pod": pname}))
        if spec.get("hostNetwork") or spec.get("hostPID") or spec.get("hostIPC"):
            out.append(_f(account_id, region, cluster, K8S_HOST_NAMESPACE_POD, "medium",
                          f"Pod {ns}/{pname} uses a host namespace (cluster {cluster})",
                          {"namespace": ns, "pod": pname}))
    return out


def analyze_services(account_id, region, cluster, services) -> list[dict]:
    out = []
    for svc in services or []:
        meta = svc.get("metadata", {}) or {}
        if (svc.get("spec", {}) or {}).get("type") != "LoadBalancer":
            continue
        name, ns = meta.get("name", ""), meta.get("namespace", "")
        if "dashboard" in name.lower() or "dashboard" in ns.lower():
            out.append(_f(account_id, region, cluster, K8S_DASHBOARD_EXPOSED, "high",
                          f"Kubernetes dashboard exposed via LoadBalancer {ns}/{name} (cluster {cluster})",
                          {"namespace": ns, "service": name}))
    return out


# ── EKS auth + raw k8s API (best-effort) ──────────────────────────────────────

def _eks_token(session, cluster: str, region: str) -> str:
    """Build a 'k8s-aws-v1.' bearer token via a presigned STS GetCallerIdentity."""
    from botocore.signers import RequestSigner

    client = session.client("sts", region_name=region)
    signer = RequestSigner(
        client.meta.service_model.service_id, region, "sts", "v4",
        session.get_credentials(), session.events,
    )
    signed = signer.generate_presigned_url(
        {"method": "GET",
         "url": f"https://sts.{region}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
         "body": {}, "headers": {"x-k8s-aws-id": cluster}, "context": {}},
        region_name=region, expires_in=60, operation_name="",
    )
    return "k8s-aws-v1." + base64.urlsafe_b64encode(signed.encode()).decode().rstrip("=")


def _k8s_get(endpoint: str, path: str, token: str, ca_path: str):
    import requests

    resp = requests.get(
        f"{endpoint}{path}",
        headers={"Authorization": f"Bearer {token}"},
        verify=ca_path, timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("items", [])


def collect(session, account_id: str, account_name: str, region: str, regions=None) -> list[dict]:
    if not sg_config.k8s_scan_enabled():
        return []
    out: list[dict] = []
    eks = session.client("eks", region_name=region)
    names = []
    with suppress(Exception):
        for page in eks.get_paginator("list_clusters").paginate():
            names.extend(page.get("clusters", []))

    for cluster in names:
        with suppress(Exception):
            desc = eks.describe_cluster(name=cluster).get("cluster", {})
            vpc = desc.get("resourcesVpcConfig", {}) or {}
            if not vpc.get("endpointPublicAccess"):
                continue  # private endpoint — not reachable from the collector
            endpoint = desc.get("endpoint")
            ca_data = (desc.get("certificateAuthority", {}) or {}).get("data")
            if not endpoint or not ca_data:
                continue
            token = _eks_token(session, cluster, region)
            with tempfile.NamedTemporaryFile("wb", suffix=".pem", delete=True) as ca:
                ca.write(base64.b64decode(ca_data))
                ca.flush()
                with suppress(Exception):
                    crbs = _k8s_get(endpoint, "/apis/rbac.authorization.k8s.io/v1/clusterrolebindings", token, ca.name)
                    out += analyze_rolebindings(account_id, region, cluster, crbs)
                with suppress(Exception):
                    pods = _k8s_get(endpoint, "/api/v1/pods", token, ca.name)
                    out += analyze_pods(account_id, region, cluster, pods)
                with suppress(Exception):
                    svcs = _k8s_get(endpoint, "/api/v1/services", token, ca.name)
                    out += analyze_services(account_id, region, cluster, svcs)
    return out
