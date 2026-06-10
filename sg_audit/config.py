"""SG-audit configuration — all env-driven, with safe defaults.

Dependency-light (stdlib only) so the Lambda collector can import the pieces it
needs (region, callback wiring) without dragging in Flask/DB code.
"""

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# --- AWS / Lambda wiring -----------------------------------------------------
# Region follows the app's primary infra region (RDS/KMS/Secrets live in
# ca-central-1); never hard-coded so deploys track the environment.
AWS_REGION = os.getenv("AWS_REGION", "ca-central-1")

# ARN (or name) of the deployed SG-audit collector Lambda. When unset, the app
# runs in "collection-disabled" mode: audits can be registered but no scan is
# launched (safe no-op rather than an error).
SG_LAMBDA_ARN = os.getenv("SG_LAMBDA_ARN", "")

# Public HTTPS base the Lambda posts findings back to (e.g. https://api.bytoid.ai).
# Falls back to the app's BACKURL when unset.
SG_CALLBACK_BASE_URL = os.getenv("SG_CALLBACK_BASE_URL", "")

# Shared secret for the HMAC-signed callback. REQUIRED for collection to run;
# without it the app refuses to invoke (fail closed, not open).
SG_HMAC_SECRET = os.getenv("SG_HMAC_SECRET", "")

# Max clock skew (seconds) tolerated on a signed callback before it is rejected.
SG_CALLBACK_MAX_SKEW = _int("SG_CALLBACK_MAX_SKEW_SECONDS", 300)

# Max callback body size (bytes) accepted from the Lambda. SG snapshots for a
# large org can be sizeable, so this is larger than VRA's default.
SG_CALLBACK_MAX_BYTES = _int("SG_CALLBACK_MAX_BYTES", 10 * 1024 * 1024)

# --- Cross-account audit role ------------------------------------------------
# Name of the read-only audit role deployed (StackSet) in each member account.
# The Lambda assumes ``arn:aws:iam::<account>:role/<this>`` with the per-tenant
# ExternalId. Overridable per audit.
SG_DEFAULT_AUDIT_ROLE_NAME = os.getenv("SG_DEFAULT_AUDIT_ROLE_NAME", "BytoidSecurityAuditRole")

# Minimum remaining lifetime (seconds) the base SAML session must have before we
# launch an async collection — an Event invoke can sit queued, and 1-hour STS
# creds could otherwise expire mid-run. Below this we ask the user to re-auth.
SG_MIN_SESSION_TTL_SECONDS = _int("SG_MIN_SESSION_TTL_SECONDS", 900)

# --- Cost / lifecycle defaults ----------------------------------------------
# How long posture snapshots are retained before the purge job drops them.
SG_RETENTION_DAYS = _int("SG_RETENTION_DAYS", 365)

# Default automatic re-audit cadence in days (0 disables auto re-audit).
SG_RESCAN_CADENCE_DAYS = _int("SG_RESCAN_CADENCE_DAYS", 30)

# Upper bound on characters of findings JSON sent to the LLM per recommendation
# pass (cost guard, paired with the per-owner AI credit gate).
SG_LLM_BUDGET_CHARS = _int("SG_LLM_BUDGET_CHARS", 60_000)

# Frontend base for the live Security Posture Dashboard; used to build the link
# embedded in reports.
SG_DASHBOARD_BASE_URL = os.getenv("SG_DASHBOARD_BASE_URL", "")


def collection_enabled() -> bool:
    """True only when the Lambda + HMAC secret are both configured."""
    return bool(SG_LAMBDA_ARN and SG_HMAC_SECRET)
