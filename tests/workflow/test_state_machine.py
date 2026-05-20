"""Tests for the workflow state machine logic (pure Python, no DB).

We test the state machine rules directly — allowed vs. forbidden transitions
and the optimistic-lock version check — without touching a real database.
"""

import pytest

# ── Inline the state machine logic so tests have zero dependencies ────────────

DEFAULT_STATES_JSON = {
    "states": ["draft", "in_review", "changes_requested", "approved", "published"],
    "transitions": {
        "draft": ["in_review"],
        "in_review": ["approved", "changes_requested"],
        "changes_requested": ["draft", "in_review"],
        "approved": ["published", "changes_requested"],
        "published": ["draft"],
    },
    "required_permission_per_transition": {
        "draft->in_review": "workflow.submit",
        "in_review->approved": "workflow.review",
        "in_review->changes_requested": "workflow.review",
        "approved->published": "workflow.approve",
        "approved->changes_requested": "workflow.approve",
        "published->draft": "workflow.submit",
        "changes_requested->draft": "workflow.submit",
        "changes_requested->in_review": "workflow.submit",
    },
}


class WorkflowConflictError(Exception):
    pass


class WorkflowTransitionError(Exception):
    pass


def validate_transition(current_state: str, to_state: str, state_version: int, expected_version: int):
    """Raise on conflict or invalid transition."""
    if state_version != expected_version:
        raise WorkflowConflictError(
            f"State version mismatch: expected {expected_version}, got {state_version}"
        )
    allowed = DEFAULT_STATES_JSON["transitions"].get(current_state, [])
    if to_state not in allowed:
        raise WorkflowTransitionError(
            f"Transition {current_state!r} → {to_state!r} not allowed"
        )


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestValidTransitions:
    """Every transition listed in the state machine should succeed."""

    @pytest.mark.parametrize("from_state,to_state", [
        ("draft", "in_review"),
        ("in_review", "approved"),
        ("in_review", "changes_requested"),
        ("changes_requested", "draft"),
        ("changes_requested", "in_review"),
        ("approved", "published"),
        ("approved", "changes_requested"),
        ("published", "draft"),
    ])
    def test_valid_transition_does_not_raise(self, from_state, to_state):
        validate_transition(from_state, to_state, state_version=1, expected_version=1)


class TestInvalidTransitions:
    """Transitions not in the state machine must raise WorkflowTransitionError."""

    @pytest.mark.parametrize("from_state,to_state", [
        ("draft", "approved"),           # skip in_review
        ("draft", "published"),          # skip two states
        ("draft", "changes_requested"),  # not a valid next from draft
        ("in_review", "draft"),          # can't go back to draft from in_review
        ("in_review", "published"),      # skip approved
        ("approved", "in_review"),       # can't go back to in_review from approved
        ("published", "in_review"),      # published only goes back to draft
        ("published", "approved"),       # nonsensical
    ])
    def test_invalid_transition_raises(self, from_state, to_state):
        with pytest.raises(WorkflowTransitionError):
            validate_transition(from_state, to_state, state_version=1, expected_version=1)


class TestOptimisticLocking:
    def test_matching_version_does_not_raise(self):
        validate_transition("draft", "in_review", state_version=3, expected_version=3)

    def test_version_mismatch_raises_conflict(self):
        with pytest.raises(WorkflowConflictError):
            validate_transition("draft", "in_review", state_version=2, expected_version=3)

    def test_version_mismatch_takes_precedence_over_transition_check(self):
        # Even an otherwise-valid transition raises Conflict first
        with pytest.raises(WorkflowConflictError):
            validate_transition("draft", "in_review", state_version=1, expected_version=5)

    def test_invalid_transition_with_wrong_version_still_raises_conflict(self):
        # Wrong version beats invalid transition — conflict is detected first
        with pytest.raises(WorkflowConflictError):
            validate_transition("draft", "published", state_version=1, expected_version=99)


class TestStateMachineCompleteness:
    """Every state has at least one outgoing transition."""

    def test_every_state_has_outgoing_transitions(self):
        for state in DEFAULT_STATES_JSON["states"]:
            assert state in DEFAULT_STATES_JSON["transitions"], f"{state} has no outgoing transitions"
            assert len(DEFAULT_STATES_JSON["transitions"][state]) > 0

    def test_every_transition_has_a_required_permission(self):
        perms = DEFAULT_STATES_JSON["required_permission_per_transition"]
        for from_state, targets in DEFAULT_STATES_JSON["transitions"].items():
            for to_state in targets:
                key = f"{from_state}->{to_state}"
                assert key in perms, f"Missing permission for {key}"

    def test_all_destination_states_are_valid(self):
        valid = set(DEFAULT_STATES_JSON["states"])
        for targets in DEFAULT_STATES_JSON["transitions"].values():
            for t in targets:
                assert t in valid, f"Transition target {t!r} is not a declared state"
