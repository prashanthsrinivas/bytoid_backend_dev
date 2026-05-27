"""Unit tests for tests_routes/webhook_auth.py.

No external dependencies — HMAC is stdlib only.
"""

import hashlib
import hmac
import json
import os
import pytest
from unittest.mock import patch

import flask

import tests_routes.webhook_auth as wh

SECRET = "test-secret-value-abc123"


def _make_request(body: bytes, sig: str | None = None, secret: str = SECRET) -> flask.Request:
    app = flask.Flask(__name__)
    with app.test_request_context(
        "/tests/webhook/ci",
        method="POST",
        data=body,
        content_type="application/json",
        headers={wh.SIGNATURE_HEADER: sig} if sig is not None else {},
    ):
        req = flask.request._get_current_object()
        return req


def _sign(body: bytes, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# ── _expected_signature ───────────────────────────────────────────────────────

@pytest.mark.unit
def test_expected_signature_format():
    sig = wh._expected_signature(SECRET, b"hello")
    assert sig.startswith("sha256=")
    assert len(sig) == 7 + 64

@pytest.mark.unit
def test_expected_signature_deterministic():
    body = b'{"category":"backend_unit"}'
    assert wh._expected_signature(SECRET, body) == wh._expected_signature(SECRET, body)

@pytest.mark.unit
def test_expected_signature_changes_with_body():
    assert wh._expected_signature(SECRET, b"a") != wh._expected_signature(SECRET, b"b")

@pytest.mark.unit
def test_expected_signature_changes_with_secret():
    body = b"same body"
    assert wh._expected_signature("secret1", body) != wh._expected_signature("secret2", body)


# ── verify_hmac ───────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_verify_hmac_valid_signature():
    body = b'{"category":"backend_unit","status":"passed"}'
    sig = _sign(body)
    app = flask.Flask(__name__)
    with app.test_request_context(
        "/tests/webhook/ci", method="POST", data=body,
        content_type="application/json",
        headers={wh.SIGNATURE_HEADER: sig},
    ):
        with patch.dict(os.environ, {wh.SECRET_ENV_VAR: SECRET}):
            assert wh.verify_hmac(flask.request) is True

@pytest.mark.unit
def test_verify_hmac_wrong_signature():
    body = b'{"category":"backend_unit"}'
    app = flask.Flask(__name__)
    with app.test_request_context(
        "/tests/webhook/ci", method="POST", data=body,
        content_type="application/json",
        headers={wh.SIGNATURE_HEADER: "sha256=deadbeef" + "0" * 56},
    ):
        with patch.dict(os.environ, {wh.SECRET_ENV_VAR: SECRET}):
            assert wh.verify_hmac(flask.request) is False

@pytest.mark.unit
def test_verify_hmac_missing_header():
    body = b'{"category":"backend_unit"}'
    app = flask.Flask(__name__)
    with app.test_request_context(
        "/tests/webhook/ci", method="POST", data=body,
        content_type="application/json",
    ):
        with patch.dict(os.environ, {wh.SECRET_ENV_VAR: SECRET}):
            assert wh.verify_hmac(flask.request) is False

@pytest.mark.unit
def test_verify_hmac_no_secret_env():
    body = b'{"x":1}'
    sig = _sign(body)
    app = flask.Flask(__name__)
    env = {k: v for k, v in os.environ.items() if k != wh.SECRET_ENV_VAR}
    with app.test_request_context(
        "/tests/webhook/ci", method="POST", data=body,
        content_type="application/json",
        headers={wh.SIGNATURE_HEADER: sig},
    ):
        with patch.dict(os.environ, env, clear=True):
            assert wh.verify_hmac(flask.request) is False

@pytest.mark.unit
def test_verify_hmac_empty_body():
    body = b""
    sig = _sign(body)
    app = flask.Flask(__name__)
    with app.test_request_context(
        "/tests/webhook/ci", method="POST", data=body,
        content_type="application/json",
        headers={wh.SIGNATURE_HEADER: sig},
    ):
        with patch.dict(os.environ, {wh.SECRET_ENV_VAR: SECRET}):
            assert wh.verify_hmac(flask.request) is True

@pytest.mark.unit
def test_verify_hmac_wrong_secret():
    body = b'{"cat":"x"}'
    sig = _sign(body, secret="correct-secret")
    app = flask.Flask(__name__)
    with app.test_request_context(
        "/tests/webhook/ci", method="POST", data=body,
        content_type="application/json",
        headers={wh.SIGNATURE_HEADER: sig},
    ):
        with patch.dict(os.environ, {wh.SECRET_ENV_VAR: "wrong-secret"}):
            assert wh.verify_hmac(flask.request) is False

@pytest.mark.unit
def test_verify_hmac_tampered_body():
    body = b'{"category":"backend_unit"}'
    sig = _sign(body)
    tampered = b'{"category":"backend_security_sast"}'
    app = flask.Flask(__name__)
    with app.test_request_context(
        "/tests/webhook/ci", method="POST", data=tampered,
        content_type="application/json",
        headers={wh.SIGNATURE_HEADER: sig},
    ):
        with patch.dict(os.environ, {wh.SECRET_ENV_VAR: SECRET}):
            assert wh.verify_hmac(flask.request) is False

@pytest.mark.unit
def test_verify_hmac_real_payload_roundtrip():
    payload = json.dumps({
        "category": "backend_security_sast",
        "run_id": "gh-123456-bandit",
        "status": "passed",
        "summary": {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 0},
    }).encode()
    sig = _sign(payload)
    app = flask.Flask(__name__)
    with app.test_request_context(
        "/tests/webhook/ci", method="POST", data=payload,
        content_type="application/json",
        headers={wh.SIGNATURE_HEADER: sig},
    ):
        with patch.dict(os.environ, {wh.SECRET_ENV_VAR: SECRET}):
            assert wh.verify_hmac(flask.request) is True


# ── sign ──────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_sign_produces_valid_signature():
    body = b"test payload"
    with patch.dict(os.environ, {wh.SECRET_ENV_VAR: SECRET}):
        sig = wh.sign(body)
    expected = _sign(body)
    assert sig == expected

@pytest.mark.unit
def test_sign_empty_secret_produces_consistent_output():
    body = b"test"
    env = {k: v for k, v in os.environ.items() if k != wh.SECRET_ENV_VAR}
    with patch.dict(os.environ, env, clear=True):
        sig = wh.sign(body)
    assert sig.startswith("sha256=")
