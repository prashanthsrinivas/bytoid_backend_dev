"""Unit tests for services.statement_tracker_refs (upsert/delete/query).

An in-memory fake emulates the specific SQL the helper runs against
``statement_tracker_refs`` (PK = tracker_id,row_id,column_id,statement_id).
"""

import sys
import types
from unittest.mock import MagicMock

import pytest

for _mod in ("pymysql", "pymysql.cursors", "db", "db.rds_db"):
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

import services.statement_tracker_refs as ref  # noqa: E402

_PK = ("tracker_id", "row_id", "column_id", "statement_id")


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows          # list[dict]
        self._result = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        s = " ".join(sql.split()).lower()
        self._result = []
        self.rowcount = 0
        if s.startswith("create table"):
            return
        if s.startswith("delete from statement_tracker_refs where tracker_id=%s and row_id=%s and column_id=%s"):
            t, r, c = params
            before = len(self._rows)
            self._rows[:] = [x for x in self._rows if not (x["tracker_id"] == t and x["row_id"] == r and x["column_id"] == c)]
            self.rowcount = before - len(self._rows)
            return
        if s.startswith("insert into statement_tracker_refs"):
            sid, policy_id, doc_type, tid, abbrev, rid, cid, status, now = params
            row = {
                "statement_id": sid, "policy_id": policy_id, "doc_type": doc_type,
                "tracker_id": tid, "tracker_abbrev": abbrev, "row_id": rid,
                "column_id": cid, "status": status, "updated_at": now,
            }
            pk = (tid, rid, cid, sid)
            for x in self._rows:
                if (x["tracker_id"], x["row_id"], x["column_id"], x["statement_id"]) == pk:
                    x.update(row)
                    return
            self._rows.append(row)
            return
        if s.startswith("delete from statement_tracker_refs where tracker_id=%s and policy_id=%s"):
            t, p = params
            before = len(self._rows)
            self._rows[:] = [x for x in self._rows if not (x["tracker_id"] == t and x["policy_id"] == p)]
            self.rowcount = before - len(self._rows)
            return
        if s.startswith("delete from statement_tracker_refs where tracker_id=%s"):
            (t,) = params
            before = len(self._rows)
            self._rows[:] = [x for x in self._rows if x["tracker_id"] != t]
            self.rowcount = before - len(self._rows)
            return
        if s.startswith("update statement_tracker_refs set status"):
            status, now, p, sid = params
            n = 0
            for x in self._rows:
                if x["policy_id"] == p and x["statement_id"] == sid:
                    x["status"] = status
                    x["updated_at"] = now
                    n += 1
            self.rowcount = n
            return
        if s.startswith("select count(*)"):
            sid = params[0]
            self._result = [{"c": sum(1 for x in self._rows if x["statement_id"] == sid)}]
            return
        if s.startswith("select tracker_id, tracker_abbrev, count(distinct row_id)"):
            p = params[0]
            agg: dict = {}
            for x in self._rows:
                if x["policy_id"] != p:
                    continue
                key = (x["tracker_id"], x["tracker_abbrev"])
                agg.setdefault(key, set()).add(x["row_id"])
            self._result = [
                {"tracker_id": t, "tracker_abbrev": a, "mapped_row_count": len(rids)}
                for (t, a), rids in agg.items()
            ]
            return
        if s.startswith("select tracker_id, tracker_abbrev, row_id, column_id, policy_id"):
            sid = params[0]
            self._result = [dict(x) for x in self._rows if x["statement_id"] == sid]
            return
        if s.startswith("select statement_id, tracker_id"):
            if len(params) == 2:
                p, t = params
                self._result = [dict(x) for x in self._rows if x["policy_id"] == p and x["tracker_id"] == t]
            else:
                p = params[0]
                self._result = [dict(x) for x in self._rows if x["policy_id"] == p]
            return
        raise AssertionError(f"unexpected SQL: {sql!r}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self, *_a, **_k):
        return _FakeCursor(self._rows)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def fake_db(monkeypatch):
    rows: list[dict] = []
    monkeypatch.setattr(ref, "connect_to_rds", lambda: _FakeConn(rows))
    return rows


@pytest.fixture
def captured_audit(monkeypatch):
    events = []
    fake_audit = types.ModuleType("services.audit_log_service")
    fake_audit.log_audit_event = lambda **kw: events.append(kw)
    monkeypatch.setitem(sys.modules, "services.audit_log_service", fake_audit)
    return events


@pytest.mark.unit
class TestReplaceCellRefs:
    def test_inserts_new_refs(self, fake_db):
        n = ref.replace_cell_refs("trk1", "r1", "c1", "pol1", "policy", "RSK-0001",
                                  [{"statement_id": "s1"}, {"statement_id": "s2"}])
        assert n == 2
        assert len(fake_db) == 2

    def test_replace_is_idempotent_on_pk(self, fake_db):
        ref.replace_cell_refs("trk1", "r1", "c1", "pol1", "policy", "RSK-0001",
                              [{"statement_id": "s1"}, {"statement_id": "s1"}])
        # duplicate statement_id in same cell collapses to one PK row
        assert len(fake_db) == 1

    def test_replace_clears_previous_cell_contents(self, fake_db):
        ref.replace_cell_refs("trk1", "r1", "c1", "pol1", "policy", "RSK-0001",
                              [{"statement_id": "s1"}, {"statement_id": "s2"}])
        # re-map the same cell to a different statement set
        ref.replace_cell_refs("trk1", "r1", "c1", "pol1", "policy", "RSK-0001",
                              [{"statement_id": "s3"}])
        ids = {r["statement_id"] for r in fake_db}
        assert ids == {"s3"}

    def test_skips_entries_without_statement_id(self, fake_db):
        n = ref.replace_cell_refs("trk1", "r1", "c1", "pol1", "policy", None,
                                  [{"statement_id": None}, {"foo": "bar"}, {"statement_id": "s1"}])
        assert n == 1

    def test_emits_audit(self, fake_db, captured_audit):
        ref.replace_cell_refs("trk1", "r1", "c1", "pol1", "policy", "RSK-0001",
                              [{"statement_id": "s1"}])
        assert any(e["action"] == "STATEMENT_TRACKER_REF_UPSERT" for e in captured_audit)


@pytest.mark.unit
class TestDeletes:
    def test_delete_for_policy(self, fake_db, captured_audit):
        ref.replace_cell_refs("trk1", "r1", "c1", "pol1", "policy", "RSK-0001", [{"statement_id": "s1"}])
        ref.replace_cell_refs("trk1", "r2", "c1", "pol2", "policy", "RSK-0001", [{"statement_id": "s9"}])
        deleted = ref.delete_refs_for_policy("trk1", "pol1")
        assert deleted == 1
        assert {r["policy_id"] for r in fake_db} == {"pol2"}
        assert any(e["action"] == "STATEMENT_TRACKER_REF_DELETE" for e in captured_audit)

    def test_cascade_delete_for_tracker(self, fake_db):
        ref.replace_cell_refs("trk1", "r1", "c1", "pol1", "policy", "RSK-0001",
                              [{"statement_id": "s1"}, {"statement_id": "s2"}])
        ref.replace_cell_refs("trk2", "r1", "c1", "pol1", "policy", "CHG-0001", [{"statement_id": "s1"}])
        deleted = ref.delete_refs_for_tracker("trk1")
        assert deleted == 2
        assert {r["tracker_id"] for r in fake_db} == {"trk2"}


@pytest.mark.unit
class TestStatusAndQueries:
    def test_set_status_for_statement(self, fake_db):
        ref.replace_cell_refs("trk1", "r1", "c1", "pol1", "policy", "RSK-0001", [{"statement_id": "s1"}])
        ref.replace_cell_refs("trk2", "r1", "c1", "pol1", "policy", "CHG-0001", [{"statement_id": "s1"}])
        n = ref.set_status_for_statement("pol1", "s1", "superseded")
        assert n == 2
        assert all(r["status"] == "superseded" for r in fake_db)

    def test_get_trackers_for_statement(self, fake_db):
        ref.replace_cell_refs("trk1", "r1", "c1", "pol1", "policy", "RSK-0001",
                              [{"statement_id": "s1"}])
        ref.replace_cell_refs("trk1", "r2", "c1", "pol1", "policy", "RSK-0001",
                              [{"statement_id": "s1"}])
        rows, total = ref.get_trackers_for_statement("s1")
        assert total == 2
        assert len(rows) == 2

    def test_get_trackers_for_policy_counts_distinct_rows(self, fake_db):
        ref.replace_cell_refs("trk1", "r1", "c1", "pol1", "policy", "RSK-0001",
                              [{"statement_id": "s1"}, {"statement_id": "s2"}])
        ref.replace_cell_refs("trk1", "r2", "c1", "pol1", "policy", "RSK-0001",
                              [{"statement_id": "s1"}])
        out = ref.get_trackers_for_policy("pol1")
        assert len(out) == 1
        assert out[0]["tracker_id"] == "trk1"
        assert out[0]["mapped_row_count"] == 2  # r1 and r2

    def test_get_refs_for_policy_scoped_to_tracker(self, fake_db):
        ref.replace_cell_refs("trk1", "r1", "c1", "pol1", "policy", "RSK-0001", [{"statement_id": "s1"}])
        ref.replace_cell_refs("trk2", "r1", "c1", "pol1", "policy", "CHG-0001", [{"statement_id": "s2"}])
        scoped = ref.get_refs_for_policy("pol1", tracker_id="trk1")
        assert len(scoped) == 1
        assert scoped[0]["tracker_id"] == "trk1"
        all_refs = ref.get_refs_for_policy("pol1")
        assert len(all_refs) == 2
