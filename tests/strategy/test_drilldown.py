"""Unit tests for the pure drill-down path builder.

Locks the CISO root-cause contract: only FAILING refs become paths, each path
is ordered objective → program → project → doc → tracker, and doc metadata is
enriched from the (optional) doc_index.
"""

import pytest

from strategy.rollup import build_drilldown_paths

PROJECT = {
    "id": "proj1",
    "name": "MFA Rollout",
    "objective_id": "obj1",
    "objective_title": "Zero Trust",
    "program_id": "prog1",
    "program_name": "Identity",
}

REFS = [
    {"policy_id": "pol1", "doc_type": "policy", "tracker_id": "trk1",
     "tracker_abbrev": "AC", "row_id": "row14", "column_id": "colP",
     "statement_id": "stmtX", "status": "failed"},
    {"policy_id": "pol1", "doc_type": "policy", "tracker_id": "trk1",
     "tracker_abbrev": "AC", "row_id": "row2", "column_id": "colP",
     "statement_id": "stmtY", "status": "passed"},
    {"policy_id": "pol2", "doc_type": "standard", "tracker_id": "trk2",
     "tracker_abbrev": "ENC", "row_id": "row9", "column_id": "colS",
     "statement_id": "stmtZ", "status": "not_assessed"},
]


@pytest.mark.unit
def test_only_failing_refs_become_paths():
    paths = build_drilldown_paths(PROJECT, {}, REFS)
    assert len(paths) == 1
    assert paths[0]["statement_id"] == "stmtX"
    assert paths[0]["status"] == "failed"


@pytest.mark.unit
def test_path_carries_full_chain():
    p = build_drilldown_paths(PROJECT, {}, REFS)[0]
    assert p["objective_id"] == "obj1"
    assert p["objective_title"] == "Zero Trust"
    assert p["program_id"] == "prog1"
    assert p["program_name"] == "Identity"
    assert p["project_id"] == "proj1"
    assert p["project_name"] == "MFA Rollout"
    assert p["policy_id"] == "pol1"
    assert p["tracker_id"] == "trk1"
    assert p["tracker_abbrev"] == "AC"
    assert p["row_id"] == "row14"
    assert p["column_id"] == "colP"


@pytest.mark.unit
def test_doc_index_enriches_title_and_ref():
    doc_index = {"pol1": {"title": "Access Control Policy", "doc_ref": "ACC-001", "doc_type": "policy"}}
    p = build_drilldown_paths(PROJECT, doc_index, REFS)[0]
    assert p["doc_title"] == "Access Control Policy"
    assert p["doc_ref"] == "ACC-001"


@pytest.mark.unit
def test_no_failures_returns_empty():
    clean = [dict(r, status="passed") for r in REFS]
    assert build_drilldown_paths(PROJECT, {}, clean) == []
