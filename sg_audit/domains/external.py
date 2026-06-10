"""External Attack Surface domain (region-scoped).

Inventories internet-facing load balancers (ALB/NLB/classic) and public API
Gateway endpoints, and flags public API methods with no authorization. EC2/RDS
public exposure is already covered by the compute/data domains. Forgotten
dev/test environments and non-AWS exposed endpoints require external scanning
that is out of scope here.
"""

from __future__ import annotations

from contextlib import suppress

from sg_audit.analysis.normalize import make_domain_finding
from sg_audit.metadata import (
    EXT_INTERNET_FACING_LB,
    EXT_PUBLIC_API,
    EXT_PUBLIC_API_NO_AUTH,
)
from sg_audit.schema import DOMAIN_EXTERNAL

DOMAIN = DOMAIN_EXTERNAL
SCOPE = "region"
_SOURCE = "elb/apigateway"


def analyze_load_balancers(account_id, region, lbs) -> list[dict]:
    """``lbs`` = [{name, scheme, type}]."""
    out = []
    for lb in lbs or []:
        if str(lb.get("scheme")) == "internet-facing":
            name = lb.get("name", "")
            out.append(make_domain_finding(
                rule_id=EXT_INTERNET_FACING_LB, severity="low",
                finding_summary=f"{lb.get('type', 'load balancer')} '{name}' is internet-facing",
                account_id=account_id, region=region, entity_type="load_balancer",
                entity_id=name, entity_name=name, source=_SOURCE))
    return out


def analyze_apis(account_id, region, apis) -> list[dict]:
    """``apis`` = [{id, name, public: bool, no_auth: bool}]."""
    out = []
    for api in apis or []:
        if not api.get("public"):
            continue
        name = api.get("name") or api.get("id")
        out.append(make_domain_finding(
            rule_id=EXT_PUBLIC_API, severity="low",
            finding_summary=f"API Gateway '{name}' has a public endpoint",
            account_id=account_id, region=region, entity_type="api",
            entity_id=api.get("id", name), entity_name=name, source=_SOURCE))
        if api.get("no_auth"):
            out.append(make_domain_finding(
                rule_id=EXT_PUBLIC_API_NO_AUTH, severity="high",
                finding_summary=f"API Gateway '{name}' exposes method(s) with no authorization",
                account_id=account_id, region=region, entity_type="api",
                entity_id=api.get("id", name), entity_name=name, source=_SOURCE))
    return out


def _collect_lbs(session, region) -> list[dict]:
    lbs = []
    with suppress(Exception):
        v2 = session.client("elbv2", region_name=region)
        for page in v2.get_paginator("describe_load_balancers").paginate():
            for lb in page.get("LoadBalancers", []):
                lbs.append({"name": lb.get("LoadBalancerName"), "scheme": lb.get("Scheme"),
                            "type": (lb.get("Type") or "load balancer")})
    with suppress(Exception):
        v1 = session.client("elb", region_name=region)
        for page in v1.get_paginator("describe_load_balancers").paginate():
            for lb in page.get("LoadBalancerDescriptions", []):
                lbs.append({"name": lb.get("LoadBalancerName"), "scheme": lb.get("Scheme"),
                            "type": "classic load balancer"})
    return lbs


def _collect_apis(session, region) -> list[dict]:
    apis = []
    # REST APIs (v1): public unless endpoint type is PRIVATE; check method auth.
    with suppress(Exception):
        ag = session.client("apigateway", region_name=region)
        for page in ag.get_paginator("get_rest_apis").paginate():
            for api in page.get("items", []):
                types = (api.get("endpointConfiguration", {}) or {}).get("types", []) or []
                if "PRIVATE" in types:
                    continue
                no_auth = False
                with suppress(Exception):
                    for rp in ag.get_paginator("get_resources").paginate(restApiId=api["id"]):
                        for res in rp.get("items", []):
                            for method, _ in (res.get("resourceMethods", {}) or {}).items():
                                with suppress(Exception):
                                    m = ag.get_method(restApiId=api["id"], resourceId=res["id"], httpMethod=method)
                                    if m.get("authorizationType") == "NONE" and not m.get("apiKeyRequired"):
                                        no_auth = True
                                        break
                            if no_auth:
                                break
                apis.append({"id": api.get("id"), "name": api.get("name"), "public": True, "no_auth": no_auth})
    # HTTP/WebSocket APIs (v2) are public-facing by design.
    with suppress(Exception):
        v2 = session.client("apigatewayv2", region_name=region)
        for api in v2.get_apis().get("Items", []):
            apis.append({"id": api.get("ApiId"), "name": api.get("Name"), "public": True, "no_auth": False})
    return apis


def collect(session, account_id: str, account_name: str, region: str, regions=None) -> list[dict]:
    lbs = _collect_lbs(session, region)
    apis = _collect_apis(session, region)
    return analyze_load_balancers(account_id, region, lbs) + analyze_apis(account_id, region, apis)
