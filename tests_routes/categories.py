"""Canonical list of test categories surfaced by the Unit Test Results module."""

BACKEND_CATEGORIES = {
    "backend_unit": {
        "display_name": "Backend Unit",
        "runner": "pytest",
        "delegated": False,
    },
    "backend_integration": {
        "display_name": "Backend Integration",
        "runner": "pytest",
        "delegated": False,
    },
    "backend_regression": {
        "display_name": "Backend Regression",
        "runner": "pytest",
        "delegated": False,
    },
    "backend_crypto": {
        "display_name": "Backend Crypto (Encryption/Decryption)",
        "runner": "pytest",
        "delegated": False,
    },
    "backend_load": {
        "display_name": "Backend Load",
        "runner": "locust",
        "delegated": False,
    },
    "backend_stress": {
        "display_name": "Backend Stress",
        "runner": "locust",
        "delegated": False,
    },
    "backend_performance": {
        "display_name": "Backend Performance",
        "runner": "locust",
        "delegated": False,
    },
    # ── Phase 1 — security scanners + coverage (delegated to CI) ──────
    "backend_security_sast": {
        "display_name": "Backend SAST (Bandit + Semgrep)",
        "runner": "multi-sast",
        "delegated": True,
    },
    "backend_security_secrets": {
        "display_name": "Backend Secrets (Gitleaks + TruffleHog)",
        "runner": "secrets",
        "delegated": True,
    },
    "backend_security_deps": {
        "display_name": "Backend Dependencies (pip-audit + Safety)",
        "runner": "deps",
        "delegated": True,
    },
    "backend_coverage": {
        "display_name": "Backend Coverage",
        "runner": "coverage",
        "delegated": True,
    },
    # ── Phase 2 — type safety + lint (delegated to CI) ─────────────
    "backend_typecheck": {
        "display_name": "Backend Type Check (mypy)",
        "runner": "mypy",
        "delegated": True,
    },
    "backend_lint": {
        "display_name": "Backend Lint (ruff + pylint)",
        "runner": "lint",
        "delegated": True,
    },
    # ── Phase 4 — dedicated security test suites (delegated to CI) ─
    "backend_security_authz": {
        "display_name": "Backend Auth & RBAC Tests",
        "runner": "pytest-security",
        "delegated": True,
    },
    "backend_security_api": {
        "display_name": "Backend API Security Tests",
        "runner": "pytest-security",
        "delegated": True,
    },
    "backend_security_llm": {
        "display_name": "Backend LLM/RAG Safety Tests",
        "runner": "pytest-security",
        "delegated": True,
    },
    "backend_security_infra": {
        "display_name": "Backend Infra Config Audit",
        "runner": "pytest-security",
        "delegated": True,
    },
    # ── Phase 5 — mutation testing (nightly, delegated to CI) ─────────────
    "backend_mutation": {
        "display_name": "Backend Mutation (mutmut)",
        "runner": "mutmut",
        "delegated": True,
    },
}

FRONTEND_CATEGORIES = {
    "frontend_unit": {
        "display_name": "Frontend Unit",
        "runner": "vitest",
        "delegated": True,
    },
    "frontend_integration": {
        "display_name": "Frontend Integration",
        "runner": "vitest",
        "delegated": True,
    },
    "frontend_e2e": {
        "display_name": "Frontend E2E",
        "runner": "playwright",
        "delegated": True,
    },
    "frontend_typecheck": {
        "display_name": "Frontend TypeCheck",
        "runner": "tsc",
        "delegated": True,
    },
    "frontend_regression": {
        "display_name": "Frontend Regression",
        "runner": "playwright",
        "delegated": True,
    },
}

ALL_CATEGORIES = {**BACKEND_CATEGORIES, **FRONTEND_CATEGORIES}


def is_valid_category(category: str) -> bool:
    return category in ALL_CATEGORIES


def is_backend_category(category: str) -> bool:
    return category in BACKEND_CATEGORIES


def is_frontend_category(category: str) -> bool:
    return category in FRONTEND_CATEGORIES


def is_delegated(category: str) -> bool:
    """True when results are posted back via the webhook rather than
    produced by a Celery task. All frontend_* categories plus the Phase 1
    `backend_security_*` / `backend_coverage` scanners are delegated."""
    meta = ALL_CATEGORIES.get(category) or {}
    return bool(meta.get("delegated"))


def is_locally_dispatchable(category: str) -> bool:
    """True for categories the backend can run via Celery itself."""
    return is_backend_category(category) and not is_delegated(category)
