"""S3 upload/download roundtrip tests using moto (mocked AWS).

Tests the underlying S3 semantics that utils/s3_utils.py wraps — upload,
overwrite, missing-key error, content-type preservation, structured key paths,
large payloads, and prefix-based listing.

moto is an optional dependency; tests are skipped automatically if it is not
installed so the suite stays green in environments that only have boto3.
"""

import json
import os
import pytest

# moto may not be installed in all environments; skip gracefully.
moto = pytest.importorskip("moto", reason="moto not installed")
mock_aws = moto.mock_aws

import boto3
from botocore.exceptions import ClientError

BUCKET = "test-bytoid-bucket"
REGION = "ca-central-1"


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def s3(tmp_path):
    """Create a fresh moto-mocked S3 bucket for each test."""
    with mock_aws():
        conn = boto3.client("s3", region_name=REGION)
        conn.create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )
        yield conn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_upload_and_download_json(s3):
    """Upload a JSON dict as bytes, download and deserialize, verify content matches."""
    original = {"user_id": "abc123", "action": "LOGIN_SUCCESS", "value": 42}
    body = json.dumps(original).encode("utf-8")
    key = "abc123/audit/2026-05-26.json"

    s3.put_object(Bucket=BUCKET, Key=key, Body=body, ContentType="application/json")
    response = s3.get_object(Bucket=BUCKET, Key=key)
    downloaded = json.loads(response["Body"].read())

    assert downloaded == original


@pytest.mark.integration
def test_upload_overwrites_existing_key(s3):
    """Uploading to the same key twice: only the second content survives."""
    key = "user1/audit/2026-05-26.json"

    s3.put_object(Bucket=BUCKET, Key=key, Body=b'[{"v": 1}]', ContentType="application/json")
    s3.put_object(Bucket=BUCKET, Key=key, Body=b'[{"v": 2}]', ContentType="application/json")

    response = s3.get_object(Bucket=BUCKET, Key=key)
    content = json.loads(response["Body"].read())
    assert content == [{"v": 2}]


@pytest.mark.integration
def test_download_missing_key_raises(s3):
    """get_object on a nonexistent key raises a ClientError (NoSuchKey)."""
    with pytest.raises(ClientError) as exc_info:
        s3.get_object(Bucket=BUCKET, Key="nonexistent/key.json")
    error_code = exc_info.value.response["Error"]["Code"]
    assert error_code == "NoSuchKey"


@pytest.mark.integration
def test_upload_preserves_content_type(s3):
    """ContentType set on upload is returned by HeadObject."""
    key = "user1/data/file.json"
    s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=b"{}",
        ContentType="application/json",
    )
    meta = s3.head_object(Bucket=BUCKET, Key=key)
    assert meta["ContentType"] == "application/json"


@pytest.mark.integration
def test_audit_log_key_structure(s3):
    """Audit log key pattern '{user_id}/audit/{date}.json' round-trips correctly."""
    user_id = "user123"
    date = "2026-05-26"
    key = f"{user_id}/audit/{date}.json"
    record = [{"action": "LOGIN_SUCCESS", "ts": "2026-05-26T10:00:00Z"}]
    body = json.dumps(record).encode("utf-8")

    s3.put_object(Bucket=BUCKET, Key=key, Body=body, ContentType="application/json")
    response = s3.get_object(Bucket=BUCKET, Key=key)
    downloaded = json.loads(response["Body"].read())

    assert downloaded == record
    # Verify the key structure is parseable as expected
    parts = key.split("/")
    assert parts[0] == user_id
    assert parts[1] == "audit"
    assert parts[2].endswith(".json")


@pytest.mark.integration
def test_large_json_payload(s3):
    """Upload a ~50 KB JSON payload and verify exact round-trip."""
    key = "user1/audit/2026-05-26.json"
    # Build a payload with 500 entries (~100 bytes each = ~50 KB)
    records = [
        {"index": i, "action": "TRACKER_ENTRY_ADDED", "user_id": f"user{i:05d}"}
        for i in range(500)
    ]
    body = json.dumps(records).encode("utf-8")
    assert len(body) > 40_000  # sanity: actually large

    s3.put_object(Bucket=BUCKET, Key=key, Body=body, ContentType="application/json")
    response = s3.get_object(Bucket=BUCKET, Key=key)
    downloaded = json.loads(response["Body"].read())

    assert len(downloaded) == 500
    assert downloaded[0]["index"] == 0
    assert downloaded[499]["index"] == 499


@pytest.mark.integration
def test_list_objects_under_prefix(s3):
    """Three objects under the same prefix are all returned by list_objects_v2."""
    prefix = "user123/audit/"
    keys = [
        "user123/audit/2026-05-24.json",
        "user123/audit/2026-05-25.json",
        "user123/audit/2026-05-26.json",
    ]
    for k in keys:
        s3.put_object(Bucket=BUCKET, Key=k, Body=b"[]", ContentType="application/json")

    response = s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix)
    listed_keys = [obj["Key"] for obj in response.get("Contents", [])]

    assert len(listed_keys) == 3
    for k in keys:
        assert k in listed_keys
