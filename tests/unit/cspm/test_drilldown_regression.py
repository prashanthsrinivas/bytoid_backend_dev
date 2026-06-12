"""Regression — pin the finding drill-down payload contract.

The action-plan work shares cspm_core.finding_detail; these tests freeze the
response keys the frontend FindingDetailPage depends on so later changes can't
silently drift them.
"""

from __future__ import annotations

from cspm_core.finding_detail import detail_payload, evaluate_guardrail, usage_evidence
from tests.unit.cspm.conftest import sg_finding, snapshot

UID, AID = "u1", "a1"

DETAIL_KEYS = {"status", "scan_id", "scanned_at", "finding", "declarations",
               "messages", "evidence", "recommendation", "last_rescan"}
FINDING_EXTRA_KEYS = {"rule_label", "rule_remediation", "effort", "cis", "status"}
DECLARATION_KEYS = {"business_purpose", "declared_ports", "marked_malicious",
                    "confirmed", "updated_at"}
GUARDRAIL_KEYS = {"allowed", "reasons", "evaluated_at", "evidence_quality"}


def test_detail_payload_contract(s3_store, sg_detail_ctx):
    f = sg_finding(fid="f1")
    body, code = detail_payload(sg_detail_ctx(snapshot([f])), UID, AID, "f1")
    assert code == 200
    assert set(body) == DETAIL_KEYS
    assert FINDING_EXTRA_KEYS <= set(body["finding"])
    assert body["finding"]["status"] == "open"
    assert set(body["declarations"]) == DECLARATION_KEYS
    assert body["recommendation"] is None and body["last_rescan"] is None


def test_detail_404s_unchanged(s3_store, sg_detail_ctx):
    assert detail_payload(sg_detail_ctx(None), UID, AID, "f1")[1] == 404
    assert detail_payload(sg_detail_ctx(snapshot([])), UID, AID, "missing")[1] == 404


def test_usage_evidence_contract(s3_store, sg_detail_ctx):
    ev = usage_evidence(sg_detail_ctx(None), sg_finding(fid="f1"))
    assert {"quality", "reason", "summary", "observed_flows", "collected_at"} <= set(ev)
    assert ev["quality"] in {"observed", "partial", "none", "not-applicable"}


def test_guardrail_contract():
    verdict = evaluate_guardrail([], {"quality": "not-applicable"}, None)
    assert set(verdict) == GUARDRAIL_KEYS
    assert verdict["allowed"] is True
    assert verdict["reasons"][0]["code"] == "OK"
