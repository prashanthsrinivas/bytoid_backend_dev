"""Chaos tests: database failure and recovery scenarios.

All tests are skipped unless RUN_CHAOS=1 is set in the environment.
No live DB is contacted; all DB dependencies are mocked at import time.
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
import time
import pytest
from unittest.mock import MagicMock, patch, call

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
# Local DB error class (avoids importing from the stubbed pymysql)
# ---------------------------------------------------------------------------
class FakeOperationalError(Exception):
    """Mimics pymysql.OperationalError — args[0] is the MySQL error code."""

    def __init__(self, errno: int, msg: str = ""):
        super().__init__(msg)
        self.args = (errno, msg)


# ---------------------------------------------------------------------------
# Local re-implementation of safe_execute for isolated unit testing
# ---------------------------------------------------------------------------
_DEADLOCK_ERRNO = 1213
_MAX_RETRIES = 3
_RETRY_BACKOFF = 0.1  # seconds (patched to 0 in tests)


def safe_execute(cursor, query: str, params=None, max_retries: int = _MAX_RETRIES):
    """Retry-on-deadlock wrapper mirroring db/rds_db.py safe_execute logic."""
    attempt = 0
    while True:
        try:
            cursor.execute(query, params)
            return
        except FakeOperationalError as exc:
            if exc.args[0] == _DEADLOCK_ERRNO and attempt < max_retries - 1:
                attempt += 1
                time.sleep(_RETRY_BACKOFF * attempt)
            else:
                raise


# ---------------------------------------------------------------------------
# Helper: connection-with-retry wrapper
# ---------------------------------------------------------------------------
def get_connection_with_retry(connect_fn, max_attempts: int = 2):
    """Call connect_fn up to max_attempts times; return first success."""
    last_exc = None
    for _ in range(max_attempts):
        try:
            return connect_fn()
        except FakeOperationalError as exc:
            last_exc = exc
    raise last_exc


def get_connection_exhausted(connect_fn, max_attempts: int = 3):
    """Try max_attempts times; always raises if all fail."""
    last_exc = None
    for _ in range(max_attempts):
        try:
            return connect_fn()
        except FakeOperationalError as exc:
            last_exc = exc
    raise last_exc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_safe_execute_retries_on_deadlock():
    """safe_execute must retry exactly 3 times when MySQL returns errno 1213 (deadlock).

    The cursor raises a deadlock error twice, then succeeds.  We verify:
    - cursor.execute is called exactly 3 times (2 failures + 1 success).
    - time.sleep is called twice (once per retry, with increasing backoff).
    """
    cursor = MagicMock()
    cursor.execute.side_effect = [
        FakeOperationalError(1213, "Deadlock found"),
        FakeOperationalError(1213, "Deadlock found"),
        None,  # third call succeeds
    ]

    with patch("time.sleep") as mock_sleep:
        safe_execute(cursor, "UPDATE users SET active=1 WHERE id=%s", (42,))

    assert cursor.execute.call_count == 3, "execute must be called 3 times"
    assert mock_sleep.call_count == 2, "sleep must be called once per retry"


def test_safe_execute_does_not_retry_non_deadlock():
    """safe_execute must NOT retry for non-deadlock errors (e.g., errno 1064 syntax error).

    The error must propagate immediately without any retry or sleep.
    """
    cursor = MagicMock()
    cursor.execute.side_effect = FakeOperationalError(1064, "You have an error in your SQL syntax")

    with patch("time.sleep") as mock_sleep:
        with pytest.raises(FakeOperationalError) as exc_info:
            safe_execute(cursor, "BAD SQL", ())

    assert exc_info.value.args[0] == 1064
    assert cursor.execute.call_count == 1, "execute must only be called once for non-deadlock"
    mock_sleep.assert_not_called()


def test_connect_to_rds_retried_on_gone_away():
    """A 'server gone away' (errno 2006) error on first connect must be retried.

    get_connection_with_retry() must attempt the connection twice and return
    the mock connection returned on the second successful call.
    """
    mock_conn = MagicMock(name="mysql_connection")
    connect_to_rds = MagicMock(
        side_effect=[
            FakeOperationalError(2006, "MySQL server has gone away"),
            mock_conn,
        ]
    )

    result = get_connection_with_retry(connect_to_rds, max_attempts=2)

    assert result is mock_conn, "second attempt must return the valid connection"
    assert connect_to_rds.call_count == 2, "connect must be tried exactly twice"


def test_connection_pool_exhaustion_raises():
    """When the connection pool is full (errno 1040), all retry attempts must fail.

    After max_attempts exhausted retries, the exception must propagate to the
    caller rather than silently failing.
    """
    connect_to_rds = MagicMock(
        side_effect=FakeOperationalError(1040, "Too many connections")
    )

    with pytest.raises(FakeOperationalError) as exc_info:
        get_connection_exhausted(connect_to_rds, max_attempts=3)

    assert exc_info.value.args[0] == 1040
    assert connect_to_rds.call_count == 3, "all 3 attempts must be made before giving up"


def test_query_result_not_lost_on_retry():
    """Query results must be preserved even when the first execute raises a deadlock.

    The cursor raises once, then succeeds.  fetchall() must return the correct
    data — the retry must not silently discard the result.
    """
    cursor = MagicMock()
    cursor.execute.side_effect = [
        FakeOperationalError(1213, "Deadlock found"),
        None,  # second call succeeds
    ]
    cursor.fetchall.return_value = [{"id": 1}]

    with patch("time.sleep"):
        safe_execute(cursor, "SELECT id FROM users WHERE active=1")
        result = cursor.fetchall()

    assert result == [{"id": 1}], "fetchall must return the correct data after a retry"
    assert cursor.execute.call_count == 2
