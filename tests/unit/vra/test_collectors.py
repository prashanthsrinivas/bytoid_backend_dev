"""OSINT collector tests — all network mocked via an injected fetch."""

import json

import pytest

from vra.osint.collectors import run_collection
from vra.osint.collectors.base import BaseCollector, CollectorContext
from vra.osint.collectors.breach_intel import BreachIntel
from vra.osint.collectors.compliance_intel import ComplianceIntel
from vra.osint.collectors.reputation_intel import ReputationIntel
from vra.osint.collectors.security_intel import SecurityIntel
from vra.osint.collectors.vuln_intel import VulnIntel
from vra.schema import CAT_BREACH, SEV_CRITICAL


class FakeResp:
    def __init__(self, text="", headers=None, status_code=200):
        self.text = text
        self.headers = headers or {}
        self.status_code = status_code


def _ctx(domain="acme.com", ips=None, fetch=None, name="Acme"):
    return CollectorContext(name, domain, ips or ["8.8.8.8"], fetch=fetch)


# ── security_intel ───────────────────────────────────────────────────────────

@pytest.mark.unit
def test_security_intel_shodan_and_headers():
    def fetch(url):
        if "internetdb" in url:
            return FakeResp(json.dumps({"ports": [443, 22], "vulns": ["CVE-2021-1"], "cpes": [], "tags": [], "hostnames": []}))
        if "crt.sh" in url:
            return FakeResp(json.dumps([{"name_value": "acme.com\nwww.acme.com"}]))
        return FakeResp(headers={"Strict-Transport-Security": "max-age=1"})

    ctx = _ctx(fetch=fetch)
    findings = SecurityIntel().collect(ctx)
    types = {f["evidence_type"] for f in findings}
    assert {"infrastructure_exposure", "certificate_transparency", "security_headers"} <= types
    assert ctx.shared["cves"] == ["CVE-2021-1"]  # passed to vuln collector
    # missing CSP etc. should be flagged
    hdr = next(f for f in findings if f["evidence_type"] == "security_headers")
    assert any(r.startswith("missing_header_") for r in hdr["risk_indicators"])


# ── vuln_intel ───────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_vuln_intel_kev_enrichment():
    def fetch(url):
        return FakeResp(json.dumps({"vulnerabilities": [{"cveID": "CVE-2021-1", "vulnerabilityName": "x"}]}))

    ctx = _ctx(fetch=fetch)
    ctx.shared["cves"] = ["CVE-2021-1", "CVE-2099-9"]
    findings = VulnIntel().collect(ctx)
    kev = [f for f in findings if f["evidence_type"] == "known_exploited_vulnerability"]
    assert len(kev) == 1 and kev[0]["severity"] == SEV_CRITICAL
    summary = [f for f in findings if f["evidence_type"] == "public_cve"]
    assert summary and summary[0]["supporting_details"]["cves"] == ["CVE-2099-9"]


@pytest.mark.unit
def test_vuln_intel_no_cves_noop():
    assert VulnIntel().collect(_ctx(fetch=lambda u: FakeResp("{}"))) == []


# ── breach_intel ─────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_breach_intel_parses_hibp():
    def fetch(url):
        return FakeResp(json.dumps([{"Name": "Acme", "Title": "Acme", "BreachDate": "2020-01-01", "PwnCount": 2_000_000, "DataClasses": ["Emails"]}]))

    findings = BreachIntel().collect(_ctx(fetch=fetch))
    assert len(findings) == 1
    assert findings[0]["category"] == CAT_BREACH
    assert "public_breach_disclosed" in findings[0]["risk_indicators"]


@pytest.mark.unit
def test_breach_intel_empty():
    assert BreachIntel().collect(_ctx(fetch=lambda u: FakeResp("[]"))) == []


# ── reputation_intel ─────────────────────────────────────────────────────────

@pytest.mark.unit
def test_reputation_intel_flags_negative():
    rss = """<rss><channel>
      <item><title>Acme suffers data breach</title><link>http://x/1</link><pubDate>now</pubDate></item>
      <item><title>Acme launches product</title><link>http://x/2</link><pubDate>now</pubDate></item>
    </channel></rss>"""
    findings = ReputationIntel().collect(_ctx(fetch=lambda u: FakeResp(rss)))
    negative = [f for f in findings if "negative_press" in f["risk_indicators"]]
    assert len(negative) == 1
    assert any(f["evidence_type"] == "reputation_summary" for f in findings)


# ── compliance_intel ─────────────────────────────────────────────────────────

@pytest.mark.unit
def test_compliance_intel_detects_certs_and_security_txt():
    def fetch(url):
        if "security.txt" in url:
            return FakeResp("Contact: mailto:s@acme.com", status_code=200)
        return FakeResp("<html>We are SOC 2 and ISO 27001 certified. Visit our Trust Center.</html>")

    findings = ComplianceIntel().collect(_ctx(fetch=fetch))
    types = {f["evidence_type"] for f in findings}
    assert "security_txt" in types and "certification_mention" in types and "trust_center" in types
    cert = next(f for f in findings if f["evidence_type"] == "certification_mention")
    assert "SOC 2" in cert["supporting_details"]["certifications"]


# ── run_collection orchestrator ──────────────────────────────────────────────

class _Good(BaseCollector):
    name = "good"
    def collect(self, ctx):
        from vra.osint.normalize import make_finding
        return [make_finding(category="domain", evidence_type="t", source="s", finding_summary="x")]


class _Bad(BaseCollector):
    name = "bad"
    def collect(self, ctx):
        raise RuntimeError("boom")


@pytest.mark.unit
def test_run_collection_isolates_failures():
    snap = run_collection(
        scan_id="s1", assessment_id="a1", vendor_name="Acme", vendor_domain="acme.com",
        collectors=[_Good(), _Bad()], resolver=lambda d: ["8.8.8.8"],
    )
    assert snap["counts"]["total"] == 1
    assert snap["collector_status"]["good"].startswith("ok")
    assert snap["collector_status"]["bad"].startswith("error")


@pytest.mark.unit
def test_run_collection_ssrf_resolution_failure_recorded():
    from vra.osint.safe_fetch import SsrfError

    def bad_resolver(domain):
        raise SsrfError("private ip")

    snap = run_collection(
        scan_id="s1", assessment_id="a1", vendor_name="Acme", vendor_domain="acme.com",
        collectors=[_Good()], resolver=bad_resolver,
    )
    assert "error" in snap["collector_status"]["_resolve"]
    # collectors still run (with empty ips)
    assert snap["collector_status"]["good"].startswith("ok")


@pytest.mark.unit
def test_run_collection_invalid_domain():
    snap = run_collection(
        scan_id="s1", assessment_id="a1", vendor_name="Acme", vendor_domain="not a domain",
        collectors=[_Good()], resolver=lambda d: ["8.8.8.8"],
    )
    assert snap["collector_status"]["_resolve"] == "error: no valid domain"
    assert snap["vendor_domain"] == ""
