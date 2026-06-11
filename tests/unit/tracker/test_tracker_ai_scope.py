"""Unit tests for tab_tracker.tab_ai_tracker scoped-edit helpers.

Regression coverage for the column-scope contract mismatch: the frontend
sends ``selected_column: {column_id, name, type, values_per_row}`` while the
backend helpers key off ``col_id``. Before the fix, every selected-column AI
edit merged its values under a ``None`` key, so the target column never
filled in the grid.
"""

import sys
import types
from unittest.mock import MagicMock

# ── Stub the heavy imports routes.py pulls in at module level ────────────────

for _mod in (
    "credits_route",
    "credits_route.route",
    "db",
    "db.rds_db",
    "db.lance_db_service",
    "playbook",
    "playbook.background_worker",
    "tab_tracker.helper",
    "utils.fireworkzz",
    "websockets_custom",
    "websockets_custom.ws_instance",
):
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

# permission_required_body decorates route handlers at import time, so it must
# be a real passthrough decorator (a MagicMock breaks Blueprint registration).
if "utils.permission_required" not in sys.modules:
    _perm_mod = types.ModuleType("utils.permission_required")

    def _passthrough(_permission):
        def _deco(fn):
            return fn

        return _deco

    _perm_mod.permission_required_body = _passthrough
    _perm_mod.permission_required = _passthrough
    sys.modules["utils.permission_required"] = _perm_mod

import tab_tracker.tab_ai_tracker.routes as tr  # noqa: E402


def _tracker_data():
    return {
        "type": "table",
        "schema": {
            "columns": [
                {"id": "col_1", "name": "Rule"},
                {"id": "col_7", "name": "Confirmation"},
            ]
        },
        "rows": [
            {"row_id": "r1", "values": {"col_1": "Admin port open", "col_7": ""}},
            {"row_id": "r2", "values": {"col_1": "No MFA", "col_7": ""}},
        ],
    }


# Shape the frontend actually sends (trackerAiService.ts) — column_id, not col_id.
def _frontend_selected_column():
    return {
        "column_id": "col_7",
        "name": "Confirmation",
        "type": "text",
        "values_per_row": {"r1": "", "r2": ""},
    }


class TestNormalizeSelectedColumn:
    def test_maps_column_id_to_col_id(self):
        col = tr._normalize_selected_column(_frontend_selected_column())
        assert col["col_id"] == "col_7"
        assert col["column_id"] == "col_7"
        assert col["name"] == "Confirmation"

    def test_keeps_existing_col_id(self):
        col = tr._normalize_selected_column({"col_id": "col_2", "column_id": "col_9"})
        assert col["col_id"] == "col_2"

    def test_passthrough_without_either_key(self):
        col = tr._normalize_selected_column({"name": "X"})
        assert col == {"name": "X"}


def _column_scope():
    return {
        "type": "selected_column",
        "selected_column": tr._normalize_selected_column(_frontend_selected_column()),
    }


class TestMergeSelectedColumn:
    def test_merge_writes_to_target_column(self):
        ai_result = [
            {"row_id": "r1", "new_value": "Confirmed"},
            {"row_id": "r2", "new_value": "Confirmed"},
        ]
        merged, applied = tr._merge_scoped_changes(
            _tracker_data(), _column_scope(), ai_result
        )
        by_id = {r["row_id"]: r for r in merged["rows"]}
        assert applied == 2
        assert by_id["r1"]["values"]["col_7"] == "Confirmed"
        assert by_id["r2"]["values"]["col_7"] == "Confirmed"
        # Nothing leaked under a None key (the pre-fix failure mode).
        assert None not in by_id["r1"]["values"]
        assert None not in by_id["r2"]["values"]

    def test_merge_accepts_value_key_fallback(self):
        # Models sometimes echo the input key "value" instead of "new_value";
        # previously that wrote None into every row → "No diff detected".
        ai_result = [{"row_id": "r1", "value": "Confirmed"}]
        merged, applied = tr._merge_scoped_changes(
            _tracker_data(), _column_scope(), ai_result
        )
        by_id = {r["row_id"]: r for r in merged["rows"]}
        assert applied == 1
        assert by_id["r1"]["values"]["col_7"] == "Confirmed"

    def test_merge_ignores_unknown_rows_and_reports_zero(self):
        merged, applied = tr._merge_scoped_changes(
            _tracker_data(), _column_scope(), [{"row_id": "ghost", "new_value": "x"}]
        )
        assert applied == 0
        for row in merged["rows"]:
            assert row["values"]["col_7"] == ""

    def test_merge_counts_zero_when_values_unchanged(self):
        # Model lazily echoing the current (empty) values is a no-op proposal.
        ai_result = [
            {"row_id": "r1", "new_value": ""},
            {"row_id": "r2", "new_value": ""},
        ]
        _, applied = tr._merge_scoped_changes(
            _tracker_data(), _column_scope(), ai_result
        )
        assert applied == 0

    def test_merge_skips_missing_values(self):
        merged, applied = tr._merge_scoped_changes(
            _tracker_data(), _column_scope(), [{"row_id": "r1"}]
        )
        assert applied == 0
        by_id = {r["row_id"]: r for r in merged["rows"]}
        assert by_id["r1"]["values"]["col_7"] == ""


class TestMergeSelectedColumns:
    def test_merge_applies_only_selected_columns(self):
        selected = [
            tr._normalize_selected_column(
                {"column_id": "col_7", "name": "Confirmation"}
            )
        ]
        scope = {"type": "selected_columns", "selected_columns": selected}
        ai_result = [
            # col_1 is not in the selected set — must not be overwritten.
            {"row_id": "r1", "values": {"col_7": "Confirmed", "col_1": "tampered"}},
        ]
        merged, applied = tr._merge_scoped_changes(_tracker_data(), scope, ai_result)
        by_id = {r["row_id"]: r for r in merged["rows"]}
        assert applied == 1
        assert by_id["r1"]["values"]["col_7"] == "Confirmed"
        assert by_id["r1"]["values"]["col_1"] == "Admin port open"


class TestScopeContextStr:
    def test_selected_column_uses_normalized_id(self):
        scope = {
            "type": "selected_column",
            "selected_column": tr._normalize_selected_column(
                _frontend_selected_column()
            ),
        }
        ctx = tr._build_scope_context_str(scope, _tracker_data())
        assert "col_7" in ctx
        assert "None" not in ctx

    def test_selected_row_reads_values_key(self):
        # Frontend sends {row_id, values}; the prompt context must include them.
        scope = {
            "type": "selected_row",
            "selected_row": {"row_id": "r1", "values": {"col_1": "Admin port open"}},
        }
        ctx = tr._build_scope_context_str(scope, _tracker_data())
        assert "Admin port open" in ctx
