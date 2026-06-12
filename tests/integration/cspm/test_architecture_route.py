"""Integration — architecture endpoint through both blueprints."""

from __future__ import annotations

from tests.workflow_playbook import _wf_pb_stubs as stubs

UID = "u1"


def test_architecture_sg(client, seeded):
    with stubs.allow_auth():
        res = client.get("/sg-audit/audit/a1/architecture", query_string={"user_id": UID})
    assert res.status_code == 200
    body = res.get_json()
    assert body["status"] == "success"
    assert body["totals"]["findings"] == 2
    scope = body["scopes"][0]
    assert scope["id"] == "394711685916"
    entity = scope["regions"][0]["entities"][0]
    assert entity["max_severity"] == "critical"
    assert entity["findings"][0]["rule_label"]


def test_architecture_azure_uses_scope_fields(client, seeded):
    with stubs.allow_auth():
        res = client.get("/azure-audit/audit/a1/architecture", query_string={"user_id": UID})
    assert res.status_code == 200
    body = res.get_json()
    scope = body["scopes"][0]
    assert scope["id"] == "sub-123" and scope["name"] == "Prod"
    assert scope["regions"][0]["entities"][0]["entity_name"] == "prodstore1"


def test_architecture_404_without_scan(client, seeded, monkeypatch):
    from sg_audit.storage import SgAuditStorage

    monkeypatch.setattr(SgAuditStorage, "get_latest_snapshot", lambda self, u, a: None)
    with stubs.allow_auth():
        res = client.get("/sg-audit/audit/a1/architecture", query_string={"user_id": UID})
    assert res.status_code == 404


def test_architecture_denied(client, seeded):
    with stubs.deny_auth():
        res = client.get("/sg-audit/audit/a1/architecture", query_string={"user_id": UID})
    assert res.status_code == 403
