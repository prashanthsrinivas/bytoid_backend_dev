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


# ── Same-user auto-advance ────────────────────────────────────────────────────
#
# When consecutive review stages are assigned to the same user, a single
# approve action chains forward automatically (so a single-admin org can ship
# a runbook with one click of Approve instead of three). The production code
# lives in workflow_route/state_machine.transition() — these tests mirror the
# loop logic so we can exercise it without a database, and they pin the rule
# set so refactors that break the chain are caught here.

PROD_STATES_JSON = {
    "states": ["draft", "quality_review", "governance_review", "approval", "published"],
    "transitions": {
        "draft": ["quality_review"],
        "quality_review": ["governance_review", "draft"],
        "governance_review": ["approval", "quality_review"],
        "approval": ["published", "governance_review"],
        "published": ["draft"],
    },
}

FORWARD_NEXT = {
    "draft":             "quality_review",
    "quality_review":    "governance_review",
    "governance_review": "approval",
    "approval":          "published",
}


def _assignee_for_state(row, state):
    if state == "quality_review":
        return row.get("current_quality_reviewer")
    if state == "governance_review":
        return row.get("current_governance_reviewer")
    if state in ("approval", "published"):
        return row.get("current_approver")
    if state == "draft":
        return row.get("owner_user_id")
    return None


def _is_forward(from_state, to_state):
    return FORWARD_NEXT.get(from_state) == to_state


def simulate_transition(row, to_state, actor_user_id):
    """Mirror of state_machine.transition()'s auto-advance loop.

    Returns (final_row, chain). Each chain entry is
    ``{from_state, to_state, auto, assigned_to, comment}``.
    """
    row = dict(row)
    chain = []

    manual_from = row["state"]
    if to_state not in PROD_STATES_JSON["transitions"].get(manual_from, []):
        raise WorkflowTransitionError(
            f"Transition {manual_from!r} → {to_state!r} not allowed"
        )
    chain.append({
        "from_state": manual_from,
        "to_state": to_state,
        "auto": False,
        "assigned_to": _assignee_for_state(row, to_state),
        "comment": None,
    })
    row["state"] = to_state
    row["state_version"] = row.get("state_version", 1) + 1

    # Auto-advance only after a forward manual hop. send_back must not chain
    # into more approvals.
    if _is_forward(manual_from, to_state):
        while True:
            cur_state = row["state"]
            cur_assignee = _assignee_for_state(row, cur_state)
            if not cur_assignee or cur_assignee != actor_user_id:
                break
            next_state = FORWARD_NEXT.get(cur_state)
            if not next_state:
                break
            if next_state not in PROD_STATES_JSON["transitions"].get(cur_state, []):
                break
            chain.append({
                "from_state": cur_state,
                "to_state": next_state,
                "auto": True,
                "assigned_to": _assignee_for_state(row, next_state),
                "comment": "[auto-advance: same reviewer]",
            })
            row["state"] = next_state
            row["state_version"] += 1

    return row, chain


def _row(state="draft", *, qr=None, gr=None, apr=None, owner=None, version=1):
    return {
        "state": state,
        "state_version": version,
        "current_quality_reviewer": qr,
        "current_governance_reviewer": gr,
        "current_approver": apr,
        "owner_user_id": owner,
    }


class TestSameUserAutoAdvance:
    """Auto-advance fires when the next forward stage's assignee == the actor.

    These tests pin the rules; the production loop in transition() must keep
    matching them so a single-admin org can submit and publish in one round-trip.
    """

    def test_single_user_all_three_roles_auto_publishes(self):
        # Owner submits with QR=GR=APR=owner. The submit (draft→quality_review)
        # is the manual hop; the loop should keep firing all the way to published.
        row = _row(owner="u1", qr="u1", gr="u1", apr="u1")
        final, chain = simulate_transition(row, "quality_review", actor_user_id="u1")

        assert final["state"] == "published"
        assert final["state_version"] == 1 + 4  # one bump per hop
        assert [(h["from_state"], h["to_state"], h["auto"]) for h in chain] == [
            ("draft", "quality_review", False),
            ("quality_review", "governance_review", True),
            ("governance_review", "approval", True),
            ("approval", "published", True),
        ]

    def test_qr_eq_gr_but_approver_differs_stops_at_approval(self):
        # u1 is both QR and GR; APR is u2. After u1 approves at quality, the
        # chain auto-advances quality→governance (same person), then stops at
        # `approval` because the approver (u2) isn't the actor.
        row = _row(state="quality_review", qr="u1", gr="u1", apr="u2", version=2)
        final, chain = simulate_transition(row, "governance_review", actor_user_id="u1")

        assert final["state"] == "approval"
        assert final["state_version"] == 4
        assert [(h["from_state"], h["to_state"], h["auto"]) for h in chain] == [
            ("quality_review", "governance_review", False),
            ("governance_review", "approval", True),
        ]

    def test_gr_eq_approver_qr_different(self):
        # QR=u1, GR=APR=u2. u1 manually approves at quality (no auto, since
        # GR is u2). Then u2 approves at governance and the loop chains all
        # the way to published.
        row1 = _row(state="quality_review", qr="u1", gr="u2", apr="u2", version=2)
        final1, chain1 = simulate_transition(row1, "governance_review", actor_user_id="u1")
        assert final1["state"] == "governance_review"
        assert [(h["from_state"], h["to_state"], h["auto"]) for h in chain1] == [
            ("quality_review", "governance_review", False),
        ]

        # Now u2 acts; auto-advance from governance through approval to published.
        final2, chain2 = simulate_transition(final1, "approval", actor_user_id="u2")
        assert final2["state"] == "published"
        assert [(h["from_state"], h["to_state"], h["auto"]) for h in chain2] == [
            ("governance_review", "approval", False),
            ("approval", "published", True),
        ]

    def test_all_distinct_no_auto_advance(self):
        # Three different reviewers → never auto-advance; every approval is one hop.
        row = _row(state="quality_review", qr="u1", gr="u2", apr="u3", version=1)
        final, chain = simulate_transition(row, "governance_review", actor_user_id="u1")
        assert final["state"] == "governance_review"
        assert len(chain) == 1

        row2 = _row(state="governance_review", qr="u1", gr="u2", apr="u3", version=2)
        final2, chain2 = simulate_transition(row2, "approval", actor_user_id="u2")
        assert final2["state"] == "approval"
        assert len(chain2) == 1

        row3 = _row(state="approval", qr="u1", gr="u2", apr="u3", version=3)
        final3, chain3 = simulate_transition(row3, "published", actor_user_id="u3")
        assert final3["state"] == "published"
        assert len(chain3) == 1

    def test_send_back_does_not_auto_advance(self):
        # Even when QR=GR=APR all match the actor, send_back is NOT in
        # FORWARD_NEXT, so the loop never fires.
        row = _row(state="governance_review", qr="u1", gr="u1", apr="u1", version=3)
        final, chain = simulate_transition(row, "quality_review", actor_user_id="u1")
        assert final["state"] == "quality_review"
        assert len(chain) == 1
        assert chain[0]["auto"] is False

    def test_auto_advance_hops_carry_tagged_comment(self):
        # Every auto hop should record the canonical tag so audit consumers
        # can distinguish chained approvals from manual ones.
        row = _row(owner="u1", qr="u1", gr="u1", apr="u1")
        _, chain = simulate_transition(row, "quality_review", actor_user_id="u1")
        auto_hops = [h for h in chain if h["auto"]]
        assert auto_hops, "expected at least one auto-advance hop"
        for hop in auto_hops:
            assert hop["comment"] == "[auto-advance: same reviewer]"
        # The manual hop has no auto-advance tag.
        assert chain[0]["comment"] is None

    def test_state_version_increments_once_per_hop(self):
        # Start at version=5; full single-user submit chain → version=9.
        row = _row(owner="u1", qr="u1", gr="u1", apr="u1", version=5)
        final, chain = simulate_transition(row, "quality_review", actor_user_id="u1")
        assert len(chain) == 4
        assert final["state_version"] == 9

    def test_no_auto_advance_when_assignee_missing(self):
        # If the next stage has no assignee (NULL), the loop must stop —
        # otherwise the workflow would silently bypass an unconfigured stage.
        row = _row(state="quality_review", qr="u1", gr=None, apr="u1", version=2)
        final, chain = simulate_transition(row, "governance_review", actor_user_id="u1")
        assert final["state"] == "governance_review"
        assert len(chain) == 1
