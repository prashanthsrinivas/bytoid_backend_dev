"""HMAC callback signing tests — vra/osint/signing.py."""

import time
from unittest.mock import patch

import pytest

from vra.osint import signing

SECRET = "test-shared-secret"  # noqa: S105 (test fixture, not a real secret)


@pytest.mark.security
def test_sign_then_verify_roundtrip():
    body = b'{"scan_id":"s1"}'
    headers = signing.sign_payload(SECRET, body)
    assert signing.signature_valid(
        SECRET,
        headers[signing.TS_HEADER],
        headers[signing.NONCE_HEADER],
        body,
        headers[signing.SIG_HEADER],
    )


@pytest.mark.security
def test_tampered_body_fails():
    body = b'{"scan_id":"s1"}'
    headers = signing.sign_payload(SECRET, body)
    assert not signing.signature_valid(
        SECRET,
        headers[signing.TS_HEADER],
        headers[signing.NONCE_HEADER],
        b'{"scan_id":"TAMPERED"}',
        headers[signing.SIG_HEADER],
    )


@pytest.mark.security
def test_wrong_secret_fails():
    body = b"abc"
    headers = signing.sign_payload(SECRET, body)
    assert not signing.signature_valid(
        "other-secret",
        headers[signing.TS_HEADER],
        headers[signing.NONCE_HEADER],
        body,
        headers[signing.SIG_HEADER],
    )


@pytest.mark.security
@pytest.mark.parametrize("missing", ["secret", "ts", "nonce", "sig"])
def test_missing_pieces_fail(missing):
    body = b"abc"
    h = signing.sign_payload(SECRET, body)
    args = {
        "secret": SECRET,
        "ts": h[signing.TS_HEADER],
        "nonce": h[signing.NONCE_HEADER],
        "sig": h[signing.SIG_HEADER],
    }
    args[missing] = ""
    assert not signing.signature_valid(
        args["secret"], args["ts"], args["nonce"], body, args["sig"]
    )


@pytest.mark.security
def test_nonces_are_unique():
    a = signing.sign_payload(SECRET, b"x")[signing.NONCE_HEADER]
    b = signing.sign_payload(SECRET, b"x")[signing.NONCE_HEADER]
    assert a != b


@pytest.mark.security
def test_timestamp_within_skew():
    now = int(time.time())
    assert signing.timestamp_within_skew(str(now), 300)
    assert signing.timestamp_within_skew(str(now - 100), 300)
    assert not signing.timestamp_within_skew(str(now - 1000), 300)
    assert not signing.timestamp_within_skew("not-a-number", 300)


@pytest.mark.security
def test_signature_stable_for_fixed_inputs():
    # Same secret/ts/nonce/body -> same signature (deterministic).
    with patch.object(signing.time, "time", return_value=1_700_000_000):
        with patch.object(signing.uuid, "uuid4") as mock_uuid:
            mock_uuid.return_value.hex = "fixednonce"
            h1 = signing.sign_payload(SECRET, b"body")
            h2 = signing.sign_payload(SECRET, b"body")
    assert h1 == h2
