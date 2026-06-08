"""S3 storage round-trip tests — vra/storage.py.

Uses an in-memory S3 backend + a trivial KMS fake so save/get/list/trend/delete
are exercised end-to-end without AWS. Verifies findings are encrypted at rest
and never stored in clear.
"""

import json

import pytest

from vra import storage as storage_mod
from vra.storage import VraStorage


class _FakeS3:
    """Minimal in-memory stand-in for the boto3 S3 client used by storage.py."""

    def __init__(self, store):
        self.store = store

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.store[Key] = Body.decode("utf-8") if isinstance(Body, bytes) else Body


class _FakeKMS:
    """Reversible 'encryption' that hex-obscures the plaintext (so the
    plaintext never appears literally in the stored object — mirroring real
    KMS opacity for the 'encrypted at rest' assertion)."""

    def encrypt(self, user_id, plaintext):
        ct = plaintext.encode("utf-8").hex()
        return {"ciphertext": ct, "iv": "iv", "encrypted_key": f"k:{user_id}"}

    def decrypt(self, user_id, encrypted_key, iv, ciphertext):
        assert encrypted_key == f"k:{user_id}"
        return bytes.fromhex(ciphertext).decode("utf-8")


@pytest.fixture
def store(monkeypatch):
    backing = {}
    fake = _FakeS3(backing)

    monkeypatch.setattr(storage_mod, "s3bucket", lambda: fake)
    monkeypatch.setattr(storage_mod, "SecureKMSService", _FakeKMS)

    def _read_json(key):
        raw = backing.get(key)
        return json.loads(raw) if raw is not None else None

    def _delete_file(key):
        backing.pop(key, None)

    def _delete_folder(prefix):
        for k in [k for k in backing if k.startswith(prefix)]:
            backing.pop(k, None)

    monkeypatch.setattr(storage_mod, "read_json_from_s3", _read_json)
    monkeypatch.setattr(storage_mod, "delete_file_from_s3", _delete_file)
    monkeypatch.setattr(storage_mod, "delete_folder_from_s3", _delete_folder)
    # _list_keys imports list_all_files lazily from utils.s3_utils
    import utils.s3_utils as s3u

    monkeypatch.setattr(
        s3u,
        "list_all_files",
        lambda prefix=None: [{"Key": k} for k in backing if k.startswith(prefix or "")],
    )

    return VraStorage(), backing


# ── assessment mapping ───────────────────────────────────────────────────────

@pytest.mark.unit
def test_assessment_roundtrip(store):
    s, _ = store
    rec = {"assessment_id": "a1", "vendor_name": "Acme", "created_at": "2026-01-01"}
    s.save_assessment("u1", rec)
    assert s.get_assessment("u1", "a1") == rec
    assert s.get_assessment("u1", "missing") is None


@pytest.mark.unit
def test_list_assessments_sorted_desc(store):
    s, _ = store
    s.save_assessment("u1", {"assessment_id": "a1", "created_at": "2026-01-01"})
    s.save_assessment("u1", {"assessment_id": "a2", "created_at": "2026-03-01"})
    ids = [r["assessment_id"] for r in s.list_assessments("u1")]
    assert ids == ["a2", "a1"]


@pytest.mark.unit
def test_assessment_user_scoped(store):
    s, backing = store
    s.save_assessment("u1", {"assessment_id": "a1", "created_at": "x"})
    assert s.get_assessment("u2", "a1") is None
    assert all(k.startswith("u1/vra/") for k in backing)


# ── snapshots ────────────────────────────────────────────────────────────────

def _snapshot(scan_id, scanned_at, score, findings):
    return {
        "scan_id": scan_id,
        "assessment_id": "a1",
        "vendor_name": "Acme",
        "vendor_domain": "acme.com",
        "scanned_at": scanned_at,
        "risk_score": score,
        "counts": {"total": len(findings)},
        "collector_status": {},
        "findings": findings,
    }


@pytest.mark.unit
def test_snapshot_findings_encrypted_at_rest(store):
    s, backing = store
    s.save_snapshot("u1", _snapshot("s1", "2026-01-01T00:00:00Z", 40.0, [{"x": "secret"}]))
    raw = backing["u1/vra/intelligence/a1/s1.json"]
    assert "secret" not in raw            # plaintext finding never on disk
    assert "findings_enc" in raw
    # but a read decrypts it back
    snap = s.get_snapshot("u1", "a1", "s1")
    assert snap["findings"] == [{"x": "secret"}]


@pytest.mark.unit
def test_latest_and_trend(store):
    s, _ = store
    s.save_snapshot("u1", _snapshot("s1", "2026-01-01T00:00:00Z", 20.0, []))
    s.save_snapshot("u1", _snapshot("s2", "2026-02-01T00:00:00Z", 55.0, []))
    latest = s.get_latest_snapshot("u1", "a1")
    assert latest["scan_id"] == "s2"
    trend = s.trend("u1", "a1")
    assert [p["risk_score"] for p in trend] == [20.0, 55.0]  # oldest -> newest


@pytest.mark.unit
def test_index_idempotent_on_resave(store):
    s, _ = store
    s.save_snapshot("u1", _snapshot("s1", "2026-01-01T00:00:00Z", 20.0, []))
    s.save_snapshot("u1", _snapshot("s1", "2026-01-01T00:00:00Z", 99.0, []))  # same id
    index = s.list_snapshot_index("u1", "a1")
    assert len(index) == 1 and index[0]["risk_score"] == 99.0


@pytest.mark.unit
def test_delete_assessment_cascades_snapshots(store):
    s, backing = store
    s.save_assessment("u1", {"assessment_id": "a1", "created_at": "x"})
    s.save_snapshot("u1", _snapshot("s1", "2026-01-01T00:00:00Z", 20.0, []))
    s.delete_assessment("u1", "a1")
    assert s.get_assessment("u1", "a1") is None
    assert not any(k.startswith("u1/vra/intelligence/a1/") for k in backing)
