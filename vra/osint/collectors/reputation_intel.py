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
from vra.schema import CAT_REPUTATION, SEV_INFO, SEV_LOW, SEV_MEDIUM

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

        findings: list[dict] = []
        items = root.findall(".//item")[:_MAX_ITEMS]
        negative_hits = 0
        for item in items:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            if not title:
                continue
            low = title.lower()
            negative = any(term in low for term in _NEGATIVE)
            if negative:
                negative_hits += 1
            findings.append(
                make_finding(
                    category=CAT_REPUTATION,
                    evidence_type="news_article",
                    source="Google News",
                    source_url=link,
                    finding_summary=title,
                    supporting_details={"published": pub},
                    risk_indicators=["negative_press"] if negative else [],
                    severity=SEV_MEDIUM if negative else SEV_INFO,
                )
            )

        if findings:
            findings.append(
                make_finding(
                    category=CAT_REPUTATION,
                    evidence_type="reputation_summary",
                    source="Google News",
                    finding_summary=(
                        f"{len(findings)} recent news items, {negative_hits} security-negative"
                    ),
                    supporting_details={"total": len(findings), "negative": negative_hits},
                    risk_indicators=["adverse_media"] if negative_hits else [],
                    severity=SEV_LOW if negative_hits else SEV_INFO,
                )
            )
        return findings
