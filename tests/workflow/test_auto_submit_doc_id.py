"""Regression: _auto_submit_runbook_workflow MUST key the workflow by
result_id, not runbook_id. Mismatching here was the original cause of the
"Submit succeeded but UI never updates" bug — the manual submit popover
registered the workflow under result_id while auto-submit registered it
under runbook_id, so the frontend's by-doc lookup either found the wrong
row or none at all.

We verify the contract by source inspection — runbook.helper has very deep
transitive imports (LanceDB, Fireworks, S3, Celery, etc.) that aren't
worth stubbing for a small set of assertions.
"""

import ast
import inspect
import pathlib


HELPER_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "runbook" / "helper.py"
)


def _get_function_source(name: str) -> str:
    source = HELPER_PATH.read_text()
    module = ast.parse(source)
    for node in ast.walk(module):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"function {name!r} not found in {HELPER_PATH}")


def _find_keyword(call_source: str, fn_name: str, kw_name: str) -> str:
    """Return the source text of the keyword argument passed to fn_name."""
    tree = ast.parse(call_source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == fn_name:
            for kw in node.keywords:
                if kw.arg == kw_name:
                    return ast.unparse(kw.value)
    raise AssertionError(f"call to {fn_name}(... {kw_name}=...) not found")


def test_auto_submit_signature_has_result_id_param():
    """The function must accept a result_id parameter."""
    src = _get_function_source("_auto_submit_runbook_workflow")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    arg_names = [a.arg for a in fn.args.args]
    assert "result_id" in arg_names, (
        f"_auto_submit_runbook_workflow must accept result_id; got {arg_names}"
    )


def test_create_workflow_uses_result_id_as_doc_id():
    """The create_workflow call inside _auto_submit_runbook_workflow MUST
    pass doc_id=result_id (not runbook_id)."""
    src = _get_function_source("_auto_submit_runbook_workflow")
    doc_id_expr = _find_keyword(src, "create_workflow", "doc_id")
    assert doc_id_expr == "result_id", (
        f"create_workflow doc_id must be result_id; got {doc_id_expr!r}"
    )


def test_get_workflow_for_doc_uses_result_id():
    """The pre-existing-workflow lookup must also key by result_id."""
    src = _get_function_source("_auto_submit_runbook_workflow")
    # The call is positional: get_workflow_for_doc("runbook", result_id, doc_version)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "get_workflow_for_doc"
        ):
            assert len(node.args) >= 2, "get_workflow_for_doc must be called positionally"
            assert ast.unparse(node.args[1]) == "result_id", (
                f"get_workflow_for_doc second arg must be result_id; "
                f"got {ast.unparse(node.args[1])!r}"
            )
            return
    raise AssertionError("get_workflow_for_doc call not found in _auto_submit_runbook_workflow")


def test_auto_submit_call_site_passes_new_result_id():
    """The single call site (run_runbook_job inner generator) must pass
    new_result_id, not just runbook_id."""
    source = HELPER_PATH.read_text()
    tree = ast.parse(source)
    callers = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "_auto_submit_runbook_workflow"
    ]
    assert callers, "_auto_submit_runbook_workflow is never called"
    for call in callers:
        passed_kw = {kw.arg: ast.unparse(kw.value) for kw in call.keywords}
        assert passed_kw.get("result_id") == "new_result_id", (
            f"call site must pass result_id=new_result_id; got {passed_kw!r}"
        )
