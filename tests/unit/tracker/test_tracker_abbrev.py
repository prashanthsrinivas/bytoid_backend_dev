"""Unit tests for tab_tracker.abbrev.mint_tracker_abbrev.

Uses an in-memory fake emulating the SQL the minter runs against
``tab_tracker_abbrev_seq``.
"""

import sys
import threading
from unittest.mock import MagicMock

import pytest

for _mod in ("pymysql", "pymysql.cursors", "db", "db.rds_db"):
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

import tab_tracker.abbrev as abbrev  # noqa: E402


class _FakeCursor:
    def __init__(self, store, lock):
        self._store = store        # (org, prefix) -> {seed, next_seq}
        self._lock = lock
        self._last = None

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()
        return False

    def execute(self, sql, params=()):
        s = " ".join(sql.split()).lower()
        if s.startswith("create table"):
            self._last = None
        elif s.startswith("select"):
            org, prefix = params
            self._last = self._store.get((org, prefix))
        elif s.startswith("insert into tab_tracker_abbrev_seq"):
            org, prefix, seed = params
            self._store[(org, prefix)] = {"seed": seed, "next_seq": 2}
            self._last = None
        elif s.startswith("update tab_tracker_abbrev_seq"):
            org, prefix = params
            self._store[(org, prefix)]["next_seq"] += 1
            self._last = None
        else:
            raise AssertionError(f"unexpected SQL: {sql!r}")

    def fetchone(self):
        return dict(self._last) if self._last is not None else None


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
    monkeypatch.setattr(abbrev, "connect_to_rds", lambda: _FakeConn(store, lock))
    return store


@pytest.mark.unit
class TestMintTrackerAbbrev:
    def test_first_is_0001(self, fake_db):
        # "Risk Management" matches the override → RSK
        assert abbrev.mint_tracker_abbrev("org1", "Risk Management") == "RSK-0001"

    def test_first_word_prefix_when_no_override(self, fake_db):
        # No override for "Risk Register" → first word "risk" → RIS
        assert abbrev.mint_tracker_abbrev("org1", "Risk Register") == "RIS-0001"

    def test_sequence_increments(self, fake_db):
        out = [abbrev.mint_tracker_abbrev("org1", "Risk Management") for _ in range(3)]
        assert out == ["RSK-0001", "RSK-0002", "RSK-0003"]

    def test_org_isolation(self, fake_db):
        assert abbrev.mint_tracker_abbrev("orgA", "Risk Management") == "RSK-0001"
        assert abbrev.mint_tracker_abbrev("orgB", "Risk Management") == "RSK-0001"

    def test_collision_salts(self, fake_db):
        # Two different seeds that both derive prefix ACC via first-word:
        #   "Access Plan"  -> seed "access"
        #   "Accounting"   -> seed "accounting"
        first = abbrev.mint_tracker_abbrev("org1", "Access Plan")
        second = abbrev.mint_tracker_abbrev("org1", "Accounting")
        assert first == "ACC-0001"
        assert second == "ACC2-0001"

    def test_missing_org_raises(self, fake_db):
        with pytest.raises(ValueError):
            abbrev.mint_tracker_abbrev("", "Risk Management")

    def test_threaded_distinct(self, fake_db):
        results = []
        rlock = threading.Lock()

        def worker():
            r = abbrev.mint_tracker_abbrev("org1", "Risk Management")
            with rlock:
                results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(set(results)) == 8


@pytest.mark.unit
class TestSafeMint:
    def test_returns_none_when_no_org(self, fake_db, monkeypatch):
        # safe_mint lazily imports get_user_org_id from workflow_route.state_machine;
        # inject a stub module that resolves to no org.
        import sys as _sys
        import types as _types
        fake_sm = _types.ModuleType("workflow_route.state_machine")
        fake_sm.get_user_org_id = lambda uid: None
        monkeypatch.setitem(_sys.modules, "workflow_route.state_machine", fake_sm)
        assert abbrev.safe_mint_tracker_abbrev("u1", "Risk Management") is None

    def test_mints_when_org_resolves(self, fake_db, monkeypatch):
        import sys as _sys
        import types as _types
        fake_sm = _types.ModuleType("workflow_route.state_machine")
        fake_sm.get_user_org_id = lambda uid: "org1"
        monkeypatch.setitem(_sys.modules, "workflow_route.state_machine", fake_sm)
        assert abbrev.safe_mint_tracker_abbrev("u1", "Risk Management") == "RSK-0001"
