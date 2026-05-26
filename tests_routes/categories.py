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
