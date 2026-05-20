import os
import threading
import time
from dotenv import load_dotenv

load_dotenv()
dev = os.getenv("DEV", "").lower()
IS_DEV = dev == "true"
# Only production-safe origins here
PROD_ORIGINS = {
    "https://www.bytoid.ai",
    "https://bytoid.ai",
    "https://app.bytoid.ai",
    "https://api.bytoid.ai",
}

# Lovable preview frontends — always allowed (dev & prod server)
STAGING_ORIGINS = {
    "https://preview--bytoiddev.lovable.app",
    "https://preview--bytoid-45.lovable.app",
    "preview--bytoiddev.lovable.app",
    "preview--bytoid-45.lovable.app",
}

# Dev-only origins
DEV_ORIGINS = {
    "https://dev.bytoid.ai",
    "dev.bytoid.ai",
    "http://localhost:8080",
}
FRAMEWORK_OWNER = "service@bytoid.ca"
ALLOWED_ORIGINS = PROD_ORIGINS | STAGING_ORIGINS | (DEV_ORIGINS if IS_DEV else set())
if IS_DEV:
    ACCESSIBLE_IDS = ["109161866299858012556", "113605503284012967393"]
else:
    ACCESSIBLE_IDS = ["113605503284012967393"]
BACKURL = (
    "https://rtdtj5q9dh.execute-api.ca-central-1.amazonaws.com"
    if IS_DEV
    else "https://api.bytoid.ai"
)

# ── Policy Hub V2 feature flag ───────────────────────────────────────────────
# Per-org gating. Default false — enable per-org via DB or env override.
# Cache entries expire after 60s so a flag flip takes effect quickly.
POLICY_HUB_V2_GLOBAL_KILL = os.getenv("POLICY_HUB_V2_GLOBAL_KILL", "").lower() == "true"
_STATEMENT_REID_THRESHOLD_DEFAULT = float(
    os.getenv("STATEMENT_REID_THRESHOLD", "0.85")
)
_v2_cache: dict = {}  # {org_id: (enabled: bool, expires_at: float)}
_v2_cache_lock = threading.Lock()
_V2_CACHE_TTL = 60.0  # seconds


def policy_hub_v2_enabled(org_id: str) -> bool:
    """Return True if Policy Hub V2 is active for the given org.

    Resolution order:
      1. Global kill switch (env POLICY_HUB_V2_GLOBAL_KILL=true) → always False.
      2. 60-second in-process cache keyed by org_id.
      3. org_feature_flags table lookup (if table exists).
      4. Env var POLICY_HUB_V2_ENABLED=true → True for all orgs (dev convenience).
    """
    if POLICY_HUB_V2_GLOBAL_KILL:
        return False

    now = time.monotonic()
    with _v2_cache_lock:
        entry = _v2_cache.get(org_id)
        if entry and entry[1] > now:
            return entry[0]

    enabled = _lookup_v2_flag(org_id)

    with _v2_cache_lock:
        _v2_cache[org_id] = (enabled, now + _V2_CACHE_TTL)

    return enabled


def _lookup_v2_flag(org_id: str) -> bool:
    # Try DB first; fall back to env var so dev environments don't need a DB row.
    try:
        from db.rds_db import connect_to_rds
        import pymysql.cursors

        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT flag_value FROM org_feature_flags "
                "WHERE org_id=%s AND flag_name='POLICY_HUB_V2_ENABLED' LIMIT 1",
                (org_id,),
            )
            row = cur.fetchone()
        conn.close()
        if row is not None:
            return str(row["flag_value"]).lower() in ("1", "true")
    except Exception:
        pass  # Table may not exist yet; fall through to env var

    return os.getenv("POLICY_HUB_V2_ENABLED", "").lower() == "true"


def statement_reid_threshold(org_id: str | None = None) -> float:
    """Cosine-similarity threshold for statement ID recovery after an LLM edit.

    Per-org overrides can be added to org_feature_flags with
    flag_name='STATEMENT_REID_THRESHOLD'. Default is 0.85 (env-overridable).
    """
    if org_id:
        try:
            from db.rds_db import connect_to_rds
            import pymysql.cursors

            conn = connect_to_rds()
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(
                    "SELECT flag_value FROM org_feature_flags "
                    "WHERE org_id=%s AND flag_name='STATEMENT_REID_THRESHOLD' LIMIT 1",
                    (org_id,),
                )
                row = cur.fetchone()
            conn.close()
            if row is not None:
                return float(row["flag_value"])
        except Exception:
            pass
    return _STATEMENT_REID_THRESHOLD_DEFAULT


# ── Migration config ───────────────────────────────────────────────────────────

MIGRATION_CHUNK_SIZE = int(os.getenv("MIGRATION_CHUNK_SIZE", "25"))
MIGRATION_FIREWORKS_CONCURRENCY = int(os.getenv("MIGRATION_FIREWORKS_CONCURRENCY", "3"))
DLQ_DAILY_RETRY_CAP = int(os.getenv("DLQ_DAILY_RETRY_CAP", "500"))
DLQ_MAX_RETRIES = int(os.getenv("DLQ_MAX_RETRIES", "8"))
