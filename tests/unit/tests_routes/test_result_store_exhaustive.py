"""Exhaustive parametrized unit tests for tests_routes/result_store.py.

Uses tmp_path fixture and monkeypatches RESULTS_ROOT / SUMMARY_PATH so tests
write to an isolated tmp dir.
"""

import json
import os
import re
import pytest

from tests_routes import result_store as rs
from tests_routes.categories import ALL_CATEGORIES, BACKEND_CATEGORIES, FRONTEND_CATEGORIES


@pytest.fixture(autouse=True)
def isolated_results(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "RESULTS_ROOT", str(tmp_path))
    monkeypatch.setattr(rs, "SUMMARY_PATH", str(tmp_path / "summary.json"))
    return tmp_path


def _payload(category, run_id, status="passed", **extras):
    p = {
        "category": category, "run_id": run_id, "status": status,
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:01:00+00:00",
        "duration_seconds": 60.0,
        "summary": {"total": 3, "passed": 3, "failed": 0, "skipped": 0, "errors": 0},
        "tests": [],
        "metrics": None,
    }
    p.update(extras)
    return p


# ── parametrized: write_category_result for every valid category ─────────────

@pytest.mark.unit
@pytest.mark.parametrize("category", list(ALL_CATEGORIES.keys()))
def test_write_for_every_category(category):
    rs.write_category_result(category, "run-1", _payload(category, "run-1"))
    assert os.path.exists(os.path.join(rs.RESULTS_ROOT, category, "latest.json"))

@pytest.mark.unit
@pytest.mark.parametrize("category", list(ALL_CATEGORIES.keys()))
def test_history_path_created_for_every_category(category):
    rs.write_category_result(category, "rid-001", _payload(category, "rid-001"))
    assert os.path.exists(os.path.join(rs.RESULTS_ROOT, category, "history", "rid-001.json"))

@pytest.mark.unit
@pytest.mark.parametrize("bad_category", [
    "", "unknown", "BACKEND_UNIT", "backend unit", "foo_bar",
])
def test_write_rejects_unknown_category(bad_category):
    with pytest.raises(ValueError):
        rs.write_category_result(bad_category, "run-1", _payload(bad_category, "run-1"))


# ── parametrized: round-trip (write then read) ───────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("category", list(ALL_CATEGORIES.keys()))
def test_write_then_read_roundtrip(category):
    p = _payload(category, "rid-x", status="failed")
    rs.write_category_result(category, "rid-x", p)
    got = rs.read_category_result(category)
    assert got is not None
    assert got["run_id"] == "rid-x"
    assert got["status"] == "failed"


# ── parametrized: read_category_result with no write returns None ─────────────

@pytest.mark.unit
@pytest.mark.parametrize("category", list(ALL_CATEGORIES.keys()))
def test_read_nothing_yet_returns_none(category):
    assert rs.read_category_result(category) is None

@pytest.mark.unit
@pytest.mark.parametrize("bad_category", ["", "no_such_cat", "BACKEND_UNIT"])
def test_read_invalid_category_returns_none(bad_category):
    assert rs.read_category_result(bad_category) is None


# ── parametrized: summary includes "never_run" for unrun categories ──────────

@pytest.mark.unit
@pytest.mark.parametrize("category", list(ALL_CATEGORIES.keys()))
def test_summary_marks_unrun_as_never_run(category):
    summary = rs.read_summary()
    assert summary["categories"][category]["status"] == "never_run"

@pytest.mark.unit
@pytest.mark.parametrize("category", list(ALL_CATEGORIES.keys()))
def test_summary_after_write_has_status(category):
    rs.write_category_result(category, "rid-1", _payload(category, "rid-1"))
    summary = rs.read_summary()
    assert summary["categories"][category]["status"] == "passed"


# ── parametrized: list_history limit ─────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("count,limit,expected", [
    (0, 25, 0),
    (1, 25, 1),
    (5, 25, 5),
    (25, 25, 25),
    (30, 25, 25),
    (5, 3, 3),
])
def test_list_history_limit(count, limit, expected):
    cat = "backend_unit"
    for i in range(count):
        rs.write_category_result(cat, f"rid-{i:03d}", _payload(cat, f"rid-{i:03d}"))
    hist = rs.list_history(cat, limit=limit)
    assert len(hist) == expected

@pytest.mark.unit
@pytest.mark.parametrize("bad_category", ["", "unknown", "BACKEND_UNIT"])
def test_list_history_invalid_category_returns_empty(bad_category):
    assert rs.list_history(bad_category) == []


# ── parametrized: new_run_id format ──────────────────────────────────────────

NEW_RUN_ID_PATTERN = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")

@pytest.mark.unit
@pytest.mark.parametrize("_n", range(20))
def test_new_run_id_format(_n):
    rid = rs.new_run_id()
    assert NEW_RUN_ID_PATTERN.match(rid), f"unexpected run_id format: {rid}"

@pytest.mark.unit
def test_new_run_id_uniqueness():
    ids = {rs.new_run_id() for _ in range(100)}
    assert len(ids) == 100


# ── parametrized: atomic_write_json doesn't leak temp files ──────────────────

@pytest.mark.unit
@pytest.mark.parametrize("data", [
    {}, {"a": 1}, {"nested": {"x": "y"}}, {"list": [1, 2, 3]},
    {"unicode": "café 中文"}, {"large": "x" * 1000},
])
def test_atomic_write_no_leftover_temp(isolated_results, data):
    target = str(isolated_results / "subdir" / "out.json")
    rs._atomic_write_json(target, data)
    assert os.path.exists(target)
    parent = os.path.dirname(target)
    # No .tmp_ files should remain after success
    leftovers = [f for f in os.listdir(parent) if f.startswith(".tmp_")]
    assert leftovers == []

@pytest.mark.unit
def test_atomic_write_overwrites(isolated_results):
    p = str(isolated_results / "ow.json")
    rs._atomic_write_json(p, {"v": 1})
    rs._atomic_write_json(p, {"v": 2})
    with open(p) as f:
        assert json.load(f)["v"] == 2


# ── parametrized: summary updated_at present after write ─────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("category", list(BACKEND_CATEGORIES.keys()))
def test_summary_has_updated_at(category):
    rs.write_category_result(category, "rid", _payload(category, "rid"))
    summary = rs.read_summary()
    assert summary.get("updated_at") is not None


# ── parametrized: history file content matches input payload ──────────────────

@pytest.mark.unit
@pytest.mark.parametrize("category", list(ALL_CATEGORIES.keys()))
def test_history_content_matches(category):
    p = _payload(category, "rid-x")
    rs.write_category_result(category, "rid-x", p)
    hist_file = os.path.join(rs.RESULTS_ROOT, category, "history", "rid-x.json")
    with open(hist_file) as f:
        got = json.load(f)
    assert got["run_id"] == "rid-x"
    assert got["summary"]["total"] == p["summary"]["total"]


# ── _summary_entry shape ──────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("status", ["passed", "failed", "error", "running"])
def test_summary_entry_carries_status(status):
    payload = _payload("backend_unit", "rid", status=status)
    e = rs._summary_entry(payload)
    assert e["status"] == status

@pytest.mark.unit
@pytest.mark.parametrize("k", ["total", "passed", "failed", "skipped", "errors"])
def test_summary_entry_carries_summary_keys(k):
    payload = _payload("backend_unit", "rid")
    e = rs._summary_entry(payload)
    assert k in e

@pytest.mark.unit
@pytest.mark.parametrize("metric_k", ["rps", "p50", "p95", "p99", "num_users", "failures"])
def test_summary_entry_carries_metric_keys(metric_k):
    payload = _payload("backend_load", "rid", metrics={metric_k: 42})
    e = rs._summary_entry(payload)
    assert e[metric_k] == 42
