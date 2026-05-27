"""Unit tests for tab_tracker.refs_sync.extract_policy_cell_refs and
tab_tracker.reconcile.reconcile_user (with injected loaders/syncer)."""

import sys
from unittest.mock import MagicMock

import pytest

for _mod in ("pymysql", "pymysql.cursors", "db", "db.rds_db"):
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

from tab_tracker.refs_sync import extract_policy_cell_refs  # noqa: E402
from tab_tracker.reconcile import reconcile_user  # noqa: E402


def _tracker_with_policy_cells():
    return {
        "schema": {
            "columns": [
                {"id": "col_1", "name": "Control", "source_column": "Control"},
                {"id": "col_p", "source_column": "policies", "policy_id": "pol1",
                 "doc_type": "policy"},
                {"id": "col_s", "source_column": "policies", "policy_id": "std1",
                 "doc_type": "standard"},
            ]
        },
        "rows": [
            {"row_id": "r1", "values": {
                "col_1": "text",
                "col_p": [{"statement_id": "s1", "status": "passed"},
                          {"statement_id": "s2", "status": "not_assessed"}],
                "col_s": [{"statement_id": "rq1", "status": "passed"}],
            }},
            {"row_id": "r2", "values": {
                "col_1": "text2",
                "col_p": [],  # empty cell
            }},
        ],
    }


@pytest.mark.unit
class TestExtractPolicyCellRefs:
    def test_extracts_per_cell(self):
        refs = extract_policy_cell_refs(_tracker_with_policy_cells())
        # 2 policy columns x 2 rows = 4 cells
        assert len(refs) == 4

    def test_statements_carried_with_status(self):
        refs = extract_policy_cell_refs(_tracker_with_policy_cells())
        r1_polp = next(r for r in refs if r["row_id"] == "r1" and r["column_id"] == "col_p")
        assert {s["statement_id"] for s in r1_polp["statements"]} == {"s1", "s2"}
        assert r1_polp["policy_id"] == "pol1"
        assert r1_polp["doc_type"] == "policy"

    def test_standard_doc_type_preserved(self):
        refs = extract_policy_cell_refs(_tracker_with_policy_cells())
        std_cell = next(r for r in refs if r["column_id"] == "col_s" and r["row_id"] == "r1")
        assert std_cell["doc_type"] == "standard"
        assert std_cell["statements"][0]["statement_id"] == "rq1"

    def test_empty_cell_yields_empty_statements(self):
        refs = extract_policy_cell_refs(_tracker_with_policy_cells())
        empty = next(r for r in refs if r["row_id"] == "r2" and r["column_id"] == "col_p")
        assert empty["statements"] == []

    def test_ignores_non_policy_columns(self):
        refs = extract_policy_cell_refs(_tracker_with_policy_cells())
        assert all(r["column_id"] in ("col_p", "col_s") for r in refs)

    def test_empty_tracker(self):
        assert extract_policy_cell_refs({}) == []
        assert extract_policy_cell_refs({"schema": {"columns": []}, "rows": []}) == []

    def test_skips_columns_without_policy_id(self):
        data = {"schema": {"columns": [{"id": "c", "source_column": "policies"}]},
                "rows": [{"row_id": "r", "values": {"c": [{"statement_id": "x"}]}}]}
        assert extract_policy_cell_refs(data) == []


@pytest.mark.unit
class TestReconcileUser:
    def test_syncs_each_tracker(self):
        config = {"trackers": [
            {"tracker_id": "trk1", "tracker_abbrev": "RSK-0001"},
            {"tracker_id": "trk2", "tracker_abbrev": "CHG-0001"},
        ]}
        loaded = {"trk1": _tracker_with_policy_cells(), "trk2": _tracker_with_policy_cells()}
        synced_calls = []

        def fake_syncer(tracker_id, abbrev, tracker_data):
            synced_calls.append((tracker_id, abbrev))
            return len(extract_policy_cell_refs(tracker_data))

        summary = reconcile_user(
            "u1",
            config_loader=lambda uid: config,
            tracker_loader=lambda uid, tid: loaded.get(tid),
            syncer=fake_syncer,
        )
        assert summary["trackers"] == 2
        assert summary["cells_synced"] == 8  # 4 cells each
        assert summary["errors"] == 0
        assert {c[0] for c in synced_calls} == {"trk1", "trk2"}

    def test_no_config_is_noop(self):
        summary = reconcile_user("u1", config_loader=lambda uid: None,
                                 tracker_loader=lambda uid, tid: None,
                                 syncer=lambda *a: 0)
        assert summary == {"trackers": 0, "cells_synced": 0, "errors": 0}

    def test_missing_tracker_blob_skipped(self):
        config = {"trackers": [{"tracker_id": "trk1"}]}
        summary = reconcile_user("u1", config_loader=lambda uid: config,
                                 tracker_loader=lambda uid, tid: None,
                                 syncer=lambda *a: 99)
        assert summary["trackers"] == 1
        assert summary["cells_synced"] == 0

    def test_syncer_error_counted(self):
        config = {"trackers": [{"tracker_id": "trk1"}]}

        def boom(*a):
            raise RuntimeError("db down")

        summary = reconcile_user("u1", config_loader=lambda uid: config,
                                 tracker_loader=lambda uid, tid: {"schema": {}, "rows": []},
                                 syncer=boom)
        assert summary["errors"] == 1
