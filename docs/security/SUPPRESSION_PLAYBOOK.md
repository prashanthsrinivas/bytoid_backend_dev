# Suppression Playbook

Sometimes a security scanner flags code that is genuinely safe. This document explains the **only** acceptable ways to suppress such findings, and the hard prohibition on suppressing anything inside a protected module.

## Hard rule: protected modules

You **cannot** add a suppression token to a file matched by [`security/protected_modules.yml`](../../security/protected_modules.yml).

The `protected-module-guardrail` rejects diffs that add any of:

- `# nosec` / `# nosec: <code>`
- `# noqa: S<NNN>` (the `S` series flake8-bandit codes)
- `# type: ignore` (on lines also flagged by mypy security rules)
- `# semgrep:ignore`
- `# pragma: no cover`
- New entries in `.semgrepignore` that match a protected path
- New `pip-audit --ignore-vuln` flags for packages used by a protected module
- New `bandit.yaml` skip entries that intersect a protected path

If a security tool flags a true positive in a protected module, the answer is to **fix it**, not silence it. If the tool flags a false positive in a protected module, escalate to `@bytoid/security-leads` to either (a) fix the tool's rule pattern, or (b) re-classify the file out of the protected set via a CODEOWNERS-reviewed PR.

## Non-protected paths: when suppression is acceptable

For everything else, a suppression is acceptable when:

1. The finding is a genuine false positive (the tool misunderstood the code).
2. The risk is documented and accepted (e.g. an internal-only debug endpoint guarded by an environment flag).
3. The fix is in progress and tracked in a ticket (the suppression is temporary).

Every suppression **must** include a `REASON:` comment in the same diff. Examples:

```python
# Bandit thinks this is SQL injection, but the query is built from a closed
# enumeration in services/foo.py:STATES — none of which are user-supplied.
# REASON: false positive, see issue #4321.
result = cursor.execute(f"SELECT * FROM x WHERE state = '{state}'")  # nosec B608
```

```yaml
# pip-audit GHSA-xxxx-yyyy: vulnerability is in test-only path
# (tests/security/llm/fixtures/), not the runtime. REASON: false-positive.
ignore:
  - GHSA-xxxx-yyyy
```

A suppression without a `REASON:` line is treated as malformed and will be re-flagged on the next scan.

## Baseline vs. inline suppression

Two mechanisms exist; pick one:

### Inline suppression (preferred for one-off cases)
Add `# nosec` / `# noqa` directly on the line. Visible to readers. Easy to remove when the false positive is fixed in the tool itself.

### Baseline entry (preferred for systemic false positives across many lines)
Add to [`security/baseline.json`](../../security/baseline.json) with the documented schema. Every baseline entry **must have**:
- `tool`: the scanner emitting the finding
- `finding_id`: the scanner's stable id (e.g. `B608`, `owasp-top-ten.sql-injection`)
- `path`: `file:line`
- `reason`: specific justification
- `approver`: GitHub login of a non-author reviewer
- `added_at`, `expires_at`: ISO dates; entries past `expires_at` re-fail CI until renewed

Baseline entries that intersect a protected path additionally require:
- `protected_module_acknowledged`: GitHub login of a `@bytoid/security-leads` member
- `expires_at`: must be ≤ 14 days from `added_at`

## When the suppression you need is forbidden

If the diff guardrail blocks you, the workflow is:

1. Fix the underlying issue (most common path). The vast majority of bandit/semgrep findings in this codebase are real and have a small, mechanical fix.
2. If the finding is genuinely a false positive in a protected module, open a separate PR adjusting the scanner rule (e.g. tightening a `.semgrep/protected/*.yml` pattern, or contributing to upstream). Land that fix, then revisit the original PR.
3. If the file should not be protected, open a `risk:critical` PR proposing to remove it from `security/protected_modules.yml`. `@bytoid/security-leads` review required.
4. **Never** disable the protected-module-guardrail workflow or branch protection to ship a hotfix. The emergency override path is documented in [PROTECTED_MODULES.md](./PROTECTED_MODULES.md#what-happens-if-the-guardrail-breaks).

## Verifying a suppression

After adding a suppression, run the relevant scanner locally to confirm:

```bash
bandit -c pyproject.toml -r path/to/file.py
semgrep scan --config .semgrep/protected/ path/to/file.py
make protected-check-suppression   # confirms no protected-module suppression was added
```

CI re-runs the same checks. If your suppression is well-formed, the finding will no longer block.

## Audit trail

Every suppression is visible:
- Inline `# nosec` / `# noqa` shows up in `git blame`.
- Baseline entries are reviewed in PR description; the reviewer's GitHub login is captured in `approver`.
- Protected-module suppression attempts emit `PROTECTED_MODULE_SUPPRESSION_BLOCKED` audit events even when blocked, so we can see who tried.

## Quarterly review

Once per quarter, `@bytoid/security-leads` reviews `security/baseline.json` and grep for `# nosec` / `# noqa: S`. Anything stale (resolved upstream, fixed elsewhere) is removed; anything still legitimate has its `expires_at` extended.
