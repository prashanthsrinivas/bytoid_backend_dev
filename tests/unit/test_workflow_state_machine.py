"""Unit tests for workflow_route/state_machine.py pure helpers.

Pure-logic helpers (forward chain, user-column resolution, eligibility checks)
need no DB. DB-touching functions are stubbed.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

# Stub DB before import
for _mod in ("pymysql", "pymysql.cursors", "db", "db.rds_db", "db.db_checkers",
             "boto3", "dotenv", "dbutils", "dbutils.pooled_db"):
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

import workflow_route.state_machine as sm  # noqa: E402


# ── _next_forward_state ──────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("state,expected", [
    ("draft", "quality_review"),
    ("quality_review", "governance_review"),
    ("governance_review", "approval"),
    ("approval", "published"),
    ("published", None),
    ("unknown", None),
    ("", None),
])
def test_next_forward_state(state, expected):
    assert sm._next_forward_state(state) == expected


# ── _is_forward_hop ──────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("from_state,to_state,expected", [
    ("draft", "quality_review", True),
    ("quality_review", "governance_review", True),
    ("governance_review", "approval", True),
    ("approval", "published", True),
    # Reverse hops are not forward
    ("quality_review", "draft", False),
    ("governance_review", "quality_review", False),
    ("approval", "governance_review", False),
    ("published", "draft", False),
    # Skipping ahead is not a forward hop in the linear sense
    ("draft", "governance_review", False),
    ("draft", "approval", False),
    # Unknown
    ("foo", "bar", False),
    ("", "", False),
])
def test_is_forward_hop(from_state, to_state, expected):
    assert sm._is_forward_hop(from_state, to_state) is expected


# ── _user_col_for_state ──────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("state,expected", [
    ("quality_review", "current_quality_reviewer"),
    ("governance_review", "current_governance_reviewer"),
    ("approval", "current_approver"),
    ("published", "current_approver"),
    ("draft", None),
    ("unknown", None),
    ("", None),
])
def test_user_col_for_state(state, expected):
    assert sm._user_col_for_state(state) == expected


# ── _role_col_for_state ──────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("state,expected", [
    ("governance_review", "current_governance_reviewer_role"),
    ("approval", "current_approver_role"),
    ("published", "current_approver_role"),
    ("draft", None),
    ("quality_review", None),
    ("unknown", None),
])
def test_role_col_for_state(state, expected):
    assert sm._role_col_for_state(state) == expected


# ── _assignee_for_state ──────────────────────────────────────────────────────

@pytest.mark.unit
def test_assignee_quality_review():
    row = {"current_quality_reviewer": "u1"}
    assert sm._assignee_for_state(row, "quality_review") == "u1"

@pytest.mark.unit
def test_assignee_governance_review():
    row = {"current_governance_reviewer": "u2"}
    assert sm._assignee_for_state(row, "governance_review") == "u2"

@pytest.mark.unit
@pytest.mark.parametrize("state", ["approval", "published"])
def test_assignee_approval_states(state):
    row = {"current_approver": "u3"}
    assert sm._assignee_for_state(row, state) == "u3"

@pytest.mark.unit
def test_assignee_draft():
    row = {"owner_user_id": "owner"}
    assert sm._assignee_for_state(row, "draft") == "owner"

@pytest.mark.unit
def test_assignee_unknown_state():
    assert sm._assignee_for_state({}, "unknown") is None

@pytest.mark.unit
def test_assignee_missing_field_returns_none():
    assert sm._assignee_for_state({}, "quality_review") is None


# ── actor_eligible_for_state ─────────────────────────────────────────────────

@pytest.mark.unit
def test_actor_eligible_direct_assignment():
    row = {"current_quality_reviewer": "userA"}
    assert sm.actor_eligible_for_state(row, "quality_review", "userA", set()) is True

@pytest.mark.unit
def test_actor_not_eligible_different_user():
    row = {"current_quality_reviewer": "userA"}
    assert sm.actor_eligible_for_state(row, "quality_review", "userB", set()) is False

@pytest.mark.unit
def test_actor_eligible_via_role_when_user_null():
    row = {
        "current_governance_reviewer": None,
        "current_governance_reviewer_role": "role1",
    }
    assert sm.actor_eligible_for_state(row, "governance_review", "anyUser", {"role1"}) is True

@pytest.mark.unit
def test_actor_not_eligible_role_mismatch():
    row = {
        "current_governance_reviewer": None,
        "current_governance_reviewer_role": "role1",
    }
    assert sm.actor_eligible_for_state(row, "governance_review", "x", {"role2"}) is False

@pytest.mark.unit
def test_actor_eligible_draft_owner():
    row = {"owner_user_id": "owner"}
    assert sm.actor_eligible_for_state(row, "draft", "owner", set()) is True

@pytest.mark.unit
def test_actor_not_eligible_draft_non_owner():
    row = {"owner_user_id": "owner"}
    assert sm.actor_eligible_for_state(row, "draft", "stranger", set()) is False

@pytest.mark.unit
def test_actor_direct_user_takes_precedence_over_role():
    """When a user is directly assigned, role membership alone is not enough."""
    row = {
        "current_governance_reviewer": "specificUser",
        "current_governance_reviewer_role": "role1",
    }
    # Different user who has the role should still NOT be eligible
    assert sm.actor_eligible_for_state(row, "governance_review", "otherUser", {"role1"}) is False
    # The directly assigned user should be eligible
    assert sm.actor_eligible_for_state(row, "governance_review", "specificUser", set()) is True


# ── _FORWARD_NEXT shape ──────────────────────────────────────────────────────

@pytest.mark.unit
def test_forward_chain_has_4_hops():
    assert len(sm._FORWARD_NEXT) == 4

@pytest.mark.unit
@pytest.mark.parametrize("state", ["draft", "quality_review", "governance_review", "approval"])
def test_forward_chain_includes_state(state):
    assert state in sm._FORWARD_NEXT

@pytest.mark.unit
def test_forward_chain_does_not_loop_back():
    """Published is terminal — it must not be a key in the forward chain."""
    assert "published" not in sm._FORWARD_NEXT


# ── DEFAULT_STATES_JSON ──────────────────────────────────────────────────────

@pytest.mark.unit
def test_default_states_json_has_5_states():
    assert len(sm.DEFAULT_STATES_JSON["states"]) == 5

@pytest.mark.unit
@pytest.mark.parametrize("state", [
    "draft", "quality_review", "governance_review", "approval", "published",
])
def test_default_states_includes(state):
    assert state in sm.DEFAULT_STATES_JSON["states"]

@pytest.mark.unit
def test_default_states_has_transitions():
    assert "transitions" in sm.DEFAULT_STATES_JSON
    assert isinstance(sm.DEFAULT_STATES_JSON["transitions"], dict)

@pytest.mark.unit
def test_default_states_transitions_match_states():
    transitions = sm.DEFAULT_STATES_JSON["transitions"]
    states = set(sm.DEFAULT_STATES_JSON["states"])
    for from_state, allowed in transitions.items():
        assert from_state in states
        for to_state in allowed:
            assert to_state in states

@pytest.mark.unit
def test_default_states_has_permissions():
    assert "required_permission_per_transition" in sm.DEFAULT_STATES_JSON
    perms = sm.DEFAULT_STATES_JSON["required_permission_per_transition"]
    assert isinstance(perms, dict)

@pytest.mark.unit
@pytest.mark.parametrize("transition,expected_perm", [
    ("draft->quality_review", "workflow.submit"),
    ("quality_review->governance_review", "workflow.review"),
    ("quality_review->draft", "workflow.review"),
    ("governance_review->approval", "workflow.review"),
    ("governance_review->quality_review", "workflow.review"),
    ("approval->published", "workflow.approve"),
    ("approval->governance_review", "workflow.approve"),
    ("published->draft", "workflow.submit"),
])
def test_default_transition_permissions(transition, expected_perm):
    assert sm.DEFAULT_STATES_JSON["required_permission_per_transition"][transition] == expected_perm


# ── Exception classes ────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("exc_cls", [
    sm.WorkflowConflictError,
    sm.WorkflowTransitionError,
    sm.WorkflowNotFoundError,
    sm.RoleResolutionError,
])
def test_exception_classes(exc_cls):
    assert issubclass(exc_cls, Exception)
    # Can be instantiated with a message
    e = exc_cls("test message")
    assert "test message" in str(e)


# ── AUTO_ADVANCE_COMMENT constant ────────────────────────────────────────────

@pytest.mark.unit
def test_auto_advance_comment_is_string():
    assert isinstance(sm.AUTO_ADVANCE_COMMENT, str)
    assert len(sm.AUTO_ADVANCE_COMMENT) > 0
    assert "auto-advance" in sm.AUTO_ADVANCE_COMMENT.lower()
