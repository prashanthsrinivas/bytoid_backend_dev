"""Tests for VRA question injection logic (pure parts) — vra/workflow_inject.py."""

import pytest

from vra import workflow_inject as wi
from vra.osint.normalize import build_snapshot, make_finding
from vra.schema import CAT_BREACH, CAT_DOMAIN, CAT_SECURITY, SEV_CRITICAL, SEV_INFO, SEV_LOW


# ── item builders ─────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_make_question_item_shape():
    it = wi.make_question_item("Q?", osint_derived=True, source_url="http://x")
    assert it["section"] == wi.VENDOR_INTEL_SECTION
    assert it["options"] == {} and it["answer"] is None
    assert it["vra_question"] is True and it["osint_derived"] is True
    assert it["source_url"] == "http://x"


@pytest.mark.unit
def test_vendor_question_items():
    items = wi.vendor_question_items()
    assert len(items) == 2
    roles = {i["vra_role"] for i in items}
    assert roles == {"vendor_name", "vendor_domain"}
    assert all(i["locked"] and i["vra_question"] for i in items)


# ── set_vra_block ─────────────────────────────────────────────────────────────

def _sig(qid):  # a non-VRA (e.g. SIG) question
    return {"id": qid, "question": f"sig {qid}", "options": {}, "section": "General"}


@pytest.mark.unit
def test_block_puts_vendor_first_then_sig():
    out = wi.set_vra_block([_sig("a"), _sig("b")], vendor_items=wi.vendor_question_items())
    assert [q.get("vra_role") for q in out[:2]] == ["vendor_name", "vendor_domain"]
    assert [q["id"] for q in out[2:]] == ["a", "b"]  # SIG preserved, after VRA


@pytest.mark.unit
def test_vendor_idempotent():
    once = wi.set_vra_block([], vendor_items=wi.vendor_question_items())
    twice = wi.set_vra_block(once, vendor_items=wi.vendor_question_items())
    assert len([q for q in twice if q.get("vra_role")]) == 2  # not duplicated


@pytest.mark.unit
def test_osint_replace_vs_append():
    base = wi.set_vra_block([_sig("a")], vendor_items=wi.vendor_question_items())
    o1 = [wi.make_question_item("o1", osint_derived=True)]
    appended = wi.set_vra_block(base, osint_items=o1)
    assert sum(1 for q in appended if q.get("osint_derived")) == 1
    # replace: a fresh osint set supersedes the old one
    o2 = [wi.make_question_item("o2", osint_derived=True), wi.make_question_item("o3", osint_derived=True)]
    replaced = wi.set_vra_block(appended, osint_items=o2, replace_osint=True)
    osint_qs = [q["question"] for q in replaced if q.get("osint_derived")]
    assert osint_qs == ["o2", "o3"]
    # order: vendor, then osint, then sig
    assert replaced[0].get("vra_role") == "vendor_name"
    assert replaced[-1]["id"] == "a"


@pytest.mark.unit
def test_block_preserves_non_vra_only_once():
    out = wi.set_vra_block([_sig("a")], vendor_items=wi.vendor_question_items())
    out2 = wi.set_vra_block(out, vendor_items=wi.vendor_question_items())
    assert sum(1 for q in out2 if q["id"] == "a") == 1


# ── derive_osint_questions ────────────────────────────────────────────────────

def _snap(findings):
    return build_snapshot(scan_id="s", assessment_id="a", vendor_name="Acme",
                          vendor_domain="acme.com", findings=findings)


@pytest.mark.unit
def test_derive_from_kev_and_breach():
    snap = _snap([
        make_finding(category=CAT_SECURITY, evidence_type="known_exploited_vulnerability",
                     source="CISA KEV", finding_summary="x", source_url="http://cve",
                     severity=SEV_CRITICAL, supporting_details={"cve": "CVE-2021-1"}),
        make_finding(category=CAT_BREACH, evidence_type="breach_disclosure", source="HIBP",
                     finding_summary="breach", source_url="http://hibp", severity="high",
                     supporting_details={"name": "Acme2020"}),
    ])
    qs = wi.derive_osint_questions(snap)
    assert len(qs) == 2
    assert all(q["osint_derived"] and q["section"] == wi.VENDOR_INTEL_SECTION for q in qs)
    assert any("CVE-2021-1" in q["question"] for q in qs)
    assert {q["source_url"] for q in qs} == {"http://cve", "http://hibp"}


@pytest.mark.unit
def test_derive_skips_low_info_and_unmapped():
    snap = _snap([
        make_finding(category=CAT_DOMAIN, evidence_type="dns_mx", source="DNS",
                     finding_summary="mx", severity=SEV_INFO),
        make_finding(category=CAT_DOMAIN, evidence_type="spf", source="DNS",
                     finding_summary="spf", severity=SEV_LOW),
    ])
    assert wi.derive_osint_questions(snap) == []


@pytest.mark.unit
def test_derive_dmarc_missing():
    snap = _snap([
        make_finding(category=CAT_DOMAIN, evidence_type="dmarc", source="DNS",
                     finding_summary="no dmarc", severity="high",
                     risk_indicators=["dmarc_missing"]),
    ])
    qs = wi.derive_osint_questions(snap)
    assert len(qs) == 1 and "DMARC" in qs[0]["question"]
