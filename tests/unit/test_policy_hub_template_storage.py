"""Unit tests for policy_hub/template_storage.py — S3 fully mocked."""

import io
import sys
from unittest.mock import MagicMock, patch

import pytest

class _ClientError(Exception):
    def __init__(self, code="NoSuchKey"):
        self.response = {"Error": {"Code": code}}

# Stub heavy deps
_botocore = MagicMock(name="botocore_stub")
_botocore.exceptions = MagicMock(ClientError=_ClientError)
sys.modules.setdefault("botocore", _botocore)
sys.modules.setdefault("botocore.exceptions", _botocore.exceptions)
for _mod in ("pymysql", "pymysql.cursors", "db", "db.rds_db", "db.db_checkers",
             "boto3", "dotenv", "dbutils", "dbutils.pooled_db"):
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

# Force-clear stale stubs so template_storage gets a proper s3 module + real yaml
import types as _types
for _to_pop in ("policy_hub.template_storage", "utils.s3_utils", "yaml"):
    sys.modules.pop(_to_pop, None)

_s3_mod = _types.ModuleType("utils.s3_utils")
_s3_mod.S3_BUCKET = "test-bucket"
_s3_mod.s3bucket = MagicMock()
_s3_mod.read_json_from_s3 = MagicMock(return_value={})
_s3_mod.save_json_to_s3 = MagicMock(return_value=True)
_s3_mod.save_app_runbase_S3 = MagicMock(return_value=True)
sys.modules["utils.s3_utils"] = _s3_mod

sys.modules.setdefault("utils.base_logger",
                      MagicMock(get_logger=MagicMock(return_value=MagicMock())))

import policy_hub.template_storage as ts  # noqa: E402

# Override ClientError to a real class so isinstance checks work
ts.ClientError = _ClientError


DOC_TYPES = ["policy", "procedure", "standard"]


# ── template_s3_key ──────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("user_id,doc_type", [
    ("u1", "policy"), ("admin-123", "procedure"), ("uuid-abc", "standard"),
])
def test_template_s3_key_format(user_id, doc_type):
    key = ts.template_s3_key(user_id, doc_type)
    assert key == f"{user_id}/templates/{doc_type}.yaml"

@pytest.mark.unit
@pytest.mark.parametrize("doc_type", DOC_TYPES)
def test_template_s3_key_includes_doc_type(doc_type):
    key = ts.template_s3_key("u", doc_type)
    assert doc_type in key
    assert key.endswith(".yaml")


# ── VALID_DOC_TYPES / VALID_KINDS ────────────────────────────────────────────

@pytest.mark.unit
def test_valid_doc_types_tuple():
    assert isinstance(ts.VALID_DOC_TYPES, tuple)
    assert set(ts.VALID_DOC_TYPES) == {"policy", "procedure", "standard"}

@pytest.mark.unit
def test_valid_kinds_tuple():
    assert isinstance(ts.VALID_KINDS, tuple)
    assert "text" in ts.VALID_KINDS
    assert "statements" in ts.VALID_KINDS
    assert "header_table" in ts.VALID_KINDS


# ── load_custom_template ─────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("bad_doc_type", ["unknown", "", "POLICY", "policies"])
def test_load_invalid_doc_type_returns_none(bad_doc_type):
    assert ts.load_custom_template("u1", bad_doc_type) is None

@pytest.mark.unit
def test_load_no_such_key_returns_none():
    s3 = MagicMock()
    s3.get_object.side_effect = _ClientError("NoSuchKey")
    with patch("policy_hub.template_storage.s3bucket", return_value=s3):
        assert ts.load_custom_template("u", "policy") is None

@pytest.mark.unit
def test_load_404_returns_none():
    s3 = MagicMock()
    s3.get_object.side_effect = _ClientError("404")
    with patch("policy_hub.template_storage.s3bucket", return_value=s3):
        assert ts.load_custom_template("u", "policy") is None

@pytest.mark.unit
def test_load_other_client_error_returns_none():
    s3 = MagicMock()
    s3.get_object.side_effect = _ClientError("AccessDenied")
    with patch("policy_hub.template_storage.s3bucket", return_value=s3):
        assert ts.load_custom_template("u", "policy") is None

@pytest.mark.unit
def test_load_generic_exception_returns_none():
    s3 = MagicMock()
    s3.get_object.side_effect = RuntimeError("boom")
    with patch("policy_hub.template_storage.s3bucket", return_value=s3):
        assert ts.load_custom_template("u", "policy") is None

@pytest.mark.unit
def test_load_returns_parsed_sections():
    yaml_body = b"""
sections:
  - id: policy.purpose
    title: Purpose
    kind: text
    required: true
"""
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=lambda: yaml_body)}
    with patch("policy_hub.template_storage.s3bucket", return_value=s3):
        out = ts.load_custom_template("u", "policy")
    assert out is not None
    assert len(out) == 1
    assert out[0].id == "policy.purpose"

@pytest.mark.unit
def test_load_empty_yaml_returns_empty_list():
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=lambda: b"")}
    with patch("policy_hub.template_storage.s3bucket", return_value=s3):
        out = ts.load_custom_template("u", "policy")
    assert out == []

@pytest.mark.unit
def test_load_malformed_yaml_returns_none():
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=lambda: b"\xff\xfe broken")}
    with patch("policy_hub.template_storage.s3bucket", return_value=s3):
        out = ts.load_custom_template("u", "policy")
    assert out is None


# ── save_custom_template ─────────────────────────────────────────────────────

VALID_SECTION = {
    "id": "policy.purpose", "title": "Purpose",
    "kind": "text", "required": True, "prompt_help": "h",
}

@pytest.mark.unit
@pytest.mark.parametrize("bad_doc_type", ["unknown", "", "POLICY"])
def test_save_rejects_invalid_doc_type(bad_doc_type):
    with pytest.raises(ValueError, match="doc_type"):
        ts.save_custom_template("u", bad_doc_type, [VALID_SECTION])

@pytest.mark.unit
def test_save_rejects_empty_sections():
    with pytest.raises(ValueError, match="non-empty"):
        ts.save_custom_template("u", "policy", [])

@pytest.mark.unit
def test_save_rejects_non_list_sections():
    with pytest.raises(ValueError):
        ts.save_custom_template("u", "policy", {"not": "list"})  # type: ignore

@pytest.mark.unit
def test_save_rejects_non_dict_section_entry():
    with pytest.raises(ValueError, match="not an object"):
        ts.save_custom_template("u", "policy", ["not-a-dict"])  # type: ignore

@pytest.mark.unit
def test_save_rejects_missing_id():
    with pytest.raises(ValueError, match="missing id"):
        ts.save_custom_template("u", "policy", [{"title": "T", "kind": "text"}])

@pytest.mark.unit
def test_save_rejects_missing_title():
    with pytest.raises(ValueError, match="missing title"):
        ts.save_custom_template("u", "policy", [{"id": "x", "kind": "text"}])

@pytest.mark.unit
@pytest.mark.parametrize("kind", ["invalid", "TEXT", "list", ""])
def test_save_rejects_invalid_kind(kind):
    with pytest.raises(ValueError, match="kind"):
        ts.save_custom_template("u", "policy", [{"id": "x", "title": "T", "kind": kind}])

@pytest.mark.unit
def test_save_rejects_duplicate_ids():
    sections = [
        {"id": "x", "title": "A", "kind": "text"},
        {"id": "x", "title": "B", "kind": "text"},
    ]
    with pytest.raises(ValueError, match="duplicate"):
        ts.save_custom_template("u", "policy", sections)

@pytest.mark.unit
def test_save_uploads_to_correct_key():
    s3 = MagicMock()
    with patch("policy_hub.template_storage.s3bucket", return_value=s3):
        ts.save_custom_template("user-x", "policy", [VALID_SECTION])
    args = s3.upload_fileobj.call_args.args
    assert args[1] == "test-bucket"
    assert args[2] == "user-x/templates/policy.yaml"

@pytest.mark.unit
@pytest.mark.parametrize("kind", ["text", "statements", "steps", "header_table", "history"])
def test_save_accepts_all_valid_kinds(kind):
    s3 = MagicMock()
    with patch("policy_hub.template_storage.s3bucket", return_value=s3):
        ts.save_custom_template("u", "policy", [{"id": "x", "title": "T", "kind": kind}])

@pytest.mark.unit
def test_save_strips_whitespace():
    s3 = MagicMock()
    with patch("policy_hub.template_storage.s3bucket", return_value=s3):
        # Should not raise — id and title get .strip()'d
        ts.save_custom_template("u", "policy", [{"id": "  x  ", "title": "  T  ", "kind": "text"}])


# ── delete_custom_template ───────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("bad_doc_type", ["unknown", "", "POLICY"])
def test_delete_invalid_doc_type_raises(bad_doc_type):
    with pytest.raises(ValueError):
        ts.delete_custom_template("u", bad_doc_type)

@pytest.mark.unit
@pytest.mark.parametrize("doc_type", DOC_TYPES)
def test_delete_calls_s3_delete(doc_type):
    s3 = MagicMock()
    with patch("policy_hub.template_storage.s3bucket", return_value=s3):
        ts.delete_custom_template("u", doc_type)
    s3.delete_object.assert_called_once()

@pytest.mark.unit
def test_delete_swallows_s3_exception():
    s3 = MagicMock()
    s3.delete_object.side_effect = RuntimeError("S3 down")
    with patch("policy_hub.template_storage.s3bucket", return_value=s3):
        # Should not raise
        ts.delete_custom_template("u", "policy")


# ── get_custom_template_metadata ─────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("bad_doc_type", ["unknown", "", "POLICY"])
def test_metadata_invalid_doc_type_returns_none(bad_doc_type):
    assert ts.get_custom_template_metadata("u", bad_doc_type) is None

@pytest.mark.unit
def test_metadata_no_such_key_returns_none():
    s3 = MagicMock()
    s3.get_object.side_effect = _ClientError("NoSuchKey")
    with patch("policy_hub.template_storage.s3bucket", return_value=s3):
        assert ts.get_custom_template_metadata("u", "policy") is None

@pytest.mark.unit
def test_metadata_returns_updated_at():
    yaml_body = b"updated_at: '2026-01-01T00:00:00+00:00'\nsections: []"
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=lambda: yaml_body)}
    with patch("policy_hub.template_storage.s3bucket", return_value=s3):
        out = ts.get_custom_template_metadata("u", "policy")
    assert out == {"updated_at": "2026-01-01T00:00:00+00:00"}

@pytest.mark.unit
def test_metadata_exception_returns_none():
    s3 = MagicMock()
    s3.get_object.side_effect = RuntimeError("boom")
    with patch("policy_hub.template_storage.s3bucket", return_value=s3):
        assert ts.get_custom_template_metadata("u", "policy") is None
