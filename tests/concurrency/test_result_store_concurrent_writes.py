"""Concurrency tests for tests_routes/result_store.py.

Validates that concurrent reads and writes to the result store do not
produce corrupted data, non-unique run IDs, or visible partial writes.
The result store uses atomic os.replace() for all JSON writes, which should
make reads always see either the old or the new complete file — never a
half-written one.

Each test redirects RESULTS_ROOT and SUMMARY_PATH to a tmp_path so the
test suite does not pollute the real testing/results directory.
"""

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import MagicMock
import sys

import pytest

# Stub heavy deps that result_store's transitive imports may pull in
for _mod in ("pymysql", "pymysql.cursors", "db", "db.rds_db", "db.db_checkers",
             "boto3", "dotenv", "dbutils", "dbutils.pooled_db"):
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))
sys.modules.setdefault("utils.s3_utils", MagicMock())
sys.modules.setdefault("utils.base_logger", MagicMock(get_logger=MagicMock(return_value=MagicMock())))

import tests_routes.result_store as rs
from tests_routes.result_store import (
    new_run_id,
    read_category_result,
    read_summary,
    write_category_result,
)


# ---------------------------------------------------------------------------
# Fixture: redirect result store to a temp directory
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolate_result_store(tmp_path):
    """Redirect all result_store paths to tmp_path for test isolation."""
    old_root = rs.RESULTS_ROOT
    old_summary = rs.SUMMARY_PATH
    new_root = str(tmp_path / "results")
    rs.RESULTS_ROOT = new_root
    rs.SUMMARY_PATH = os.path.join(new_root, "summary.json")
    os.makedirs(new_root, exist_ok=True)
    yield
    rs.RESULTS_ROOT = old_root
    rs.SUMMARY_PATH = old_summary


# ---------------------------------------------------------------------------
# Helper: build a minimal valid payload
# ---------------------------------------------------------------------------

def _make_payload(category: str, run_id: str, status: str = "passed") -> dict:
    return {
        "category": category,
        "run_id": run_id,
        "started_at": "2026-05-26T10:00:00+00:00",
        "finished_at": "2026-05-26T10:00:05+00:00",
        "duration_seconds": 5.0,
        "status": status,
        "summary": {"total": 1, "passed": 1, "failed": 0, "skipped": 0, "errors": 0},
        "tests": [],
        "metrics": None,
    }


# ---------------------------------------------------------------------------
# Test 1: Concurrent writes to the same category do not corrupt data
# ---------------------------------------------------------------------------

@pytest.mark.concurrency
def test_concurrent_writes_same_category_no_corruption():
    """10 threads writing to backend_unit produce a valid final latest.json."""
    category = "backend_unit"
    errors = []

    def worker(i):
        run_id = f"run-{i:04d}-{new_run_id()}"
        status = "passed" if i % 2 == 0 else "failed"
        try:
            write_category_result(category, run_id, _make_payload(category, run_id, status))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"Threads raised: {errors}"

    # latest.json must be valid JSON and have a known status
    latest_path = os.path.join(rs.RESULTS_ROOT, category, "latest.json")
    assert os.path.exists(latest_path)
    with open(latest_path, encoding="utf-8") as f:
        data = json.load(f)
    assert data["status"] in ("passed", "failed")
    assert data["category"] == category


# ---------------------------------------------------------------------------
# Test 2: Concurrent writes to different categories do not interfere
# ---------------------------------------------------------------------------

@pytest.mark.concurrency
def test_concurrent_writes_different_categories_no_interference():
    """5 threads each write to a different category; all latest.json files are valid."""
    categories = [
        "backend_unit",
        "backend_integration",
        "backend_regression",
        "backend_load",
        "backend_stress",
    ]
    errors = []

    def worker(cat):
        run_id = new_run_id()
        try:
            write_category_result(cat, run_id, _make_payload(cat, run_id, "passed"))
        except Exception as exc:
            errors.append((cat, exc))

    threads = [threading.Thread(target=worker, args=(c,)) for c in categories]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"Threads raised: {errors}"

    for cat in categories:
        latest_path = os.path.join(rs.RESULTS_ROOT, cat, "latest.json")
        assert os.path.exists(latest_path), f"Missing latest.json for {cat}"
        with open(latest_path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["category"] == cat


# ---------------------------------------------------------------------------
# Test 3: new_run_id is unique across threads
# ---------------------------------------------------------------------------

@pytest.mark.concurrency
def test_new_run_id_is_unique_across_threads():
    """100 threads each generate a run_id; all 100 must be distinct."""
    run_ids = []
    lock = threading.Lock()

    def worker():
        rid = new_run_id()
        with lock:
            run_ids.append(rid)

    threads = [threading.Thread(target=worker) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(run_ids) == 100
    assert len(set(run_ids)) == 100, "Duplicate run IDs detected — UUID generation not unique"


# ---------------------------------------------------------------------------
# Test 4: Concurrent summary reads are stable while writes happen
# ---------------------------------------------------------------------------

@pytest.mark.concurrency
def test_concurrent_summary_reads_stable():
    """Reader threads never get a JSONDecodeError or missing keys during concurrent writes."""
    category = "backend_unit"
    read_errors = []
    stop_event = threading.Event()

    def writer():
        for i in range(20):
            if stop_event.is_set():
                break
            run_id = new_run_id()
            try:
                write_category_result(
                    category, run_id,
                    _make_payload(category, run_id, "passed")
                )
            except Exception:
                pass  # writer errors are not what we're testing
            time.sleep(0.005)

    def reader():
        while not stop_event.is_set():
            try:
                summary = read_summary()
                # Must have the top-level keys
                _ = summary["categories"]
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                read_errors.append(exc)
            time.sleep(0.001)

    writer_threads = [threading.Thread(target=writer) for _ in range(5)]
    reader_threads = [threading.Thread(target=reader) for _ in range(5)]

    for t in reader_threads + writer_threads:
        t.start()

    # Let writers finish, then signal readers to stop
    for t in writer_threads:
        t.join(timeout=15)
    stop_event.set()
    for t in reader_threads:
        t.join(timeout=5)

    assert not read_errors, (
        f"Readers observed {len(read_errors)} errors during concurrent writes: "
        f"{read_errors[:3]}"
    )
