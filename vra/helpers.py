"""App-side VRA helpers: Redis dedup/in-flight locking + LLM credit gating.

These keep OSINT collection cost-bounded and idempotent (the two failure modes
flagged during planning: runaway Bedrock cost and duplicate concurrent scans).
All Redis access goes through the shared async ``RedisService``.
"""

from __future__ import annotations

import hashlib

from services.redis_service import get_redis
from utils.base_logger import get_logger

logger = get_logger(__name__)

# Key namespaces.
_INFLIGHT_PREFIX = "vra:inflight:"          # assessment currently being scanned
_FINGERPRINT_PREFIX = "vra:fingerprint:"    # last-scanned input fingerprint
_NONCE_PREFIX = "vra:nonce:"                # seen callback nonces (replay guard)

# An in-flight lock auto-expires so a crashed scan can't wedge an assessment.
_INFLIGHT_TTL_SECONDS = 60 * 30


def domain_fingerprint(vendor_name: str, vendor_domain: str) -> str:
    """Stable fingerprint of the scan inputs for change-detection dedup."""
    raw = f"{(vendor_name or '').strip().lower()}|{(vendor_domain or '').strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def acquire_inflight(assessment_id: str) -> bool:
    """Atomically claim the scan slot for an assessment. False if already held.

    Uses SET NX EX so concurrent triggers (frontend + reconciliation poller)
    can never launch two collections for the same assessment.
    """
    redis = get_redis()
    return await redis.set(
        f"{_INFLIGHT_PREFIX}{assessment_id}", "1", ex=_INFLIGHT_TTL_SECONDS, nx=True
    )


async def release_inflight(assessment_id: str) -> None:
    redis = get_redis()
    try:
        await redis.delete(f"{_INFLIGHT_PREFIX}{assessment_id}")
    except Exception:  # best-effort; TTL will reap it anyway
        logger.debug("release_inflight failed for %s", assessment_id, exc_info=True)


async def inputs_unchanged(assessment_id: str, fingerprint: str) -> bool:
    """True if the inputs match the last completed scan (skip re-fire)."""
    redis = get_redis()
    prev = await redis.get(f"{_FINGERPRINT_PREFIX}{assessment_id}")
    return prev == fingerprint


async def record_fingerprint(assessment_id: str, fingerprint: str) -> None:
    redis = get_redis()
    try:
        await redis.set(f"{_FINGERPRINT_PREFIX}{assessment_id}", fingerprint)
    except Exception:
        logger.debug("record_fingerprint failed for %s", assessment_id, exc_info=True)


async def consume_nonce(nonce: str, ttl_seconds: int) -> bool:
    """Single-use nonce check for callback replay protection.

    Returns True if the nonce was unseen (and is now reserved for ``ttl``),
    False if it has already been used.
    """
    redis = get_redis()
    return await redis.set(f"{_NONCE_PREFIX}{nonce}", "1", ex=ttl_seconds, nx=True)


async def has_llm_budget(user_id: str, total_chars: int) -> bool:
    """Gate an OSINT LLM call on the assessment owner's AI credits.

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
