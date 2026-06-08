# VRA Frontend Handoff Bundle

Ready-to-drop **reference** TypeScript for the Vendor Risk Assessment (VRA)
frontend. The live UI lives in the **bytoiddev** repo (not this backend repo);
these files are a starting point to copy into
`bytoiddev/src/features/vra/` and reconcile with that repo's actual HTTP client,
React Query setup, and design system.

## Files
- `types.ts` — TS interfaces mirroring the backend contract (authoritative).
- `vraApi.ts` — framework-agnostic API client (fetch-based, injectable). Handles
  the repo's `user_id` convention automatically.
- `hooks.ts` — `@tanstack/react-query` hooks + the scan-status polling rule.

## Backend conventions baked in
- **Auth/identity:** every call carries the composite `user_id`. GET requests
  send it as `?user_id=`; writes (POST/DELETE) send it in the JSON body. The
  client below does this for you. Requests use `credentials: 'include'` so the
  session cookie / bearer rides along (same as the rest of the app).
- **Base path:** endpoints are unprefixed (`/vra/...`) — set `baseUrl` to the
  API origin (e.g. `https://api.bytoid.ai`).

## Three integration rules (must honor)
1. **Dashboard route must be exactly `/vra/dashboard/:assessmentId`** — the
   backend embeds `VRA_DASHBOARD_BASE_URL + /vra/dashboard/<id>` into the report;
   a different route breaks the report's live link.
2. **FE prepends the two locked questions** (server-side injection is deferred):
   use `default_questions` from `createAssessment` (or `getDefaultQuestions`) and
   insert them as the first two builder questions, honoring `locked`/`required`.
3. **FE owns the trigger chain:** vendor fields complete → `setVendor` (title
   sync) → `runCollection`. Without this, OSINT auto-collection never fires.

## Risk-color consistency
Risk **text** comes from the backend (`overall_risk_rating`); the chip **color**
should run through the existing `useRiskConfig` band palette so VRA matches RADAR
visually. Do not introduce a second risk-color source.

## Permissions
Gate UI on the user's resolved perms: `vra.assessment.create` (builder toggle,
create, vendor, collect, link, report, delete), `vra.intelligence.read`
(assessment, evidence, analysis), `vra.dashboard.read` (dashboard).
