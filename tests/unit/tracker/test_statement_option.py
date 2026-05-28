"""Unit tests for ``add_statement_option`` / ``remove_statement_option``.

These helpers let users manually pin or unpin statements on a tracker's
policy_statements cell — the manual-override fallback for when the AI
matcher returns no rows.
"""

import sys
import types
from unittest.mock import MagicMock

import pytest

# Stub the heavy transitive imports that tab_tracker.helper drags in. We
# only exercise pure dict/cell logic here, so AWS, pptx, pytz, and friends
# don't need to be installed locally to run these tests.
for _mod in ("pymysql", "pymysql.cursors", "db", "db.rds_db",
             "boto3", "botocore", "botocore.exceptions"):
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

# utils.normal pulls in pptx/pytz at import time. Replace with a thin stub
# exposing the one symbol tab_tracker.helper actually consumes.
_utils_normal = types.ModuleType("utils.normal")
_utils_normal.ensure_dir = lambda *_a, **_k: None
sys.modules.setdefault("utils.normal", _utils_normal)

# utils.s3_utils and utils.key_rotation_manager hit boto3 at import time.
_utils_s3 = types.ModuleType("utils.s3_utils")
for _name in ("delete_file_from_s3", "read_json_from_s3",
              "upload_any_file", "save_any_s3", "load_yaml_from_s3"):
    setattr(_utils_s3, _name, lambda *a, **k: None)
sys.modules.setdefault("utils.s3_utils", _utils_s3)

_utils_krm = types.ModuleType("utils.key_rotation_manager")
class _StubKMS:
    def encrypt(self, *_a, **_k): return {"ciphertext": "", "iv": "", "encrypted_key": ""}
    def decrypt(self, *_a, **_k): return ""
_utils_krm.SecureKMSService = _StubKMS
sys.modules.setdefault("utils.key_rotation_manager", _utils_krm)

import tab_tracker.helper as helper  # noqa: E402


def _tracker_factory():
    """Build a minimal table tracker with one policy_statements column and two rows."""
    return {
        "type": "table",
        "tracker_abbrev": "RSK-0001",
        "schema": {
            "columns": [
                {"id": "col_pcidss", "source_column": "frameworks", "type": "framework_requirements", "name": "PCI DSS"},
                {
                    "id": "col_policy",
                    "source_column": "policies",
                    "type": "policy_statements",
                    "name": "Access Control Policy",
                    "policy_id": "policy-abc",
                    "policy_version": "1.0",
                    "doc_type": "policy",
                },
            ],
        },
        "rows": [
            {"row_id": "r1", "values": {"col_pcidss": [{"requirement": "...", "section": "..."}],
                                          "col_policy": []}},
            {"row_id": "r2", "values": {"col_policy": [
                {"statement_id": "s-existing", "statement_text": "kept",
                 "section_id": "policy.statements", "mapped_against_version": "1.0",
                 "status": "not_assessed"},
            ]}},
        ],
    }


@pytest.fixture
def fake_helper(monkeypatch):
    """Install fakes so the helper runs without S3 or RDS."""
    state = {"saved": None, "sync_calls": []}
    tracker = _tracker_factory()

    monkeypatch.setattr(helper, "_load_and_decrypt_tracker", lambda *_a, **_k: tracker)

    def _save(user_id, tracker_id, data):
        state["saved"] = data
    monkeypatch.setattr(helper, "save_tracker_file", _save)

    def _sync(tracker_id, abbrev, column, row_id, entries):
        state["sync_calls"].append({
            "tracker_id": tracker_id,
            "tracker_abbrev": abbrev,
            "column_id": column.get("id"),
            "policy_id": column.get("policy_id"),
            "row_id": row_id,
            "statement_ids": [e.get("statement_id") for e in (entries or [])],
        })
    monkeypatch.setattr(helper, "_sync_statement_refs_for_cell", _sync)

    return {"tracker": tracker, "state": state}


# ── add_statement_option ──────────────────────────────────────────────────────


@pytest.mark.unit
class TestAddStatementOption:
    def test_appends_new_statement_with_full_shape(self, fake_helper):
        result = helper.add_statement_option(
            "u1", "trk1", "r1", "col_policy",
            [{"statement_id": "s-new", "statement_text": "MFA required",
              "section_id": "policy.statements"}],
        )
        assert result["success"] is True
        assert result["added"] == 1
        assert result["skipped_duplicates"] == 0
        row = next(r for r in fake_helper["tracker"]["rows"] if r["row_id"] == "r1")
        cell = row["values"]["col_policy"]
        assert len(cell) == 1
        entry = cell[0]
        assert entry["statement_id"] == "s-new"
        assert entry["statement_text"] == "MFA required"
        assert entry["section_id"] == "policy.statements"
        assert entry["mapped_against_version"] == "1.0"
        assert entry["status"] == "not_assessed"
        assert row["last_updated_from"] == "manual"
        assert fake_helper["state"]["saved"] is not None

    def test_dedups_by_statement_id(self, fake_helper):
        # r2 already has s-existing; adding it again must be skipped.
        result = helper.add_statement_option(
            "u1", "trk1", "r2", "col_policy",
            [{"statement_id": "s-existing", "statement_text": "ignored"},
             {"statement_id": "s-fresh", "statement_text": "kept"}],
        )
        assert result["added"] == 1
        assert result["skipped_duplicates"] == 1
        row = next(r for r in fake_helper["tracker"]["rows"] if r["row_id"] == "r2")
        ids = [e["statement_id"] for e in row["values"]["col_policy"]]
        assert ids == ["s-existing", "s-fresh"]

    def test_rejects_non_policy_column(self, fake_helper):
        # col_pcidss is a frameworks column — the helper must refuse to
        # write policy-shaped entries into it.
        result = helper.add_statement_option(
            "u1", "trk1", "r1", "col_pcidss",
            [{"statement_id": "s-new", "statement_text": "..."}],
        )
        assert result["success"] is False
        assert "policy_statements" in result["error"]

    def test_rejects_unknown_column(self, fake_helper):
        result = helper.add_statement_option(
            "u1", "trk1", "r1", "col_nope",
            [{"statement_id": "s-new", "statement_text": "..."}],
        )
        assert result["success"] is False

    def test_rejects_unknown_row(self, fake_helper):
        result = helper.add_statement_option(
            "u1", "trk1", "r-missing", "col_policy",
            [{"statement_id": "s-new", "statement_text": "..."}],
        )
        assert result["success"] is False
        assert "Row" in result["error"]

    def test_rejects_empty_statements(self, fake_helper):
        assert helper.add_statement_option("u1", "trk1", "r1", "col_policy", [])["success"] is False
        assert helper.add_statement_option("u1", "trk1", "r1", "col_policy", None)["success"] is False

    def test_skips_entries_without_statement_id(self, fake_helper):
        result = helper.add_statement_option(
            "u1", "trk1", "r1", "col_policy",
            [{"statement_text": "no id"},
             {"statement_id": "s-good", "statement_text": "kept"}],
        )
        assert result["added"] == 1
        assert result["skipped_duplicates"] == 1

    def test_resyncs_reverse_lookup(self, fake_helper):
        helper.add_statement_option(
            "u1", "trk1", "r2", "col_policy",
            [{"statement_id": "s-fresh", "statement_text": "kept"}],
        )
        calls = fake_helper["state"]["sync_calls"]
        assert len(calls) == 1
        call = calls[0]
        assert call["tracker_id"] == "trk1"
        assert call["tracker_abbrev"] == "RSK-0001"
        assert call["column_id"] == "col_policy"
        assert call["policy_id"] == "policy-abc"
        assert call["row_id"] == "r2"
        # Full union: pre-existing + newly added.
        assert set(call["statement_ids"]) == {"s-existing", "s-fresh"}


# ── remove_statement_option ───────────────────────────────────────────────────


@pytest.mark.unit
class TestRemoveStatementOption:
    def test_removes_by_statement_id(self, fake_helper):
        result = helper.remove_statement_option(
            "u1", "trk1", "r2", "col_policy", ["s-existing"],
        )
        assert result["success"] is True
        assert result["removed"] == 1
        row = next(r for r in fake_helper["tracker"]["rows"] if r["row_id"] == "r2")
        assert row["values"]["col_policy"] == []
        assert row["last_updated_from"] == "manual"

    def test_unknown_ids_are_noop(self, fake_helper):
        result = helper.remove_statement_option(
            "u1", "trk1", "r2", "col_policy", ["does-not-exist"],
        )
        assert result["success"] is True
        assert result["removed"] == 0
        row = next(r for r in fake_helper["tracker"]["rows"] if r["row_id"] == "r2")
        assert len(row["values"]["col_policy"]) == 1  # original entry untouched

    def test_rejects_non_policy_column(self, fake_helper):
        result = helper.remove_statement_option(
            "u1", "trk1", "r1", "col_pcidss", ["s-existing"],
        )
        assert result["success"] is False

    def test_rejects_empty_list(self, fake_helper):
        assert helper.remove_statement_option("u1", "trk1", "r2", "col_policy", [])["success"] is False
        assert helper.remove_statement_option("u1", "trk1", "r2", "col_policy", None)["success"] is False

    def test_resyncs_with_remaining_entries(self, fake_helper):
        # Add two then remove one; sync should reflect the survivors.
        helper.add_statement_option(
            "u1", "trk1", "r2", "col_policy",
            [{"statement_id": "s-fresh", "statement_text": "kept"}],
        )
        helper.remove_statement_option(
            "u1", "trk1", "r2", "col_policy", ["s-existing"],
        )
        last_sync = fake_helper["state"]["sync_calls"][-1]
        assert last_sync["statement_ids"] == ["s-fresh"]
