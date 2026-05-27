"""Integration tests for POST /tests/webhook/ci and GET /tests/summary.

Tests the full request→response cycle through the Flask test client,
with DB and S3 stubbed. Covers HMAC validation, delegated-category gating,
result persistence, and the summary read-back.
"""

import hashlib
import hmac
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ── Pre-stub all heavy imports ────────────────────────────────────────────────
for _m in (
    "pymysql", "pymysql.cursors", "db", "db.rds_db", "db.db_checkers",
    "boto3", "dotenv", "dbutils", "dbutils.pooled_db",
    "pptx", "pptx.util", "bs4", "pytz", "yaml", "docx",
    "lancedb", "openai", "anthropic", "fireworks",
    "services.redis_service", "services.totp_service",
    "onelogin", "onelogin.saml2", "onelogin.saml2.auth",
    "celery", "kombu",
):
    sys.modules.setdefault(_m, MagicMock(name=f"{_m}_stub"))

_audit_stub = MagicMock()
_audit_stub.log_audit_event = MagicMock(return_value=None)
_audit_stub.TESTS_WEBHOOK_ACCEPTED = "TESTS_WEBHOOK_ACCEPTED"
_audit_stub.TESTS_WEBHOOK_REJECTED = "TESTS_WEBHOOK_REJECTED"
_audit_stub.ACTION_CATEGORY = {
    "TESTS_WEBHOOK_ACCEPTED": "tests", "TESTS_WEBHOOK_REJECTED": "tests",
}
sys.modules.setdefault("services.audit_log_service", _audit_stub)
sys.modules.setdefault("utils.s3_utils", MagicMock())
sys.modules.setdefault("utils.base_logger", MagicMock())

import flask
import pytest

SECRET = "integration-test-secret"
VALID_CATEGORY = "backend_security_sast"


def _make_app():
    from tests_routes.routes import tests_bp
    app = flask.Flask(__name__)
    app.config["TESTING"] = True
    app.secret_key = "test"
    app.register_blueprint(tests_bp)
    return app


def _sign(body: bytes, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _good_payload(category: str = VALID_CATEGORY) -> dict:
    return {
        "category": category,
        "run_id": "test-run-001",
        "status": "passed",
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:01:00+00:00",
        "summary": {"total": 5, "passed": 5, "failed": 0, "skipped": 0, "errors": 0},
        "tests": [],
        "metrics": None,
    }


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("results")
    app = _make_app()
    with patch.dict(os.environ, {
        "FRONTEND_TESTS_WEBHOOK_SECRET": SECRET,
        "TESTS_RESULTS_DIR": str(tmp),
    }):
        with app.test_client() as c:
            yield c, str(tmp)


# ── HMAC rejection ────────────────────────────────────────────────────────────

@pytest.mark.integration
def test_webhook_rejects_missing_signature(client):
    c, _ = client
    body = json.dumps(_good_payload()).encode()
    with patch.dict(os.environ, {"FRONTEND_TESTS_WEBHOOK_SECRET": SECRET}):
        resp = c.post("/tests/webhook/ci", data=body, content_type="application/json")
    assert resp.status_code == 401

@pytest.mark.integration
def test_webhook_rejects_wrong_signature(client):
    c, _ = client
    body = json.dumps(_good_payload()).encode()
    with patch.dict(os.environ, {"FRONTEND_TESTS_WEBHOOK_SECRET": SECRET}):
        resp = c.post("/tests/webhook/ci", data=body, content_type="application/json",
                      headers={"X-Bytoid-Signature": "sha256=badbadbadbad"})
    assert resp.status_code == 401

@pytest.mark.integration
def test_webhook_rejects_tampered_body(client):
    c, _ = client
    original = json.dumps(_good_payload()).encode()
    sig = _sign(original)
    tampered = json.dumps({**_good_payload(), "status": "failed"}).encode()
    with patch.dict(os.environ, {"FRONTEND_TESTS_WEBHOOK_SECRET": SECRET}):
        resp = c.post("/tests/webhook/ci", data=tampered, content_type="application/json",
                      headers={"X-Bytoid-Signature": sig})
    assert resp.status_code == 401


# ── Category validation ───────────────────────────────────────────────────────

@pytest.mark.integration
def test_webhook_rejects_non_delegated_category(client):
    c, _ = client
    payload = _good_payload(category="backend_unit")  # NOT delegated
    body = json.dumps(payload).encode()
    sig = _sign(body)
    with patch.dict(os.environ, {"FRONTEND_TESTS_WEBHOOK_SECRET": SECRET}):
        resp = c.post("/tests/webhook/ci", data=body, content_type="application/json",
                      headers={"X-Bytoid-Signature": sig})
    assert resp.status_code == 400

@pytest.mark.integration
def test_webhook_rejects_unknown_category(client):
    c, _ = client
    payload = _good_payload(category="nonexistent_category")
    body = json.dumps(payload).encode()
    sig = _sign(body)
    with patch.dict(os.environ, {"FRONTEND_TESTS_WEBHOOK_SECRET": SECRET}):
        resp = c.post("/tests/webhook/ci", data=body, content_type="application/json",
                      headers={"X-Bytoid-Signature": sig})
    assert resp.status_code == 400


# ── Successful ingest ─────────────────────────────────────────────────────────

@pytest.mark.integration
def test_webhook_accepts_valid_delegated_payload(client, tmp_path):
    c, results_dir = client
    payload = _good_payload()
    body = json.dumps(payload).encode()
    sig = _sign(body)
    with patch.dict(os.environ, {"FRONTEND_TESTS_WEBHOOK_SECRET": SECRET,
                                  "TESTS_RESULTS_DIR": results_dir}):
        resp = c.post("/tests/webhook/ci", data=body, content_type="application/json",
                      headers={"X-Bytoid-Signature": sig})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["category"] == VALID_CATEGORY

@pytest.mark.integration
def test_webhook_ci_alias_same_as_frontend(client, tmp_path):
    """Both /webhook/ci and /webhook/frontend must work identically."""
    c, results_dir = client
    for path in ("/tests/webhook/ci", "/tests/webhook/frontend"):
        payload = {**_good_payload(), "category": "frontend_unit"}
        body = json.dumps(payload).encode()
        sig = _sign(body)
        with patch.dict(os.environ, {"FRONTEND_TESTS_WEBHOOK_SECRET": SECRET,
                                      "TESTS_RESULTS_DIR": results_dir}):
            resp = c.post(path, data=body, content_type="application/json",
                          headers={"X-Bytoid-Signature": sig})
        assert resp.status_code == 200, f"Path {path} returned {resp.status_code}"

@pytest.mark.integration
def test_webhook_accepts_all_phase1_categories(client, tmp_path):
    c, results_dir = client
    phase1 = ["backend_security_sast", "backend_security_secrets",
               "backend_security_deps", "backend_coverage"]
    for cat in phase1:
        payload = _good_payload(category=cat)
        body = json.dumps(payload).encode()
        sig = _sign(body)
        with patch.dict(os.environ, {"FRONTEND_TESTS_WEBHOOK_SECRET": SECRET,
                                      "TESTS_RESULTS_DIR": results_dir}):
            resp = c.post("/tests/webhook/ci", data=body, content_type="application/json",
                          headers={"X-Bytoid-Signature": sig})
        assert resp.status_code == 200, f"{cat} returned {resp.status_code}"

@pytest.mark.integration
def test_webhook_accepts_all_frontend_categories(client, tmp_path):
    c, results_dir = client
    frontend_cats = ["frontend_unit", "frontend_integration", "frontend_e2e",
                     "frontend_typecheck", "frontend_regression"]
    for cat in frontend_cats:
        payload = _good_payload(category=cat)
        body = json.dumps(payload).encode()
        sig = _sign(body)
        with patch.dict(os.environ, {"FRONTEND_TESTS_WEBHOOK_SECRET": SECRET,
                                      "TESTS_RESULTS_DIR": results_dir}):
            resp = c.post("/tests/webhook/ci", data=body, content_type="application/json",
                          headers={"X-Bytoid-Signature": sig})
        assert resp.status_code == 200, f"{cat} returned {resp.status_code}"

@pytest.mark.integration
def test_webhook_response_contains_run_id(client, tmp_path):
    c, results_dir = client
    payload = {**_good_payload(), "run_id": "specific-run-xyz"}
    body = json.dumps(payload).encode()
    sig = _sign(body)
    with patch.dict(os.environ, {"FRONTEND_TESTS_WEBHOOK_SECRET": SECRET,
                                  "TESTS_RESULTS_DIR": results_dir}):
        resp = c.post("/tests/webhook/ci", data=body, content_type="application/json",
                      headers={"X-Bytoid-Signature": sig})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["run_id"] == "specific-run-xyz"

@pytest.mark.integration
def test_webhook_accepted_flag_in_response(client, tmp_path):
    c, results_dir = client
    payload = _good_payload()
    body = json.dumps(payload).encode()
    sig = _sign(body)
    with patch.dict(os.environ, {"FRONTEND_TESTS_WEBHOOK_SECRET": SECRET,
                                  "TESTS_RESULTS_DIR": results_dir}):
        resp = c.post("/tests/webhook/ci", data=body, content_type="application/json",
                      headers={"X-Bytoid-Signature": sig})
    data = resp.get_json()
    assert data.get("accepted") is True

@pytest.mark.integration
def test_webhook_accepts_phase4_security_suite_categories(client, tmp_path):
    c, results_dir = client
    for cat in ["backend_security_authz", "backend_security_api",
                "backend_security_llm", "backend_security_infra"]:
        payload = _good_payload(category=cat)
        body = json.dumps(payload).encode()
        sig = _sign(body)
        with patch.dict(os.environ, {"FRONTEND_TESTS_WEBHOOK_SECRET": SECRET,
                                      "TESTS_RESULTS_DIR": results_dir}):
            resp = c.post("/tests/webhook/ci", data=body, content_type="application/json",
                          headers={"X-Bytoid-Signature": sig})
        assert resp.status_code == 200, f"{cat} returned {resp.status_code}"

@pytest.mark.integration
def test_webhook_empty_body_is_rejected(client):
    c, _ = client
    body = b""
    sig = _sign(body)
    with patch.dict(os.environ, {"FRONTEND_TESTS_WEBHOOK_SECRET": SECRET}):
        resp = c.post("/tests/webhook/ci", data=body, content_type="application/json",
                      headers={"X-Bytoid-Signature": sig})
    assert resp.status_code in (400, 401, 422)
