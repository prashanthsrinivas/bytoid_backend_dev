"""HMAC verification for the /tests/webhook/frontend ingest endpoint.

GitHub Actions in the bytoiddev repo signs each payload with a shared secret
(FRONTEND_TESTS_WEBHOOK_SECRET) and sends:
    X-Bytoid-Signature: sha256=<hex>
"""

import hashlib
import hmac
import os

from flask import Request

SIGNATURE_HEADER = "X-Bytoid-Signature"
SECRET_ENV_VAR = "FRONTEND_TESTS_WEBHOOK_SECRET"


def _expected_signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_hmac(req: Request) -> bool:
    """Returns True if the signature header is present, well-formed, and valid."""
    secret = os.getenv(SECRET_ENV_VAR)
    if not secret:
        return False
    provided = req.headers.get(SIGNATURE_HEADER)
    if not provided:
        return False
    body = req.get_data() or b""
    expected = _expected_signature(secret, body)
    return hmac.compare_digest(provided, expected)


def sign(body: bytes) -> str:
    """Helper for tests/scripts that need to produce a valid signature locally."""
    secret = os.getenv(SECRET_ENV_VAR, "")
    return _expected_signature(secret, body)
