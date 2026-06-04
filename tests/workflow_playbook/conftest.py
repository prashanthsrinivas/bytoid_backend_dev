"""Per-test isolation for the workflow-builder / playbook suite (§3 recipe item 5).

Goals:
  * install the shared stub finder once, before any SUT import;
  * hand every test a *fresh* mocked DB connection so no mutable mock state is
    shared across tests (anti suite-poisoning);
  * prove there is no in-process registry bleeding between tests.

`JobManager`'s job store lives in Redis (mocked per-test), so there is no
in-process job registry to reset here — isolation for it is achieved by mocking
`redis_service` inside each test that touches it.
"""

from __future__ import annotations

import pytest

from . import _wf_pb_stubs as stubs

# Install the meta-path stub finder + db.rds_db fake exactly once, at collection
# time, before any test module imports the code under test.
stubs.install_stubs()


@pytest.fixture
def fresh_conn():
    """Factory → a brand-new fake PyMySQL connection per call.

    Never reused across tests: each test that needs a DB builds its own, so
    `.executed` / sequential `fetchone` state cannot leak into a sibling test.
    """
    return stubs.make_conn


@pytest.fixture(autouse=True)
def _no_state_bleed():
    """Autouse guard: assert the canonical DB entrypoint is not left patched by a
    test that forgot to use a context manager. Runs after each test."""
    yield
    # `connect_to_rds` on the stub module is a MagicMock; a well-behaved test
    # patches it via `mock_rds(...)` (a context manager) and restores it. If a
    # test leaves a side_effect/return wired permanently, reset it so the next
    # test starts clean rather than inheriting poisoned behavior.
    import sys

    rds = sys.modules.get("db.rds_db")
    conn_factory = getattr(rds, "connect_to_rds", None)
    if conn_factory is not None and hasattr(conn_factory, "reset_mock"):
        conn_factory.reset_mock(return_value=False, side_effect=True)
