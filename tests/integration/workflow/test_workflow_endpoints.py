"""§4 (Phase 4) — ``workflow_bp`` route integration via Flask test client.

Covers the contract/validation/authZ surface for representative endpoints using
the shared ``make_app`` factory and ``allow_auth``/``deny_auth`` toggles. DB is a
fake connection wired via ``mock_rds``.
"""

from __future__ import annotations

import pytest

from tests.workflow_playbook import _wf_pb_stubs as stubs

stubs.bootstrap_sut()

import workflow_route.routes as wr  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.contract]

_ALIAS = "workflow_route.routes"


@pytest.fixture
def client():
    app = stubs.make_app(wr.workflow_bp)
    return app.test_client()


# ── GET /workflow/assignable-users (inline validation, no decorator) ──────────

def test_assignable_users_requires_user_id(client):
    resp = client.get("/workflow/assignable-users")
    assert resp.status_code == 400
    assert "user_id" in resp.get_json()["error"]


def test_assignable_users_unknown_caller_returns_empty(client):
    conn = stubs.make_conn(fetchone=None)        # caller row not found
    with stubs.mock_rds(conn, _ALIAS):
        resp = client.get("/workflow/assignable-users?user_id=u1")
    assert resp.status_code == 200
    assert resp.get_json() == {"users": []}


# ── POST /workflow/submit (permission_required_body) ──────────────────────────

def test_submit_denied_without_permission(client):
    with stubs.deny_auth():
        resp = client.post("/workflow/submit", json={"user_id": "u1",
                                                      "doc_type": "policy",
                                                      "doc_id": "d1"})
    assert resp.status_code == 403


def test_submit_validation_400_when_authorized(client):
    # auth passes → handler runs → missing required fields → 400
    with stubs.allow_auth():
        resp = client.post("/workflow/submit", json={})
    assert resp.status_code == 400
    body = resp.get_json()
    assert "doc_id" in body["error"] and "doc_type" in body["error"]


def test_submit_malformed_json_is_handled(client):
    with stubs.allow_auth():
        resp = client.post("/workflow/submit", data="not-json",
                            content_type="application/json")
    # silent JSON parse → {} → validation 400 (never an unhandled 500)
    assert resp.status_code == 400
