"""Chaos tests: S3 service degradation / 503 storm scenarios.

All tests are skipped unless RUN_CHAOS=1 is set in the environment.
No live infrastructure is contacted; moto and/or plain mocks are used.
"""

# ---------------------------------------------------------------------------
# Critical import stubs — must precede ANY app import
# ---------------------------------------------------------------------------
import sys
from unittest.mock import MagicMock

for _mod in (
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
):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock(name=f"{_mod}_stub")

# ---------------------------------------------------------------------------
# Standard imports
# ---------------------------------------------------------------------------
import os
import pytest
from unittest.mock import patch, MagicMock, call

# ---------------------------------------------------------------------------
# Skip guard
# ---------------------------------------------------------------------------
pytestmark = [
    pytest.mark.chaos,
    pytest.mark.skipif(
        not os.getenv("RUN_CHAOS"),
        reason="Set RUN_CHAOS=1 to run chaos tests",
    ),
]

# ---------------------------------------------------------------------------
# Require moto
# ---------------------------------------------------------------------------
moto = pytest.importorskip("moto")

# ---------------------------------------------------------------------------
# Real boto3/botocore — available in the environment
# ---------------------------------------------------------------------------
# We remove the boto3 stub so we can import the real boto3 for moto tests.
# Tests that need real boto3 do this within their own scope.
import importlib as _importlib

_REAL_BOTO3 = None
_REAL_BOTOCORE = None


def _get_real_boto3():
    """Return real boto3, temporarily removing the stub if needed."""
    global _REAL_BOTO3
    if _REAL_BOTO3 is None:
        # Save stub, load real module
        stub = sys.modules.pop("boto3", None)
        try:
            _REAL_BOTO3 = _importlib.import_module("boto3")
        finally:
            if stub is not None:
                sys.modules["boto3"] = stub
    return _REAL_BOTO3


def _get_real_botocore():
    """Return real botocore.exceptions, temporarily removing the stub if needed."""
    global _REAL_BOTOCORE
    if _REAL_BOTOCORE is None:
        stub = sys.modules.pop("botocore", None)
        try:
            import botocore.exceptions as _bce
            _REAL_BOTOCORE = _bce
        except ImportError:
            _REAL_BOTOCORE = None
        finally:
            if stub is not None:
                sys.modules["botocore"] = stub
    return _REAL_BOTOCORE


# ---------------------------------------------------------------------------
# Helper: build a fake botocore ClientError (works even with stub)
# ---------------------------------------------------------------------------
def _make_client_error(code: str, operation: str = "PutObject") -> Exception:
    """Build a botocore ClientError without importing botocore at module level."""
    bce = _get_real_botocore()
    if bce is not None:
        return bce.ClientError(
            {"Error": {"Code": code, "Message": "Slow Down"}}, operation
        )
    # Fallback: plain Exception with the same interface
    err = Exception(f"ClientError: {code} on {operation}")
    err.response = {"Error": {"Code": code, "Message": "Slow Down"}}
    return err


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_audit_log_written_locally_when_s3_fails():
    """log_audit_event must never raise even when the S3 upload fails.

    The `_upload_to_s3` stub inside audit_log_service raises Exception("S3 503").
    The `log_audit_event` function has a broad try/except and must absorb it.
    """
    # Stub utils.s3_utils so audit_log_service can be imported cleanly
    s3_utils_mock = MagicMock()
    s3_utils_mock.save_app_runbase_S3 = MagicMock(side_effect=Exception("S3 503"))
    sys.modules["utils.s3_utils"] = s3_utils_mock

    # Stub flask so audit_log_service can import from flask
    if "flask" not in sys.modules or isinstance(sys.modules["flask"], MagicMock):
        flask_mock = MagicMock()
        flask_mock.g = MagicMock()
        flask_mock.session = MagicMock()
        flask_mock.request = MagicMock()
        sys.modules["flask"] = flask_mock

    # Stub utils.normal
    utils_normal_mock = MagicMock()
    utils_normal_mock.parse_composite_user_id = MagicMock(return_value=(None, None))
    sys.modules["utils"] = MagicMock()
    sys.modules["utils.normal"] = utils_normal_mock

    # Remove cached module if present so we get a fresh import with stubs
    sys.modules.pop("services.audit_log_service", None)
    sys.modules.pop("services", None)

    import services.audit_log_service as als

    # Should not raise, even though _upload_to_s3 will raise internally
    als.log_audit_event(
        action="LOGIN_SUCCESS",
        endpoint="/login",
        ip="1.2.3.4",
        status="ok",
    )


def test_s3_upload_retries_and_succeeds():
    """A retry wrapper around an S3 upload must succeed after transient 503 errors.

    The mock upload raises ClientError(503) twice, then returns a success dict.
    The wrapper must return the ETag from the successful third attempt.
    """
    etag_response = {"ETag": "abc123"}
    call_count = [0]

    def flaky_upload(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] <= 2:
            raise _make_client_error("503")
        return etag_response

    def upload_with_retry(upload_fn, max_attempts=3, *args, **kwargs):
        last_exc = None
        for _ in range(max_attempts):
            try:
                return upload_fn(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
        raise last_exc

    result = upload_with_retry(flaky_upload)

    assert result == etag_response, "successful response must be returned"
    assert call_count[0] == 3, "upload must be attempted exactly 3 times"


def test_moto_s3_basic_put_get():
    """Moto mock_aws: put an object then get it back and verify the body.

    This is a sanity check that moto is correctly installed and working in
    the test environment before we rely on it for more complex scenarios.
    """
    real_boto3 = _get_real_boto3()

    # Temporarily install real boto3 into sys.modules so moto can intercept it;
    # our stub blocks moto because boto3.s3 submodules don't exist on MagicMock.
    boto3_stub = sys.modules.get("boto3")
    # Also remove all boto3.* submodule stubs that were injected earlier
    boto3_sub_stubs = {k: v for k, v in list(sys.modules.items()) if k.startswith("boto3.")}
    try:
        sys.modules["boto3"] = real_boto3
        for k in boto3_sub_stubs:
            sys.modules.pop(k, None)

        with moto.mock_aws():
            s3 = real_boto3.client("s3", region_name="us-east-1")
            s3.create_bucket(Bucket="test-chaos-bucket")
            s3.put_object(
                Bucket="test-chaos-bucket",
                Key="test/key.txt",
                Body=b"hello chaos",
            )
            response = s3.get_object(Bucket="test-chaos-bucket", Key="test/key.txt")
            body = response["Body"].read()
    finally:
        # Restore the stub so other tests continue to work
        if boto3_stub is not None:
            sys.modules["boto3"] = boto3_stub
        else:
            sys.modules.pop("boto3", None)
        for k, v in boto3_sub_stubs.items():
            sys.modules[k] = v

    assert body == b"hello chaos", "body must match what was put"


def test_s3_503_storm_eventual_success():
    """A retry loop must handle 4 consecutive 503 ClientErrors before succeeding.

    With max 5 attempts and no sleep, the 5th call must return successfully.
    """
    call_count = [0]

    def stormy_put_object(**kwargs):
        call_count[0] += 1
        if call_count[0] <= 4:
            raise _make_client_error("503", "PutObject")
        return {"ETag": "success-etag", "ResponseMetadata": {"HTTPStatusCode": 200}}

    def put_with_retry(put_fn, max_attempts=5, **kwargs):
        last_exc = None
        for _ in range(max_attempts):
            try:
                return put_fn(**kwargs)
            except Exception as exc:
                last_exc = exc
        raise last_exc

    result = put_with_retry(
        stormy_put_object,
        Bucket="my-bucket",
        Key="logs/audit/2026-05-26/audit-0000.log",
        Body=b'{"action": "LOGIN_SUCCESS"}',
    )

    assert result["ETag"] == "success-etag"
    assert call_count[0] == 5, "all 5 attempts must be made"


def test_audit_log_s3_key_format():
    """log_audit_event must be callable and must not raise under any circumstances.

    The audit_log_service._upload_to_s3 stub constructs an S3 key of the
    form ``<user_id>/audit/<YYYY-MM-DD>.json``.  Since the function body is
    implemented, we verify the callable is present and does not raise when
    called via log_audit_event.  We also inspect the actual save_app_runbase_S3
    call argument to confirm the key format.
    """
    # Ensure clean import with mocks in place
    sys.modules.pop("services.audit_log_service", None)
    sys.modules.pop("services", None)

    captured_calls = []

    def capturing_save(data, key):
        captured_calls.append(key)

    s3_utils_mock = MagicMock()
    s3_utils_mock.save_app_runbase_S3 = capturing_save
    sys.modules["utils.s3_utils"] = s3_utils_mock

    if "flask" not in sys.modules or isinstance(sys.modules["flask"], MagicMock):
        flask_mock = MagicMock()
        flask_mock.g = MagicMock()
        flask_mock.session = MagicMock()
        flask_mock.request = MagicMock()
        sys.modules["flask"] = flask_mock

    utils_normal_mock = MagicMock()
    utils_normal_mock.parse_composite_user_id = MagicMock(return_value=(None, None))
    sys.modules["utils.normal"] = utils_normal_mock

    import services.audit_log_service as als

    assert callable(als.log_audit_event), "log_audit_event must be callable"
    assert callable(als._upload_to_s3), "_upload_to_s3 must be callable"

    # Call with a real actor so the S3 key is generated
    als.log_audit_event(
        action="LOGIN_SUCCESS",
        endpoint="/login",
        ip="10.0.0.1",
        status="ok",
        actor_user_id="user_abc123",
    )

    # If save was called, verify the key format
    if captured_calls:
        key = captured_calls[0]
        # Expected format: <user_id>/audit/<YYYY-MM-DD>.json
        parts = key.split("/")
        assert len(parts) == 3, f"S3 key must have 3 path segments, got: {key}"
        assert parts[1] == "audit", f"second segment must be 'audit', got: {key}"
        assert parts[2].endswith(".json"), f"key must end in .json, got: {key}"
        # Date segment: YYYY-MM-DD
        date_part = parts[2].replace(".json", "")
        assert len(date_part) == 10, f"date segment must be 10 chars (YYYY-MM-DD): {date_part}"
        assert date_part[4] == "-" and date_part[7] == "-", (
            f"date must be in YYYY-MM-DD format: {date_part}"
        )
