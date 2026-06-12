"""Unit — human edit of action-point commands + approval-hash divergence flag."""

from __future__ import annotations

from cspm_core.action_plan import (
    _commands_hash,
    edit_command_payload,
    plan_get_payload,
)
from cspm_core.finding_detail import _save_sidecar

UID, AID, PID = "u1", "a1", "p1"


def _seed_plan(ctx, *, approval=None):
    plan = {"status": "success", "audit_id": AID, "scan_id": "scan-1",
            "action_points": [{
                "point_id": PID, "rule_id": "SG_ADMIN_WORLD_INGRESS",
                "commands": [{"command": "aws ec2 revoke-security-group-ingress --group-id sg-1",
                              "source": "template", "finding_id": "f1",
                              "irreversible": False, "edited": False}],
                "approval": approval,
            }]}
    _save_sidecar(ctx, UID, AID, "action_plan", plan)
    return plan


def test_edit_persists_and_marks_edited(s3_store, sg_plan_ctx):
    ctx = sg_plan_ctx(None)
    _seed_plan(ctx)
    body, code = edit_command_payload(ctx, UID, AID, PID, 0,
                                      "aws ec2 revoke-security-group-ingress --group-id sg-1 --dry-run")
    assert code == 200
    cmd = body["point"]["commands"][0]
    assert cmd["edited"] is True and cmd["edited_by"] == UID and cmd["edited_at"]
    assert cmd["command"].endswith("--dry-run")
    # persisted: a fresh GET sees the edit
    got, _ = plan_get_payload(ctx, UID, AID)
    assert got["plan"]["action_points"][0]["commands"][0]["edited"] is True


def test_edit_rejects_multiline_and_oversized(s3_store, sg_plan_ctx):
    ctx = sg_plan_ctx(None)
    _seed_plan(ctx)
    assert edit_command_payload(ctx, UID, AID, PID, 0, "aws ec2 x\naws s3 y")[1] == 400
    assert edit_command_payload(ctx, UID, AID, PID, 0, "aws " + "x" * 600)[1] == 400
    assert edit_command_payload(ctx, UID, AID, PID, 0, "   ")[1] == 400


def test_edit_invalid_index_or_point(s3_store, sg_plan_ctx):
    ctx = sg_plan_ctx(None)
    _seed_plan(ctx)
    assert edit_command_payload(ctx, UID, AID, PID, 5, "aws x y")[1] == 400
    assert edit_command_payload(ctx, UID, AID, PID, "nan", "aws x y")[1] == 400
    assert edit_command_payload(ctx, UID, AID, "missing", 0, "aws x y")[1] == 404


def test_edit_after_approval_request_sets_flag(s3_store, sg_plan_ctx):
    ctx = sg_plan_ctx(None)
    plan = _seed_plan(ctx, approval={"workflow_id": "wf-1", "state": "quality_review"})
    point = plan["action_points"][0]
    point["approval"]["command_hash"] = _commands_hash(point)
    _save_sidecar(ctx, UID, AID, "action_plan", plan)

    body, code = edit_command_payload(ctx, UID, AID, PID, 0, "aws ec2 something-else")
    assert code == 200
    assert body["point"]["approval"]["edited_after_request"] is True


def test_edit_before_request_does_not_flag(s3_store, sg_plan_ctx):
    ctx = sg_plan_ctx(None)
    _seed_plan(ctx, approval=None)
    body, code = edit_command_payload(ctx, UID, AID, PID, 0, "aws ec2 something-else")
    assert code == 200
    assert body["point"]["approval"] is None
