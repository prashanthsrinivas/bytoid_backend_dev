"""Provider-agnostic engine tests for ``cspm_core`` via a synthetic fake provider.

Covers the generic layers that every cloud reuses: the finding/snapshot contract
(normalize), scoring + priority + global rollup (score), multi-framework
compliance coverage math (compliance), the table → tracker/evidence mappers
(exports), and the pure dashboard/report builders. No cloud SDK, DB, S3, or Redis
— a fake ``Provider`` supplies rule metadata and trivial collection callables.

Run with::

    python -m cspm_core.tests.test_engine

Exits non-zero on the first failure.
"""

from __future__ import annotations

import sys

from cspm_core import score
from cspm_core.compliance import FRAMEWORKS, all_frameworks, coverage_for
from cspm_core.dashboard import build_dashboard
from cspm_core.exports import TABLES, build_table
from cspm_core.normalize import build_snapshot, make_domain_finding, validate_finding
from cspm_core.provider import Provider
from cspm_core.report_inputs import build_report

_checks = 0


def ok(cond, label):
    global _checks
    _checks += 1
    if not cond:
        print(f"FAIL: {label}")
        sys.exit(1)


# ── a synthetic provider (two subscriptions, four domains) ──────────────────
FAKE_RULE_META = {
    "FAKE_NSG_ADMIN_OPEN": {
        "domain": "network", "category": "network_exposure", "severity": "critical",
        "exploitability": 3, "blast_radius": 3, "effort": "low",
        "label": "NSG admin port open to Internet",
        "remediation": "Restrict NSG inbound to known CIDRs.", "cis": ["6.1"], "soc2": [], "iso": [],
    },
    "FAKE_OWNER_EVERYWHERE": {
        "domain": "identity", "category": "identity", "severity": "high",
        "exploitability": 2, "blast_radius": 3, "effort": "medium",
        "label": "Subscription Owner assignment", "remediation": "Use least-privilege RBAC.",
        "cis": ["1.23"], "soc2": [], "iso": [],
    },
    "FAKE_STORAGE_PUBLIC": {
        "domain": "data", "category": "public_access", "severity": "high",
        "exploitability": 3, "blast_radius": 2, "effort": "low",
        "label": "Storage account allows public blob access",
        "remediation": "Disable public blob access.", "cis": ["3.7"], "soc2": [], "iso": [],
    },
    "FAKE_NO_DIAG": {
        "domain": "logging", "category": "logging", "severity": "medium",
        "exploitability": 1, "blast_radius": 1, "effort": "medium",
        "label": "No diagnostic settings", "remediation": "Enable diagnostic settings.",
        "cis": ["5.3"], "soc2": [], "iso": [],
    },
}

FAKE_PROVIDER = Provider(
    key="fake", label="Fake Cloud", route_prefix="fake-audit", s3_namespace="fake_audit",
    redis_namespace="fake_audit",
    domains=("network", "identity", "data", "logging"),
    domain_labels={"network": "Network", "identity": "Identity", "data": "Data", "logging": "Logging"},
    rule_meta=FAKE_RULE_META, cis_label="CIS Fake Cloud Foundations",
    cis_families={"1": "Identity & Access", "3": "Storage", "5": "Logging & Monitoring", "6": "Networking"},
    perms={"create": "fake.audit.create", "findings_read": "fake.findings.read",
           "dashboard_read": "fake.dashboard.read", "recommend": "fake.recommend.generate",
           "remediation": "fake.remediation.request"},
    resolve_credentials=lambda user_id: {"token": "x"},
    enumerate_scopes=lambda creds, scope_filter=None: [{"id": "sub-a", "name": "Prod"}, {"id": "sub-b", "name": "Dev"}],
    collect=lambda creds, scope, domains=None: ([], {}),
    scope_label="subscription", default_audit_name="Fake Posture Audit",
)


def _fake_findings():
    return [
        make_domain_finding(rule_meta=FAKE_RULE_META, rule_id="FAKE_NSG_ADMIN_OPEN", severity="critical",
                            finding_summary="NSG nsg-1 allows 0.0.0.0/0 → 22", scope_id="sub-a", scope_name="Prod",
                            region="eastus", entity_type="nsg", entity_id="nsg-1", entity_name="nsg-1", source="fake"),
        make_domain_finding(rule_meta=FAKE_RULE_META, rule_id="FAKE_OWNER_EVERYWHERE", severity="high",
                            finding_summary="user@x has Owner on sub-a", scope_id="sub-a", scope_name="Prod",
                            entity_type="role_assignment", entity_id="ra-1", entity_name="user@x", source="fake"),
        make_domain_finding(rule_meta=FAKE_RULE_META, rule_id="FAKE_STORAGE_PUBLIC", severity="high",
                            finding_summary="storage acct stor1 allows public blobs", scope_id="sub-b", scope_name="Dev",
                            region="westus", entity_type="storage_account", entity_id="stor1", entity_name="stor1",
                            source="fake"),
        make_domain_finding(rule_meta=FAKE_RULE_META, rule_id="FAKE_NO_DIAG", severity="medium",
                            finding_summary="sub-b has no diagnostic settings", scope_id="sub-b", scope_name="Dev",
                            entity_type="subscription", entity_id="sub-b", entity_name="Dev", source="fake"),
    ]


def _fake_snapshot():
    return build_snapshot(scan_id="scan1", audit_id="aud1", findings=_fake_findings(),
                          scopes_scanned=["sub-a", "sub-b"],
                          collector_status={"sub-a:network": "ok", "sub-b:data": "ok"},
                          scope={"scope_ids": ["sub-a", "sub-b"], "domains": list(FAKE_PROVIDER.domains)})


# ── normalize / contract ─────────────────────────────────────────────────────
def test_normalize():
    f = _fake_findings()[0]
    ok(f["finding_id"] == "sub-a:network:FAKE_NSG_ADMIN_OPEN:nsg-1", "finding_id composite format")
    ok(f["domain"] == "network" and f["category"] == "network_exposure", "domain+category pulled from rule_meta")
    ok(f["supporting_details"]["scope_id"] == "sub-a", "scope_id in supporting_details")
    ok(f["supporting_details"]["scope_name"] == "Prod", "scope_name preserved")
    ok(f["risk_indicators"] == ["FAKE_NSG_ADMIN_OPEN"], "risk_indicators = [rule_id]")
    ok(all(validate_finding(x) for x in _fake_findings()), "all fake findings validate")


def test_snapshot_counts():
    snap = _fake_snapshot()
    ok(snap["counts"]["total"] == 4, "snapshot total = 4")
    ok(snap["counts"]["by_severity"]["critical"] == 1, "1 critical")
    ok(snap["counts"]["by_severity"]["high"] == 2, "2 high")
    ok(set(snap["counts"]["by_domain"].keys()) == {"network", "identity", "data", "logging"}, "4 domains counted")
    # risk_score = 0.7*max + 0.3*mean of weights [100,65,65,30] → 0.7*100 + 0.3*65 = 89.5
    ok(snap["risk_score"] == 89.5, f"risk_score 89.5 (got {snap['risk_score']})")
    ok(snap["posture_score"] == 10.5, "posture_score = 100 - risk")


# ── scoring / priority / rollup ─────────────────────────────────────────────
def test_scoring():
    f = _fake_findings()
    pd = score.per_domain(f, FAKE_PROVIDER)
    ok(pd[0]["domain"] == "network", "per_domain sorted: network (critical) first")
    ok(pd[0]["rating"] == "Critical", "network domain rated Critical")
    ok(pd[0]["label"] == "Network", "per_domain label from provider")

    # NSG admin-open: sev_w=100, (3+3)/6*100=100, ease low=100 → 0.5*100+0.3*100+0.2*100 = 100.0
    nsg = f[0]
    ok(score.priority_score(nsg, FAKE_PROVIDER) == 100.0, "NSG admin-open priority = 100.0")

    q = score.remediation_priority_queue(f, FAKE_PROVIDER, 50)
    ok(q[0]["rule_id"] == "FAKE_NSG_ADMIN_OPEN" and q[0]["rank"] == 1, "priority queue rank 1 = NSG admin-open")
    ok(q[0]["remediation"] == "Restrict NSG inbound to known CIDRs.", "queue carries remediation text")
    ok(q[0]["cis"] == ["6.1"], "queue carries CIS controls")

    tc = score.top_critical(f, FAKE_PROVIDER, 10)
    ok(len(tc) == 3, "top_critical = 3 (1 critical + 2 high; medium excluded)")

    g = score.global_posture(_fake_snapshot(), FAKE_PROVIDER)
    ok(g["overall_risk_score"] == 89.5, "global overall_risk_score")
    ok(g["rating"] == "Critical", "global rating Critical (has critical)")
    ok(len(g["top_10_critical"]) == 3 and g["risk_by_domain"][0]["domain"] == "network", "global rollup shapes")


# ── compliance ───────────────────────────────────────────────────────────────
def test_compliance():
    snap = _fake_snapshot()
    cis = coverage_for(snap, FAKE_PROVIDER, "CIS")
    ok(cis["framework_label"] == "CIS Fake Cloud Foundations", "CIS label from provider")
    ok(cis["evaluated"] == 4, "4 distinct CIS controls evaluated")
    ok(cis["passing"] == 0 and cis["failing"] == 4, "all 4 CIS controls failing (each has a finding)")
    ok(cis["coverage_pct"] == 0.0, "CIS coverage 0%")
    fams = {h["family"] for h in cis["heatmap"]}
    ok(fams == {"1", "3", "5", "6"}, "CIS heatmap families from control prefixes")

    soc2 = coverage_for(snap, FAKE_PROVIDER, "SOC2")
    ok(soc2["evaluated"] == 7, f"SOC2 evaluates 7 controls (got {soc2['evaluated']})")
    ok(soc2["passing"] == 1, "SOC2: only CC7.1 (monitoring/patch) passes — no such findings")
    cc71 = next(c for c in soc2["controls"] if c["control"] == "CC7.1")
    ok(cc71["status"] == "pass", "CC7.1 passes")

    iso = coverage_for(snap, FAKE_PROVIDER, "ISO27001")
    ok(iso["framework"] == "ISO27001" and iso["evaluated"] > 0, "ISO27001 produces controls")

    allf = all_frameworks(snap, FAKE_PROVIDER)
    ok({a["framework"] for a in allf} == set(FRAMEWORKS), "all_frameworks returns CIS+SOC2+ISO")


# ── exports (tracker columns/rows + canonical evidence) ─────────────────────
def test_exports():
    snap = _fake_snapshot()
    ok(set(TABLES) == {"findings", "per_scope", "per_domain", "compliance", "priority_queue", "remediations"},
       "TABLES registry")

    findings_t = build_table(FAKE_PROVIDER, snap, "findings")
    ok(len(findings_t["rows"]) == 4, "findings table has 4 rows")
    ok(any(c["name"] == "Subscription" for c in findings_t["columns"]), "scope column titled 'Subscription'")
    ok(len(findings_t["evidence"]) == 4, "findings evidence has 4 records")
    ok(all(e["source"] == "fake" for e in findings_t["evidence"]), "evidence source = provider.key")

    ps = build_table(FAKE_PROVIDER, snap, "per_scope")
    ok(len(ps["rows"]) == 2, "per_scope: 2 subscriptions")

    pd = build_table(FAKE_PROVIDER, snap, "per_domain")
    ok(len(pd["rows"]) == 4, "per_domain: 4 domains")

    comp = build_table(FAKE_PROVIDER, snap, "compliance", framework="CIS")
    ok(len(comp["rows"]) == 4, "compliance table: 4 CIS controls")

    pq = build_table(FAKE_PROVIDER, snap, "priority_queue")
    ok(pq["rows"][0]["Rule"] == "NSG admin port open to Internet", "priority_queue first row = NSG admin-open")

    rem = build_table(FAKE_PROVIDER, snap, "remediations", remediation_links={})
    ok(rem["rows"] == [], "remediations empty with no links")

    ok(build_table(FAKE_PROVIDER, snap, "nonexistent") is None, "unknown table → None")


# ── pure dashboard + report builders ────────────────────────────────────────
def test_dashboard_and_report():
    snap = _fake_snapshot()
    rec = {"audit_id": "aud1", "name": "Fake Posture Audit", "scan_state": "complete"}
    dash = build_dashboard(FAKE_PROVIDER, rec, snap, [], prior_scores=[])
    ok(dash["executive_summary"]["total_findings"] == 4, "dashboard executive total")
    ok("per_domain" in dash and "compliance_frameworks" in dash, "dashboard has per_domain + frameworks")
    ok(dash["global"]["rating"] == "Critical", "dashboard global rating")

    md = build_report(FAKE_PROVIDER, snap, rec, [])
    ok(isinstance(md, str) and "Fake Cloud" in md, "report markdown mentions provider label")
    ok("Critical" in md or "critical" in md, "report mentions criticality")


def main():
    test_normalize()
    test_snapshot_counts()
    test_scoring()
    test_compliance()
    test_exports()
    test_dashboard_and_report()
    print(f"CSPM_CORE ENGINE OK — {_checks} assertions passed")


if __name__ == "__main__":
    main()
