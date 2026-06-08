"""trigger_collection (invoke) + process_callback (HMAC) tests.

Async functions are driven via asyncio.run; Redis helpers, boto3, and config are
all mocked/monkeypatched. HMAC signatures are real (vra.osint.signing).
"""

import asyncio
import json

import pytest

from vra import collect as collect_mod
from vra import config as vra_config
from vra.osint import signing
from vra.service import VraService

SECRET = "unit-secret"  # noqa: S105 (test fixture)


def _run(coro):
    return asyncio.run(coro)


def _async_return(value):
    async def _inner(*a, **k):
        return value
    return _inner


class FakeStorage:
    def __init__(self):
        self.assessments = {}
        self.snapshots = []

    def get_assessment(self, u, a):
        return self.assessments.get((u, a))

    def save_assessment(self, u, r):
        self.assessments[(u, r["assessment_id"])] = dict(r)
        return r

    def save_snapshot(self, u, s):
        self.snapshots.append((u, s))
        return s


class FakeLambda:
    def __init__(self):
        self.calls = []

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        return {"StatusCode": 202}


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setattr(vra_config, "VRA_LAMBDA_ARN", "arn:lambda:vra")
    monkeypatch.setattr(vra_config, "VRA_HMAC_SECRET", SECRET)
    monkeypatch.setattr(vra_config, "VRA_CALLBACK_BASE_URL", "https://api.test")
    monkeypatch.setattr(vra_config, "VRA_CALLBACK_MAX_BYTES", 1_000_000)
    monkeypatch.setattr(vra_config, "VRA_CALLBACK_MAX_SKEW", 300)


@pytest.fixture
def svc():
    s = VraService(storage=FakeStorage())
    rec = {
        "assessment_id": "a1",
        "vendor_name": "Acme",
        "vendor_domain": "acme.com",
        "scan_state": "pending",
    }
    s.storage.save_assessment("u1", rec)
    return s


def _patch_redis(monkeypatch, *, acquire=True, unchanged=False, nonce_ok=True):
    monkeypatch.setattr(collect_mod, "acquire_inflight", _async_return(acquire))
    monkeypatch.setattr(collect_mod, "release_inflight", _async_return(None))
    monkeypatch.setattr(collect_mod, "inputs_unchanged", _async_return(unchanged))
    monkeypatch.setattr(collect_mod, "record_fingerprint", _async_return(None))
    monkeypatch.setattr(collect_mod, "consume_nonce", _async_return(nonce_ok))


# ── trigger_collection ───────────────────────────────────────────────────────

@pytest.mark.unit
def test_trigger_not_found(cfg, monkeypatch):
    _patch_redis(monkeypatch)
    out = _run(collect_mod.trigger_collection("u1", "missing", service=VraService(storage=FakeStorage())))
    assert out["status"] == "error"


@pytest.mark.unit
def test_trigger_not_ready(cfg, monkeypatch):
    _patch_redis(monkeypatch)
    s = VraService(storage=FakeStorage())
    s.storage.save_assessment("u1", {"assessment_id": "a1", "vendor_name": "Acme", "vendor_domain": ""})
    out = _run(collect_mod.trigger_collection("u1", "a1", service=s))
    assert out["status"] == "skipped"


@pytest.mark.unit
def test_trigger_disabled(monkeypatch, svc):
    _patch_redis(monkeypatch)
    monkeypatch.setattr(vra_config, "VRA_LAMBDA_ARN", "")  # collection_enabled() -> False
    monkeypatch.setattr(vra_config, "VRA_HMAC_SECRET", "")
    out = _run(collect_mod.trigger_collection("u1", "a1", service=svc))
    assert out["status"] == "disabled"


@pytest.mark.unit
def test_trigger_unchanged(cfg, monkeypatch, svc):
    _patch_redis(monkeypatch, unchanged=True)
    out = _run(collect_mod.trigger_collection("u1", "a1", service=svc))
    assert out["status"] == "unchanged"


@pytest.mark.unit
def test_trigger_already_running(cfg, monkeypatch, svc):
    _patch_redis(monkeypatch, acquire=False)
    out = _run(collect_mod.trigger_collection("u1", "a1", service=svc))
    assert out["status"] == "already_running"


@pytest.mark.unit
def test_trigger_launches(cfg, monkeypatch, svc):
    _patch_redis(monkeypatch)
    fake = FakeLambda()
    out = _run(collect_mod.trigger_collection("u1", "a1", service=svc, lambda_client=fake))
    assert out["status"] == "launched" and out["scan_id"]
    assert len(fake.calls) == 1
    assert fake.calls[0]["InvocationType"] == "Event"
    payload = json.loads(fake.calls[0]["Payload"])
    assert payload["user_id"] == "u1" and payload["hmac_secret"] == SECRET
    assert svc.storage.get_assessment("u1", "a1")["scan_state"] == "in_flight"


@pytest.mark.unit
def test_trigger_invoke_failure_rolls_back(cfg, monkeypatch, svc):
    _patch_redis(monkeypatch)

    class BoomLambda:
        def invoke(self, **k):
            raise RuntimeError("aws down")

    out = _run(collect_mod.trigger_collection("u1", "a1", service=svc, lambda_client=BoomLambda()))
    assert out["status"] == "error"
    assert svc.storage.get_assessment("u1", "a1")["scan_state"] == "pending"


@pytest.mark.unit
def test_trigger_force_bypasses_unchanged(cfg, monkeypatch, svc):
    _patch_redis(monkeypatch, unchanged=True)
    fake = FakeLambda()
    out = _run(collect_mod.trigger_collection("u1", "a1", service=svc, lambda_client=fake, force=True))
    assert out["status"] == "launched"


# ── process_callback ─────────────────────────────────────────────────────────

def _signed(body: dict):
    raw = json.dumps(body).encode("utf-8")
    return raw, signing.sign_payload(SECRET, raw)


def _snapshot():
    return {
        "scan_id": "s1",
        "assessment_id": "a1",
        "user_id": "u1",
        "scanned_at": "2026-06-08T00:00:00Z",
        "risk_score": 42.0,
        "findings": [
            {"category": "domain", "evidence_type": "spf", "source": "DNS",
             "finding_summary": "x", "severity": "low", "collected_at": "t"},
            {"bad": "finding"},  # should be dropped
        ],
    }


@pytest.mark.unit
def test_callback_success(cfg, monkeypatch, svc):
    _patch_redis(monkeypatch)
    raw, headers = _signed(_snapshot())
    code, body = _run(collect_mod.process_callback(raw, headers, service=svc))
    assert code == 200 and body["findings"] == 1  # malformed finding dropped
    assert svc.storage.snapshots and svc.storage.snapshots[0][1]["scan_id"] == "s1"
    rec = svc.storage.get_assessment("u1", "a1")
    assert rec["scan_state"] == "complete" and rec["latest_scan_id"] == "s1"


@pytest.mark.unit
def test_callback_bad_signature(cfg, monkeypatch, svc):
    _patch_redis(monkeypatch)
    raw, headers = _signed(_snapshot())
    headers[signing.SIG_HEADER] = "deadbeef"
    code, _ = _run(collect_mod.process_callback(raw, headers, service=svc))
    assert code == 401


@pytest.mark.unit
def test_callback_replay_rejected(cfg, monkeypatch, svc):
    _patch_redis(monkeypatch, nonce_ok=False)  # nonce already seen
    raw, headers = _signed(_snapshot())
    code, _ = _run(collect_mod.process_callback(raw, headers, service=svc))
    assert code == 409


@pytest.mark.unit
def test_callback_too_large(cfg, monkeypatch, svc):
    _patch_redis(monkeypatch)
    monkeypatch.setattr(vra_config, "VRA_CALLBACK_MAX_BYTES", 10)
    raw, headers = _signed(_snapshot())
    code, _ = _run(collect_mod.process_callback(raw, headers, service=svc))
    assert code == 413


@pytest.mark.unit
def test_callback_unknown_assessment(cfg, monkeypatch):
    _patch_redis(monkeypatch)
    empty = VraService(storage=FakeStorage())
    raw, headers = _signed(_snapshot())
    code, _ = _run(collect_mod.process_callback(raw, headers, service=empty))
    assert code == 404


@pytest.mark.unit
def test_callback_stale_timestamp(cfg, monkeypatch, svc):
    _patch_redis(monkeypatch)
    raw = json.dumps(_snapshot()).encode("utf-8")
    headers = signing.sign_payload(SECRET, raw)
    headers[signing.TS_HEADER] = "100"  # ancient
    code, _ = _run(collect_mod.process_callback(raw, headers, service=svc))
    assert code == 401
