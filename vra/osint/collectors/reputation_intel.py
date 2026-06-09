"""Reputation Intelligence — public news via Google News RSS (free, keyless).

Collects raw, security-relevant news items as findings. No LLM summarization
happens here — that is deferred to the app-side AI risk analysis (credit-gated).
Items whose headline mentions breach/lawsuit/incident terms are flagged.
"""

from __future__ import annotations

from urllib.parse import quote
from xml.etree import ElementTree as ET

from vra.osint.collectors.base import BaseCollector, CollectorContext
from vra.osint.normalize import make_finding
from vra.schema import CAT_REPUTATION, SEV_LOW, SEV_MEDIUM

_RSS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
_MAX_ITEMS = 15
_NEGATIVE = (
    "breach", "hack", "leak", "lawsuit", "fine", "ransomware",
    "incident", "outage", "vulnerability", "exposed", "settlement",
)


class ReputationIntel(BaseCollector):
    name = "reputation_intel"
    category = CAT_REPUTATION

    def collect(self, ctx: CollectorContext) -> list[dict]:
        query = ctx.vendor_name or ctx.vendor_domain
        if not query:
            return []
        q = quote(f'"{query}" (security OR breach OR data OR privacy OR lawsuit)')
        try:
            resp = ctx.fetch(_RSS.format(q=q))
            # Source is news.google.com over HTTPS (not vendor-controlled) and the
            # body is size-capped by safe_fetch; stdlib ET does not resolve
            # external entities, so XML-bomb/XXE surface is acceptable here.
            root = ET.fromstring(resp.text)  # noqa: S314
        except Exception:
            return []

        # Distinctive vendor token to require in the headline — drops sector/
        # industry pieces that merely match a keyword but don't name the vendor.
        token = (ctx.vendor_name or ctx.vendor_domain or "").strip().lower().split(" ")[0]

        findings: list[dict] = []
        for item in root.findall(".//item")[: _MAX_ITEMS * 3]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            if not title:
                continue
            low = title.lower()
            # Gate 1: must specifically name the vendor.
            if token and token not in low:
                continue
            # Gate 2: must carry a material risk signal — vendor-specific but
            # neutral/marketing news is excluded as noise.
            if not any(term in low for term in _NEGATIVE):
                continue
            findings.append(
                make_finding(
                    category=CAT_REPUTATION,
                    evidence_type="news_article",
                    source="Google News",
                    source_url=link,
                    finding_summary=title,
                    supporting_details={"published": pub},
                    risk_indicators=["negative_press"],
                    severity=SEV_MEDIUM,
                )
            )
            if len(findings) >= _MAX_ITEMS:
                break

        if findings:
            findings.append(
                make_finding(
                    category=CAT_REPUTATION,
                    evidence_type="reputation_summary",
                    source="Google News",
                    finding_summary=f"{len(findings)} vendor-specific adverse-media item(s)",
                    supporting_details={"adverse_items": len(findings)},
                    risk_indicators=["adverse_media"],
                    severity=SEV_LOW,
                )
            )
        return findings
