"""App-side SG-audit helpers: Redis dedup/in-flight locking + LLM credit gating.

Keep cross-account collection cost-bounded and idempotent (the two failure modes
flagged in planning: duplicate concurrent audits and uncapped Bedrock cost). All
Redis access goes through the shared async ``RedisService``.
"""

from __future__ import annotations

import hashlib
import json

from services.redis_service import get_redis
from utils.base_logger import get_logger

logger = get_logger(__name__)

# Key namespaces.
_INFLIGHT_PREFIX = "sg_audit:inflight:"        # audit currently being scanned
_FINGERPRINT_PREFIX = "sg_audit:fingerprint:"  # last-scanned scope fingerprint
_NONCE_PREFIX = "sg_audit:nonce:"              # seen callback nonces (replay guard)
_REC_INFLIGHT_PREFIX = "sg_audit:rec_inflight:"  # AI recommendation being generated

# A recommendation generation lock auto-expires so a crashed/slow Bedrock call
# can't wedge regeneration.
_REC_INFLIGHT_TTL_SECONDS = 60 * 10

# An in-flight lock auto-expires so a crashed scan can't wedge an audit. Set
# above the Lambda's max runtime so a still-running scan never double-fires.
_INFLIGHT_TTL_SECONDS = 60 * 30


def scope_fingerprint(scope: dict) -> str:
    """Stable fingerprint of an audit's scope for change-detection dedup.

    Scope = the target account list + regions + audit role name. Re-running with
    an unchanged scope is skipped (unless forced).
    """
    norm = {
        "account_ids": sorted(scope.get("account_ids") or []),
        "regions": sorted(scope.get("regions") or []),
        "role_name": (scope.get("role_name") or "").strip(),
        "discover": bool(scope.get("discover")),
        "domains": sorted(scope.get("domains") or []),
    }
    raw = json.dumps(norm, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def acquire_inflight(audit_id: str) -> bool:
    """Atomically claim the scan slot for an audit. False if already held.

    SET NX EX so concurrent triggers (frontend + reconciliation poller) can never
    launch two collections for the same audit.
    """
    redis = get_redis()
    return await redis.set(
        f"{_INFLIGHT_PREFIX}{audit_id}", "1", ex=_INFLIGHT_TTL_SECONDS, nx=True
    )


async def release_inflight(audit_id: str) -> None:
    redis = get_redis()
    try:
        await redis.delete(f"{_INFLIGHT_PREFIX}{audit_id}")
    except Exception:  # best-effort; TTL will reap it anyway
        logger.debug("release_inflight failed for %s", audit_id, exc_info=True)


async def inputs_unchanged(audit_id: str, fingerprint: str) -> bool:
    """True if the scope matches the last completed audit (skip re-fire)."""
    redis = get_redis()
    prev = await redis.get(f"{_FINGERPRINT_PREFIX}{audit_id}")
    return prev == fingerprint


async def record_fingerprint(audit_id: str, fingerprint: str) -> None:
    redis = get_redis()
    try:
        await redis.set(f"{_FINGERPRINT_PREFIX}{audit_id}", fingerprint)
    except Exception:
        logger.debug("record_fingerprint failed for %s", audit_id, exc_info=True)


async def acquire_rec_inflight(key: str) -> bool:
    """Claim the AI-recommendation generation slot for an (audit, scan). SET NX EX."""
    redis = get_redis()
    return await redis.set(
        f"{_REC_INFLIGHT_PREFIX}{key}", "1", ex=_REC_INFLIGHT_TTL_SECONDS, nx=True
    )


async def release_rec_inflight(key: str) -> None:
    redis = get_redis()
    try:
        await redis.delete(f"{_REC_INFLIGHT_PREFIX}{key}")
    except Exception:
        logger.debug("release_rec_inflight failed for %s", key, exc_info=True)


async def consume_nonce(nonce: str, ttl_seconds: int) -> bool:
    """Single-use nonce check for callback replay protection.

    Returns True if the nonce was unseen (and is now reserved for ``ttl``),
    False if it has already been used.
    """
    redis = get_redis()
    return await redis.set(f"{_NONCE_PREFIX}{nonce}", "1", ex=ttl_seconds, nx=True)


async def has_llm_budget(user_id: str, total_chars: int) -> bool:
    """Gate an AI recommendation call on the audit owner's AI credits.

    Mirrors the rest of the app (``Credits.has_ai_credits``) and fails closed on
    any error so a credit-system hiccup never silently runs uncapped LLM cost.
    """
    try:
        from credits_route.route import Credits
        from db.rds_db import connect_to_rds

        db = connect_to_rds()
        try:
            return await Credits(db).has_ai_credits(
                total_chars=total_chars, user_id=user_id
            )
        finally:
            try:
                db.close()
            except Exception:
                logger.debug("has_llm_budget db.close failed", exc_info=True)
    except Exception:
        logger.warning("has_llm_budget check failed; denying", exc_info=True)
        return False
