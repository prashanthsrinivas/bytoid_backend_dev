"""Compute domain collector — EC2 posture (region-scoped).

Findings: public IP exposure, IMDSv1 allowed (SSRF credential-theft path), launch
from a public AMI, and instances not managed by SSM (patch posture unknown).
Open-management-port linkage is covered by the Security Groups domain
(SG_ADMIN_WORLD_INGRESS); a dedicated cross-domain EC2_OPEN_MGMT_PORT join is
deferred to Phase 2.
"""

from __future__ import annotations

from sg_audit.analysis.normalize import make_domain_finding
from sg_audit.metadata import (
    EC2_IMDSV1_ENABLED,
    EC2_NOT_SSM_MANAGED,
    EC2_PUBLIC_AMI,
    EC2_PUBLIC_IP,
)
from sg_audit.schema import DOMAIN_COMPUTE

DOMAIN = DOMAIN_COMPUTE
SCOPE = "region"
_SOURCE = "ec2:compute"


def _name(tags) -> str:
    for t in tags or []:
        if t.get("Key") == "Name":
            return t.get("Value", "")
    return ""


def analyze_instances(account_id, region, instances, public_ami_ids, ssm_managed_ids) -> list[dict]:
    """``instances`` = flattened Reservations[].Instances[]; ``public_ami_ids`` /
    ``ssm_managed_ids`` are sets (ssm None => SSM data unavailable, skip that rule)."""
    out = []
    for inst in instances or []:
        iid = inst.get("InstanceId", "")
        if inst.get("State", {}).get("Name") in ("terminated", "shutting-down"):
            continue
        name = _name(inst.get("Tags")) or iid

        def f(rule_id, severity, summary, details=None, iid=iid, name=name):
            return make_domain_finding(
                rule_id=rule_id, severity=severity, finding_summary=summary,
                account_id=account_id, region=region, entity_type="ec2_instance",
                entity_id=iid, entity_name=name, source=_SOURCE, details=details or {})

        if inst.get("PublicIpAddress"):
            out.append(f(EC2_PUBLIC_IP, "medium",
                        f"EC2 instance {name} has a public IP ({inst.get('PublicIpAddress')})"))

        http_tokens = (inst.get("MetadataOptions", {}) or {}).get("HttpTokens")
        if http_tokens and http_tokens != "required":
            out.append(f(EC2_IMDSV1_ENABLED, "high",
                        f"EC2 instance {name} allows IMDSv1 (HttpTokens={http_tokens})"))

        if inst.get("ImageId") in public_ami_ids:
            out.append(f(EC2_PUBLIC_AMI, "medium",
                        f"EC2 instance {name} was launched from a public AMI ({inst.get('ImageId')})"))

        if ssm_managed_ids is not None and iid not in ssm_managed_ids:
            out.append(f(EC2_NOT_SSM_MANAGED, "low",
                        f"EC2 instance {name} is not managed by SSM (patch posture unknown)"))
    return out


def collect(session, account_id: str, account_name: str, region: str, regions=None) -> list[dict]:
    ec2 = session.client("ec2", region_name=region)
    instances = []
    image_ids = set()
    try:
        for page in ec2.get_paginator("describe_instances").paginate():
            for res in page.get("Reservations", []):
                for inst in res.get("Instances", []):
                    instances.append(inst)
                    if inst.get("ImageId"):
                        image_ids.add(inst["ImageId"])
    except Exception:
        return []

    # Which of the in-use AMIs are public?
    public_ami_ids = set()
    if image_ids:
        try:
            imgs = ec2.describe_images(ImageIds=list(image_ids)).get("Images", [])
            public_ami_ids = {i["ImageId"] for i in imgs if i.get("Public")}
        except Exception:
            public_ami_ids = set()

    # SSM-managed instance ids (None => SSM unavailable, skip the rule).
    ssm_managed_ids = None
    try:
        ssm = session.client("ssm", region_name=region)
        ssm_managed_ids = set()
        for page in ssm.get_paginator("describe_instance_information").paginate():
            for item in page.get("InstanceInformationList", []):
                if item.get("InstanceId"):
                    ssm_managed_ids.add(item["InstanceId"])
    except Exception:
        ssm_managed_ids = None

    return analyze_instances(account_id, region, instances, public_ami_ids, ssm_managed_ids)
