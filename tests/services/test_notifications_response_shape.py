"""Regression: GET /notifications/<user_id> MUST return the workflow context
columns (doc_type, doc_id, workflow_id, workflow_state, action_required) so
the reviewer's bell can render the workflow task. Previously the SELECT only
returned (id, message, is_read, created_at) and the bell stayed empty even
though notifications were being written.

Verified by source inspection — exercising the full route would require an
http harness and a live DB.
"""

import ast
import pathlib


ROUTES_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "invited_users" / "routes.py"
)


def _get_function_source(name: str) -> str:
    source = ROUTES_PATH.read_text()
    module = ast.parse(source)
    for node in ast.walk(module):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"function {name!r} not found in {ROUTES_PATH}")


REQUIRED_COLUMNS = (
    "doc_type",
    "doc_id",
    "workflow_id",
    "workflow_state",
    "action_required",
)


def test_get_notifications_select_includes_workflow_columns():
    src = _get_function_source("get_notifications")
    # Strip whitespace for robust matching against multi-line SQL formatting.
    flat = " ".join(src.split())
    for col in REQUIRED_COLUMNS:
        assert col in flat, (
            f"GET /notifications must SELECT {col!r} so the bell can render "
            f"workflow tasks; query did not contain it. Source:\n{src}"
        )


def test_get_notifications_normalizes_action_required_to_bool():
    """MySQL returns TINYINT(1) as int; the bell filter `n.action_required === true`
    won't match unless we coerce to bool."""
    src = _get_function_source("get_notifications")
    assert "bool(row[\"action_required\"])" in src or "bool(row['action_required'])" in src, (
        "action_required must be coerced to bool before JSON serialization"
    )
