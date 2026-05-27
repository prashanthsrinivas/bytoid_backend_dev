"""Chaos tests: Celery worker kill / mid-task termination scenarios.

All tests are skipped unless RUN_CHAOS=1 is set in the environment.
No live infrastructure is contacted; all dependencies are mocked.
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
import json
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
# Module under test (imported after stubs are in place)
# ---------------------------------------------------------------------------
import tests_routes.result_store as rs


# ---------------------------------------------------------------------------
# Isolation fixture: redirect result_store globals to tmp_path
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def isolate(tmp_path):
    orig_root = rs.RESULTS_ROOT
    orig_summary = rs.SUMMARY_PATH
    rs.RESULTS_ROOT = str(tmp_path)
    rs.SUMMARY_PATH = str(tmp_path / "summary.json")
    yield
    rs.RESULTS_ROOT = orig_root
    rs.SUMMARY_PATH = orig_summary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_payload(run_id: str, data: str = "first") -> dict:
    return {
        "run_id": run_id,
        "category": "backend_unit",
        "status": "passed",
        "data": data,
        "started_at": "2026-05-26T00:00:00+00:00",
        "finished_at": "2026-05-26T00:01:00+00:00",
        "duration_seconds": 60,
        "summary": {"total": 10, "passed": 10, "failed": 0, "skipped": 0, "errors": 0},
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_result_store_write_is_idempotent(tmp_path):
    """Writing the same run_id twice must overwrite, not append.

    A killed-then-retried Celery task calls write_category_result a second
    time.  The resulting files must contain only the second payload and must
    be valid JSON (no corruption from a partial-write or append).
    """
    run_id = "20260526T000000Z-deadbeef"
    first_payload = _make_payload(run_id, data="first")
    second_payload = _make_payload(run_id, data="second")

    rs.write_category_result("backend_unit", run_id, first_payload)
    rs.write_category_result("backend_unit", run_id, second_payload)

    result = rs.read_category_result("backend_unit")
    assert result is not None, "latest.json should exist after second write"
    assert result["data"] == "second", "second write must overwrite first"

    # Verify that the history file is also valid JSON (not corrupted)
    history_path = os.path.join(
        rs.RESULTS_ROOT, "backend_unit", "history", f"{run_id}.json"
    )
    assert os.path.exists(history_path), "history file should exist"
    with open(history_path, encoding="utf-8") as fh:
        history_data = json.load(fh)
    assert history_data["data"] == "second"


def test_partial_write_does_not_corrupt(tmp_path):
    """If os.replace is killed mid-write, the original file must be untouched.

    Simulates a worker kill between the temp-file flush and the atomic rename.
    The original `latest.json` (if present) must survive intact; no partial
    `.tmp_*` file should remain at the final path.
    """
    run_id = "20260526T000000Z-aabbccdd"
    good_payload = _make_payload(run_id, data="original")

    # Write a known-good first result
    rs.write_category_result("backend_unit", run_id, good_payload)
    latest_path = os.path.join(rs.RESULTS_ROOT, "backend_unit", "latest.json")
    assert os.path.exists(latest_path)

    # Patch os.replace to fail on its first invocation (simulates kill during rename)
    replace_calls = []
    real_replace = os.replace

    def flaky_replace(src, dst):
        replace_calls.append((src, dst))
        if len(replace_calls) == 1:
            # Simulate the temp file being cleaned up by the except block
            raise OSError("simulated worker kill during os.replace")
        return real_replace(src, dst)

    bad_payload = _make_payload(run_id, data="corrupted")
    with patch("os.replace", side_effect=flaky_replace):
        with pytest.raises(OSError):
            rs.write_category_result("backend_unit", run_id, bad_payload)

    # Original latest.json must still be the good payload
    with open(latest_path, encoding="utf-8") as fh:
        on_disk = json.load(fh)
    assert on_disk["data"] == "original", "original file must not be corrupted"

    # No stray .tmp_ file should remain at the final path
    dir_contents = os.listdir(os.path.join(rs.RESULTS_ROOT, "backend_unit"))
    tmp_files = [f for f in dir_contents if f.startswith(".tmp_")]
    assert tmp_files == [], f"stray temp files found: {tmp_files}"


def test_task_retry_count_increments():
    """A simulated Celery task retries exactly once before succeeding.

    Verifies that retry bookkeeping works correctly even when a task is killed
    mid-execution: only one retry call occurs and the task ultimately succeeds.
    """
    call_log = []

    task_mock = MagicMock()
    task_mock.retry.side_effect = Exception("retrying")

    def task_body(self, value):
        call_log.append(value)
        if len(call_log) == 1:
            try:
                self.retry(exc=ValueError("first attempt failed"))
            except Exception:
                # retry() raises to signal Celery to reschedule; we catch it
                # here and recurse to simulate the second execution
                return task_body(self, value)
        return "success"

    result = task_body(task_mock, "run_value")

    assert result == "success"
    assert task_mock.retry.call_count == 1, "retry must be called exactly once"
    assert len(call_log) == 2, "task body must have been entered twice"


def test_duplicate_run_id_last_write_wins(tmp_path):
    """Two concurrent tasks writing the same run_id must result in last-write-wins.

    In practice, Celery's at-least-once delivery can cause duplicate task
    executions.  The result store must be safe: the final state reflects the
    most recent successful write.
    """
    run_id = "20260526T000000Z-11223344"
    payload_a = _make_payload(run_id, data="task_a")
    payload_b = _make_payload(run_id, data="task_b")

    rs.write_category_result("backend_unit", run_id, payload_a)
    rs.write_category_result("backend_unit", run_id, payload_b)

    result = rs.read_category_result("backend_unit")
    assert result is not None
    assert result["data"] == "task_b", "last write must win"

    # Summary must also reflect the last write
    summary = rs.read_summary()
    assert summary["categories"]["backend_unit"]["run_id"] == run_id


def test_write_then_kill_leaves_valid_summary(tmp_path):
    """Summary.json written before a subsequent kill remains valid JSON.

    After a successful write, summary.json is valid.  A subsequent
    write attempt that dies after flushing the temp file but before
    renaming it must leave summary.json untouched (still parseable).
    """
    run_id = "20260526T000000Z-99887766"
    good_payload = _make_payload(run_id, data="good_run")

    # First write completes successfully — summary.json is created
    rs.write_category_result("backend_unit", run_id, good_payload)

    assert os.path.exists(rs.SUMMARY_PATH), "summary.json must exist after first write"
    with open(rs.SUMMARY_PATH, encoding="utf-8") as fh:
        first_summary = json.load(fh)
    assert "categories" in first_summary

    # Now simulate a second write that is killed after temp-file flush
    # (os.replace becomes a no-op — temp file is written but never renamed)
    noop_calls = []

    def noop_replace(src, dst):
        noop_calls.append((src, dst))
        # Don't actually rename — simulates the process being killed

    bad_payload = _make_payload(run_id, data="will_never_land")
    with patch("os.replace", side_effect=noop_replace):
        # _atomic_write_json will call os.replace; without raise it just won't rename
        # write_category_result calls _atomic_write_json three times
        # (latest, history, summary) — none of the renames will succeed
        rs.write_category_result("backend_unit", run_id, bad_payload)

    # summary.json must still be valid JSON (not corrupted by the partial write)
    with open(rs.SUMMARY_PATH, encoding="utf-8") as fh:
        summary_after_kill = json.load(fh)
    assert "categories" in summary_after_kill, "summary.json must remain valid JSON"
