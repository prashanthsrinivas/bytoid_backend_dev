"""Unit tests for policy_hub.reconcile_doc_index.reconcile_user."""

import sys
from unittest.mock import MagicMock

import pytest

for _mod in ("pymysql", "pymysql.cursors", "db", "db.rds_db"):
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

import policy_hub.reconcile_doc_index as rec  # noqa: E402


@pytest.fixture
def patched(monkeypatch):
    """Install fakes for every external dep reconcile_user touches.

    Returns a state dict the test inspects after the call.
    """
    state = {
        "s3": {},          # key -> {policy_id, ...}
        "indexed": set(),  # policy_ids currently in the index
        "upserted": [],    # list[tuple(user_id, item)]
        "deleted": [],     # list[policy_id]
    }

    # Patch the s3_utils.list_all_files (lazy-imported inside reconcile_user)
    s3_utils = MagicMock(name="s3_utils_stub")
    s3_utils.list_all_files = lambda folder=None: [
        {"Key": k} for k in sorted(state["s3"].keys())
    ]
    sys.modules["utils.s3_utils"] = s3_utils

    # Patch the policy_hub.doc_index helpers
    doc_index = MagicMock(name="doc_index_stub")
    doc_index.list_document_ids = lambda u: set(state["indexed"])
    def _upsert(u, item):
        state["upserted"].append((u, item))
        state["indexed"].add(item["policy_id"])
    def _delete(pid):
        state["deleted"].append(pid)
        state["indexed"].discard(pid)
    doc_index.upsert_document = _upsert
    doc_index.delete_document = _delete
    sys.modules["policy_hub.doc_index"] = doc_index

    # Patch _read_policy_yaml (lives in policy_hub.routes) without importing
    # the real routes module — install a stub package.
    routes_stub = MagicMock(name="policy_hub.routes_stub")
    routes_stub._read_policy_yaml = lambda u, key: state["s3"].get(key)
    sys.modules["policy_hub.routes"] = routes_stub

    yield state

    # Clean up the per-test module patches so they don't leak.
    for mod in ("utils.s3_utils", "policy_hub.doc_index", "policy_hub.routes"):
        sys.modules.pop(mod, None)


@pytest.mark.unit
class TestReconcileUser:
    def test_upserts_missing_yaml(self, patched):
        # S3 has p1 and p2, index has neither → both get upserted.
        patched["s3"] = {
            "u1/policies/p1.yaml": {"policy_id": "p1", "title": "A"},
            "u1/policies/p2.yaml": {"policy_id": "p2", "title": "B"},
        }
        patched["indexed"] = set()

        summary = rec.reconcile_user("u1")

        assert summary["upserted"] == 2
        assert summary["deleted"] == 0
        assert summary["errors"] == 0
        assert {u[1]["policy_id"] for u in patched["upserted"]} == {"p1", "p2"}

    def test_deletes_orphaned_index_row(self, patched):
        # S3 has p1, index has p1 + p2-orphan → p2-orphan deleted.
        patched["s3"] = {"u1/policies/p1.yaml": {"policy_id": "p1"}}
        patched["indexed"] = {"p1", "p2-orphan"}

        summary = rec.reconcile_user("u1")

        assert summary["deleted"] == 1
        assert summary["upserted"] == 0
        assert patched["deleted"] == ["p2-orphan"]

    def test_in_sync_is_a_noop(self, patched):
        patched["s3"] = {"u1/policies/p1.yaml": {"policy_id": "p1"}}
        patched["indexed"] = {"p1"}

        summary = rec.reconcile_user("u1")

        assert summary == {"upserted": 0, "deleted": 0, "errors": 0}
        assert patched["upserted"] == []
        assert patched["deleted"] == []

    def test_ignores_jobs_and_non_yaml(self, patched):
        patched["s3"] = {
            "u1/policies/p1.yaml": {"policy_id": "p1"},
            "u1/policies/jobs/state.json": {"junk": True},  # /jobs/ skipped
            "u1/policies/raw/upload.docx": {"junk": True},  # /raw/ skipped
        }
        patched["indexed"] = set()

        summary = rec.reconcile_user("u1")
        assert summary["upserted"] == 1
        assert {u[1]["policy_id"] for u in patched["upserted"]} == {"p1"}

    def test_read_failure_counts_as_error_not_crash(self, patched):
        # S3 lists p1 + p2, but reading p2 throws.
        patched["s3"] = {"u1/policies/p1.yaml": {"policy_id": "p1"}}
        patched["indexed"] = set()

        # Override read to throw for one key.
        def boom_read(u, key):
            if key.endswith("p2.yaml"):
                raise RuntimeError("decrypt boom")
            return patched["s3"].get(key)
        sys.modules["policy_hub.routes"]._read_policy_yaml = boom_read

        # Add a listing for p2 without storing its data.
        sys.modules["utils.s3_utils"].list_all_files = lambda folder=None: [
            {"Key": "u1/policies/p1.yaml"},
            {"Key": "u1/policies/p2.yaml"},
        ]

        summary = rec.reconcile_user("u1")

        # p1 was upserted; p2 read failed → recorded as error, not crash.
        assert summary["upserted"] == 1
        assert summary["errors"] == 1
