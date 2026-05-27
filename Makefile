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

# ── Phase 1 (placeholders; populated when phase lands) ─────────────────────

.PHONY: security
security: ## Run all security scanners (Phase 1).
	@echo "Phase 1 not yet landed; see plan."
	@exit 0

.PHONY: security-secrets
security-secrets: ## Run secrets scanners (Phase 1).
	@echo "Phase 1 not yet landed; see plan."
	@exit 0

.PHONY: security-deps
security-deps: ## Run dependency vulnerability scanners (Phase 1).
	@echo "Phase 1 not yet landed; see plan."
	@exit 0

# ── Phase 2 (placeholders) ─────────────────────────────────────────────────

.PHONY: typecheck
typecheck: ## Run mypy (Phase 2).
	@echo "Phase 2 not yet landed; see plan."
	@exit 0

.PHONY: lint
lint: ## Run ruff + pylint (Phase 2).
	ruff check .

# ── Aggregate ──────────────────────────────────────────────────────────────

.PHONY: all-checks
all-checks: lint test protected-check ## Run everything that's wired up so far.
