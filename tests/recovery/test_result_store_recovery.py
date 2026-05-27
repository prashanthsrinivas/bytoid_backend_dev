"""Recovery tests for tests_routes/result_store.py.

Validates that the result store degrades gracefully when the filesystem is in
a bad state: missing files, corrupted JSON, missing parent directories, and
OS-level errors during atomic writes. The store must never propagate exceptions
to callers for read operations and must rebuild state correctly after corruption.

Each test redirects RESULTS_ROOT and SUMMARY_PATH to tmp_path to avoid
polluting the real testing/results directory.
"""

import json
import os
import threading
import time
from unittest.mock import MagicMock, patch
import sys

import pytest

# Stub heavy transitive deps before importing result_store
for _mod in ("pymysql", "pymysql.cursors", "db", "db.rds_db", "db.db_checkers",
             "boto3", "dotenv", "dbutils", "dbutils.pooled_db"):
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))
sys.modules.setdefault("utils.s3_utils", MagicMock())
sys.modules.setdefault("utils.base_logger", MagicMock(get_logger=MagicMock(return_value=MagicMock())))

import tests_routes.result_store as rs
from tests_routes.result_store import (
    list_history,
    new_run_id,
    read_category_result,
    read_summary,
    write_category_result,
)


# ---------------------------------------------------------------------------
# Fixture: redirect result store paths
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolate(tmp_path):
    old_root = rs.RESULTS_ROOT
    old_summary = rs.SUMMARY_PATH
    new_root = str(tmp_path / "results")
    rs.RESULTS_ROOT = new_root
    rs.SUMMARY_PATH = os.path.join(new_root, "summary.json")
    os.makedirs(new_root, exist_ok=True)
    yield tmp_path
    rs.RESULTS_ROOT = old_root
    rs.SUMMARY_PATH = old_summary


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_payload(category: str, run_id: str, status: str = "passed") -> dict:
    return {
        "category": category,
        "run_id": run_id,
        "started_at": "2026-05-26T10:00:00+00:00",
        "finished_at": "2026-05-26T10:00:05+00:00",
        "duration_seconds": 5.0,
        "status": status,
        "summary": {"total": 3, "passed": 3, "failed": 0, "skipped": 0, "errors": 0},
        "tests": [],
        "metrics": None,
    }


# ---------------------------------------------------------------------------
# Test 1: Missing summary.json returns safe default
# ---------------------------------------------------------------------------

def test_read_summary_returns_safe_default_when_file_missing():
    """read_summary returns a valid default dict when summary.json does not exist."""
    # summary.json was not created; isolate fixture gives a clean dir
    result = read_summary()
    assert isinstance(result, dict)
    assert "categories" in result
    assert "updated_at" in result
    # Every known category should appear
    from tests_routes.categories import ALL_CATEGORIES
    for cat in ALL_CATEGORIES:
        assert cat in result["categories"]


# ---------------------------------------------------------------------------
# Test 2: Corrupt summary.json returns safe default
# ---------------------------------------------------------------------------

def test_read_summary_recovers_from_corrupt_json():
    """read_summary returns safe default (no raise) when summary.json is invalid JSON."""
    with open(rs.SUMMARY_PATH, "w") as f:
        f.write("{INVALID JSON ...")

    result = read_summary()
    assert isinstance(result, dict)
    assert "categories" in result


# ---------------------------------------------------------------------------
# Test 3: Missing latest.json returns None
# ---------------------------------------------------------------------------

def test_read_category_result_returns_none_when_file_missing():
    """read_category_result returns None when the category's latest.json doesn't exist."""
    result = read_category_result("backend_unit")
    assert result is None


# ---------------------------------------------------------------------------
# Test 4: Invalid category returns None (not ValueError)
# ---------------------------------------------------------------------------

def test_read_category_result_returns_none_for_invalid_category():
    """read_category_result returns None for an unknown category — does not raise."""
    result = read_category_result("this_is_not_a_real_category")
    assert result is None


# ---------------------------------------------------------------------------
# Test 5: write succeeds even when parent directory doesn't exist yet
# ---------------------------------------------------------------------------

def test_write_category_result_handles_missing_parent_dir(tmp_path):
    """write_category_result creates missing directories automatically."""
    # Redirect to a path that definitely doesn't exist yet
    deep_root = str(tmp_path / "deep" / "nested" / "results")
    rs.RESULTS_ROOT = deep_root
    rs.SUMMARY_PATH = os.path.join(deep_root, "summary.json")
    # Do NOT pre-create the directory
    assert not os.path.exists(deep_root)

    run_id = new_run_id()
    # Should not raise; makedirs must be called internally
    write_category_result("backend_unit", run_id, _make_payload("backend_unit", run_id))

    latest = os.path.join(deep_root, "backend_unit", "latest.json")
    assert os.path.exists(latest)
    with open(latest) as f:
        data = json.load(f)
    assert data["run_id"] == run_id


# ---------------------------------------------------------------------------
# Test 6: list_history returns [] when history dir doesn't exist
# ---------------------------------------------------------------------------

def test_list_history_returns_empty_for_missing_dir():
    """list_history returns an empty list when the history directory doesn't exist."""
    result = list_history("backend_unit", limit=25)
    assert result == []


# ---------------------------------------------------------------------------
# Test 7: list_history skips corrupt JSON files
# ---------------------------------------------------------------------------

def test_list_history_skips_corrupt_json_files():
    """list_history returns valid entries and silently skips corrupt JSON files."""
    category = "backend_regression"
    hist_dir = os.path.join(rs.RESULTS_ROOT, category, "history")
    os.makedirs(hist_dir, exist_ok=True)

    run_ids = [new_run_id() for _ in range(2)]

    # Write 2 valid history files
    for rid in run_ids:
        payload = _make_payload(category, rid, "passed")
        with open(os.path.join(hist_dir, f"{rid}.json"), "w") as f:
            json.dump(payload, f)

    # Write 1 corrupt file
    with open(os.path.join(hist_dir, "corrupt_file.json"), "w") as f:
        f.write("{CORRUPTED}")

    result = list_history(category, limit=25)
    # Should return exactly the 2 valid ones
    assert len(result) == 2
    returned_run_ids = {entry["run_id"] for entry in result}
    for rid in run_ids:
        assert rid in returned_run_ids


# ---------------------------------------------------------------------------
# Test 8: write recovers after os.replace raises once
# ---------------------------------------------------------------------------

def test_write_recovers_after_temp_file_collision():
    """write_category_result succeeds on the second call even if first had an OS error."""
    category = "backend_unit"
    run_id = new_run_id()

    # First call: simulate os.replace raising OSError once
    original_replace = os.replace
    call_count = [0]

    def flaky_replace(src, dst):
        call_count[0] += 1
        if call_count[0] == 1:
            # Simulate a transient failure on the first call
            if os.path.exists(src):
                os.remove(src)
            raise OSError("Simulated transient rename failure")
        return original_replace(src, dst)

    with patch("os.replace", side_effect=flaky_replace):
        with pytest.raises(OSError):
            write_category_result(category, run_id, _make_payload(category, run_id))

    # Second call (no mock) must succeed
    run_id2 = new_run_id()
    write_category_result(category, run_id2, _make_payload(category, run_id2, "passed"))
    result = read_category_result(category)
    assert result is not None
    assert result["run_id"] == run_id2


# ---------------------------------------------------------------------------
# Test 9: summary is rebuilt correctly after corrupt summary + fresh write
# ---------------------------------------------------------------------------

def test_summary_all_categories_after_corrupt_summary():
    """After corrupt summary.json, a fresh write rebuilds a valid summary."""
    # Write corrupt summary
    with open(rs.SUMMARY_PATH, "w") as f:
        f.write("{CORRUPTED}")

    # Now write a result — this should rebuild the summary from scratch
    run_id = new_run_id()
    write_category_result(
        "backend_unit", run_id, _make_payload("backend_unit", run_id, "passed")
    )

    # summary.json must now be valid
    with open(rs.SUMMARY_PATH) as f:
        summary = json.load(f)

    assert "categories" in summary
    assert "backend_unit" in summary["categories"]
    assert summary["categories"]["backend_unit"]["run_id"] == run_id
    assert summary["categories"]["backend_unit"]["status"] == "passed"


# ---------------------------------------------------------------------------
# Test 10: No exception propagates to caller during concurrent read/write
# ---------------------------------------------------------------------------

def test_concurrent_read_during_write_no_panic():
    """Simultaneous reads while writing must never raise an exception to the caller."""
    category = "backend_integration"
    caller_errors = []
    stop_event = threading.Event()

    def writer():
        for i in range(15):
            if stop_event.is_set():
                break
            run_id = new_run_id()
            try:
                write_category_result(
                    category, run_id,
                    _make_payload(category, run_id, "passed" if i % 2 == 0 else "failed")
                )
            except Exception:
                pass
            time.sleep(0.005)
        stop_event.set()

    def reader():
        while not stop_event.is_set():
            try:
                result = read_category_result(category)
                # result may be None (file not yet written) or a dict — both fine
                if result is not None:
                    assert isinstance(result, dict)
            except Exception as exc:
                caller_errors.append(exc)
            time.sleep(0.001)

    writer_thread = threading.Thread(target=writer)
    reader_threads = [threading.Thread(target=reader) for _ in range(4)]

    for t in reader_threads:
        t.start()
    writer_thread.start()

    writer_thread.join(timeout=10)
    stop_event.set()
    for t in reader_threads:
        t.join(timeout=5)

    assert not caller_errors, (
        f"Reader threads saw {len(caller_errors)} exceptions: {caller_errors[:3]}"
    )
