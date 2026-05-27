# Local Security Setup

This guide walks you through installing the Phase 1 security tooling locally so your `git commit` runs the same fast-subset checks CI runs on every PR.

## One-time setup

From the repo root, in the same venv you use for development:

```bash
pip install -r requirements.txt    # picks up bandit, semgrep, pip-audit, safety, pytest-cov, pre-commit
pre-commit install                  # wires up the git hooks
```

Verify:

```bash
pre-commit run --all-files
```

The first run may take ~30s while pre-commit downloads tool environments. Subsequent runs are fast (<5s on a small diff).

## What runs on `git commit`

The hooks defined in [`.pre-commit-config.yaml`](../../.pre-commit-config.yaml) execute against the **staged** files. From fastest to slowest:

| Hook | What it does |
|---|---|
| `ruff` (+ fix) | Auto-fix lint errors; reject on remaining. |
| `ruff-format` | Auto-format Python files. |
| `bandit -lll` | SAST quick scan (LOW+ severity); rejects high-confidence findings. |
| `gitleaks` | Searches for secret tokens / API keys in the diff. |
| `protected-module-suppression-check` | Blocks new `# nosec` / `# noqa: S` / `# type: ignore` on protected files. |
| `check-yaml`, `check-toml`, `detect-private-key`, `trailing-whitespace`, `end-of-file-fixer`, `check-added-large-files` | Generic hygiene. |

A failed hook leaves the commit unstaged. Fix the issue and re-commit, or — for cases where the hook is wrong — escalate (see Suppression Playbook below). **You may not add suppressions to protected files.**

## Running the scanners directly

Each scanner has a `make` target:

```bash
make test               # pytest --json-report (323 tests today)
make test-fast          # skip slow/chaos/live_llm markers
make coverage           # pytest with --cov-report=html → htmlcov/index.html
make security           # all scanners (Phase 1 — see Makefile)
make security-secrets   # gitleaks + trufflehog
make security-deps      # pip-audit + safety
make protected-check    # the protected-module guardrail (full mode)
make protected-check-suppression   # only fails on new suppressions in protected paths
make lint               # ruff check .
```

If you want to re-create exactly what CI runs:

```bash
# SAST
bandit -c pyproject.toml -r . -f json -o bandit.json
semgrep scan --config p/python --config p/owasp-top-ten --config p/flask --config p/secrets --config .semgrep/protected/ --sarif --output semgrep.sarif

# Secrets
gitleaks detect --config .gitleaks.toml --report-format sarif --report-path gitleaks.sarif
trufflehog filesystem --only-verified --json . > trufflehog.json

# Deps
pip-audit -r requirements.txt -f sarif -o pip-audit.sarif
safety scan --output json --save-as json safety-report.json
```

## CI / dashboard wiring

Every scanner job in [`.github/workflows/security.yml`](../../.github/workflows/security.yml):

1. Runs the tool, captures SARIF + JSON.
2. Uploads SARIF to the GitHub Security tab.
3. POSTs a normalized JSON payload to the backend's `/tests/webhook/ci` (HMAC-signed with `FRONTEND_TESTS_WEBHOOK_SECRET`).
4. The dashboard at `/unit-test-results` shows the result in the matching category card (`backend_security_sast`, `backend_security_secrets`, `backend_security_deps`, `backend_coverage`).
5. Workflow uploads SARIF + raw JSON as artifacts on the run page.

Phase 1 is **warn-only** for non-protected paths: a finding does NOT fail the build. Protected-module diffs are gated separately by [`protected-module-guardrail.yml`](../../.github/workflows/protected-module-guardrail.yml).

## Required repo secrets

| Secret | Used by | Purpose |
|---|---|---|
| `BYTOID_BACKEND_URL` | scripts/post-scan-result.py | webhook destination (e.g. `https://api-dev.bytoid.ai`) |
| `FRONTEND_TESTS_WEBHOOK_SECRET` | scripts/post-scan-result.py | HMAC secret shared with the backend |
| `SAFETY_API_KEY` | `safety scan` | Required for Safety community-DB lookups |
| `BYTOID_AUDIT_WEBHOOK_URL` / `BYTOID_AUDIT_WEBHOOK_SECRET` | protected-module-guardrail | Optional; for sending PROTECTED_MODULE_CHANGE audit events |

Set them in the GitHub UI under **Settings → Secrets and variables → Actions → New repository secret**.

## Troubleshooting

### "ruff fails on legacy violations I didn't introduce"

The Phase 0 baseline keeps ruff at its current ruleset. Phase 2 turns on the strict ruleset and chases the legacy backlog. If you hit this and your diff is unrelated, run `make lint` to confirm the failures predate your change, then mention in the PR description.

### "Bandit flags `assert` in my test"

Bandit B101 is suppressed globally via `bandit.yaml` + `pyproject.toml`. If it still fires, you may be on an older bandit; `pip install --upgrade "bandit[toml]>=1.7"`.

### "pip-audit is slow / OOMs locally"

Use the Docker image: `docker run --rm -v "$PWD":/src ghcr.io/pypa/pip-audit:latest pip-audit -r /src/requirements.txt`.

### "Pre-commit reports `protected-module-suppression-check` blocked but I'm not adding a suppression"

The check inspects only added lines. Run `make protected-check-suppression` to see the exact line(s) it flagged. If it's a false positive, copy the line and ping `@bytoid/security-leads`.

## See also

- [Protected Modules](../security/PROTECTED_MODULES.md)
- [Change Review Template](../security/CHANGE_REVIEW_TEMPLATE.md)
- [Suppression Playbook](../security/SUPPRESSION_PLAYBOOK.md)
