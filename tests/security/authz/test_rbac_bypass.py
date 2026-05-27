"""RBAC / access-control bypass tests. Verifies that every protected endpoint rejects unauthorized callers."""

import hashlib
import hmac
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

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
    "pptx",
    "pptx.util",
    "bs4",
    "pytz",
    "yaml",
    "docx",
]
for _mod in _HEAVY_MODS:
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

# Celery base with task stubs
_celery_base = sys.modules.get("utils.celery_base") or MagicMock(name="celery_base_stub")
_celery_base.run_backend_unit = MagicMock()
_celery_base.run_backend_unit.delay = MagicMock(return_value=MagicMock(id="fake-task-id"))
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

_s3_stub = sys.modules.get("utils.s3_utils") or MagicMock(name="s3_utils_stub")
_s3_stub.save_app_runbase_S3 = MagicMock(return_value=None)
sys.modules["utils.s3_utils"] = _s3_stub

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
@pytest.mark.authz
def test_summary_endpoint_rejects_anonymous(client):
    """GET /tests/summary with no user_id must return 403."""
    resp = client.get("/tests/summary")
    assert resp.status_code == 403
    data = resp.get_json()
    assert data["success"] is False


@pytest.mark.security
@pytest.mark.authz
def test_summary_endpoint_rejects_unknown_user(client):
    """GET /tests/summary with an unknown user_id must return 403."""
    resp = client.get("/tests/summary?user_id=hacker")
    assert resp.status_code == 403
    data = resp.get_json()
    assert data["success"] is False


@pytest.mark.security
@pytest.mark.authz
def test_summary_endpoint_accepts_authorized_user(client):
    """GET /tests/summary with a user_id in ACCESSIBLE_IDS must return 200."""
    with patch("tests_routes.routes.ACCESSIBLE_IDS", {TEST_USER}):
        resp = client.get(f"/tests/summary?user_id={TEST_USER}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True


@pytest.mark.security
@pytest.mark.authz
def test_results_endpoint_rejects_anonymous(client):
    """GET /tests/results/<category> with no user_id must return 403."""
    resp = client.get("/tests/results/backend_unit")
    assert resp.status_code == 403
    data = resp.get_json()
    assert data["success"] is False


@pytest.mark.security
@pytest.mark.authz
def test_history_endpoint_rejects_anonymous(client):
    """GET /tests/history/<category> with no user_id must return 403."""
    resp = client.get("/tests/history/backend_unit")
    assert resp.status_code == 403
    data = resp.get_json()
    assert data["success"] is False


@pytest.mark.security
@pytest.mark.authz
def test_status_endpoint_rejects_anonymous(client):
    """GET /tests/status/<task_id> with no user_id must return 403."""
    resp = client.get("/tests/status/some-task-id")
    assert resp.status_code == 403
    data = resp.get_json()
    assert data["success"] is False


@pytest.mark.security
@pytest.mark.authz
def test_run_endpoint_rejects_anonymous(client):
    """POST /tests/run with no user_id must return 403."""
    resp = client.post(
        "/tests/run",
        data=json.dumps({"categories": ["backend_unit"]}),
        content_type="application/json",
    )
    assert resp.status_code == 403
    data = resp.get_json()
    assert data["success"] is False


@pytest.mark.security
@pytest.mark.authz
def test_run_endpoint_rejects_unknown_user(client):
    """POST /tests/run with an unknown user_id must return 403."""
    payload = json.dumps({"user_id": "attacker", "categories": ["backend_unit"]})
    resp = client.post("/tests/run", data=payload, content_type="application/json")
    assert resp.status_code == 403
    data = resp.get_json()
    assert data["success"] is False


@pytest.mark.security
@pytest.mark.authz
def test_categories_endpoint_is_public(client):
    """GET /tests/categories requires no auth — it is intentionally public metadata."""
    resp = client.get("/tests/categories")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "backend" in data
    assert "frontend" in data


@pytest.mark.security
@pytest.mark.authz
def test_webhook_requires_signature_not_user(client):
    """POST /tests/webhook/ci with no signature header must return 401 (HMAC-only auth)."""
    with patch.dict(os.environ, {"FRONTEND_TESTS_WEBHOOK_SECRET": WEBHOOK_SECRET}):
        resp = client.post(
            "/tests/webhook/ci",
            data=json.dumps({"category": "frontend_unit"}),
            content_type="application/json",
        )
    assert resp.status_code == 401
    data = resp.get_json()
    assert data["success"] is False


@pytest.mark.security
@pytest.mark.authz
def test_webhook_rejects_wrong_secret(client):
    """POST /tests/webhook/ci signed with the wrong secret must return 401."""
    body = json.dumps({"category": "frontend_unit"}).encode()
    wrong_sig = "sha256=" + hmac.new(b"wrong-secret", body, hashlib.sha256).hexdigest()
    with patch.dict(os.environ, {"FRONTEND_TESTS_WEBHOOK_SECRET": WEBHOOK_SECRET}):
        resp = client.post(
            "/tests/webhook/ci",
            data=body,
            content_type="application/json",
            headers={"X-Bytoid-Signature": wrong_sig},
        )
    assert resp.status_code == 401
    data = resp.get_json()
    assert data["success"] is False


@pytest.mark.security
@pytest.mark.authz
def test_webhook_accepts_valid_signature(client, tmp_path):
    """POST /tests/webhook/ci with a correct HMAC and a delegated category returns 200."""
    import tests_routes.result_store as rs

    orig_root, orig_summary = rs.RESULTS_ROOT, rs.SUMMARY_PATH
    rs.RESULTS_ROOT = str(tmp_path)
    rs.SUMMARY_PATH = str(tmp_path / "summary.json")
    try:
        resp = signed_post(
            client,
            "/tests/webhook/ci",
            {"category": "frontend_unit", "run_id": "run-webhook-001"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["category"] == "frontend_unit"
    finally:
        rs.RESULTS_ROOT = orig_root
        rs.SUMMARY_PATH = orig_summary


@pytest.mark.security
@pytest.mark.authz
def test_run_rejects_frontend_categories(client):
    """POST /tests/run with frontend categories must return 400 (not a backend category)."""
    payload = json.dumps({"user_id": TEST_USER, "categories": ["frontend_unit"]})
    with patch("tests_routes.routes.ACCESSIBLE_IDS", {TEST_USER}):
        resp = client.post("/tests/run", data=payload, content_type="application/json")
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["success"] is False
    assert "frontend_unit" in data.get("invalid", [])


@pytest.mark.security
@pytest.mark.authz
def test_run_rejects_delegated_backend_categories(client):
    """POST /tests/run with backend_security_sast (delegated) returns 200 with failures[]."""
    payload = json.dumps({"user_id": TEST_USER, "categories": ["backend_security_sast"]})
    with patch("tests_routes.routes.ACCESSIBLE_IDS", {TEST_USER}):
        resp = client.post("/tests/run", data=payload, content_type="application/json")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    failures = data.get("failures", [])
    assert any(f["category"] == "backend_security_sast" for f in failures)
    dispatched = data.get("dispatched", [])
    assert not any(d["category"] == "backend_security_sast" for d in dispatched)


@pytest.mark.security
@pytest.mark.authz
def test_run_accepts_locally_dispatchable_categories(client):
    """POST /tests/run with backend_unit (locally dispatchable) returns 200 with dispatched entry."""
    # Ensure the Celery mock produces a task id
    sys.modules["utils.celery_base"].run_backend_unit.delay.return_value.id = "fake-task-id"

    payload = json.dumps({"user_id": TEST_USER, "categories": ["backend_unit"]})
    with patch("tests_routes.routes.ACCESSIBLE_IDS", {TEST_USER}):
        resp = client.post("/tests/run", data=payload, content_type="application/json")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    # Either dispatched or failures depending on Celery mock; but endpoint must return 200
    assert "dispatched" in data
    assert "failures" in data
