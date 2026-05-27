"""Exhaustive parametrized unit tests for tests_routes/categories.py.

Tests every category × every property × every public helper.
"""

import pytest

from tests_routes.categories import (
    ALL_CATEGORIES,
    BACKEND_CATEGORIES,
    FRONTEND_CATEGORIES,
    is_backend_category,
    is_delegated,
    is_frontend_category,
    is_locally_dispatchable,
    is_valid_category,
)


# ── parametrized: every category has required metadata keys ───────────────────

REQUIRED_META_KEYS = ["display_name", "runner", "delegated"]


@pytest.mark.unit
@pytest.mark.parametrize("name", list(ALL_CATEGORIES.keys()))
def test_category_has_display_name(name):
    assert "display_name" in ALL_CATEGORIES[name]

@pytest.mark.unit
@pytest.mark.parametrize("name", list(ALL_CATEGORIES.keys()))
def test_category_has_runner(name):
    assert "runner" in ALL_CATEGORIES[name]

@pytest.mark.unit
@pytest.mark.parametrize("name", list(ALL_CATEGORIES.keys()))
def test_category_has_delegated(name):
    assert "delegated" in ALL_CATEGORIES[name]

@pytest.mark.unit
@pytest.mark.parametrize("name", list(ALL_CATEGORIES.keys()))
def test_category_display_name_is_non_empty(name):
    assert ALL_CATEGORIES[name]["display_name"].strip()

@pytest.mark.unit
@pytest.mark.parametrize("name", list(ALL_CATEGORIES.keys()))
def test_category_runner_is_non_empty(name):
    assert ALL_CATEGORIES[name]["runner"].strip()

@pytest.mark.unit
@pytest.mark.parametrize("name", list(ALL_CATEGORIES.keys()))
def test_category_delegated_is_bool(name):
    assert isinstance(ALL_CATEGORIES[name]["delegated"], bool)


# ── parametrized: name format conventions ─────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("name", list(ALL_CATEGORIES.keys()))
def test_category_name_uses_snake_case(name):
    """Category names should match a `prefix_section[_suffix]` snake_case pattern."""
    assert name.islower()
    assert all(ch.isalnum() or ch == "_" for ch in name)

@pytest.mark.unit
@pytest.mark.parametrize("name", list(BACKEND_CATEGORIES.keys()))
def test_backend_category_name_starts_with_backend(name):
    assert name.startswith("backend_")

@pytest.mark.unit
@pytest.mark.parametrize("name", list(FRONTEND_CATEGORIES.keys()))
def test_frontend_category_name_starts_with_frontend(name):
    assert name.startswith("frontend_")


# ── parametrized: predicates round-trip per category ─────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("name", list(ALL_CATEGORIES.keys()))
def test_is_valid_category_true_for_known(name):
    assert is_valid_category(name) is True

@pytest.mark.unit
@pytest.mark.parametrize("name", [
    "", "unknown", "backend_foo", "frontend_bar", "BACKEND_UNIT",
    "backend unit", "backend-unit", None,
])
def test_is_valid_category_false_for_unknown(name):
    if name is None:
        assert is_valid_category("") is False
    else:
        assert is_valid_category(name) is False

@pytest.mark.unit
@pytest.mark.parametrize("name", list(BACKEND_CATEGORIES.keys()))
def test_is_backend_category(name):
    assert is_backend_category(name) is True
    assert is_frontend_category(name) is False

@pytest.mark.unit
@pytest.mark.parametrize("name", list(FRONTEND_CATEGORIES.keys()))
def test_is_frontend_category(name):
    assert is_frontend_category(name) is True
    assert is_backend_category(name) is False

@pytest.mark.unit
@pytest.mark.parametrize("name", list(ALL_CATEGORIES.keys()))
def test_is_delegated_matches_metadata(name):
    assert is_delegated(name) == ALL_CATEGORIES[name]["delegated"]

@pytest.mark.unit
@pytest.mark.parametrize("name", list(ALL_CATEGORIES.keys()))
def test_is_locally_dispatchable_is_backend_and_not_delegated(name):
    expected = is_backend_category(name) and not is_delegated(name)
    assert is_locally_dispatchable(name) == expected


# ── parametrized: runner buckets ─────────────────────────────────────────────

VALID_RUNNERS = {
    "pytest", "locust", "multi-sast", "secrets", "deps", "coverage",
    "mypy", "lint", "pytest-security", "mutmut",
    "vitest", "playwright", "tsc",
}

@pytest.mark.unit
@pytest.mark.parametrize("name", list(ALL_CATEGORIES.keys()))
def test_runner_is_known(name):
    runner = ALL_CATEGORIES[name]["runner"]
    assert runner in VALID_RUNNERS, f"Unknown runner '{runner}' for {name}"


# ── parametrized: delegation invariants ───────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("name", list(FRONTEND_CATEGORIES.keys()))
def test_all_frontend_categories_are_delegated(name):
    """Every frontend category is delegated (results come from bytoiddev CI)."""
    assert is_delegated(name) is True

@pytest.mark.unit
@pytest.mark.parametrize("name", [
    "backend_security_sast", "backend_security_secrets", "backend_security_deps",
    "backend_security_authz", "backend_security_api", "backend_security_llm",
    "backend_security_infra", "backend_coverage", "backend_typecheck",
    "backend_lint", "backend_mutation",
])
def test_backend_ci_driven_categories_are_delegated(name):
    assert is_delegated(name) is True

@pytest.mark.unit
@pytest.mark.parametrize("name", [
    "backend_unit", "backend_integration", "backend_regression",
    "backend_load", "backend_stress", "backend_performance",
])
def test_celery_dispatched_categories_are_not_delegated(name):
    assert is_delegated(name) is False
    assert is_locally_dispatchable(name) is True


# ── parametrized: display_name uniqueness ─────────────────────────────────────

@pytest.mark.unit
def test_display_names_are_unique():
    names = [meta["display_name"] for meta in ALL_CATEGORIES.values()]
    assert len(names) == len(set(names)), "Two categories share a display_name"

@pytest.mark.unit
def test_category_keys_are_unique_between_backend_and_frontend():
    overlap = set(BACKEND_CATEGORIES.keys()) & set(FRONTEND_CATEGORIES.keys())
    assert overlap == set()

@pytest.mark.unit
def test_all_categories_is_union():
    assert set(ALL_CATEGORIES.keys()) == set(BACKEND_CATEGORIES.keys()) | set(FRONTEND_CATEGORIES.keys())


# ── parametrized: every category's display_name contains a capital letter ─────

@pytest.mark.unit
@pytest.mark.parametrize("name", list(ALL_CATEGORIES.keys()))
def test_display_name_has_capital_letter(name):
    """Human-readable display names should contain at least one capital."""
    assert any(c.isupper() for c in ALL_CATEGORIES[name]["display_name"])
