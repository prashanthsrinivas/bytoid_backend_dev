"""Vendor Risk Assessment (VRA) — OSINT automation module.

A VRA is a special questionnaire type that, once the vendor name + primary
domain are answered, automatically collects free/open-source vendor OSINT, folds
the findings into the runbook as evidence, surfaces them on a Vendor Intelligence
Dashboard, and adds an "OSINT Intelligence Assessment" section to the report.

Design constraints (do not violate):
  * **Additive only.** This package never modifies the logic of existing
    modules. It drives the playbook/runbook/risk engines through their existing
    public functions and the ``structure_theme``/evidence parameters they
    already accept. The only edits to existing files are append-only
    registrations (blueprint list, permission metadata, a new table-creation
    function, new LanceDB methods, one EXEMPT_PATHS entry).
  * **Feature-gated.** Everything here is reached only when
    ``assessment_type == "vra"``. Standard assessments never execute VRA code.
  * **Fail-safe.** OSINT collection is isolated; any collector/Lambda failure
    degrades to "no findings" and can never break assessment or report flows.
  * **Security-first.** The vendor domain is attacker-controllable input. Every
    outbound fetch goes through ``vra.osint.safe_fetch`` (SSRF guard). The
    inbound Lambda callback is HMAC-signed, replay-protected, and idempotent.

The heavy OSINT collection runs in a separate AWS Lambda (``collector_lambda/``)
so it cannot touch app code; it posts normalized findings back to the HMAC-signed
callback, and the app owns all encryption/persistence (single source of truth).
"""
