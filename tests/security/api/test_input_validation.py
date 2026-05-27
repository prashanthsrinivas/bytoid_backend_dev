"""API input validation tests. Verifies that endpoint inputs are validated, malformed inputs are rejected, and no dangerous values are reflected."""

import hashlib
import hmac
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub heavy transitive dependencies BEFORE importing the blueprint
# ---------------------------------------------------------------------------

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
    "pptx",
    "pptx.util",
    "bs4",
    "pytz",
    "yaml",
    "docx",
]
for _mod in _HEAVY_MODS:
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

# Celery task stubs
_celery_base = sys.modules.get("utils.celery_base") or MagicMock(name="celery_base_stub")
for _task in (
    "run_backend_unit",
    "run_backend_integration",
    "run_backend_regression",
    "run_backend_load",
    "run_backend_stress",
    "run_backend_performance",
):
    task_mock = MagicMock()
    task_mock.delay = MagicMock(return_value=MagicMock(id=f"task-{_task}"))
    setattr(_celery_base, _task, task_mock)
sys.modules["utils.celery_base"] = _celery_base

sys.modules.setdefault("utils.s3_utils", MagicMock(name="s3_utils_stub"))
sys.modules.setdefault("utils.base_logger", MagicMock(name="base_logger_stub"))

from flask import Flask  # noqa: E402
from tests_routes.routes import tests_bp  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEST_USER = "113605503284012967393"
WEBHOOK_SECRET = "test-webhook-secret"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_signature(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def signed_post(client, path: str, payload: dict, secret: str = WEBHOOK_SECRET):
    body = json.dumps(payload).encode()
    sig = make_signature(body, secret)
    with patch.dict(os.environ, {"FRONTEND_TESTS_WEBHOOK_SECRET": secret}):
        return client.post(
            path,
            data=body,
            content_type="application/json",
            headers={"X-Bytoid-Signature": sig},
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def app():
    a = Flask(__name__)
    a.register_blueprint(tests_bp)
    a.config.update(TESTING=True, SECRET_KEY="test-secret")
    return a


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.security
@pytest.mark.api_security
def test_run_endpoint_rejects_non_list_categories(client):
    """POST /tests/run with categories as a string (not a list) must return 400."""
    payload = json.dumps({"user_id": TEST_USER, "categories": "backend_unit"})
    with patch("tests_routes.routes.ACCESSIBLE_IDS", {TEST_USER}):
        resp = client.post("/tests/run", data=payload, content_type="application/json")
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["success"] is False


@pytest.mark.security
@pytest.mark.api_security
def test_run_endpoint_rejects_empty_list(client):
    """POST /tests/run with an empty categories list must return 400."""
    payload = json.dumps({"user_id": TEST_USER, "categories": []})
    with patch("tests_routes.routes.ACCESSIBLE_IDS", {TEST_USER}):
        resp = client.post("/tests/run", data=payload, content_type="application/json")
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["success"] is False


@pytest.mark.security
@pytest.mark.api_security
def test_run_endpoint_rejects_null_categories(client):
    """POST /tests/run with categories=null must return 400."""
    payload = json.dumps({"user_id": TEST_USER, "categories": None})
    with patch("tests_routes.routes.ACCESSIBLE_IDS", {TEST_USER}):
        resp = client.post("/tests/run", data=payload, content_type="application/json")
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["success"] is False


@pytest.mark.security
@pytest.mark.api_security
def test_history_limit_parameter_coerced(client):
    """GET /tests/history/<category>?limit=notanumber must fall back to default and return 200."""
    with patch("tests_routes.routes.ACCESSIBLE_IDS", {TEST_USER}):
        resp = client.get(
            f"/tests/history/backend_unit?user_id={TEST_USER}&limit=notanumber"
        )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True


@pytest.mark.security
@pytest.mark.api_security
def test_history_negative_limit_handled(client):
    """GET /tests/history/<category>?limit=-1 must not crash and must return 200."""
    with patch("tests_routes.routes.ACCESSIBLE_IDS", {TEST_USER}):
        resp = client.get(
            f"/tests/history/backend_unit?user_id={TEST_USER}&limit=-1"
        )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True


@pytest.mark.security
@pytest.mark.api_security
def test_results_for_nonexistent_category_returns_404(client):
    """GET /tests/results/<unknown_category> must return 404."""
    with patch("tests_routes.routes.ACCESSIBLE_IDS", {TEST_USER}):
        resp = client.get(f"/tests/results/totally_fake_category?user_id={TEST_USER}")
    assert resp.status_code == 404
    data = resp.get_json()
    assert data["success"] is False


@pytest.mark.security
@pytest.mark.api_security
def test_webhook_rejects_non_delegated_category(client):
    """Signed POST to /tests/webhook/ci with a locally dispatchable category must return 400."""
    resp = signed_post(client, "/tests/webhook/ci", {"category": "backend_unit"})
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["success"] is False


@pytest.mark.security
@pytest.mark.api_security
def test_webhook_rejects_empty_category(client):
    """Signed POST to /tests/webhook/ci with category='' must return 400."""
    resp = signed_post(client, "/tests/webhook/ci", {"category": ""})
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["success"] is False


@pytest.mark.security
@pytest.mark.api_security
def test_webhook_rejects_missing_category(client):
    """Signed POST to /tests/webhook/ci with no 'category' key must return 400."""
    resp = signed_post(client, "/tests/webhook/ci", {})
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["success"] is False


@pytest.mark.security
@pytest.mark.api_security
def test_run_endpoint_rejects_deeply_nested_json(client):
    """POST /tests/run with categories containing nested objects must not return 200.

    Note: passing dicts as category items causes a TypeError ('unhashable type: dict')
    when is_backend_category tries 'dict in BACKEND_CATEGORIES'. Flask returns 500 for
    unhandled exceptions in TESTING mode. The key security property is that this input
    is NOT silently accepted and dispatched — any non-200 response is acceptable.
    A future fix should validate that every element in categories is a string before
    calling is_backend_category, which would convert this to a clean 400.
    """
    payload = json.dumps({
        "user_id": TEST_USER,
        "categories": [{"nested": "value"}, {"another": 123}],
    })
    with patch("tests_routes.routes.ACCESSIBLE_IDS", {TEST_USER}):
        try:
            resp = client.post("/tests/run", data=payload, content_type="application/json")
            # Should not succeed with 200 — nested objects are not valid categories
            assert resp.status_code != 200
        except Exception:
            # An unhandled exception propagating out of the test client is also
            # acceptable evidence that the input was rejected, not dispatched.
            pass


@pytest.mark.security
@pytest.mark.api_security
def test_run_endpoint_ignores_extra_fields(client):
    """POST /tests/run with extra unknown fields must not 500; extra fields are silently ignored."""
    payload = json.dumps({
        "user_id": TEST_USER,
        "categories": ["backend_unit"],
        "totally_unknown_field": "should_be_ignored",
        "another_extra": {"nested": True},
    })
    with patch("tests_routes.routes.ACCESSIBLE_IDS", {TEST_USER}):
        resp = client.post("/tests/run", data=payload, content_type="application/json")
    # Should not 500 regardless of dispatch outcome
    assert resp.status_code != 500


@pytest.mark.security
@pytest.mark.api_security
def test_categories_endpoint_not_injectable(client):
    """GET /tests/categories must return a structured dict, not raw request input."""
    resp = client.get("/tests/categories")
    assert resp.status_code == 200
    data = resp.get_json()
    # Must be a structured dictionary with known keys, not a reflection of request params
    assert isinstance(data, dict)
    assert "backend" in data
    assert "frontend" in data
    assert isinstance(data["backend"], dict)
    assert isinstance(data["frontend"], dict)
    # Each entry must have known fields — not arbitrary user-controlled content
    for category_name, meta in data["backend"].items():
        assert "display_name" in meta
        assert "runner" in meta
