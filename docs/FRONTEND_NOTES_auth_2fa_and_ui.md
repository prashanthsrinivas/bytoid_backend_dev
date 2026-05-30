# Frontend Notes — 2FA Handling, Login Redirects & Two Console Fixes

> Hand this to the frontend (or paste into Lovable). It covers the backend auth
> changes the FE must react to, plus two frontend-only fixes for console errors
> seen on `/radar`. Backend routes/permissions are otherwise unchanged.

---

## 1. Handle `403 + totp_required` — do NOT treat it as "logged out"

The backend now enforces 2FA **server-side**. After a successful password login
for a user with 2FA enabled, the session is established but flagged pending, and
**every protected API call returns `403`** until the TOTP code is verified:

```json
// HTTP 403 from any protected endpoint while 2FA is pending
{
  "error": "Two-factor authentication required",
  "redirect": "/totp-verify",
  "totp_required": true
}
```

**Required FE behavior:**
- Your global error/interceptor must **distinguish 403 from 401**:
  - `401` → genuinely unauthenticated → may redirect to login.
  - `403` with `totp_required: true` → user is logged in but must finish 2FA →
    **route to `/totp-verify`, do NOT log out or hard-redirect to login.**
- On the `/totp-verify` page, don't fire protected data calls before the code is
  verified — they'll 403 by design. Just render the code form and call
  `POST /totp_verify` (that endpoint is not gated).
- After `POST /totp_verify` returns `{ "verified": true }`, the session is fully
  usable — re-fetch and proceed into the app.

**Why this matters:** previously the FE's 401 handler did
`window.location = "https://app.bytoid.ai/login"` (a hardcoded prod URL), which
bounced users off the 2FA page to the wrong domain. Treating `403 + totp_required`
as "stay on /totp-verify" fixes that.

### Login response reminder
`POST /user_login` returns `"has_totp": true|false`. Keep using it to route to
`/totp-verify` when `true`. The session is created at login but is inert until
2FA passes, so routing must be driven by `has_totp`, not by "is there a session."

---

## 2. Never hardcode the prod login/redirect URL

Any "go to login" navigation must use the **app's own origin** (the current
`window.location.origin`), not a hardcoded `https://app.bytoid.ai`. On
`localhost:8080` or the Lovable preview, a hardcoded prod URL throws the user out
of their environment.

### SAML: pass an exact `redirect`
When starting SAML (`GET /auth/saml/login?org=...`), also pass
`&redirect=<your-origin>` where the value **exactly matches** an allowlisted
origin (no trailing slash, no path), e.g. `http://localhost:8080` or
`https://preview--bytoiddev.lovable.app`.

The backend now also falls back to the request's `Origin`/`Referer` (validated
against the allowlist) if `redirect` is missing/invalid — so you stay on your
own domain — but passing an exact `redirect` is the reliable path. Note:
`http://localhost:8080` is only allowlisted when the backend runs with
`DEV=true`; the Lovable preview is always allowed.

---

## 3. Console fix — `validateDOMNesting: <button> cannot appear as a descendant of <button>`

Frontend-only. On `/radar`, a workspace card renders a `<button>` (the clickable
row) containing another `<button>` (the expand chevron / inline action). Stack
trace points at `src/components/ui/scroll-area.tsx` + the Radar card components.

**Fix:** don't nest buttons. Either
- make the inner control a non-button (`<span role="button" tabIndex={0}>` with
  `onClick`/`onKeyDown`, or a plain `div` with an `onClick`), or
- restructure so the row container is a `div` and the two buttons are **siblings**
  inside it rather than one inside the other.

Invalid-HTML warning only (not breaking), but it can cause odd click/focus
behavior, so worth cleaning up.

---

## 4. Console note — `net::ERR_NETWORK_CHANGED` / "Cannot Connect"

Not a code bug. This is a browser/OS event (the network changed mid-request —
Wi‑Fi handoff, VPN toggle). It resolves on retry (the page loaded all runbooks
moments later). No FE or BE change needed. If it's frequent, add a quiet
auto-retry/backoff on transient network errors before showing the
"Cannot Connect" modal, and only show the modal after N consecutive failures.
