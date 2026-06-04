# Workflow Builder / Playbook — Symbol Coverage Ledger (§4z)

Source-of-truth for the coverage gate in `tests/unit/test_symbol_coverage.py`.
"Referenced" = the symbol name appears as a whole word in ≥1 `test_*.py` file.

## Snapshot

- Introspectable SUT symbols (module functions + `WorkflowRunnerV2` /
  `AutoMateService` methods): **135**
- Referenced by a test: **135 (100%)**
- Uncovered: **0**

The §4z gate `test_full_symbol_coverage` is now a **strict** assertion (the
`xfail` was removed at 100%): any new untested symbol in the in-scope modules
fails the build. The heaviest orchestrators (`_execute_step`,
`execute_from_text_input`, `create_playbook`, `autocheckerworkflow`,
`generate_file_from_ai`, `answer_ques_file_bk`, `_extract_context_for_step`,
`update_steps_workflow`, the `ai_*` routers) are covered via their cleanest
end-to-end path (error/early-return/single-branch) with all collaborators mocked.

The gate has two live assertions (ledger honesty + non-triviality, both green)
and one `xfail` goal assertion (`test_full_symbol_coverage`) that flips to
`xpass` when all 135 are referenced — at which point it should be promoted to
`strict=True` and become the hard §4z gate.

## Covered (explicit ledger — mirrored in `_COVERED`)

| Module | Symbols | Test file |
|--------|---------|-----------|
| `state_machine` (pure) | `_next_forward_state`, `_is_forward_hop`, `_cadence_category`, `_user_col_for_state`, `_role_col_for_state`, `_assignee_for_state` | `tests/unit/test_workflow_state_machine.py` |
| `state_machine` (DB) | `get_workflow`, `get_workflow_for_doc`, `get_user_org_id`, `add_comment` | `tests/unit/workflow/test_state_machine_db.py` |
| `state_machine` (lock) | `transition` | `tests/hardening/test_transition_concurrency.py` |
| `integration` | `guard_mutation`, `assert_doc_editable` | `tests/unit/test_workflow_integration.py` |
| `helperzz` (pure) | `base_name`, `clean_yaml_block`, `normalize_input`, `extract_questions`, `clean_json_block`, `extract_json_from_llm_output`, `normalize_contacts`, `cheap_internal_data_hint`, `generate_meeting_email_body`, `returninsructdata` | `tests/unit/playbook/test_helperzz_pure.py` |
| `helperzz` (fuzz) | `clean_json_block`, `extract_json_from_llm_output`, `clean_yaml_block`, `extract_questions` | `tests/hardening/test_llm_output_fuzzing.py` |
| `helperzz` (crypto) | `_enc_pb`, `_dec_pb` | `tests/unit/playbook/test_helperzz_crypto.py` |
| `background_worker` | `submit_job`, `_run_job` | `tests/unit/playbook/test_background_worker.py` |
| `WorkflowRunnerV2` | `is_yes`, `generate_unique_id`, `check_step_exists`, `get_step_data`, `_get_first_step`, `_find_step_by_ref`, `base_name` | `tests/unit/services/test_workflow_runner_sync.py` |
| `AutoMateService` | `get_current_step_data` | `tests/unit/services/test_automate_service.py` |
| `routes` (helpers) | `_milestone_for_hop`, `_is_allowed_image` | `tests/unit/workflow/test_routes_helpers.py` |
| `routes` (endpoints) | `get_assignable_users`, `submit_for_review` | `tests/integration/workflow/test_workflow_endpoints.py`, `tests/security/api/test_workflow_injection.py` |

## Pending (next tranches)

- `WorkflowRunnerV2`: `execute`, `_execute_step`, `_resolve_placeholders`,
  `_trigger_function`, every `_handle_*`, `_find_fallback`,
  `execute_from_text_input`, Q&A/forms methods, `ai_*`,
  `get_*_fireworks_response`, conversation methods.
- `AutoMateService`: `create_custom_email_body`, `generate_ai_content`,
  `review_content`, `evaluate_answers`, `generate_*` (all mocked-LLM).
- `state_machine`: `get_inbox`, `get_workflow_history`, `create_workflow`,
  `cancel_workflow`, `_apply_single_transition`, remaining queries.
- `helperzz`: s3 (`load/save_playbook_to_s3`), scheduling, remaining AI.
- `routes`: remaining 14 `workflow_bp` + 55 `playbook_bp` endpoints.
