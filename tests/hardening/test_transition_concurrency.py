"""§4y-2 — concurrent transition / optimistic-lock race.

Proves the claim behind ``WorkflowConflictError``: when N callers transition the
*same* workflow at the *same* ``state_version`` simultaneously, exactly one wins
and the rest are rejected — no dual-approved / double-committed state.

We run the real ``transition`` orchestration. The DB is emulated by a fake
connection whose ``SELECT … FOR UPDATE`` acquires a shared lock (the row lock)
and snapshots the current version; the winning hop bumps the shared version, so
the next caller to acquire the lock sees a mismatch and raises. Leaf DB
collaborators are patched so the test isolates the lock/version gate.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from tests.workflow_playbook import _wf_pb_stubs as stubs

stubs.bootstrap_sut()

import workflow_route.state_machine as sm  # noqa: E402

pytestmark = [pytest.mark.concurrency, pytest.mark.state]


class _LockingCursor:
    def __init__(self, store, lock, holder):
        self._store, self._lock, self._holder = store, lock, holder
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if "FOR UPDATE" in sql:
            self._lock.acquire()            # emulate the row lock
            self._holder["held"] = True
            self._row = dict(self._store["row"])   # snapshot under the lock

    def fetchone(self):
        return self._row

    def close(self):
        pass


class _LockingConn:
    """Shares ``store`` + ``lock`` across threads; holds the lock from the
    FOR UPDATE select until ``close()`` (i.e. for the whole transaction)."""

    def __init__(self, store, lock):
        self._store, self._lock = store, lock
        self._holder = {"held": False}
        self._cur = _LockingCursor(store, lock, self._holder)

    def cursor(self, *a, **k):
        return self._cur

    def commit(self):
        self._store["commits"] += 1

    def close(self):
        if self._holder["held"]:
            self._holder["held"] = False
            self._lock.release()


def test_concurrent_transitions_exactly_one_wins():
    store = {"row": {"workflow_id": "w1", "state": "quality_review",
                     "state_version": 1, "org_id": "org", "doc_type": "policy"},
             "commits": 0}
    lock = threading.Lock()

    def _apply(cur, config, row, to_state, actor, comment, **kw):
        # winning hop: bump the shared version (the UPDATE … WHERE state_version)
        store["row"]["state_version"] += 1
        store["row"]["state"] = to_state
        hop = {"from_state": "quality_review", "to_state": "draft",  # send-back: not forward
               "event_id": f"e{store['row']['state_version']}", "auto": False}
        return dict(store["row"]), hop

    with patch.object(sm, "connect_to_rds", lambda: _LockingConn(store, lock)), \
         patch.object(sm, "get_workflow_config", return_value={"states_json": {"transitions": {}}}), \
         patch.object(sm, "_apply_single_transition", side_effect=_apply), \
         patch.object(sm, "get_actor_role_ids", return_value=[]), \
         patch.object(sm, "get_workflow", side_effect=lambda wid: dict(store["row"])):

        def _attempt(_):
            try:
                sm.transition("w1", expected_state_version=1, to_state="draft",
                              actor_user_id="actor")
                return "ok"
            except sm.WorkflowConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(_attempt, range(8)))

    assert results.count("ok") == 1, results          # exactly one winner
    assert results.count("conflict") == 7
    assert store["commits"] == 1                       # only the winner committed
    assert store["row"]["state_version"] == 2          # bumped exactly once
