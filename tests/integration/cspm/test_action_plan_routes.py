"""Integration — action-plan lifecycle through the real blueprints.

Covers both wiring styles: the cspm_core routes_factory (azure) and the
sg_audit adapter (aws). Generation -> poll -> edit -> request-approval, plus
permission denial and the no-execution guarantee.
"""

from __future__ import annotations

import pytest

from tests.integration.cspm.conftest import wait_for_plan
from tests.workflow_playbook import _wf_pb_stubs as stubs

UID = "u1"

CASES = [
    ("azure", "/azure-audit/audit/a1", f"{UID}/azure_audit/audits/a1.action_plan.json",
     "AZ_STORAGE_PUBLIC_BLOB", "az "),
    ("sg", "/sg-audit/audit/a1", f"{UID}/sg_audit/audits/a1.action_plan.json",
     "SG_ADMIN_WORLD_INGRESS", "aws "),
]


def _generate(client, base, store, key):
    res = client.post(f"{base}/action-plan", json={"user_id": UID})
    assert res.status_code in (200, 202)
    return wait_for_plan(store, key)


@pytest.mark.parametrize("provider,base,key,top_rule,tool", CASES)
def test_full_lifecycle(client, seeded, recorder, provider, base, key, top_rule, tool):
    store, _snap = seeded
    with stubs.allow_auth():
        plan = _generate(client, base, store, key)
        assert plan["status"] == "success"

        # GET returns the stored plan (ready)
        res = client.get(f"{base}/action-plan", query_string={"user_id": UID})
        assert res.status_code == 200
        body = res.get_json()
        assert body["status"] == "ready"
        points = body["plan"]["action_points"]
        assert points[0]["rule_id"] == top_rule
        assert points[0]["commands"][0]["command"].startswith(tool)
        # Bedrock was stubbed to fail -> deterministic fallback note
        assert any("unavailable" in n for n in body["plan"]["notes"])
        pid = points[0]["point_id"]

        # human edit persists
        res = client.post(f"{base}/action-plan/point/{pid}/command",
                          json={"user_id": UID, "index": 0,
                                "command": f"{tool.strip()} edited-by-human --flag"})
        assert res.status_code == 200
        assert res.get_json()["point"]["commands"][0]["edited"] is True

        # request approval -> workflow created with the provider doc_type
        res = client.post(f"{base}/action-plan/point/{pid}/request-approval",
                          json={"user_id": UID})
        assert res.status_code == 201
        approval = res.get_json()["point"]["approval"]
        assert approval["workflow_id"] == recorder.created[-1]["workflow_id"]
        assert recorder.created[-1]["doc_type"] == f"{provider}_action_point"
        assert recorder.created[-1]["doc_id"] == f"a1:{pid}"
        assert approval["state"] == "quality_review"
        assert approval["approver"] == "admin-1"

        # second request is idempotent
        res = client.post(f"{base}/action-plan/point/{pid}/request-approval",
                          json={"user_id": UID})
        assert res.status_code == 200 and res.get_json()["status"] == "exists"

        # live state reflected on GET
        res = client.get(f"{base}/action-plan", query_string={"user_id": UID})
        pts = res.get_json()["plan"]["action_points"]
        assert next(p for p in pts if p["point_id"] == pid)["approval"]["state"] == "quality_review"


@pytest.mark.parametrize("provider,base,key,top_rule,tool", CASES)
def test_permission_denied(client, seeded, provider, base, key, top_rule, tool):
    with stubs.deny_auth():
        assert client.post(f"{base}/action-plan", json={"user_id": UID}).status_code == 403
        assert client.get(f"{base}/action-plan",
                          query_string={"user_id": UID}).status_code == 403
        assert client.post(f"{base}/action-plan/point/x/command",
                           json={"user_id": UID, "index": 0, "command": "aws x"}).status_code == 403
        assert client.post(f"{base}/action-plan/point/x/request-approval",
                           json={"user_id": UID}).status_code == 403


def test_no_fixer_is_ever_invoked(client, seeded, recorder, monkeypatch):
    """The plan lifecycle must never touch the auto-remediation fixers."""
    from azure_audit.provider import AZURE_PROVIDER

    def _boom(*_a, **_kw):
        raise AssertionError("fixer executed during action-plan flow")

    for rule_id in list(AZURE_PROVIDER.fixers):
        monkeypatch.setitem(AZURE_PROVIDER.fixers, rule_id, _boom)

    store, _ = seeded
    with stubs.allow_auth():
        plan = _generate(client, "/azure-audit/audit/a1", store,
                         f"{UID}/azure_audit/audits/a1.action_plan.json")
        assert plan["status"] == "success"
        pid = plan["action_points"][0]["point_id"]
        client.post(f"/azure-audit/audit/a1/action-plan/point/{pid}/request-approval",
                    json={"user_id": UID})


def test_missing_user_id_rejected(client, seeded):
    with stubs.allow_auth():
        res = client.post("/sg-audit/audit/a1/action-plan", json={})
        assert res.status_code == 400
