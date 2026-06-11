"""GCP domain analyzer tests on synthetic REST payloads (no network).

Run with::

    python -m gcp_audit.tests.test_domains

Exits non-zero on the first failure. Also exercises GCP_PROVIDER through the
generic engine (build_snapshot/score/compliance).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from cspm_core import score
from cspm_core.compliance import coverage_for
from cspm_core.normalize import build_snapshot, validate_finding
from gcp_audit.domains import compute, data, iam, logging_domain, network
from gcp_audit.metadata import RULE_META
from gcp_audit.provider import GCP_PROVIDER

PROJ = "my-project"
_checks = 0


def ok(cond, label):
    global _checks
    _checks += 1
    if not cond:
        print(f"FAIL: {label}")
        sys.exit(1)


def rules(findings):
    return {f["rule_id"] for f in findings}


def test_metadata_integrity():
    for rid, m in RULE_META.items():
        ok(m["domain"] in GCP_PROVIDER.domains, f"{rid} domain valid")
        ok(m["severity"] in ("info", "low", "medium", "high", "critical"), f"{rid} severity valid")
        ok(m["effort"] in ("low", "medium", "high"), f"{rid} effort valid")


def test_network():
    fws = [
        {"name": "ssh-open", "direction": "INGRESS", "sourceRanges": ["0.0.0.0/0"],
         "allowed": [{"IPProtocol": "tcp", "ports": ["22"]}]},
        {"name": "sql-open", "direction": "INGRESS", "sourceRanges": ["0.0.0.0/0"],
         "allowed": [{"IPProtocol": "tcp", "ports": ["5432"]}]},
        {"name": "all-open", "direction": "INGRESS", "sourceRanges": ["0.0.0.0/0"],
         "allowed": [{"IPProtocol": "all"}]},
        {"name": "internal", "direction": "INGRESS", "sourceRanges": ["10.0.0.0/8"],
         "allowed": [{"IPProtocol": "tcp", "ports": ["443"]}]},
        {"name": "disabled", "direction": "INGRESS", "sourceRanges": ["0.0.0.0/0"], "disabled": True,
         "allowed": [{"IPProtocol": "tcp", "ports": ["22"]}]},
    ]
    f = network.analyze_firewalls(fws, PROJ, "Prod")
    r = rules(f)
    ok("GCP_FW_ADMIN_WORLD_OPEN" in r, "fw admin world open (22)")
    ok("GCP_FW_DB_WORLD_OPEN" in r, "fw db world open (5432)")
    ok("GCP_FW_ALL_PORTS_WORLD" in r, "fw all ports/protocols world open")
    ok(len(f) == 3, "internal + disabled rules ignored")
    # tcp with no ports == all ports
    ok("GCP_FW_ALL_PORTS_WORLD" in rules(network.analyze_firewalls(
        [{"name": "x", "sourceRanges": ["0.0.0.0/0"], "allowed": [{"IPProtocol": "tcp"}]}], PROJ)),
       "fw tcp no-ports == all ports")
    nets = [{"id": "1", "name": "default"}, {"id": "2", "name": "custom-vpc"}]
    nf = network.analyze_networks(nets, PROJ)
    ok("GCP_DEFAULT_NETWORK" in rules(nf) and len(nf) == 1, "only default network flagged")


def test_iam():
    policy = {"bindings": [
        {"role": "roles/storage.objectViewer", "members": ["allUsers"]},
        {"role": "roles/owner", "members": ["user:admin@x.com", "serviceAccount:sa@x.iam"]},
        {"role": "roles/viewer", "members": ["user:dev@x.com"]},
    ]}
    f = iam.analyze_iam_policy(policy, PROJ)
    r = rules(f)
    ok("GCP_IAM_PUBLIC_MEMBER" in r, "iam public allUsers binding")
    ok("GCP_IAM_PRIMITIVE_ROLE" in r, "iam primitive owner to user")
    ok(not any(x["rule_id"] == "GCP_IAM_PRIMITIVE_ROLE" and x["supporting_details"]["role"] == "roles/viewer"
               for x in f), "viewer (non-primitive) not flagged")
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    keys = [{"name": "p/k1", "keyType": "USER_MANAGED", "validAfterTime": "2023-01-01T00:00:00Z"},
            {"name": "p/k2", "keyType": "USER_MANAGED", "validAfterTime": "2023-12-20T00:00:00Z"},
            {"name": "p/k3", "keyType": "SYSTEM_MANAGED", "validAfterTime": "2020-01-01T00:00:00Z"}]
    kf = iam.analyze_sa_keys(keys, "sa@x.iam", PROJ, now=now)
    ok("GCP_SA_USER_MANAGED_KEY_STALE" in rules(kf) and len(kf) == 1, "only old user-managed key flagged")


def test_data():
    bkt = {"id": "b1", "name": "public-bucket",
           "iamConfiguration": {"uniformBucketLevelAccess": {"enabled": False}}}
    iam_pol = {"bindings": [{"role": "roles/storage.objectViewer", "members": ["allAuthenticatedUsers"]}]}
    f = data.analyze_bucket(bkt, iam_pol, PROJ)
    ok({"GCP_BUCKET_PUBLIC", "GCP_BUCKET_NO_UNIFORM_ACCESS"} <= rules(f), "bucket public + no uniform access")
    ok(data.analyze_bucket({"name": "ok", "iamConfiguration": {"uniformBucketLevelAccess": {"enabled": True}}},
                           {"bindings": [{"role": "roles/viewer", "members": ["user:x@y.com"]}]}, PROJ) == [],
       "hardened private bucket clean")
    sql = [{"name": "db1", "settings": {"ipConfiguration": {"ipv4Enabled": True, "requireSsl": False}}}]
    ok({"GCP_SQL_PUBLIC_IP", "GCP_SQL_NO_SSL"} <= rules(data.analyze_sql_instances(sql, PROJ)), "sql public + no ssl")


def test_compute():
    inst = [{"id": "i1", "name": "vm1", "zone": "x/us-central1-a",
             "networkInterfaces": [{"accessConfigs": [{"type": "ONE_TO_ONE_NAT", "natIP": "1.2.3.4"}]}],
             "serviceAccounts": [{"email": "123-compute@developer.gserviceaccount.com",
                                  "scopes": ["https://www.googleapis.com/auth/cloud-platform"]}],
             "metadata": {"items": []}}]
    r = rules(compute.analyze_instances(inst, PROJ))
    ok({"GCP_INSTANCE_PUBLIC_IP", "GCP_OS_LOGIN_DISABLED", "GCP_DEFAULT_SA_FULL_SCOPE", "GCP_SHIELDED_VM_OFF"} <= r,
       "compute all four rules")
    hard = [{"id": "i2", "name": "vm2", "zone": "x/z",
             "networkInterfaces": [{"accessConfigs": []}],
             "metadata": {"items": [{"key": "enable-oslogin", "value": "TRUE"}]},
             "serviceAccounts": [{"email": "custom@x.iam", "scopes": ["https://www.googleapis.com/auth/devstorage.read_only"]}],
             "shieldedInstanceConfig": {"enableSecureBoot": True, "enableVtpm": True, "enableIntegrityMonitoring": True}}]
    ok(compute.analyze_instances(hard, PROJ) == [], "hardened instance clean")


def test_logging():
    ok("GCP_AUDIT_LOGGING_INCOMPLETE" in rules(logging_domain.analyze_audit_configs({}, PROJ)),
       "no audit configs → incomplete")
    full = {"auditConfigs": [{"service": "allServices", "auditLogConfigs": [
        {"logType": "DATA_READ"}, {"logType": "DATA_WRITE"}, {"logType": "ADMIN_READ"}]}]}
    ok(logging_domain.analyze_audit_configs(full, PROJ) == [], "full audit config clean")
    ok("GCP_NO_LOG_SINK" in rules(logging_domain.analyze_sinks([], PROJ)), "no sink flagged")
    ok(logging_domain.analyze_sinks([{"name": "s"}], PROJ) == [], "sink present clean")


def test_provider_engine_wiring():
    f = []
    f += network.analyze_firewalls([{"name": "ssh", "sourceRanges": ["0.0.0.0/0"],
                                     "allowed": [{"IPProtocol": "tcp", "ports": ["22"]}]}], PROJ, "Prod")
    f += iam.analyze_iam_policy({"bindings": [{"role": "roles/editor", "members": ["allUsers"]}]}, PROJ, "Prod")
    ok(all(validate_finding(x) for x in f), "all gcp findings validate")
    snap = build_snapshot(scan_id="s", audit_id="a", findings=f, scopes_scanned=[PROJ])
    g = score.global_posture(snap, GCP_PROVIDER)
    ok(g["rating"] == "Critical", "gcp snapshot rating Critical")
    cis = coverage_for(snap, GCP_PROVIDER, "CIS")
    ok(cis["framework_label"] == "CIS Google Cloud Platform Foundation", "gcp CIS label")
    ok(any(c["control"] == "1.1" for c in cis["controls"]), "gcp CIS includes control 1.1")


def main():
    test_metadata_integrity()
    test_network()
    test_iam()
    test_data()
    test_compute()
    test_logging()
    test_provider_engine_wiring()
    print(f"GCP_AUDIT OK — {_checks} assertions passed")


if __name__ == "__main__":
    main()
