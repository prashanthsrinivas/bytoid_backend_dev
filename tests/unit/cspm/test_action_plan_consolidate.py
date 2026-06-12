"""Unit — action plan consolidation: grouping, ranking, suppression, AI merge + fallback."""

from __future__ import annotations

import asyncio
import json

import pytest

import cspm_core.action_plan as ap
from tests.unit.cspm.conftest import sg_finding, snapshot

UID, AID = "u1", "a1"


def _run(ctx, monkeypatch, ai_response=None, ai_error=None):
    async def fake_bedrock(uid, prompt, temp=0.1):
        if ai_error:
            raise ai_error
        return json.dumps(ai_response) if isinstance(ai_response, dict) else ai_response

    monkeypatch.setattr(ap, "_bedrock", fake_bedrock)
    return asyncio.run(ap.build_plan(ctx, UID, AID))


def _ai_ok_for(points_source):
    """Valid AI reply covering every point of a built plan."""
    return {"summary": "Tighten exposed services first.",
            "points": [{"point_id": p["point_id"], "reasoning": f"Because {p['rule_id']}.",
                        "draft_commands": []} for p in points_source]}


def _deterministic_points(ctx):
    snap = ctx.get_snapshot(UID, AID, None)
    return ap._build_points(ctx, UID, AID, snap)


def test_groups_by_rule_and_ranks_by_severity(s3_store, sg_plan_ctx, monkeypatch):
    snap = snapshot([
        sg_finding(fid="f1"), sg_finding(fid="f2", group_id="sg-0aaaaaaaaaaaaaaa1"),
        sg_finding("SG_MISSING_RULE_DESCRIPTION", fid="f3", severity="info"),
    ])
    ctx = sg_plan_ctx(snap)
    plan = _run(ctx, monkeypatch, ai_response=_ai_ok_for(_deterministic_points(ctx)))
    assert plan["status"] == "success"
    points = plan["action_points"]
    assert len(points) == 2
    assert points[0]["rule_id"] == "SG_ADMIN_WORLD_INGRESS"
    assert points[0]["rank"] == 1 and points[0]["severity"] == "critical"
    assert sorted(points[0]["finding_ids"]) == ["f1", "f2"]
    assert len(points[0]["commands"]) == 2
    assert all(c["source"] == "template" for c in points[0]["commands"])
    assert not points[0]["advisory_only"]


def test_suppressed_findings_are_excluded(s3_store, sg_plan_ctx, monkeypatch):
    s3_store[f"{UID}/sg_audit/audits/{AID}.suppressions.json"] = {
        "f1": {"reason": "accepted risk", "at": "2026-06-12T00:00:00Z", "by": UID}}
    ctx = sg_plan_ctx(snapshot([sg_finding(fid="f1"), sg_finding(fid="f2")]))
    plan = _run(ctx, monkeypatch, ai_response=_ai_ok_for(_deterministic_points(ctx)))
    assert plan["action_points"][0]["finding_ids"] == ["f2"]


def test_rationale_falls_back_to_rule_remediation(s3_store, sg_plan_ctx, monkeypatch):
    from sg_audit.metadata import remediation_for

    ctx = sg_plan_ctx(snapshot([sg_finding(fid="f1")]))
    plan = _run(ctx, monkeypatch, ai_error=RuntimeError("bedrock down"))
    point = plan["action_points"][0]
    assert point["reasoning_source"] == "rule-based"
    assert point["reasoning"] == remediation_for("SG_ADMIN_WORLD_INGRESS")
    assert any("AI generation via Bedrock (Kimi 2.5) unavailable" in n for n in plan["notes"])


def test_scan_level_recommendation_feeds_rationale(s3_store, sg_plan_ctx, monkeypatch):
    rec = {"per_finding": [{"finding_id": "f1", "recommended_action": "Close SSH to the VPN CIDR."}]}
    ctx = sg_plan_ctx(snapshot([sg_finding(fid="f1")]), rec=rec)
    plan = _run(ctx, monkeypatch, ai_error=RuntimeError("down"))
    assert plan["action_points"][0]["reasoning"] == "Close SSH to the VPN CIDR."


def test_point_ids_stable_across_regeneration(s3_store, sg_plan_ctx, monkeypatch):
    ctx = sg_plan_ctx(snapshot([sg_finding(fid="f1")]))
    p1 = _run(ctx, monkeypatch, ai_error=RuntimeError("x"))
    p2 = _run(ctx, monkeypatch, ai_error=RuntimeError("x"))
    assert p1["action_points"][0]["point_id"] == p2["action_points"][0]["point_id"]


def test_ai_reasoning_and_valid_draft_merge(s3_store, sg_plan_ctx, monkeypatch):
    # IAM-style rule with no template -> AI may draft a validated command.
    f = sg_finding("SG_UNUSED", fid="f1", severity="low",
                   entity_id="sg-0123456789abcdef0")
    ctx = sg_plan_ctx(snapshot([f]))
    pts = _deterministic_points(ctx)
    ai = {"summary": "One cleanup.",
          "points": [{"point_id": pts[0]["point_id"], "reasoning": "Unused groups widen blast radius.",
                      "draft_commands": [
                          "aws ec2 delete-security-group --group-id sg-0123456789abcdef0",
                          "aws ec2 delete-security-group --group-id sg-9999999999notmine; rm -rf /",
                      ]}]}
    plan = _run(ctx, monkeypatch, ai_response=ai)
    point = plan["action_points"][0]
    assert point["reasoning_source"] == "ai"
    assert point["reasoning"] == "Unused groups widen blast radius."
    drafts = [c for c in point["commands"] if c["source"] == "ai-draft"]
    assert len(drafts) == 1  # the chained/foreign-id command was dropped
    assert drafts[0]["command"].startswith("aws ec2 delete-security-group")
    assert any("failed validation" in n for n in plan["notes"])
    assert not point["advisory_only"]


def test_ai_never_adds_to_template_points(s3_store, sg_plan_ctx, monkeypatch):
    ctx = sg_plan_ctx(snapshot([sg_finding(fid="f1")]))
    pts = _deterministic_points(ctx)
    ai = {"summary": "s",
          "points": [{"point_id": pts[0]["point_id"], "reasoning": "r",
                      "draft_commands": ["aws ec2 revoke-security-group-ingress --group-id sg-0123456789abcdef0"]}]}
    plan = _run(ctx, monkeypatch, ai_response=ai)
    assert all(c["source"] == "template" for c in plan["action_points"][0]["commands"])


def test_empty_snapshot_yields_empty_plan(s3_store, sg_plan_ctx, monkeypatch):
    plan = _run(sg_plan_ctx(snapshot([])), monkeypatch, ai_error=RuntimeError("unused"))
    assert plan["status"] == "success"
    assert plan["action_points"] == []
    assert plan["counts"]["points"] == 0


def test_insufficient_credits_propagates(s3_store, sg_plan_ctx, monkeypatch):
    ctx = sg_plan_ctx(snapshot([sg_finding(fid="f1")]))
    plan = _run(ctx, monkeypatch, ai_response="INSUFFICIENT")
    assert plan["status"] == "insufficient_credits"


def test_no_transient_keys_persist(s3_store, sg_plan_ctx, monkeypatch):
    ctx = sg_plan_ctx(snapshot([sg_finding(fid="f1")]))
    plan = _run(ctx, monkeypatch, ai_error=RuntimeError("x"))
    assert "_blob" not in plan["action_points"][0]
    assert "_details" not in plan["action_points"][0]


@pytest.mark.parametrize("bad", [
    {"summary": "", "points": []},
    {"points": [{"point_id": "nope", "reasoning": "r"}]},
    "not json at all {{{",
])
def test_invalid_ai_reply_falls_back(s3_store, sg_plan_ctx, monkeypatch, bad):
    ctx = sg_plan_ctx(snapshot([sg_finding(fid="f1")]))
    plan = _run(ctx, monkeypatch, ai_response=bad)
    assert plan["status"] == "success"
    assert any("unavailable" in n for n in plan["notes"])
