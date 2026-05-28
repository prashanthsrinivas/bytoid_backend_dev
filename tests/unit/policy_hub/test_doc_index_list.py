"""Unit tests for policy_hub.doc_index list/get/list_ids and title round-trip."""

import sys
import threading
from unittest.mock import MagicMock

import pytest

for _mod in ("pymysql", "pymysql.cursors", "db", "db.rds_db"):
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

import policy_hub.doc_index as di  # noqa: E402
from tests.unit.policy_hub.test_doc_index_upsert import _FakeConn  # noqa: E402


@pytest.fixture
def fake_db(monkeypatch):
    store: dict = {}
    lock = threading.Lock()
    monkeypatch.setattr(di, "connect_to_rds", lambda: _FakeConn(store, lock))
    monkeypatch.setattr(di, "_resolve_org", lambda _u: "org-test")
    return store


@pytest.fixture
def fake_db_identity(fake_db, monkeypatch):
    monkeypatch.setattr(di, "_encrypt_title", lambda _u, t: t)
    monkeypatch.setattr(di, "_decrypt_title", lambda _u, v: v or "")
    return fake_db


SAMPLE_A = {
    "policy_id": "pA",
    "title": "Access Control Policy",
    "type": "policy",
    "doc_ref": "ACC-0001",
    "frameworks": ["ISO27001"],
    "created_at": "2026-05-03T00:00:00Z",
}
SAMPLE_B = {
    "policy_id": "pB",
    "title": "Encryption Standard",
    "type": "standard",
    "doc_ref": "ENC-S0001",
    "frameworks": [],
    "created_at": "2026-05-01T00:00:00Z",
}


@pytest.mark.unit
class TestListDocuments:
    def test_returns_owners_documents(self, fake_db_identity):
        di.upsert_document("u1", SAMPLE_A)
        di.upsert_document("u1", SAMPLE_B)
        di.upsert_document("u2", {**SAMPLE_A, "policy_id": "pX"})

        rows = di.list_documents("u1")

        assert {r["policy_id"] for r in rows} == {"pA", "pB"}
        # Sorted DESC by created_at
        assert rows[0]["policy_id"] == "pA"
        assert rows[1]["policy_id"] == "pB"

    def test_returned_shape_matches_list_item(self, fake_db_identity):
        di.upsert_document("u1", SAMPLE_A)
        rows = di.list_documents("u1")
        row = rows[0]
        # The shape /policy-hub/list emits per item
        for k in ("policy_id", "title", "type", "doc_ref", "frameworks",
                  "validation_status", "created_at", "etag", "owner_user_id"):
            assert k in row
        assert row["type"] == "policy"
        assert row["frameworks"] == ["ISO27001"]
        assert row["owner_user_id"] == "u1"

    def test_empty_user_returns_empty(self, fake_db_identity):
        assert di.list_documents("nobody") == []
        assert di.list_documents("") == []

    def test_heavy_fields_are_empty_not_missing(self, fake_db_identity):
        # The fast path must not carry the heavy content/sections values, but
        # the *keys* must be present (as empty) so frontend `.map`/`.length`
        # over them never hits `undefined`. Detail view loads the real data.
        di.upsert_document("u1", {**SAMPLE_A, "content": "<html>...</html>",
                                   "sections": [{"id": "s", "statements": []}]})
        row = di.list_documents("u1")[0]
        # Present but empty — shape contract for the frontend.
        assert row["content"] == ""
        assert row["sections"] == []
        assert row["statements"] == []


@pytest.mark.unit
class TestGetDocuments:
    def test_batched_lookup(self, fake_db_identity):
        di.upsert_document("u1", SAMPLE_A)
        di.upsert_document("u2", SAMPLE_B)

        hits = di.get_documents(["pA", "pB", "missing"])

        assert set(hits.keys()) == {"pA", "pB"}
        assert hits["pA"]["title"] == "Access Control Policy"
        assert hits["pB"]["title"] == "Encryption Standard"

    def test_empty_input(self, fake_db_identity):
        assert di.get_documents([]) == {}
        assert di.get_documents(None) == {}

    def test_skips_blank_ids(self, fake_db_identity):
        di.upsert_document("u1", SAMPLE_A)
        assert di.get_documents(["", None, "pA"]) == {
            "pA": di.get_documents(["pA"])["pA"]
        }


@pytest.mark.unit
class TestListDocumentIds:
    def test_returns_set_for_user(self, fake_db_identity):
        di.upsert_document("u1", SAMPLE_A)
        di.upsert_document("u1", SAMPLE_B)
        di.upsert_document("u2", {**SAMPLE_A, "policy_id": "pX"})

        assert di.list_document_ids("u1") == {"pA", "pB"}
        assert di.list_document_ids("u2") == {"pX"}
        assert di.list_document_ids("nobody") == set()


@pytest.mark.unit
class TestTitleRoundTrip:
    def test_encrypted_title_round_trips_through_index(self, fake_db, monkeypatch):
        # Reversible "encryption" that wraps the title; proves encrypt-on-write
        # / decrypt-on-read both fire and that the stored value is not the
        # plaintext.
        monkeypatch.setattr(di, "_encrypt_title",
                            lambda u, t: f"ENC[{u}]:{t}" if t else t)
        monkeypatch.setattr(di, "_decrypt_title",
                            lambda u, v: v[len(f"ENC[{u}]:"):] if v and v.startswith(f"ENC[{u}]:") else v)

        di.upsert_document("u1", SAMPLE_A)

        # Stored value is encrypted (not the plaintext title).
        assert fake_db["pA"]["title_enc"] == "ENC[u1]:Access Control Policy"

        # Read goes through decrypt → plaintext.
        rows = di.list_documents("u1")
        assert rows[0]["title"] == "Access Control Policy"
