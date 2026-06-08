"""Lambda entrypoint for OSINT collection.

Stateless: validate the event, run the free/keyless collectors, sign the
normalized snapshot, and POST it to the app callback. Holds no KMS keys and
never touches the DB — the app owns all encryption/persistence.

Runtime contract (event):
    {
      "scan_id": "...",            # idempotency key (echoed in the snapshot)
      "assessment_id": "...",
      "vendor_name": "...",
      "vendor_domain": "...",
      "callback_url": "https://api.bytoid.ai/vra/osint/callback",
      "hmac_secret": "..."         # passed at invoke time, never logged
    }
"""

from __future__ import annotations

import json
import os
import urllib.request

from vra.osint.collectors import run_collection
from vra.osint.signing import sign_payload


def _post_callback(callback_url: str, secret: str, snapshot: dict) -> int:
    body = json.dumps(snapshot).encode("utf-8")
    headers = {"Content-Type": "application/json", **sign_payload(secret, body)}
    # callback_url is supplied by the app at invoke time (not user input); the
    # https scheme is enforced by the app before invoking.
    req = urllib.request.Request(  # noqa: S310 (trusted app-supplied https URL)
        callback_url, data=body, headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return resp.status


def lambda_handler(event, context):
    scan_id = event.get("scan_id", "")
    assessment_id = event.get("assessment_id", "")
    user_id = event.get("user_id", "")
    vendor_name = event.get("vendor_name", "")
    vendor_domain = event.get("vendor_domain", "")
    callback_url = event.get("callback_url", "")
    secret = event.get("hmac_secret") or os.getenv("VRA_HMAC_SECRET", "")

    if not (scan_id and assessment_id and user_id and callback_url and secret):
        return {"ok": False, "error": "missing required event fields"}

    # run_collection never raises: per-collector failures land in
    # collector_status and the snapshot is still produced.
    snapshot = run_collection(
        scan_id=scan_id,
        assessment_id=assessment_id,
        vendor_name=vendor_name,
        vendor_domain=vendor_domain,
    )
    # Echo the user_id (HMAC-signed below) so the callback can scope S3 storage.
    snapshot["user_id"] = user_id

    status = _post_callback(callback_url, secret, snapshot)
    return {"ok": 200 <= status < 300, "callback_status": status, "scan_id": scan_id}
