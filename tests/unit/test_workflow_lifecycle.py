"""Unit tests for workflow_route/lifecycle.py — all DB/notification calls mocked."""

import sys
from unittest.mock import MagicMock, patch

import pytest

for _mod in ("pymysql", "pymysql.cursors", "db", "db.rds_db", "db.db_checkers",
             "boto3", "dotenv", "dbutils", "dbutils.pooled_db"):
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

sys.modules.setdefault("utils.base_logger",
                      MagicMock(get_logger=MagicMock(return_value=MagicMock())))

import workflow_route.lifecycle as lc  # noqa: E402


def _mock_conn(fetchall=None):
    """Build a mock pymysql connection that returns *fetchall* from fetchall()."""
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchall.return_value = fetchall or []
    conn.cursor.return_value = cur
    return conn, cur


# ── reassign_orphaned_workflows ──────────────────────────────────────────────

@pytest.mark.unit
def test_reassign_no_orphans_returns_zero():
    conn, _ = _mock_conn(fetchall=[])
    with patch("workflow_route.lifecycle.connect_to_rds", return_value=conn):
        n = lc.reassign_orphaned_workflows("u-deactivated", "org-1")
    assert n == 0

@pytest.mark.unit
def test_reassign_one_orphan_per_document_mode():
    orphan = {
        "workflow_id": "wf-1", "assignment_mode": "per_document",
        "current_reviewer": "u-deactivated", "current_approver": "u2",
        "doc_type": "policy", "doc_id": "d1", "owner_user_id": "owner",
    }
    conn, _ = _mock_conn(fetchall=[orphan])
    with patch("workflow_route.lifecycle.connect_to_rds", return_value=conn):
        with patch("workflow_route.lifecycle._handle_orphan") as h:
            n = lc.reassign_orphaned_workflows("u-deactivated", "org-1")
    assert n == 1
    h.assert_called_once_with(orphan, "u-deactivated", "org-1")

@pytest.mark.unit
def test_reassign_multiple_orphans():
    rows = [{"workflow_id": f"wf-{i}", "current_reviewer": "u-d"} for i in range(5)]
    conn, _ = _mock_conn(fetchall=rows)
    with patch("workflow_route.lifecycle.connect_to_rds", return_value=conn):
        with patch("workflow_route.lifecycle._handle_orphan"):
            n = lc.reassign_orphaned_workflows("u-d", "org-1")
    assert n == 5

@pytest.mark.unit
def test_reassign_continues_after_individual_failure():
    """A failed handler on one orphan must not block the rest."""
    rows = [{"workflow_id": f"wf-{i}"} for i in range(3)]
    conn, _ = _mock_conn(fetchall=rows)
    call_count = [0]
    def _handler(row, *args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 2:
            raise RuntimeError("transient")
    with patch("workflow_route.lifecycle.connect_to_rds", return_value=conn):
        with patch("workflow_route.lifecycle._handle_orphan", side_effect=_handler):
            n = lc.reassign_orphaned_workflows("u-d", "org-1")
    # 2 succeeded, 1 failed — touched count = 2
    assert n == 2


# ── _handle_orphan ───────────────────────────────────────────────────────────

@pytest.mark.unit
def test_handle_orphan_per_document_nullifies_and_notifies():
    row = {
        "workflow_id": "wf-1", "assignment_mode": "per_document",
        "current_reviewer": "u-d", "current_approver": "u2",
    }
    with patch("workflow_route.lifecycle._nullify_and_notify") as null_m:
        with patch("workflow_route.lifecycle._do_reassign") as do_m:
            lc._handle_orphan(row, "u-d", "org-1")
    null_m.assert_called_once()
    do_m.assert_not_called()

@pytest.mark.unit
def test_handle_orphan_role_based_reassigns_when_user_found():
    row = {
        "workflow_id": "wf-1", "assignment_mode": "role_based",
        "current_reviewer": "u-d", "current_approver": "u2",
        "reviewer_role_id": "role-r",
    }
    with patch("workflow_route.lifecycle._do_reassign") as do_m:
        with patch("workflow_route.lifecycle._nullify_and_notify") as null_m:
            with patch.dict("sys.modules", {"shared_configuration":
                          MagicMock(get_round_robin_user_for_resource=lambda *a, **k: "u-new")}):
                lc._handle_orphan(row, "u-d", "org-1")
    do_m.assert_called_once()
    null_m.assert_not_called()

@pytest.mark.unit
def test_handle_orphan_role_based_falls_back_when_no_user_found():
    row = {
        "workflow_id": "wf-1", "assignment_mode": "role_based",
        "current_reviewer": "u-d",
        "reviewer_role_id": "role-r",
    }
    with patch("workflow_route.lifecycle._do_reassign") as do_m:
        with patch("workflow_route.lifecycle._nullify_and_notify") as null_m:
            with patch.dict("sys.modules", {"shared_configuration":
                          MagicMock(get_round_robin_user_for_resource=lambda *a, **k: None)}):
                lc._handle_orphan(row, "u-d", "org-1")
    do_m.assert_not_called()
    null_m.assert_called_once()

@pytest.mark.unit
def test_handle_orphan_approver_role():
    """When the deactivated user is current_approver (not reviewer), uses approver_role_id."""
    row = {
        "workflow_id": "wf-1", "assignment_mode": "role_based",
        "current_reviewer": "u-other", "current_approver": "u-d",
        "approver_role_id": "role-a",
    }
    captured = {}
    def _capture(role_id, _wf_id):
        captured["role_id"] = role_id
        return "u-new"
    with patch("workflow_route.lifecycle._do_reassign"):
        with patch("workflow_route.lifecycle._notify_reassigned"):
            with patch.dict("sys.modules", {"shared_configuration":
                          MagicMock(get_round_robin_user_for_resource=_capture)}):
                lc._handle_orphan(row, "u-d", "org-1")
    assert captured["role_id"] == "role-a"

@pytest.mark.unit
def test_handle_orphan_assignment_mode_defaults_to_per_document():
    row = {
        "workflow_id": "wf-1",
        "current_reviewer": "u-d",
        # no assignment_mode key
    }
    with patch("workflow_route.lifecycle._nullify_and_notify") as null_m:
        lc._handle_orphan(row, "u-d", "org-1")
    null_m.assert_called_once()


# ── _do_reassign ─────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_do_reassign_updates_reviewer_column():
    conn, cur = _mock_conn()
    with patch("workflow_route.lifecycle.connect_to_rds", return_value=conn):
        lc._do_reassign("wf-1", "reviewer", "u-new", "u-old", "comment")
    # First execute is the UPDATE on document_workflow
    update_call = cur.execute.call_args_list[0]
    sql = update_call.args[0]
    assert "current_reviewer" in sql
    assert "UPDATE document_workflow" in sql

@pytest.mark.unit
def test_do_reassign_updates_approver_column():
    conn, cur = _mock_conn()
    with patch("workflow_route.lifecycle.connect_to_rds", return_value=conn):
        lc._do_reassign("wf-1", "approver", "u-new", "u-old", "comment")
    update_call = cur.execute.call_args_list[0]
    assert "current_approver" in update_call.args[0]

@pytest.mark.unit
def test_do_reassign_increments_state_version():
    conn, cur = _mock_conn()
    with patch("workflow_route.lifecycle.connect_to_rds", return_value=conn):
        lc._do_reassign("wf-1", "reviewer", "u-new", "u-old", "comment")
    assert "state_version=state_version+1" in cur.execute.call_args_list[0].args[0]

@pytest.mark.unit
def test_do_reassign_inserts_event_row():
    conn, cur = _mock_conn()
    with patch("workflow_route.lifecycle.connect_to_rds", return_value=conn):
        lc._do_reassign("wf-1", "reviewer", "u-new", "u-old", "test comment")
    # Second execute is the INSERT
    insert_call = cur.execute.call_args_list[1]
    assert "INSERT INTO document_workflow_events" in insert_call.args[0]
    assert insert_call.args[1][5] == "test comment"


# ── _nullify_and_notify ──────────────────────────────────────────────────────

@pytest.mark.unit
def test_nullify_reviewer_sets_null():
    conn, cur = _mock_conn()
    with patch("workflow_route.lifecycle.connect_to_rds", return_value=conn):
        with patch.dict("sys.modules", {"services.workflow_notifications_service":
                      MagicMock(notify_orphaned_workflow=MagicMock())}):
            lc._nullify_and_notify("wf-1", {}, "reviewer", "org-1")
    sql = cur.execute.call_args.args[0]
    assert "current_reviewer=NULL" in sql

@pytest.mark.unit
def test_nullify_approver_sets_null():
    conn, cur = _mock_conn()
    with patch("workflow_route.lifecycle.connect_to_rds", return_value=conn):
        with patch.dict("sys.modules", {"services.workflow_notifications_service":
                      MagicMock(notify_orphaned_workflow=MagicMock())}):
            lc._nullify_and_notify("wf-1", {}, "approver", "org-1")
    sql = cur.execute.call_args.args[0]
    assert "current_approver=NULL" in sql

@pytest.mark.unit
def test_nullify_swallows_notification_exception():
    conn, _ = _mock_conn()
    with patch("workflow_route.lifecycle.connect_to_rds", return_value=conn):
        with patch.dict("sys.modules", {"services.workflow_notifications_service":
                      MagicMock(notify_orphaned_workflow=MagicMock(side_effect=RuntimeError("boom")))}):
            # Should NOT raise
            lc._nullify_and_notify("wf-1", {}, "reviewer", "org-1")


# ── _notify_reassigned ───────────────────────────────────────────────────────

@pytest.mark.unit
def test_notify_reassigned_calls_notification_service():
    mock_notify = MagicMock()
    with patch.dict("sys.modules", {"services.workflow_notifications_service":
                  MagicMock(notify_workflow_reassigned=mock_notify)}):
        lc._notify_reassigned("wf-1", {"workflow_id": "wf-1"}, "u-new", "reviewer")
    mock_notify.assert_called_once()

@pytest.mark.unit
def test_notify_reassigned_swallows_exception():
    with patch.dict("sys.modules", {"services.workflow_notifications_service":
                  MagicMock(notify_workflow_reassigned=MagicMock(side_effect=RuntimeError("boom")))}):
        # Should NOT raise
        lc._notify_reassigned("wf-1", {}, "u-new", "reviewer")
