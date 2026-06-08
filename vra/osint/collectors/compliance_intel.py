"""Compliance Intelligence — security.txt + trust-center / certification signals.

Free, scrape-based signals of a vendor's published security posture:
  * ``/.well-known/security.txt`` (RFC 9116) presence + contents;
  * homepage scan for compliance/certification mentions (SOC 2, ISO 27001,
    GDPR, HIPAA, PCI DSS) and a linked trust center.

Keyword detection only — no LLM. Deeper interpretation is left to the credit-
gated AI risk analysis.
"""

from __future__ import annotations

import re

from vra.osint.collectors.base import BaseCollector, CollectorContext
from vra.osint.normalize import make_finding
from vra.schema import CAT_COMPLIANCE, SEV_INFO, SEV_LOW

_CERT_PATTERNS = {
    "SOC 2": r"soc\s*2",
    "SOC 1": r"soc\s*1",
    "ISO 27001": r"iso[\s/-]*27001",
    "ISO 27701": r"iso[\s/-]*27701",
    "GDPR": r"\bgdpr\b",
    "HIPAA": r"\bhipaa\b",
    "PCI DSS": r"pci[\s-]*dss",
    "FedRAMP": r"\bfedramp\b",
}
_TRUST_CENTER = re.compile(r"trust\s*center|security\s*portal|compliance\s*center", re.I)


class ComplianceIntel(BaseCollector):
    name = "compliance_intel"
    category = CAT_COMPLIANCE

    def collect(self, ctx: CollectorContext) -> list[dict]:
        domain = ctx.vendor_domain
        if not domain:
            return []
        findings: list[dict] = []

        # security.txt
        try:
            resp = ctx.fetch(f"https://{domain}/.well-known/security.txt")
            ok = 200 <= getattr(resp, "status_code", 0) < 300 and resp.text.strip()
        except Exception:
            ok = False
        findings.append(
            make_finding(
                category=CAT_COMPLIANCE,
                evidence_type="security_txt",
                source="security.txt",
                source_url=f"https://{domain}/.well-known/security.txt",
                finding_summary="Published security.txt" if ok else "No security.txt (RFC 9116)",
                risk_indicators=[] if ok else ["no_security_txt"],
                severity=SEV_INFO if ok else SEV_LOW,
            )
        )

        # Homepage compliance/certification mentions + trust center.
        try:
            html = ctx.fetch(f"https://{domain}").text or ""
        except Exception:
            html = ""
        if html:
            found = [name for name, pat in _CERT_PATTERNS.items() if re.search(pat, html, re.I)]
            if found:
                findings.append(
                    make_finding(
                        category=CAT_COMPLIANCE,
                        evidence_type="certification_mention",
                        source="Vendor website",
                        source_url=f"https://{domain}",
                        finding_summary=f"Compliance/certification mentions: {', '.join(found)}",
                        supporting_details={"certifications": found},
                        severity=SEV_INFO,
                    )
                )
            if _TRUST_CENTER.search(html):
                findings.append(
                    make_finding(
                        category=CAT_COMPLIANCE,
                        evidence_type="trust_center",
                        source="Vendor website",
                        source_url=f"https://{domain}",
                        finding_summary="Trust center / security portal referenced on site",
                        severity=SEV_INFO,
                    )
                )
        return findings
