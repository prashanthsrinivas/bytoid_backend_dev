"""Redis dedup/in-flight locking + LLM credit gating (namespaced per provider)."""

from __future__ import annotations

import hashlib
import json

from services.redis_service import get_redis
from utils.base_logger import get_logger

logger = get_logger(__name__)

_INFLIGHT_TTL = 60 * 30
_REC_INFLIGHT_TTL = 60 * 10


def scope_fingerprint(scope: dict) -> str:
    norm = {
        "scope_ids": sorted(scope.get("scope_ids") or []),
        "domains": sorted(scope.get("domains") or []),
        "organization_id": (scope.get("organization_id") or "").strip(),
        "regions": sorted(scope.get("regions") or []),
    }
    return hashlib.sha256(json.dumps(norm, sort_keys=True).encode()).hexdigest()


async def acquire_inflight(ns: str, audit_id: str) -> bool:
    return await get_redis().set(f"{ns}:inflight:{audit_id}", "1", ex=_INFLIGHT_TTL, nx=True)


async def release_inflight(ns: str, audit_id: str) -> None:
    try:
        await get_redis().delete(f"{ns}:inflight:{audit_id}")
    except Exception:
        logger.debug("release_inflight failed", exc_info=True)


async def inputs_unchanged(ns: str, audit_id: str, fingerprint: str) -> bool:
    return (await get_redis().get(f"{ns}:fingerprint:{audit_id}")) == fingerprint


async def record_fingerprint(ns: str, audit_id: str, fingerprint: str) -> None:
    try:
        await get_redis().set(f"{ns}:fingerprint:{audit_id}", fingerprint)
    except Exception:
        logger.debug("record_fingerprint failed", exc_info=True)


async def acquire_rec_inflight(ns: str, key: str) -> bool:
    return await get_redis().set(f"{ns}:rec_inflight:{key}", "1", ex=_REC_INFLIGHT_TTL, nx=True)


async def release_rec_inflight(ns: str, key: str) -> None:
    try:
        await get_redis().delete(f"{ns}:rec_inflight:{key}")
    except Exception:
        logger.debug("release_rec_inflight failed", exc_info=True)


async def has_llm_budget(user_id: str, total_chars: int) -> bool:
    try:
        from credits_route.route import Credits
        from db.rds_db import connect_to_rds

        db = connect_to_rds()
        try:
            return await Credits(db).has_ai_credits(total_chars=total_chars, user_id=user_id)
        finally:
            try:
                db.close()
            except Exception:
                logger.debug("has_llm_budget db.close failed", exc_info=True)
    except Exception:
        logger.warning("has_llm_budget check failed; denying", exc_info=True)
        return False
