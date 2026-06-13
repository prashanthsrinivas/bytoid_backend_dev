"""Unit tests for the grounded "overall security posture" brief.

The brief feeds the cloud auto-fill (so the AI sees positive aspects, not just
findings) and the Security Posture UI surfaces. Positives must come only from
real passing controls / real severity counts — never invented.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from tests.workflow_playbook import _wf_pb_stubs as stubs

stubs.bootstrap_sut()

_rs = sys.modules.get("services.redis_service")
if _rs is not None and not hasattr(_rs, "RedisService"):
    _rs.RedisService = MagicMock(name="RedisService")

import playbook.posture_brief as pb  # noqa: E402

pytestmark = pytest.mark.unit


class FakeStorage:
    def __init__(self, audits=None, indexes=None, snapshots=None):
        self._audits = audits or []
        self._indexes = indexes or {}
        self._snapshots = snapshots or {}

    def list_audits(self, user_id):
        return list(self._audits)

    def list_snapshot_index(self, user_id, audit_id):
        return list(self._indexes.get(audit_id, []))

    def get_latest_snapshot(self, user_id, audit_id):
        return self._snapshots.get(audit_id)


# ── _derive_positives (pure) ────────────────────────────────────────────────

def test_derive_positives_from_compliance_and_clean_severity():
    snap = {"counts": {"by_severity": {"critical": 0, "high": 0}}}
    compliance = [{"framework_label": "CIS", "passing": 40, "evaluated": 50, "coverage_pct": 80.0}]
    pos = pb._derive_positives(snap, compliance, ["base signal"])
    assert "base signal" in pos
    assert any("No critical" in p for p in pos)
    assert any("No high" in p for p in pos)
    assert any("40/50 controls passing" in p for p in pos)


def test_derive_positives_omits_severity_signal_when_findings_present():
    snap = {"counts": {"by_severity": {"critical": 1, "high": 3}}}
    pos = pb._derive_positives(snap, [], [])
    assert not any("No critical" in p for p in pos)
    assert not any("No high" in p for p in pos)


# ── build_posture_brief ─────────────────────────────────────────────────────

def test_build_brief_includes_positives_and_markdown():
    snap = {"scanned_at": "t", "counts": {"by_severity": {"critical": 0, "high": 0}}, "findings": []}
    aws = FakeStorage(
        audits=[{"audit_id": "a1"}],
        indexes={"a1": [{"scan_id": "s1", "scanned_at": "t"}]},
        snapshots={"a1": snap},
    )
    ctx = {"posture_rating": "Good", "posture_score": 90, "risk_score": 5,
           "scanned_at": "t", "key_risk_drivers": []}
    compliance = [{"framework": "cis", "framework_label": "CIS", "passing": 40,
                   "evaluated": 50, "coverage_pct": 80.0, "failing": 10}]
    with patch.object(pb, "_storage_for", return_value=aws), \
         patch.object(pb, "_analysis", return_value=(ctx, compliance, [], [])):
        out = pb.build_posture_brief("u1", [{"provider": "aws", "audit_id": "a1"}])

    p = out["providers"][0]
    assert p["available"] is True
    assert any("40/50 controls passing" in s for s in p["positives"])
    assert "Positive Aspects" in p["markdown"]
    assert out["brief_text"]


def test_build_brief_marks_unavailable_without_snapshot():
    empty = FakeStorage(audits=[{"audit_id": "a1"}], indexes={"a1": []}, snapshots={})
    with patch.object(pb, "_storage_for", return_value=empty):
        out = pb.build_posture_brief("u1", [{"provider": "azure"}])
    assert out["providers"][0]["available"] is False
    assert out["brief_text"] == ""


def test_build_brief_isolates_provider_errors():
    with patch.object(pb, "_storage_for", side_effect=RuntimeError("storage down")):
        out = pb.build_posture_brief("u1", [{"provider": "gcp"}])
    assert out["providers"][0]["available"] is False
    assert "storage down" in out["providers"][0]["error"]


def test_build_brief_skips_unknown_providers():
    out = pb.build_posture_brief("u1", [{"provider": "oracle"}])
    assert out["providers"] == []
