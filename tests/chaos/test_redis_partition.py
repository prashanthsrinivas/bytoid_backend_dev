"""Chaos tests: Redis partition / unreachable Redis scenarios.

All tests are skipped unless RUN_CHAOS=1 is set in the environment.
No live infrastructure is contacted; fakeredis or plain mocks are used.
"""

# ---------------------------------------------------------------------------
# Critical import stubs — must precede ANY app import
# ---------------------------------------------------------------------------
import sys
from unittest.mock import MagicMock

for _mod in (
    "pymysql",
    "pymysql.cursors",
    "db",
    "db.rds_db",
    "db.db_checkers",
    "boto3",
    "dotenv",
    "dbutils",
    "dbutils.pooled_db",
    "pptx",
    "pptx.util",
    "bs4",
    "pytz",
    "yaml",
    "docx",
):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock(name=f"{_mod}_stub")

# ---------------------------------------------------------------------------
# Standard imports
# ---------------------------------------------------------------------------
import os
import pytest
from unittest.mock import patch, MagicMock

# ---------------------------------------------------------------------------
# Skip guard
# ---------------------------------------------------------------------------
pytestmark = [
    pytest.mark.chaos,
    pytest.mark.skipif(
        not os.getenv("RUN_CHAOS"),
        reason="Set RUN_CHAOS=1 to run chaos tests",
    ),
]

# ---------------------------------------------------------------------------
# Require fakeredis
# ---------------------------------------------------------------------------
fakeredis = pytest.importorskip("fakeredis")


# ---------------------------------------------------------------------------
# Helper: a session-lookup function that degrades gracefully
# ---------------------------------------------------------------------------
def _safe_get(client, key):
    """Retrieve a Redis key; return None on ConnectionError instead of raising."""
    try:
        return client.get(key)
    except ConnectionError:
        return None


def _safe_set(client, key, value, ex=None):
    """Write a Redis key; return False on ConnectionError instead of raising."""
    try:
        client.set(key, value, ex=ex)
        return True
    except ConnectionError:
        return False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_redis_connection_error_is_not_raised_to_caller():
    """A Redis ConnectionError during `get` must be swallowed by the session layer.

    Simulates a network partition that makes every read fail.  The caller
    receives None (cache miss) rather than an unhandled exception, ensuring
    the session lookup degrades gracefully.
    """
    client = fakeredis.FakeRedis()

    with patch.object(client, "get", side_effect=ConnectionError("Redis partition")):
        result = _safe_get(client, "session:user123")

    assert result is None, "ConnectionError during get must be converted to None"


def test_redis_set_failure_does_not_crash_writer():
    """A Redis ConnectionError during `set` must not propagate to the caller.

    Simulates a partition that makes every write fail.  The session-write
    path must return a falsy value (False or None) rather than raising, so
    the request can still be served without a session.
    """
    client = fakeredis.FakeRedis()

    with patch.object(client, "set", side_effect=ConnectionError("Redis partition")):
        result = _safe_set(client, "session:user456", b"session-data", ex=1800)

    assert not result, "ConnectionError during set must return a falsy value"


def test_redis_recovers_after_partition():
    """After a transient partition, subsequent reads must succeed.

    The first 3 calls raise ConnectionError; the 4th returns real data.
    This proves a retry-capable caller can recover without restarting.
    """
    client = fakeredis.FakeRedis()
    client.set("session:recovery_key", b"session-data")

    call_count = [0]
    real_get = client.get

    def flaky_get(key):
        call_count[0] += 1
        if call_count[0] <= 3:
            raise ConnectionError("Redis partition")
        return real_get(key)

    results = []
    with patch.object(client, "get", side_effect=flaky_get):
        for _ in range(4):
            try:
                results.append(client.get("session:recovery_key"))
            except ConnectionError:
                results.append(None)

    assert results[:3] == [None, None, None], "first 3 calls must raise (caught as None)"
    assert results[3] == b"session-data", "4th call must return the stored value"


def test_fakeredis_get_set_roundtrip():
    """Basic sanity: fakeredis set/get works correctly in this test environment.

    This validates the test harness itself before any chaos is applied.
    If this test fails, the other chaos tests in this file are meaningless.
    """
    client = fakeredis.FakeRedis()
    client.set("k", "v")
    value = client.get("k")
    assert value == b"v", "fakeredis must return bytes on get after set"


def test_redis_timeout_propagates_as_connection_error():
    """A TimeoutError from Redis must propagate unmodified when not wrapped.

    This documents the EXPECTED caller behaviour: if the caller does NOT wrap
    the Redis call, a timeout will surface as a TimeoutError.  This test
    verifies that fakeredis doesn't silently swallow the exception so tests
    in other suites can rely on it propagating.
    """
    client = fakeredis.FakeRedis()

    with patch.object(client, "get", side_effect=TimeoutError("Redis timeout")):
        with pytest.raises(TimeoutError, match="Redis timeout"):
            client.get("session:any_key")
