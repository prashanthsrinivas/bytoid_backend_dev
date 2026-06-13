"""Unit + regression tests for the pure strategy health-rollup math.

These import only ``strategy.rollup`` (no DB / AWS), so they run with zero
external dependencies. They lock the rollup semantics the CISO drill-down
depends on: only passed/failed count; superseded and the legacy 'active'
default are excluded; an objective aggregates programs AND direct projects.
"""

import pytest

from strategy.rollup import (
    aggregate_rollups,
    classify_health,
    normalize_status,
    rollup_status,
)


@pytest.mark.unit
def test_normalize_status_maps_known_and_unknown():
    assert normalize_status("passed") == "passed"
    assert normalize_status("FAILED") == "failed"
    assert normalize_status("not_assessed") == "not_assessed"
    assert normalize_status("superseded") == "not_assessed"
    assert normalize_status("active") == "not_assessed"   # legacy default
    assert normalize_status(None) == "not_assessed"
    assert normalize_status("anything-else") == "not_assessed"


@pytest.mark.unit
def test_all_passed_is_healthy_score_one():
    r = rollup_status([{"status": "passed"}, {"status": "passed"}])
    assert r == {
        "passed": 2, "failed": 0, "not_assessed": 0,
        "total": 2, "score": 1.0, "health": "healthy",
    }


@pytest.mark.unit
def test_empty_is_not_assessed_with_null_score():
    r = rollup_status([])
    assert r["score"] is None
    assert r["health"] == "not_assessed"
    assert r["total"] == 0


@pytest.mark.unit
@pytest.mark.regression
def test_superseded_and_active_excluded_from_denominator():
    refs = [
        {"status": "passed"},
        {"status": "failed"},
        {"status": "superseded"},
        {"status": "active"},
        {"status": "not_assessed"},
    ]
    r = rollup_status(refs)
    # Only passed+failed are assessed → 1/2 = 0.5.
    assert r["passed"] == 1
    assert r["failed"] == 1
    assert r["not_assessed"] == 3
    assert r["score"] == 0.5
    assert r["health"] == "at_risk"  # has a failure but >= 50% passing


@pytest.mark.unit
def test_majority_failing_is_failing():
    r = rollup_status([{"status": "failed"}, {"status": "failed"}, {"status": "passed"}])
    assert r["score"] == pytest.approx(1 / 3, abs=1e-4)
    assert r["health"] == "failing"


@pytest.mark.unit
def test_accepts_raw_status_strings():
    r = rollup_status(["passed", "failed"])
    assert r["passed"] == 1 and r["failed"] == 1


@pytest.mark.unit
def test_classify_health_thresholds():
    assert classify_health(0, None) == "not_assessed"
    assert classify_health(0, 1.0) == "healthy"
    assert classify_health(1, 0.9) == "at_risk"
    assert classify_health(3, 0.2) == "failing"


@pytest.mark.unit
@pytest.mark.regression
def test_aggregate_combines_programs_and_direct_projects():
    program_a = rollup_status([{"status": "passed"}, {"status": "failed"}])
    program_b = rollup_status([{"status": "passed"}])
    direct_project = rollup_status([{"status": "failed"}, {"status": "failed"}])

    obj = aggregate_rollups([program_a, program_b, direct_project])
    assert obj["passed"] == 2
    assert obj["failed"] == 3
    assert obj["total"] == 5
    assert obj["score"] == pytest.approx(2 / 5, abs=1e-4)
    assert obj["health"] == "failing"


@pytest.mark.unit
def test_aggregate_ignores_none_children():
    a = rollup_status([{"status": "passed"}])
    assert aggregate_rollups([a, None]) == a
