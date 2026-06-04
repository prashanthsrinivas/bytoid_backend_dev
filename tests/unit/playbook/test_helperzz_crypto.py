"""§4e crypto — ``playbook/helperzz.py`` ``_enc_pb`` / ``_dec_pb``.

Roundtrip, plaintext passthrough (non-envelope values), cross-user rejection
(no plaintext leak to the wrong user — A02 / CWE-200), and the graceful
json-decode fallback. The KMS backend is replaced with a reversible fake so the
test asserts the wrapper's behavior, not real KMS.
"""

from __future__ import annotations

import base64

import pytest

from tests.workflow_playbook import _wf_pb_stubs as stubs

stubs.bootstrap_sut()

import playbook.helperzz as h  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.security]


class FakeKMS:
    """Reversible, user-scoped fake: ciphertext = b64("<user>|<plaintext>")."""

    def encrypt(self, user_id, s):
        token = base64.b64encode(f"{user_id}|{s}".encode()).decode()
        return {"ciphertext": token, "iv": "iv0", "encrypted_key": "ek0"}

    def decrypt(self, user_id, encrypted_key, iv, ciphertext):
        raw = base64.b64decode(ciphertext.encode()).decode()
        owner, _, s = raw.partition("|")
        if owner != user_id:
            raise ValueError("KMS: key does not belong to this user")
        return s


@pytest.fixture
def fake_kms(monkeypatch):
    kms = FakeKMS()
    monkeypatch.setattr(h, "_pb_kms", kms)
    return kms


# ── _dec_pb passthrough (non-envelope inputs returned unchanged) ──────────────

@pytest.mark.parametrize("value", [
    {"foo": 1},            # dict without encrypted_key
    "plaintext",
    [1, 2, 3],
    123,
    None,
])
def test_dec_pb_passthrough_non_envelope(value):
    assert h._dec_pb("user-1", value) == value


# ── roundtrip ─────────────────────────────────────────────────────────────────

def test_enc_dec_roundtrip_dict(fake_kms):
    env = h._enc_pb("user-1", {"a": 1, "b": "x"})
    assert set(env) == {"ciphertext", "iv", "encrypted_key"}
    assert h._dec_pb("user-1", env) == {"a": 1, "b": "x"}


def test_enc_dec_roundtrip_string_falls_back_to_raw(fake_kms):
    # A plain string isn't valid JSON → _dec_pb returns the raw decrypted string.
    env = h._enc_pb("user-1", "hello world")
    assert h._dec_pb("user-1", env) == "hello world"


def test_ciphertext_does_not_leak_plaintext(fake_kms):
    env = h._enc_pb("user-1", {"secret": "topsecret-value"})
    assert "topsecret-value" not in env["ciphertext"]


# ── cross-user rejection (no plaintext leak to wrong user) ────────────────────

def test_dec_pb_rejects_wrong_user(fake_kms):
    env = h._enc_pb("owner", {"a": 1})
    with pytest.raises(ValueError):
        h._dec_pb("attacker", env)
