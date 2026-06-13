"""Phase 5 — cloud auto-fill sourced from CSPM posture snapshots.

The posture resolver uses ``get_latest_snapshot`` only; a provider with no
snapshot is unavailable and excluded ("never displayed → never available"); the
availability endpoint isolates providers; and legacy connector selections still
route through the old connector path.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from tests.workflow_playbook import _wf_pb_stubs as stubs

stubs.bootstrap_sut()

# playbook.routes imports services.redis_service.RedisService (the bootstrap stub
# only exposes get_redis) — add it so the blueprint import resolves.
_rs = sys.modules.get("services.redis_service")
if _rs is not None and not hasattr(_rs, "RedisService"):
    _rs.RedisService = MagicMock(name="RedisService")

import playbook.cloud_autofill as ca  # noqa: E402

pytestmark = pytest.mark.unit


class FakeStorage:
    """Minimal stand-in matching SgAuditStorage / CspmStorage interface."""

    def __init__(self, audits=None, indexes=None, snapshots=None):
        self._audits = audits or []
        self._indexes = indexes or {}      # audit_id -> index list
        self._snapshots = snapshots or {}  # audit_id -> latest snapshot

    def list_audits(self, user_id):
        return list(self._audits)

    def list_snapshot_index(self, user_id, audit_id):
        return list(self._indexes.get(audit_id, []))

    def get_latest_snapshot(self, user_id, audit_id):
        return self._snapshots.get(audit_id)


# ── get_provider_availability ──────────────────────────────────────────────────

def test_availability_true_only_when_snapshot_exists():
    aws = FakeStorage(
        audits=[{"audit_id": "a1", "name": "Prod AWS"}],
        indexes={"a1": [{"scan_id": "s1", "scanned_at": "2026-06-10T00:00:00Z"}]},
    )
    azure = FakeStorage(audits=[{"audit_id": "z1", "name": "Az"}], indexes={"z1": []})
    gcp = FakeStorage(audits=[])  # no audits at all

    def _store(provider):
        return {"aws": aws, "azure": azure, "gcp": gcp}[provider]

    with patch.object(ca, "_storage_for", side_effect=_store):
        out = ca.get_provider_availability("u1")
    assert out["aws"]["available"] is True
    assert out["aws"]["audits"][0]["latest_scanned_at"] == "2026-06-10T00:00:00Z"
    # audit exists but never scanned → not ingestible
    assert out["azure"]["available"] is False
    assert out["gcp"]["available"] is False


def test_availability_isolates_provider_errors():
    aws = FakeStorage(
        audits=[{"audit_id": "a1", "name": "AWS"}],
        indexes={"a1": [{"scan_id": "s1", "scanned_at": "t"}]},
    )

    def _store(provider):
        if provider == "azure":
            raise RuntimeError("storage down")
        if provider == "gcp":
            raise RuntimeError("storage down")
        return aws

    with patch.object(ca, "_storage_for", side_effect=_store):
        out = ca.get_provider_availability("u1")
    assert out["aws"]["available"] is True
    assert out["azure"] == {"available": False, "audits": []}
    assert out["gcp"] == {"available": False, "audits": []}


# ── resolve_posture_payload ─────────────────────────────────────────────────

def test_resolve_uses_latest_snapshot_and_tags_source():
    snap = {
        "scan_id": "s1", "scanned_at": "2026-06-10T00:00:00Z",
        "risk_score": 14.5, "posture_score": 85.5,
        "counts": {"critical": 2}, "findings": [{"id": "f1", "sev": "critical"}],
    }
    aws = FakeStorage(
        audits=[{"audit_id": "a1"}],
        indexes={"a1": [{"scan_id": "s1", "scanned_at": "2026-06-10T00:00:00Z"}]},
        snapshots={"a1": snap},
    )
    with patch.object(ca, "_storage_for", return_value=aws):
        text, blob = ca.resolve_posture_payload(
            "u1", [{"provider": "aws", "audit_id": "a1"}])
    assert "[SOURCE provider=aws audit=a1 scanned_at=2026-06-10T00:00:00Z]" in text
    assert "critical" in text
    assert blob[0]["source"] == "posture"
    assert blob[0]["data"]["risk_score"] == 14.5


def test_resolve_missing_snapshot_records_error():
    empty = FakeStorage(audits=[{"audit_id": "a1"}], indexes={"a1": []}, snapshots={})
    with patch.object(ca, "_storage_for", return_value=empty):
        text, blob = ca.resolve_posture_payload(
            "u1", [{"provider": "azure", "audit_id": "a1"}])
    assert text == ""
    assert blob[0]["error"] == "no snapshot"


def test_resolve_defaults_to_latest_audit_when_no_audit_id():
    snap = {"scan_id": "s2", "scanned_at": "t", "risk_score": 1, "posture_score": 2,
            "counts": {}, "findings": []}
    storage = FakeStorage(
        audits=[{"audit_id": "newest"}, {"audit_id": "older"}],
        indexes={"newest": [{"scan_id": "s2", "scanned_at": "t"}]},
        snapshots={"newest": snap},
    )
    with patch.object(ca, "_storage_for", return_value=storage):
        text, blob = ca.resolve_posture_payload("u1", [{"provider": "gcp"}])
    assert "audit=newest" in text


def test_resolve_truncates_to_max_chars():
    big = {"scan_id": "s", "scanned_at": "t", "risk_score": 0, "posture_score": 0,
           "counts": {}, "findings": [{"x": "y" * 100000}]}
    storage = FakeStorage(
        audits=[{"audit_id": "a"}],
        indexes={"a": [{"scan_id": "s", "scanned_at": "t"}]},
        snapshots={"a": big},
    )
    with patch.object(ca, "_storage_for", return_value=storage):
        text, _ = ca.resolve_posture_payload(
            "u1", [{"provider": "aws", "audit_id": "a"}], max_chars=500)
    # header + truncated body; the snapshot summary itself is capped at 500
    assert len(text) < 1200


# ── availability endpoint ──────────────────────────────────────────────────

def test_availability_endpoint():
    from playbook.routes import playbook_bp

    app = stubs.make_app(playbook_bp)
    fake = {"aws": {"available": True, "audits": []},
            "azure": {"available": False, "audits": []},
            "gcp": {"available": False, "audits": []}}
    with stubs.allow_auth(), \
         patch("playbook.cloud_autofill.get_provider_availability", return_value=fake):
        client = app.test_client()
        resp = client.get("/workflow/cloud_autofill/availability?user_id=u1")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["providers"]["aws"]["available"] is True


def test_availability_endpoint_requires_user_id():
    from playbook.routes import playbook_bp

    app = stubs.make_app(playbook_bp)
    with stubs.allow_auth():
        client = app.test_client()
        resp = client.get("/workflow/cloud_autofill/availability")
    assert resp.status_code == 400
