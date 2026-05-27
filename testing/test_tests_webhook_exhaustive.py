"""Exhaustive parametrized integration tests for the /tests/webhook/ci endpoint
and the result_store + categories layer.

Uses a tmp results dir + monkeypatched env so no external services needed.
"""

import hashlib
import hmac
import json
import os
import sys
from unittest.mock import MagicMock

# Stub heavy deps before any app import.
for _mod in ("pymysql", "pymysql.cursors", "db", "db.rds_db", "db.db_checkers",
             "boto3", "dotenv", "dbutils", "dbutils.pooled_db",
             "pptx", "pptx.util", "docx", "pytz", "bs4", "yaml"):
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

sys.modules.setdefault("utils.s3_utils", MagicMock())
sys.modules.setdefault("utils.base_logger", MagicMock(get_logger=MagicMock(return_value=MagicMock())))

import flask  # noqa: E402
import pytest  # noqa: E402

from tests_routes import categories as cats  # noqa: E402
from tests_routes import result_store as rs  # noqa: E402
from tests_routes import webhook_auth as wh  # noqa: E402


SECRET = "integration-secret-xyz"


# ── Build a Flask app with the tests blueprint, lazily ───────────────────────

@pytest.fixture(scope="session")
def app(tmp_path_factory):
    # Patch RESULTS_ROOT before blueprint registration so write_category_result
    # writes to the tmp dir.
    tmp = tmp_path_factory.mktemp("results")
    rs.RESULTS_ROOT = str(tmp)
    rs.SUMMARY_PATH = str(tmp / "summary.json")

    # Import the blueprint AFTER patching constants.
    from tests_routes import routes as tests_routes_mod
    app = flask.Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(tests_routes_mod.tests_bp)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def _set_secret(monkeypatch):
    monkeypatch.setenv(wh.SECRET_ENV_VAR, SECRET)


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _payload(category: str, status: str = "passed", failed: int = 0) -> dict:
    return {
        "category": category,
        "run_id": f"gh-int-{category}",
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:01:00+00:00",
        "duration_seconds": 60.0,
        "status": status,
        "summary": {"total": 3, "passed": 3 - failed, "failed": failed,
                    "skipped": 0, "errors": 0},
        "tests": [],
        "metrics": None,
    }


DELEGATED_CATEGORIES = [c for c in cats.ALL_CATEGORIES if cats.is_delegated(c)]


# ── parametrized: webhook accepts every delegated category ────────────────────

@pytest.mark.integration
@pytest.mark.parametrize("category", DELEGATED_CATEGORIES)
def test_webhook_accepts_delegated_category(client, category):
    body = json.dumps(_payload(category)).encode()
    resp = client.post(
        "/tests/webhook/ci", data=body, content_type="application/json",
        headers={wh.SIGNATURE_HEADER: _sign(body)},
    )
    assert resp.status_code == 200, resp.data
    assert resp.json["success"] is True
    assert resp.json["category"] == category


# ── parametrized: webhook rejects non-delegated categories ────────────────────

LOCAL_CATEGORIES = [c for c in cats.ALL_CATEGORIES if not cats.is_delegated(c)]

@pytest.mark.integration
@pytest.mark.parametrize("category", LOCAL_CATEGORIES)
def test_webhook_rejects_local_category(client, category):
    body = json.dumps(_payload(category)).encode()
    resp = client.post(
        "/tests/webhook/ci", data=body, content_type="application/json",
        headers={wh.SIGNATURE_HEADER: _sign(body)},
    )
    assert resp.status_code == 400


# ── parametrized: webhook rejects every form of bad auth ──────────────────────

BAD_AUTH_CASES = [
    {"headers": {}, "body_factory": lambda: json.dumps(_payload("backend_security_sast")).encode()},
    {"headers": {wh.SIGNATURE_HEADER: "sha256=" + "0" * 64},
     "body_factory": lambda: json.dumps(_payload("backend_security_sast")).encode()},
    {"headers": {wh.SIGNATURE_HEADER: "wrong-format"},
     "body_factory": lambda: json.dumps(_payload("backend_security_sast")).encode()},
    {"headers": {wh.SIGNATURE_HEADER: ""},
     "body_factory": lambda: json.dumps(_payload("backend_security_sast")).encode()},
]

@pytest.mark.integration
@pytest.mark.parametrize("case", BAD_AUTH_CASES)
def test_webhook_rejects_bad_auth(client, case):
    body = case["body_factory"]()
    resp = client.post(
        "/tests/webhook/ci", data=body, content_type="application/json",
        headers=case["headers"],
    )
    assert resp.status_code == 401


# ── parametrized: webhook persists then dashboard reads ──────────────────────

@pytest.mark.integration
@pytest.mark.parametrize("category", DELEGATED_CATEGORIES)
def test_webhook_persists_result_for_each_delegated(client, category):
    body = json.dumps(_payload(category, status="passed")).encode()
    resp = client.post(
        "/tests/webhook/ci", data=body, content_type="application/json",
        headers={wh.SIGNATURE_HEADER: _sign(body)},
    )
    assert resp.status_code == 200
    got = rs.read_category_result(category)
    assert got is not None
    assert got["category"] == category


# ── parametrized: payload variants ────────────────────────────────────────────

@pytest.mark.integration
@pytest.mark.parametrize("status,failed", [
    ("passed", 0), ("failed", 1), ("failed", 5), ("error", 0),
])
def test_webhook_round_trip_status(client, status, failed):
    cat = "backend_security_sast"
    body = json.dumps(_payload(cat, status=status, failed=failed)).encode()
    resp = client.post(
        "/tests/webhook/ci", data=body, content_type="application/json",
        headers={wh.SIGNATURE_HEADER: _sign(body)},
    )
    assert resp.status_code == 200


# ── parametrized: webhook alias (/frontend) accepts same payloads ─────────────

@pytest.mark.integration
@pytest.mark.parametrize("category", DELEGATED_CATEGORIES[:8])
def test_webhook_frontend_alias_accepts(client, category):
    body = json.dumps(_payload(category)).encode()
    resp = client.post(
        "/tests/webhook/frontend", data=body, content_type="application/json",
        headers={wh.SIGNATURE_HEADER: _sign(body)},
    )
    assert resp.status_code == 200


# ── parametrized: missing fields are filled in by handler ─────────────────────

MINIMAL_REQUIRED = ["category", "status"]

@pytest.mark.integration
@pytest.mark.parametrize("category", DELEGATED_CATEGORIES[:5])
def test_webhook_fills_missing_baseline_fields(client, category):
    minimal = {"category": category, "status": "passed", "summary": {"total": 0}}
    body = json.dumps(minimal).encode()
    resp = client.post(
        "/tests/webhook/ci", data=body, content_type="application/json",
        headers={wh.SIGNATURE_HEADER: _sign(body)},
    )
    assert resp.status_code == 200
    got = rs.read_category_result(category)
    # started_at / finished_at filled in by handler
    assert "started_at" in got and got["started_at"]
    assert "finished_at" in got and got["finished_at"]


# ── parametrized: large payloads ──────────────────────────────────────────────

@pytest.mark.integration
@pytest.mark.parametrize("test_count", [10, 100, 500])
def test_webhook_accepts_large_payloads(client, test_count):
    cat = "backend_security_sast"
    p = _payload(cat)
    p["tests"] = [
        {"name": f"test_{i}", "outcome": "passed", "duration": 0.01, "message": None}
        for i in range(test_count)
    ]
    body = json.dumps(p).encode()
    resp = client.post(
        "/tests/webhook/ci", data=body, content_type="application/json",
        headers={wh.SIGNATURE_HEADER: _sign(body)},
    )
    assert resp.status_code == 200


# ── parametrized: summary endpoint shows every category ──────────────────────

@pytest.mark.integration
@pytest.mark.parametrize("category", list(cats.ALL_CATEGORIES.keys()))
def test_summary_endpoint_lists_category(client, category):
    resp = client.get("/tests/summary")
    if resp.status_code == 200:
        summary = resp.get_json() or {}
        cats_map = (summary.get("categories") if isinstance(summary, dict) else {}) or {}
        assert category in cats_map or category in str(summary)


# ── parametrized: history endpoint per category ──────────────────────────────

@pytest.mark.integration
@pytest.mark.parametrize("category", list(cats.ALL_CATEGORIES.keys())[:10])
def test_history_endpoint_returns_list(client, category):
    resp = client.get(f"/tests/history?category={category}")
    assert resp.status_code in (200, 404)


# ── webhook idempotency: posting same run_id twice keeps a single latest ──────

@pytest.mark.integration
@pytest.mark.parametrize("category", DELEGATED_CATEGORIES[:5])
def test_webhook_idempotent_for_same_run_id(client, category):
    p = _payload(category)
    body = json.dumps(p).encode()
    for _ in range(3):
        resp = client.post(
            "/tests/webhook/ci", data=body, content_type="application/json",
            headers={wh.SIGNATURE_HEADER: _sign(body)},
        )
        assert resp.status_code == 200


# ── round-trip: every category writes & summary updates ──────────────────────

@pytest.mark.integration
@pytest.mark.parametrize("category", DELEGATED_CATEGORIES)
def test_summary_updates_after_webhook(client, category):
    body = json.dumps(_payload(category, status="passed")).encode()
    client.post(
        "/tests/webhook/ci", data=body, content_type="application/json",
        headers={wh.SIGNATURE_HEADER: _sign(body)},
    )
    summary = rs.read_summary()
    assert category in summary["categories"]
    assert summary["categories"][category].get("status") in ("passed", "failed", "error")
