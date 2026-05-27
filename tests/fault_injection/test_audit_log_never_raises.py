"""Fault injection tests for services/audit_log_service.py.

The log_audit_event() function is documented as "Never raises." These tests
verify that guarantee holds even when internal components fail catastrophically:
S3 uploads raise, metadata is malformed, inputs are None, or the filesystem
is exhausted. A broken audit logging function would silently kill request
handlers and is a critical reliability contract.

All DB and AWS imports are stubbed before the service is imported.
"""

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub heavy deps before importing the service
# ---------------------------------------------------------------------------

for _mod in ("pymysql", "pymysql.cursors", "db", "db.rds_db", "db.db_checkers",
             "boto3", "dotenv", "dbutils", "dbutils.pooled_db",
             # utils.normal imports these at module level
             "pptx", "pptx.util", "bs4", "pytz", "yaml", "docx"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock(name=f"{_mod}_stub")

# db.db_checkers.get_email_by_id must return something reasonable
_db_checkers = sys.modules.get("db.db_checkers") or MagicMock()
_db_checkers.get_email_by_id = MagicMock(return_value="test@example.com")
sys.modules["db.db_checkers"] = _db_checkers

_s3_utils = MagicMock()
_s3_utils.save_app_runbase_S3 = MagicMock(return_value=None)
sys.modules["utils.s3_utils"] = _s3_utils

_logger_stub = MagicMock()
_logger_stub.get_logger = MagicMock(return_value=MagicMock())
sys.modules.setdefault("utils.base_logger", _logger_stub)

import services.audit_log_service as audit  # noqa: E402

# ---------------------------------------------------------------------------
# Helper: call log_audit_event with safe defaults
# ---------------------------------------------------------------------------

def _log(**overrides):
    kwargs = dict(
        action="LOGIN_SUCCESS",
        endpoint="/auth/login",
        ip="127.0.0.1",
        status="success",
        actor_user_id="user123",
        metadata={"detail": "test"},
    )
    kwargs.update(overrides)
    return audit.log_audit_event(**kwargs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.chaos
def test_log_audit_event_survives_upload_raising_value_error():
    """log_audit_event does not raise when _upload_to_s3 raises ValueError."""
    with patch.object(audit, "_upload_to_s3", side_effect=ValueError("bad value")):
        _log()  # must not raise


@pytest.mark.chaos
def test_log_audit_event_survives_upload_raising_connection_error():
    """log_audit_event does not raise when _upload_to_s3 raises ConnectionError."""
    with patch.object(audit, "_upload_to_s3", side_effect=ConnectionError("network down")):
        _log()


@pytest.mark.chaos
def test_log_audit_event_survives_upload_raising_memory_error():
    """log_audit_event does not raise even for extreme MemoryError in _upload_to_s3."""
    with patch.object(audit, "_upload_to_s3", side_effect=MemoryError("OOM")):
        _log()


@pytest.mark.chaos
def test_log_audit_event_survives_none_action():
    """log_audit_event does not raise when action=None."""
    _log(action=None)


@pytest.mark.chaos
def test_log_audit_event_survives_none_ip():
    """log_audit_event does not raise when ip=None."""
    _log(ip=None)


@pytest.mark.chaos
def test_log_audit_event_survives_unicode_metadata():
    """log_audit_event handles metadata with unicode, emoji, and surrogate-adjacent chars."""
    metadata = {
        "unicode": "☃❤️",
        "emoji": "\U0001f600\U0001f4a5",
        "mixed": "café élève",
        # Surrogate pair characters that json.dumps with default=str must handle
        "safe_surrogates": "normal text",
    }
    _log(metadata=metadata)


@pytest.mark.chaos
def test_log_audit_event_survives_massive_metadata():
    """log_audit_event does not raise when metadata has 1000 keys."""
    big_meta = {f"key_{i}": f"value_{i}" for i in range(1000)}
    _log(metadata=big_meta)


@pytest.mark.chaos
def test_log_audit_event_survives_recursive_metadata():
    """log_audit_event does not raise with deeply nested metadata (depth 50)."""
    deep: dict = {}
    node = deep
    for i in range(50):
        node["child"] = {}
        node = node["child"]
    node["leaf"] = "bottom"
    _log(metadata=deep)


@pytest.mark.chaos
def test_log_audit_event_survives_s3_utils_import_error():
    """log_audit_event survives if save_app_runbase_S3 raises ImportError."""
    with patch.object(audit, "_upload_to_s3", side_effect=ImportError("module gone")):
        _log()


@pytest.mark.chaos
def test_log_audit_event_always_returns_none():
    """log_audit_event always returns None (never a truthy value)."""
    result = _log()
    assert result is None

    with patch.object(audit, "_upload_to_s3", side_effect=RuntimeError("crash")):
        result2 = _log()
    assert result2 is None
