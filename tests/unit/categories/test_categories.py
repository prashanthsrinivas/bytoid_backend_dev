"""Unit tests for tests_routes/categories.py.

Zero external dependencies — the module uses only built-in Python constructs.
"""

import pytest

from tests_routes.categories import (
    ALL_CATEGORIES,
    BACKEND_CATEGORIES,
    FRONTEND_CATEGORIES,
    is_valid_category,
    is_backend_category,
    is_frontend_category,
    is_delegated,
    is_locally_dispatchable,
)

# The six original backend categories that are NOT delegated
LOCALLY_DISPATCHABLE_BACKEND = {
    "backend_unit",
    "backend_integration",
    "backend_regression",
    "backend_load",
    "backend_stress",
    "backend_performance",
}

# Phase 1 / Phase 2 delegated backend scanners
DELEGATED_BACKEND = {
    "backend_security_sast",
    "backend_security_secrets",
    "backend_security_deps",
    "backend_coverage",
    "backend_typecheck",
    "backend_lint",
}


# ===========================================================================
# is_valid_category
# ===========================================================================


@pytest.mark.unit
def test_is_valid_category_true_for_all_known():
    """Every key in ALL_CATEGORIES must be valid."""
    for cat in ALL_CATEGORIES:
        assert is_valid_category(cat), f"Expected {cat!r} to be valid"


@pytest.mark.unit
def test_is_valid_category_false_for_garbage():
    """Unknown strings must not be valid."""
    assert is_valid_category("garbage") is False
    assert is_valid_category("garbage_category") is False
    assert is_valid_category("") is False


@pytest.mark.unit
def test_is_valid_category_case_sensitive():
    """Category lookup is case-sensitive; uppercase variants must be invalid."""
    assert is_valid_category("BACKEND_UNIT") is False
    assert is_valid_category("Backend_Unit") is False


# ===========================================================================
# is_backend_category
# ===========================================================================


@pytest.mark.unit
def test_is_backend_category_true_for_all_backend():
    """Every key in BACKEND_CATEGORIES must be identified as backend."""
    for cat in BACKEND_CATEGORIES:
        assert is_backend_category(cat), f"Expected {cat!r} to be backend"


@pytest.mark.unit
def test_is_backend_category_false_for_frontend():
    """Frontend categories must not be identified as backend."""
    assert is_backend_category("frontend_unit") is False
    assert is_backend_category("frontend_e2e") is False


@pytest.mark.unit
def test_is_backend_category_false_for_garbage():
    assert is_backend_category("garbage_category") is False


# ===========================================================================
# is_frontend_category
# ===========================================================================


@pytest.mark.unit
def test_is_frontend_category_true_for_all_frontend():
    """Every key in FRONTEND_CATEGORIES must be identified as frontend."""
    for cat in FRONTEND_CATEGORIES:
        assert is_frontend_category(cat), f"Expected {cat!r} to be frontend"


@pytest.mark.unit
def test_is_frontend_category_false_for_backend():
    """Backend categories must not be identified as frontend."""
    assert is_frontend_category("backend_unit") is False
    assert is_frontend_category("backend_security_sast") is False


@pytest.mark.unit
def test_is_frontend_category_false_for_garbage():
    assert is_frontend_category("garbage_category") is False


# ===========================================================================
# is_delegated
# ===========================================================================


@pytest.mark.unit
def test_is_delegated_true_for_backend_security_sast():
    assert is_delegated("backend_security_sast") is True


@pytest.mark.unit
def test_is_delegated_false_for_backend_unit():
    """Core backend categories run locally, so they must not be delegated."""
    assert is_delegated("backend_unit") is False


@pytest.mark.unit
def test_is_delegated_all_frontend_are_delegated():
    """All frontend categories must be delegated."""
    for cat in FRONTEND_CATEGORIES:
        assert is_delegated(cat), f"Expected frontend category {cat!r} to be delegated"


@pytest.mark.unit
def test_is_delegated_all_delegated_backend_categories():
    """Phase 1/2 backend scanners must all be delegated."""
    for cat in DELEGATED_BACKEND:
        assert is_delegated(cat), f"Expected {cat!r} to be delegated"


@pytest.mark.unit
def test_is_delegated_false_for_garbage():
    """Unknown category must not be considered delegated."""
    assert is_delegated("garbage_category") is False


# ===========================================================================
# is_locally_dispatchable
# ===========================================================================


@pytest.mark.unit
def test_is_locally_dispatchable_true_for_six_core_backend():
    """The six original (non-delegated) backend categories must be locally dispatchable."""
    for cat in LOCALLY_DISPATCHABLE_BACKEND:
        assert is_locally_dispatchable(cat), (
            f"Expected {cat!r} to be locally dispatchable"
        )


@pytest.mark.unit
def test_is_locally_dispatchable_false_for_delegated_backend():
    """Delegated backend categories must NOT be locally dispatchable."""
    for cat in DELEGATED_BACKEND:
        assert is_locally_dispatchable(cat) is False, (
            f"Expected {cat!r} NOT to be locally dispatchable"
        )


@pytest.mark.unit
def test_is_locally_dispatchable_false_for_frontend():
    """No frontend category is locally dispatchable."""
    for cat in FRONTEND_CATEGORIES:
        assert is_locally_dispatchable(cat) is False, (
            f"Expected frontend category {cat!r} NOT to be locally dispatchable"
        )


@pytest.mark.unit
def test_is_locally_dispatchable_false_for_garbage():
    assert is_locally_dispatchable("garbage_category") is False


# ===========================================================================
# Structural / invariant assertions
# ===========================================================================


@pytest.mark.unit
def test_all_categories_is_union_of_backend_and_frontend():
    """ALL_CATEGORIES must contain exactly BACKEND_CATEGORIES ∪ FRONTEND_CATEGORIES."""
    expected = {**BACKEND_CATEGORIES, **FRONTEND_CATEGORIES}
    assert set(ALL_CATEGORIES.keys()) == set(expected.keys())


@pytest.mark.unit
def test_backend_and_frontend_are_disjoint():
    """No category key should appear in both BACKEND_CATEGORIES and FRONTEND_CATEGORIES."""
    overlap = set(BACKEND_CATEGORIES.keys()) & set(FRONTEND_CATEGORIES.keys())
    assert overlap == set(), f"Overlapping categories: {overlap}"


@pytest.mark.unit
def test_all_locally_dispatchable_are_backend_non_delegated():
    """is_locally_dispatchable must hold iff is_backend and not is_delegated."""
    for cat in ALL_CATEGORIES:
        expected = is_backend_category(cat) and not is_delegated(cat)
        actual = is_locally_dispatchable(cat)
        assert actual == expected, (
            f"Inconsistency for {cat!r}: "
            f"is_backend={is_backend_category(cat)}, "
            f"is_delegated={is_delegated(cat)}, "
            f"is_locally_dispatchable={actual}"
        )


@pytest.mark.unit
def test_exactly_six_locally_dispatchable_categories():
    """Exactly 6 categories (the original backend ones) must be locally dispatchable."""
    dispatchable = [c for c in ALL_CATEGORIES if is_locally_dispatchable(c)]
    assert set(dispatchable) == LOCALLY_DISPATCHABLE_BACKEND


@pytest.mark.unit
def test_all_categories_have_display_name_and_runner():
    """Every category entry must include a non-empty display_name and runner."""
    for cat, meta in ALL_CATEGORIES.items():
        assert "display_name" in meta, f"{cat!r} missing display_name"
        assert "runner" in meta, f"{cat!r} missing runner"
        assert meta["display_name"], f"{cat!r} has empty display_name"
        assert meta["runner"], f"{cat!r} has empty runner"


@pytest.mark.unit
def test_all_categories_have_delegated_bool():
    """Every category entry must have a boolean 'delegated' key."""
    for cat, meta in ALL_CATEGORIES.items():
        assert "delegated" in meta, f"{cat!r} missing delegated key"
        assert isinstance(meta["delegated"], bool), (
            f"{cat!r} delegated must be bool, got {type(meta['delegated'])}"
        )
