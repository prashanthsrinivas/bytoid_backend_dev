# Workflow Builder / Playbook — Bugs Found (running log)

Per TEST_PLAN.md §6/§7: every defect a test surfaces is logged here with
symptom → root cause → classification (newly-introduced vs pre-existing) → fix →
covering test. Advisory items (latent robustness gaps not yet triggered by a
realistic input) are logged but not blocking.

## Status

- Baseline: full-suite `pytest tests/ -q` currently fails at **collection** due to
  a pre-existing cross-module stub leak — `tab_tracker/helper.py` imports
  `delete_file_from_s3` from `utils.s3_utils`, which breaks when another suite's
  `utils.s3_utils` MagicMock stub leaks into `sys.modules`. **Not** caused by this
  module; tracked here as a suite-poisoning instance the §3 isolation discipline is
  meant to prevent. Workflow/playbook tests pass cleanly in isolation (280 passed).

## Findings

### ADVISORY-1 — JSON/text parsers raise on non-`str` input
- **Symptom:** `clean_json_block(None)` / `extract_json_from_llm_output(None)` raise
  `AttributeError` (`.strip()` on non-str); `normalize_input(None)` raises (`.copy()`).
- **Root cause:** the parsers assume a `str`/`dict` contract with no type guard.
- **Classification:** pre-existing latent robustness gap.
- **Trigger status:** NOT hit by the §4y-1 fuzz suite — real LLM output is always a
  `str`, and the fuzz corpus is string-typed, so the "never crash on model output"
  invariant holds (verified: `test_llm_output_fuzzing.py`, all green). Logged as
  advisory; a defensive `if not isinstance(...)` guard would close it but is not
  required by any current caller path.
- **Covering test:** `tests/hardening/test_llm_output_fuzzing.py` (string-fuzz
  invariant); a type-guard test would be added if/when the contract is hardened.

### TESTINFRA-1 — collection-order stub poisoning broke SUT imports
- **Symptom:** running the new suites together (vs. in isolation) failed at
  *collection* with `cannot import name '<fn>' from 'db.db_checkers'` and, transitively,
  `from utils.s3_utils import read_json_from_s3` — order-dependent.
- **Root cause:** other suites replace `db.db_checkers` with bare stubs missing names
  the SUT imports; and `playbook.helperzz`'s deep import chain
  (`helperzz → agent_route.doc_clarity → utils.chatopenzz → utils.s3_utils`) could be
  entered *mid-partial-import*, exposing a not-yet-bound name.
- **Classification:** pre-existing test-infra fragility (the §3 suite-poisoning hazard).
- **Fix:** added `_wf_pb_stubs.bootstrap_sut()` — pins a permissive `db.db_checkers`
  stub, stubs `services.redis_service.get_redis`, and **eagerly warms the heavy SUT
  chain in clean leaf-first order** so every module fully caches before any test
  triggers a partial import. All SUT-importing test files call it. Verified:
  189 tests pass in **both** forward and reverse module order.
- **Covering test:** the whole tranche is the regression guard (it only passes
  order-independently with the fix in place).

### TESTINFRA-2 — lazy-import s3 poisoning surfaced only under full-run order
- **Symptom:** a `get_workflow_history` parameterization test passed per-file but
  failed in the full run.
- **Root cause:** `get_workflow_history` does `from utils.s3_utils import
  generate_presigned_url` *at call time*; a sibling suite's `utils.s3_utils` stub
  poisons it after `bootstrap_sut`'s eager warm-up.
- **Fix:** the test injects a minimal `utils.s3_utils` via `patch.dict(sys.modules, …)`
  for the call. (The §7 full-run regression is exactly what surfaced this.)

### TESTINFRA-3 — coverage gate was self-satisfying
- **Symptom:** symbols added to the `_COVERED` ledger counted as "referenced" even
  with no real test, because the gate file is itself part of the scanned corpus.
- **Fix:** `_test_source_corpus()` now excludes the gate file. This immediately
  exposed two honest gaps (`get_assignable_users`, `submit_for_review` are covered
  by *route-level* tests that call them by URL, so the name-proxy can't see them);
  they were removed from the name-ledger and are tracked via the integration/security
  suites instead. The gate is now truthful.

### TESTINFRA-4 — endpoint-coverage import isolation (Phase 4/5)
- **Symptom:** importing `playbook.routes` (needed to enumerate all 55 playbook
  endpoints) failed several ways under full-suite order: (a) `services.scheduler_service
  → utils.celery_base → … → runbook.utils → import pandas` (broken pandas in env);
  (b) `'db' is not a package` after another suite did `sys.modules.setdefault("db",
  MagicMock())`; (c) `cannot import name 's3bucket' from 'utils.s3_utils'` (s3 leak);
  (d) the job-status route awaited the Redis stub (`MagicMock` not awaitable).
- **Fix (all in `bootstrap_sut`):** stub `services.scheduler_service`; restore the real
  `db` package when poisoned; restore real `yaml`/`bs4` when stubbed; make the
  `get_redis` stub return a redis whose ops are `AsyncMock`s; and warm-import
  `playbook.routes` early so it caches with real `utils.s3_utils`.
- **Result:** all 71 endpoints enumerate and test cleanly; suite passes in forward
  **and** reverse module order (1815 passed).

_No production-code bugs requiring a source fix have been surfaced; all source-side
changes are test-infra. The pre-existing full-repo `utils.s3_utils` / `db` collection
leak from other suites is now defended against within this module's `bootstrap_sut`._
