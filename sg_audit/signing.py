"""HMAC signing + verification for the Lambda <-> app callback.

Shared by both sides (the Lambda signs, the app verifies), so it is stdlib-only
and vendors cleanly into the Lambda bundle.

A signature covers the raw body bytes plus a timestamp, and a single-use nonce
guards replay. The app stores seen nonces in Redis with a TTL == max skew, so a
captured request can never be replayed once it (or the skew window) expires.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid

SIG_HEADER = "X-SGA-Signature"
TS_HEADER = "X-SGA-Timestamp"
NONCE_HEADER = "X-SGA-Nonce"


def _to_bytes(data) -> bytes:
    if isinstance(data, bytes):
        return data
    return str(data).encode("utf-8")


def compute_signature(secret: str, timestamp: str, nonce: str, body: bytes) -> str:
    """Return the hex HMAC-SHA256 over ``timestamp.nonce.body``."""
    mac = hmac.new(_to_bytes(secret), digestmod=hashlib.sha256)
    mac.update(_to_bytes(timestamp))
    mac.update(b".")
    mac.update(_to_bytes(nonce))
    mac.update(b".")
    mac.update(_to_bytes(body))
    return mac.hexdigest()


def sign_payload(secret: str, body: bytes) -> dict:
    """Produce the headers the Lambda attaches to a callback POST."""
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    signature = compute_signature(secret, timestamp, nonce, body)
    return {TS_HEADER: timestamp, NONCE_HEADER: nonce, SIG_HEADER: signature}


def signature_valid(
    secret: str, timestamp: str, nonce: str, body: bytes, provided_sig: str
) -> bool:
    """Constant-time check of a provided signature (skew/nonce checked by caller)."""
    if not (secret and timestamp and nonce and provided_sig):
        return False
    expected = compute_signature(secret, timestamp, nonce, body)
    return hmac.compare_digest(expected, provided_sig)


def timestamp_within_skew(timestamp: str, max_skew_seconds: int) -> bool:
    """True if ``timestamp`` (unix seconds) is within +/- skew of now."""
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    return abs(int(time.time()) - ts) <= max_skew_seconds
