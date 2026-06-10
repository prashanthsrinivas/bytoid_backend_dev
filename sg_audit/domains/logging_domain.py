"""Logging & Monitoring domain collector (account-scoped, sweeps regions).

Findings: no enabled CloudTrail, trail not multi-region, log-file validation off,
VPCs without flow logs, short log retention, and AWS Config not recording.
Named ``logging_domain`` (not ``logging``) to avoid shadowing the stdlib module.
"""

from __future__ import annotations

from contextlib import suppress

from sg_audit.analysis.normalize import make_domain_finding
from sg_audit.metadata import (
    LOG_FLOW_LOGS_MISSING,
    LOG_NO_CLOUDTRAIL,
    LOG_NO_CONFIG_RECORDER,
    LOG_NO_LOG_VALIDATION,
    LOG_SHORT_RETENTION,
    LOG_TRAIL_NOT_MULTIREGION,
)
from sg_audit.schema import DOMAIN_LOGGING

DOMAIN = DOMAIN_LOGGING
SCOPE = "account"
_SOURCE = "cloudtrail/ec2/logs/config"
_MIN_RETENTION_DAYS = 365


# ── pure analyzers ────────────────────────────────────────────────────────────

def analyze_trails(account_id, trails) -> list[dict]:
    """``trails`` = list of {Name, TrailARN, IsMultiRegionTrail, IsLogging,
    LogFileValidationEnabled} (deduped across regions)."""
    out = []
    logging_trails = [t for t in trails if t.get("IsLogging")]
    if not logging_trails:
        out.append(make_domain_finding(
            rule_id=LOG_NO_CLOUDTRAIL, severity="high",
            finding_summary="No enabled CloudTrail trail captures account activity",
            account_id=account_id, entity_type="account", entity_id=account_id,
            entity_name="cloudtrail", source=_SOURCE))
        return out  # the per-trail checks are moot with no logging trail

    if not any(t.get("IsMultiRegionTrail") for t in logging_trails):
        out.append(make_domain_finding(
            rule_id=LOG_TRAIL_NOT_MULTIREGION, severity="medium",
            finding_summary="No multi-region CloudTrail trail is enabled",
            account_id=account_id, entity_type="account", entity_id=account_id,
            entity_name="cloudtrail", source=_SOURCE))

    for t in logging_trails:
        if not t.get("LogFileValidationEnabled"):
            name = t.get("Name", t.get("TrailARN", ""))
            out.append(make_domain_finding(
                rule_id=LOG_NO_LOG_VALIDATION, severity="low",
                finding_summary=f"CloudTrail '{name}' has log file validation disabled",
                account_id=account_id, entity_type="trail", entity_id=name, entity_name=name,
                source=_SOURCE))
    return out


def analyze_flow_logs(account_id, region, vpc_ids, flow_log_vpc_ids) -> list[dict]:
    out = []
    for vpc_id in vpc_ids:
        if vpc_id not in flow_log_vpc_ids:
            out.append(make_domain_finding(
                rule_id=LOG_FLOW_LOGS_MISSING, severity="medium",
                finding_summary=f"VPC {vpc_id} has no flow logs in {region}",
                account_id=account_id, region=region, entity_type="vpc",
                entity_id=vpc_id, entity_name=vpc_id, source=_SOURCE))
    return out


def analyze_retention(account_id, region, log_groups) -> list[dict]:
    out = []
    for g in log_groups or []:
        retention = g.get("retentionInDays")
        if retention is not None and retention < _MIN_RETENTION_DAYS:
            name = g.get("logGroupName", "")
            out.append(make_domain_finding(
                rule_id=LOG_SHORT_RETENTION, severity="low",
                finding_summary=f"Log group '{name}' retention is {retention} days (<{_MIN_RETENTION_DAYS})",
                account_id=account_id, region=region, entity_type="log_group",
                entity_id=name, entity_name=name, source=_SOURCE, details={"retention_days": retention}))
    return out


def analyze_config(account_id, recording_anywhere: bool) -> list[dict]:
    if recording_anywhere:
        return []
    return [make_domain_finding(
        rule_id=LOG_NO_CONFIG_RECORDER, severity="medium",
        finding_summary="AWS Config is not recording in any scanned region",
        account_id=account_id, entity_type="account", entity_id=account_id,
        entity_name="aws_config", source=_SOURCE)]


# ── boto3 collect ──────────────────────────────────────────────────────────────

def collect(session, account_id: str, account_name: str, region: str, regions=None) -> list[dict]:
    regions = regions or [region]
    findings: list[dict] = []

    # Trails (dedup by ARN across regions; enrich with logging status).
    trails_by_arn: dict[str, dict] = {}
    recording_anywhere = False
    for r in regions:
        with suppress(Exception):
            ct = session.client("cloudtrail", region_name=r)
            for t in ct.describe_trails(includeShadowTrails=False).get("trailList", []):
                arn = t.get("TrailARN")
                if arn in trails_by_arn:
                    continue
                entry = {
                    "Name": t.get("Name"), "TrailARN": arn,
                    "IsMultiRegionTrail": t.get("IsMultiRegionTrail"),
                    "LogFileValidationEnabled": t.get("LogFileValidationEnabled"),
                    "IsLogging": False,
                }
                with suppress(Exception):
                    entry["IsLogging"] = ct.get_trail_status(Name=arn).get("IsLogging", False)
                trails_by_arn[arn] = entry

        # Flow logs vs VPCs (per region).
        with suppress(Exception):
            ec2 = session.client("ec2", region_name=r)
            vpc_ids = [v["VpcId"] for v in ec2.describe_vpcs().get("Vpcs", [])]
            fl = ec2.describe_flow_logs().get("FlowLogs", [])
            fl_vpcs = {f.get("ResourceId") for f in fl}
            findings += analyze_flow_logs(account_id, r, vpc_ids, fl_vpcs)

        # Log retention (per region).
        with suppress(Exception):
            logs = session.client("logs", region_name=r)
            groups = []
            for page in logs.get_paginator("describe_log_groups").paginate():
                groups.extend(page.get("logGroups", []))
            findings += analyze_retention(account_id, r, groups)

        # Config recorders (per region).
        with suppress(Exception):
            cfg = session.client("config", region_name=r)
            statuses = cfg.describe_configuration_recorder_status().get("ConfigurationRecordersStatus", [])
            if any(s.get("recording") for s in statuses):
                recording_anywhere = True

    findings += analyze_trails(account_id, list(trails_by_arn.values()))
    findings += analyze_config(account_id, recording_anywhere)
    return findings
