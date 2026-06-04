"""§4a (logic) — ``actor_eligible_for_state`` eligibility rules.

Pure: direct user-column assignment, role-broadcast (only when the user column
is NULL), direct-takes-precedence, and the draft-owner special case.
"""

from __future__ import annotations

import pytest

from tests.workflow_playbook import _wf_pb_stubs as stubs

stubs.bootstrap_sut()

import workflow_route.state_machine as sm  # noqa: E402

pytestmark = pytest.mark.unit


def test_direct_quality_reviewer_eligible():
    row = {"current_quality_reviewer": "u1"}
    assert sm.actor_eligible_for_state(row, "quality_review", "u1", set()) is True


def test_wrong_quality_reviewer_not_eligible():
    row = {"current_quality_reviewer": "u1"}
    assert sm.actor_eligible_for_state(row, "quality_review", "u2", set()) is False


def test_governance_role_broadcast_member_eligible():
    row = {"current_governance_reviewer": None,
           "current_governance_reviewer_role": "r1"}
    assert sm.actor_eligible_for_state(row, "governance_review", "u1", {"r1"}) is True


def test_governance_role_broadcast_non_member_not_eligible():
    row = {"current_governance_reviewer": None,
           "current_governance_reviewer_role": "r1"}
    assert sm.actor_eligible_for_state(row, "governance_review", "u1", {"r9"}) is False


def test_direct_assignment_takes_precedence_over_role():
    # user column set → role broadcast is bypassed; only the named user is eligible
    row = {"current_governance_reviewer": "named",
           "current_governance_reviewer_role": "r1"}
    assert sm.actor_eligible_for_state(row, "governance_review", "u1", {"r1"}) is False
    assert sm.actor_eligible_for_state(row, "governance_review", "named", {"r1"}) is True


def test_approval_uses_approver_columns():
    row = {"current_approver": None, "current_approver_role": "ra"}
    assert sm.actor_eligible_for_state(row, "approval", "u1", {"ra"}) is True


def test_draft_owner_eligible():
    row = {"owner_user_id": "owner"}
    assert sm.actor_eligible_for_state(row, "draft", "owner", set()) is True
    assert sm.actor_eligible_for_state(row, "draft", "someone", set()) is False
