"""VraService tests — assessment lifecycle + vendor/title sync (in-memory store)."""

import pytest

from vra.schema import ASSESSMENT_VRA, SCAN_PENDING, VRA_ROLE_VENDOR_DOMAIN, VRA_ROLE_VENDOR_NAME
from vra.service import VraService, build_default_question_items


class _MemStore:
    def __init__(self):
        self.db = {}

    def _k(self, user_id, aid):
        return f"{user_id}:{aid}"

    def save_assessment(self, user_id, record):
        self.db[self._k(user_id, record["assessment_id"])] = record
        return record

    def get_assessment(self, user_id, aid):
        return self.db.get(self._k(user_id, aid))

    def list_assessments(self, user_id):
        return [v for k, v in self.db.items() if k.startswith(f"{user_id}:")]

    def delete_assessment(self, user_id, aid):
        self.db.pop(self._k(user_id, aid), None)


@pytest.fixture
def svc():
    return VraService(storage=_MemStore())


# ── default questions ────────────────────────────────────────────────────────

@pytest.mark.unit
def test_default_questions_shape_and_roles():
    items = build_default_question_items()
    assert len(items) == 2
    assert items[0]["vra_role"] == VRA_ROLE_VENDOR_NAME
    assert items[1]["vra_role"] == VRA_ROLE_VENDOR_DOMAIN
    for it in items:
        assert it["locked"] is True and it["required"] is True
        # carries the playbook output-item keys
        assert {"id", "question", "user_answer", "options", "comment", "section"} <= set(it)
    # unique ids
    assert items[0]["id"] != items[1]["id"]


# ── create / read / list / delete ────────────────────────────────────────────

@pytest.mark.unit
def test_create_assessment_defaults(svc):
    rec = svc.create_assessment("u1", vendor_name="Microsoft", vendor_domain="https://Microsoft.com/x")
    assert rec["assessment_type"] == ASSESSMENT_VRA
    assert rec["scan_state"] == SCAN_PENDING
    assert rec["vendor_domain"] == "microsoft.com"        # normalized
    assert rec["report_title"] == "Vendor Risk Assessment – Microsoft"  # noqa: RUF001
    assert rec["retention_until"] and rec["created_at"]
    # persisted + retrievable
    assert svc.get_assessment("u1", rec["assessment_id"]) == rec


@pytest.mark.unit
def test_create_with_invalid_domain_blanks_it(svc):
    rec = svc.create_assessment("u1", vendor_name="X", vendor_domain="not a domain")
    assert rec["vendor_domain"] == ""


@pytest.mark.unit
def test_list_and_delete(svc):
    a = svc.create_assessment("u1", vendor_name="A")
    svc.create_assessment("u1", vendor_name="B")
    assert len(svc.list_assessments("u1")) == 2
    assert svc.delete_assessment("u1", a["assessment_id"]) is True
    assert svc.delete_assessment("u1", "missing") is False
    assert len(svc.list_assessments("u1")) == 1


@pytest.mark.unit
def test_user_scoped(svc):
    rec = svc.create_assessment("u1", vendor_name="A")
    assert svc.get_assessment("u2", rec["assessment_id"]) is None


# ── vendor / title sync ──────────────────────────────────────────────────────

@pytest.mark.unit
def test_set_vendor_resyncs_title(svc):
    rec = svc.create_assessment("u1", vendor_name="Old")
    updated = svc.set_vendor("u1", rec["assessment_id"], vendor_name="Amazon Web Services")
    assert updated["vendor_name"] == "Amazon Web Services"
    assert updated["report_title"] == "Vendor Risk Assessment – Amazon Web Services"  # noqa: RUF001


@pytest.mark.unit
def test_set_vendor_normalizes_domain(svc):
    rec = svc.create_assessment("u1", vendor_name="A")
    updated = svc.set_vendor("u1", rec["assessment_id"], vendor_domain="HTTPS://AWS.Amazon.com")
    assert updated["vendor_domain"] == "aws.amazon.com"


@pytest.mark.unit
def test_set_vendor_missing_returns_none(svc):
    assert svc.set_vendor("u1", "nope", vendor_name="X") is None


@pytest.mark.unit
def test_ready_for_collection(svc):
    assert svc.ready_for_collection({"vendor_name": "A", "vendor_domain": "a.com"}) is True
    assert svc.ready_for_collection({"vendor_name": "A", "vendor_domain": ""}) is False
    assert svc.ready_for_collection({}) is False
