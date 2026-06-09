"""Risk-relevance scoring + classification + executive summary tests."""

import pytest

from vra.osint import relevance as rel
from vra.osint.normalize import make_finding
from vra import report_inputs as ri
from vra.osint.normalize import build_snapshot
from vra.schema import (
    CAT_BREACH, CAT_DOMAIN, CAT_REPUTATION, CAT_SECURITY,
    SEV_CRITICAL, SEV_HIGH, SEV_INFO, SEV_MEDIUM,
)


def _f(evidence_type, **kw):
    kw.setdefault("category", CAT_SECURITY)
    kw.setdefault("source", "S")
    kw.setdefault("finding_summary", "x")
    return make_finding(evidence_type=evidence_type, **kw)


# ── classification ────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_classify_by_evidence_type():
    assert rel.classify_risk_category(_f("breach_disclosure")) == rel.RISK_SECURITY
    assert rel.classify_risk_category(_f("certification_mention", category="compliance")) == rel.RISK_COMPLIANCE
    assert rel.classify_risk_category(_f("security_txt", category="compliance")) == rel.RISK_COMPLIANCE


@pytest.mark.unit
def test_classify_news_by_keyword():
    legal = _f("news_article", category=CAT_REPUTATION, finding_summary="Acme hit with class action lawsuit")
    fin = _f("news_article", category=CAT_REPUTATION, finding_summary="Acme announces major layoffs")
    ops = _f("news_article", category=CAT_REPUTATION, finding_summary="Acme suffers nationwide outage")
    generic = _f("news_article", category=CAT_REPUTATION, finding_summary="Acme sponsors a conference")
    assert rel.classify_risk_category(legal) == rel.RISK_LEGAL
    assert rel.classify_risk_category(fin) == rel.RISK_FINANCIAL
    assert rel.classify_risk_category(ops) == rel.RISK_OPERATIONAL
    assert rel.classify_risk_category(generic) == rel.RISK_REPUTATION


# ── scoring ────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_high_signal_scores_high():
    score, _ = rel.score_relevance(_f("known_exploited_vulnerability", severity=SEV_CRITICAL))
    assert score >= 90


@pytest.mark.unit
def test_generic_dns_scores_low():
    score, _ = rel.score_relevance(_f("dns_mx", category=CAT_DOMAIN, severity=SEV_INFO))
    assert score < 35  # below default threshold -> would be suppressed


@pytest.mark.unit
def test_news_vendor_mentioned_and_risk_keyword_scores_high():
    f = _f("news_article", category=CAT_REPUTATION, finding_summary="Acme Corp discloses data breach affecting users")
    score, reasons = rel.score_relevance(f, vendor_name="Acme Corp", vendor_domain="acme.com")
    assert score >= 60
    assert any("vendor explicitly mentioned" in r for r in reasons)


@pytest.mark.unit
def test_news_sector_only_scores_low():
    # Vendor not named -> sector/keyword-only -> suppressed.
    f = _f("news_article", category=CAT_REPUTATION, finding_summary="The fintech industry faces new breach risks")
    score, _ = rel.score_relevance(f, vendor_name="Acme Corp", vendor_domain="acme.com")
    assert score < 35


# ── annotate_and_filter ────────────────────────────────────────────────────────

@pytest.mark.unit
def test_annotate_filters_noise_and_annotates():
    findings = [
        _f("known_exploited_vulnerability", severity=SEV_CRITICAL, finding_summary="CVE on host"),
        _f("dns_mx", category=CAT_DOMAIN, severity=SEV_INFO, finding_summary="2 MX records"),
        _f("news_article", category=CAT_REPUTATION, finding_summary="Generic industry trends piece"),
    ]
    out = rel.annotate_and_filter(findings, vendor_name="Acme", vendor_domain="acme.com", threshold=35)
    summaries = [o["finding_summary"] for o in out]
    assert "CVE on host" in summaries
    assert "2 MX records" not in summaries  # generic DNS suppressed
    assert all("relevance" in o and "risk_category" in o for o in out)
    # sorted by relevance desc
    assert out == sorted(out, key=lambda x: x["relevance"], reverse=True)


@pytest.mark.unit
def test_annotate_dedups():
    findings = [
        _f("breach_disclosure", category=CAT_BREACH, severity=SEV_HIGH, finding_summary="Breach 2020"),
        _f("breach_disclosure", category=CAT_BREACH, severity=SEV_HIGH, finding_summary="Breach 2020"),
    ]
    out = rel.annotate_and_filter(findings, threshold=35)
    assert len(out) == 1


# ── executive summary (report_inputs) ──────────────────────────────────────────

def _snap(findings):
    return build_snapshot(scan_id="s", assessment_id="a", vendor_name="Acme",
                          vendor_domain="acme.com", findings=findings)


@pytest.mark.unit
def test_overall_rating_uses_moderate():
    # medium-only -> "Moderate" (not "Medium")
    ctx = ri.build_analysis_context(_snap([_f("security_headers", severity=SEV_MEDIUM, risk_indicators=["missing_header_CSP"])]))
    assert ctx["overall_vendor_rating"] in ("Low", "Moderate", "High")
    assert "Medium" not in ctx["overall_vendor_rating"]


@pytest.mark.unit
def test_positive_and_negative_signals():
    findings = [
        _f("spf", category=CAT_DOMAIN, severity=SEV_INFO, finding_summary="SPF present"),
        _f("dmarc", category=CAT_DOMAIN, severity=SEV_INFO, finding_summary="DMARC p=reject"),
        _f("certification_mention", category="compliance", severity=SEV_INFO,
           finding_summary="SOC 2", supporting_details={"certifications": ["SOC 2", "ISO 27001"]}),
        _f("breach_disclosure", category=CAT_BREACH, severity=SEV_HIGH,
           finding_summary="Public breach", risk_indicators=["public_breach_disclosed"]),
    ]
    ctx = ri.build_analysis_context(_snap(findings))
    assert any("SPF" in p for p in ctx["positive_signals"])
    assert any("DMARC" in p for p in ctx["positive_signals"])
    assert any("SOC 2" in p for p in ctx["positive_signals"])
    assert any(n["severity"] == SEV_HIGH for n in ctx["negative_signals"])
    assert ctx["key_risk_drivers"] and ctx["key_risk_drivers"][0]["severity"] == SEV_HIGH


@pytest.mark.unit
def test_risk_categories_present():
    ctx = ri.build_analysis_context(_snap([_f("breach_disclosure", category=CAT_BREACH, severity=SEV_HIGH)]))
    cats = {c["category"]: c["count"] for c in ctx["risk_categories"]}
    assert cats[rel.RISK_SECURITY] >= 1
    assert set(cats) == set(rel.RISK_CATEGORIES)


@pytest.mark.unit
def test_markdown_has_exec_sections():
    findings = [
        _f("spf", category=CAT_DOMAIN, severity=SEV_INFO, finding_summary="SPF present"),
        _f("breach_disclosure", category=CAT_BREACH, severity=SEV_HIGH, finding_summary="Public breach", source_url="http://b"),
    ]
    md = ri.render_context_markdown(ri.build_analysis_context(_snap(findings)))
    assert "Key Risk Drivers" in md and "Positive Signals" in md and "Negative Signals" in md
