"""Breach Intelligence — HIBP free, unauthenticated breaches dataset.

Uses ``GET /api/v3/breaches?domain=<domain>`` which is free and keyless (unlike
the paid ``breachedaccount`` endpoint). Returns domain-level breach disclosures
with metadata only — no individual accounts/PII are queried or stored.
"""

from __future__ import annotations

import json

from vra.osint.collectors.base import BaseCollector, CollectorContext
from vra.osint.normalize import make_finding
from vra.schema import CAT_BREACH, SEV_HIGH, SEV_MEDIUM

_HIBP_URL = "https://haveibeenpwned.com/api/v3/breaches?domain={domain}"


class BreachIntel(BaseCollector):
    name = "breach_intel"
    category = CAT_BREACH

    def collect(self, ctx: CollectorContext) -> list[dict]:
        if not ctx.vendor_domain:
            return []
        try:
            resp = ctx.fetch(_HIBP_URL.format(domain=ctx.vendor_domain))
            breaches = json.loads(resp.text)
        except Exception:
            return []
        if not isinstance(breaches, list) or not breaches:
            return []

        findings = []
        for b in breaches:
            if not isinstance(b, dict):
                continue
            pwn = b.get("PwnCount") or 0
            sev = SEV_HIGH if pwn and pwn >= 1_000_000 else SEV_MEDIUM
            findings.append(
                make_finding(
                    category=CAT_BREACH,
                    evidence_type="breach_disclosure",
                    source="HIBP",
                    source_url=f"https://haveibeenpwned.com/PwnedWebsites#{b.get('Name', '')}",
                    finding_summary=(
                        f"{b.get('Title') or b.get('Name')} breach "
                        f"({b.get('BreachDate', 'unknown date')}, ~{pwn:,} accounts)"
                    ),
                    supporting_details={
                        "name": b.get("Name"),
                        "breach_date": b.get("BreachDate"),
                        "pwn_count": pwn,
                        "data_classes": b.get("DataClasses") or [],
                        "is_verified": b.get("IsVerified"),
                    },
                    risk_indicators=["public_breach_disclosed"],
                    severity=sev,
                )
            )
        return findings
