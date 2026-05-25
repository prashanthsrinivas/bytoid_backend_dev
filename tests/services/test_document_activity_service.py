"""Unit tests for services.document_activity_service.

Covers the pure helpers (_truncate, _get_path, _section_paths, _to_text) and
the high-level emit_field_diff_events behaviour with a mocked DB layer.

DB stubs are installed by tests/conftest_db_stubs.py (autouse fixture that
restores ``sys.modules`` on teardown).
"""

import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def svc(db_stubs):
    sys.modules.pop("services.document_activity_service", None)
    from services import document_activity_service as mod
    return mod


# ── Pure helpers ──────────────────────────────────────────────────────────────


def test_truncate_short_value_unchanged(svc):
    assert svc._truncate("hello", limit=10) == "hello"


def test_truncate_long_value_clipped_with_ellipsis(svc):
    out = svc._truncate("x" * 600, limit=500)
    assert len(out) == 500
    assert out.endswith("…")


def test_to_text_none_is_empty_string(svc):
    assert svc._to_text(None) == ""


def test_to_text_dict_is_stable_json(svc):
    """Two equal dicts must produce identical text (sort_keys=True)."""
    a = svc._to_text({"b": 1, "a": 2})
    b = svc._to_text({"a": 2, "b": 1})
    assert a == b


def test_get_path_nested_dict(svc):
    doc = {"sections": {"privacy": {"body": "abc"}}}
    assert svc._get_path(doc, "sections.privacy") == {"body": "abc"}


def test_get_path_missing_returns_none(svc):
    assert svc._get_path({"a": 1}, "b.c") is None


def test_get_path_sections_as_list_of_objects(svc):
    """Sections sometimes come through as [{id: "...", ...}, …] — we want to
    address them by id with the same dotted path syntax."""
    doc = {"sections": [{"id": "privacy", "body": "x"}, {"id": "scope", "body": "y"}]}
    assert svc._get_path(doc, "sections.privacy") == {"id": "privacy", "body": "x"}


def test_section_paths_includes_top_level_keys(svc):
    paths = list(svc._section_paths({}, {"sections": {"a": 1, "b": 2}}))
    assert "title" in paths
    assert "risk_score" in paths
    assert "sections.a" in paths
    assert "sections.b" in paths


# ── emit_field_diff_events ────────────────────────────────────────────────────


def test_no_diff_when_documents_identical(svc):
    """When before == after, zero rows should be inserted."""
    doc = {"title": "T", "sections": {"a": "body"}}
    with patch.object(svc, "connect_to_rds") as mock_conn:
        count = svc.emit_field_diff_events(
            doc_type="runbook",
            doc_id="rb-1",
            previous_result_id="r1",
            new_result_id="r2",
            actor_user_id="u1",
            before=doc,
            after=doc,
            workflow_id="wf-1",
        )
    assert count == 0
    # No DB connection should be opened when there's nothing to write.
    mock_conn.assert_not_called()


def test_diff_emits_one_row_per_changed_section(svc):
    before = {"title": "Old", "sections": {"a": "v1", "b": "v1"}}
    after = {"title": "New", "sections": {"a": "v2", "b": "v1"}}

    fake_cursor = MagicMock()
    fake_conn = MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = fake_cursor

    with patch.object(svc, "connect_to_rds", return_value=fake_conn):
        count = svc.emit_field_diff_events(
            doc_type="runbook",
            doc_id="rb-1",
            previous_result_id="r1",
            new_result_id="r2",
            actor_user_id="u1",
            before=before,
            after=after,
            workflow_id="wf-1",
        )
    # title and sections.a changed; sections.b did not.
    assert count == 2
    args, _ = fake_cursor.executemany.call_args
    inserted_rows = args[1]
    paths = sorted(row[7] for row in inserted_rows)
    assert paths == ["sections.a", "title"]


def test_diff_truncates_long_snippets(svc):
    before = {"title": "x" * 1000}
    after = {"title": "y" * 1000}

    fake_cursor = MagicMock()
    fake_conn = MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = fake_cursor

    with patch.object(svc, "connect_to_rds", return_value=fake_conn):
        svc.emit_field_diff_events(
            doc_type="runbook",
            doc_id="rb-1",
            previous_result_id="r1",
            new_result_id="r2",
            actor_user_id="u1",
            before=before,
            after=after,
            workflow_id="wf-1",
        )
    args, _ = fake_cursor.executemany.call_args
    row = args[1][0]
    before_snippet, after_snippet = row[8], row[9]
    assert len(before_snippet) <= svc.SNIPPET_MAX_CHARS
    assert len(after_snippet) <= svc.SNIPPET_MAX_CHARS


def test_diff_records_delta_chars(svc):
    """delta_chars = len(after) - len(before) — signed."""
    before = {"title": "abc"}
    after = {"title": "abcdef"}

    fake_cursor = MagicMock()
    fake_conn = MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = fake_cursor

    with patch.object(svc, "connect_to_rds", return_value=fake_conn):
        svc.emit_field_diff_events(
            doc_type="runbook",
            doc_id="rb-1",
            previous_result_id="r1",
            new_result_id="r2",
            actor_user_id="u1",
            before=before,
            after=after,
            workflow_id="wf-1",
        )
    row = fake_cursor.executemany.call_args[0][1][0]
    assert row[10] == 3  # "abcdef" (6) - "abc" (3)


def test_emit_swallows_db_errors(svc):
    """Diff capture must never crash the save path."""
    fake_conn = MagicMock()
    fake_conn.cursor.side_effect = Exception("db down")

    with patch.object(svc, "connect_to_rds", return_value=fake_conn):
        count = svc.emit_field_diff_events(
            doc_type="runbook",
            doc_id="rb-1",
            previous_result_id="r1",
            new_result_id="r2",
            actor_user_id="u1",
            before={"title": "a"},
            after={"title": "b"},
            workflow_id="wf-1",
        )
    assert count == 0  # returns 0 instead of raising
