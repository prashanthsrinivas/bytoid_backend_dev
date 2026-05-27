"""Unit tests for policy_hub.backfill_doc_refs."""

import sys
import types
from unittest.mock import MagicMock

import pytest

for _mod in ("pymysql", "pymysql.cursors", "db", "db.rds_db"):
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

from policy_hub.backfill_doc_refs import (  # noqa: E402
    assign_doc_ref,
    needs_doc_ref,
)


@pytest.mark.unit
class TestNeedsDocRef:
    def test_missing_field(self):
        assert needs_doc_ref({"title": "X"}) is True

    def test_empty_string(self):
        assert needs_doc_ref({"doc_ref": ""}) is True

    def test_present(self):
        assert needs_doc_ref({"doc_ref": "ACC-0001"}) is False

    def test_none_item(self):
        assert needs_doc_ref(None) is True


@pytest.mark.unit
class TestAssignDocRef:
    def test_adds_doc_ref_and_preserves_keys(self):
        item = {"policy_id": "p1", "title": "Access Control Policy", "type": "policy", "content": "<h1>x</h1>"}
        calls = []

        def fake_mint(org, dtype, title):
            calls.append((org, dtype, title))
            return "ACC-0001"

        out = assign_doc_ref(item, "org1", mint_fn=fake_mint)
        assert out["doc_ref"] == "ACC-0001"
        # original keys all preserved
        for k, v in item.items():
            assert out[k] == v
        # minter received the right args
        assert calls == [("org1", "policy", "Access Control Policy")]

    def test_does_not_mutate_input(self):
        item = {"title": "X", "type": "standard"}
        _ = assign_doc_ref(item, "org1", mint_fn=lambda *a: "STD-0001")
        assert "doc_ref" not in item

    def test_defaults_type_to_policy(self):
        seen = {}

        def fake_mint(org, dtype, title):
            seen["dtype"] = dtype
            return "DOC-0001"

        assign_doc_ref({"title": "Mystery"}, "org1", mint_fn=fake_mint)
        assert seen["dtype"] == "policy"


def _install_routes_stub(monkeypatch, written):
    """Stub policy_hub.routes (_s3_key, _write_yaml_to_s3) and utils.s3_utils."""
    routes_stub = types.ModuleType("policy_hub.routes")
    routes_stub._s3_key = lambda uid, pid: f"{uid}/policies/{pid}.yaml"
    routes_stub._write_yaml_to_s3 = lambda key, data: written.append((key, data))
    monkeypatch.setitem(sys.modules, "policy_hub.routes", routes_stub)


@pytest.mark.unit
class TestBackfillUser:
    def test_mints_only_missing(self, monkeypatch):
        written = []
        _install_routes_stub(monkeypatch, written)

        s3_stub = types.ModuleType("utils.s3_utils")
        s3_stub.list_all_files = lambda folder=None: [
            {"Key": "u1/policies/a.yaml"},
            {"Key": "u1/policies/b.yaml"},
            {"Key": "u1/policies/jobs/state.yaml"},   # ignored
            {"Key": "u1/policies/raw/orig.yaml"},     # ignored
            {"Key": "u1/policies/c.txt"},             # ignored (not yaml)
        ]
        store = {
            "u1/policies/a.yaml": {"policy_id": "a", "title": "Access Control Policy", "type": "policy"},
            "u1/policies/b.yaml": {"policy_id": "b", "title": "Encryption Standard", "type": "standard", "doc_ref": "ENC-S0001"},
        }
        s3_stub.load_yaml_from_s3 = lambda key: store.get(key)
        monkeypatch.setitem(sys.modules, "utils.s3_utils", s3_stub)

        from policy_hub.backfill_doc_refs import backfill_user

        summary = backfill_user(
            "u1",
            apply=True,
            mint_fn=lambda org, dtype, title: "ACC-0001",
            org_resolver=lambda uid: "org1",
        )

        assert summary == {"scanned": 2, "minted": 1, "skipped": 1, "errors": 0}
        # only the doc lacking a ref was written
        assert len(written) == 1
        assert written[0][0] == "u1/policies/a.yaml"
        assert written[0][1]["doc_ref"] == "ACC-0001"

    def test_dry_run_writes_nothing(self, monkeypatch):
        written = []
        _install_routes_stub(monkeypatch, written)
        s3_stub = types.ModuleType("utils.s3_utils")
        s3_stub.list_all_files = lambda folder=None: [{"Key": "u1/policies/a.yaml"}]
        s3_stub.load_yaml_from_s3 = lambda key: {"policy_id": "a", "title": "Access Control", "type": "policy"}
        monkeypatch.setitem(sys.modules, "utils.s3_utils", s3_stub)

        from policy_hub.backfill_doc_refs import backfill_user

        summary = backfill_user(
            "u1",
            apply=False,
            mint_fn=lambda *a: "ACC-0001",
            org_resolver=lambda uid: "org1",
        )
        assert summary["minted"] == 1
        assert written == []

    def test_no_org_skips(self, monkeypatch):
        written = []
        _install_routes_stub(monkeypatch, written)
        s3_stub = types.ModuleType("utils.s3_utils")
        s3_stub.list_all_files = lambda folder=None: [{"Key": "u1/policies/a.yaml"}]
        s3_stub.load_yaml_from_s3 = lambda key: {"title": "X"}
        monkeypatch.setitem(sys.modules, "utils.s3_utils", s3_stub)

        from policy_hub.backfill_doc_refs import backfill_user

        summary = backfill_user("u1", apply=True, mint_fn=lambda *a: "X", org_resolver=lambda uid: None)
        assert summary == {"scanned": 0, "minted": 0, "skipped": 0, "errors": 0}
        assert written == []
