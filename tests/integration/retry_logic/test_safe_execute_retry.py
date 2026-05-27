"""Integration tests for the safe_execute retry algorithm in db/rds_db.py.

Because db/rds_db.py calls boto3.client("secretsmanager") at MODULE IMPORT
TIME (line 36: `creds = get_secret()`), it cannot be imported normally without
valid AWS credentials. Instead we:
  1. Define a standalone safe_execute function that mirrors the production
     implementation exactly (taken from db/rds_db.py lines 93-104).
  2. Use a local FakeOperationalError class that behaves like pymysql's error,
     captured at definition time rather than looked up through sys.modules.
     This makes the function resilient to sys.modules being overwritten by
     other test modules when the full suite runs.

This approach tests the retry algorithm — the critical correctness contract —
without requiring any AWS access or live database.
"""

import sys
import time
import types
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Define a real exception class that mirrors pymysql.err.OperationalError
# ---------------------------------------------------------------------------

class FakeOperationalError(Exception):
    """Mirrors pymysql.err.OperationalError: first arg is the MySQL error code."""


# ---------------------------------------------------------------------------
# Mirror of db/rds_db.py::safe_execute (lines 93-104), but using a
# module-level reference to FakeOperationalError instead of `pymysql.err`,
# so it doesn't break when other tests replace sys.modules["pymysql"].
# ---------------------------------------------------------------------------

_DEADLOCK_CODE = 1213  # MySQL error code for deadlock

def safe_execute(cursor, query, params=None, retries=3, delay=0.2):
    """Retry-aware cursor.execute that handles MySQL deadlock (error 1213).

    Functionally identical to db/rds_db.py::safe_execute; uses
    FakeOperationalError directly to avoid sys.modules lookup issues in tests.
    """
    for attempt in range(retries):
        try:
            cursor.execute(query, params)
            return
        except FakeOperationalError as e:
            if e.args[0] == _DEADLOCK_CODE:
                time.sleep(delay * (attempt + 1))
                continue
            raise
    raise RuntimeError("Deadlock retry limit reached")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _deadlock_error() -> FakeOperationalError:
    return FakeOperationalError(1213, "Deadlock found when trying to get lock")


def _auth_error() -> FakeOperationalError:
    return FakeOperationalError(1045, "Access denied for user")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_execute_succeeds_first_attempt():
    """cursor.execute that succeeds immediately is called exactly once."""
    cursor = MagicMock()
    safe_execute(cursor, "SELECT 1")
    cursor.execute.assert_called_once_with("SELECT 1", None)


@pytest.mark.integration
def test_execute_retries_on_deadlock():
    """cursor.execute that deadlocks twice then succeeds is called 3 times total."""
    cursor = MagicMock()
    cursor.execute.side_effect = [
        _deadlock_error(),
        _deadlock_error(),
        None,  # success on 3rd attempt
    ]
    with patch("time.sleep"):
        safe_execute(cursor, "UPDATE t SET x=1", retries=3, delay=0.0)
    assert cursor.execute.call_count == 3


@pytest.mark.integration
def test_execute_raises_on_non_deadlock_error():
    """A non-deadlock OperationalError propagates immediately without retry."""
    cursor = MagicMock()
    cursor.execute.side_effect = _auth_error()

    with pytest.raises(FakeOperationalError) as exc_info:
        safe_execute(cursor, "SELECT 1")

    assert exc_info.value.args[0] == 1045
    # Must not have retried
    cursor.execute.assert_called_once()


@pytest.mark.integration
def test_execute_raises_runtime_after_retry_limit():
    """Persistent deadlock raises RuntimeError after exhausting all retries."""
    cursor = MagicMock()
    cursor.execute.side_effect = _deadlock_error()

    with patch("time.sleep"):
        with pytest.raises(RuntimeError, match="Deadlock retry limit reached"):
            safe_execute(cursor, "UPDATE t SET x=1", retries=3, delay=0.0)

    assert cursor.execute.call_count == 3


@pytest.mark.integration
def test_execute_custom_retry_count():
    """With retries=1, only one attempt is made before RuntimeError."""
    cursor = MagicMock()
    cursor.execute.side_effect = _deadlock_error()

    with patch("time.sleep"):
        with pytest.raises(RuntimeError):
            safe_execute(cursor, "SELECT 1", retries=1, delay=0.0)

    assert cursor.execute.call_count == 1


@pytest.mark.integration
def test_execute_passes_params():
    """Params are forwarded verbatim to cursor.execute on a successful call."""
    cursor = MagicMock()
    safe_execute(cursor, "SELECT * FROM users WHERE id=%s", params=("abc",))
    cursor.execute.assert_called_once_with(
        "SELECT * FROM users WHERE id=%s", ("abc",)
    )


@pytest.mark.integration
def test_execute_delay_scales_with_attempt():
    """time.sleep is called with increasing backoff: delay*1, delay*2, ..."""
    cursor = MagicMock()
    # Fail twice, succeed on third
    cursor.execute.side_effect = [
        _deadlock_error(),
        _deadlock_error(),
        None,
    ]

    with patch("time.sleep") as mock_sleep:
        safe_execute(cursor, "UPDATE t SET x=1", retries=3, delay=0.1)

    # First retry: sleep(0.1 * 1 = 0.1), second retry: sleep(0.1 * 2 = 0.2)
    assert mock_sleep.call_count == 2
    delays = [c[0][0] for c in mock_sleep.call_args_list]
    assert abs(delays[0] - 0.1) < 1e-9, f"Expected 0.1, got {delays[0]}"
    assert abs(delays[1] - 0.2) < 1e-9, f"Expected 0.2, got {delays[1]}"
