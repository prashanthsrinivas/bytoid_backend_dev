# Frontend Contract — Report Review Cadence & Individual Report Names

> Hand this to the frontend (or paste into Lovable) to align the UI with the
> backend. Every field/endpoint below matches the deployed backend exactly.
> All routes are on the **same origin/BACKURL** the app already uses, with
> `credentials: 'include'`. Auth/permissions are unchanged from existing calls.

Two capabilities:
1. **Per-doc-type review cadence** — org admins pick a review frequency
   (3 / 6 / 9 / 12 months) **separately** for each category: `policy`,
   `runbook`, `report`. When a document is finalized (workflow → `published`),
   the backend stamps its next review date.
2. **Individual runbook report names** — each runbook report now has its own
   name (`<runbook> — <2-3 word descriptor>`), editable inline.

---

## 1. Review cadence settings

### Enum values (use these exact strings)
| value        | label (display) | interval_months |
|--------------|-----------------|-----------------|
| `3_months`   | `3 months`      | 3               |
| `6_months`   | `6 months`      | 6               |
| `9_months`   | `9 months`      | 9               |
| `12_months`  | `1 year`        | 12              |

Doc-type **categories** (the only valid `doc_type` values for cadence):
`policy` | `runbook` | `report`.
Note: `policy` governs all Policy Hub types (policy/procedure/standard).

### GET `/workflow/review-frequency`
Returns the cadence for one category plus a map of all three.

- **Query params:** `user_id` (required), `doc_type` (optional, default `policy`)
- **Permission:** `workflow.config.manage` (admins). 404 `{error}` if no org.
- **200 response:**
```json
{
  "org_id": "org_123",
  "doc_type": "runbook",
  "frequency": "6_months",
  "interval_months": 6,
  "cadences": { "policy": "12_months", "runbook": "6_months", "report": "3_months" },
  "options": [
    { "value": "3_months",  "label": "3 months", "interval_months": 3 },
    { "value": "6_months",  "label": "6 months", "interval_months": 6 },
    { "value": "9_months",  "label": "9 months", "interval_months": 9 },
    { "value": "12_months", "label": "1 year",   "interval_months": 12 }
  ]
}
```
**UI guidance:** render one cadence selector per category using `cadences` for
current values and `options` for the dropdown. A single GET (no `doc_type`)
returns everything you need (`cadences` + `options`); you don't need 3 calls.

### PUT `/workflow/review-frequency`
Sets the cadence for one category.

- **Body (JSON):**
```json
{ "user_id": "user_123", "frequency": "6_months", "doc_type": "runbook" }
```
  - `doc_type` optional (default `policy`).
  - `frequency` must be one of the enum values above → else **400**
    `{ "error": "frequency must be one of: 3_months, 6_months, 9_months, 12_months" }`.
- **200 response:**
```json
{ "status": "ok", "org_id": "org_123", "doc_type": "runbook",
  "frequency": "6_months", "interval_months": 6 }
```
Send one PUT per category the admin changes.

---

## 2. Where the review dates appear (read models)

`next_review_date` etc. are **stamped only when the doc is published** via the
workflow. Before that they are `null`/absent. Fields (all ISO `YYYY-MM-DD`
strings or enums):

| field                    | type            | meaning                              |
|--------------------------|-----------------|--------------------------------------|
| `review_frequency`       | enum string     | cadence applied at publish           |
| `review_interval_months` | int             | 3 / 6 / 9 / 12                        |
| `last_reviewed_at`       | date string     | when it was published/last reviewed  |
| `next_review_date`       | date string     | when it's next due                   |

### 2a. Runbook reports — GET `/runbook/results_list/<user_id>`
(Unchanged endpoint; new fields added.) Response shape:
```json
{
  "success": true,
  "runbook": [ { "runbook_id": "runbook_ab12", "name": "PIA", ... } ],
  "results": [
    {
      "result_id": "res_001",
      "runbook_id": "runbook_ab12",
      "status": "completed",
      "workflow_state": "published",
      "report_name": "PIA — Acme Billing",
      "review_frequency": "6_months",
      "review_interval_months": 6,
      "last_reviewed_at": "2026-05-29",
      "next_review_date": "2026-11-29",
      "result": { ... full report blob (also contains report_name + review_* ) ... }
    }
  ]
}
```
**Contract notes for the UI:**
- **`report_name`** is now a **top-level** field on each result — use it as the
  report's display name. It falls back to the runbook name for legacy reports,
  so it is always present. Do **not** derive the name from the runbook only.
- `review_frequency` / `review_interval_months` / `last_reviewed_at` /
  `next_review_date` are **top-level** too (may be `null` if not yet published).
  You no longer need to parse the `result` blob for these.
- Each `runbook` appears **once** (backend de-duplicates by `runbook_id`); group
  `results` under their runbook via `result.runbook_id === runbook.runbook_id`.

### 2b. Standalone reports — `users.reports` items
Standalone report objects (the ones listed in the AI reporting workspace) gain
the same four `review_*` fields at the top level of each report object, next to
`report_id` and `brief_summary`. `brief_summary` remains the report's name.

---

## 3. Renaming a report (already supported — wire the UI to it)

### Runbook report — POST `/result/<result_id>/rename`
- **Permission:** `compliance.runbook.edit`
- **Body:** `{ "user_id": "user_123", "name": "New report name" }`
  - empty `name` → **400** `{ "error": "name is required" }`
- **200:** `{ "success": true }` (writes `report_name` into the result blob)

### Standalone report — POST `/change_name`
- **Body:** `{ "user_id": "user_123", "report_id": "...", "name": "New name" }`
- **200:** `{ "success": true, "message": "..." }` (writes `brief_summary`)

---

## 4. How publishing triggers the stamp (no new FE action)

The review date is stamped automatically by the existing workflow when a
document transitions to `published` (via the existing `/workflow/submit`,
`/workflow/review`, `/workflow/approve`, `/workflow/publish` flows). The FE does
not call anything extra — just re-fetch the read model after a publish to show
the new `next_review_date`. Workflow doc keys (for reference):
- runbook report → `doc_type: "runbook"`, `doc_id: <result_id>`
- standalone report → `doc_type: "report"`, `doc_id: <report_id>`

---

## 5. Acceptance checklist for the FE
- [ ] Settings screen shows 3 cadence selectors (policy / runbook / report),
      pre-filled from `cadences`, options from `options`; PUT per change.
- [ ] Runbook report list uses top-level `report_name` as the title (distinct
      per report), with inline rename → `/result/<id>/rename`.
- [ ] Reports show `next_review_date` (and a "due/overdue" indicator vs today)
      when present; hidden/"—" when `null`.
- [ ] Standalone reports read `review_*` top-level fields + `brief_summary`.
- [ ] No client-side dedup of runbooks needed; trust the `runbook` array.
