"""Lambda entrypoint for cross-account SG-audit collection.

Stateless: validate the event, run the cross-account collection, sign the
snapshot, and POST it to the app callback. Holds no KMS keys and never touches
the DB — the app owns all encryption/persistence.

SECURITY: the event carries short-lived base STS credentials + the HMAC secret +
the per-tenant ExternalId. These are NEVER logged.

Runtime contract (event):
    {
      "scan_id": "...",                 # idempotency key (echoed in the snapshot)
      "audit_id": "...",
      "user_id": "...",
      "callback_url": "https://api.bytoid.ai/sg-audit/callback",
      "hmac_secret": "...",             # passed at invoke time, never logged
      "external_id": "...",             # per-tenant confused-deputy nonce
      "scope": {"account_ids": [...], "regions": [...], "role_name": "...", "discover": true},
      "base_credentials": {"access_key_id": "...", "secret_access_key": "...",
                            "session_token": "...", "region": "ca-central-1"},
      "management_account_id": "..."
    }
"""

from __future__ import annotations

import json
import urllib.request

from sg_audit.collector_lambda.runner import run_collection
from sg_audit.signing import sign_payload


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
    audit_id = event.get("audit_id", "")
    user_id = event.get("user_id", "")
    callback_url = event.get("callback_url", "")
    secret = event.get("hmac_secret", "")
    external_id = event.get("external_id", "")
    scope = event.get("scope", {}) or {}
    base_credentials = event.get("base_credentials", {}) or {}
    management_account_id = event.get("management_account_id", "")

    if not (scan_id and audit_id and user_id and callback_url and secret):
        return {"ok": False, "error": "missing required event fields"}
    if not base_credentials.get("access_key_id"):
        return {"ok": False, "error": "missing base credentials"}

    # run_collection never raises: per-account/region failures land in
    # collector_status and the snapshot is still produced.
    snapshot = run_collection(
        scan_id=scan_id,
        audit_id=audit_id,
        scope=scope,
        external_id=external_id,
        base_credentials=base_credentials,
        management_account_id=management_account_id,
    )
    # Echo the user_id (HMAC-signed below) so the callback can scope S3 storage.
    snapshot["user_id"] = user_id

    status = _post_callback(callback_url, secret, snapshot)
    return {"ok": 200 <= status < 300, "callback_status": status, "scan_id": scan_id}
