# Frontend Contract — Editable Report Risk Levels (Manual Overrides)

> Hand this to the frontend (or paste into Lovable) to align the UI with the
> backend. Every field/endpoint below matches the deployed backend exactly.
> All routes are on the **same origin/BACKURL** the app already uses, with
> `credentials: 'include'`. Auth/permissions are unchanged from existing calls.

## What this enables

Users can manually override:
- the **overall report risk** (the "High · 55" chip in the Workspace list and the
  "High Risk · Score 55 / 25" header on the report), and
- each **individual finding** in the Risk Analysis section (its level / score / impact /
  likelihood).

Overrides persist: they survive re-runs and the Assessment Runbook Chat modify flow.

> ⚠️ **This is the launch gate.** Today the chip label is derived client-side from the
> numeric `risk_score` via `bandForScore()` (see `useRiskConfig.ts`). Until the frontend
> **honors the new override flags below**, a manual edit to a *label* will be invisible
> (the UI will re-derive it from the score). The backend is already deployed; the feature
> ships only when this frontend change lands.

---

## 1. Data shape additions (already returned by the backend)

`result.risk_analysis` now also carries:

```jsonc
{
  "risks": [
    {
      "finding_id": "weak-mfa",     // NEW — stable id; use as the edit key
      "threat": "...",
      "vulnerability": "...",
      "impact": 4,
      "likelihood": 4,
      "risk_score": 16,
      "risk_level": "High",
      "overridden": true             // NEW — finding manually overridden
    }
  ],
  "final_risk_score": 55,
  "risk_level": "High",
  "risk_overridden": true,           // NEW — overall risk manually overridden
  "rev": 3,                          // NEW — optimistic-lock counter (send back on edit)
  "dropped_overrides": [             // NEW — overrides lost on the last regen (may be absent)
    { "finding_id": "...", "threat": "...", "risk_score": 99, "risk_level": "Critical" }
  ],
  "max_score": 25,
  "config": { "impact_scale": 5, "likelihood_scale": 5, "bands": [ ... ] }
}
```

The denormalized per-report `risk_score` (used by the Workspace chip) is kept in sync by
the backend, so `refreshAllRadarData()` after an edit updates the badge correctly.

---

## 2. Rendering rule change — honor the override flags

Centralize the label/score resolution so all three render sites agree. Add a helper:

```ts
// resolves what to DISPLAY, honoring manual overrides
function resolveRisk(score: number, level: string | undefined, overridden: boolean,
                     bands: RiskBand[]) {
  if (overridden && level) {
    const band = bands.find(b => b.label === level);     // color by LABEL, not score
    return { label: level, score, color: band?.color };
  }
  const band = bandForScore(score, bands);               // existing behavior
  return { label: band?.label ?? "Unknown", score, color: band?.color };
}
```

Apply it at the three sites the explorer identified:

| Site | File | Change |
|------|------|--------|
| Workspace report chip | `src/components/radar/WorkspaceReportItem.tsx` (~157–167) | Use `resolveRisk(riskScore, latestReview.risk_level, latestReview.risk_overridden, bands)`. Requires the workspace list to surface `risk_level` + `risk_overridden` per report (see §6). |
| Report overall header | `src/components/radar/results/RadarStructuredResult.tsx` (~242–261) | Use `resolveRisk(ra.final_risk_score, ra.risk_level, ra.risk_overridden, bands)`. |
| Per-finding rows | `src/components/radar/results/RadarStructuredResult.tsx` (~284–306) | Use `resolveRisk(risk.risk_score, risk.risk_level, risk.overridden, bands)` per row. |

Add an **"edited" affordance** (e.g. a small pencil/dot badge or tooltip "Manually set")
wherever `overridden`/`risk_overridden` is true, so users can distinguish manual values
from computed ones.

---

## 3. Edit endpoint — `PUT /result/{result_id}/risk_analysis`

Mirror the existing evidence-edit fetch (`PATCH /result/{id}/evidence_analysis` in
`RadarStructuredResult.tsx`), including the `withWsSession()` wrapper and
`credentials: 'include'`. **Permission:** `compliance.runbook.edit` (same as evidence).

### Request body (all sections optional; send only what changed)
```jsonc
{
  "user_id": "<active user id>",
  "expected_rev": 3,                                  // current ra.rev (omit to skip the lock)
  "report":   { "risk_level": "Critical", "final_risk_score": 60 },
  "findings": [
    { "finding_id": "weak-mfa", "risk_level": "High", "risk_score": 64,
      "impact": 9, "likelihood": 8 }
  ],
  "clear": false
}
```
- **Override report:** send `report` with `risk_level` and/or `final_risk_score`.
- **Override a finding:** send a `findings[]` entry keyed by `finding_id` (falls back to
  `index` if you must). Any subset of `impact` / `likelihood` / `risk_score` / `risk_level`.
- **Revert (clear):** `report: {"clear": true}`, a finding `{"finding_id": "...", "clear": true}`,
  or top-level `"clear": true` to reset the whole analysis to computed values.

### Validation (backend enforces)
- `risk_level` must be one of the org's band labels (from `useRiskConfig`).
- `final_risk_score` / `risk_score`: `0 … impact_scale*likelihood_scale`.
- `impact`: `1 … impact_scale`; `likelihood`: `1 … likelihood_scale`.
- A label that doesn't fall in its score's band is **allowed** (intentional override) and
  flagged in `label_score_mismatch` of the response — optionally show a soft "label
  doesn't match score band" hint, but do not block.

### Responses
- **200** `{ "success": true, "risk_analysis": { ...updated, "rev": 4 }, "label_score_mismatch": false }`
  → replace local `result.risk_analysis` with the returned object (it has the new `rev`).
- **409** `{ "error": "stale_rev", "message": "...", "current_rev": 5 }`
  → someone edited since you loaded it. Refetch the result, re-apply the user's pending
  change onto the fresh `rev`, and re-submit (or prompt the user).
- **400** `{ "error": "<reason>" }` → show inline validation error.
- **404** `{ "error": "This report has no editable risk analysis" }` → report has risk
  analysis disabled; hide the edit affordances entirely (see §7).

---

## 4. Edit UI — reuse the evidence inline-edit pattern

Reuse the `editingSection` / `editedItems` / `savingSection` machinery already in
`RadarStructuredResult.tsx` (lines ~92–156, 319–543).

**Overall header:** a pencil next to the chip opens an editor with a level dropdown
(options = band labels from `useRiskConfig`) and a numeric score input. Save → PUT with
`{ report: {...}, expected_rev }`. Add a "Reset to computed" link → PUT `{ report: {clear:true} }`.

**Per-finding row:** a pencil per row opens inline editors for level (dropdown), score,
impact, likelihood. Save → PUT with `{ findings: [{ finding_id, ... }], expected_rev }`.
"Reset" → `{ findings: [{ finding_id, clear: true }] }`.

**Decision to make:** when the user edits `impact`/`likelihood`, either (a) let the user
also set the level/score explicitly (pure manual override — what the backend stores), or
(b) preview a recomputed score locally via `impact*likelihood` and the band, then send
that. The backend stores exactly what you send and stamps `overridden`, so (a) is the
simplest faithful path; (b) is nicer UX. Either works against the same endpoint.

---

## 5. Optimistic-lock (`rev`) flow

1. Read `result.risk_analysis.rev` when the report loads.
2. Send it as `expected_rev` on every PUT.
3. On **200**, store the returned `risk_analysis` (new `rev`) so the next edit uses it.
4. On **409**, refetch (`fetchRunbookResults`), tell the user "this report changed,
   re-applying", and retry once, or surface a conflict toast.

---

## 6. Workspace chip needs two more fields

The Workspace list (`GET /runbook/results_list/{user_id}` → `radarWorkspaceSlice`) already
carries the numeric `risk_score`. To honor an overridden *label* on the chip, the per-report
entry should also expose `risk_level` and `risk_overridden` (both live in
`result.risk_analysis`). Confirm the list payload includes them for each report; if the
slice currently maps only `risk_score`, extend the mapping. Then `WorkspaceReportItem`
uses `resolveRisk()` (§2). If these fields aren't present in the list response, the chip
falls back to `bandForScore(risk_score)` — i.e. score stays correct, only an overridden
*label* that disagrees with its band would look wrong on the list (acceptable degraded mode).

---

## 7. `dropped_overrides` warning

After a re-run or chat-modify, `result.risk_analysis.dropped_overrides` may list manual
edits the backend couldn't carry forward (a finding the new run no longer produced). When
present and non-empty, show a dismissible banner near the Risk Analysis section, e.g.
"N manual risk edit(s) couldn't be carried into the regenerated report" listing the
`threat` of each. This makes lost edits visible rather than silent. (With the new stable
`risk_id`, drops should be rare — they happen only when a finding truly disappears.)

---

## 8. Disabled-risk reports

If `result.risk_analysis` is null/absent (risk analysis was turned off for the runbook),
render no risk section and no edit affordances. The edit endpoint returns **404** for these.

---

## 9. State & refetch

- Local: replace `result.risk_analysis` in `RunbookResultsView` state with the PUT
  response (in-place, like evidence edits).
- After a successful edit that changes the overall score/level, `dispatch(refreshAllRadarData(userId))`
  so the Workspace badge updates (the backend already synced the denormalized `risk_score`).
- `useRiskConfig()` (React Query, `["riskConfig", userId]`) is unchanged — keep using its
  `bands` for the level dropdown options and for `resolveRisk()` color lookup.

---

## 10. Implementation checklist (frontend repo `bytoiddev`)

1. `src/services/runbookService.ts` — add `updateRiskAnalysis(resultId, body)` calling
   `PUT /result/{id}/risk_analysis` with `withWsSession()` + `credentials:'include'`.
2. Add a shared `resolveRisk()` helper (next to `bandForScore`) and a `bandForLabel()`.
3. `src/components/radar/results/RadarStructuredResult.tsx` — render via `resolveRisk()`;
   add overall-header and per-finding inline editors + "reset to computed"; handle 200/409/400;
   render the `dropped_overrides` banner; "edited" affordance.
4. `src/components/radar/WorkspaceReportItem.tsx` — render chip via `resolveRisk()`.
5. `src/store/slices/radarWorkspaceSlice.ts` (+ the list mapping) — surface `risk_level`
   and `risk_overridden` per report entry.
6. Thread `expected_rev` through edits; store returned `rev`.
7. Gate all edit affordances behind the user's `compliance.runbook.edit` permission and
   hide them when `risk_analysis` is absent.

---

## Quick reference

| Capability | Method & path | Permission |
|------------|---------------|------------|
| Get report results | `GET /runbook/results/{runbook_id}?user_id=` | `compliance.runbook.read` |
| Get risk config (bands) | `GET /risk-config?user_id=` | `compliance.runbook.read` |
| **Edit risk (new)** | `PUT /result/{result_id}/risk_analysis` | `compliance.runbook.edit` |
