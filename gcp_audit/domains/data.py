"""Data domain — Cloud Storage buckets + Cloud SQL instances.

Pure analyzers evaluate a parsed bucket resource + its IAM policy, and Cloud SQL
instance payloads, for public access / weak transport.
"""

from __future__ import annotations

from cspm_core.normalize import make_domain_finding
from gcp_audit.metadata import RULE_META

_PUBLIC = {"allusers", "allauthenticatedusers"}


def _f(rule_id, severity, summary, project_id, project_name, etype, eid, ename, details=None):
    return make_domain_finding(
        rule_meta=RULE_META, rule_id=rule_id, severity=severity, finding_summary=summary,
        scope_id=project_id, scope_name=project_name, entity_type=etype, entity_id=eid,
        entity_name=ename, source="gcp", details=details or {})


def analyze_bucket(bucket, iam_policy, project_id, project_name="") -> list:
    findings = []
    name = bucket.get("name", "")
    bid = bucket.get("id") or name
    uniform = ((bucket.get("iamConfiguration", {}) or {}).get("uniformBucketLevelAccess", {}) or {}).get("enabled")
    if uniform is False:
        findings.append(_f("GCP_BUCKET_NO_UNIFORM_ACCESS", "low",
                           f"Bucket '{name}' has uniform bucket-level access disabled",
                           project_id, project_name, "bucket", bid, name))
    members = set()
    for binding in (iam_policy or {}).get("bindings", []) or []:
        members |= {m.lower() for m in (binding.get("members", []) or [])}
    if members & _PUBLIC:
        findings.append(_f("GCP_BUCKET_PUBLIC", "high",
                           f"Bucket '{name}' is publicly accessible (allUsers / allAuthenticatedUsers)",
                           project_id, project_name, "bucket", bid, name,
                           details={"public_members": sorted(members & _PUBLIC)}))
    return findings


def analyze_sql_instances(instances, project_id, project_name="") -> list:
    findings = []
    for inst in instances or []:
        name = inst.get("name", "")
        iid = inst.get("name") or name
        ipc = (inst.get("settings", {}) or {}).get("ipConfiguration", {}) or {}
        if ipc.get("ipv4Enabled") is True:
            findings.append(_f("GCP_SQL_PUBLIC_IP", "high",
                               f"Cloud SQL instance '{name}' has a public IPv4 address",
                               project_id, project_name, "sql_instance", iid, name))
        if ipc.get("requireSsl") is False:
            findings.append(_f("GCP_SQL_NO_SSL", "medium",
                               f"Cloud SQL instance '{name}' does not require SSL",
                               project_id, project_name, "sql_instance", iid, name))
    return findings


def collect(creds, project_id, project_name="") -> list:
    from gcp_audit.rest import SQLADMIN_V1, STORAGE_V1, get, list_items
    findings = []
    for bkt in list_items(creds, f"{STORAGE_V1}/b", params={"project": project_id}):
        try:
            iam = get(creds, f"{STORAGE_V1}/b/{bkt.get('name')}/iam")
        except Exception:
            iam = {}
        findings += analyze_bucket(bkt, iam, project_id, project_name)
    findings += analyze_sql_instances(
        list_items(creds, f"{SQLADMIN_V1}/projects/{project_id}/instances"), project_id, project_name)
    return findings
