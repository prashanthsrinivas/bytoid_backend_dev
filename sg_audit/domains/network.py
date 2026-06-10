"""Network domain collector — VPC architecture posture (region-scoped).

Findings are grouped at the VPC level (the requested dashboard unit): public
subnets, route tables leaking 0.0.0.0/0 to an internet gateway, auto-assigned
public IPs, default-VPC usage, and cross-account VPC peering.
"""

from __future__ import annotations

from sg_audit.analysis.normalize import make_domain_finding
from sg_audit.metadata import (
    NET_DEFAULT_VPC_PRESENT,
    NET_PEERING_CROSS_ACCOUNT,
    NET_PUBLIC_SUBNET,
    NET_ROUTE_OPEN_TO_IGW,
    NET_SUBNET_AUTO_PUBLIC_IP,
)
from sg_audit.schema import DOMAIN_NETWORK

DOMAIN = DOMAIN_NETWORK
SCOPE = "region"
_SOURCE = "ec2:network"


def _name(tags) -> str:
    for t in tags or []:
        if t.get("Key") == "Name":
            return t.get("Value", "")
    return ""


def _route_to_igw(rt: dict) -> bool:
    for r in rt.get("Routes", []) or []:
        gw = str(r.get("GatewayId", ""))
        if gw.startswith("igw-") and (
            r.get("DestinationCidrBlock") == "0.0.0.0/0"
            or r.get("DestinationIpv6CidrBlock") == "::/0"
        ):
            return True
    return False


def analyze_network(account_id, region, vpcs, subnets, route_tables, peerings) -> list[dict]:
    out: list[dict] = []
    vpc_name = {v.get("VpcId"): (_name(v.get("Tags")) or v.get("VpcId")) for v in vpcs or []}

    def vpc_finding(rule_id, severity, summary, vpc_id, details):
        return make_domain_finding(
            rule_id=rule_id, severity=severity, finding_summary=summary,
            account_id=account_id, region=region,
            entity_type="vpc", entity_id=vpc_id or "unknown",
            entity_name=vpc_name.get(vpc_id, vpc_id or "unknown"),
            source=_SOURCE, details=details)

    # Default VPCs.
    for v in vpcs or []:
        if v.get("IsDefault"):
            out.append(vpc_finding(
                NET_DEFAULT_VPC_PRESENT, "low",
                f"Default VPC {v.get('VpcId')} is present in {region}",
                v.get("VpcId"), {}))

    # Public route tables + the subnets they make public.
    public_rt_ids = set()
    main_rt_by_vpc = {}
    rt_by_subnet = {}
    for rt in route_tables or []:
        rid = rt.get("RouteTableId")
        if _route_to_igw(rt):
            public_rt_ids.add(rid)
            out.append(vpc_finding(
                NET_ROUTE_OPEN_TO_IGW, "low",
                f"Route table {rid} routes 0.0.0.0/0 to an internet gateway",
                rt.get("VpcId"), {"route_table_id": rid}))
        for assoc in rt.get("Associations", []) or []:
            if assoc.get("Main"):
                main_rt_by_vpc[rt.get("VpcId")] = rid
            elif assoc.get("SubnetId"):
                rt_by_subnet[assoc["SubnetId"]] = rid

    for s in subnets or []:
        sid, vpc_id = s.get("SubnetId"), s.get("VpcId")
        rid = rt_by_subnet.get(sid, main_rt_by_vpc.get(vpc_id))
        if rid in public_rt_ids:
            out.append(vpc_finding(
                NET_PUBLIC_SUBNET, "medium",
                f"Subnet {sid} is internet-facing (public route table)",
                vpc_id, {"subnet_id": sid, "route_table_id": rid}))
        if s.get("MapPublicIpOnLaunch"):
            out.append(vpc_finding(
                NET_SUBNET_AUTO_PUBLIC_IP, "medium",
                f"Subnet {sid} auto-assigns public IPs on launch",
                vpc_id, {"subnet_id": sid}))

    # Cross-account peering.
    for p in peerings or []:
        if str(p.get("Status", {}).get("Code")) not in ("active", "pending-acceptance", "provisioning"):
            continue
        req = p.get("RequesterVpcInfo", {}) or {}
        acc = p.get("AccepterVpcInfo", {}) or {}
        owners = {req.get("OwnerId"), acc.get("OwnerId")}
        owners.discard(None)
        if account_id and any(o != account_id for o in owners):
            local_vpc = req.get("VpcId") if req.get("OwnerId") == account_id else acc.get("VpcId")
            out.append(vpc_finding(
                NET_PEERING_CROSS_ACCOUNT, "medium",
                f"VPC peering {p.get('VpcPeeringConnectionId')} connects to an external account",
                local_vpc, {"peering_id": p.get("VpcPeeringConnectionId"),
                            "owners": sorted(o for o in owners if o)}))
    return out


def _paginate(client, op, key):
    out = []
    paginator = client.get_paginator(op)
    for page in paginator.paginate():
        out.extend(page.get(key, []))
    return out


def collect(session, account_id: str, account_name: str, region: str, regions=None) -> list[dict]:
    ec2 = session.client("ec2", region_name=region)
    vpcs = _paginate(ec2, "describe_vpcs", "Vpcs")
    subnets = _paginate(ec2, "describe_subnets", "Subnets")
    route_tables = _paginate(ec2, "describe_route_tables", "RouteTables")
    try:
        peerings = _paginate(ec2, "describe_vpc_peering_connections", "VpcPeeringConnections")
    except Exception:
        peerings = []
    return analyze_network(account_id, region, vpcs, subnets, route_tables, peerings)
