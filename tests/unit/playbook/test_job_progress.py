"""Unit tests for the Redis-backed live auto-fill progress log.

Counters increment, entries accumulate (and are bounded), and every function is
best-effort — a Redis failure must never propagate into the fill loop.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock, patch

import pytest

from tests.workflow_playbook import _wf_pb_stubs as stubs

stubs.bootstrap_sut()

_rs = sys.modules.get("services.redis_service")
if _rs is not None and not hasattr(_rs, "RedisService"):
    _rs.RedisService = MagicMock(name="RedisService")

import playbook.job_progress as jp  # noqa: E402

pytestmark = pytest.mark.unit


class FakeRedis:
    def __init__(self):
        self.store = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value
        return True

    async def get(self, key):
        return self.store.get(key)


class BoomRedis:
    async def set(self, *a, **k):
        raise RuntimeError("redis down")

    async def get(self, *a, **k):
        raise RuntimeError("redis down")


def test_init_and_entries_track_counters():
    fake = FakeRedis()

    async def _run():
        await jp.init_progress("j1", total=3)
        await jp.add_entry("j1", status="filled", question="Q1", answer="Yes",
                           detail="cited", inc_processed=True, inc_answered=True)
        await jp.add_entry("j1", status="skipped", question="Q2",
                           detail="no data", inc_processed=True)
        return await jp.get_progress("j1")

    with patch.object(jp, "get_redis", return_value=fake):
        prog = asyncio.run(_run())

    assert prog["total"] == 3
    assert prog["processed"] == 2
    assert prog["answered"] == 1
    assert len(prog["entries"]) == 2
    assert prog["entries"][0]["answer"] == "Yes"
    assert prog["entries"][1]["status"] == "skipped"


def test_entries_are_bounded():
    fake = FakeRedis()

    async def _run():
        await jp.init_progress("j2", total=0)
        for i in range(jp._MAX_ENTRIES + 25):
            await jp.add_entry("j2", status="filled", question=f"Q{i}")
        return await jp.get_progress("j2")

    with patch.object(jp, "get_redis", return_value=fake):
        prog = asyncio.run(_run())

    assert len(prog["entries"]) == jp._MAX_ENTRIES


def test_add_entry_swallows_redis_errors():
    async def _run():
        # Must not raise even though Redis is broken.
        await jp.add_entry("j3", status="filled", question="Q")
        await jp.init_progress("j3", total=1)

    with patch.object(jp, "get_redis", return_value=BoomRedis()):
        asyncio.run(_run())  # no exception == pass


def test_noop_without_job_id():
    async def _run():
        await jp.init_progress("", total=5)
        await jp.add_entry("", status="filled")
        return await jp.get_progress("")

    assert asyncio.run(_run()) is None
