"""Oversized payload tests. Verifies that large request bodies are handled without crashing or memory exhaustion."""

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

_s3_stub = sys.modules.get("utils.s3_utils") or MagicMock(name="s3_utils_stub")
_s3_stub.save_app_runbase_S3 = MagicMock(return_value=None)
sys.modules["utils.s3_utils"] = _s3_stub
sys.modules.setdefault("utils.base_logger", MagicMock(name="base_logger_stub"))

from flask import Flask  # noqa: E402
from tests_routes.routes import tests_bp  # noqa: E402
import tests_routes.result_store as rs  # noqa: E402

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


def signed_post_raw(client, path: str, body: bytes, secret: str = WEBHOOK_SECRET):
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


@pytest.fixture
def isolated_store(tmp_path):
    """Redirect RESULTS_ROOT for tests that exercise result_store directly."""
    orig_root, orig_summary = rs.RESULTS_ROOT, rs.SUMMARY_PATH
    rs.RESULTS_ROOT = str(tmp_path)
    rs.SUMMARY_PATH = str(tmp_path / "summary.json")
    yield tmp_path
    rs.RESULTS_ROOT = orig_root
    rs.SUMMARY_PATH = orig_summary


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.security
@pytest.mark.api_security
@pytest.mark.slow
def test_oversized_json_body_does_not_crash_run_endpoint(client):
    """POST /tests/run with ~1MB body must not 500 — auth or validation rejects it cleanly."""
    # Build a 1MB-ish categories list of long strings (none are valid categories)
    large_categories = ["x" * 200 for _ in range(5000)]
    payload = json.dumps({"user_id": "attacker", "categories": large_categories}).encode()
    assert len(payload) > 1_000_000, "Payload must be at least 1MB for this test"

    resp = client.post(
        "/tests/run",
        data=payload,
        content_type="application/json",
    )
    # Must not 500 — either 403 (auth fails) or 400 (bad input)
    assert resp.status_code != 500
    assert resp.status_code in (400, 403)


@pytest.mark.security
@pytest.mark.api_security
@pytest.mark.slow
def test_oversized_webhook_body_does_not_crash(client):
    """POST /tests/webhook/ci with ~500KB body + valid HMAC must not 500."""
    # Build a large body: valid JSON structure but with an oversized string value
    large_payload = {"category": "frontend_unit", "data": "y" * 500_000}
    body = json.dumps(large_payload).encode()
    assert len(body) > 500_000

    resp = signed_post_raw(client, "/tests/webhook/ci", body)
    # Must not crash the server
    assert resp.status_code != 500


@pytest.mark.security
@pytest.mark.api_security
@pytest.mark.slow
def test_large_metadata_in_audit_log_does_not_raise():
    """log_audit_event with a 10,000-key metadata dict must not raise any exception."""
    # We need to stub the audit log's transitive deps (db, s3)
    from services.audit_log_service import log_audit_event

    large_metadata = {f"key_{i}": f"value_{i}" for i in range(10_000)}

    # log_audit_event is documented to never raise; verify that contract holds
    try:
        log_audit_event(
            action="TESTS_RUN_DISPATCHED",
            endpoint="/tests/run",
            ip="127.0.0.1",
            status="success",
            metadata=large_metadata,
        )
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"log_audit_event raised unexpectedly: {exc}")


@pytest.mark.security
@pytest.mark.api_security
@pytest.mark.slow
def test_run_endpoint_handles_very_long_user_id(client):
    """GET /tests/summary with a 10,000-character user_id must return 403, not crash."""
    long_user_id = "A" * 10_000
    resp = client.get(f"/tests/summary?user_id={long_user_id}")
    # Not in ACCESSIBLE_IDS → 403, but no crash
    assert resp.status_code == 403
    data = resp.get_json()
    assert data["success"] is False


@pytest.mark.security
@pytest.mark.api_security
@pytest.mark.slow
def test_webhook_handles_very_long_category_name(client):
    """Signed POST with category='x'*10000 must return 400 (invalid delegated category), not crash."""
    very_long_category = "x" * 10_000
    payload = {"category": very_long_category}
    body = json.dumps(payload).encode()
    sig = make_signature(body)
    with patch.dict(os.environ, {"FRONTEND_TESTS_WEBHOOK_SECRET": WEBHOOK_SECRET}):
        resp = client.post(
            "/tests/webhook/ci",
            data=body,
            content_type="application/json",
            headers={"X-Bytoid-Signature": sig},
        )
    # Not a valid delegated category → 400, no crash
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["success"] is False


@pytest.mark.security
@pytest.mark.api_security
@pytest.mark.slow
def test_result_store_handles_large_payload(isolated_store):
    """Writing a ~100KB result payload must persist and read back without corruption."""
    large_tests = [{"id": f"test-{i}", "name": f"Test case {i}", "status": "passed"} for i in range(500)]
    large_payload = {
        "category": "backend_unit",
        "run_id": "20260101T000000Z-large001",
        "status": "passed",
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:01:00+00:00",
        "summary": {"total": 500, "passed": 500, "failed": 0},
        "tests": large_tests,
        "metrics": None,
        "extra_data": "z" * 120_000,  # pad to ensure well over 100KB
    }

    import json as _json
    payload_bytes = _json.dumps(large_payload).encode()
    assert len(payload_bytes) > 100_000, "Payload must be at least 100KB for this test"

    rs.write_category_result("backend_unit", "20260101T000000Z-large001", large_payload)
    result = rs.read_category_result("backend_unit")

    assert result is not None
    assert result["status"] == "passed"
    assert result["run_id"] == "20260101T000000Z-large001"
    assert len(result["tests"]) == 500
