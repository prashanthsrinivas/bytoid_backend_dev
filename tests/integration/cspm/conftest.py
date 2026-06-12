"""Integration fixtures — real blueprints (azure factory + sg adapter) on a
minimal Flask app, with storage/S3/Redis/Bedrock/workflow all stubbed in-memory.

``bootstrap_sut()`` installs the db/redis stub modules; a fake
``workflow_route.state_machine`` is inserted into sys.modules BEFORE any route
code can import it, recording create/transition calls for assertions.
"""

from __future__ import annotations

import json
import sys
import time
import types

import pytest

from tests.workflow_playbook import _wf_pb_stubs as stubs

stubs.bootstrap_sut()


# ── fake workflow_route.state_machine (recorded) ──────────────────────────────

class WorkflowRecorder:
    def __init__(self):
        self.created: list = []
        self.transitions: list = []
        self.workflows: dict = {}
        self.org_id = "org-1"

    def create_workflow(self, org_id, doc_type, doc_id, doc_version, owner_user_id,
                        quality_reviewer_user_id=None, governance_reviewer_user_id=None,
                        approver_user_id=None):
        wf = {"workflow_id": f"wf-{len(self.created) + 1}", "org_id": org_id,
              "doc_type": doc_type, "doc_id": doc_id, "doc_version": doc_version,
              "owner_user_id": owner_user_id, "state": "draft", "state_version": 1}
        self.created.append(wf)
        self.workflows[wf["workflow_id"]] = wf
        return dict(wf)

    def transition(self, workflow_id, expected_state_version, to_state, actor_user_id,
                   comment=None, **_kw):
        wf = self.workflows[workflow_id]
        wf["state"] = to_state
        wf["state_version"] += 1
        self.transitions.append((workflow_id, to_state, actor_user_id))
        return dict(wf)

    def get_workflow(self, workflow_id):
        return dict(self.workflows[workflow_id])

    def get_user_org_id(self, _user_id):
        return self.org_id


RECORDER = WorkflowRecorder()

_wf_pkg = types.ModuleType("workflow_route")
_wf_sm = types.ModuleType("workflow_route.state_machine")
for name in ("create_workflow", "transition", "get_workflow", "get_user_org_id"):
    setattr(_wf_sm, name, getattr(RECORDER, name))
_wf_pkg.state_machine = _wf_sm

import azure_audit.routes as azure_routes  # noqa: E402
import cspm_core.finding_detail as fd  # noqa: E402
import sg_audit.routes as sg_routes  # noqa: E402

APP = stubs.make_app(azure_routes.azure_audit_bp, sg_routes.sg_audit_bp)


@pytest.fixture(autouse=True)
def _fake_workflow(monkeypatch):
    """Fixture-scoped sys.modules stub — the route code imports
    workflow_route.state_machine lazily at request time, and a module-level
    insertion would leak into the rest of the session's collection."""
    monkeypatch.setitem(sys.modules, "workflow_route", _wf_pkg)
    monkeypatch.setitem(sys.modules, "workflow_route.state_machine", _wf_sm)


class _Bucket:
    def __init__(self, store):
        self._store = store

    def put_object(self, Bucket=None, Key=None, Body=None, ContentType=None):
        self._store[Key] = json.loads(Body)


@pytest.fixture
def recorder():
    RECORDER.created.clear()
    RECORDER.transitions.clear()
    RECORDER.workflows.clear()
    return RECORDER


@pytest.fixture
def client():
    return APP.test_client()


@pytest.fixture
def s3_store(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(fd, "read_json_from_s3", store.get)
    monkeypatch.setattr(fd, "s3bucket", lambda: _Bucket(store))
    monkeypatch.setattr(fd, "S3_BUCKET", "test-bucket", raising=False)
    return store


@pytest.fixture
def seeded(monkeypatch, s3_store):
    """Seed a snapshot behind both providers' storages + stub locks, Bedrock and
    the org-admin lookup. Returns (s3_store, snapshot)."""
    import cspm_core.action_plan as ap
    import cspm_core.helpers as helpers
    import cspm_core.remediation as remediation
    from cspm_core.storage import CspmStorage
    from sg_audit.storage import SgAuditStorage
    from tests.unit.cspm.conftest import sg_finding, snapshot

    snap = snapshot([
        sg_finding(fid="f1"),
        sg_finding("SG_MISSING_RULE_DESCRIPTION", fid="f2", severity="info"),
    ])
    azure_snap = {
        "scan_id": "scan-az", "audit_id": "a1", "scanned_at": "2026-06-12T00:00:00Z",
        "findings": [{
            "finding_id": "az-f1", "domain": "data", "category": "data_exposure",
            "rule_id": "AZ_STORAGE_PUBLIC_BLOB", "severity": "high",
            "finding_summary": "Storage account 'prodstore1' allows public blob access",
            "evidence_type": "posture_finding", "source": "azure",
            "collected_at": "2026-06-12T00:00:00Z",
            "risk_indicators": ["AZ_STORAGE_PUBLIC_BLOB"],
            "supporting_details": {
                "scope_id": "sub-123", "scope_name": "Prod", "region": "eastus",
                "entity_type": "storage_account", "entity_name": "prodstore1",
                "entity_id": "/subscriptions/sub-123/resourceGroups/rg-data/providers/"
                             "Microsoft.Storage/storageAccounts/prodstore1",
                "rule_id": "AZ_STORAGE_PUBLIC_BLOB"},
        }],
    }

    monkeypatch.setattr(CspmStorage, "get_latest_snapshot", lambda self, u, a: azure_snap)
    monkeypatch.setattr(CspmStorage, "get_snapshot", lambda self, u, a, s: azure_snap)
    monkeypatch.setattr(CspmStorage, "get_recommendation", lambda self, u, a, s: None)
    monkeypatch.setattr(SgAuditStorage, "get_latest_snapshot", lambda self, u, a: snap)
    monkeypatch.setattr(SgAuditStorage, "get_snapshot", lambda self, u, a, s: snap)
    monkeypatch.setattr(SgAuditStorage, "get_recommendation", lambda self, u, a, s: None)

    async def _lock(ns, key):
        return True

    async def _unlock(ns, key):
        return None

    monkeypatch.setattr(helpers, "acquire_rec_inflight", _lock)
    monkeypatch.setattr(helpers, "release_rec_inflight", _unlock)

    async def fake_bedrock(uid, prompt, temp=0.1):
        raise RuntimeError("bedrock stubbed out")  # deterministic fallback path

    monkeypatch.setattr(ap, "_bedrock", fake_bedrock)
    monkeypatch.setattr(remediation, "_resolve_org_admin",
                        lambda uid: ("admin-1", "admin@test.io"))
    return s3_store, snap


def wait_for_plan(store, key, timeout=5.0):
    """Poll the in-memory sidecar until async generation lands."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        plan = store.get(key)
        if plan and plan.get("status") != "generating":
            return plan
        time.sleep(0.05)
    raise AssertionError(f"plan generation timed out for {key}")
