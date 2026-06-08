"""Vulnerability Intelligence — enrich discovered CVEs against CISA KEV.

CVEs surfaced by Shodan InternetDB (``ctx.shared['cves']``) are cross-referenced
against the free CISA **Known Exploited Vulnerabilities** catalog. A CVE that is
actively exploited in the wild is a critical signal, so those are emitted as
individual critical findings; the remainder are summarized.
"""

from __future__ import annotations

import json

from vra.osint.collectors.base import BaseCollector, CollectorContext
from vra.osint.normalize import make_finding
from vra.schema import CAT_VULNERABILITY, SEV_CRITICAL, SEV_HIGH, SEV_INFO

_KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"


def _load_kev(fetch) -> dict:
    """Return {cveID: entry} from the CISA KEV catalog, or {} on failure."""
    try:
        data = json.loads(fetch(_KEV_URL).text)
    except Exception:
        return {}
    out = {}
    for v in (data.get("vulnerabilities") or []):
        cid = (v.get("cveID") or "").upper()
        if cid:
            out[cid] = v
    return out


class VulnIntel(BaseCollector):
    name = "vuln_intel"
    category = CAT_VULNERABILITY

    def collect(self, ctx: CollectorContext) -> list[dict]:
        cves = [c.upper() for c in (ctx.shared.get("cves") or [])]
        if not cves:
            return []

        kev = _load_kev(ctx.fetch)
        findings: list[dict] = []
        exploited = []

        for cve in cves:
            entry = kev.get(cve)
            if entry:
                exploited.append(cve)
                findings.append(
                    make_finding(
                        category=CAT_VULNERABILITY,
                        evidence_type="known_exploited_vulnerability",
                        source="CISA KEV",
                        source_url=f"https://nvd.nist.gov/vuln/detail/{cve}",
                        finding_summary=f"{cve} is a CISA Known Exploited Vulnerability",
                        supporting_details={
                            "cve": cve,
                            "name": entry.get("vulnerabilityName"),
                            "description": entry.get("shortDescription"),
                            "due_date": entry.get("dueDate"),
                        },
                        risk_indicators=["known_exploited", "actively_exploited"],
                        severity=SEV_CRITICAL,
                    )
                )

        remaining = [c for c in cves if c not in exploited]
        if remaining:
            findings.append(
                make_finding(
                    category=CAT_VULNERABILITY,
                    evidence_type="public_cve",
                    source="Shodan InternetDB / NVD",
                    finding_summary=f"{len(remaining)} public CVE(s) on vendor infrastructure (not in KEV)",
                    supporting_details={"cves": remaining},
                    risk_indicators=["public_cves"],
                    severity=SEV_HIGH if remaining else SEV_INFO,
                )
            )
        return findings
