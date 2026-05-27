"""Unit tests for policy_hub.doc_ref.mint_doc_ref.

Uses an in-memory fake that emulates the exact SQL the minter runs against
``policy_hub_doc_ref_seq`` (CREATE / SELECT ... FOR UPDATE / INSERT / UPDATE),
so no real RDS is needed.
"""

import sys
import threading
from unittest.mock import MagicMock

import pytest

for _mod in ("pymysql", "pymysql.cursors", "db", "db.rds_db"):
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

import policy_hub.doc_ref as doc_ref  # noqa: E402


class _FakeCursor:
    """Interprets the handful of statements _claim_sequence issues."""

    def __init__(self, store, lock):
        self._store = store          # dict: (org, prefix, dtype) -> {seed, next_seq}
        self._lock = lock
        self._last = None

    def __enter__(self):
        # Hold the shared lock for the whole cursor lifetime to emulate the
        # serialization that SELECT ... FOR UPDATE provides in real RDS.
        self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()
        return False

    def execute(self, sql, params=()):
        s = " ".join(sql.split()).lower()
        if s.startswith("create table"):
            self._last = None
            return
        if s.startswith("select"):
            org, prefix, dtype = params
            self._last = self._store.get((org, prefix, dtype))
            return
        if s.startswith("insert into policy_hub_doc_ref_seq"):
            org, prefix, dtype, seed = params
            self._store[(org, prefix, dtype)] = {"seed": seed, "next_seq": 2}
            self._last = None
            return
        if s.startswith("update policy_hub_doc_ref_seq"):
            org, prefix, dtype = params
            self._store[(org, prefix, dtype)]["next_seq"] += 1
            self._last = None
            return
        raise AssertionError(f"unexpected SQL: {sql!r}")

    def fetchone(self):
        if self._last is None:
            return None
        return dict(self._last)


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
    monkeypatch.setattr(doc_ref, "connect_to_rds", lambda: _FakeConn(store, lock))
    return store


@pytest.mark.unit
class TestMintBasics:
    def test_first_ref_is_0001(self, fake_db):
        assert doc_ref.mint_doc_ref("org1", "policy", "Access Control Policy") == "ACC-0001"

    def test_sequence_increments_per_prefix(self, fake_db):
        refs = [
            doc_ref.mint_doc_ref("org1", "policy", "Access Control Policy")
            for _ in range(3)
        ]
        assert refs == ["ACC-0001", "ACC-0002", "ACC-0003"]

    def test_type_suffix(self, fake_db):
        assert doc_ref.mint_doc_ref("org1", "policy", "Access Control") == "ACC-0001"
        assert doc_ref.mint_doc_ref("org1", "procedure", "Access Control") == "ACC-P0001"
        assert doc_ref.mint_doc_ref("org1", "standard", "Access Control") == "ACC-S0001"

    def test_four_digit_zero_padding(self, fake_db):
        store_key = ("org1", "ACC", "policy")
        # pre-seed at seq 41 so the next mint is 0042
        fake_db[store_key] = {"seed": "access control", "next_seq": 42}
        assert doc_ref.mint_doc_ref("org1", "policy", "Access Control") == "ACC-0042"


@pytest.mark.unit
class TestSequenceIsolation:
    def test_org_isolation(self, fake_db):
        assert doc_ref.mint_doc_ref("orgA", "policy", "Access Control") == "ACC-0001"
        assert doc_ref.mint_doc_ref("orgB", "policy", "Access Control") == "ACC-0001"

    def test_doc_type_isolation(self, fake_db):
        assert doc_ref.mint_doc_ref("org1", "policy", "Access Control") == "ACC-0001"
        assert doc_ref.mint_doc_ref("org1", "procedure", "Access Control") == "ACC-P0001"
        # the procedure didn't consume the policy sequence
        assert doc_ref.mint_doc_ref("org1", "policy", "Access Control") == "ACC-0002"

    def test_same_prefix_same_seed_shares_sequence(self, fake_db):
        # Two access-control titles share the ACC sequence
        a = doc_ref.mint_doc_ref("org1", "policy", "Access Control Policy")
        b = doc_ref.mint_doc_ref("org1", "policy", "Remote Access Control Rules")
        assert a == "ACC-0001"
        assert b == "ACC-0002"


@pytest.mark.unit
class TestCollisionSalting:
    def test_different_seed_same_prefix_gets_salted(self, fake_db):
        # "Accounting" derives ACC via stopword fallback; "Access Control"
        # derives ACC via override. Different seeds → second one salts to ACC2.
        first = doc_ref.mint_doc_ref("org1", "policy", "Accounting Policy")
        second = doc_ref.mint_doc_ref("org1", "policy", "Access Control Policy")
        assert first == "ACC-0001"
        assert second == "ACC2-0001"

    def test_salt_is_stable_across_calls(self, fake_db):
        doc_ref.mint_doc_ref("org1", "policy", "Accounting Policy")          # ACC
        s1 = doc_ref.mint_doc_ref("org1", "policy", "Access Control Policy")  # ACC2
        s2 = doc_ref.mint_doc_ref("org1", "policy", "Access Control Rules")   # ACC2 again (same seed)
        assert s1 == "ACC2-0001"
        assert s2 == "ACC2-0002"


@pytest.mark.unit
class TestValidation:
    def test_missing_org_raises(self, fake_db):
        with pytest.raises(ValueError):
            doc_ref.mint_doc_ref("", "policy", "Access Control")

    def test_bad_doc_type_raises(self, fake_db):
        with pytest.raises(ValueError):
            doc_ref.mint_doc_ref("org1", "guideline", "Access Control")


@pytest.mark.unit
class TestConcurrency:
    def test_threaded_mints_are_distinct(self, fake_db):
        # The fake serializes via a lock to emulate SELECT ... FOR UPDATE.
        results: list[str] = []
        res_lock = threading.Lock()

        def worker():
            ref = doc_ref.mint_doc_ref("org1", "policy", "Access Control Policy")
            with res_lock:
                results.append(ref)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 10
        assert len(set(results)) == 10, f"duplicate refs minted: {results}"
