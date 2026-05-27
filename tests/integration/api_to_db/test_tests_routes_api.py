"""Integration tests for the tests_routes Flask blueprint.

Covers the full HTTP layer of tests_routes/routes.py:
  - /tests/categories (unauthenticated static metadata)
  - /tests/summary (auth-gated aggregate view)
  - /tests/run (dispatch gate, category validation, delegated-category handling)
  - /tests/webhook/ci (HMAC signature enforcement)
  - /tests/results/<category> and /tests/history/<category> (404 for bad cats)

All DB, Celery, and S3 imports are stubbed before the blueprint is imported so
this module can run without AWS credentials or a live database.
"""

import hashlib
import hmac
import json
import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

# ---------------------------------------------------------------------------
# Stub heavy transitive dependencies BEFORE importing tests_routes.routes
# ---------------------------------------------------------------------------

def _stub_module(name: str, **attrs) -> MagicMock:
    mod = MagicMock(name=name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


_HEAVY_MODS = [
    "pymysql",
    "pymysql.cursors",
    "db",
    "db.rds_db",
    "db.db_checkers",
    "boto3",
    "dotenv",
    "dbutils",
    "dbutils.pooled_db",
    # utils.normal imports these at module level
    "pptx",
    "pptx.util",
    "bs4",
    "pytz",
    "yaml",
    "docx",
]
for _mod in _HEAVY_MODS:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock(name=f"{_mod}_stub")

# Celery task attrs needed by _dispatch_backend_task
_celery_base = MagicMock(name="celery_base_stub")
_celery_base.run_backend_unit = MagicMock()
_celery_base.run_backend_unit.delay = MagicMock(return_value=MagicMock(id="task-unit-001"))
_celery_base.run_backend_integration = MagicMock()
_celery_base.run_backend_integration.delay = MagicMock(return_value=MagicMock(id="task-int-001"))
_celery_base.run_backend_regression = MagicMock()
_celery_base.run_backend_regression.delay = MagicMock(return_value=MagicMock(id="task-reg-001"))
_celery_base.run_backend_load = MagicMock()
_celery_base.run_backend_load.delay = MagicMock(return_value=MagicMock(id="task-load-001"))
_celery_base.run_backend_stress = MagicMock()
_celery_base.run_backend_stress.delay = MagicMock(return_value=MagicMock(id="task-stress-001"))
_celery_base.run_backend_performance = MagicMock()
_celery_base.run_backend_performance.delay = MagicMock(return_value=MagicMock(id="task-perf-001"))
sys.modules["utils.celery_base"] = _celery_base

# Stub utils.s3_utils
_s3_stub = MagicMock(name="s3_utils_stub")
_s3_stub.save_app_runbase_S3 = MagicMock(return_value=None)
sys.modules["utils.s3_utils"] = _s3_stub

# Stub utils.base_logger so get_logger doesn't crash
_logger_stub = MagicMock(name="base_logger_stub")
_logger_stub.get_logger = MagicMock(return_value=MagicMock())
sys.modules["utils.base_logger"] = _logger_stub

# Now it's safe to import the blueprint
from tests_routes.routes import tests_bp  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEST_USER = "test-admin-user-abc123"
WEBHOOK_SECRET = "test-webhook-secret-xyz"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def app(tmp_path):
    """Minimal Flask app with the tests blueprint registered."""
    flask_app = Flask(__name__)
    flask_app.register_blueprint(tests_bp)
    flask_app.config["TESTING"] = True
    flask_app.config["SECRET_KEY"] = "test-secret-key"
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _make_valid_sig(body: bytes) -> str:
    digest = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# ---------------------------------------------------------------------------
# Helper context manager: patch ACCESSIBLE_IDS to include TEST_USER
# ---------------------------------------------------------------------------

def _authorized_ids():
    return patch("tests_routes.routes.ACCESSIBLE_IDS", {TEST_USER})


# ---------------------------------------------------------------------------
# Test 1: GET /tests/categories returns success with backend and frontend
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_get_categories_returns_success(client):
    """Categories endpoint is public and always returns success."""
    resp = client.get("/tests/categories")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True


@pytest.mark.integration
def test_get_categories_has_backend_and_frontend(client):
    """Categories response contains both backend and frontend dicts with expected keys."""
    resp = client.get("/tests/categories")
    data = resp.get_json()
    assert "backend" in data
    assert "frontend" in data

    # At least one key in each dict should look like a known category
    backend = data["backend"]
    frontend = data["frontend"]

    assert "backend_unit" in backend
    assert "backend_integration" in backend
    assert "frontend_unit" in frontend

    # Each category entry should have display_name and runner
    bu = backend["backend_unit"]
    assert "display_name" in bu
    assert "runner" in bu


# ---------------------------------------------------------------------------
# Test 3: GET /tests/summary requires auth
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_summary_requires_auth(client):
    """GET /tests/summary with no user_id returns 403."""
    resp = client.get("/tests/summary")
    assert resp.status_code == 403
    data = resp.get_json()
    assert data["success"] is False


# ---------------------------------------------------------------------------
# Test 4: GET /tests/summary succeeds with authorized user
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_summary_authorized(client):
    """GET /tests/summary with authorized user_id in query returns 200."""
    with _authorized_ids():
        resp = client.get(f"/tests/summary?user_id={TEST_USER}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert "categories" in data


# ---------------------------------------------------------------------------
# Test 5: POST /tests/run requires body with user_id
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_run_requires_body(client):
    """POST /tests/run with no body/user_id returns 403 (no authorized user)."""
    resp = client.post(
        "/tests/run",
        data="{}",
        content_type="application/json",
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Test 6: POST /tests/run with empty categories returns 400
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_run_requires_categories(client):
    """POST /tests/run with authorized user but empty categories list returns 400."""
    payload = json.dumps({"user_id": TEST_USER, "categories": []})
    with _authorized_ids():
        resp = client.post(
            "/tests/run",
            data=payload,
            content_type="application/json",
        )
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["success"] is False
    assert "categories" in data["error"].lower()


# ---------------------------------------------------------------------------
# Test 7: POST /tests/run rejects frontend categories
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_run_rejects_frontend_categories(client):
    """Frontend categories cannot be dispatched from /tests/run — expect 400."""
    payload = json.dumps({
        "user_id": TEST_USER,
        "categories": ["frontend_unit"],
    })
    with _authorized_ids():
        resp = client.post(
            "/tests/run",
            data=payload,
            content_type="application/json",
        )
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["success"] is False
    # The rejected category should be listed in the 'invalid' field
    assert "frontend_unit" in data.get("invalid", [])


# ---------------------------------------------------------------------------
# Test 8: POST /tests/run with delegated backend category lands in failures[]
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_run_rejects_delegated_backend_categories(client):
    """Delegated categories (backend_security_sast) land in failures[], not dispatched."""
    payload = json.dumps({
        "user_id": TEST_USER,
        "categories": ["backend_security_sast"],
    })
    with _authorized_ids():
        resp = client.post(
            "/tests/run",
            data=payload,
            content_type="application/json",
        )
    # The endpoint still returns 200 — it accepted the request, but the category
    # is in failures[] because it is delegated to CI.
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    failures = data.get("failures", [])
    assert any(f["category"] == "backend_security_sast" for f in failures)
    # It must not appear in dispatched[]
    dispatched = data.get("dispatched", [])
    assert not any(d["category"] == "backend_security_sast" for d in dispatched)


# ---------------------------------------------------------------------------
# Test 9: POST /tests/webhook/ci without signature header → 401
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_webhook_rejects_missing_signature(client):
    """Webhook endpoint with no X-Bytoid-Signature header returns 401."""
    os.environ["FRONTEND_TESTS_WEBHOOK_SECRET"] = WEBHOOK_SECRET
    try:
        resp = client.post(
            "/tests/webhook/ci",
            data=json.dumps({"category": "frontend_unit"}),
            content_type="application/json",
        )
    finally:
        os.environ.pop("FRONTEND_TESTS_WEBHOOK_SECRET", None)
    assert resp.status_code == 401
    assert resp.get_json()["success"] is False


# ---------------------------------------------------------------------------
# Test 10: POST /tests/webhook/ci with wrong signature → 401
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_webhook_rejects_invalid_signature(client):
    """Webhook endpoint with a wrong signature returns 401."""
    os.environ["FRONTEND_TESTS_WEBHOOK_SECRET"] = WEBHOOK_SECRET
    try:
        body = json.dumps({"category": "frontend_unit"}).encode()
        resp = client.post(
            "/tests/webhook/ci",
            data=body,
            content_type="application/json",
            headers={"X-Bytoid-Signature": "sha256=deadbeef00000000"},
        )
    finally:
        os.environ.pop("FRONTEND_TESTS_WEBHOOK_SECRET", None)
    assert resp.status_code == 401
    assert resp.get_json()["success"] is False


# ---------------------------------------------------------------------------
# Test 11: GET /tests/results/<bad_category> → 404
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_results_for_unknown_category(client):
    """GET /tests/results/<unknown_category> returns 404."""
    with _authorized_ids():
        resp = client.get(f"/tests/results/bad_category?user_id={TEST_USER}")
    assert resp.status_code == 404
    data = resp.get_json()
    assert data["success"] is False


# ---------------------------------------------------------------------------
# Test 12: GET /tests/history/<bad_category> → 404
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_history_unknown_category(client):
    """GET /tests/history/<unknown_category> returns 404."""
    with _authorized_ids():
        resp = client.get(f"/tests/history/bad_category?user_id={TEST_USER}")
    assert resp.status_code == 404
    data = resp.get_json()
    assert data["success"] is False
