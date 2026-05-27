"""Regression tests for webhook HMAC and category schema.

These lock the security-critical HMAC contract and category definitions
so that refactoring can never silently break either.
"""

import hashlib
import hmac
import json
import os
import sys
from unittest.mock import MagicMock, patch

import flask
import pytest

# ── Stubs (normalizers/categories have no heavy deps) ────────────────────────
for _m in ("pymysql", "pymysql.cursors", "db", "db.rds_db", "db.db_checkers",
           "boto3", "dotenv", "dbutils", "dbutils.pooled_db"):
    sys.modules.setdefault(_m, MagicMock(name=f"{_m}_stub"))

import tests_routes.webhook_auth as wh
import tests_routes.categories as cats

SECRET = "regression-test-secret"


# ── HMAC contract ─────────────────────────────────────────────────────────────

@pytest.mark.regression
def test_regression_hmac_header_name_unchanged():
    """The header name X-Bytoid-Signature must never change without a migration."""
    assert wh.SIGNATURE_HEADER == "X-Bytoid-Signature"

@pytest.mark.regression
def test_regression_hmac_algorithm_is_sha256():
    """Signature must use sha256 (not md5, sha1, etc.)."""
    sig = wh._expected_signature(SECRET, b"test")
    assert sig.startswith("sha256="), f"Expected sha256 prefix, got: {sig[:20]!r}"

@pytest.mark.regression
def test_regression_hmac_uses_compare_digest_not_equals():
    """verify_hmac must use constant-time compare (timing-safe)."""
    import inspect
    src = inspect.getsource(wh.verify_hmac)
    assert "compare_digest" in src, "verify_hmac must use hmac.compare_digest for timing safety"

@pytest.mark.regression
def test_regression_hmac_rejects_missing_secret():
    """When FRONTEND_TESTS_WEBHOOK_SECRET is unset, verify_hmac must return False."""
    body = b'{"category":"backend_unit"}'
    sig = f"sha256={hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()}"
    app = flask.Flask(__name__)
    with app.test_request_context("/tests/webhook/ci", method="POST", data=body,
                                  headers={wh.SIGNATURE_HEADER: sig}):
        env = {k: v for k, v in os.environ.items() if k != wh.SECRET_ENV_VAR}
        with patch.dict(os.environ, env, clear=True):
            assert wh.verify_hmac(flask.request) is False

@pytest.mark.regression
def test_regression_hmac_valid_accepts():
    body = json.dumps({"category": "backend_security_sast", "status": "passed"}).encode()
    sig = f"sha256={hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()}"
    app = flask.Flask(__name__)
    with app.test_request_context("/tests/webhook/ci", method="POST", data=body,
                                  content_type="application/json",
                                  headers={wh.SIGNATURE_HEADER: sig}):
        with patch.dict(os.environ, {wh.SECRET_ENV_VAR: SECRET}):
            assert wh.verify_hmac(flask.request) is True

@pytest.mark.regression
def test_regression_hmac_tampered_payload_rejected():
    original = b'{"category":"backend_unit"}'
    sig = f"sha256={hmac.new(SECRET.encode(), original, hashlib.sha256).hexdigest()}"
    tampered = b'{"category":"backend_security_sast"}'
    app = flask.Flask(__name__)
    with app.test_request_context("/tests/webhook/ci", method="POST", data=tampered,
                                  headers={wh.SIGNATURE_HEADER: sig}):
        with patch.dict(os.environ, {wh.SECRET_ENV_VAR: SECRET}):
            assert wh.verify_hmac(flask.request) is False


# ── Category schema ───────────────────────────────────────────────────────────

@pytest.mark.regression
def test_regression_all_categories_have_required_keys():
    """Every category must have 'runner' and 'delegated' keys."""
    all_cats = {**cats.BACKEND_CATEGORIES, **cats.FRONTEND_CATEGORIES}
    for cat_id, cfg in all_cats.items():
        assert "runner" in cfg, f"{cat_id}: missing 'runner'"
        assert "delegated" in cfg, f"{cat_id}: missing 'delegated'"

@pytest.mark.regression
def test_regression_ci_driven_categories_are_delegated():
    """SAST/secrets/deps/coverage/lint/typecheck/security suites must be delegated=True."""
    ci_driven = {
        "backend_security_sast", "backend_security_secrets", "backend_security_deps",
        "backend_coverage", "backend_typecheck", "backend_lint",
        "backend_security_authz", "backend_security_api",
        "backend_security_llm", "backend_security_infra", "backend_mutation",
    }
    for cat_id in ci_driven:
        assert cat_id in cats.BACKEND_CATEGORIES, f"{cat_id} not in BACKEND_CATEGORIES"
        assert cats.BACKEND_CATEGORIES[cat_id]["delegated"] is True, (
            f"{cat_id} must be delegated=True (CI-driven)"
        )

@pytest.mark.regression
def test_regression_celery_categories_not_delegated():
    """Celery-run categories must have delegated=False."""
    celery_cats = {
        "backend_unit", "backend_integration", "backend_regression",
        "backend_load", "backend_stress", "backend_performance",
    }
    for cat_id in celery_cats:
        assert cat_id in cats.BACKEND_CATEGORIES, f"{cat_id} not in BACKEND_CATEGORIES"
        assert cats.BACKEND_CATEGORIES[cat_id]["delegated"] is False, (
            f"{cat_id} must be delegated=False (Celery-run)"
        )

@pytest.mark.regression
def test_regression_frontend_categories_all_delegated():
    for cat_id, cfg in cats.FRONTEND_CATEGORIES.items():
        assert cfg["delegated"] is True, f"Frontend {cat_id} must be delegated=True"

@pytest.mark.regression
def test_regression_is_delegated_function_works():
    assert cats.is_delegated("backend_security_sast") is True
    assert cats.is_delegated("backend_unit") is False
    assert cats.is_delegated("frontend_unit") is True
    assert cats.is_delegated("nonexistent_category") is False

@pytest.mark.regression
def test_regression_category_ids_are_lowercase_underscore():
    """Category IDs must use snake_case only — the frontend maps them by exact name."""
    import re
    all_cats = {**cats.BACKEND_CATEGORIES, **cats.FRONTEND_CATEGORIES}
    for cat_id in all_cats:
        assert re.match(r'^[a-z][a-z0-9_]*$', cat_id), (
            f"Category ID {cat_id!r} must be lowercase snake_case"
        )

@pytest.mark.regression
def test_regression_phase1_categories_exist():
    """Phase 1 security categories must exist — removing them would break dashboard."""
    phase1 = ["backend_security_sast", "backend_security_secrets",
               "backend_security_deps", "backend_coverage"]
    for c in phase1:
        assert c in cats.BACKEND_CATEGORIES, f"Phase 1 category missing: {c}"

@pytest.mark.regression
def test_regression_phase4_security_suites_exist():
    phase4 = ["backend_security_authz", "backend_security_api",
               "backend_security_llm", "backend_security_infra"]
    for c in phase4:
        assert c in cats.BACKEND_CATEGORIES, f"Phase 4 category missing: {c}"

@pytest.mark.regression
def test_regression_phase5_mutation_exists():
    assert "backend_mutation" in cats.BACKEND_CATEGORIES
