"""Unit tests for workflow_route.integration.assert_doc_editable.

The guard locks the document while it is in governance_review or approval
states. Quality reviewers can still edit (per product spec), and missing
workflows fail-open.

DB stubs are installed by tests/conftest_db_stubs.py (autouse, restores
``sys.modules`` on teardown so other tests don't see the stubs).
"""

import sys

import pytest


@pytest.fixture
def integration(db_stubs):
    sys.modules.pop("workflow_route.integration", None)
    sys.modules.pop("workflow_route.state_machine", None)
    from workflow_route import integration as mod
    return mod


@pytest.mark.parametrize("state", ["governance_review", "approval"])
def test_locked_states_return_not_editable(integration, state, monkeypatch):
    monkeypatch.setattr(
        integration, "get_workflow_for_doc",
        lambda *a, **k: {"state": state, "workflow_id": "wf-1"},
    )
    ok, reason, wf = integration.assert_doc_editable("runbook", "rb-1", "user-1")
    assert ok is False
    assert reason is not None
    assert state.replace("_", " ") in reason.lower()
    assert wf == {"state": state, "workflow_id": "wf-1"}


@pytest.mark.parametrize("state", ["draft", "quality_review", "published"])
def test_editable_states_allow_mutation(integration, state, monkeypatch):
    monkeypatch.setattr(
        integration, "get_workflow_for_doc",
        lambda *a, **k: {"state": state, "workflow_id": "wf-1"},
    )
    ok, reason, _ = integration.assert_doc_editable("runbook", "rb-1", "user-1")
    assert ok is True
    assert reason is None


def test_quality_review_remains_editable(integration, monkeypatch):
    """Per product spec: the quality reviewer can still edit the report
    before approving."""
    monkeypatch.setattr(
        integration, "get_workflow_for_doc",
        lambda *a, **k: {"state": "quality_review"},
    )
    ok, _, _ = integration.assert_doc_editable("runbook", "rb-1", "qr-user")
    assert ok is True


def test_no_workflow_fails_open(integration, monkeypatch):
    monkeypatch.setattr(integration, "get_workflow_for_doc", lambda *a, **k: None)
    ok, reason, wf = integration.assert_doc_editable("runbook", "rb-1", "user-1")
    assert ok is True
    assert reason is None
    assert wf is None


def test_lookup_failure_fails_open(integration, monkeypatch):
    """A transient DB blip must never lock every report."""
    def boom(*a, **k):
        raise Exception("boom")
    monkeypatch.setattr(integration, "get_workflow_for_doc", boom)
    ok, reason, _ = integration.assert_doc_editable("runbook", "rb-1", "user-1")
    assert ok is True
    assert reason is None
