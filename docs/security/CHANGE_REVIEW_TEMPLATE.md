# Protected-Module PR Description Template

Use this template (or paste it into the PR body) for every PR touching a
protected module. Items marked **required** are validated by the guardrail.

---

## Risk: <!-- required: low | medium | high | critical -->

<!--
  Match the label you applied to this PR. See the risk matrix in
  docs/security/PROTECTED_MODULES.md.
-->

## Rationale: <!-- required -->

<!--
  Why is this change being made? What is the user-visible or operator-visible
  outcome? If this is a security fix, link the discovery (ticket, audit
  finding, threat-model output, etc.) but do not paste secrets.
-->

## Test evidence: <!-- required -->

<!--
  Concrete evidence the change behaves as intended. At least one of:
  - The CI run link with the relevant `backend_security_*` + `backend_unit` +
    `backend_integration` jobs green.
  - Output of a local `make all-checks` run, pasted as a code block.
  - A diff showing the new test(s) under tests/security/<area>/.

  For changes to crypto, authz, or audit logging:
  - Explicitly state which existing test demonstrates the property is
    preserved (e.g. "tests/security/authz/test_rbac_bypass.py::test_admin_cannot_bypass_special_access still passes").
-->

## Impacted protected categories

<!-- The guardrail comment lists these; copy them here for reviewer convenience. -->
- [ ] authentication_identity
- [ ] authorization_rbac
- [ ] tenant_isolation
- [ ] cryptography_keys_tokens
- [ ] secrets_credentials
- [ ] workflow_orchestration
- [ ] ai_agent_execution
- [ ] audit_compliance_logging
- [ ] billing_payments_financial
- [ ] governance_meta

## Backwards-compatibility checklist

- [ ] No existing audit-log call sites were removed.
- [ ] No `@permission_required*` decorator was removed.
- [ ] No cryptographic primitive was downgraded.
- [ ] No new `# nosec`, `# noqa: S`, or `# type: ignore` was added in a
      protected path. (The guardrail blocks this automatically; this
      checkbox is a reviewer reminder.)
- [ ] Celery serializer remains `json`; pickle is not reintroduced.
- [ ] If this changes the protected-modules spec, CODEOWNERS was updated in
      the same PR.

## Rollback plan

<!--
  How to revert this change if a production incident is traced to it. Most
  protected-module changes are revertable by `git revert <sha>` + redeploy;
  call out cases where data migrations, key rotations, or workflow state
  changes complicate that.
-->
