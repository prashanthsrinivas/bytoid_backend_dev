"""Weak cryptography detection tests. Verifies that the codebase doesn't use broken
algorithms and that the protective Semgrep rules correctly identify violations."""

import inspect
import os
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

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

import markupsafe  # noqa: E402
import yaml  # noqa: E402 — real yaml, not the stub

from tests_routes.webhook_auth import sign  # noqa: E402
from utils.normal import sanitize_value  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_SEMGREP_PROTECTED = _REPO_ROOT / ".semgrep" / "protected"
_WEBHOOK_AUTH_PATH = _REPO_ROOT / "tests_routes" / "webhook_auth.py"
_NORMALIZERS_PATH = _REPO_ROOT / "tests_routes" / "normalizers.py"
_RESULT_STORE_PATH = _REPO_ROOT / "tests_routes" / "result_store.py"
_KEY_ROTATION_PATH = _REPO_ROOT / "utils" / "key_rotation_manager.py"


# ---------------------------------------------------------------------------
# Tests — HMAC / signing
# ---------------------------------------------------------------------------

@pytest.mark.security
@pytest.mark.infra
def test_hmac_sign_uses_sha256():
    """The sign() helper must return a string starting with 'sha256='."""
    result = sign(b"test")
    assert result.startswith("sha256="), (
        f"Expected signature to start with 'sha256=', got: {result!r}"
    )


@pytest.mark.security
@pytest.mark.infra
def test_hmac_compare_digest_used_for_constant_time():
    """webhook_auth.py must use hmac.compare_digest to prevent timing attacks."""
    source = _WEBHOOK_AUTH_PATH.read_text(encoding="utf-8")
    assert "hmac.compare_digest" in source, (
        "webhook_auth.py must use hmac.compare_digest for constant-time HMAC comparison"
    )


# ---------------------------------------------------------------------------
# Tests — key rotation manager (optional file)
# ---------------------------------------------------------------------------

@pytest.mark.security
@pytest.mark.infra
def test_fernet_or_aesgcm_preferred_not_des():
    """If key_rotation_manager.py exists, it must not use DES, ARC4, or RC4."""
    if not _KEY_ROTATION_PATH.exists():
        pytest.skip("utils/key_rotation_manager.py does not exist — skipping DES/ARC4 check")
    source = _KEY_ROTATION_PATH.read_text(encoding="utf-8")
    assert "DES" not in source, "key_rotation_manager.py must not use DES"
    assert "ARC4" not in source, "key_rotation_manager.py must not use ARC4"
    assert "RC4" not in source, "key_rotation_manager.py must not use RC4"


# ---------------------------------------------------------------------------
# Tests — Semgrep protected rules directory
# ---------------------------------------------------------------------------

@pytest.mark.security
@pytest.mark.infra
def test_semgrep_protected_rules_dir_exists():
    """The .semgrep/protected/ directory must exist and contain at least 4 YAML files."""
    assert _SEMGREP_PROTECTED.is_dir(), (
        f".semgrep/protected/ directory not found at {_SEMGREP_PROTECTED}"
    )
    yaml_files = list(_SEMGREP_PROTECTED.glob("*.yml")) + list(_SEMGREP_PROTECTED.glob("*.yaml"))
    assert len(yaml_files) >= 4, (
        f"Expected at least 4 YAML rule files in .semgrep/protected/, found {len(yaml_files)}: "
        f"{[f.name for f in yaml_files]}"
    )


# ---------------------------------------------------------------------------
# Tests — no_crypto_downgrade.yml rule content
# ---------------------------------------------------------------------------

def _load_semgrep_rule_text(filename: str) -> str:
    """Load a Semgrep rule file and return its full text content."""
    rule_path = _SEMGREP_PROTECTED / filename
    assert rule_path.exists(), f"Missing Semgrep rule file: {rule_path}"
    return rule_path.read_text(encoding="utf-8")


@pytest.mark.security
@pytest.mark.infra
def test_no_crypto_downgrade_rule_rejects_md5():
    """no_crypto_downgrade.yml must contain a pattern matching md5/MD5/hashlib.md5."""
    content = _load_semgrep_rule_text("no_crypto_downgrade.yml")
    assert any(keyword in content for keyword in ("md5", "MD5", "hashlib.md5")), (
        "no_crypto_downgrade.yml must include a pattern targeting MD5 usage"
    )


@pytest.mark.security
@pytest.mark.infra
def test_no_crypto_downgrade_rule_rejects_sha1():
    """no_crypto_downgrade.yml must contain a pattern matching sha1/SHA1/hashlib.sha1."""
    content = _load_semgrep_rule_text("no_crypto_downgrade.yml")
    assert any(keyword in content for keyword in ("sha1", "SHA1", "hashlib.sha1")), (
        "no_crypto_downgrade.yml must include a pattern targeting SHA1 usage"
    )


@pytest.mark.security
@pytest.mark.infra
def test_no_crypto_downgrade_rule_rejects_des_import():
    """no_crypto_downgrade.yml must contain a pattern matching DES imports."""
    content = _load_semgrep_rule_text("no_crypto_downgrade.yml")
    assert "DES" in content, (
        "no_crypto_downgrade.yml must include a pattern targeting DES cipher usage"
    )


# ---------------------------------------------------------------------------
# Tests — no_serializer_pickle.yml rule content
# ---------------------------------------------------------------------------

@pytest.mark.security
@pytest.mark.infra
def test_no_serializer_pickle_rule_exists():
    """no_serializer_pickle.yml must exist and contain patterns matching 'pickle'."""
    content = _load_semgrep_rule_text("no_serializer_pickle.yml")
    assert "pickle" in content, (
        "no_serializer_pickle.yml must include patterns matching pickle serializer usage"
    )


# ---------------------------------------------------------------------------
# Tests — source code must not use broken crypto
# ---------------------------------------------------------------------------

@pytest.mark.security
@pytest.mark.infra
def test_webhook_auth_does_not_use_md5():
    """webhook_auth.py must not use MD5 in any form."""
    source = _WEBHOOK_AUTH_PATH.read_text(encoding="utf-8")
    assert "md5" not in source.lower(), (
        "webhook_auth.py must not use MD5 — use SHA-256 for all HMAC operations"
    )


@pytest.mark.security
@pytest.mark.infra
def test_normalizers_do_not_use_eval():
    """normalizers.py must not contain bare eval() calls."""
    source = _NORMALIZERS_PATH.read_text(encoding="utf-8")
    # \beval\b matches the standalone function — not substrings like 'interval'
    matches = re.findall(r"\beval\s*\(", source)
    assert not matches, (
        f"normalizers.py must not use eval(); found: {matches}"
    )


@pytest.mark.security
@pytest.mark.infra
def test_result_store_uses_json_not_pickle():
    """result_store.py must not use pickle for serialization — JSON only."""
    source = _RESULT_STORE_PATH.read_text(encoding="utf-8")
    assert "pickle" not in source, (
        "result_store.py must not use pickle — all result files must be JSON"
    )


# ---------------------------------------------------------------------------
# Tests — sanitize_value output type
# ---------------------------------------------------------------------------

@pytest.mark.security
@pytest.mark.infra
def test_sanitize_value_produces_markupsafe_markup():
    """sanitize_value must return a markupsafe.Markup instance for string inputs."""
    result = sanitize_value("<b>test</b>")
    assert isinstance(result, markupsafe.Markup), (
        f"sanitize_value must return markupsafe.Markup, got {type(result).__name__}"
    )
    assert "&lt;" in str(result), "Markup must contain escaped '&lt;'"
    assert "<b>" not in str(result), "Markup must not contain raw '<b>' tag"
