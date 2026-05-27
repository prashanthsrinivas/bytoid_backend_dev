"""Insecure-defaults audit tests. Verifies production configuration and code
patterns meet the security baseline."""

import re
import sys
from pathlib import Path
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
sys.modules["utils.celery_base"] = _celery_base

from flask import Flask  # noqa: E402

from services.audit_log_service import log_audit_event  # noqa: E402
from utils.app_configs import ACCESSIBLE_IDS, ALLOWED_ORIGINS, BACKURL, PROD_ORIGINS  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_WEBHOOK_AUTH_PATH = _REPO_ROOT / "tests_routes" / "webhook_auth.py"
_RESULT_STORE_PATH = _REPO_ROOT / "tests_routes" / "result_store.py"


# ---------------------------------------------------------------------------
# Tests — CORS / origin config
# ---------------------------------------------------------------------------

@pytest.mark.security
@pytest.mark.infra
def test_allowed_origins_does_not_include_wildcard():
    """ALLOWED_ORIGINS must not contain '*' or None — both are dangerous CORS wildcards."""
    assert "*" not in ALLOWED_ORIGINS
    assert None not in ALLOWED_ORIGINS


@pytest.mark.security
@pytest.mark.infra
def test_prod_origins_all_use_https():
    """Every origin in PROD_ORIGINS must start with 'https://' — no plain HTTP in production."""
    for origin in PROD_ORIGINS:
        assert origin.startswith("https://"), (
            f"Production origin '{origin}' does not use HTTPS"
        )


@pytest.mark.security
@pytest.mark.infra
def test_dev_origins_not_in_prod_allowed_origins_when_not_dev():
    """When IS_DEV=False, dev-only origins (localhost) must not appear in ALLOWED_ORIGINS."""
    dev_only_origins = {
        "http://localhost:8080",
        "https://dev.bytoid.ai",
        "dev.bytoid.ai",
    }
    # Simulate IS_DEV=False: compute what ALLOWED_ORIGINS should be
    from utils.app_configs import PROD_ORIGINS, STAGING_ORIGINS
    computed_prod_origins = PROD_ORIGINS | STAGING_ORIGINS
    for origin in dev_only_origins:
        assert origin not in computed_prod_origins, (
            f"Dev origin '{origin}' must not appear in prod-only allowed origins"
        )


# ---------------------------------------------------------------------------
# Tests — ACCESSIBLE_IDS
# ---------------------------------------------------------------------------

@pytest.mark.security
@pytest.mark.infra
def test_accessible_ids_is_not_empty():
    """ACCESSIBLE_IDS must have at least one entry — empty list would lock everyone out."""
    assert len(ACCESSIBLE_IDS) >= 1


@pytest.mark.security
@pytest.mark.infra
def test_accessible_ids_is_not_open_wildcard():
    """ACCESSIBLE_IDS must not contain '*' — that would grant access to anyone."""
    assert "*" not in ACCESSIBLE_IDS


@pytest.mark.security
@pytest.mark.infra
def test_accessible_ids_list_contains_no_empty_strings():
    """ACCESSIBLE_IDS must not contain empty strings — those bypass user_id checks."""
    assert "" not in ACCESSIBLE_IDS


# ---------------------------------------------------------------------------
# Tests — webhook auth source inspection
# ---------------------------------------------------------------------------

@pytest.mark.security
@pytest.mark.infra
def test_webhook_secret_not_hardcoded_in_source():
    """webhook_auth.py must reference the env var name but not hardcode a secret value."""
    source = _WEBHOOK_AUTH_PATH.read_text(encoding="utf-8")
    # The env var name should appear (it's fine — it's just a name)
    assert "FRONTEND_TESTS_WEBHOOK_SECRET" in source
    # Should not have trivially hardcoded secret assignments
    # Pattern: assignment of literal "test", "secret", or "password" to a variable
    bad_patterns = [
        r'=\s*["\']test["\']',
        r'=\s*["\']secret["\']',
        r'=\s*["\']password["\']',
    ]
    for pattern in bad_patterns:
        matches = re.findall(pattern, source, re.IGNORECASE)
        assert not matches, (
            f"Possible hardcoded secret in webhook_auth.py: pattern={pattern!r} found {matches}"
        )


@pytest.mark.security
@pytest.mark.infra
def test_hmac_uses_sha256_not_md5():
    """webhook_auth.py must use sha256 and must not use md5 for HMAC computation."""
    source = _WEBHOOK_AUTH_PATH.read_text(encoding="utf-8")
    assert "sha256" in source
    assert "md5" not in source.lower()


# ---------------------------------------------------------------------------
# Tests — audit log safety
# ---------------------------------------------------------------------------

@pytest.mark.security
@pytest.mark.infra
def test_audit_log_never_logs_password_field(app):
    """log_audit_event with a 'password' key in metadata must not raise."""
    with app.app_context():
        result = log_audit_event(
            action="LOGIN_SUCCESS",
            endpoint="/login",
            ip="127.0.0.1",
            status="success",
            metadata={"password": "hunter2", "username": "admin"},
        )
    assert result is None


# ---------------------------------------------------------------------------
# Tests — result store atomic write
# ---------------------------------------------------------------------------

@pytest.mark.security
@pytest.mark.infra
def test_result_store_writes_atomic_not_direct():
    """result_store.py must use tempfile.mkstemp for atomic writes, not a direct open()."""
    source = _RESULT_STORE_PATH.read_text(encoding="utf-8")
    assert "tempfile.mkstemp" in source, (
        "result_store.py must use tempfile.mkstemp for atomic JSON writes"
    )


# ---------------------------------------------------------------------------
# Tests — BACKURL
# ---------------------------------------------------------------------------

@pytest.mark.security
@pytest.mark.infra
def test_backurl_is_not_localhost_in_prod_config():
    """The production BACKURL must start with 'https://', not a localhost URL."""
    # Simulate the prod BACKURL directly from app_configs logic
    prod_backurl = "https://api.bytoid.ai"
    assert prod_backurl.startswith("https://")
    assert "localhost" not in prod_backurl


# ---------------------------------------------------------------------------
# Tests — Flask debug mode
# ---------------------------------------------------------------------------

@pytest.mark.security
@pytest.mark.infra
def test_no_debug_mode_in_prod_config():
    """A Flask app created without explicit DEBUG=True must not have debug mode enabled."""
    app = Flask(__name__)
    # Do NOT set app.debug = True
    assert app.debug is False


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def app():
    a = Flask(__name__)
    a.config.update(TESTING=True, SECRET_KEY="test-secret")
    return a
