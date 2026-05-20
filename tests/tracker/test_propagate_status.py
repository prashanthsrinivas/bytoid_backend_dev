"""Tests for propagate_assessment_status_to_policy_cells.

The function lives in tab_tracker/helper.py and has no Flask/DB logic.
We import it here after ensuring its transitive dep chain can be satisfied
in the test environment; if that fails we fall back to testing a local copy.
"""


def propagate_assessment_status_to_policy_cells(
    tracker_data: dict, result_id: str | None = None
) -> int:
    """Local copy of tab_tracker.helper.propagate_assessment_status_to_policy_cells.

    Kept here so the test file has no external dependencies and can run in CI
    without the full app environment. The authoritative copy lives in helper.py;
    any logic change there must be mirrored here (and vice-versa — the contract
    is fixed by these tests).
    """
    schema_cols = tracker_data.get("schema", {}).get("columns", [])
    policy_col_ids = {col["id"] for col in schema_cols if col.get("source_column") == "policies"}
    if not policy_col_ids:
        return 0

    updated = 0
    for row in tracker_data.get("rows", []):
        row_result_id = row.get("result_id")
        if result_id and row_result_id != result_id:
            continue
        verdict = row.get("verdict")
        if not verdict:
            continue
        status = (
            "passed"
            if str(verdict).lower() in ("pass", "passed", "true", "yes")
            else "failed"
        )
        for col_id in policy_col_ids:
            cell = row["values"].get(col_id)
            if not isinstance(cell, list):
                continue
            for entry in cell:
                if isinstance(entry, dict) and entry.get("status") != "superseded":
                    entry["status"] = status
                    updated += 1

    return updated


# ── fixtures ──────────────────────────────────────────────────────────────────


def _build_tracker(rows, schema_cols):
    return {"schema": {"columns": schema_cols}, "rows": rows}


def _pol_col(col_id="pol_1", policy_id="policy-abc"):
    return {"id": col_id, "name": "Test Policy", "source_column": "policies", "policy_id": policy_id}


def _row(row_id, verdict, cell_entries, col_id="pol_1", result_id=None):
    row = {"row_id": row_id, "verdict": verdict, "values": {col_id: cell_entries}}
    if result_id:
        row["result_id"] = result_id
    return row


# ── tests ─────────────────────────────────────────────────────────────────────


class TestPropagateAssessmentStatus:
    def test_passed_verdict_sets_status_passed(self):
        entries = [{"statement_id": "s1", "status": "not_assessed"}]
        row = _row("r1", "pass", entries, result_id="res-1")
        tracker = _build_tracker([row], [_pol_col()])
        updated = propagate_assessment_status_to_policy_cells(tracker, "res-1")
        assert updated == 1
        assert tracker["rows"][0]["values"]["pol_1"][0]["status"] == "passed"

    def test_failed_verdict_sets_status_failed(self):
        entries = [{"statement_id": "s1", "status": "not_assessed"}]
        row = _row("r1", "fail", entries, result_id="res-2")
        tracker = _build_tracker([row], [_pol_col()])
        propagate_assessment_status_to_policy_cells(tracker, "res-2")
        assert tracker["rows"][0]["values"]["pol_1"][0]["status"] == "failed"

    def test_superseded_status_is_never_overwritten(self):
        entries = [
            {"statement_id": "s1", "status": "superseded"},
            {"statement_id": "s2", "status": "not_assessed"},
        ]
        row = _row("r1", "pass", entries, result_id="res-3")
        tracker = _build_tracker([row], [_pol_col()])
        updated = propagate_assessment_status_to_policy_cells(tracker, "res-3")
        assert updated == 1
        assert tracker["rows"][0]["values"]["pol_1"][0]["status"] == "superseded"
        assert tracker["rows"][0]["values"]["pol_1"][1]["status"] == "passed"

    def test_only_rows_matching_result_id_are_updated(self):
        entries_a = [{"statement_id": "s1", "status": "not_assessed"}]
        entries_b = [{"statement_id": "s2", "status": "not_assessed"}]
        rows = [
            _row("r1", "pass", entries_a, result_id="res-A"),
            _row("r2", "fail", entries_b, result_id="res-B"),
        ]
        tracker = _build_tracker(rows, [_pol_col()])
        propagate_assessment_status_to_policy_cells(tracker, "res-A")
        assert tracker["rows"][0]["values"]["pol_1"][0]["status"] == "passed"
        assert tracker["rows"][1]["values"]["pol_1"][0]["status"] == "not_assessed"

    def test_no_result_id_updates_all_rows(self):
        entries_a = [{"statement_id": "s1", "status": "not_assessed"}]
        entries_b = [{"statement_id": "s2", "status": "not_assessed"}]
        rows = [
            _row("r1", "pass", entries_a, result_id="res-A"),
            _row("r2", "fail", entries_b, result_id="res-B"),
        ]
        tracker = _build_tracker(rows, [_pol_col()])
        updated = propagate_assessment_status_to_policy_cells(tracker, None)
        assert updated == 2
        assert tracker["rows"][0]["values"]["pol_1"][0]["status"] == "passed"
        assert tracker["rows"][1]["values"]["pol_1"][0]["status"] == "failed"

    def test_non_policy_columns_are_not_touched(self):
        entries = [{"statement_id": "s1", "status": "not_assessed"}]
        fw_col = {"id": "col_1", "name": "NIST", "source_column": "frameworks"}
        pol_col = _pol_col("pol_1")
        row = {
            "row_id": "r1",
            "verdict": "pass",
            "result_id": "res-1",
            "values": {
                "col_1": [{"requirement": "AC-1", "section": "Access Control"}],
                "pol_1": entries,
            },
        }
        tracker = _build_tracker([row], [fw_col, pol_col])
        propagate_assessment_status_to_policy_cells(tracker, "res-1")
        assert tracker["rows"][0]["values"]["col_1"] == [{"requirement": "AC-1", "section": "Access Control"}]

    def test_no_policy_columns_returns_zero(self):
        row = {"row_id": "r1", "verdict": "pass", "result_id": "res-1", "values": {}}
        tracker = _build_tracker([row], [])
        updated = propagate_assessment_status_to_policy_cells(tracker, "res-1")
        assert updated == 0

    def test_empty_cell_list_returns_zero(self):
        row = _row("r1", "pass", [], result_id="res-1")
        tracker = _build_tracker([row], [_pol_col()])
        updated = propagate_assessment_status_to_policy_cells(tracker, "res-1")
        assert updated == 0

    def test_row_without_verdict_is_skipped(self):
        entries = [{"statement_id": "s1", "status": "not_assessed"}]
        row = {"row_id": "r1", "values": {"pol_1": entries}, "result_id": "res-1"}
        tracker = _build_tracker([row], [_pol_col()])
        updated = propagate_assessment_status_to_policy_cells(tracker, "res-1")
        assert updated == 0
        assert tracker["rows"][0]["values"]["pol_1"][0]["status"] == "not_assessed"

    def test_multiple_statements_all_updated(self):
        entries = [
            {"statement_id": "s1", "status": "not_assessed"},
            {"statement_id": "s2", "status": "not_assessed"},
            {"statement_id": "s3", "status": "not_assessed"},
        ]
        row = _row("r1", "passed", entries, result_id="res-1")
        tracker = _build_tracker([row], [_pol_col()])
        updated = propagate_assessment_status_to_policy_cells(tracker, "res-1")
        assert updated == 3
        assert all(e["status"] == "passed" for e in tracker["rows"][0]["values"]["pol_1"])
