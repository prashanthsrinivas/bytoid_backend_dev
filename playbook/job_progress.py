"""Live progress log for background auto-fill jobs (Redis-backed).

The per-question fill loop appends entries here; the ``/playbook/jbs/<job_id>``
poll endpoint merges them so the UI can render a live, expandable log. Stored
under a separate key from the JobManager status blob (``job:{job_id}``) so the
two writers never clobber each other. Best-effort — every function swallows its
own errors so progress bookkeeping can never break the actual fill.
"""

from __future__ import annotations

import time

from services.redis_service import get_redis
from utils.base_logger import get_logger

logger = get_logger(__name__)

_TTL = 3600  # match the JobManager status TTL
_MAX_ENTRIES = 500  # bound the blob; keep the most recent


def _key(job_id: str) -> str:
    return f"autofill_progress:{job_id}"


def _empty() -> dict:
    return {"total": 0, "processed": 0, "answered": 0, "entries": []}


async def init_progress(job_id: str, total: int = 0) -> None:
    if not job_id:
        return
    try:
        state = _empty()
        state["total"] = int(total or 0)
        await get_redis().set(_key(job_id), state, ex=_TTL)
    except Exception as e:
        logger.debug("init_progress failed: %s", e)


async def add_entry(
    job_id: str,
    *,
    status: str,
    question: str | None = None,
    answer: str | None = None,
    detail: str | None = None,
    qid: str | None = None,
    inc_processed: bool = False,
    inc_answered: bool = False,
) -> None:
    """Append one progress entry and bump counters. ``status`` is one of
    ``reading | filled | skipped | error | done``."""
    if not job_id:
        return
    try:
        redis = get_redis()
        state = await redis.get(_key(job_id))
        if not isinstance(state, dict):
            state = _empty()

        entry: dict = {"ts": round(time.time(), 3), "status": status}
        if qid is not None:
            entry["qid"] = qid
        if question:
            entry["question"] = str(question)[:300]
        if answer:
            entry["answer"] = str(answer)[:300]
        if detail:
            entry["detail"] = str(detail)[:400]

        entries = state.get("entries") or []
        entries.append(entry)
        if len(entries) > _MAX_ENTRIES:
            entries = entries[-_MAX_ENTRIES:]
        state["entries"] = entries
        if inc_processed:
            state["processed"] = int(state.get("processed", 0)) + 1
        if inc_answered:
            state["answered"] = int(state.get("answered", 0)) + 1

        await redis.set(_key(job_id), state, ex=_TTL)
    except Exception as e:
        logger.debug("add_entry failed: %s", e)


async def get_progress(job_id: str):
    """Return the progress blob (or None). Used by the poll endpoint."""
    if not job_id:
        return None
    try:
        return await get_redis().get(_key(job_id))
    except Exception as e:
        logger.debug("get_progress failed: %s", e)
        return None
