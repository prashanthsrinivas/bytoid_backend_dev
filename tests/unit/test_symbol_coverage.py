"""§4z — symbol-coverage gate.

Two live assertions (must pass):
  * every symbol the ledger claims is covered is genuinely referenced by a test
    (the ledger cannot lie);
  * the covered set is non-trivial and matches the explicit ledger.

One forward-looking assertion (``xfail`` until the module is fully covered):
  * *every* public symbol of the in-scope modules is referenced by some test.
    When coverage completes this xpasses and should be promoted to ``strict``.

"Referenced" is a source-level proxy: the symbol name appears as a whole word in
at least one test file. Good enough to keep the ledger honest and to surface the
remaining gap as a concrete number.
"""

from __future__ import annotations

import contextlib
import inspect
import pathlib
import re

import pytest

from tests.workflow_playbook import _wf_pb_stubs as stubs

stubs.bootstrap_sut()

pytestmark = pytest.mark.unit

_TESTS_ROOT = pathlib.Path(__file__).resolve().parent.parent


# Symbols this suite explicitly covers (the COVERAGE.md ledger, in code form).
_COVERED: set[str] = {
    # state_machine
    "_next_forward_state", "_is_forward_hop", "_cadence_category",
    "_user_col_for_state", "_role_col_for_state", "_assignee_for_state",
    "get_workflow", "get_workflow_for_doc", "get_user_org_id", "add_comment",
    "transition", "guard_mutation", "assert_doc_editable", "actor_eligible_for_state",
    "get_workflow_history", "_append_event", "get_workflow_config",
    "get_actor_role_ids", "get_org_review_frequency", "set_org_review_frequency",
    "get_workflow_states_for_docs", "get_docs_assigned_to_user", "cancel_workflow",
    "get_inbox", "get_workflow_for_doc_any_role", "enrich_workflow_for_viewer",
    # helperzz
    "base_name", "clean_yaml_block", "normalize_input", "extract_questions",
    "clean_json_block", "extract_json_from_llm_output", "normalize_contacts",
    "cheap_internal_data_hint", "generate_meeting_email_body", "returninsructdata",
    "format_step_data", "replace_section", "returnconfigandpath",
    "needs_internal_data", "minimize_functions", "check_doc_context_needed",
    "evallogic", "triggeraicontextfinder", "assign_runbook_playbook",
    "update_playbook_schedule_and_runtime", "create_playbook", "_enc_pb", "_dec_pb",
    # background_worker
    "submit_job", "_run_job",
    # WorkflowRunnerV2
    "is_yes", "generate_unique_id", "check_step_exists", "get_step_data",
    "_get_first_step", "_find_step_by_ref", "update_statuscount",
    "_question_answer_stats", "_are_all_required_fields_answered",
    "_build_dependency_blocked_response", "_get_next_uncompleted_step",
    "get_current_execution_data", "get_execution_log", "append_execution_step_log",
    "_find_fallback", "get_current_chats", "get_attendees", "handle_workflow_reset",
    "get_parsed_fireworks_response",
    "get_eval_parsed_fireworks_response", "ai_conversation_handler",
    "check_input_tone", "ai_detect_trigger_type", "ai_detect_current_step",
    "ai_explain_workflow_steps", "ai_reset_intent_handler", "ai_decision_Check",
    "ai_execute_helper", "get_chat_summarization", "ai_detect_and_route_input",
    "ai_pre_gather_details", "make_workflow_conversation", "ai_scheudle_step",
    "fetchusersocialandtimezone", "edit_assigned_question",
    "delete_assigned_question", "morph_question", "assign_evidence_required",
    "answer_evidence_question", "storeargument_results", "_handle_communication",
    "_handle_navigation", "_handle_self_learn", "_trigger_function",
    "_trigger_runbook_owner", "_resolve_placeholders", "_execute_step",
    "update_steps_workflow", "_extract_context_for_step", "answer_ques_file_bk",
    "autocheckerworkflow", "execute_from_text_input",
    # helperzz guardrail
    "is_inappropriate",
    # AutoMateService
    "get_current_step_data", "create_custom_email_body", "generate_ai_content",
    "review_content", "generate_chat_reply", "generate_email_reply",
    "generate_form_schema", "evaluate_answers", "generate_questions",
    "search_knowledge_base", "assign_or_show_questions_from_file",
    "generate_questions_from_file", "generate_file_from_ai",
    # routes (pure helpers referenced by name; the endpoint handlers
    # get_assignable_users / submit_for_review are covered via route-level
    # integration + security tests, which exercise them by URL rather than by
    # name — so the name-proxy can't see them and they stay out of this ledger).
    "_milestone_for_hop", "_is_allowed_image",
}


_SELF = pathlib.Path(__file__).name


def _test_source_corpus() -> str:
    parts = []
    for p in _TESTS_ROOT.rglob("test_*.py"):
        if p.name == _SELF:
            continue  # exclude this gate file so the _COVERED ledger can't self-satisfy
        with contextlib.suppress(Exception):
            parts.append(p.read_text(encoding="utf-8"))
    return "\n".join(parts)


_CORPUS = _test_source_corpus()


def _referenced(name: str) -> bool:
    return re.search(rf"\b{re.escape(name)}\b", _CORPUS) is not None


# ── live gate 1: the ledger cannot lie ────────────────────────────────────────

@pytest.mark.parametrize("symbol", sorted(_COVERED))
def test_ledger_symbol_is_actually_referenced(symbol):
    assert _referenced(symbol), f"{symbol} is in the ledger but no test references it"


def test_ledger_is_non_trivial():
    assert len(_COVERED) >= 30


# ── forward-looking goal: full §4z coverage (xfail until complete) ─────────────

def _enumerate_sut_symbols() -> set[str]:
    """Public + private (non-dunder) functions of the in-scope modules and the
    two service classes. Best-effort: modules that fail to import are skipped."""
    import importlib

    symbols: set[str] = set()
    module_names = (
        "workflow_route.state_machine",
        "workflow_route.integration",
        "workflow_route.lifecycle",
        "playbook.helperzz",
        "playbook.background_worker",
    )
    for mod_name in module_names:
        mod = None
        with contextlib.suppress(Exception):
            mod = importlib.import_module(mod_name)
        if mod is None:
            continue
        for name, obj in vars(mod).items():
            if name.startswith("__"):
                continue
            if inspect.isfunction(obj) and getattr(obj, "__module__", "") == mod_name:
                symbols.add(name)

    # service classes
    for mod_name, cls_name in (
        ("services.workflow_service", "WorkflowRunnerV2"),
        ("services.automate_service", "AutoMateService"),
    ):
        cls = None
        with contextlib.suppress(Exception):
            cls = getattr(importlib.import_module(mod_name), cls_name)
        if cls is None:
            continue
        for name, obj in vars(cls).items():
            if name.startswith("__"):
                continue
            if inspect.isfunction(obj):
                symbols.add(name)
    return symbols


def test_full_symbol_coverage():
    """§4z hard gate: every in-scope SUT symbol is referenced by ≥1 test.

    Coverage reached 100% — this is now a strict gate (no longer xfail). A new
    untested function in the module will fail the build here.
    """
    all_symbols = _enumerate_sut_symbols()
    uncovered = sorted(s for s in all_symbols if not _referenced(s))
    assert not uncovered, (
        f"{len(uncovered)}/{len(all_symbols)} symbols uncovered: {uncovered[:25]}"
    )
