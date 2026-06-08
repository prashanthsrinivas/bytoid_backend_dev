"""Dashboard + scheduler + retention + storage-purge tests (Phase 4)."""

import asyncio
import json

import pytest

from vra import dashboard as dash
from vra import retention as ret
from vra import scheduler as sched
from vra import storage as storage_mod
from vra.osint.normalize import build_snapshot, make_finding
from vra.report_template import resolve_dashboard_url
from vra.schema import CAT_COMPLIANCE, CAT_SECURITY, SEV_CRITICAL, SEV_HIGH, SEV_LOW
from vra.service import VraService
from vra.storage import VraStorage


def _run(coro):
    return asyncio.run(coro)


def _f(cat, sev, et="t", details=None, url="http://e"):
    return make_finding(category=cat, evidence_type=et, source="S", finding_summary="x",
                        source_url=url, severity=sev, supporting_details=details or {})


def _snap(findings, scan_id="s1", scanned_at="2026-06-08T00:00:00Z"):
    return build_snapshot(scan_id=scan_id, assessment_id="a1", vendor_name="Acme",
                          vendor_domain="acme.com", findings=findings, scanned_at=scanned_at)


# ── dashboard ────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_dashboard_executive_summary_counts():
    snap = _snap([_f(CAT_SECURITY, SEV_CRITICAL), _f(CAT_SECURITY, SEV_HIGH), _f(CAT_COMPLIANCE, SEV_LOW)])
    d = dash.build_dashboard({"assessment_id": "a1", "vendor_name": "Acme"}, snap, [], prior_scores=[])
    es = d["executive_summary"]
    assert es["overall_risk_rating"] == "Critical"
    assert es["critical_findings"] == 1 and es["high_findings"] == 1 and es["low_findings"] == 1
    assert es["total_findings"] == 3
    assert d["scanned"] is True


@pytest.mark.unit
def test_dashboard_risk_overview_and_categories():
    snap = _snap([
        _f(CAT_SECURITY, SEV_HIGH),
        _f(CAT_COMPLIANCE, SEV_LOW, et="certification_mention", details={"certifications": ["SOC 2"]}),
        _f(CAT_COMPLIANCE, SEV_LOW, et="trust_center"),
    ])
    d = dash.build_dashboard({"assessment_id": "a1", "vendor_name": "Acme"}, snap, [{"scanned_at": "x", "risk_score": 1}])
    ro = d["risk_overview"]
    assert ro["finding_distribution"][SEV_HIGH] == 1
    assert ro["compliance_coverage"]["certifications"] == ["SOC 2"]
    assert ro["compliance_coverage"]["has_trust_center"] is True
    assert d["categories"]["security"]["count"] == 1
    assert any(c["category"] == "compliance" and c["count"] == 2 for c in ro["category_summary"])


@pytest.mark.unit
def test_dashboard_never_scanned():
    d = dash.build_dashboard({"assessment_id": "a1", "vendor_name": "Acme", "scan_state": "pending"}, None)
    assert d["scanned"] is False
    assert d["executive_summary"]["overall_risk_rating"] == "Unknown"


@pytest.mark.unit
def test_dashboard_url(monkeypatch):
    monkeypatch.setattr(dash, "VRA_DASHBOARD_BASE_URL", "https://app.bytoid.ai")
    assert dash.dashboard_url("a1") == "https://app.bytoid.ai/vra/dashboard/a1"
    monkeypatch.setattr(dash, "VRA_DASHBOARD_BASE_URL", "")
    assert dash.dashboard_url("a1") == ""


@pytest.mark.unit
def test_resolve_dashboard_placeholder(monkeypatch):
    monkeypatch.setattr(dash, "VRA_DASHBOARD_BASE_URL", "https://app.bytoid.ai")
    struct = {"blocks": [{"items": ["Live dashboard: {{VRA_DASHBOARD_URL}}"]}]}
    out = resolve_dashboard_url(struct, "a1")
    assert out["blocks"][0]["items"][0] == "Live dashboard: https://app.bytoid.ai/vra/dashboard/a1"


# ── scheduler ────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_compute_next_scan_at(monkeypatch):
    from datetime import datetime, timezone
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(sched, "VRA_RESCAN_CADENCE_DAYS", 30)
    assert sched.compute_next_scan_at(now) == "2026-01-31T00:00:00Z"
    assert sched.compute_next_scan_at(now, cadence_days=0) is None


@pytest.mark.unit
def test_is_due_for_rescan():
    assert sched.is_due_for_rescan({"scan_state": "complete", "next_scan_at": "2026-01-01T00:00:00Z"}, "2026-02-01T00:00:00Z")
    assert not sched.is_due_for_rescan({"scan_state": "complete", "next_scan_at": "2026-03-01T00:00:00Z"}, "2026-02-01T00:00:00Z")
    assert not sched.is_due_for_rescan({"scan_state": "pending", "next_scan_at": "2020-01-01T00:00:00Z"}, "2026-02-01T00:00:00Z")
    assert not sched.is_due_for_rescan({"scan_state": "complete"}, "2026-02-01T00:00:00Z")


class _MemStore:
    def __init__(self):
        self.db = {}

    def get_assessment(self, u, a):
        return self.db.get((u, a))

    def save_assessment(self, u, r):
        self.db[(u, r["assessment_id"])] = dict(r)
        return r

    def list_assessments(self, u):
        return [v for (uu, _a), v in self.db.items() if uu == u]


@pytest.mark.unit
def test_rescan_due_triggers_only_due():
    s = VraService(storage=_MemStore())
    s.storage.save_assessment("u1", {"assessment_id": "due", "scan_state": "complete", "next_scan_at": "2000-01-01T00:00:00Z", "vendor_name": "A", "vendor_domain": "a.com"})
    s.storage.save_assessment("u1", {"assessment_id": "notdue", "scan_state": "complete", "next_scan_at": "2999-01-01T00:00:00Z"})
    calls = []

    async def trigger(uid, aid, **k):
        calls.append(aid)
        return {"status": "launched"}

    out = _run(sched.rescan_due("u1", service=s, trigger=trigger, now_iso="2026-06-08T00:00:00Z"))
    assert calls == ["due"]
    assert out[0]["assessment_id"] == "due"


@pytest.mark.unit
def test_reconcile_pending_triggers_ready_unscanned():
    s = VraService(storage=_MemStore())
    s.storage.save_assessment("u1", {"assessment_id": "p", "scan_state": "pending", "vendor_name": "A", "vendor_domain": "a.com"})
    s.storage.save_assessment("u1", {"assessment_id": "noweb", "scan_state": "pending", "vendor_name": "A", "vendor_domain": ""})
    s.storage.save_assessment("u1", {"assessment_id": "done", "scan_state": "complete", "vendor_name": "A", "vendor_domain": "a.com", "latest_scan_id": "x"})
    calls = []

    async def trigger(uid, aid, **k):
        calls.append(aid)
        return {"status": "launched"}

    _run(sched.reconcile_pending("u1", service=s, trigger=trigger))
    assert calls == ["p"]


# ── retention (real VraStorage + in-memory S3) ───────────────────────────────

class _FakeKMS:
    def encrypt(self, user_id, plaintext):
        return {"ciphertext": plaintext.encode().hex(), "iv": "iv", "encrypted_key": f"k:{user_id}"}

    def decrypt(self, user_id, encrypted_key, iv, ciphertext):
        return bytes.fromhex(ciphertext).decode()


@pytest.fixture
def s3_storage(monkeypatch):
    backing = {}

    class _S3:
        def put_object(self, Bucket, Key, Body, ContentType=None):
            backing[Key] = Body.decode("utf-8") if isinstance(Body, bytes) else Body

    monkeypatch.setattr(storage_mod, "s3bucket", lambda: _S3())
    monkeypatch.setattr(storage_mod, "SecureKMSService", _FakeKMS)
    monkeypatch.setattr(storage_mod, "read_json_from_s3", lambda k: json.loads(backing[k]) if k in backing else None)
    monkeypatch.setattr(storage_mod, "delete_file_from_s3", lambda k: backing.pop(k, None))
    monkeypatch.setattr(storage_mod, "delete_folder_from_s3", lambda p: [backing.pop(k) for k in list(backing) if k.startswith(p)])
    import utils.s3_utils as s3u
    monkeypatch.setattr(s3u, "list_all_files", lambda prefix=None: [{"Key": k} for k in backing if k.startswith(prefix or "")])
    return VraStorage(), backing


@pytest.mark.unit
def test_purge_snapshots_keeps_latest(s3_storage):
    storage, _ = s3_storage
    for sid, when in [("old1", "2025-01-01T00:00:00Z"), ("old2", "2025-02-01T00:00:00Z"), ("new", "2026-06-01T00:00:00Z")]:
        storage.save_snapshot("u1", {"scan_id": sid, "assessment_id": "a1", "scanned_at": when, "risk_score": 1.0, "counts": {}, "findings": []})
    removed = storage.purge_snapshots_before("u1", "a1", "2026-01-01T00:00:00Z", keep_latest=True)
    assert removed == 2
    remaining = [e["scan_id"] for e in storage.list_snapshot_index("u1", "a1")]
    assert remaining == ["new"]


@pytest.mark.unit
def test_retention_purge_expired(s3_storage, monkeypatch):
    from datetime import datetime, timezone
    storage, _ = s3_storage
    svc = VraService(storage=storage)
    # an assessment must exist for list_assessments to return it
    storage.save_assessment("u1", {"assessment_id": "a1", "created_at": "2025-01-01"})
    storage.save_snapshot("u1", {"scan_id": "old", "assessment_id": "a1", "scanned_at": "2024-01-01T00:00:00Z", "risk_score": 1.0, "counts": {}, "findings": []})
    storage.save_snapshot("u1", {"scan_id": "recent", "assessment_id": "a1", "scanned_at": "2026-06-01T00:00:00Z", "risk_score": 1.0, "counts": {}, "findings": []})
    out = ret.purge_expired("u1", service=svc, now=datetime(2026, 6, 8, tzinfo=timezone.utc), retention_days=90)
    assert out["purged"] == 1
    assert [e["scan_id"] for e in storage.list_snapshot_index("u1", "a1")] == ["recent"]
