"""Unit tests for tests_routes/result_store.py.

The module normally writes to testing/results/ relative to the project root.
We redirect RESULTS_ROOT and SUMMARY_PATH to a pytest tmp_path so tests are
fully isolated and leave no real files behind.

Pattern used: mutate the already-imported module's globals in a fixture, then
restore them on teardown.  This is simpler than patching import machinery.
"""

import json
import os
import re
import time

import pytest

import tests_routes.result_store as rs
from tests_routes.categories import ALL_CATEGORIES


# ===========================================================================
# Fixture
# ===========================================================================


@pytest.fixture()
def store(tmp_path):
    """Redirect result_store to a fresh temp directory for each test."""
    orig_root = rs.RESULTS_ROOT
    orig_summary = rs.SUMMARY_PATH

    rs.RESULTS_ROOT = str(tmp_path)
    rs.SUMMARY_PATH = str(tmp_path / "summary.json")

    yield rs

    rs.RESULTS_ROOT = orig_root
    rs.SUMMARY_PATH = orig_summary


# ===========================================================================
# Helpers
# ===========================================================================

def _minimal_payload(run_id: str, status: str = "passed") -> dict:
    return {
        "run_id": run_id,
        "status": status,
        "started_at": "2026-05-26T10:00:00+00:00",
        "finished_at": "2026-05-26T10:00:05+00:00",
        "duration_seconds": 5,
        "summary": {"total": 10, "passed": 10, "failed": 0, "skipped": 0, "errors": 0},
    }


# ===========================================================================
# Tests
# ===========================================================================


@pytest.mark.unit
def test_new_run_id_format(store):
    """new_run_id() must start with a timestamp-like prefix and end with a hex suffix."""
    run_id = store.new_run_id()
    # Expected form: 20260526T123456Z-abcdef12
    assert re.match(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$", run_id), (
        f"Unexpected run_id format: {run_id!r}"
    )


@pytest.mark.unit
def test_new_run_id_unique(store):
    """Two consecutive new_run_id() calls should not collide (hex suffix differs)."""
    ids = {store.new_run_id() for _ in range(10)}
    # All 10 should be distinct
    assert len(ids) == 10


@pytest.mark.unit
def test_write_and_read_round_trip(store):
    """write_category_result followed by read_category_result must return equal data."""
    run_id = store.new_run_id()
    payload = _minimal_payload(run_id, status="passed")
    store.write_category_result("backend_unit", run_id, payload)

    result = store.read_category_result("backend_unit")
    assert result == payload


@pytest.mark.unit
def test_read_returns_none_for_unknown_category(store):
    """read_category_result for a category that has never been written returns None."""
    result = store.read_category_result("backend_unit")
    assert result is None


@pytest.mark.unit
def test_read_summary_includes_all_categories(store):
    """read_summary() must include a key for every category in ALL_CATEGORIES."""
    summary = store.read_summary()
    for cat in ALL_CATEGORIES:
        assert cat in summary["categories"], (
            f"Category {cat!r} missing from summary"
        )


@pytest.mark.unit
def test_write_updates_summary(store):
    """After writing a result, read_summary must reflect the new status."""
    run_id = store.new_run_id()
    payload = _minimal_payload(run_id, status="failed")
    store.write_category_result("backend_unit", run_id, payload)

    summary = store.read_summary()
    entry = summary["categories"]["backend_unit"]
    assert entry["status"] == "failed"


@pytest.mark.unit
def test_write_creates_history_file(store, tmp_path):
    """write_category_result must create <category>/history/<run_id>.json."""
    run_id = store.new_run_id()
    payload = _minimal_payload(run_id)
    store.write_category_result("backend_regression", run_id, payload)

    hist_file = tmp_path / "backend_regression" / "history" / f"{run_id}.json"
    assert hist_file.exists(), f"History file not created: {hist_file}"

    with hist_file.open(encoding="utf-8") as f:
        stored = json.load(f)
    assert stored == payload


@pytest.mark.unit
def test_list_history_empty_before_writes(store):
    """list_history must return an empty list if no results have been written."""
    result = store.list_history("backend_unit", 10)
    assert result == []


@pytest.mark.unit
def test_list_history_after_writes(store):
    """list_history must return entries after writing."""
    for i in range(3):
        run_id = store.new_run_id()
        store.write_category_result("backend_unit", run_id, _minimal_payload(run_id))

    history = store.list_history("backend_unit", 10)
    assert len(history) == 3


@pytest.mark.unit
def test_list_history_respects_limit(store):
    """list_history(limit=2) must return exactly 2 entries even if 5 exist."""
    for i in range(5):
        run_id = store.new_run_id()
        store.write_category_result("backend_unit", run_id, _minimal_payload(run_id))
        # Small sleep is unavoidable here because run_id embeds a second-precision
        # timestamp — generate different IDs without sleeping by using new_run_id's
        # uuid suffix uniqueness (already guaranteed), but the *sort* relies on the
        # lexicographic timestamp prefix.  Five rapid writes within the same second
        # produce identical prefixes; the hex suffix keeps them unique AND sortable.

    history = store.list_history("backend_unit", limit=2)
    assert len(history) == 2


@pytest.mark.unit
def test_atomic_write_recovers_on_failure(store, tmp_path):
    """If latest.json is corrupt (truncated), a subsequent write must succeed."""
    # First, create a valid entry
    run_id1 = store.new_run_id()
    store.write_category_result("backend_unit", run_id1, _minimal_payload(run_id1))

    # Corrupt the latest.json
    latest_path = tmp_path / "backend_unit" / "latest.json"
    latest_path.write_text("{{corrupt_json", encoding="utf-8")

    # Write a new result — must succeed despite the corrupt file
    run_id2 = store.new_run_id()
    payload2 = _minimal_payload(run_id2, status="passed")
    store.write_category_result("backend_unit", run_id2, payload2)

    result = store.read_category_result("backend_unit")
    assert result == payload2


@pytest.mark.unit
def test_invalid_category_raises(store):
    """write_category_result with an unknown category must raise ValueError."""
    with pytest.raises(ValueError, match="Unknown category"):
        store.write_category_result("bad_category", "run_1", {})


@pytest.mark.unit
def test_summary_never_run_status_for_unwritten_categories(store):
    """Categories that have never been written must show status='never_run'."""
    summary = store.read_summary()
    for cat in ALL_CATEGORIES:
        entry = summary["categories"][cat]
        assert entry["status"] == "never_run", (
            f"Expected never_run for {cat!r}, got {entry['status']!r}"
        )


@pytest.mark.unit
def test_write_category_result_latest_json_overwritten(store, tmp_path):
    """A second write for the same category must overwrite latest.json."""
    run_id1 = store.new_run_id()
    store.write_category_result("backend_integration", run_id1, _minimal_payload(run_id1, "failed"))

    run_id2 = store.new_run_id()
    payload2 = _minimal_payload(run_id2, "passed")
    store.write_category_result("backend_integration", run_id2, payload2)

    result = store.read_category_result("backend_integration")
    assert result["run_id"] == run_id2
    assert result["status"] == "passed"


@pytest.mark.unit
def test_summary_updated_at_is_set_after_write(store):
    """summary.updated_at must be a non-None ISO timestamp after any write."""
    run_id = store.new_run_id()
    store.write_category_result("backend_unit", run_id, _minimal_payload(run_id))

    summary = store.read_summary()
    assert summary.get("updated_at") is not None
    # Should look like an ISO 8601 datetime
    assert "T" in str(summary["updated_at"])


@pytest.mark.unit
def test_read_category_result_returns_none_for_invalid_category(store):
    """read_category_result for a completely invalid category must return None (not raise)."""
    result = store.read_category_result("totally_bogus")
    assert result is None


@pytest.mark.unit
def test_list_history_entries_have_expected_keys(store):
    """Each entry returned by list_history must have run_id, timestamp, summary, status keys."""
    run_id = store.new_run_id()
    store.write_category_result("backend_unit", run_id, _minimal_payload(run_id, "passed"))

    history = store.list_history("backend_unit", 5)
    assert len(history) == 1
    entry = history[0]
    for key in ("run_id", "timestamp", "summary", "status"):
        assert key in entry, f"History entry missing key: {key!r}"


@pytest.mark.unit
def test_list_history_invalid_category_returns_empty_list(store):
    """list_history for an unknown category must return an empty list (not raise)."""
    result = store.list_history("nonexistent_category", 10)
    assert result == []
