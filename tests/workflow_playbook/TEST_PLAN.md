# Workflow Builder / Playbook — Master Test Plan

Goal: **exhaustive, 100%-passing** unit + integration + security coverage for the
entire workflow-builder / playbook module, used as the driver to find and fix
every functional bug so the feature runs cleanly in the playground.

Environment: `/Users/ashkadosh/miniconda3/bin/python` · pytest 9.0.3 · flask 3.1.3.
Run: `python -m pytest tests/ -q` (json report auto-written to `testing/results/latest.json`).

---

## 0. Scope

**In scope (the "workflow builder / playbook" module):**

| Area | File(s) | Blueprint | Endpoints | Funcs/Methods |
|------|---------|-----------|-----------|---------------|
| Workflow routes | `workflow_route/routes.py` | `workflow_bp` (`/workflow`) | 16 | 11 helpers |
| Workflow state machine | `workflow_route/state_machine.py` | — | — | 28 fns + 4 exc classes |
| Workflow integration guards | `workflow_route/integration.py` | — | — | 2 |
| Workflow lifecycle | `workflow_route/lifecycle.py` | — | — | 5 |
| Playbook routes | `playbook/routes.py` | `playbook_bp` | 55 | 17 helpers/nested |
| Playbook helpers | `playbook/helperzz.py` | — | — | 28 |
| Playbook background worker | `playbook/background_worker.py` | — | — | `JobManager` (2) |
| Workflow service | `services/workflow_service.py` | — | — | `WorkflowRunnerV2` (63) + `base_name` |
| Automation service | `services/automate_service.py` | — | — | `AutoMateService` (18) |

**Totals: 2 blueprints · 71 endpoints · ~230 functions/methods.**

**Adjacent / out of scope (separate module, own plan):** `runbook/` already has
`tests/integration/runbook/TEST_PLAN.md`. We test the *seam* where playbook triggers
runbook (`_trigger_runbook_owner`, `check_runbook_exists_playbook`,
`assign_runbook_playbook`) but not runbook internals.

---

## 1. Endpoint inventory

### 1a. `workflow_bp` — `workflow_route/routes.py` (prefix `/workflow`)

| # | Method | Path | Handler | Perm |
|---|--------|------|---------|------|
| 1 | GET | `/assignable-users` | `get_assignable_users` | (inline) |
| 2 | GET | `/config` | `get_workflow_config_all` | `workflow.config.manage` |
| 3 | PUT | `/config/<doc_type>` | `update_workflow_config` | `workflow.config.manage` |
| 4 | GET | `/review-frequency` | `get_review_frequency` | `workflow.config.manage` |
| 5 | PUT | `/review-frequency` | `update_review_frequency` | `workflow.config.manage` |
| 6 | POST | `/submit` | `submit_for_review` | `compliance.runbook.read` |
| 7 | POST | `/review` | `review_document` | `compliance.runbook.read` |
| 8 | POST | `/approve` | `approve_document` | `compliance.runbook.read` |
| 9 | GET | `/by-doc/<doc_type>/<path:doc_id>` | `workflow_by_doc` | `compliance.runbook.read` |
| 10 | POST | `/publish` | `publish_document` | `compliance.runbook.read` |
| 11 | GET | `/inbox` | `workflow_inbox` | `compliance.runbook.read` |
| 12 | GET | `/history/<workflow_id>` | `workflow_history` | `compliance.runbook.read` |
| 13 | POST | `/upload_attachment` | `workflow_upload_attachment` | `compliance.runbook.read` |
| 14 | POST | `/comment` | `workflow_comment` | `compliance.runbook.read` |
| 15 | POST | `/reassign` | `reassign_workflow` | `workflow.config.manage` |
| 16 | POST | `/cancel` | `cancel_workflow_route` | `compliance.runbook.read` |

### 1b. `playbook_bp` — `playbook/routes.py` (55 endpoints)

CRUD: `create_instruction`, `update_instruction`, `get_all_instructions`,
`get_single_instruction`, `delete_instruction`, `modify_instruction`,
`playbook/jbs/<job_id>` (job_status).

Steps: `add_a_step`, `edit_a_step`, `update_step_arguments`, `delete_step_argument`,
`delete_a_step`.

Execution: `run_workflow`, `run_workflow_step`, `test-playground-step`,
`clear-playground-data`, `clear-testing-data`, `generate-workflow-input`,
`test-mid`, `test-email-checks`, `autocheck-workflow`, `autocheck-status-update`,
`workflow/conversation`, `wf-form`.

Scheduling: `schedule-workflow-checker`, `schedule-workflow`.

Questions/forms/evidence: `update-questions`, `update-questions-bulk`,
`update-form-field`, `update-form-fields-bulk`, `generate_ques_by_file`,
`make_ans_by_files`, `evidence_ques_ans_attach_playbook`, `make_s3upload`,
`edit_assigned_question`, `delete_assigned_question`, `morph_question`,
`assign_evidence_to_question`, `evidence_confirmation` (GET+POST),
`questionarie_confirmation` (GET+POST).

Config/meta: `get-allfunctions`, `list_chat_config`.

Sharing / cloning / global: `pb_temp_clone`, `pb_delete_clone`,
`share_playbook_template`, `undo_share_playbook_template`,
`get_all_global_instructions`, `get_single_global_instruction`,
`make_global_playbook`, `delete_global_playbook`, `install_global_playbook`.

Runbook seam: `check_runbook_exists_playbook`, `clear_runbook_exists_playbook`.

---

## 2. Function inventory (non-route)

- **state_machine.py:** config/freq (`get_workflow_config`, `_cadence_category`,
  `get/set_org_review_frequency`), queries (`get_workflow`, `get_workflow_for_doc`,
  `get_workflow_states_for_docs`, `get_docs_assigned_to_user`, `get_user_org_id`,
  `get_workflow_for_doc_any_role`, `enrich_workflow_for_viewer`), lifecycle
  (`create_workflow`, `transition`, `_apply_single_transition`, `cancel_workflow`),
  forward-chain pure helpers (`_next_forward_state`, `_is_forward_hop`,
  `_user_col_for_state`, `_role_col_for_state`, `_assignee_for_state`,
  `actor_eligible_for_state`, `get_actor_role_ids`), roles (`pick_user_for_role`),
  events/comments (`_append_event`, `add_comment`, `get_workflow_history`),
  inbox (`get_inbox`), schema (`bootstrap_schema`), 4 exception classes.
- **integration.py:** `guard_mutation`, `assert_doc_editable`.
- **lifecycle.py:** `reassign_orphaned_workflows`, `_handle_orphan`, `_do_reassign`,
  `_nullify_and_notify`, `_notify_reassigned`.
- **routes.py helpers:** `_apply_publish_effects`, `_milestone_for_hop`,
  `_build_milestone_entries`, `_append_doc_revision_entries`,
  `_record_review_milestones`, `_record_cancel_milestone`, `_notify`,
  `_resolve_assignee`, `_log_chain_audits`, `_dispatch_review`, `_is_allowed_image`.
- **helperzz.py:** crypto (`_enc_pb`/`_dec_pb`), s3 (`load_playbook_from_s3`,
  `save_playbook_to_s3`, `save_execution_playbook_to_s3`), pure parsers (`base_name`,
  `clean_yaml_block`, `normalize_input`, `extract_questions`, `clean_json_block`,
  `normalize_contacts`, `returninsructdata`, `replace_section`, `returnconfigandpath`,
  `format_step_data`, `extract_json_from_llm_output`, `generate_meeting_email_body`),
  AI (`check_doc_context_needed`, `evallogic`, `triggeraicontextfinder`,
  `cheap_internal_data_hint`, `needs_internal_data`, `minimize_functions`,
  `create_playbook`), guardrail (`is_inappropriate`), scheduling
  (`update_playbook_schedule_and_runtime`, `assign_runbook_playbook`).
- **background_worker.py:** `JobManager.submit_job`, `JobManager._run_job`.
- **workflow_service.py — `WorkflowRunnerV2`:** sync helpers (`is_yes`,
  `get_current_execution_data`, `generate_unique_id`, `_get_next_uncompleted_step`,
  `update_statuscount`, `check_step_exists`, `_get_first_step`, `get_step_data`,
  `_find_step_by_ref`, `_build_dependency_blocked_response`,
  `_question_answer_stats`, `_are_all_required_fields_answered`,
  `append_execution_step_log`, `get_execution_log`, `base_name`), persistence
  (`saveworkflowtos3`, `storeargument_results`), execution (`execute`,
  `_execute_step`, `_resolve_placeholders`, `_trigger_function`, `_handle_*`,
  `_find_fallback`, `execute_from_text_input`), Q&A/forms (`answer_questions`,
  `answer_questions_bulk`, `update_form_field`, `update_form_bulk`,
  `answer_evidence_question`, `edit_assigned_question`, `delete_assigned_question`,
  `morph_question`, `assign_evidence_required`), AI (`ai_*`, `get_*_fireworks_response`),
  conversation (`make_workflow_conversation`, `savechatcheck`, `check_input_tone`).
- **automate_service.py — `AutoMateService`:** `get_current_step_data`,
  `create_custom_email_body`, `generate_file_from_ai`, `generate_email_reply`,
  `generate_chat_reply`, `generate_ai_content`, `generate_questions`,
  `review_content`, `generate_form_schema`, `evaluate_answers`,
  `search_knowledge_base`, `generate_questions_from_file`,
  `assign_or_show_questions_from_file`.

---

## 3. Test architecture

**Proven recipe** (validated against `test_workflow_runbook_trigger.py`, 107 green):

1. **Stub heavy/AWS/LLM modules before import.** Real & usable in env: `flask`,
   `pymysql`, `boto3`, `dotenv`, `dbutils`, `yaml`, `bs4`, `markupsafe`, `docx`.
   Must stub (missing or side-effectful at import): `pytz`, `pptx`,
   `langchain_openai`, `apscheduler`, and **`db.rds_db`/`db.db_checkers`/
   `db.lance_db_service`** (AWS Secrets Manager at import), plus internal heavies
   `utils.fireworkzz`, `utils.s3_utils`, `utils.normal`, `utils.pb_config_utils`,
   `utils.key_rotation_manager`, `utils.celery_base`, `services.redis_service`,
   `services.scheduler_service`, `services.meet_service`,
   `services.microsoft_calender_service`, `agent_route.doc_clarity`,
   `credits_route.route`, `request_context`, `cust_helpers.pathconfig`.
2. **Shared stub installer:** new `tests/workflow_playbook/_wf_pb_stubs.py` exposing
   `install_stubs()` (idempotent, `setdefault`-based) + `make_conn(...)` /
   `DictConn` DB-mock helpers + `make_app(*bps)` Flask factory + `allow_auth()` /
   `deny_auth()` context managers that patch
   `utils.permission_required._evaluate_access`.
3. **Auth control:** the only auth gate is `permission_required_body` →
   `_evaluate_access` → `connect_to_rds`. Happy-path integration tests patch
   `_evaluate_access` to return `None` (allow). Security tests exercise the real
   `_evaluate_access` with a mocked `users`/`special_access`/`shared_users` DB.
4. **Async:** drive coroutines with `asyncio.run(...)`; build `WorkflowRunnerV2`
   via `object.__new__` to skip the heavy `__init__` (as the existing trigger test).
5. **Test isolation & teardown (anti suite-poisoning):** every test gets a **fresh
   mocked connection and fresh in-memory state per test** via function-scoped fixtures —
   no module-global mutable DB mock is shared across tests. DB writes are asserted at the
   mock boundary (the mocked conn records `execute`/`commit`/`rollback`); there is no real
   DB, so isolation is guaranteed by **constructing a new `DictConn`/`make_conn` per test**
   and never reusing it. For any integration test that does mutate a shared seeded fixture
   (e.g. a stateful in-memory store), the fixture wraps the test in **setup → act →
   explicit teardown/reset** (or a rollback shim) in `finally`, so order-dependence and
   cross-test bleed are impossible. A `tests/workflow_playbook/conftest.py`
   `autouse` fixture resets all shared registries (job store, scheduler stub, S3 stub) and
   asserts they are empty at teardown. Suites must pass under `pytest -p no:randomly` **and**
   `pytest -p randomly` (random order) to prove no inter-test coupling.
6. **Markers:** `unit`, `integration`, `security`, `authz`, `api_security`,
   `llm_attack`, `infra`, `contract`, `regression`, `idempotency`, `state`,
   `concurrency`, `fuzz`, `resilience`.

### Directory layout (new)

```
tests/
  workflow_playbook/
    _wf_pb_stubs.py            # shared stubs + app factory + auth toggles + DB mocks
    conftest.py                # autouse per-test isolation: fresh conn/state, reset+assert-empty registries
    TEST_PLAN.md               # this file
    BUGS_FOUND.md              # running log of every bug the tests surface + fix
    COVERAGE.md                # symbol→test ledger (all ~230 symbols), gate source (§4z)
    _fuzz_corpus.py            # FuzzyLLM broken-output vectors (§4y-1)
  unit/
    workflow/                  # state_machine, lifecycle, integration, routes helpers
    playbook/                  # helperzz (pure/async/crypto), routes helpers, JobManager
    services/                  # test_workflow_service_*.py, test_automate_service.py (extend)
    test_symbol_coverage.py    # §4z gate: every inventoried symbol referenced by ≥1 test
  integration/
    workflow/                  # Flask test-client, all 16 workflow_bp endpoints
    playbook/                  # Flask test-client, all 55 playbook_bp endpoints
  hardening/
    test_llm_output_fuzzing.py # §4y-1 parser fuzz corpus
    test_transition_concurrency.py # §4y-2 concurrent lock/race
    test_upstream_degradation.py   # §4y-3 429/503/timeout resilience
  security/
    authz/                     # +test_workflow_playbook_authz.py
    api/                       # +test_workflow_playbook_injection.py
    llm/                       # +test_playbook_prompt_injection.py
    infra/                     # +test_workflow_playbook_upload.py
```

---

## 4. Per-target test matrix — 100% function coverage

Coverage is **per-symbol, not representative**: **every one of the 71 endpoints (§1)
and every one of the ~230 functions/methods (§2) has its own dedicated test(s)** — no
function is left to be "implicitly exercised" by another's test. A symbol counts as
covered only when it has, as applicable: a **happy path**, an **edge** case (empty /
None / unknown / boundary), and an **error/exception** case. The enumerated tables in
§4a–§4h are the authoritative checklist for all ~230 symbols; §4z is the gate that
fails the build if any listed symbol has zero referencing tests.

### 4.0 Baseline contract per kind

- **Every endpoint** → happy 200 + response shape (contract); validation 400 (missing/
  empty/bad-enum/malformed JSON); authN 401 (no session) / authZ 403 (wrong perm,
  `totp_pending`, cross-org, unshared cross-user IDOR); error handling (DB/LLM/S3 raise
  → graceful 4xx/5xx, never an unhandled 500 stack leak); idempotency where the route
  claims it; illegal transition → 409 (`WorkflowTransitionError`), lock conflict → 409
  (`WorkflowConflictError`).
- **Every pure function** → table-driven incl. empty/None/unknown/boundary inputs.
- **Every DB function** → mocked-conn assertions on SQL shape + params + commit/rollback
  + the exception path.
- **Every async/AI function** → mock the fireworks/LLM call; assert parsing, fallback on
  malformed **and** empty model output, no crash, and output-sanitization where persisted.

### 4a. `workflow_route/state_machine.py` — 28 fns + 4 exc

| Symbol | Kind | Required cases |
|--------|------|----------------|
| `get_workflow_config` | DB | found; missing→default; malformed JSON |
| `_cadence_category` | pure | each bucket; unknown; None |
| `get_org_review_frequency` / `set_org_review_frequency` | DB | read hit/miss; write commit; rollback on error |
| `get_workflow` | DB | found; not-found |
| `get_workflow_for_doc` | DB | found; none; multiple |
| `get_workflow_states_for_docs` | DB | batch; empty input; partial |
| `get_docs_assigned_to_user` | DB | rows; empty |
| `get_user_org_id` | DB | found; none |
| `get_workflow_for_doc_any_role` | DB | each role; none |
| `enrich_workflow_for_viewer` | logic | viewer with/without perms; missing fields |
| `create_workflow` | DB | create+bootstrap; duplicate; commit/rollback |
| `transition` | orchestration | valid hop; illegal→`WorkflowTransitionError`; stale version→`WorkflowConflictError` |
| `_apply_single_transition` | internal | forward; guard reject |
| `cancel_workflow` | DB | valid; already-cancelled idempotent |
| `_next_forward_state` | pure | each state; terminal→None |
| `_is_forward_hop` | pure | forward; backward; same |
| `_user_col_for_state` / `_role_col_for_state` / `_assignee_for_state` | pure | full mapping; unknown state |
| `actor_eligible_for_state` | logic | eligible; ineligible |
| `get_actor_role_ids` | DB | roles; empty |
| `pick_user_for_role` | logic/DB | pick; none-available |
| `_append_event` | pure | append; ordering preserved |
| `add_comment` | DB | insert; empty text reject |
| `get_workflow_history` | DB | ordered events; empty |
| `get_inbox` | DB | items; empty; filtered |
| `bootstrap_schema` | DDL | idempotent re-run |
| 4 exception classes | class | construct; raise/catch; message preserved |

### 4b. `workflow_route/integration.py` — 2 fns

| `guard_mutation` | logic | allowed; blocked (locked/published) |
| `assert_doc_editable` | logic | editable; raises when not |

### 4c. `workflow_route/lifecycle.py` — 5 fns

| `reassign_orphaned_workflows` | orchestration | orphans→reassigned; none; partial failure |
| `_handle_orphan` | logic | reassign vs nullify branch |
| `_do_reassign` | DB | success; commit; error |
| `_nullify_and_notify` | DB+notify | nullify; notify invoked |
| `_notify_reassigned` | notify | sends; swallows notify error |

### 4d. `workflow_route/routes.py` helpers — 11 fns

| `_apply_publish_effects` | logic | publish side-effects applied; no-op when already published |
| `_milestone_for_hop` | pure | each hop→milestone; unknown hop |
| `_build_milestone_entries` | pure | entries built; empty |
| `_append_doc_revision_entries` | pure | append; dedupe/order |
| `_record_review_milestones` / `_record_cancel_milestone` | DB | recorded; error path |
| `_notify` | notify | sends; swallows error |
| `_resolve_assignee` | logic | resolves; none |
| `_log_chain_audits` | audit | one entry per hop; empty chain |
| `_dispatch_review` | logic | dispatch; reject |
| `_is_allowed_image` | pure | allowed ext/MIME; disallowed; missing |

### 4e. `playbook/helperzz.py` — 28 fns

| `_enc_pb` / `_dec_pb` | crypto | roundtrip; tamper→reject; wrong key; no plaintext leak |
| `load_playbook_from_s3` / `save_playbook_to_s3` / `save_execution_playbook_to_s3` | s3 | hit; miss; error; key-shape |
| `base_name`, `clean_yaml_block`, `normalize_input`, `extract_questions`, `clean_json_block`, `normalize_contacts`, `returninsructdata`, `replace_section`, `returnconfigandpath`, `format_step_data`, `extract_json_from_llm_output`, `generate_meeting_email_body` | pure | valid; empty/None; malformed; (parsers: junk-in→safe-out) |
| `check_doc_context_needed`, `evallogic`, `triggeraicontextfinder`, `cheap_internal_data_hint`, `needs_internal_data`, `minimize_functions`, `create_playbook` | async/AI | mocked LLM; parse; malformed/empty fallback; no crash |
| `is_inappropriate` | guardrail/AI | flags abusive; passes benign; empty |
| `update_playbook_schedule_and_runtime`, `assign_runbook_playbook` | scheduling/DB | schedule set; runbook-seam invoked; error |

### 4f. `playbook/background_worker.py` — `JobManager` (2)

| `submit_job` | worker | enqueue→returns id; arg passing |
| `_run_job` | worker | success→status done; exception captured→status failed (no crash) |

### 4g. `services/workflow_service.py` — `WorkflowRunnerV2` (63) + `base_name`

| Group | Methods (each individually covered) | Required cases |
|-------|--------------------------------------|----------------|
| sync helpers | `is_yes`, `get_current_execution_data`, `generate_unique_id`, `_get_next_uncompleted_step`, `update_statuscount`, `check_step_exists`, `_get_first_step`, `get_step_data`, `_find_step_by_ref`, `_build_dependency_blocked_response`, `_question_answer_stats`, `_are_all_required_fields_answered`, `append_execution_step_log`, `get_execution_log`, `base_name` | pure/edge: valid; empty/None; not-found |
| persistence | `saveworkflowtos3`, `storeargument_results` | save ok; s3 error |
| execution | `execute`, `_execute_step`, `_resolve_placeholders`, `_trigger_function`, `_find_fallback`, `execute_from_text_input`, **each concrete `_handle_*`** | happy; missing step; dependency-blocked; fallback; trigger error |
| Q&A / forms | `answer_questions`, `answer_questions_bulk`, `update_form_field`, `update_form_bulk`, `answer_evidence_question`, `edit_assigned_question`, `delete_assigned_question`, `morph_question`, `assign_evidence_required` | answer ok; required-missing; bad id; bulk partial |
| AI | **each concrete `ai_*`** and **each `get_*_fireworks_response`** | mocked LLM; parse; malformed/empty fallback |
| conversation | `make_workflow_conversation`, `savechatcheck`, `check_input_tone` | reply ok; injection-safe; empty |

Each wildcard (`_handle_*`, `ai_*`, `get_*_fireworks_response`) **expands to every
concrete method**, enumerated by name in the §4z ledger — none may be skipped.

### 4h. `services/automate_service.py` — `AutoMateService` (18)

Each of `get_current_step_data`, `create_custom_email_body`, `generate_file_from_ai`,
`generate_email_reply`, `generate_chat_reply`, `generate_ai_content`,
`generate_questions`, `review_content`, `generate_form_schema`, `evaluate_answers`,
`search_knowledge_base`, `generate_questions_from_file`,
`assign_or_show_questions_from_file` gets: happy (mocked LLM), malformed/empty-output
fallback, and error-path cases.

### 4y. Resilience, concurrency & non-determinism (hardening)

Mocking the LLM/AWS call is the unit baseline; it is **not** sufficient on its own,
because these services fail in unpredictable, adversarial ways. The following are
**required**, not optional.

**4y-1 · LLM non-determinism & parser fuzzing** *(`fuzz`)* — every parser that consumes
model output (`extract_json_from_llm_output`, `clean_json_block`, `clean_yaml_block`,
`extract_questions`, `normalize_input`, `format_step_data`, plus the `ai_*` /
`get_*_fireworks_response` / `AutoMateService.generate_*` parse paths) is tested against
a **fuzz corpus of deliberately broken model outputs**, asserting *parse-or-safe-fallback,
never a crash and never a partial write*:

| Fuzz vector | Expected behavior |
|-------------|-------------------|
| truncated / partial JSON (`{"a":1,`) | caught → fallback default, no exception |
| markdown-fenced JSON/YAML (` ```json … ``` `) | fence stripped → parsed |
| prose around JSON ("Sure! Here is: {…} hope this helps") | JSON extracted or safe fallback |
| hallucinated / extra / missing keys; wrong types | schema-validated → unknown keys ignored, missing→default |
| empty string / whitespace / `null` | fallback, no crash |
| duplicate keys, trailing commas, single quotes, NaN/Inf | tolerated or safe-fallback |
| huge / deeply-nested output | bounded, no unbounded recursion (ties CWE-770) |
| non-UTF8 / control chars / prompt-injection echo in output | sanitized before persist (ties ML09) |

Implemented via a parametrized `FuzzyLLM` mock that yields each vector; a property/
table-driven harness asserts the invariant for **all** parser symbols (enumerated in the
§4z ledger).

**4y-2 · Concurrency & race conditions** *(`concurrency`, `state`)* — the optimistic-lock
claim (`WorkflowConflictError` → 409) must be *proven*, not assumed:

- **Concurrent transition blast:** fire N simultaneous `transition`/`/approve` calls on the
  **same workflow at the same `state_version`** (threads / `asyncio.gather`); assert
  **exactly one wins** and persists, the rest get `WorkflowConflictError`/409, and the
  final state is single-valued — **no dual-approved / double-published** outcome.
- **Lost-update guard:** two readers load v=k, both write; the second write must be rejected
  by the version check (mock conn returns `rowcount=0` on the stale `UPDATE … WHERE
  state_version=k`).
- **Idempotency under concurrency:** duplicate `submit`/`cancel` racing produce one effect.
- **`JobManager` concurrency:** overlapping `submit_job` calls don't corrupt the shared job
  store; `_run_job` failures in one job don't poison sibling jobs.

**4y-3 · Upstream degradation / quota boundaries** *(`resilience`)* — simulate external
service failure for **every** boundary (Bedrock/fireworks LLM, S3, Redis, scheduler,
Secrets Manager, calendar/meet) and assert the module **degrades gracefully and the
background worker never dies**:

| Injected upstream failure | Required handling |
|---------------------------|-------------------|
| HTTP 429 Too Many Requests / throttling | backoff/retry or clean surfaced error; no crash, no partial write |
| HTTP 503 Service Unavailable | graceful 4xx/5xx to caller; job marked failed, not hung |
| socket timeout / connection reset | timeout caught; bounded wait; retry budget respected |
| malformed / empty upstream body | falls back (ties §4y-1) |
| S3/Redis raise (`ClientError`, conn error) | route → graceful error; worker → captured status, sibling jobs unaffected |
| credit/quota exhausted | feature skipped/flagged, not a 500 |

These cases run against the same mocked boundaries with the mock configured to **raise /
return the degraded response**, asserting `_run_job` records a failed status (never an
uncaught exception) and no orphaned/partially-mutated state remains.

### 4z. Coverage gate — every symbol provably tested

- A generated `tests/workflow_playbook/COVERAGE.md` ledger maps **each of the ~230
  inventoried symbols → ≥1 collected test node id**. Wildcards in §4g are expanded to
  concrete names here.
- A meta-test `test_symbol_coverage.py` reflects over the in-scope modules, builds the
  symbol set, and **asserts every symbol is referenced by at least one collected test**;
  the build **fails on any uncovered symbol** (and on any new symbol added to the module
  but not the ledger).
- "Covered" requires the kind-appropriate case set from §4.0 — a single happy-path
  reference does not satisfy the gate for DB/AI functions, which also need their
  error/fallback cases.

---

## 5. Security test matrix (exhaustive)

| Class | Vectors | Targets |
|-------|---------|---------|
| AuthN (OWASP API2) | no session → 401; expired/`totp_pending` → 403 | all 71 endpoints (representative + parametrized) |
| AuthZ / BOLA-IDOR (API1) | cross-user without share; cross-admin without `special_access`; cross-org | submit/review/approve, instruction CRUD, share/install |
| Privilege escalation (API5) | `user_type`/permission tamper via body; viewer-delegation writes | config.manage routes, transitions |
| Mass assignment (API6) | inject `owner_user_id`, `state`, `state_version`, `org_id` in body | submit, update_instruction, transitions |
| SQL injection | `'; DROP`, `' OR '1'='1`, UNION in `doc_id`,`doc_type`,`workflow_id`,`job_id`,ids | by-doc, history, get_single, delete, all id params |
| XSS / stored | `<script>`/`onerror=` in comment, instruction name, question text | comment, create/update_instruction, questions |
| Path traversal | `../../etc/passwd`, abs paths in `doc_id`, filenames, s3 keys | by-doc, upload_attachment, make_s3upload, load_playbook_from_s3 |
| SSRF | internal URLs/metadata IP in any URL-bearing arg (meeting links, attachments) | step arguments, generate_meeting_email_body |
| File upload (infra) | disallowed ext/MIME, oversized, polyglot, missing file | upload_attachment (`_is_allowed_image`), make_s3upload, make_ans_by_files |
| Oversized payload (API4) | multi-MB body, deeply nested JSON, huge arrays | create_instruction, bulk question/form updates |
| LLM prompt injection (llm_attack) | jailbreak/override in instruction, answers, file text; output-sanitization | `is_inappropriate`, `create_playbook`, `generate_questions`, `morph_question`, `make_workflow_conversation` |
| Crypto | `_enc_pb`/`_dec_pb` roundtrip; tamper → reject; no plaintext leak | helperzz crypto |
| Error/info leak | stack traces, secrets, SQL in error bodies | all (assert sanitized errors) |

### 5a. Standards coverage — mandatory frameworks

Security testing **must** explicitly cover every applicable item of **OWASP Top 10
(2021)**, **OWASP Machine Learning Top 10 (2023)**, and the **SANS/CWE Top 25 Most
Dangerous Software Errors**. The §5 vector table is the implementation; the tables
below are the mapping that proves each framework item is realized by ≥1 test (or is
explicitly justified N/A). The OWASP **API** Security Top 10 tags already inline in §5
(API1–API6) are retained and cross-referenced.

**OWASP Top 10 (2021):**

| Item | In-module manifestation | Test target |
|------|-------------------------|-------------|
| A01 Broken Access Control | IDOR cross-user/admin/org; missing perm gate; mass-assignment of `owner_user_id`/`state`/`org_id` | authz suite; transitions; submit/review/approve |
| A02 Cryptographic Failures | `_enc_pb`/`_dec_pb` strength + roundtrip; plaintext-at-rest leak; key handling | helperzz crypto |
| A03 Injection | SQLi in id params; stored XSS in comment/instruction/question; template/JSON injection | api injection suite |
| A04 Insecure Design | illegal state transitions; missing idempotency; optimistic-lock race | state suite; idempotency |
| A05 Security Misconfiguration | verbose error/stack leak; debug surface; CORS | error-leak tests |
| A06 Vulnerable/Outdated Components | dependency surface — flagged via SCA, out of unit scope | advisory (note in BUGS_FOUND) |
| A07 Identification & Auth Failures | no session→401; `totp_pending`→403; expired/refreshed token | authn suite |
| A08 Software/Data Integrity Failures | unsigned playbook clone/install/global-share; unsafe yaml/json deserialization | share/install; `clean_yaml_block`, `extract_json_from_llm_output` |
| A09 Logging/Monitoring Failures | audit event emitted for submit/review/approve/cancel/publish | audit-presence assertions |
| A10 SSRF | internal URL / metadata IP in meeting links, attachments, step args | ssrf suite |

**OWASP ML Top 10 (2023)** — this module drives LLMs (`create_playbook`,
`generate_questions`, `morph_question`, `make_workflow_conversation`, `evallogic`,
`review_content`), so ML risks are in-scope:

| Item | In-module manifestation | Test target |
|------|-------------------------|-------------|
| ML01 Input Manipulation | adversarial/jailbreak input to any LLM-fed field | llm_attack |
| ML02 Data Poisoning | poisoned instruction / knowledge-base steering output | `search_knowledge_base`, `create_playbook` |
| ML03 Model Inversion | prompt/template/system extraction via conversation | `make_workflow_conversation` |
| ML04 Membership Inference | probing for other-tenant data leakage in answers/output | cross-tenant llm tests |
| ML05 Model Theft | bulk prompt scraping; function-list exfil via `get-allfunctions` | abuse/rate (advisory) |
| ML06 AI Supply Chain | model/SDK integrity — out of runtime scope | advisory |
| ML07 Transfer Learning Attack | N/A (no fine-tuning in module) | — |
| ML08 Model Skewing | feedback manipulation of `evallogic`/`review_content` scoring | evallogic, review_content |
| ML09 Output Integrity Attack | tamper LLM output before persistence; assert sanitization | `extract_json_from_llm_output`, `clean_yaml_block`, `is_inappropriate` |
| ML10 Model Poisoning | N/A (no training pipeline in module) | — |

**SANS / CWE Top 25 (Most Dangerous Software Errors)** — applicable CWEs and where each
is exercised; items genuinely absent from the module are marked N/A with reason:

| CWE | Name | Test target |
|-----|------|-------------|
| CWE-79 | XSS | comment, create/update_instruction, questions |
| CWE-89 | SQL Injection | all id params (by-doc, history, get_single, delete, job_id) |
| CWE-20 | Improper Input Validation | every endpoint validation case |
| CWE-78 / CWE-77 | OS/Command Injection | step arguments (any shell-bearing function) |
| CWE-22 | Path Traversal | by-doc, upload_attachment, make_s3upload, `load_playbook_from_s3` |
| CWE-352 | CSRF | state-changing POSTs (cookie/session assertions) |
| CWE-918 | SSRF | URL-bearing step args, `generate_meeting_email_body` |
| CWE-862 / CWE-863 | Missing / Incorrect Authorization | authz suite, transitions |
| CWE-639 | Authorization Bypass via user-controlled key (IDOR) | submit/review/approve, instruction CRUD |
| CWE-269 / CWE-732 | Improper Privilege Mgmt / Incorrect Permission Assignment | config.manage routes, viewer-delegation |
| CWE-434 | Unrestricted File Upload | `_is_allowed_image`, make_s3upload, make_ans_by_files |
| CWE-502 | Unsafe Deserialization | yaml/json parsers, playbook install |
| CWE-287 | Improper Authentication | authn suite |
| CWE-200 | Sensitive Information Exposure | error/info-leak tests |
| CWE-770 | Allocation w/o Limits (resource exhaustion) | oversized/nested-payload tests |
| CWE-94 | Code Injection (template/LLM-output eval) | output-sanitization, `extract_json_from_llm_output` |

Any framework item without a passing test is a coverage gap and is tracked in
`BUGS_FOUND.md` until closed or justified N/A.

---

## 6. Execution phases

- **Phase 0 — Infra:** `_wf_pb_stubs.py` (stubs + app factory + auth toggles + DB
  mock), establish baseline (`pytest tests/ -q`), `BUGS_FOUND.md`.
- **Phase 1 — Pure units:** state_machine pure helpers (extend), lifecycle (extend),
  integration guards, helperzz pure/crypto, routes pure helpers, JobManager. *(fast,
  no I/O — flush out logic bugs first.)*
- **Phase 2 — Service units:** `WorkflowRunnerV2` sync + Q&A/forms + execution +
  AI (mocked); `AutoMateService` all methods.
- **Phase 3 — DB-touching units:** state_machine queries/transitions/comments/inbox,
  helperzz s3/scheduling, with mocked connections.
- **Phase 4 — Route integration:** all 16 workflow + 55 playbook endpoints via Flask
  test client (happy + validation + contract + error handling), under the §3 (recipe
  item 5) per-test isolation/teardown discipline; suite must pass in randomized order.
- **Phase 4h — Hardening (§4y):** LLM-output fuzzing of every parser (`fuzz`), concurrent
  transition/lock race tests (`concurrency`), and upstream-degradation/429-503 resilience
  tests (`resilience`) for every external boundary.
- **Phase 5 — Security:** the full §5 matrix, including the §5a OWASP Top 10 /
  OWASP ML Top 10 / SANS-CWE Top 25 coverage mapping.
- **Phase 6 — Bug-fix loop:** for each failure, triage *test-bug vs code-bug*; fix
  code bugs in source, log in `BUGS_FOUND.md` (symptom → root cause → fix → test);
  then run the **§7 regression protocol** before the fix counts as done; `ruff check .`
  clean.

## 7. Regression protocol (mandatory after every fix)

Every code change — a test-surfaced bug, a security finding, or an incidentally
discovered defect — triggers this loop. A fix is **not done** when its own test goes
green; it is done only when the loop below terminates clean.

1. **Re-run the entire suite, not just the affected test:** `python -m pytest tests/ -q`
   (the full 3365-test baseline **plus** all new workflow/playbook tests). Per-fix green
   is necessary but not sufficient.
2. **Diff against the last-known-green baseline** (`testing/results/latest.json`). Any
   test that was green before this fix and is now red is a **regression introduced by
   the fix**.
3. **Triage every new failure** as *test-bug vs code-bug* (same as Phase 6).
4. **Log every incidental bug in `BUGS_FOUND.md`** with: symptom → which fix exposed or
   introduced it → root cause → **classification (newly introduced vs pre-existing)** →
   fix → covering test. **Pre-existing bugs are not skipped** — surfacing one means it
   gets the same treatment as any other, regardless of how long it has been latent.
5. **Fix it, then recurse to step 1.** The loop terminates only when a full run is 100%
   green with **zero new failures** and `ruff check .` is clean.
6. **Apply one logical fix at a time** where feasible — batching fixes hides which one
   caused a regression and defeats the diff in step 2.

This makes a full regression run a hard gate after *every* fix, and treats any
incidentally-found bug (including long-standing ones) as a first-class item that
re-enters the same triage → fix → regress cycle until the suite is clean.

## 8. Definition of done

- 100% of new tests pass; existing 3365-test suite stays green (no regressions),
  verified by the §7 regression protocol after the final fix.
- Every endpoint, blueprint, and listed function has ≥1 unit/integration test.
- Security matrix fully exercised, including every applicable OWASP Top 10, OWASP ML
  Top 10, and SANS/CWE Top 25 item per §5a (each mapped to a passing test or a justified
  N/A); no unhandled 500s, no info leaks, no IDOR.
- All code bugs surfaced are fixed and logged in `BUGS_FOUND.md`, including incidental
  and pre-existing bugs found during regression; `ruff check .` clean.
- Every fix has passed the §7 regression gate (full-suite green, zero new failures).
- §4y hardening proven: every LLM-output parser passes the fuzz corpus; concurrent
  transition races yield exactly one winner (no dual-approve/double-publish); every
  external boundary handles 429/503/timeout/malformed degradation without crashing the
  worker or leaving partial state.
- Suite passes under **randomized test order** (no inter-test coupling / suite poisoning);
  all shared registries assert-empty at teardown.
- Module imports & runs in the playground with no stub-only crashes.
