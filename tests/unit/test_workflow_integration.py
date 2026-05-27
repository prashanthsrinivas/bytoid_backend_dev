"""Unit tests for workflow_route/integration.py.

Both functions take doc state from get_workflow_for_doc, which we mock.
"""

import sys
from unittest.mock import MagicMock, patch

import flask
import pytest

for _mod in ("pymysql", "pymysql.cursors", "db", "db.rds_db", "db.db_checkers",
             "boto3", "dotenv", "dbutils", "dbutils.pooled_db"):
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

sys.modules.setdefault("utils.base_logger",
                      MagicMock(get_logger=MagicMock(return_value=MagicMock())))

import workflow_route.integration as wfi  # noqa: E402


@pytest.fixture
def app():
    return flask.Flask(__name__)


# ── _READ_ONLY_STATES ────────────────────────────────────────────────────────

@pytest.mark.unit
def test_read_only_states_includes_governance():
    assert "governance_review" in wfi._READ_ONLY_STATES

@pytest.mark.unit
def test_read_only_states_includes_approval():
    assert "approval" in wfi._READ_ONLY_STATES

@pytest.mark.unit
def test_read_only_states_excludes_draft():
    assert "draft" not in wfi._READ_ONLY_STATES

@pytest.mark.unit
def test_read_only_states_excludes_quality_review():
    """Quality reviewers can still edit — this is the spec."""
    assert "quality_review" not in wfi._READ_ONLY_STATES

@pytest.mark.unit
def test_read_only_states_excludes_published():
    """Published is locked by guard_mutation, not the read-only-states set."""
    assert "published" not in wfi._READ_ONLY_STATES

@pytest.mark.unit
def test_read_only_states_is_frozen():
    assert isinstance(wfi._READ_ONLY_STATES, frozenset)


# ── guard_mutation ───────────────────────────────────────────────────────────

@pytest.mark.unit
def test_guard_mutation_no_workflow_returns_none(app):
    with app.app_context():
        with patch("workflow_route.integration.get_workflow_for_doc", return_value=None):
            assert wfi.guard_mutation("policy", "doc1", "1.0") is None

@pytest.mark.unit
def test_guard_mutation_db_exception_fails_open(app):
    with app.app_context():
        with patch("workflow_route.integration.get_workflow_for_doc",
                  side_effect=RuntimeError("DB down")):
            assert wfi.guard_mutation("policy", "doc1", "1.0") is None

@pytest.mark.unit
def test_guard_mutation_published_returns_409(app):
    wf = {"state": "published", "workflow_id": "wf-1"}
    with app.app_context():
        with patch("workflow_route.integration.get_workflow_for_doc", return_value=wf):
            result = wfi.guard_mutation("policy", "doc1", "1.0")
        assert result is not None
        resp, code = result
        assert code == 409
        body = resp.get_json()
        assert "Cannot modify a published" in body["error"]
        assert body["workflow_id"] == "wf-1"

@pytest.mark.unit
@pytest.mark.parametrize("state", [
    "draft", "quality_review", "governance_review", "approval",
])
def test_guard_mutation_non_published_returns_none(app, state):
    with app.app_context():
        with patch("workflow_route.integration.get_workflow_for_doc",
                  return_value={"state": state}):
            assert wfi.guard_mutation("policy", "doc1", "1.0") is None

@pytest.mark.unit
@pytest.mark.parametrize("doc_type", ["policy", "procedure", "standard", "runbook", "report"])
def test_guard_mutation_works_for_any_doc_type(app, doc_type):
    with app.app_context():
        with patch("workflow_route.integration.get_workflow_for_doc", return_value=None):
            assert wfi.guard_mutation(doc_type, "doc1", "1.0") is None


# ── assert_doc_editable ──────────────────────────────────────────────────────

@pytest.mark.unit
def test_assert_doc_editable_no_workflow(app):
    with app.app_context():
        with patch("workflow_route.integration.get_workflow_for_doc", return_value=None):
            ok, reason, wf = wfi.assert_doc_editable("policy", "doc1", "actor")
    assert ok is True
    assert reason is None
    assert wf is None

@pytest.mark.unit
def test_assert_doc_editable_db_exception_fails_open(app):
    with app.app_context():
        with patch("workflow_route.integration.get_workflow_for_doc",
                  side_effect=RuntimeError("boom")):
            ok, reason, wf = wfi.assert_doc_editable("policy", "doc1", "actor")
    assert ok is True
    assert reason is None
    assert wf is None

@pytest.mark.unit
@pytest.mark.parametrize("state", ["draft", "quality_review", "published"])
def test_assert_doc_editable_writable_states(app, state):
    wf = {"state": state, "workflow_id": "wf1"}
    with app.app_context():
        with patch("workflow_route.integration.get_workflow_for_doc", return_value=wf):
            ok, reason, w = wfi.assert_doc_editable("policy", "doc1", "actor")
    assert ok is True
    assert reason is None
    assert w == wf

@pytest.mark.unit
@pytest.mark.parametrize("state", ["governance_review", "approval"])
def test_assert_doc_editable_read_only_states(app, state):
    wf = {"state": state, "workflow_id": "wf1"}
    with app.app_context():
        with patch("workflow_route.integration.get_workflow_for_doc", return_value=wf):
            ok, reason, w = wfi.assert_doc_editable("policy", "doc1", "actor")
    assert ok is False
    assert reason is not None
    assert state.replace("_", " ") in reason
    assert w == wf

@pytest.mark.unit
def test_assert_doc_editable_missing_state_defaults_to_draft(app):
    """A workflow row without explicit state defaults to draft (editable)."""
    wf = {"workflow_id": "wf1"}  # no 'state' key
    with app.app_context():
        with patch("workflow_route.integration.get_workflow_for_doc", return_value=wf):
            ok, reason, w = wfi.assert_doc_editable("policy", "doc1", "actor")
    assert ok is True

@pytest.mark.unit
def test_assert_doc_editable_message_format(app):
    """Error message should be user-friendly (no underscores)."""
    wf = {"state": "governance_review", "workflow_id": "wf1"}
    with app.app_context():
        with patch("workflow_route.integration.get_workflow_for_doc", return_value=wf):
            ok, reason, _ = wfi.assert_doc_editable("policy", "doc1", "actor")
    assert "governance review" in reason
    assert "_" not in reason  # underscores replaced

@pytest.mark.unit
def test_assert_doc_editable_default_version_is_1_0(app):
    """Verifies the doc_version default kwarg is '1.0'."""
    # We call without specifying doc_version
    with app.app_context():
        with patch("workflow_route.integration.get_workflow_for_doc",
                  return_value=None) as m:
            wfi.assert_doc_editable("policy", "doc1", "actor")
        # The signature passes ("policy", "doc1", "1.0")
        m.assert_called_once_with("policy", "doc1", "1.0")
