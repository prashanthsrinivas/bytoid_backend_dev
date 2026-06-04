# Workflow Builder / Playbook — Test Suite & Fixes (Documentation)

Single-file reference for the test suite built against the workflow-builder /
playbook module, plus every error fixed along the way.

## Headline

- **28 test modules + 3 infra files** (under `tests/`).
- **252 test functions → 1,641 collected cases** after parametrization.
- **100% symbol coverage**: all 135 in-scope functions/methods referenced by ≥1
  real test, enforced by a **strict** gate (`test_full_symbol_coverage`).
- **All 71 endpoints** (16 `workflow_bp` + 55 `playbook_bp`) integration-tested.
- **Security**: OWASP Top 10 (2021), OWASP ML Top 10 (2023), SANS/CWE Top 25 rows
  realized and marker-tagged.
- **Order-independent**: passes in forward and reverse module order; ruff-clean.
- Run: `python -m pytest tests/unit tests/hardening tests/integration/workflow
  tests/security tests/workflow_playbook -q`

---

## 1. Infrastructure files (`tests/workflow_playbook/`)

| File | Purpose |
|------|---------|
| `_wf_pb_stubs.py` | Shared stubs, fake DB conn/cursor, Flask app factory, auth toggles (`allow_auth`/`deny_auth`), `mock_rds`, and **`bootstrap_sut()`** (order-independent import isolation). |
| `conftest.py` | Autouse per-test isolation; installs stubs once; `fresh_conn` fixture; resets leaked `connect_to_rds` patches. |
| `_fuzz_corpus.py` | 22-vector `FuzzyLLM` corpus of broken model outputs (truncated/markdown-fenced/prose/NaN/huge/non-UTF8/etc.). |
| `TEST_PLAN.md` | Master plan (scope, matrix, phases, regression protocol). |
| `COVERAGE.md` | Symbol→test ledger; coverage-gate source of truth. |
| `BUGS_FOUND.md` | Running defect log (see §3). |

---

## 2. Test modules — what each covers

### Phase 1 — pure units · `tests/unit/playbook/`, `tests/unit/workflow/`

| File | Tests | Covers |
|------|------:|--------|
| `test_helperzz_pure.py` | 30 | `base_name`, `clean_yaml_block`, `normalize_input`, `extract_questions`, `clean_json_block`, `extract_json_from_llm_output`, `normalize_contacts`, `cheap_internal_data_hint`, `generate_meeting_email_body`, `returninsructdata`, `format_step_data`, `replace_section`, `returnconfigandpath` |
| `test_helperzz_crypto.py` | 5 | `_enc_pb`/`_dec_pb` — roundtrip, passthrough, cross-user reject, no plaintext leak |
| `test_helperzz_guardrail.py` | 4 | `is_inappropriate` — foul words, gibberish, whole-word matching |
| `test_workflow_runner_sync.py` | 11 | `WorkflowRunnerV2`: `base_name`, `is_yes`, `generate_unique_id`, `check_step_exists`, `get_step_data`, `_get_first_step`, `_find_step_by_ref` |
| `test_routes_helpers.py` | 7 | `workflow_route.routes`: `_milestone_for_hop`, `_is_allowed_image` |
| `test_state_machine_logic.py` | 7 | `actor_eligible_for_state` (direct/role-broadcast/precedence/draft-owner) |

### Phase 2 — service units (mocked LLM) · `tests/unit/services/`

| File | Tests | Covers |
|------|------:|--------|
| `test_workflow_runner_more.py` | 32 | `update_statuscount`, `_question_answer_stats`, `_are_all_required_fields_answered`, `_build_dependency_blocked_response`, `get_current_execution_data`, `get_execution_log`, `append_execution_step_log`, `_find_fallback`, `get_current_chats`, `get_attendees`, `handle_workflow_reset`, `storeargument_results`, `_resolve_placeholders` |
| `test_workflow_runner_next_step.py` | 5 | `_get_next_uncompleted_step` (testing/online shapes, order) |
| `test_workflow_runner_ai.py` | 6 | `get_parsed_fireworks_response`, `get_eval_parsed_fireworks_response`, `ai_conversation_handler`, `check_input_tone` |
| `test_workflow_runner_ai_batch.py` | 10 | `ai_detect_trigger_type`, `ai_detect_current_step`, `ai_explain_workflow_steps`, `ai_reset_intent_handler`, `ai_decision_Check`, `ai_execute_helper`, `ai_detect_and_route_input`, `ai_pre_gather_details`, `make_workflow_conversation`, `ai_scheudle_step`, `fetchusersocialandtimezone`, `get_chat_summarization` |
| `test_workflow_runner_questions.py` | 11 | `edit_assigned_question`, `delete_assigned_question`, `morph_question`, `assign_evidence_required`, `answer_evidence_question` |
| `test_workflow_runner_handlers.py` | 6 | `_handle_communication`, `_handle_navigation`, `_handle_self_learn`, `_trigger_function`, `_trigger_runbook_owner` |
| `test_workflow_runner_heavy.py` | 7 | `_execute_step`, `update_steps_workflow`, `_extract_context_for_step`, `answer_ques_file_bk`, `autocheckerworkflow`, `execute_from_text_input` |
| `test_automate_service.py` | 9 | `get_current_step_data`, `assign_or_show_questions_from_file`, `generate_questions_from_file` |
| `test_automate_service_ai.py` | 4 | `create_custom_email_body`, `generate_file_from_ai` |
| `test_automate_service_ai_batch.py` | 12 | `generate_ai_content`, `review_content`, `generate_chat_reply`, `generate_email_reply`, `generate_form_schema`, `evaluate_answers`, `generate_questions`, `search_knowledge_base` |
| `test_helperzz_ai.py` (playbook) | 12 | `needs_internal_data`, `minimize_functions`, `check_doc_context_needed`, `evallogic`, `triggeraicontextfinder` |
| `test_helperzz_scheduling.py` (playbook) | 2 | `assign_runbook_playbook`, `update_playbook_schedule_and_runtime` |
| `test_helperzz_create_playbook.py` (playbook) | 1 | `create_playbook` (no-functions 400 path) |
| `test_background_worker.py` (playbook) | 5 | `JobManager.submit_job` / `_run_job` (+ resilience/concurrency) |

### Phase 3 — DB-touching units · `tests/unit/workflow/`

| File | Tests | Covers |
|------|------:|--------|
| `test_state_machine_db.py` | 38 | `get_workflow`, `get_workflow_for_doc`, `get_user_org_id`, `get_workflow_config`, `get_actor_role_ids`, `get_org/set_org_review_frequency`, `get_workflow_states_for_docs`, `get_docs_assigned_to_user`, `add_comment`, `_append_event`, `get_inbox`, `cancel_workflow`, `get_workflow_for_doc_any_role`, `enrich_workflow_for_viewer`, `_apply_single_transition` |

### Phase 4y — hardening · `tests/hardening/`

| File | Tests | Covers |
|------|------:|--------|
| `test_llm_output_fuzzing.py` | 5 (×22 vectors) | §4y-1 — parsers never crash on broken LLM output; fenced JSON recovered |
| `test_transition_concurrency.py` | 1 | §4y-2 — concurrent `transition` race: exactly one winner, rest `WorkflowConflictError` |
| (resilience) `test_background_worker.py` | — | §4y-3 — 429/503/timeout → job failed, worker survives; sibling isolation |

### Phase 4 — route integration · `tests/integration/workflow/`

| File | Tests | Covers |
|------|------:|--------|
| `test_workflow_endpoints.py` | 5 | `assignable-users`, `submit` — validation/authz/contract/malformed-JSON |
| `test_all_endpoints.py` | 3 (×71 routes) | **All 71 endpoints**: no unhandled 500 + authorization enforced; route-table-complete guard |

### Phase 5 — security · `tests/security/`

| File | Tests | Covers |
|------|------:|--------|
| `api/test_workflow_injection.py` | 3 | A03/CWE-89 SQLi (body + path params bound); A01/CWE-862 authZ |
| `test_owasp_matrix.py` | 8 | CWE-79 XSS, CWE-918 SSRF, CWE-22 traversal, CWE-770 oversized, A02/CWE-200 crypto, ML01 guardrail, ML09 output sanitization, A09 audit logging |

### Phase 6 — coverage gate · `tests/unit/`

| File | Tests | Covers |
|------|------:|--------|
| `test_symbol_coverage.py` | 3 (+49 ledger params) | §4z **strict** gate: every one of 135 SUT symbols referenced by ≥1 test; ledger-honesty; non-triviality |

---

## 3. Errors / bugs fixed

All defects surfaced were **test-infrastructure / isolation** issues (the
regression protocol caught them); no production source bug required a code change.

### TESTINFRA-1 — collection-order stub poisoning broke SUT imports
- **Symptom:** running suites together failed at collection with
  `cannot import name '<fn>' from 'db.db_checkers'` and, transitively,
  `from utils.s3_utils import read_json_from_s3` — order-dependent.
- **Cause:** other suites replace `db.db_checkers` with bare stubs; `helperzz`'s
  deep chain (`helperzz → agent_route.doc_clarity → utils.chatopenzz →
  utils.s3_utils`) could be entered mid-partial-import.
- **Fix:** `bootstrap_sut()` — pins a permissive `db.db_checkers`, stubs
  `services.redis_service.get_redis`, and **eagerly warms the heavy import chain
  in clean leaf-first order** so everything caches before any partial import.

### TESTINFRA-2 — lazy `utils.s3_utils` import poisoning (surfaced only full-run)
- **Symptom:** a `get_workflow_history` test passed per-file, failed in the full run.
- **Cause:** `get_workflow_history` does `from utils.s3_utils import
  generate_presigned_url` **at call time**; a sibling stub poisons it after warm-up.
- **Fix:** the test injects a minimal `utils.s3_utils` via
  `patch.dict(sys.modules, …)` for the call.

### TESTINFRA-3 — coverage gate was self-satisfying
- **Symptom:** symbols in the `_COVERED` ledger counted as "referenced" with no
  real test, because the gate file was in its own scanned corpus.
- **Fix:** `_test_source_corpus()` excludes the gate file. This immediately
  exposed two honest gaps (`get_assignable_users`/`submit_for_review` tested by
  URL, not by name) — removed from the name-ledger; covered by integration tests.

### TESTINFRA-4 — endpoint-coverage import isolation (Phase 4/5)
Importing `playbook.routes` (to enumerate all 55 playbook endpoints) surfaced four
order-dependent failures, all fixed in `bootstrap_sut`:
- **(a) broken pandas chain** — `services.scheduler_service → utils.celery_base →
  … → runbook.utils → import pandas` (pandas broken in env). → stub
  `services.scheduler_service`.
- **(b) `'db' is not a package`** — another suite did
  `sys.modules.setdefault("db", MagicMock())`. → restore the real `db` package.
- **(c) `cannot import name 's3bucket' from 'utils.s3_utils'`** — s3 leak. →
  warm-import `playbook.routes` early so it caches with real `utils.s3_utils`.
- **(d) non-awaitable Redis stub** — the job-status route `await`s the Redis
  stub (`MagicMock`). → `get_redis` stub returns a redis whose ops are `AsyncMock`s.
- **(e) `yaml`/`bs4` stub collision** — `evallogic`'s `yaml.safe_load` failed when
  another suite stubbed `yaml`. → restore real `yaml`/`bs4` when stubbed.

### `_apply_single_transition` false-positive (closed)
- **Symptom:** the symbol showed as "covered" but was only **patched out** in the
  concurrency test (name matched the proxy; function never executed).
- **Fix:** added genuine tests in `test_state_machine_db.py` that execute it
  against a `FakeCursor` (UPDATE + event INSERT + role-claim logic).

### ADVISORY-1 — parsers raise on non-`str` input (logged, not blocking)
- `clean_json_block(None)` / `extract_json_from_llm_output(None)` raise
  `AttributeError`. Not triggered by real LLM output (always `str`); the fuzz
  suite proves the "never crash on model output" invariant holds. Logged as
  advisory.

### Lint fixes applied to authored tests
- B017 (blind `Exception` in `pytest.raises`) → specific `ValueError`.
- S110/S112 (`try/except/pass|continue`) → `contextlib.suppress`.
- S105 (test SECRET_KEY) → `# noqa: S105`.
- RUF100 (unused `noqa`) and RUF059 (unused unpack) → removed / `_`-prefixed.

> Pre-existing baseline note: the full-repo single-invocation run still fails at
> collection due to a `utils.s3_utils` / `db` leak from **other** modules'
> suites (e.g. `tab_tracker`). This module's `bootstrap_sut` defends against it
> internally; the cross-module baseline leak is out of scope here.
