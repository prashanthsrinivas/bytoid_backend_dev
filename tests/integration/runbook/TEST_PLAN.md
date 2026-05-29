# Test Plan — `runbook/routes.py` (28 endpoints)

Status: **PLAN ONLY** — no test code written yet. This document enumerates every
test case to be implemented. Implementation lands in
`tests/integration/runbook/test_runbook_routes_api.py` (one module, grouped by
endpoint) once this plan is approved.

---

## 0. Scope & coverage model

Every endpoint is tested against the four criteria from the request:

1. **Use cases (UC)** — a plain-English statement of what the endpoint does.
2. **Primary success (P-tests)** — the happy path(s) must return 2xx + the
   documented body shape, *always*.
3. **Edge cases (E-tests)** — split into:
   - **E-func**: the system still behaves correctly at boundaries (empty lists,
     list-vs-dict normalization, missing optional fields, shared-access
     fallbacks, composite `##SU##` IDs, double-encoded JSON, etc.).
   - **E-graceful**: when a dependency fails, the endpoint returns a *structured*
     JSON error with a deliberate status code — never an unhandled stack trace
     and never a bare framework 404/500. Where the current code already 500s on
     a dependency exception, the test asserts the **JSON envelope** (`{"error":
     ...}`) so a regression that leaks HTML or crashes the worker is caught.
4. **Misuse / CIA (M-tests)** — Confidentiality, Integrity, Availability:
   - **Confidentiality**: cannot read another user's runbook/result without an
     explicit share or workflow assignment; cross-admin access requires an
     `approved` `special_access` row; shared-access security guards
     (`authorized_shared_ids`) hold.
   - **Integrity**: writes are permission-gated (`.edit`/`.create`/`.delete`),
     protected fields (`runbook_id`, `user_id`) cannot be overwritten, viewer
     delegation cannot mutate, governance-locked docs reject edits.
   - **Availability**: malformed input / oversized payload / injection strings
     do not crash the worker or hang; a failing optional sub-step (audit log,
     workflow-state lookup, shared fetch) is swallowed and the primary response
     still returns.

### Shared harness (applies to all tests)

- A minimal Flask app registers `runbook_bp`; tests drive it via `test_client()`
  (mirrors `tests/integration/api_to_db/test_tests_routes_api.py`).
- Heavy transitive imports are stubbed in `sys.modules` **before** importing
  `runbook.routes`: `pymysql`, `db`, `db.rds_db`, `db.lance_db_service`,
  `playbook.*`, `radar.radar_helpers`, `websockets_custom.ws_instance`,
  `services.redis_service`, `utils.s3_utils`, `utils.celery_base`,
  `runbook.helper`, `runbook.helper2`, `runbook.utils`, `shared_configuration`,
  and `workflow_route.*`. `services.audit_log_service` is stubbed but keeps the
  action-constant names as plain strings.
- The auth decorator `permission_required_body` calls `connect_to_rds()` in
  `utils.permission_required`. A fixture patches it with a fake connection whose
  `users` SELECT returns an **admin self-access** row by default
  (`user_type="admin"`, `owner==user` ⇒ allowed). Per-test overrides simulate
  normal users, cross-admin, viewer delegation, and "user not found".
- Module-level `runbook.routes.dbserver` (a `LanceDBServer` instance) is patched
  per test with async-returning mocks (`AsyncMock`/coroutine fakes), since the
  routes call it through `_run_async(...)`.
- Helper builders: `composite(logged, target)` → `"{logged}##SU##{target}"`;
  `valid_result(...)`, `valid_runbook(...)`, `share_entry(...)`.

### Cross-cutting test (applies once, not per-endpoint)

- **X1 (M-availability)**: every endpoint, when called with **no `user_id`
  anywhere** (no body/query/route/session), returns `401 Unauthorized` from the
  decorator — never reaches handler. Parametrized over all 28 routes.
- **X2 (M-integrity)**: every `.edit`/`.create`/`.delete` route, when the caller
  is a **normal user without the required permission**, returns `403`.
  Parametrized over the write routes.
- **X3 (M-confidentiality)**: cross-admin access without an `approved`
  `special_access` row returns `403 "Admin access restricted"`. Parametrized
  over a representative read + write route.
- **X4 (M-integrity)**: cross-admin **viewer** delegation (`access_level
  ="viewer"`) is blocked on every `POST/PUT/PATCH/DELETE` route (`403 "Viewer
  access cannot modify resources"`) but allowed on `GET`.

### Cross-cutting test — auth-decorator branch coverage (`permission_required_body`)

These exercise the decorator's own branches once (parametrized over a small set
of representative routes — one read, one write), because every route shares it:

- **X5 (M-confidentiality)**: calling user_id is present but **not in the
  `users` table** → `404 "User not found"` (distinct from X1's missing-id 401).
- **X6 (M-integrity)**: **normal-user self-access** (`owner == user`) is allowed
  **without** any permission lookup — assert a permission-less normal user
  reaching their *own* data succeeds (the decorator returns before the role
  check).
- **X7 (M-integrity)**: normal user whose `permissions.status != "active"` →
  `403 "No active role assigned"` (separate branch from "Permission denied").
- **X8 (M-availability, finding-candidate)**: admin caller targets a
  `owner_user_id` that **does not exist** → `owner` is `None` →
  `owner["user_type"]` raises `TypeError` **inside the decorator**, surfacing as
  a raw 500. Test asserts current behavior and flags it (the decorator should
  return `404`/`403` for a missing target, not crash). Tagged `security`.

---

## 1. `POST /runbook/assign` — `assign_runbook`

**UC**: Share a generated runbook *result* with another user, either by naming a
target user (`manual`) or by round-robin assignment to a role (`role`). Records
an audit event and returns the resulting `sharing_access` list.

**P-tests**
- P1: `manual` assignment to an existing target user → `200 {success:true,
  sharing_access:[...]}`; `core_assign_report` called with the resolved
  target email/id and `parent_id=runbook_id`.
- P2: `role` assignment, role has `compliance.runbook.read`, round-robin returns
  an eligible user → `200`, assignment uses the round-robin user.
- P3: audit event `REPORT_SHARED` is emitted with the right metadata on success.

**E-func**
- E1: target identified by **email** instead of user_id resolves via the
  `WHERE user_id=%s OR email=%s` lookup.
- E2: `result_record.runbook_id` is empty/None ⇒ ownership cross-check skipped,
  assignment still succeeds.

**E-graceful**
- E3: missing any of `user_id/runbook_id/result_id/assignment_type` → `400` with
  the exact field-list message (not 500).
- E4: `assignment_type="manual"` but no `target_user_id` → `400`.
- E5: `assignment_type="role"` but no `role_id` → `400`.
- E6: `assignment_type` is neither manual nor role → `400`.
- E7: target user not found → `404 "User not found"`.
- E8: admin row not found → `404 "Admin not found"`.
- E9: `runbook_get_result` returns `None` / `{"status":"not_found"}` → `404
  "Runbook result not found"`.
- E10: `result_id` belongs to a *different* runbook_id → `400`.
- E11: `core_assign_report` returns an error containing "permission" → `403`;
  any other error → `400`.
- E12: `core_assign_report` raises → caught → `500 {"error": ...}` (JSON
  envelope, connection closed in `finally`).
- E13: audit logging raises → swallowed (warning), response still `200`.

**M-tests**
- M1 (integrity): role assignment where the role lacks
  `compliance.runbook.read` → `403 "Role does not have runbook access
  permission"` (cannot grant read to a role that can't read).
- M2 (confidentiality): `result_id` not belonging to `runbook_id` is rejected
  (E10) — prevents binding a share to an unrelated runbook.
- M3 (integrity): endpoint requires `compliance.runbook.edit`; normal user
  without it → `403` (covered by X2, asserted here too).
- M4 (availability): `core_assign_report` exception does not leak the DB
  connection (assert `conn.close()` called) and returns structured JSON.

---

## 2. `POST /runbook/revoke` — `revoke_runbook`

**UC**: Revoke a previously shared runbook result from a target user. Audited.

**P-tests**
- P1: valid revoke → `200 {success:true, sharing_access:[...]}`;
  `core_revoke_report` called with `(admin_id, user_id, result_id, "runbook")`.
- P2: audit `REPORT_SHARE_REVOKED` emitted with correct metadata.

**E-graceful**
- E1: missing any of `user_id/target_user_id/runbook_id/result_id` → `400`.
- E2: `core_revoke_report` returns an error → `400 {"error": ...}`.
- E3: `core_revoke_report` raises → `500 {"error": ...}` (structured).
- E4: audit raises → swallowed, still `200`.

**M-tests**
- M1 (integrity): requires `.edit`; normal user without it → `403`.
- M2 (integrity): revoking a result not currently shared → core returns error →
  `400` (no silent success).
- M3 (confidentiality): a non-owner admin cannot revoke another admin's share
  without `special_access` (X3 applies).

---

## 3. `GET /runbook/shared/<user_id>` — `get_user_shared_runbooks`

**UC**: List the runbooks shared *with* the given user (filters the user's shared
reports down to `type=="runbook"`).

**P-tests**
- P1: returns only the `runbook`-typed entries → `200 {rid: data, ...}`.
- P2: user with mixed shares (runbook + other types) → only runbook entries.

**E-func**
- E1: user with no shares → `200 {}` (empty object, not 404).

**E-graceful**
- E2: `get_user_shared_reports` raises → `500 {"error": ...}` (structured).

**M-tests**
- M1 (confidentiality): response contains only entries shared with *this*
  user_id (the helper is the boundary; assert it's called with the path user_id
  exactly and nothing else is added).
- M2 (integrity/read-gate): requires `.read` (X1/permission gate applies).

---

## 4. `GET /runbook/shared/view/<user_id>` — `get_shared_runbook_view`

**UC**: For a shared recipient, return the full runbook definition + the specific
result, fetched from the *owner's* LanceDB space, after verifying the share is
active and access is granted.

**P-tests**
- P1: valid share + active access → `200 {success, runbook, result, shared_by}`.
- P2: owner's `get_runbook_by_id` returns a list → normalized to first element.

**E-func**
- E1: result fetch returns `{"status":"not_found"}` → `result` coerced to
  `None`, still `200` with runbook present.

**E-graceful**
- E2: missing `runbook_id` or `result_id` query param → `400`.
- E3: no share entry for `result_id`, or entry type ≠ runbook → `404 "No shared
  access found"`.
- E4: entry's `runbook_id` ≠ requested `runbook_id` → `400`.
- E5: entry missing `mainuser_id` → `500 {"error":"Invalid shared report
  entry"}` (structured, deliberate).
- E6: LanceDB fetch raises → `500 {"error": ...}`.

**M-tests**
- M1 (confidentiality, **critical**): access entry exists but
  `user_access.access` is false/absent → `403 "Access revoked or not granted"`.
  Revoked share must not leak the report.
- M2 (confidentiality): the `sharing_access` lookup keys on *this* `user_id`; a
  recipient cannot view a result shared only with someone else (assert the
  `next(... if e["id"]==user_id)` boundary by feeding a foreign id).
- M3 (confidentiality): data is read from `main_user_id`'s space (owner), and
  `shared_by` echoes the owner — recipient never gets to specify the owner.

---

## 5. `GET /runbook/sharedconfig/<user_id>` — `get_runbook_sharedconfig`

**UC**: Return the admin's full shared-report configuration object.

**P-tests**
- P1: returns the config dict from `get_admin_shared_config` → `200`.

**E-graceful**
- E1: helper raises → `500 {"error": ...}`.

**M-tests**
- M1 (read-gate): requires `.read`.
- M2 (confidentiality): config fetched for path `user_id` only.

---

## 6. `POST /runbook/create` — `create_runbook`

**UC**: Accept multipart form (fields + `structure_file` + `files[]`), enqueue a
background `execute_runbook_create` job, audit `RUNBOOK_CREATED`, and return a
`job_id` immediately (`queued`). The heavy work runs async in `JobManager`.

**P-tests**
- P1: minimal valid form (`user_id`, `name`) → `200 {success:true, job_id,
  status:"queued"}`; `JobManager.submit_job` called once with
  `execute_runbook_create`.
- P2: with a `structure_file` upload → file is base64-encoded into
  `data["structure_file"]` before submit.
- P3: with multiple `files` → each base64-encoded into `data["files"]`.
- P4: audit `RUNBOOK_CREATED` emitted with `job_id` + `runbook_name`.

**E-func**
- E1: form sends `is_template="true"` (string) — accepted; submit still queues
  (normalization happens in the job, but the route must not choke on string).
- E2: no files at all → still queues (files optional).

**E-graceful**
- E3: `user_id` missing in form → `401 Unauthorized` (route-level check after
  parse, before submit).
- E4: `JobManager.submit_job` raises → caught → `500 {"error": ...}` (no leaked
  trace).

**M-tests**
- M1 (integrity): requires `.create`; normal user without it → `403`.
- M2 (availability): an oversized / many-file upload still returns promptly with
  a `job_id` (work is deferred) — assert the route does not block on processing.
- M3 (integrity): the job is submitted with the *parsed* `user_id`, so a
  composite `##SU##` caller is attributed correctly in the audit actor.

> Note: `execute_runbook_create` (the async worker) is itself behavior-rich
> (CloudWatch parsing, S3 upload, template fallback, log/api/playbook branches).
> It is unit-tested **separately** from the HTTP route — see §29 "Worker
> coroutines" below — because the route only enqueues.

---

## 7. `GET /runbook/status/<job_id>` — `get_job_status`

**UC**: Look up a queued/running job's status from Redis by `job_id`.

**P-tests**
- P1: existing job → `200` with the job payload.

**E-graceful**
- E1: job not found in Redis → `404 {"error":"Job not found"}`.
- E2: Redis raises → `500 {"error", "trace"}` (structured; note this route is
  **not** permission-gated — see M1).

**M-tests**
- M1 (confidentiality, **finding-candidate**): this route has **no
  `@permission_required_body`** and no ownership check on `job_id` — any caller
  who knows/guesses a `job_id` reads its status. Test documents current behavior
  (open read) and is tagged `@pytest.mark.security` so the gap is visible; a
  follow-up assertion checks whether job payloads contain sensitive data.
- M2 (availability): a malformed/huge `job_id` string returns `404`, not a
  crash.

---

## 8. `POST /runbook/modify` — `modify_runbook`

**UC**: Accept multipart form, enforce the governance edit-lock, merge framework
policies into `reference_sources`, enqueue `execute_modify_runbook`, audit
`RUNBOOK_UPDATED`, return `job_id` (`queued`).

**P-tests**
- P1: valid form with `runbook_id` → `200 {success, job_id, status:"queued"}`.
- P2: `reference_sources` with frameworks → policies merged into `policy_ids`
  before submit.
- P3: audit `RUNBOOK_UPDATED` emitted.

**E-func**
- E1: `reference_sources` as a JSON **string** → parsed; as malformed string →
  falls back via `_safe_json_parse_full` → `{}` (no crash).
- E2: structure/files uploads base64-encoded like create.

**E-graceful**
- E3: `user_id` missing → `401`.
- E4: `assert_doc_editable` import/lookup raises → **fail-open** (edit allowed,
  job queued) — assert it does NOT 500.

**M-tests**
- M1 (integrity, **critical**): doc is in governance review →
  `assert_doc_editable` returns `(False, reason, _)` → `403 {"error": reason}`.
  Locked reports cannot be modified.
- M2 (integrity): requires `.edit`; normal user without it → `403`.
- M3 (integrity): framework-policy merge is additive (existing `policy_ids`
  preserved, deduped) — assert no silent dropping of caller-supplied policies.

---

## 9. `GET /runbook/results/<runbook_id>` — `get_runbook_results`

**UC**: Return all *valid-status* results for a runbook that the caller owns,
plus a defensive shared-access fallback that surfaces results explicitly shared
with the caller (and the runbook definition). Attaches workflow state.

**P-tests**
- P1: owner with valid results → `200 {success, results:[...sorted by
  ended_at desc...], runbook}`.
- P2: results filtered to `{completed,success,done,draft}` only — a `running`/
  `failed` row is excluded.
- P3: `runbook_details` returned as a list → normalized to first element.
- P4: workflow state attached to each result via
  `get_workflow_states_for_docs`.

**E-func**
- E1: owner has no valid results but a **share entry with matching runbook_id**
  exists → shared results surfaced, `shared=True`/`shared_by` stamped, runbook
  backfilled from owner.
- E2: **legacy share entry** (no `runbook_id`) → fetched individually via
  `runbook_get_result_by_id`, kept only if its `runbook_id` matches.
- E3: `user_id` is space-/dot-padded (`" abc. "`) → trimmed
  (`.strip().rstrip(".")`) before parse.
- E4: double-encoded JSON in results → `normalize_json` recursively decodes.

**E-graceful**
- E5: `user_id` resolves empty → `401`.
- E6: `get_user_shared_reports` returns a non-dict / malformed shape → coerced
  to `{}`, endpoint still `200` (no 500 cascade — the comment explicitly guards
  the "session-expired loop").
- E7: a per-owner shared fetch raises → logged warning, that owner skipped,
  endpoint still `200`.
- E8: `get_workflow_states_for_docs` raises → swallowed, results returned
  without `workflow_state`.
- E9: top-level dbserver call raises → `500 {"error":"Failed to fetch runbook
  results","details": ...}` (structured).

**M-tests**
- M1 (confidentiality, **critical**): shared fallback keeps **only** result_ids
  in `authorized_shared_ids` — feed a runbook owned by `main_user` that has two
  results, only one shared → the sibling (unshared) result is NOT returned.
- M2 (confidentiality): results owned by another user with no share and no
  matching entry are never returned.
- M3 (availability): malformed `shared_reports` cannot 500 the endpoint (E6
  asserts the documented guard).

---

## 10. `GET /runbook/results_list/<user_id>` — `result_list`

**UC**: Aggregate every result the user should see: owned (valid status,
non-zero risk, known parent runbook), explicitly shared, and assigned-for-review
(workflow party). Returns results + runbook definitions, with workflow state.

**P-tests**
- P1: owned valid results with risk>0 and known runbook → returned.
- P2: explicitly-shared results → returned with `shared`/`shared_by`, runbook
  backfilled.
- P3: assigned-for-review docs (QR/GR/Approver) → returned with
  `assigned_for_review=True`, `assigned_role`, parent runbook unioned.
- P4: workflow state attached; results sorted by `ended_at` desc.

**E-func**
- E1: owned result with `risk_score==0` and **not shared** → excluded
  (`_keep` gate); same result if **shared** → included.
- E2: owned result whose `runbook_id` is not in known runbooks and not shared →
  excluded.
- E3: legacy share entry missing `runbook_id` → backfilled from fetched result.
- E4: duplicate runbook ids not added twice (`added_runbook_ids`).

**E-graceful**
- E5: `get_user_shared_reports` non-dict → coerced `{}`.
- E6: a shared/assigned fetch raises → warning, skipped, endpoint still `200`.
- E7: workflow assignment lookup raises → swallowed, owned/shared still
  returned.
- E8: top-level failure → `500 {"error": ...}`.

**M-tests**
- M1 (confidentiality): a result neither owned nor shared nor assigned is never
  returned (`_keep` + union boundaries).
- M2 (confidentiality): assigned-for-review union skips items where
  `owner_id == user_id` and items already seen (no duplication / no
  self-leak).
- M3 (integrity): the `risk_score>0` gate prevents draft/test (score 0) SU-mode
  runs from surfacing unless explicitly assigned/shared.

---

## 11. `GET /runbooks/list/<user_id>` — `list_runbooks`

**UC**: Return all runbooks owned by the user plus shared runbook definitions.

**P-tests**
- P1: owned runbooks + shared runbooks (stamped `shared`/`shared_by`) → `200
  {success, runbooks:[...]}`.
- P2: shared entry uses `runbook_id` (falls back to share key) to fetch from
  owner.

**E-func**
- E1: shared record returned as list → first element used.
- E2: user with no shares → only owned runbooks.

**E-graceful**
- E3: a shared-runbook fetch raises → warning, skipped, others still returned.
- E4: empty/falsy `user_id` path → `401`. (Note: route param is required so this
  is the `not user_id` guard.)

**M-tests**
- M1 (confidentiality): only runbooks owned by the user or explicitly shared
  (via `shared_reports`) appear; an arbitrary owner's runbook is never pulled.
- M2 (availability, **finding-candidate**): unlike sibling routes, the bare
  `dbserver.get_all_runbooks` / `get_user_shared_reports` calls here are **not**
  wrapped in try/except → an exception yields a raw framework 500 (HTML), not a
  JSON envelope. Test asserts current behavior and flags it (`security`/
  `graceful` marker) as the only read route lacking the structured-error guard.

---

## 12. `GET /runbook/<runbook_id>/<user_id>` — `get_runbook`

**UC**: Fetch a single runbook by id for the user; if not owned, fall back to a
shared entry and fetch from the owner. Normalizes JSON.

**P-tests**
- P1: owned runbook → `200 {success:true, runbook}`.
- P2: not owned but shared → fetched from `main_user_id`, stamped
  `shared`/`shared_by`.
- P3: list response normalized to first element; nested JSON normalized.

**E-graceful**
- E1: empty `user_id` (and no session) → `401`.
- E2: not found anywhere → `404 {success:false, error:"Runbook not found"}`.
- E3: dbserver raises → `500 {success:false, error:"Failed to fetch runbook",
  details}` (structured).

**M-tests**
- M1 (confidentiality, **critical**): runbook not owned and **not** in the
  user's `shared_reports` → `404` (NOT served). A user cannot fetch an arbitrary
  `runbook_id` belonging to someone else.
- M2 (confidentiality): shared fallback matches on both `type=="runbook"` AND
  `runbook_id` — a share of a *different* runbook does not unlock this one.

---

## 13. `GET /allrunbook/<user_id>` — `get_all_runbook`

**UC**: Return every runbook for the user (`get_user_runbook`) plus a count.

**P-tests**
- P1: → `200 {result:[...], all:<len>}`.

**E-graceful**
- E1: dbserver raises → `500 {"error": ...}`.

**M-tests**
- M1 (confidentiality): results scoped to the parsed `user_id` only.
- M2 (availability): `len(result)` on a `None` return — test that an empty/None
  return is handled (currently `len(None)` would raise → caught as 500; assert
  structured envelope, flag as edge).

---

## 14. `DELETE /runbook/delete/<runbook_id>` — `delete_runbook`

**UC**: Delete a runbook and its results for the user; audited
(`RUNBOOK_DELETED`).

**P-tests**
- P1: valid → `200 {success:true}`; both `delete_runbook` and
  `delete_runbook_result` called.
- P2: audit emitted.

**E-graceful**
- E1: no `user_id` (session/query) → `401`.
- E2: dbserver raises → `500 {"error": ...}`.

**M-tests**
- M1 (integrity): requires `.delete`; normal user without it → `403`.
- M2 (integrity): viewer-delegated admin cannot DELETE (X4) → `403`.
- M3 (confidentiality/integrity): delete scoped to parsed `user_id` — a caller
  cannot delete by passing only `runbook_id` without owning it (deletion is
  keyed on `(user_id, runbook_id)` in dbserver; assert user_id is passed).

---

## 15. `POST /runbook/delete_all` — `delete_all`

**UC**: Bulk-delete a list of runbooks; audited (`RUNBOOK_BULK_DELETED`).

**P-tests**
- P1: list of ids → `200 {success:true, deleted_ids:<count>}`;
  `delete_all_runbook` called with `(user_id, ids)`.
- P2: audit metadata caps `runbook_ids` to first 10.

**E-func**
- E1: empty `runbook_id` list → `200 deleted_ids:0` (no-op, not error).

**E-graceful**
- E2: missing `user_id` → `401`.
- E3: dbserver raises → `500 {"error": ...}`.

**M-tests**
- M1 (integrity): requires `.delete`.
- M2 (integrity): viewer delegation blocked (X4).

---

## 16. `DELETE /runbook/delete_result` — `delte_result`

**UC**: Delete a single result of a runbook by `(user_id, runbook_id,
result_id)`.

**P-tests**
- P1: valid → `200 {success:true}`; `delete_runbook_result_by_id` called with
  all three ids.

**E-graceful**
- E1: missing `user_id` → `401`.
- E2: dbserver raises → `500 {"error": ...}`.

**M-tests**
- M1 (integrity): requires `.edit`.
- M2 (integrity): deletion keyed on parsed `user_id` (cannot delete another
  user's result by id alone).

---

## 17. `POST /runbook/update/<runbook_id>` — `update_runbook_api`

**UC**: Patch arbitrary runbook fields (except protected `runbook_id`/`user_id`),
normalizing nested dict/list values to JSON strings. Audited.

**P-tests**
- P1: valid updates → `200 {success:true, runbook:<updated>}`;
  `update_runbook` called with normalized payload.
- P2: nested dict/list values JSON-encoded before persist.
- P3: audit metadata lists `fields_updated`.

**E-func**
- E1: scalar values pass through unchanged.

**E-graceful**
- E2: missing `user_id` → `401`.
- E3: dbserver raises → `500 {success:false, error}` (structured).

**M-tests**
- M1 (integrity, **critical**): caller cannot overwrite `runbook_id` or
  `user_id` — both are stripped from `updates`. Test that supplying them is
  ignored (re-ownership / id-reassignment blocked).
- M2 (integrity): requires `.edit`; viewer delegation blocked (X4).
- M3 (confidentiality): update keyed on parsed `user_id`.

---

## 18. `DELETE /runbook/results_delete/<runbook_id>` — `delete_runbook_results`

**UC**: Delete all results for a runbook (user from **session only**).

**P-tests**
- P1: session user set → `200 {success:true}`; `delete_runbook_result` called.

**E-graceful**
- E1 (**finding-candidate**): `user_id` read from `session.get("user_id")`
  *only* — not from body/query. With no Flask session, → `401`. But the
  decorator reads user_id from query/body, so it's possible to pass the
  permission gate (via `?user_id=`) yet hit `401` in the handler (session
  empty). Test documents this **inconsistency** and asserts the `401`.
- E2: dbserver raises → `500 {"error": ...}`.

**M-tests**
- M1 (integrity): requires `.edit`.

---

## 19. `POST /create_playbook_runbook` — `create_playbook_runbook`

**UC**: Trigger (synchronously) creating/regenerating a runbook from a playbook
via `trigger_runbook_from_playbook`.

**P-tests**
- P1: valid `user_id`+`playbook_id` (+optional `runbook_id`) → `200 {status:
  <result>}`.

**E-graceful**
- E1: missing `user_id` or `playbook_id` → `400 "Missing user_id or
  playbook_id"`.
- E2: `trigger_runbook_from_playbook` raises → `500 {"error": ...}`.

**M-tests**
- M1 (integrity): requires `.create`.
- M2 (integrity): the trigger runs for the parsed `user_id` (composite handled).

---

## 20. `GET /runbook/check_playbook/<playbook_id>` — `check_playbook_runbook`

**UC**: Check whether a runbook already exists for a given playbook (user from
**session**).

**P-tests**
- P1: runbook exists → `200 {status:true, result}`.

**E-graceful**
- E1: no session `user_id` → `401`.
- E2: no runbook for playbook → `400 {status:false, message:"No runbook is
  present"}` (note: 400, not 404 — by design).
- E3: dbserver raises → `500 {"error": ...}`.

**M-tests**
- M1 (read-gate): requires `.read`.
- M2 (confidentiality): lookup scoped to session user_id.

---

## 21. `GET /result/<result_id>` — `result_by_id`

**UC**: Fetch a single runbook result by id (user from **session**).

**P-tests**
- P1: → `200 {result: <res>}`.

**E-func**
- E1: result not found → dbserver returns `{"status":"not_found"}` → returned
  as-is in `{result:...}` with `200` (no 404 in current code — documented).

**E-graceful**
- E2: dbserver raises → `500 {"error": ...}`.

**M-tests**
- M1 (read-gate): requires `.read`.
- M2 (confidentiality, **finding-candidate**): user comes from
  `session.get("user_id")` only; result is fetched for that user's space, so a
  cross-user read requires owning the space. Test that fetch uses the parsed
  session user (not the path `result_id`'s owner) → cross-user read returns the
  caller's (likely empty) space, not the victim's data.

---

## 22. `PUT /result/<result_id>/evidence_analysis` — `patch_evidence_analysis`

**UC**: Patch a single item (by index) inside a result's `evidence_analysis`
array, deep-merging dict fields. Persists and audits
(`RUNBOOK_EVIDENCE_UPDATED`).

**P-tests**
- P1: valid index + updates → `200 {success, message}`; `update_runbook_result`
  persisted with merged item.
- P2: dict-valued update key deep-merges into existing dict; scalar replaces.
- P3: `evidence_analysis` as a **list** vs as a **dict with `items`** — both
  shapes handled, persisted back in the same shape.
- P4: audit emitted with `index_updated` + `fields_changed`.

**E-graceful**
- E1: no `user_id` (session+body) → `400 "user_id required"`.
- E2: `index` is None or `updates` not a dict → `400`.
- E3: result not found / `not_found` → `404 "Result not found"`.
- E4: no `evidence_analysis` (neither dict nor list) → `404 "No evidence_
  analysis in result"`.
- E5: index out of range (negative or ≥ len) → `400 "Index N out of range"`.
- E6: dbserver/update raises → `500 {"error": ...}` (logged via
  `logger.exception`).

**M-tests**
- M1 (integrity, **critical**): out-of-range / negative index cannot write
  past the array (E5) — no IndexError, no silent append.
- M2 (integrity): requires `.edit`; viewer delegation blocked (X4).
- M3 (integrity): the merge only touches `items[index]`; sibling items
  unchanged (assert other indices identical after patch).
- M4 (confidentiality): result fetched/updated for parsed `user_id` space only.

---

## 23. `POST /result/<result_id>/evidence_admissibility` — `toggle_evidence_admissibility`

**UC**: Move an evidence file between `admissible`/`inadmissible` in the source
playbook's `evidence_overview`, persist to S3, update the runbook's
`runbook_evidence_config` decisions, and trigger async report regeneration.
Audited. Returns `202`.

**P-tests**
- P1: valid toggle inadmissible→admissible → `202 {success, message:"Report
  regeneration started", file, new_status, affected_artifacts}`; S3 saved,
  `update_runbook` called, `create_playbook_runbook_task.delay` invoked.
- P2: admissible→inadmissible path mirrors P1; decision flag set to
  `still_admissible`.
- P3: existing `runbook_evidence_config` entry for the artifact is updated
  (not duplicated); missing entry is appended.
- P4: audit `RUNBOOK_EVIDENCE_ADMISSIBILITY_CHANGED` emitted.

**E-func**
- E1: file present in multiple artifacts → all affected artifacts moved; empty
  source artifacts pruned (`_toggle_file_in_overview` STEP 2).
- E2: file path is an `http(s)` URL or a dict `{file: ...}` →
  `_normalize_file` basename match still works.
- E3: `runbook_evidence_config` is malformed JSON → falls back to `[]` (no
  crash).

**E-graceful**
- E4: no `user_id` → `400`.
- E5: missing `file` or invalid `target_status` → `400`.
- E6: result not found → `404`.
- E7: result has no `base_playbook_id` (not playbook-based) → `400 "Result is
  not from a playbook-based execution"`.
- E8: playbook not found → `404 "Playbook data not found"`.
- E9: file not present in source list → `_toggle_file_in_overview` raises
  `ValueError` → caught → `400 {"error":"File not found in ... evidence"}`
  (graceful, not 500).
- E10: S3/update/task raises → `500 {"error": ...}`.

**M-tests**
- M1 (integrity): requires `.edit`; viewer delegation blocked (X4).
- M2 (integrity, **critical**): `target_status` strictly validated to
  `{admissible, inadmissible}` at both the route and `_toggle_file_in_overview`
  — arbitrary status rejected `400` (no creation of rogue overview keys).
- M3 (availability): toggling a file already in the target (idempotency) →
  ValueError "File not found in source" → `400`, not a crash or duplicate.
- M4 (confidentiality): playbook + runbook fetched/written for parsed `user_id`
  space only.

---

## 24. `POST /result/<result_id>/rename` — `rename_runbook_result`

**UC**: Set `report_name` inside a result's stored blob.

**P-tests**
- P1: valid `name` → `200 {success:true}`; `update_runbook_result` persisted
  with `report_name` set.
- P2: name is trimmed of surrounding whitespace.

**E-graceful**
- E1: missing `user_id` → `401`.
- E2: empty/whitespace `name` → `400 "name is required"`.
- E3: result not found → `404 "result not found"`.
- E4: dbserver raises → `500 {success:false, error}`.

**E-func**
- E5: stored `result` blob is non-dict / unparseable → coerced to `{}` then
  `report_name` set (no crash).

**M-tests**
- M1 (integrity): requires `.edit`.
- M2 (integrity): rename only mutates `report_name`; the rest of the blob is
  preserved (assert other keys intact).
- M3 (availability): XSS/script string as name is stored verbatim (no
  server-side execution) and round-trips — assert it's persisted as data, not
  interpreted. (Output-encoding is a frontend concern; flag for `llm_safety`/
  `api_security` awareness.)

---

## 25. `POST /schedule_runbook` — `schedule_runbook`

**UC**: Save a runbook execution schedule (frequency/timezone/start). Audited
(`RUNBOOK_SCHEDULED`).

**P-tests**
- P1: valid `scheduledActivation.frequency` → `200 {status:"success",
  runbook_id, schedule_type, scheduler_result}`; `save_runbook_schedule`
  called with normalized data.
- P2: audit emitted.
- P3: default timezone `"UTC"` applied when omitted.

**E-graceful**
- E1: missing `frequency` → `400 "Missing frequency"`.
- E2 (**finding-candidate**): `body["user_id"]` / `body["runbook_id"]` accessed
  via **subscript** (not `.get`) — missing keys raise `KeyError` → unhandled
  (no try/except in this route) → raw 500. Test documents this and asserts the
  failure mode (so adding `.get` + 400 later is a tracked improvement).

**M-tests**
- M1 (integrity): requires `.execute`.
- M2 (integrity): schedule saved for parsed `user_id`.
- M3 (availability): `save_runbook_schedule` raising propagates uncaught
  (no try/except) → flagged: scheduling failures should return structured JSON,
  not a worker stacktrace.

---

## 26. `POST /runbook/structure_extract` — `structure_extract`

**UC**: Accept JSON or multipart (+optional `structure_file`), enqueue
`execute_structure_extract` (background), return `job_id` (`queued`).

**P-tests**
- P1: JSON body with `user_id` → `200 {success, job_id, status:"queued"}`.
- P2: multipart with `structure_file` → file base64-encoded into payload before
  submit.

**E-graceful**
- E1: missing `user_id` → `401`.
- E2: `JobManager.submit_job` raises → (no try/except here) propagates → flagged
  as ungraceful; test asserts current behavior.

**M-tests**
- M1 (integrity): requires `.create`.
- M2 (availability): large structure file still returns `job_id` promptly (work
  deferred).

---

## 27. `POST /runbook/structure_extract_modify` — `structure_extract_modify`

**UC**: Persist a (possibly edited) `default_structure` as the runbook's
`structure_theme`.

**P-tests**
- P1: valid → `200 {success:true, data:<updated_row>}`; `update_runbook` called
  with `structure_theme=json.dumps(default_structure)`.

**E-func**
- E1: `default_structure` None → stored as JSON `null` (no crash).

**E-graceful**
- E2: dbserver raises → `500 {"error": ...}`.

**M-tests**
- M1 (integrity): requires `.edit`; viewer delegation blocked (X4).
- M2 (confidentiality): update keyed on parsed `user_id`.

---

## 28. `POST /check_pb_output` — `check_pb_output`

**UC**: Preview a playbook's extracted Q&A and (if the runbook has reference
sources) the analyzed/merged document data, without persisting a run.

**P-tests**
- P1: runbook **with** `reference_sources` → `200 {status:"completed",
  questions, final:[...]}`; analyze+merge invoked.
- P2: runbook **without** `reference_sources` → `200` with `final:[]` and
  `runtime_input` set from instruction chat.

**E-func**
- E1: runbook returned as list → first element; as JSON string → parsed.

**E-graceful**
- E2: dbserver/helper raises → `500 {"error", "trace"}` (structured).
- E3 (**finding-candidate**): if `runbook` resolves to `None` (bad rb_id),
  `runbook.get(...)` raises `AttributeError` → caught by the broad except →
  `500`. Test asserts structured envelope and flags that a missing runbook
  should arguably be `404`.

**M-tests**
- M1 (read-gate): requires `.read`.
- M2 (confidentiality): playbook + runbook fetched for parsed `user_id` only.
- M3 (availability): broad `except` guarantees no raw stacktrace leaks to the
  client (always JSON).

---

## 29. Worker coroutines (out-of-band, unit-tested separately)

These are not HTTP routes but carry the real business logic the routes enqueue.
They get their own unit tests (mirroring `test_workflow_runbook_trigger.py`
style — construct inputs, mock `dbserver`/`emit`/S3/CloudWatch):

- `execute_runbook_create`: template fallback when no structure file; CloudWatch
  URL parse failure → raises; `logs`/`api`/`playbook` branch selection;
  `is_template` string normalization; success vs failure WS/notification emit.
- `execute_modify_runbook`: existing-template load, list/str normalization,
  playbook-evidence stash, structure file vs existing-structure branches,
  exception → error emit (no re-raise).
- `execute_structure_extract`: default-structure fallback, file vs default
  branch, `main` flag success/progress emit, exception → `{success:false}`.
- Pure helpers: `normalize_json` (recursive/double-encoded/invalid),
  `extract_filenames` (dict/list/None/non-dict), `_normalize_file`
  (url/dict/plain/None), `_toggle_file_in_overview` (move, prune, dedupe,
  not-found ValueError, invalid status).

---

## 30. Summary of confidentiality / integrity / availability findings surfaced

The plan deliberately includes tests that **document current weak spots** so
they're tracked (each tagged `security`/`authz`):

| # | Endpoint | Concern | CIA |
|---|----------|---------|-----|
| F1 | `GET /runbook/status/<job_id>` | no permission gate, no ownership check on job_id | Confidentiality |
| F2 | `GET /runbooks/list/<user_id>` | no try/except → raw 500 (HTML) on dependency failure | Availability |
| F3 | `POST /schedule_runbook` | `body["user_id"]`/`["runbook_id"]` subscript + no try/except → KeyError/raw 500 | Availability |
| F4 | `POST /runbook/structure_extract` | submit failure not wrapped → ungraceful | Availability |
| F5 | `GET /result/<result_id>`, `.../results_delete` | user from session only → inconsistent with decorator's body/query resolution | Confidentiality/Availability |
| F6 | `POST /check_pb_output`, `GET /allrunbook` | missing runbook / `len(None)` → 500 where 404/handled would be correct | Availability |
| F7 | `permission_required_body` | admin targeting a non-existent owner → `owner["user_type"]` on `None` → raw 500 (X8) | Availability |
| F8 | `POST /result/<id>/evidence_admissibility` | `playbook_id` used unsanitized as S3 key → cross-tenant key traversal (S1) | Confidentiality |
| F9 | `execute_modify_runbook` / `execute_runbook_create` | `result_id`/`runbook_id` unsanitized in `/tmp/...json` path (S2) | Integrity/Availability |
| F10 | `normalize_json` | unbounded recursion on nested/double-encoded JSON → RecursionError (S5) | Availability |
| F11 | `assign/revoke/delete_all/create_playbook_runbook/check_pb_output` | `request.get_json()` w/o `silent=True` → `None.get` on empty body (S7) | Availability |
| F12 | `PUT /result/<id>/evidence_analysis` | `index` not type-validated → `TypeError` 500 on string index (S9) | Availability |

These are asserted as *current behavior* (so the suite is green today) and
marked so a follow-up hardening PR can flip them to the desired behavior with
the test as the spec.

---

## 32. Security payloads — injection / path-traversal / DoS / malformed body

A dedicated class (`TestRunbookSecurityPayloads`, markers `security`,
`api_security`, `payload`, `cwe`, `owasp`) covering attack-surface dimensions the
behavioral sections above intentionally skipped. Several assert *current*
(possibly vulnerable) behavior and are tagged so a hardening PR can flip them.

### 32a. Path traversal (CWE-22) — **highest priority**
- S1 (**finding-candidate**): `evidence_admissibility` builds an **S3 key** as
  `filename = playbook_id if playbook_id.endswith(".json") else f"{playbook_id}.json"`.
  Supply `playbook_id="../../tenant-b/secret"` → assert what key
  `save_playbook_to_s3` is called with. The test pins the call args and flags
  that `playbook_id` is unsanitized → cross-tenant S3 key traversal risk.
- S2 (**finding-candidate**): `execute_modify_runbook` writes
  `/tmp/structure_file_{result_id}.json`; `execute_runbook_create` writes
  `/tmp/structure_file_{runbook_id}.json` and `/tmp/{filename}`. Supply
  `result_id="../../../tmp/evil"` → assert the resolved `os.path.join("/tmp",
  ...)` path; document that traversal outside `/tmp` is possible. (Worker-level
  test in the §29 suite, driven with a mocked `open`/`upload_any_file`.)
- S3: positive control — a benign `result_id`/`playbook_id` produces a path/key
  strictly under the intended prefix (guards a future fix from over-reaching).

### 32b. SQL injection (CWE-89) — positive control
- S4: `assign_runbook` issues `SELECT ... WHERE user_id=%s OR email=%s` and the
  decorator issues `WHERE user_id=%s`. Drive `target_user_id` /`user_id` with
  `"' OR '1'='1"` and assert the value is passed as a **bound parameter** (the
  mocked cursor records `execute(query, params)` with the payload in `params`,
  never interpolated into the query string). Proves parametrization holds.

### 32c. Recursion / DoS via `normalize_json` (CWE-674)
- S5 (**finding-candidate**): `normalize_json` recurses on nested dict/list and
  on double-encoded JSON strings with **no depth cap**. Feed a result/runbook
  whose field is a deeply-nested (or deeply double-encoded) JSON value through
  `get_runbook_results` / `get_runbook`. Assert that beyond a threshold it
  raises `RecursionError` → caught by the route's broad `except` → structured
  `500` (so it degrades to a handled error, not a worker crash). Documents that
  there is no input-size guard. Use a depth safely below the interpreter limit
  for the "still works" case and one above for the "degrades gracefully" case.
- S6 (E-func control): moderately nested double-encoded JSON normalizes
  correctly (already in §9 E4; reused here as the safe baseline).

### 32d. Malformed / wrong-content-type body
Routes that call `request.get_json()` **without** `silent=True` then `.get(...)`:
`assign`, `revoke`, `delete_all`, `create_playbook_runbook`, `check_pb_output`.
- S7 (**finding-candidate**): POST with `Content-Type: application/json` but an
  **empty/invalid** body → `get_json()` returns `None` → `None.get(...)` →
  `AttributeError`. For routes with a `try/except` (assign/revoke/delete_all/
  check_pb_output) → structured `500`; for `create_playbook_runbook` (no
  try/except around `data.get`) → raw 500. Parametrized; asserts the actual
  status per route and flags the ungraceful ones.
- S8: POST with a **non-JSON content-type** (e.g. `text/plain`) → Flask
  `get_json()` raises `415`/returns None → assert the route doesn't leak a
  stack trace to the client.

### 32e. Type-confusion
- S9 (**finding-candidate**): `patch_evidence_analysis` with `index` as a
  **string** (`"0"`) → `0 <= "0" < len(items)` raises `TypeError` (Py3) →
  caught → `500`. Asserts current behavior; flags that `index` should be
  coerced/validated to `int` and rejected with `400`.
- S10: `delete_all` with `runbook_id` as a **string** instead of a list →
  `len("abc")` succeeds (3) and `delete_all_runbook` receives a string →
  document the unintended shape (should validate `isinstance(list)`).

### 32f. HTTP-method hygiene
- S11: calling a route with the wrong verb (e.g. `GET /runbook/assign`) →
  Flask `405 Method Not Allowed`. One representative parametrized check.

### 32g. Idempotency / duplicate submission (`idempotency` marker)
- S12: double-POST of `evidence_admissibility` for the same file+status →
  second call (file already in target) raises `ValueError "File not found in
  source"` → `400`; assert `create_playbook_runbook_task.delay` is **not** fired
  the second time (no duplicate regeneration storm).
- S13: repeated `DELETE /runbook/delete/<id>` is safe (idempotent) — second
  delete still returns `200`/`success` (dbserver delete of a missing row is a
  no-op).

---

## 33. Coverage matrix (criterion × dimension)

Confirms no criterion/dimension is left empty after the additions:

| Dimension | Where covered |
|-----------|---------------|
| UC (what it does) | §1–28 "UC" line each |
| Primary success | §1–28 P-tests |
| Edge — functional | §1–28 E-func |
| Edge — graceful failure | §1–28 E-graceful + §30 findings |
| Misuse — Confidentiality | §1–28 M (conf) + X3, X5 + §32a |
| Misuse — Integrity | §1–28 M (integ) + X2, X4, X6, X7 + §32b/e |
| Misuse — Availability | §1–28 M (avail) + X1, X8 + §32c/d/f/g + §30 |
| Auth-decorator branches | X1–X8 |
| Worker logic | §29 |

---

## 34. Markers & layout

- File: `tests/integration/runbook/test_runbook_routes_api.py`
- Markers: `@pytest.mark.integration` on all; `@pytest.mark.security` /
  `@pytest.mark.authz` on M-tests; `@pytest.mark.smoke` on the P-tests.
- One test class per endpoint (`TestAssignRunbook`, `TestRevokeRunbook`, …) for
  readability and selective runs.
- Worker-coroutine + pure-helper tests: `tests/unit/runbook/test_runbook_workers.py`.
- Security payloads: `TestRunbookSecurityPayloads` in the route test module
  (§32), markers `security`/`api_security`/`payload`/`cwe`/`owasp`/`idempotency`.

**Estimated count**: ~190 HTTP-route tests + ~25 security-payload/decorator-branch
tests (§32 + X5–X8) + ~40 worker/helper tests ≈ **255**.
