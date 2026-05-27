# Protected Modules

A subset of this codebase carries elevated security and compliance risk. Any
change to a **protected module** — whether by a human or an AI agent —
follows the rules below. This document is human-readable; the machine-readable
source of truth lives in [`security/protected_modules.yml`](../../security/protected_modules.yml).

## What counts as a protected module?

The categories below are enforced. See `protected_modules.yml` for the exact path globs.

1. **Authentication & identity** — `google_route/`, `microsoft_route/`, `users_routes/`, `invited_users/`, `session_middleware.py`, `sso_by/`, `services/totp_service.py`
2. **RBAC / ABAC / authorization** — `utils/permission_required.py`, `utils/permission_resolver.py`, `utils/normal.py`
3. **Tenant isolation** — `db/db_checkers.py`, `app.py`
4. **Cryptography / keys / tokens** — `utils/key_rotation_manager.py`, `users_routes/routes.py`, `invited_users/uszr_helper.py`; plus any file importing `cryptography`
5. **Secrets / credentials / sensitive config** — `db/rds_db.py`, `utils/app_configs.py`, `.env*`, `services/redis_service.py`
6. **Workflow orchestration & execution** — `workflow_route/`, `runbook/`, `playbook/`, `services/workflow_service.py`, `services/automate_service.py`, `services/scheduler_service.py`
7. **AI agent execution & tool invocation** — `agent_routes/`, `agent_hub/`, `ai_assistant_chat/`, scrape services, `umail_lance/`
8. **Audit / compliance / forensic logging** — `services/audit_log_service.py`
9. **Billing / payments / financial** — `payments/`, `credits/`, `credits_route/`, `plans/`, `services/stripe_webhook_handler.py`, `services/credit_system.py`
10. **Governance meta** — the protected-modules spec itself, CODEOWNERS, the guardrail workflow & script, the protective semgrep rules, the branch-protection init script.

Adding a file to any category is itself a protected-module change.

## What changes when a PR touches a protected module?

The [`protected-module-guardrail`](../../.github/workflows/protected-module-guardrail.yml) workflow runs first on every PR. If the PR diff intersects a protected path:

1. It labels the PR `protected-module` and `human-review:required`.
2. It posts a sticky comment listing the requirements.
3. It blocks merge until **all** of the following are true:
   - The PR has one of `risk:low`, `risk:medium`, `risk:high`, `risk:critical`.
   - At least one approving review is from a **human (non-bot)** account.
     AI-account reviews don't count. See `ai_actor_logins:` in the spec.
   - No new suppression token (`# nosec`, `# noqa: S`, `# type: ignore`,
     `# semgrep:ignore`, `# pragma: no cover`) was added on a protected line.
   - No CRITICAL Semgrep rule under `.semgrep/protected/` fires.
   - Phase-1 scanners pass without warn-only carve-outs on the protected diff.
4. It emits a `PROTECTED_MODULE_CHANGE` audit event.

## How to propose a change to a protected module

### As a human
1. Open the PR. Title should describe the change concretely.
2. Apply the appropriate `risk:*` label. The matrix below gives guidance.
3. Fill in the structured PR description from [`CHANGE_REVIEW_TEMPLATE.md`](./CHANGE_REVIEW_TEMPLATE.md).
4. Request review from the CODEOWNERS that the guardrail auto-assigned. A human approver from the relevant team is required to merge.

### As an AI agent
All of the above, **plus**:
1. Prefix the PR title with `[ai-proposal]`.
2. The PR body must include three headings: `Risk:`, `Rationale:`, `Test evidence:`. The guardrail rejects PRs missing any of these.
3. The AI account's own review does not count toward the human-reviewer requirement. A human must approve explicitly.

### Risk matrix

| Change shape | Suggested label |
|---|---|
| Comment-only edit, no behavior change | `risk:low` |
| Adding a new test case, new audit constant, new doc | `risk:low` |
| Adding a new permission check, narrowing an allow-list | `risk:medium` |
| Modifying authz logic, adding a new role | `risk:high` |
| Changing crypto primitive, key rotation, token format | `risk:critical` |
| Changing the protected-modules spec itself | `risk:critical` |

## How to add a path to a protected category

Edit `security/protected_modules.yml`. This is itself a protected change with `risk:critical`. The CODEOWNERS file may need to be updated in the same PR to point reviewers at the new path.

## How to remove a path from a protected category

Almost never done. If you have a strong reason:

1. Open a PR with `risk:critical`.
2. The PR body must explain why the file no longer carries elevated risk (e.g. the file has been fully deleted, or the security-relevant code has moved to a different protected file).
3. CODEOWNERS for the `governance_meta` category must approve.

## What happens if the guardrail breaks?

The guardrail script (`scripts/protected-module-guardrail.py`) is itself protected. If you find a bug:

1. Open a PR with `risk:high`.
2. Add a regression test under `tests/security/governance/test_guardrail_<bug>.py` (this test file path will be created in Phase 4).
3. Reviewers from `@bytoid/security-leads` must approve.

In an emergency where the guardrail is blocking a critical hotfix, the only path is for a security lead to disable branch protection via `gh api`, document the bypass in the audit log, fix the hotfix's underlying issue, then re-enable. Never delete or skip the guardrail workflow.

## Local development

Before opening a PR, you can run the guardrail against your local diff:

```bash
make protected-check
```

If you've installed pre-commit hooks (`pre-commit install`), the `make protected-check-suppression` mode runs automatically on commits and rejects new suppressions in protected paths.

## See also

- [`security/protected_modules.yml`](../../security/protected_modules.yml) — the machine-readable spec.
- [`.github/CODEOWNERS`](../../.github/CODEOWNERS) — required reviewers per category.
- [`.semgrep/protected/`](../../.semgrep/protected/) — CRITICAL semgrep rules that detect weakening patterns.
- [`docs/security/CHANGE_REVIEW_TEMPLATE.md`](./CHANGE_REVIEW_TEMPLATE.md) — PR description template.
