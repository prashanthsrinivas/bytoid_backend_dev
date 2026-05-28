"""Unit tests for policy_hub.doc_index upsert/delete (in-memory fake DB)."""

import sys
import threading
from unittest.mock import MagicMock

import pytest

for _mod in ("pymysql", "pymysql.cursors", "db", "db.rds_db"):
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

import policy_hub.doc_index as di  # noqa: E402


class _FakeCursor:
    """Interprets the handful of statements doc_index issues."""

    def __init__(self, store, lock):
        self._store = store  # dict: policy_id -> row dict
        self._lock = lock
        self._last_rows: list[dict] = []
        self._last_count = 0

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()
        return False

    def execute(self, sql, params=()):
        s = " ".join(sql.split()).lower()
        if s.startswith("create table"):
            return
        if s.startswith("insert into policy_hub_documents"):
            (policy_id, user_id, org_id, title_enc, doc_ref, doc_type,
             frameworks_json, validation_status, etag, created_at, updated_at) = params
            self._store[policy_id] = {
                "policy_id": policy_id, "user_id": user_id, "org_id": org_id,
                "title_enc": title_enc, "doc_ref": doc_ref, "doc_type": doc_type,
                "frameworks_json": frameworks_json,
                "validation_status": validation_status, "etag": etag,
                "created_at": created_at, "updated_at": updated_at,
            }
            return
        if s.startswith("delete from policy_hub_documents"):
            (policy_id,) = params
            deleted = 1 if policy_id in self._store else 0
            self._store.pop(policy_id, None)
            self._last_count = deleted
            return
        if s.startswith("select * from policy_hub_documents where user_id"):
            (user_id,) = params
            self._last_rows = [
                dict(r) for r in self._store.values() if r["user_id"] == user_id
            ]
            self._last_rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
            return
        if s.startswith("select * from policy_hub_documents where policy_id in"):
            ids = list(params)
            self._last_rows = [dict(self._store[i]) for i in ids if i in self._store]
            return
        if s.startswith("select policy_id from policy_hub_documents"):
            (user_id,) = params
            self._last_rows = [
                {"policy_id": r["policy_id"]}
                for r in self._store.values() if r["user_id"] == user_id
            ]
            return
        raise AssertionError(f"unexpected SQL: {sql!r}")

    def fetchall(self):
        return list(self._last_rows)

    @property
    def rowcount(self):
        return self._last_count


class _FakeConn:
    def __init__(self, store, lock):
        self._store = store
        self._lock = lock

    def cursor(self, *_a, **_k):
        return _FakeCursor(self._store, self._lock)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def fake_db(monkeypatch):
    store: dict = {}
    lock = threading.Lock()
    monkeypatch.setattr(di, "connect_to_rds", lambda: _FakeConn(store, lock))
    # Bypass routes import for encryption — identity makes it easy to assert
    # the round-trip; encryption is exercised separately in test_doc_index_list.
    monkeypatch.setattr(di, "_encrypt_title", lambda _u, t: t)
    monkeypatch.setattr(di, "_decrypt_title", lambda _u, v: v or "")
    monkeypatch.setattr(di, "_resolve_org", lambda _u: "org-test")
    return store


SAMPLE = {
    "policy_id": "p1",
    "title": "Access Control Policy",
    "type": "policy",
    "doc_ref": "ACC-0001",
    "frameworks": ["ISO27001"],
    "validation_status": "ok",
    "etag": "etag-1",
    "created_at": "2026-05-01T00:00:00Z",
}


@pytest.mark.unit
class TestUpsert:
    def test_inserts_all_fields(self, fake_db):
        di.upsert_document("u1", SAMPLE)
        row = fake_db["p1"]
        assert row["policy_id"] == "p1"
        assert row["user_id"] == "u1"
        assert row["org_id"] == "org-test"
        assert row["title_enc"] == "Access Control Policy"  # identity stub
        assert row["doc_ref"] == "ACC-0001"
        assert row["doc_type"] == "policy"
        assert row["frameworks_json"] == '["ISO27001"]'
        assert row["validation_status"] == "ok"
        assert row["etag"] == "etag-1"
        assert row["created_at"] == "2026-05-01T00:00:00Z"
        assert row["updated_at"]  # set to now

    def test_is_idempotent(self, fake_db):
        di.upsert_document("u1", SAMPLE)
        first_updated = fake_db["p1"]["updated_at"]
        di.upsert_document("u1", {**SAMPLE, "title": "Renamed"})
        assert fake_db["p1"]["title_enc"] == "Renamed"
        assert fake_db["p1"]["updated_at"] >= first_updated
        assert len(fake_db) == 1  # still one row

    def test_doc_type_defaults_to_policy(self, fake_db):
        di.upsert_document("u1", {**SAMPLE, "type": None})
        assert fake_db["p1"]["doc_type"] == "policy"

    def test_missing_policy_id_is_a_noop(self, fake_db):
        di.upsert_document("u1", {**SAMPLE, "policy_id": None})
        assert fake_db == {}

    def test_missing_user_id_is_a_noop(self, fake_db):
        di.upsert_document("", SAMPLE)
        assert fake_db == {}

    def test_no_frameworks_stores_empty_array(self, fake_db):
        di.upsert_document("u1", {**SAMPLE, "frameworks": None})
        assert fake_db["p1"]["frameworks_json"] == "[]"


@pytest.mark.unit
class TestDelete:
    def test_removes_row(self, fake_db):
        di.upsert_document("u1", SAMPLE)
        deleted = di.delete_document("p1")
        assert deleted == 1
        assert "p1" not in fake_db

    def test_missing_id_is_noop(self, fake_db):
        di.upsert_document("u1", SAMPLE)
        deleted = di.delete_document("nope")
        assert deleted == 0
        assert "p1" in fake_db

    def test_empty_id_is_noop(self, fake_db):
        di.upsert_document("u1", SAMPLE)
        assert di.delete_document("") == 0
        assert "p1" in fake_db
