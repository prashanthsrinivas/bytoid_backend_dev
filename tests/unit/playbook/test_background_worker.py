"""§4f + §4y-3 — ``playbook/background_worker.py`` ``JobManager``.

Covers ``submit_job`` (enqueue → job id, queued status, no real execution) and
``_run_job`` (success → completed; any exception → failed status, never
propagates). The resilience cases simulate upstream 429/503/timeout to prove the
background worker records a failed status rather than crashing, and that a
failure in one job does not poison a concurrently-running sibling.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from unittest.mock import AsyncMock, patch

import pytest

from tests.workflow_playbook import _wf_pb_stubs as stubs

stubs.bootstrap_sut()

import playbook.background_worker as bw  # noqa: E402


class FakeRedis:
    """Records the final value written per key so we can assert job state."""

    def __init__(self):
        self.store: dict = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value


class FakeExecutor:
    """Captures submitted callables without running them (deterministic test)."""

    def __init__(self):
        self.submitted = []

    def submit(self, fn, *a, **kw):
        self.submitted.append(fn)
        fut: concurrent.futures.Future = concurrent.futures.Future()
        fut.set_result(None)
        return fut


# ── submit_job ────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_submit_job_returns_id_and_marks_queued():
    redis = FakeRedis()

    async def _noop(*a, **kw):
        return None

    with patch.object(bw, "redis_service", redis), \
         patch.object(bw, "executor", FakeExecutor()), \
         patch.object(bw.JobManager, "_run_job", AsyncMock()):
        job_id = asyncio.run(bw.JobManager.submit_job(_noop, "arg"))

    assert isinstance(job_id, str) and len(job_id) >= 8
    assert redis.store[f"job:{job_id}"] == {"status": "queued"}


# ── _run_job ──────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_run_job_success_injects_job_id_and_completes():
    redis = FakeRedis()

    async def _echo(*a, **kw):
        return {"saw_job_id": kw.get("job_id"), "args": a}

    with patch.object(bw, "redis_service", redis):
        asyncio.run(bw.JobManager._run_job("jid1", _echo, "x"))

    final = redis.store["job:jid1"]
    assert final["status"] == "completed"
    assert final["data"]["saw_job_id"] == "jid1"   # job_id injected as kwarg
    assert final["data"]["args"] == ("x",)


@pytest.mark.unit
def test_run_job_captures_exception_without_propagating():
    redis = FakeRedis()

    async def _boom(*a, **kw):
        raise RuntimeError("kaboom")

    # Must NOT raise out of _run_job.
    with patch.object(bw, "redis_service", redis):
        asyncio.run(bw.JobManager._run_job("jid2", _boom))

    final = redis.store["job:jid2"]
    assert final["status"] == "failed"
    assert "kaboom" in final["error"]


@pytest.mark.resilience
@pytest.mark.parametrize("err_text", [
    "ThrottlingException: Rate exceeded (HTTP 429)",
    "ServiceUnavailable (HTTP 503)",
    "Read timed out after 30s",
])
def test_run_job_handles_upstream_degradation(err_text):
    """429 / 503 / timeout from an upstream service → job marked failed, worker
    stays alive (§4y-3)."""
    redis = FakeRedis()

    async def _degraded(*a, **kw):
        raise Exception(err_text)

    with patch.object(bw, "redis_service", redis):
        asyncio.run(bw.JobManager._run_job("jid3", _degraded))

    final = redis.store["job:jid3"]
    assert final["status"] == "failed"
    assert err_text.split()[0] in final["error"]


@pytest.mark.concurrency
def test_run_job_failure_does_not_poison_sibling():
    """One job failing must not affect a concurrently-running sibling (§4y-2)."""
    redis = FakeRedis()

    async def _ok(*a, **kw):
        await asyncio.sleep(0)
        return {"ok": True}

    async def _bad(*a, **kw):
        await asyncio.sleep(0)
        raise ValueError("sibling failure")

    async def _main():
        await asyncio.gather(
            bw.JobManager._run_job("good", _ok),
            bw.JobManager._run_job("bad", _bad),
        )

    with patch.object(bw, "redis_service", redis):
        asyncio.run(_main())

    assert redis.store["job:good"]["status"] == "completed"
    assert redis.store["job:bad"]["status"] == "failed"
