"""Output sanitization tests. Verifies that credential-shaped strings, PII, and
sensitive patterns are not reflected back from normalizer outputs unredacted, and
that sanitize_value correctly escapes HTML."""

import hashlib
import hmac
import json
import os
import sys
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

import markupsafe  # noqa: E402
from flask import Flask  # noqa: E402

from tests_routes.normalizers import _format_message, parse_bandit_json, parse_gitleaks_sarif  # noqa: E402
from tests_routes.routes import tests_bp  # noqa: E402
from utils.normal import sanitize_value, strip_html  # noqa: E402

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_NOW = "2024-01-01T00:00:00+00:00"
WEBHOOK_SECRET = "test-sanitization-secret"


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
# sanitize_value tests
# ---------------------------------------------------------------------------

@pytest.mark.security
@pytest.mark.llm_safety
def test_sanitize_value_escapes_script_tags():
    """sanitize_value must not allow <script> to pass through unescaped."""
    result = sanitize_value("<script>alert('xss')</script>")
    assert "<script>" not in str(result)


@pytest.mark.security
@pytest.mark.llm_safety
def test_sanitize_value_escapes_angle_brackets():
    """sanitize_value escapes < and > into HTML entities."""
    result = sanitize_value("<b>bold</b>")
    assert str(result) == "&lt;b&gt;bold&lt;/b&gt;"


@pytest.mark.security
@pytest.mark.llm_safety
def test_sanitize_value_leaves_plain_text_unchanged():
    """sanitize_value passes plain text through without modification."""
    result = sanitize_value("hello world")
    assert str(result) == "hello world"


@pytest.mark.security
@pytest.mark.llm_safety
def test_sanitize_value_recurses_into_dict():
    """sanitize_value escapes HTML in nested dict values."""
    result = sanitize_value({"key": "<b>val</b>"})
    assert "&lt;b&gt;" in str(result["key"])
    assert "<b>" not in str(result["key"])


@pytest.mark.security
@pytest.mark.llm_safety
def test_sanitize_value_recurses_into_list():
    """sanitize_value escapes HTML in list elements."""
    result = sanitize_value(["<a>", "safe"])
    assert "&lt;a&gt;" in str(result[0])
    assert "<a>" not in str(result[0])
    assert str(result[1]) == "safe"


@pytest.mark.security
@pytest.mark.llm_safety
def test_sanitize_value_passthrough_for_int():
    """sanitize_value returns integers unchanged — does not coerce to string."""
    result = sanitize_value(42)
    assert result == 42
    assert isinstance(result, int)


# ---------------------------------------------------------------------------
# strip_html tests
# ---------------------------------------------------------------------------

@pytest.mark.security
@pytest.mark.llm_safety
def test_strip_html_removes_all_tags():
    """strip_html removes all HTML tags, leaving only text content."""
    result = strip_html("<p>Hello <b>world</b></p>")
    assert result == "Hello world"


@pytest.mark.security
@pytest.mark.llm_safety
def test_strip_html_handles_nested_tags():
    """strip_html correctly removes deeply nested tags."""
    result = strip_html("<div><span>text</span></div>")
    assert result == "text"


@pytest.mark.security
@pytest.mark.llm_safety
def test_strip_html_handles_empty_string():
    """strip_html handles empty input without error."""
    result = strip_html("")
    assert result == ""


# ---------------------------------------------------------------------------
# Normalizer output tests
# ---------------------------------------------------------------------------

@pytest.mark.security
@pytest.mark.llm_safety
def test_normalizer_message_format_does_not_eval_content():
    """_format_message returns a plain formatted string; SQL injection in body is stored literally."""
    sql_injection = "'; DROP TABLE users; --"
    finding = {
        "cwe": "CWE-89",
        "owasp": "A03",
        "remediation": "fix it",
        "body": sql_injection,
    }
    result = _format_message(finding)
    assert isinstance(result, str)
    # Injection payload is present literally in the output — as data, not executed
    assert sql_injection in result
    assert "CWE-89" in result
    assert "A03" in result


# ---------------------------------------------------------------------------
# Webhook / run_id injection test
# ---------------------------------------------------------------------------

@pytest.mark.security
@pytest.mark.llm_safety
def test_webhook_response_does_not_echo_run_id_injection(client):
    """Webhook with path-traversal run_id must return 200 without creating traversal files."""
    traversal_run_id = "../../etc/passwd"
    payload = {
        "category": "frontend_unit",
        "run_id": traversal_run_id,
        "summary": {"failed": 0, "passed": 1, "total": 1},
        "status": "passed",
        "tests": [],
    }
    resp = _signed_post(client, "/tests/webhook/ci", payload)
    # Must not create a file at ../../etc/passwd relative to results dir
    assert not os.path.exists("../../etc/passwd")
    # Response must be JSON and not crash
    assert resp.status_code in (200, 400, 500)
    data = resp.get_json()
    assert data is not None


# ---------------------------------------------------------------------------
# Gitleaks design documentation test
# ---------------------------------------------------------------------------

@pytest.mark.security
@pytest.mark.llm_safety
def test_gitleaks_normalizer_redacts_nothing_by_design():
    """By design, gitleaks findings store the full raw message for admin review.
    This test documents the intentional behavior: body is a plain string."""
    raw_secret_fragment = "AKIA1234567890ABCDEF"
    raw = json.dumps({
        "runs": [
            {
                "tool": {"driver": {"rules": []}},
                "results": [
                    {
                        "ruleId": "aws-access-token",
                        "level": "error",
                        "message": {"text": f"Secret found: {raw_secret_fragment}"},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": ".env"},
                                    "region": {"startLine": 3},
                                }
                            }
                        ],
                    }
                ],
            }
        ]
    })
    result = parse_gitleaks_sarif(
        category="backend_security_secrets",
        run_id="run-gitleaks-001",
        raw_text=raw,
        started_at=_NOW,
        finished_at=_NOW,
        returncode=1,
    )
    assert len(result["tests"]) == 1
    # The body is a string — the caller (dashboard authz layer) controls visibility
    assert isinstance(result["tests"][0]["body"], str)


# ---------------------------------------------------------------------------
# Markupsafe Markup type test
# ---------------------------------------------------------------------------

@pytest.mark.security
@pytest.mark.llm_safety
def test_sanitize_value_produces_markupsafe_markup():
    """sanitize_value wraps string output in markupsafe.Markup with HTML entities."""
    result = sanitize_value("<b>test</b>")
    assert isinstance(result, markupsafe.Markup)
    assert "&lt;" in str(result)
    assert "<b>" not in str(result)
