# Bytoid backend — developer entry points.
#
# Every Phase of the security/testing rollout adds targets here so there is
# one canonical place to find every check.

PYTHON ?= python3
PIP ?= pip

.PHONY: help
help: ## Show this help.
	@awk 'BEGIN {FS = ":.*##"; printf "Usage: make <target>\n\nTargets:\n"} \
		/^[a-zA-Z0-9_-]+:.*##/ { printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

# ── Phase 0 ────────────────────────────────────────────────────────────────

.PHONY: test
test: ## Run the full pytest suite (tests/ + testing/).
	$(PYTHON) -m pytest -v

.PHONY: test-fast
test-fast: ## Run pytest but skip the slow / chaos / live-llm markers.
	$(PYTHON) -m pytest -v -m "not slow and not chaos and not live_llm"

.PHONY: coverage
coverage: ## Run tests with line + branch coverage, write htmlcov/ + coverage.xml.
	$(PYTHON) -m pytest --cov=. --cov-report=xml --cov-report=html --cov-branch

.PHONY: protected-check
protected-check: ## Run the protected-module guardrail against the current diff.
	$(PYTHON) scripts/protected-module-guardrail.py --mode=local

.PHONY: protected-check-suppression
protected-check-suppression: ## Stricter local check: only fail on new suppressions in protected paths.
	$(PYTHON) scripts/protected-module-guardrail.py --mode=suppression

# ── Phase 1 ────────────────────────────────────────────────────────────────

.PHONY: security
security: security-sast security-secrets security-deps ## Run all Phase 1 security scanners.

.PHONY: security-sast
security-sast: ## Run Bandit + Semgrep SAST.
	@echo "→ bandit"
	bandit -c pyproject.toml -r . -f json -o bandit-report.json || true
	@echo "→ semgrep (project ruleset + .semgrep/protected/)"
	semgrep scan \
		--config p/python \
		--config p/owasp-top-ten \
		--config p/flask \
		--config p/secrets \
		--config .semgrep/protected/ \
		--sarif --output semgrep.sarif \
		--metrics off || true
	@echo "Reports: bandit-report.json, semgrep.sarif"

.PHONY: security-secrets
security-secrets: ## Run Gitleaks (TruffleHog needs Docker; see security-secrets-trufflehog).
	gitleaks detect --config .gitleaks.toml --report-format sarif --report-path gitleaks.sarif --no-banner || true

.PHONY: security-secrets-trufflehog
security-secrets-trufflehog: ## Run TruffleHog against the working tree (requires Docker).
	docker run --rm -v "$$PWD:/repo" trufflesecurity/trufflehog:latest \
		filesystem --only-verified --json /repo > trufflehog.json || true

.PHONY: security-deps
security-deps: ## Run pip-audit + Safety against requirements.txt.
	pip-audit -r requirements.txt -f json -o pip-audit.json || true
	pip-audit -r requirements.txt -f sarif -o pip-audit.sarif || true
	-safety scan --output json --save-as json safety-report.json

.PHONY: security-baseline
security-baseline: ## Check security/baseline.json for expired entries.
	$(PYTHON) -c "import json,sys; from datetime import date; \
data=json.load(open('security/baseline.json')); \
expired=[e for e in data['entries'] if e.get('expires_at') and e['expires_at'] < str(date.today())]; \
sys.exit(0 if not expired else (print(f'EXPIRED: {len(expired)}') or 1))"

# ── Phase 2 ────────────────────────────────────────────────────────────────

.PHONY: typecheck
typecheck: ## Run mypy on the strictly-typed modules (tests_routes/, key services).
	$(PYTHON) -m mypy \
		tests_routes/ \
		services/audit_log_service.py \
		utils/permission_required.py \
		--output=json --no-incremental 2>&1 | tee mypy-output.jsonl; true

.PHONY: lint
lint: ## Run ruff (full repo) + pylint (critical paths from auth_critical_paths.txt).
	ruff check .
	@echo "→ pylint (critical paths)"
	@FILES=$$(grep -v '^#' auth_critical_paths.txt | grep -v '^$$' | tr '\n' ' '); \
	if [ -n "$$FILES" ]; then \
		$(PYTHON) -m pylint $$FILES --rcfile=.pylintrc || true; \
	fi

# ── Phase 5 ────────────────────────────────────────────────────────────────

.PHONY: chaos
chaos: ## Run chaos / fault-injection tests (requires RUN_CHAOS=1).
	RUN_CHAOS=1 $(PYTHON) -m pytest tests/chaos/ -m chaos -v --tb=short

.PHONY: mutation
mutation: ## Run mutmut mutation testing on safety-critical modules (slow, nightly).
	mutmut run \
		--paths-to-mutate services/audit_log_service.py,utils/permission_required.py,tests_routes/result_store.py,tests_routes/runners.py \
		--runner "$(PYTHON) -m pytest -x -q" \
		--no-progress
	mutmut results 2>&1 | tee mutmut-results.txt

# ── Aggregate ──────────────────────────────────────────────────────────────

.PHONY: all-checks
all-checks: lint typecheck test chaos protected-check ## Run everything that's wired up so far (chaos requires RUN_CHAOS=1).
