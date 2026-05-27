"""Prompt leakage and internal state exposure tests. Verifies that API responses
don't expose internal file paths, stack traces, system configuration, or
implementation details."""

import hashlib
import hmac
import json
import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub heavy transitive dependencies BEFORE importing anything that touches them
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

_s3_stub = sys.modules.get("utils.s3_utils") or MagicMock(name="s3_utils_stub")
_s3_stub.save_app_runbase_S3 = MagicMock(return_value=None)
sys.modules["utils.s3_utils"] = _s3_stub

sys.modules.setdefault("utils.base_logger", MagicMock(name="base_logger_stub"))

_celery_base = sys.modules.get("utils.celery_base") or MagicMock(name="celery_base_stub")
for _task in (
    "run_backend_unit",
    "run_backend_integration",
    "run_backend_regression",
    "run_backend_load",
    "run_backend_stress",
    "run_backend_performance",
):
    _t = MagicMock()
    _t.delay = MagicMock(return_value=MagicMock(id=f"fake-{_task}"))
    setattr(_celery_base, _task, _t)
sys.modules["utils.celery_base"] = _celery_base

from flask import Flask  # noqa: E402

from tests_routes.normalizers import parse_bandit_json  # noqa: E402
from tests_routes.result_store import RESULTS_ROOT, write_category_result  # noqa: E402
from tests_routes.routes import tests_bp  # noqa: E402
from utils.app_configs import ACCESSIBLE_IDS  # noqa: E402

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_NOW = "2024-01-01T00:00:00+00:00"
WEBHOOK_SECRET = "test-leakage-secret"
TEST_USER = ACCESSIBLE_IDS[0] if ACCESSIBLE_IDS else "113605503284012967393"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_signature(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _signed_post(client, path: str, payload: dict, secret: str = WEBHOOK_SECRET):
    body = json.dumps(payload).encode()
    sig = _make_signature(body, secret)
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
@pytest.mark.llm_safety
def test_categories_response_contains_no_internal_paths(client):
    """GET /tests/categories must not reveal internal filesystem paths."""
    resp = client.get("/tests/categories")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "/home/" not in body
    assert "/Users/" not in body
    assert "rds_db" not in body


@pytest.mark.security
@pytest.mark.llm_safety
def test_summary_response_contains_no_internal_paths(client):
    """GET /tests/summary (authorized) must not expose internal RESULTS_ROOT paths."""
    resp = client.get(f"/tests/summary?user_id={TEST_USER}")
    assert resp.status_code == 200
    data = resp.get_json()
    body = json.dumps(data)
    assert "RESULTS_ROOT" not in body
    assert "testing/results" not in body


@pytest.mark.security
@pytest.mark.llm_safety
def test_unknown_category_404_does_not_leak_filesystem_path(client):
    """GET /tests/results/<unknown> 404 must not reveal the testing/results/ path."""
    resp = client.get(f"/tests/results/no_such_category?user_id={TEST_USER}")
    assert resp.status_code in (403, 404)
    body = resp.data.decode()
    assert "testing/results/" not in body


@pytest.mark.security
@pytest.mark.llm_safety
def test_403_response_does_not_leak_accessible_ids(client):
    """GET /tests/summary with no user → 403 must not echo ACCESSIBLE_IDS entries."""
    resp = client.get("/tests/summary")
    assert resp.status_code == 403
    body = resp.data.decode()
    for uid in ACCESSIBLE_IDS:
        assert uid not in body


@pytest.mark.security
@pytest.mark.llm_safety
def test_webhook_401_does_not_leak_secret(client):
    """POST /tests/webhook/ci with no signature → 401 must not expose the secret env var name or value."""
    secret_value = "super-secret-value-abc123"
    with patch.dict(os.environ, {"FRONTEND_TESTS_WEBHOOK_SECRET": secret_value}):
        resp = client.post(
            "/tests/webhook/ci",
            data=json.dumps({"category": "frontend_unit"}).encode(),
            content_type="application/json",
        )
    assert resp.status_code == 401
    body = resp.data.decode()
    # Must not echo the env var value
    assert secret_value not in body
    # May contain the env var NAME in error messaging but must not expose the actual secret
    assert secret_value not in body


@pytest.mark.security
@pytest.mark.llm_safety
def test_run_endpoint_400_does_not_echo_full_request_body(client):
    """POST /tests/run with malformed body → 400/403 must not reflect the full request body."""
    malformed_body = json.dumps({
        "categories": [],
        "secret_field": "hunter2",
        "injection": "'; DROP TABLE users; --",
    }).encode()
    resp = client.post(
        "/tests/run",
        data=malformed_body,
        content_type="application/json",
    )
    assert resp.status_code in (400, 403)
    body = resp.data.decode()
    # The response must not reflect back the full raw request
    assert "hunter2" not in body
    assert malformed_body.decode() not in body


@pytest.mark.security
@pytest.mark.llm_safety
def test_normalizer_output_contains_no_aws_credentials_shape():
    """Bandit normalizer finding containing a fake AWS key is stored as data only — not expanded."""
    fake_aws_key = "AKIA1234567890ABCDEF"
    raw = json.dumps({
        "results": [
            {
                "test_id": "B105",
                "issue_severity": "HIGH",
                "issue_text": f"Hardcoded key: {fake_aws_key}",
                "filename": "config.py",
                "line_number": 1,
                "issue_confidence": "HIGH",
            }
        ]
    })
    result = parse_bandit_json(
        category="backend_security_sast",
        run_id="run-leak-001",
        raw_text=raw,
        started_at=_NOW,
        finished_at=_NOW,
        returncode=1,
    )
    assert isinstance(result, dict)
    # The output is a plain dict payload — not a subprocess call or log expansion
    assert result["tests"][0]["body"] == f"Hardcoded key: {fake_aws_key}"
    # Ensure we're dealing with a normal dict, not anything evaluated
    assert "metrics" in result
    assert result["metrics"]["tool"] == "bandit"


@pytest.mark.security
@pytest.mark.llm_safety
def test_result_store_path_does_not_appear_in_api_responses(client, tmp_path):
    """Reading results via GET /tests/results/<category> must not expose RESULTS_ROOT path."""
    # Patch RESULTS_ROOT so write_category_result writes to tmp_path
    fake_results_root = str(tmp_path)
    dummy_payload = {
        "category": "backend_unit",
        "run_id": "test-run-001",
        "started_at": _NOW,
        "finished_at": _NOW,
        "duration_seconds": 0.0,
        "summary": {"total": 1, "passed": 1, "failed": 0, "skipped": 0, "errors": 0},
        "status": "passed",
        "tests": [],
        "metrics": None,
    }

    with patch("tests_routes.result_store.RESULTS_ROOT", fake_results_root), \
         patch("tests_routes.routes.write_category_result") as mock_write, \
         patch("tests_routes.routes.read_category_result", return_value=dummy_payload):
        resp = client.get(f"/tests/results/backend_unit?user_id={TEST_USER}")

    assert resp.status_code == 200
    body = resp.data.decode()
    # Internal RESULTS_ROOT path must not appear in the response
    assert RESULTS_ROOT not in body
    assert "testing/results" not in body
