"""Tenant isolation tests. Verifies that result store isolation prevents data leakage between categories and run IDs."""

import sys
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Stub heavy transitive dependencies at module level
# ---------------------------------------------------------------------------

_HEAVY_MODS = [
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
    "utils.s3_utils",
    "utils.celery_base",
    "utils.base_logger",
]
for _mod in _HEAVY_MODS:
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

import tests_routes.result_store as rs  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolate_store(tmp_path):
    """Redirect RESULTS_ROOT and SUMMARY_PATH to a temp directory for every test."""
    orig_root, orig_summary = rs.RESULTS_ROOT, rs.SUMMARY_PATH
    rs.RESULTS_ROOT = str(tmp_path)
    rs.SUMMARY_PATH = str(tmp_path / "summary.json")
    yield
    rs.RESULTS_ROOT = orig_root
    rs.SUMMARY_PATH = orig_summary


def _make_payload(category: str, run_id: str, status: str = "passed") -> dict:
    return {
        "category": category,
        "run_id": run_id,
        "status": status,
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:01:00+00:00",
        "summary": {"total": 1, "passed": 1, "failed": 0},
        "tests": [],
        "metrics": None,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.security
@pytest.mark.authz
def test_write_category_a_does_not_appear_in_category_b():
    """Writing backend_unit result must not surface when reading backend_integration."""
    payload = _make_payload("backend_unit", "run-001")
    rs.write_category_result("backend_unit", "run-001", payload)

    result = rs.read_category_result("backend_integration")
    assert result is None


@pytest.mark.security
@pytest.mark.authz
def test_category_a_history_does_not_include_category_b_runs():
    """Runs written for backend_unit must not appear in backend_integration history."""
    for i in range(3):
        run_id = f"2026010{i}T000000Z-aabb{i:04d}"
        rs.write_category_result("backend_unit", run_id, _make_payload("backend_unit", run_id))

    history = rs.list_history("backend_integration")
    assert history == []


@pytest.mark.security
@pytest.mark.authz
def test_run_id_uniqueness_across_categories():
    """Writing the same run_id to two categories must keep both results independently."""
    shared_run_id = "2026010{i}T000000Z-shared0001"
    payload_unit = _make_payload("backend_unit", shared_run_id, status="passed")
    payload_intg = _make_payload("backend_integration", shared_run_id, status="failed")

    rs.write_category_result("backend_unit", shared_run_id, payload_unit)
    rs.write_category_result("backend_integration", shared_run_id, payload_intg)

    result_unit = rs.read_category_result("backend_unit")
    result_intg = rs.read_category_result("backend_integration")

    assert result_unit is not None
    assert result_intg is not None
    assert result_unit["status"] == "passed"
    assert result_intg["status"] == "failed"


@pytest.mark.security
@pytest.mark.authz
def test_summary_isolates_per_category():
    """Summary must store each category's status independently without cross-contamination."""
    rs.write_category_result("backend_unit", "run-unit-001",
                             _make_payload("backend_unit", "run-unit-001", status="passed"))
    rs.write_category_result("backend_integration", "run-intg-001",
                             _make_payload("backend_integration", "run-intg-001", status="failed"))

    summary = rs.read_summary()
    cats = summary["categories"]

    assert cats["backend_unit"]["status"] == "passed"
    assert cats["backend_integration"]["status"] == "failed"


@pytest.mark.security
@pytest.mark.authz
def test_unknown_category_write_raises_not_leaks(tmp_path):
    """Writing to an unknown category must raise ValueError without creating any files."""
    with pytest.raises(ValueError, match="Unknown category"):
        rs.write_category_result("attacker_category", "run-x", {"status": "passed"})

    # The injected RESULTS_ROOT (tmp_path) should contain no files from the failed write
    all_files = list(tmp_path.rglob("*"))
    assert all_files == [], f"Expected no files after failed write, found: {all_files}"


@pytest.mark.security
@pytest.mark.authz
def test_category_result_returns_none_not_another_result():
    """Reading backend_regression after writing only backend_unit must return None."""
    rs.write_category_result("backend_unit", "run-001",
                             _make_payload("backend_unit", "run-001"))

    result = rs.read_category_result("backend_regression")
    assert result is None


@pytest.mark.security
@pytest.mark.authz
def test_overwrite_latest_does_not_corrupt_history():
    """Writing backend_unit twice keeps both runs in history; latest reflects the second."""
    run_id_1 = "20260101T000000Z-aaaa0001"
    run_id_2 = "20260101T000001Z-bbbb0002"

    payload_1 = _make_payload("backend_unit", run_id_1, status="passed")
    payload_2 = _make_payload("backend_unit", run_id_2, status="failed")

    rs.write_category_result("backend_unit", run_id_1, payload_1)
    rs.write_category_result("backend_unit", run_id_2, payload_2)

    history = rs.list_history("backend_unit")
    run_ids_in_history = {entry["run_id"] for entry in history}
    assert run_id_1 in run_ids_in_history
    assert run_id_2 in run_ids_in_history

    # latest.json must reflect the second (most recent) write
    latest = rs.read_category_result("backend_unit")
    assert latest is not None
    assert latest["run_id"] == run_id_2
    assert latest["status"] == "failed"


@pytest.mark.security
@pytest.mark.authz
def test_path_traversal_in_category_name_raises():
    """Writing to '../../../etc' must raise ValueError — unknown category — before filesystem access."""
    with pytest.raises(ValueError, match="Unknown category"):
        rs.write_category_result("../../../etc", "run-x", {"status": "passed"})
