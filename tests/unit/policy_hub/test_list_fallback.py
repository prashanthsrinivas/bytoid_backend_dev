"""Unit tests for policy_hub.doc_index.scan_policies_from_s3 (the fallback)."""

import sys
from unittest.mock import MagicMock

import pytest

for _mod in ("pymysql", "pymysql.cursors", "db", "db.rds_db"):
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

import policy_hub.doc_index as di  # noqa: E402


def _list_fn_factory(objects):
    """Build a fake list_all_files returning objects for the expected prefix."""
    def _list(folder=None):
        return objects
    return _list


def _read_fn_factory(by_key):
    def _read(user_id, key):
        return by_key.get(key)
    return _read


@pytest.mark.unit
class TestScanPoliciesFromS3:
    def test_reads_every_yaml_under_prefix(self):
        objects = [
            {"Key": "u1/policies/a.yaml"},
            {"Key": "u1/policies/b.yaml"},
        ]
        docs = {
            "u1/policies/a.yaml": {"policy_id": "a", "title": "A"},
            "u1/policies/b.yaml": {"policy_id": "b", "title": "B"},
        }
        upserts: list[tuple[str, dict]] = []
        result = di.scan_policies_from_s3(
            "u1",
            list_fn=_list_fn_factory(objects),
            read_fn=_read_fn_factory(docs),
            upsert_fn=lambda u, d: upserts.append((u, d)),
        )
        ids = {d["policy_id"] for d in result}
        assert ids == {"a", "b"}
        # Self-healing: every returned doc was lazily upserted.
        assert [u[0] for u in upserts] == ["u1", "u1"]
        assert {u[1]["policy_id"] for u in upserts} == {"a", "b"}

    def test_skips_non_yaml_and_job_state_files(self):
        objects = [
            {"Key": "u1/policies/a.yaml"},
            {"Key": "u1/policies/jobs/job-1.json"},   # /jobs/ skipped
            {"Key": "u1/policies/manifest.txt"},      # non-yaml skipped
        ]
        docs = {"u1/policies/a.yaml": {"policy_id": "a"}}
        upserts: list = []
        result = di.scan_policies_from_s3(
            "u1",
            list_fn=_list_fn_factory(objects),
            read_fn=_read_fn_factory(docs),
            upsert_fn=lambda u, d: upserts.append(d),
        )
        assert [d["policy_id"] for d in result] == ["a"]
        assert len(upserts) == 1

    def test_skips_unreadable_yaml_without_crashing(self):
        objects = [
            {"Key": "u1/policies/a.yaml"},
            {"Key": "u1/policies/corrupt.yaml"},
        ]
        docs = {"u1/policies/a.yaml": {"policy_id": "a"}}  # corrupt.yaml missing
        result = di.scan_policies_from_s3(
            "u1",
            list_fn=_list_fn_factory(objects),
            read_fn=_read_fn_factory(docs),
            upsert_fn=lambda *_a, **_k: None,
        )
        assert [d["policy_id"] for d in result] == ["a"]

    def test_read_exception_is_swallowed(self):
        objects = [{"Key": "u1/policies/bad.yaml"}, {"Key": "u1/policies/ok.yaml"}]
        def bad_read(user_id, key):
            if key.endswith("bad.yaml"):
                raise RuntimeError("decrypt boom")
            return {"policy_id": "ok"}
        result = di.scan_policies_from_s3(
            "u1",
            list_fn=_list_fn_factory(objects),
            read_fn=bad_read,
            upsert_fn=lambda *_a, **_k: None,
        )
        # The healthy doc still comes through.
        assert [d["policy_id"] for d in result] == ["ok"]

    def test_upsert_failure_does_not_break_listing(self):
        objects = [{"Key": "u1/policies/a.yaml"}]
        docs = {"u1/policies/a.yaml": {"policy_id": "a"}}
        def bad_upsert(*_a, **_k):
            raise RuntimeError("rds boom")
        result = di.scan_policies_from_s3(
            "u1",
            list_fn=_list_fn_factory(objects),
            read_fn=_read_fn_factory(docs),
            upsert_fn=bad_upsert,
        )
        # The fast/slow path drift is healed by the nightly reconcile, but
        # the list response must still surface the doc this request.
        assert [d["policy_id"] for d in result] == ["a"]

    def test_empty_prefix(self):
        result = di.scan_policies_from_s3(
            "u1",
            list_fn=lambda folder=None: [],
            read_fn=lambda *_a, **_k: None,
            upsert_fn=lambda *_a, **_k: None,
        )
        assert result == []
