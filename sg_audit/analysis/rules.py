"""Deterministic Security Group rule engine.

Pure, stdlib-only (``ipaddress`` is stdlib) so it runs identically in the app
(tests) and inside the Lambda collector. Turns raw EC2 ``DescribeSecurityGroups``
output into normalized findings. The engine is the single source of truth for
what is "risky" — the AI recommender only ever explains findings produced here.

Severity banding (summary):
  * world (0.0.0.0/0 or ::/0) → admin/db/cache port .......... critical
  * world → all ports/all protocols .......................... critical
  * world → other sensitive service port ..................... high
  * world → wide port range (>threshold, not all) ............ high
  * admin port → wide public range (not world) ............... high
  * sensitive port → broad internal (RFC1918 /≤16) ........... medium
  * default SG has CIDR rules / default SG attached .......... medium
  * unrestricted egress on an already-exposed SG ............. medium
  * world → ICMP ............................................. low
  * unused SG (no attachments) .............................. low
  * rule missing a description .............................. info
"""

from __future__ import annotations

import ipaddress

from sg_audit.analysis.normalize import make_finding, make_finding_id
from sg_audit.schema import (
    CAT_ACCESS_CONTROL,
    CAT_DATA_EXPOSURE,
    CAT_EGRESS,
    CAT_HYGIENE,
    CAT_NETWORK_EXPOSURE,
    PORT_CLASS_ADMIN,
    PORT_CLASS_CACHE,
    PORT_CLASS_DATABASE,
    PORT_SERVICE_MAP,
    RULE_ADMIN_WORLD_INGRESS,
    RULE_ALL_PORTS_WORLD,
    RULE_BROAD_EGRESS_ALL,
    RULE_BROAD_INTERNAL_CIDR,
    RULE_CACHE_WORLD_INGRESS,
    RULE_DB_WORLD_INGRESS,
    RULE_DEFAULT_SG_HAS_RULES,
    RULE_DEFAULT_SG_IN_USE,
    RULE_ICMP_WORLD,
    RULE_MISSING_RULE_DESCRIPTION,
    RULE_NON_WORLD_ADMIN_OPEN_WIDE,
    RULE_SENSITIVE_NON_ADMIN_WORLD,
    RULE_UNUSED,
    RULE_WIDE_RANGE_WORLD,
    SENSITIVE_PORTS,
    SEV_CRITICAL,
    SEV_HIGH,
    SEV_INFO,
    SEV_LOW,
    SEV_MEDIUM,
    WIDE_RANGE_PORT_THRESHOLD,
    WORLD_CIDRS,
)

# Rules that, when present on a security group, mean the SG is genuinely exposed
# to the internet (used to decide whether unrestricted egress is material).
_EXPOSED_RULE_IDS = frozenset({
    RULE_ADMIN_WORLD_INGRESS,
    RULE_DB_WORLD_INGRESS,
    RULE_CACHE_WORLD_INGRESS,
    RULE_ALL_PORTS_WORLD,
    RULE_SENSITIVE_NON_ADMIN_WORLD,
    RULE_WIDE_RANGE_WORLD,
})


def _is_world(cidr: str) -> bool:
    return cidr in WORLD_CIDRS


def _port_bounds(perm: dict) -> tuple[int, int]:
    """(from_port, to_port) for a permission, normalizing all-ports to 0-65535."""
    proto = str(perm.get("IpProtocol", ""))
    if proto == "-1":
        return 0, 65535
    fp = perm.get("FromPort")
    tp = perm.get("ToPort")
    if fp is None and tp is None:
        return 0, 65535
    fp = 0 if fp is None else int(fp)
    tp = 65535 if tp is None else int(tp)
    if fp > tp:
        fp, tp = tp, fp
    return fp, tp


def _covers_all_ports(fp: int, tp: int) -> bool:
    return fp <= 0 and tp >= 65535


def _sensitive_ports_in_range(fp: int, tp: int) -> list[int]:
    return sorted(p for p in SENSITIVE_PORTS if fp <= p <= tp)


def _iter_cidrs(perm: dict):
    """Yield (cidr, description, is_ipv6) for every IP range in a permission."""
    for r in perm.get("IpRanges", []) or []:
        cidr = r.get("CidrIp")
        if cidr:
            yield cidr, (r.get("Description") or ""), False
    for r in perm.get("Ipv6Ranges", []) or []:
        cidr = r.get("CidrIpv6")
        if cidr:
            yield cidr, (r.get("Description") or ""), True


def _classify_cidr(cidr: str):
    """Return (is_private, prefixlen) or (None, None) if it can't be parsed."""
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return None, None
    return net.is_private, net.prefixlen


def _proto_label(perm: dict) -> str:
    proto = str(perm.get("IpProtocol", ""))
    return {"-1": "all", "6": "tcp", "17": "udp", "1": "icmp"}.get(proto, proto or "all")


class _Ctx:
    """Carries the account/region scope through the per-SG analysis."""

    __slots__ = ("account_id", "account_name", "region")

    def __init__(self, account_id, account_name, region):
        self.account_id = account_id or ""
        self.account_name = account_name or ""
        self.region = region or ""


def _details(ctx: _Ctx, sg: dict, *, rule_id, cidr, protocol, from_port, to_port,
             port=None, service="", in_use=None) -> dict:
    return {
        "account_id": ctx.account_id,
        "account_name": ctx.account_name,
        "region": ctx.region,
        "group_id": sg.get("GroupId", ""),
        "group_name": sg.get("GroupName", ""),
        "vpc_id": sg.get("VpcId", ""),
        "rule_id": rule_id,
        "cidr": cidr,
        "protocol": protocol,
        "from_port": from_port,
        "to_port": to_port,
        "port": port,
        "service": service,
        "in_use": in_use,
    }


def _emit(ctx, sg, *, rule_id, category, severity, summary, cidr, protocol,
          from_port, to_port, port=None, service="", in_use=None) -> dict:
    fid = make_finding_id(ctx.account_id, sg.get("GroupId", ""), rule_id, cidr,
                          protocol, from_port, to_port, port or "")
    return make_finding(
        finding_id=fid,
        category=category,
        rule_id=rule_id,
        severity=severity,
        finding_summary=summary,
        supporting_details=_details(
            ctx, sg, rule_id=rule_id, cidr=cidr, protocol=protocol,
            from_port=from_port, to_port=to_port, port=port, service=service, in_use=in_use,
        ),
    )


def _analyze_world_ingress(ctx, sg, perm, cidr, fp, tp, proto_label, in_use):
    """Findings for a single world-open (0.0.0.0/0 or ::/0) ingress range."""
    out = []
    gid = sg.get("GroupId", "")
    proto = str(perm.get("IpProtocol", ""))
    where = f"{ctx.account_id}/{ctx.region}/{gid}"

    if proto in ("icmp", "icmpv6", "1", "58"):
        out.append(_emit(
            ctx, sg, rule_id=RULE_ICMP_WORLD, category=CAT_NETWORK_EXPOSURE, severity=SEV_LOW,
            summary=f"ICMP open to the internet ({cidr}) on {where}",
            cidr=cidr, protocol=proto_label, from_port=fp, to_port=tp, in_use=in_use,
        ))
        return out

    if proto == "-1" or _covers_all_ports(fp, tp):
        out.append(_emit(
            ctx, sg, rule_id=RULE_ALL_PORTS_WORLD, category=CAT_NETWORK_EXPOSURE, severity=SEV_CRITICAL,
            summary=f"All ports/protocols open to the internet ({cidr}) on {where}",
            cidr=cidr, protocol=proto_label, from_port=fp, to_port=tp, in_use=in_use,
        ))
        return out

    # Per-sensitive-port findings within the range.
    for port in _sensitive_ports_in_range(fp, tp):
        service, klass = PORT_SERVICE_MAP[port]
        if klass == PORT_CLASS_ADMIN:
            rid, cat, sev = RULE_ADMIN_WORLD_INGRESS, CAT_NETWORK_EXPOSURE, SEV_CRITICAL
        elif klass == PORT_CLASS_DATABASE:
            rid, cat, sev = RULE_DB_WORLD_INGRESS, CAT_DATA_EXPOSURE, SEV_CRITICAL
        elif klass == PORT_CLASS_CACHE:
            rid, cat, sev = RULE_CACHE_WORLD_INGRESS, CAT_DATA_EXPOSURE, SEV_CRITICAL
        else:
            rid, cat, sev = RULE_SENSITIVE_NON_ADMIN_WORLD, CAT_NETWORK_EXPOSURE, SEV_HIGH
        out.append(_emit(
            ctx, sg, rule_id=rid, category=cat, severity=sev,
            summary=f"{service} (port {port}) open to the internet ({cidr}) on {where}",
            cidr=cidr, protocol=proto_label, from_port=fp, to_port=tp, port=port,
            service=service, in_use=in_use,
        ))

    # A wide port range to the world (beyond the sensitive ports already flagged).
    if (tp - fp + 1) > WIDE_RANGE_PORT_THRESHOLD:
        out.append(_emit(
            ctx, sg, rule_id=RULE_WIDE_RANGE_WORLD, category=CAT_NETWORK_EXPOSURE, severity=SEV_HIGH,
            summary=f"Wide port range {fp}-{tp} open to the internet ({cidr}) on {where}",
            cidr=cidr, protocol=proto_label, from_port=fp, to_port=tp, in_use=in_use,
        ))
    return out


def _analyze_scoped_ingress(ctx, sg, perm, cidr, fp, tp, proto_label, in_use):
    """Findings for a non-world ingress range (wide public or broad internal)."""
    out = []
    gid = sg.get("GroupId", "")
    where = f"{ctx.account_id}/{ctx.region}/{gid}"
    is_private, prefixlen = _classify_cidr(cidr)
    if prefixlen is None:
        return out
    sensitive = _sensitive_ports_in_range(fp, tp)

    for port in sensitive:
        service, klass = PORT_SERVICE_MAP[port]
        # Admin port open to a wide *public* range (wider than /16) — short of
        # full world but still broad internet exposure.
        if klass == PORT_CLASS_ADMIN and is_private is False and prefixlen <= 16:
            out.append(_emit(
                ctx, sg, rule_id=RULE_NON_WORLD_ADMIN_OPEN_WIDE, category=CAT_ACCESS_CONTROL,
                severity=SEV_HIGH,
                summary=f"{service} (port {port}) open to a wide public range {cidr} on {where}",
                cidr=cidr, protocol=proto_label, from_port=fp, to_port=tp, port=port,
                service=service, in_use=in_use,
            ))
        # Sensitive port reachable from a broad internal range (e.g. 10.0.0.0/8).
        elif is_private is True and prefixlen <= 16:
            out.append(_emit(
                ctx, sg, rule_id=RULE_BROAD_INTERNAL_CIDR, category=CAT_ACCESS_CONTROL,
                severity=SEV_MEDIUM,
                summary=f"{service} (port {port}) open to a broad internal range {cidr} on {where}",
                cidr=cidr, protocol=proto_label, from_port=fp, to_port=tp, port=port,
                service=service, in_use=in_use,
            ))
    return out


def _is_default_sg(sg: dict) -> bool:
    return (sg.get("GroupName") or "").lower() == "default"


def _has_cidr_ingress(sg: dict) -> bool:
    for perm in sg.get("IpPermissions", []) or []:
        for _cidr, _desc, _v6 in _iter_cidrs(perm):
            return True
    return False


def _any_missing_description(sg: dict) -> bool:
    for key in ("IpPermissions", "IpPermissionsEgress"):
        for perm in sg.get(key, []) or []:
            for _cidr, desc, _v6 in _iter_cidrs(perm):
                if not desc.strip():
                    return True
    return False


def _has_world_unrestricted_egress(sg: dict) -> bool:
    for perm in sg.get("IpPermissionsEgress", []) or []:
        if str(perm.get("IpProtocol", "")) != "-1":
            continue
        for cidr, _desc, _v6 in _iter_cidrs(perm):
            if _is_world(cidr):
                return True
    return False


def analyze_security_group(ctx: _Ctx, sg: dict, eni_usage) -> list[dict]:
    """All findings for one security group within an account/region."""
    out: list[dict] = []
    gid = sg.get("GroupId", "")
    where = f"{ctx.account_id}/{ctx.region}/{gid}"

    # ENI attachment state: None => unknown (ENI data unavailable -> never used
    # to suppress an exposure finding).
    in_use = None
    if eni_usage is not None:
        in_use = eni_usage.get(gid, 0) > 0

    # Ingress analysis.
    for perm in sg.get("IpPermissions", []) or []:
        fp, tp = _port_bounds(perm)
        proto_label = _proto_label(perm)
        for cidr, _desc, _v6 in _iter_cidrs(perm):
            if _is_world(cidr):
                out += _analyze_world_ingress(ctx, sg, perm, cidr, fp, tp, proto_label, in_use)
            else:
                out += _analyze_scoped_ingress(ctx, sg, perm, cidr, fp, tp, proto_label, in_use)

    exposed = any(f["rule_id"] in _EXPOSED_RULE_IDS for f in out)

    # Unrestricted egress is near-universal (AWS default), so only flag it when
    # the SG is genuinely exposed (compromise + open egress = exfil path).
    if exposed and _has_world_unrestricted_egress(sg):
        out.append(_emit(
            ctx, sg, rule_id=RULE_BROAD_EGRESS_ALL, category=CAT_EGRESS, severity=SEV_MEDIUM,
            summary=f"Unrestricted egress to all destinations on an internet-exposed SG ({where})",
            cidr="0.0.0.0/0", protocol="all", from_port=0, to_port=65535, in_use=in_use,
        ))

    # Default SG hygiene (CIS 5.4: the default SG should restrict all traffic).
    if _is_default_sg(sg):
        if _has_cidr_ingress(sg):
            out.append(_emit(
                ctx, sg, rule_id=RULE_DEFAULT_SG_HAS_RULES, category=CAT_HYGIENE, severity=SEV_MEDIUM,
                summary=f"Default security group has CIDR-based ingress rules ({where})",
                cidr="", protocol="", from_port=None, to_port=None, in_use=in_use,
            ))
        if in_use:
            out.append(_emit(
                ctx, sg, rule_id=RULE_DEFAULT_SG_IN_USE, category=CAT_HYGIENE, severity=SEV_MEDIUM,
                summary=f"Default security group is attached to resources ({where})",
                cidr="", protocol="", from_port=None, to_port=None, in_use=in_use,
            ))

    # Unused SG (only when ENI data is available and shows zero attachments;
    # never inferred when usage is unknown).
    if eni_usage is not None and not _is_default_sg(sg) and eni_usage.get(gid, 0) == 0:
        out.append(_emit(
            ctx, sg, rule_id=RULE_UNUSED, category=CAT_HYGIENE, severity=SEV_LOW,
            summary=f"Security group is unused (no network interfaces attached) ({where})",
            cidr="", protocol="", from_port=None, to_port=None, in_use=False,
        ))

    # One aggregate hygiene finding per SG for missing rule descriptions.
    if _any_missing_description(sg):
        out.append(_emit(
            ctx, sg, rule_id=RULE_MISSING_RULE_DESCRIPTION, category=CAT_HYGIENE, severity=SEV_INFO,
            summary=f"Security group has rules without descriptions ({where})",
            cidr="", protocol="", from_port=None, to_port=None, in_use=in_use,
        ))

    return out


def analyze_account_region(
    *,
    account_id: str,
    account_name: str,
    region: str,
    security_groups: list[dict],
    eni_sg_usage: dict | None = None,
) -> list[dict]:
    """Run the full rule set over every SG in one account/region.

    ``eni_sg_usage`` maps group_id -> attachment count. Pass ``None`` when ENI
    data could not be fetched (throttle/permission) — usage-dependent findings
    are then skipped rather than guessed, and exposure findings still stand.
    """
    ctx = _Ctx(account_id, account_name, region)
    findings: list[dict] = []
    for sg in security_groups or []:
        findings += analyze_security_group(ctx, sg, eni_sg_usage)
    return findings
