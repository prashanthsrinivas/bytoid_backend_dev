"""Domain Intelligence — DNS, SPF, DKIM, DMARC, MX via dnspython.

Email-auth posture is a strong, free vendor-hygiene signal: a missing SPF or a
DMARC policy of ``p=none`` materially raises spoofing risk. All lookups are
read-only DNS queries (no connection to the vendor), so no SSRF surface.
"""

from __future__ import annotations

from vra.osint.collectors.base import BaseCollector, CollectorContext
from vra.osint.normalize import make_finding
from vra.schema import CAT_DOMAIN, SEV_HIGH, SEV_INFO, SEV_LOW, SEV_MEDIUM


def _resolve_txt(name: str) -> list[str]:
    import dns.resolver  # lazy import: keep Lambda cold-start lean

    out = []
    try:
        for rdata in dns.resolver.resolve(name, "TXT", lifetime=10):
            parts = getattr(rdata, "strings", None) or [str(rdata)]
            out.append("".join(p.decode() if isinstance(p, bytes) else p for p in parts))
    except Exception:
        return []
    return out


def _resolve(name: str, rtype: str) -> list[str]:
    import dns.resolver  # lazy import: keep Lambda cold-start lean

    try:
        return [str(r) for r in dns.resolver.resolve(name, rtype, lifetime=10)]
    except Exception:
        return []


class DnsIntel(BaseCollector):
    name = "dns_intel"
    category = CAT_DOMAIN

    def collect(self, ctx: CollectorContext) -> list[dict]:
        domain = ctx.vendor_domain
        if not domain:
            return []
        findings: list[dict] = []

        # MX / NS presence.
        mx = _resolve(domain, "MX")
        ns = _resolve(domain, "NS")
        if mx:
            findings.append(
                make_finding(
                    category=CAT_DOMAIN,
                    evidence_type="dns_mx",
                    source="DNS",
                    finding_summary=f"{len(mx)} MX record(s) configured",
                    supporting_details={"mx": mx},
                    severity=SEV_INFO,
                )
            )
        if ns:
            findings.append(
                make_finding(
                    category=CAT_DOMAIN,
                    evidence_type="dns_ns",
                    source="DNS",
                    finding_summary=f"{len(ns)} nameserver(s)",
                    supporting_details={"ns": ns},
                    severity=SEV_INFO,
                )
            )

        # SPF.
        txt = _resolve_txt(domain)
        spf = [t for t in txt if t.lower().startswith("v=spf1")]
        if spf:
            hard_fail = any("-all" in s for s in spf)
            findings.append(
                make_finding(
                    category=CAT_DOMAIN,
                    evidence_type="spf",
                    source="DNS",
                    finding_summary="SPF record present" + ("" if hard_fail else " (no hard fail)"),
                    supporting_details={"spf": spf},
                    risk_indicators=[] if hard_fail else ["spf_soft_or_neutral"],
                    severity=SEV_INFO if hard_fail else SEV_LOW,
                )
            )
        else:
            findings.append(
                make_finding(
                    category=CAT_DOMAIN,
                    evidence_type="spf",
                    source="DNS",
                    finding_summary="No SPF record — domain is spoofable",
                    risk_indicators=["spf_missing"],
                    severity=SEV_MEDIUM,
                )
            )

        # DMARC.
        dmarc_txt = _resolve_txt(f"_dmarc.{domain}")
        dmarc = [t for t in dmarc_txt if t.lower().startswith("v=dmarc1")]
        if dmarc:
            policy = "none"
            for d in dmarc:
                for part in d.split(";"):
                    if part.strip().lower().startswith("p="):
                        policy = part.split("=", 1)[1].strip().lower()
            weak = policy == "none"
            findings.append(
                make_finding(
                    category=CAT_DOMAIN,
                    evidence_type="dmarc",
                    source="DNS",
                    finding_summary=f"DMARC policy p={policy}",
                    supporting_details={"dmarc": dmarc, "policy": policy},
                    risk_indicators=["dmarc_policy_none"] if weak else [],
                    severity=SEV_LOW if weak else SEV_INFO,
                )
            )
        else:
            findings.append(
                make_finding(
                    category=CAT_DOMAIN,
                    evidence_type="dmarc",
                    source="DNS",
                    finding_summary="No DMARC record — no spoofing protection policy",
                    risk_indicators=["dmarc_missing"],
                    severity=SEV_HIGH,
                )
            )

        # DKIM (common selectors — best-effort presence check).
        dkim_present = any(
            _resolve_txt(f"{sel}._domainkey.{domain}")
            for sel in ("default", "google", "selector1", "selector2", "k1")
        )
        findings.append(
            make_finding(
                category=CAT_DOMAIN,
                evidence_type="dkim",
                source="DNS",
                finding_summary="DKIM selector found" if dkim_present else "No DKIM on common selectors",
                risk_indicators=[] if dkim_present else ["dkim_not_found_common_selectors"],
                severity=SEV_INFO if dkim_present else SEV_LOW,
            )
        )
        return findings
