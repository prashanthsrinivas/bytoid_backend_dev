"""VRA configuration — all env-driven, with safe defaults.

Kept dependency-light so the Lambda collector can import the pieces it needs
(categories, severities, source budgets) without dragging in Flask/DB code.
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

# ARN (or name) of the deployed OSINT collector Lambda. When unset, the app
# runs in "collection-disabled" mode: VRA questionnaires still work, but no
# automatic OSINT is launched (safe no-op rather than an error).
VRA_LAMBDA_ARN = os.getenv("VRA_LAMBDA_ARN", "")

# Public HTTPS base the Lambda posts findings back to (e.g. https://api.bytoid.ai).
# Falls back to the app's BACKURL when unset.
VRA_CALLBACK_BASE_URL = os.getenv("VRA_CALLBACK_BASE_URL", "")

# Shared secret for the HMAC-signed callback. REQUIRED for collection to run;
# without it the app refuses to invoke (fail closed, not open).
VRA_HMAC_SECRET = os.getenv("VRA_HMAC_SECRET", "")

# Max clock skew (seconds) tolerated on a signed callback before it is rejected.
VRA_CALLBACK_MAX_SKEW = _int("VRA_CALLBACK_MAX_SKEW_SECONDS", 300)

# Max callback body size (bytes) accepted from the Lambda.
VRA_CALLBACK_MAX_BYTES = _int("VRA_CALLBACK_MAX_BYTES", 5 * 1024 * 1024)

# Optional Shodan API key. Unset = use only the free, keyless Shodan InternetDB.
SHODAN_API_KEY = os.getenv("SHODAN_API_KEY", "")

# Frontend base for the live Vendor Intelligence Dashboard; used to build the
# link embedded in the report's OSINT section ({{VRA_DASHBOARD_URL}}).
VRA_DASHBOARD_BASE_URL = os.getenv("VRA_DASHBOARD_BASE_URL", "")

# --- Cost / lifecycle defaults ----------------------------------------------
# How long intelligence snapshots are retained before the purge job drops them.
VRA_RETENTION_DAYS = _int("VRA_RETENTION_DAYS", 365)

# Default automatic re-scan cadence in days (0 disables auto re-scan).
VRA_RESCAN_CADENCE_DAYS = _int("VRA_RESCAN_CADENCE_DAYS", 30)

# Upper bound on characters sent to the LLM per OSINT scan (cost guard, paired
# with the Redis change-detection dedup).
VRA_LLM_BUDGET_CHARS = _int("VRA_LLM_BUDGET_CHARS", 60_000)

# --- safe_fetch transport defaults ------------------------------------------
VRA_FETCH_TIMEOUT = _int("VRA_FETCH_TIMEOUT_SECONDS", 15)
VRA_FETCH_MAX_BYTES = _int("VRA_FETCH_MAX_BYTES", 5 * 1024 * 1024)
VRA_FETCH_MAX_REDIRECTS = _int("VRA_FETCH_MAX_REDIRECTS", 5)


def collection_enabled() -> bool:
    """True only when the Lambda + HMAC secret are both configured."""
    return bool(VRA_LAMBDA_ARN and VRA_HMAC_SECRET)
