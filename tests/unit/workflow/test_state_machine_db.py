"""§4a (DB) — ``workflow_route/state_machine.py`` DB-touching functions.

Exercised with a fake PyMySQL connection (``make_conn``/``FakeCursor``) wired in
via ``mock_rds``. Assertions cover the returned shape, the SQL/params at the mock
boundary, commit on writes, and the not-found / empty paths.
"""

from __future__ import annotations

import json
import sys
import types
from unittest.mock import patch

import pytest

from tests.workflow_playbook import _wf_pb_stubs as stubs

stubs.bootstrap_sut()

import workflow_route.state_machine as sm  # noqa: E402

pytestmark = pytest.mark.unit

_ALIAS = "workflow_route.state_machine"


# ── get_workflow ──────────────────────────────────────────────────────────────

def test_get_workflow_found():
    conn = stubs.make_conn(fetchone={"workflow_id": "w1", "state": "draft"})
    with stubs.mock_rds(conn, _ALIAS):
        out = sm.get_workflow("w1")
    assert out == {"workflow_id": "w1", "state": "draft"}
    assert "FROM document_workflow WHERE workflow_id" in conn.fake_cursor.last_sql
    assert conn.fake_cursor.executed[-1][1] == ("w1",)
    assert conn.close.called


def test_get_workflow_not_found_raises():
    conn = stubs.make_conn(fetchone=None)
    with stubs.mock_rds(conn, _ALIAS), pytest.raises(sm.WorkflowNotFoundError):
        sm.get_workflow("missing")
    assert conn.close.called   # connection still closed on the error path


# ── get_workflow_for_doc ──────────────────────────────────────────────────────

def test_get_workflow_for_doc_found():
    conn = stubs.make_conn(fetchone={"workflow_id": "w2"})
    with stubs.mock_rds(conn, _ALIAS):
        out = sm.get_workflow_for_doc("policy", "doc-1", "1.0")
    assert out == {"workflow_id": "w2"}
    assert conn.fake_cursor.executed[-1][1] == ("policy", "doc-1", "1.0")


def test_get_workflow_for_doc_none():
    conn = stubs.make_conn(fetchone=None)
    with stubs.mock_rds(conn, _ALIAS):
        assert sm.get_workflow_for_doc("policy", "doc-x", "1.0") is None


# ── get_user_org_id (company → launch → admin fallback → None) ────────────────

@pytest.mark.parametrize("row,expected", [
    ({"company_name": "Acme", "launch_id_fk": "L1", "user_type": "admin"}, "Acme"),
    ({"company_name": "  ", "launch_id_fk": "L1", "user_type": "user"}, "launch:L1"),
    ({"company_name": None, "launch_id_fk": None, "user_type": "admin"}, "launch:u-7"),
    ({"company_name": "", "launch_id_fk": "", "user_type": "user"}, None),
])
def test_get_user_org_id_resolution(row, expected):
    conn = stubs.make_conn(fetchone=row)
    with stubs.mock_rds(conn, _ALIAS):
        assert sm.get_user_org_id("u-7") == expected


def test_get_user_org_id_missing_user():
    conn = stubs.make_conn(fetchone=None)
    with stubs.mock_rds(conn, _ALIAS):
        assert sm.get_user_org_id("ghost") is None


# ── add_comment (insert + commit, attachments stamped) ────────────────────────

def test_add_comment_inserts_and_commits():
    conn = stubs.make_conn(fetchone={"created_at": "2026-06-04T00:00:00Z"})
    with stubs.mock_rds(conn, _ALIAS):
        out = sm.add_comment("w1", "actor-1", "looks good")

    assert out["created_at"] == "2026-06-04T00:00:00Z"
    assert isinstance(out["event_id"], str) and len(out["event_id"]) >= 8
    # First statement is the INSERT into the events table; commit happened.
    insert_sql, insert_params = conn.fake_cursor.executed[0]
    assert "INSERT INTO document_workflow_events" in insert_sql
    assert insert_params[1] == "w1" and insert_params[2] == "actor-1"
    assert insert_params[3] == "looks good"
    assert insert_params[4] is None          # no attachments → NULL payload
    assert conn.commit.called


def test_add_comment_stamps_attachments():
    conn = stubs.make_conn(fetchone={"created_at": "t"})
    atts = [{"s3_key": "k1", "original_name": "a.png",
             "content_type": "image/png", "size": 10}]
    with stubs.mock_rds(conn, _ALIAS):
        sm.add_comment("w1", "actor-1", "see attached", attachments=atts)

    payload = json.loads(conn.fake_cursor.executed[0][1][4])
    assert payload[0]["s3_key"] == "k1"
    assert "uploaded_at" in payload[0]        # server-stamped


# ── _append_event (insert + commit, returns event id) ─────────────────────────

def test_append_event_inserts_and_commits():
    conn = stubs.make_conn()
    with stubs.mock_rds(conn, _ALIAS):
        event_id = sm._append_event("w1", "draft", "quality_review", "actor-1", "ok")

    assert isinstance(event_id, str) and len(event_id) >= 8
    sql, params = conn.fake_cursor.executed[0]
    assert "INSERT INTO document_workflow_events" in sql
    assert params[1:5] == ("w1", "draft", "quality_review", "actor-1")
    assert params[6] == "ok"               # comment
    assert conn.commit.called


def test_append_event_defaults_assignee_null():
    conn = stubs.make_conn()
    with stubs.mock_rds(conn, _ALIAS):
        sm._append_event("w1", None, "draft", "actor-1", None)
    params = conn.fake_cursor.executed[0][1]
    assert params[5] is None               # assigned_to_user_id default


# ── get_workflow_config (row → parsed; missing → default) ─────────────────────

def test_get_workflow_config_parses_dict_states():
    row = {"states_json": {"states": ["draft"]}, "assignment_mode": "per_org",
           "reviewer_role_id": "r1", "approver_role_id": "a1"}
    conn = stubs.make_conn(fetchone=row)
    with stubs.mock_rds(conn, _ALIAS):
        out = sm.get_workflow_config("org", "policy")
    assert out["states_json"] == {"states": ["draft"]}
    assert out["assignment_mode"] == "per_org" and out["reviewer_role_id"] == "r1"


def test_get_workflow_config_parses_json_string_states():
    row = {"states_json": '{"states": ["draft"]}', "assignment_mode": "m",
           "reviewer_role_id": None, "approver_role_id": None}
    conn = stubs.make_conn(fetchone=row)
    with stubs.mock_rds(conn, _ALIAS):
        out = sm.get_workflow_config("org", "policy")
    assert out["states_json"] == {"states": ["draft"]}


def test_get_workflow_config_default_when_missing():
    conn = stubs.make_conn(fetchone=None)
    with stubs.mock_rds(conn, _ALIAS):
        out = sm.get_workflow_config("org", "policy")
    assert out["states_json"] == sm.DEFAULT_STATES_JSON
    assert out["assignment_mode"] == "per_document"
    assert out["reviewer_role_id"] is None


# ── get_actor_role_ids ────────────────────────────────────────────────────────

def test_get_actor_role_ids_from_permissions_json():
    conn = stubs.make_conn(fetchone={"permissions": '{"role": {"id": "r7"}}'})
    with stubs.mock_rds(conn, _ALIAS):
        assert sm.get_actor_role_ids("u1") == {"r7"}


@pytest.mark.parametrize("row", [
    None,                              # no user
    {"permissions": None},             # no permissions
    {"permissions": "not-json"},       # malformed → swallowed
    {"permissions": '{"role": {}}'},   # role without id
])
def test_get_actor_role_ids_empty_cases(row):
    conn = stubs.make_conn(fetchone=row)
    with stubs.mock_rds(conn, _ALIAS):
        assert sm.get_actor_role_ids("u1") == set()


# ── review frequency get/set (lazy policy_hub.review_lifecycle stubbed) ───────

def _fake_review_lifecycle():
    mod = types.ModuleType("policy_hub.review_lifecycle")
    mod.DEFAULT_REVIEW_FREQUENCY = "annual"
    mod.normalize_frequency = lambda x: x or "annual"
    return mod


def test_get_org_review_frequency_no_org_returns_default():
    # no DB call when org_id is empty
    with patch.dict(sys.modules, {"policy_hub.review_lifecycle": _fake_review_lifecycle()}):
        assert sm.get_org_review_frequency("", "policy") == "annual"


def test_get_org_review_frequency_picks_category_row():
    conn = stubs.make_conn(fetchall=[{"doc_type": "policy", "review_frequency": "quarterly"}])
    with patch.dict(sys.modules, {"policy_hub.review_lifecycle": _fake_review_lifecycle()}), \
         stubs.mock_rds(conn, _ALIAS):
        assert sm.get_org_review_frequency("org", "policy") == "quarterly"


def test_get_org_review_frequency_falls_back_to_policy_row():
    # runbook category absent → falls back to the 'policy' row
    conn = stubs.make_conn(fetchall=[{"doc_type": "policy", "review_frequency": "monthly"}])
    with patch.dict(sys.modules, {"policy_hub.review_lifecycle": _fake_review_lifecycle()}), \
         stubs.mock_rds(conn, _ALIAS):
        assert sm.get_org_review_frequency("org", "runbook") == "monthly"


def test_set_org_review_frequency_upserts_and_commits():
    conn = stubs.make_conn()
    with patch.dict(sys.modules, {"policy_hub.review_lifecycle": _fake_review_lifecycle()}), \
         stubs.mock_rds(conn, _ALIAS):
        out = sm.set_org_review_frequency("org", "weekly", "policy")
    assert out == "weekly"
    sql, params = conn.fake_cursor.executed[0]
    assert "INSERT INTO org_review_config" in sql and "ON DUPLICATE KEY UPDATE" in sql
    assert params == ("org", "policy", "weekly")
    assert conn.commit.called


# ── get_workflow_states_for_docs (latest-per-doc) ─────────────────────────────

def test_get_workflow_states_for_docs_empty_ids_no_db():
    assert sm.get_workflow_states_for_docs("policy", []) == {}
    assert sm.get_workflow_states_for_docs("policy", [None, ""]) == {}


def test_get_workflow_states_for_docs_latest_wins():
    conn = stubs.make_conn(fetchall=[
        {"doc_id": "d1", "state": "draft", "created_at": 1},
        {"doc_id": "d1", "state": "approval", "created_at": 2},   # newer wins
        {"doc_id": "d2", "state": "published", "created_at": 5},
    ])
    with stubs.mock_rds(conn, _ALIAS):
        out = sm.get_workflow_states_for_docs("policy", ["d1", "d2"])
    assert out == {"d1": "approval", "d2": "published"}
    assert "IN (%s,%s)" in conn.fake_cursor.last_sql       # one placeholder per id


# ── get_docs_assigned_to_user (exclusions + latest-per-doc) ───────────────────

def test_get_docs_assigned_empty_args():
    assert sm.get_docs_assigned_to_user("", "u1") == []
    assert sm.get_docs_assigned_to_user("policy", "") == []


def test_get_docs_assigned_dedupes_latest():
    conn = stubs.make_conn(fetchall=[
        {"doc_id": "d1", "state": "quality_review", "created_at": 1, "role": "x"},
        {"doc_id": "d1", "state": "approval", "created_at": 3, "role": "y"},
    ])
    with stubs.mock_rds(conn, _ALIAS):
        out = sm.get_docs_assigned_to_user("policy", "u1")
    assert len(out) == 1 and out[0]["created_at"] == 3


def test_get_docs_assigned_include_published_toggle():
    conn = stubs.make_conn(fetchall=[])
    with stubs.mock_rds(conn, _ALIAS):
        sm.get_docs_assigned_to_user("policy", "u1")                       # default
        sm.get_docs_assigned_to_user("policy", "u1", include_published=True)
    default_params = conn.fake_cursor.executed[0][1]
    incl_params = conn.fake_cursor.executed[1][1]
    assert "published" in default_params       # excluded by default
    assert "published" not in incl_params      # not excluded when included


# ── cancel_workflow (owner check, guards, reset) ──────────────────────────────

def test_cancel_workflow_not_found_raises():
    conn = stubs.make_conn(fetchone=None)
    with stubs.mock_rds(conn, _ALIAS), pytest.raises(sm.WorkflowNotFoundError):
        sm.cancel_workflow("missing", "u1")


def test_cancel_workflow_non_owner_rejected():
    row = {"owner_user_id": "owner", "state": "quality_review", "state_version": 1}
    conn = stubs.make_conn(fetchone=row)
    with stubs.mock_rds(conn, _ALIAS), pytest.raises(sm.WorkflowTransitionError):
        sm.cancel_workflow("w1", "intruder")


@pytest.mark.parametrize("state", ["published", "draft"])
def test_cancel_workflow_illegal_state_rejected(state):
    row = {"owner_user_id": "u1", "state": state, "state_version": 1}
    conn = stubs.make_conn(fetchone=row)
    with stubs.mock_rds(conn, _ALIAS), pytest.raises(sm.WorkflowTransitionError):
        sm.cancel_workflow("w1", "u1")


def test_cancel_workflow_success_resets_and_commits():
    row = {"owner_user_id": "u1", "state": "quality_review", "state_version": 1}
    conn = stubs.make_conn(fetchone=row)
    with stubs.mock_rds(conn, _ALIAS):
        out = sm.cancel_workflow("w1", "u1", comment="never mind")

    sqls = conn.fake_cursor.all_sql()
    assert "UPDATE document_workflow" in sqls and "state='draft'" in sqls
    assert "INSERT INTO document_workflow_events" in sqls
    # the cancel event records the prior state and the owner's comment
    insert = next(e for e in conn.fake_cursor.executed if "INSERT INTO" in e[0])
    assert insert[1][2] == "quality_review"          # from_state = prev
    assert insert[1][5] == "never mind"
    assert conn.commit.called
    assert out == row                                 # returns the (re-read) workflow


# ── get_inbox (role → column/state, pagination) ───────────────────────────────

def test_get_inbox_unknown_role_raises():
    with pytest.raises(ValueError):
        sm.get_inbox("u1", "bogus", "org")


def test_get_inbox_is_org_scoped_not_user_scoped():
    # The inbox is org-wide: it lists ALL workflows in the role's stage for the
    # org, NOT just the caller's assigned items. So the WHERE filters on
    # state + org_id, never on the caller's reviewer column.
    conn = stubs.make_conn(fetchone={"cnt": 2}, fetchall=[{"workflow_id": "w1"}])
    with stubs.mock_rds(conn, _ALIAS):
        rows, total = sm.get_inbox("u1", "quality_reviewer", "org")
    assert total == 2
    assert rows[0]["workflow_id"] == "w1"
    # Every row is enriched for the UI (who it's pending with + viewer gating).
    assert "assignees" in rows[0] and "viewer" in rows[0]
    sqls = conn.fake_cursor.all_sql()
    assert "state=%s" in sqls and "org_id=%s" in sqls
    assert "current_quality_reviewer=%s" not in sqls  # not user-scoped anymore


def test_get_inbox_role_maps_to_stage_state():
    conn = stubs.make_conn(fetchone={"cnt": 0}, fetchall=[])
    with stubs.mock_rds(conn, _ALIAS):
        sm.get_inbox("u1", "approver", "org")
    # COUNT params: [state_filter, org_id] — approver → approval stage.
    assert conn.fake_cursor.executed[0][1] == ["approval", "org"]


def test_get_inbox_doc_type_filter_adds_param():
    conn = stubs.make_conn(fetchone={"cnt": 0}, fetchall=[])
    with stubs.mock_rds(conn, _ALIAS):
        sm.get_inbox("u1", "governance_reviewer", "org", doc_type="policy")
    # COUNT params: [state_filter, org_id, doc_type]
    assert conn.fake_cursor.executed[0][1] == ["governance_review", "org", "policy"]


def test_inbox_viewer_gates_actions_to_current_assignee():
    # Org-wide inbox: the assignee for the current stage gets action permissions;
    # other viewers see the row (assignees populated) but no permitted_actions.
    rows = [{"workflow_id": "w1", "state": "approval", "current_approver": "u1", "owner_user_id": "owner"}]
    sm._attach_inbox_viewer([dict(rows[0])], "u1")  # smoke: no raise on bare row

    assignee_row = dict(rows[0])
    sm._attach_inbox_viewer([assignee_row], "u1")
    assert assignee_row["viewer"]["role"] == "approver"
    assert assignee_row["viewer"]["is_assignee_for_current_step"] is True
    assert "approve" in assignee_row["viewer"]["permitted_actions"]

    bystander_row = dict(rows[0])
    sm._attach_inbox_viewer([bystander_row], "someone_else")
    assert bystander_row["viewer"]["role"] is None
    assert bystander_row["viewer"]["is_assignee_for_current_step"] is False
    assert bystander_row["viewer"]["permitted_actions"] == []


# ── get_workflow_for_doc_any_role ─────────────────────────────────────────────

def test_get_workflow_for_doc_any_role_found():
    conn = stubs.make_conn(fetchone={"workflow_id": "w1", "org_id": "org"})
    with patch.object(sm, "get_user_org_id", return_value="org"), \
         stubs.mock_rds(conn, _ALIAS):
        out = sm.get_workflow_for_doc_any_role("policy", "d1", "u1")
    assert out == {"workflow_id": "w1", "org_id": "org"}


def test_get_workflow_for_doc_any_role_none():
    conn = stubs.make_conn(fetchone=None)
    with patch.object(sm, "get_user_org_id", return_value="org"), \
         stubs.mock_rds(conn, _ALIAS):
        assert sm.get_workflow_for_doc_any_role("policy", "d1", "u1") is None


# ── enrich_workflow_for_viewer (smoke: shape) ─────────────────────────────────

def test_enrich_workflow_for_viewer_adds_expected_keys():
    conn = stubs.make_conn(fetchall=[{"user_id": "o", "email": "o@x.com"}])
    wf = {"workflow_id": "w1", "state": "quality_review", "owner_user_id": "o"}
    with stubs.mock_rds(conn, _ALIAS):
        out = sm.enrich_workflow_for_viewer(wf, "o")
    assert "assignees" in out and "steps" in out and "viewer" in out
    assert out["viewer"]["user_id"] == "o"
    assert isinstance(out["steps"], list)


# ── _apply_single_transition (executed for real against a FakeCursor) ──────────

def test_apply_single_transition_illegal_hop_raises():
    cur = stubs.FakeCursor()
    config = {"states_json": {"transitions": {}}}      # nothing allowed from draft
    row = {"state": "draft", "state_version": 1, "workflow_id": "w1"}
    with pytest.raises(sm.WorkflowTransitionError):
        sm._apply_single_transition(cur, config, row, "published", "a", None, is_auto=False)


def test_apply_single_transition_submit_writes_update_and_event():
    cur = stubs.FakeCursor()
    config = {"states_json": {"transitions": {"draft": ["quality_review"]}}}
    row = {"state": "draft", "state_version": 1, "workflow_id": "w1"}
    new_row, hop = sm._apply_single_transition(
        cur, config, row, "quality_review", "actor-1", "submitting",
        quality_reviewer_user_id="qr-1", is_auto=False)

    # in-memory row reflects the writes
    assert new_row["state"] == "quality_review"
    assert new_row["state_version"] == 2
    assert new_row["current_quality_reviewer"] == "qr-1"
    assert "submitted_at" in new_row
    # hop summary
    assert hop["from_state"] == "draft" and hop["to_state"] == "quality_review"
    assert hop["auto"] is False and hop["comment"] == "submitting"
    # both the UPDATE and the event INSERT were issued on the cursor
    sqls = cur.all_sql()
    assert "UPDATE document_workflow" in sqls
    assert "INSERT INTO document_workflow_events" in sqls


def test_apply_single_transition_claims_role_broadcast_slot():
    cur = stubs.FakeCursor()
    config = {"states_json": {"transitions": {"governance_review": ["approval"]}}}
    # governance stage is role-broadcast (user col NULL, role col set) → actor claims it
    row = {"state": "governance_review", "state_version": 3, "workflow_id": "w1",
           "current_governance_reviewer": None, "current_governance_reviewer_role": "r1"}
    new_row, _hop = sm._apply_single_transition(
        cur, config, row, "approval", "actor-9", None, is_auto=True)
    assert new_row["current_governance_reviewer"] == "actor-9"   # claimed
    assert new_row["current_governance_reviewer_role"] is None    # role cleared
