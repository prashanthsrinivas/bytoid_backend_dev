"""Unit — architecture graph derivation from a posture snapshot."""

from __future__ import annotations

from cspm_core.architecture import architecture_payload, build_architecture
from tests.unit.cspm.conftest import sg_finding, snapshot

UID, AID = "u1", "a1"


def _model(ctx, snap):
    return build_architecture(ctx, UID, AID, snap)


def test_hierarchy_counts_and_max_severity(s3_store, sg_detail_ctx):
    snap = snapshot([
        sg_finding(fid="f1", severity="critical"),
        sg_finding("SG_MISSING_RULE_DESCRIPTION", fid="f2", severity="info"),
        sg_finding("SG_BROAD_EGRESS_ALL", fid="f3", severity="medium",
                   group_id="sg-0bbbbbbbbbbbbbbb2", region="us-east-1"),
    ])
    model = _model(sg_detail_ctx(snap), snap)
    assert model["totals"] == {
        "scopes": 1, "regions": 2, "entities": 2, "findings": 3,
        "by_severity": {"critical": 1, "high": 0, "medium": 1, "low": 0, "info": 1}}
    scope = model["scopes"][0]
    assert scope["id"] == "394711685916" and scope["name"] == "Test Account"
    regions = {r["name"]: r for r in scope["regions"]}
    assert set(regions) == {"ca-central-1", "us-east-1"}
    ent = regions["ca-central-1"]["entities"][0]
    assert ent["max_severity"] == "critical"
    assert ent["counts_by_severity"]["critical"] == 1
    assert ent["findings"][0]["severity"] == "critical"  # worst-first
    assert ent["findings"][0]["rule_label"]


def test_suppressed_findings_flagged_not_dropped(s3_store, sg_detail_ctx):
    s3_store[f"{UID}/sg_audit/audits/{AID}.suppressions.json"] = {
        "f1": {"reason": "ok", "at": "2026-06-12T00:00:00Z", "by": UID}}
    snap = snapshot([sg_finding(fid="f1")])
    model = _model(sg_detail_ctx(snap), snap)
    finding = model["scopes"][0]["regions"][0]["entities"][0]["findings"][0]
    assert finding["suppressed"] is True


def test_relation_edges_only_between_existing_entities(s3_store, sg_detail_ctx):
    inst = sg_finding("EC2_IMDSV1_ENABLED", fid="f1", severity="high",
                      entity_type="ec2_instance", entity_id="i-007759a63defdc275",
                      entity_name="vpnbox",
                      extra_details={"group_id": "sg-0123456789abcdef0"})
    sg = sg_finding(fid="f2")  # entity_id == sg-0123456789abcdef0
    model = _model(sg_detail_ctx(snapshot([inst, sg])), snapshot([inst, sg]))
    rels = [(r["source"], r["target"]) for r in model["relations"]]
    assert ("i-007759a63defdc275", "sg-0123456789abcdef0") in rels
    # vpc-00000001 has no entity node -> no dangling edge
    assert all(t != "vpc-00000001" for _, t in rels)


def test_empty_snapshot_valid_model(s3_store, sg_detail_ctx):
    snap = snapshot([])
    model = _model(sg_detail_ctx(snap), snap)
    assert model["scopes"] == [] and model["relations"] == []
    assert model["totals"]["findings"] == 0


def test_payload_404_without_scan(s3_store, sg_detail_ctx):
    body, code = architecture_payload(sg_detail_ctx(None), UID, AID)
    assert code == 404 and body["status"] == "error"


def test_payload_success_shape(s3_store, sg_detail_ctx):
    snap = snapshot([sg_finding(fid="f1")])
    body, code = architecture_payload(sg_detail_ctx(snap), UID, AID)
    assert code == 200
    assert set(body) == {"status", "scan_id", "scanned_at", "scopes", "relations", "totals"}
    assert body["scan_id"] == "scan-1"
