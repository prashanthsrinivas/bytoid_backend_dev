"""Security Intelligence — Shodan InternetDB, certificate transparency, headers.

All sources are free/keyless:
  * Shodan **InternetDB** (``https://internetdb.shodan.io/{ip}``) — open ports,
    hostnames, tech CPEs, tags, and known CVEs per resolved IP. Discovered CVEs
    are stashed in ``ctx.shared['cves']`` for the vuln collector to enrich.
  * **crt.sh** certificate-transparency JSON — issued-cert / subdomain exposure.
  * HTTP **security headers** on the homepage (HSTS/CSP/etc.).
"""

from __future__ import annotations

import json

from vra.osint.collectors.base import BaseCollector, CollectorContext
from vra.osint.normalize import make_finding
from vra.schema import CAT_SECURITY, SEV_HIGH, SEV_INFO, SEV_LOW, SEV_MEDIUM

_SECURITY_HEADERS = {
    "strict-transport-security": "HSTS",
    "content-security-policy": "CSP",
    "x-frame-options": "X-Frame-Options",
    "x-content-type-options": "X-Content-Type-Options",
    "referrer-policy": "Referrer-Policy",
}


def _json(resp):
    try:
        return json.loads(resp.text)
    except Exception:
        return None


class SecurityIntel(BaseCollector):
    name = "security_intel"
    category = CAT_SECURITY

    def collect(self, ctx: CollectorContext) -> list[dict]:
        findings: list[dict] = []
        discovered_cves: list[str] = []

        # --- Shodan InternetDB per resolved IP -------------------------------
        for ip in ctx.ips:
            try:
                data = _json(ctx.fetch(f"https://internetdb.shodan.io/{ip}"))
            except Exception:  # noqa: S112 (per-IP best-effort; failures expected)
                continue
            if not isinstance(data, dict):
                continue
            ports = data.get("ports") or []
            vulns = data.get("vulns") or []
            cpes = data.get("cpes") or []
            discovered_cves.extend(vulns)
            sev = SEV_HIGH if vulns else (SEV_LOW if ports else SEV_INFO)
            findings.append(
                make_finding(
                    category=CAT_SECURITY,
                    evidence_type="infrastructure_exposure",
                    source="Shodan InternetDB",
                    source_url=f"https://internetdb.shodan.io/{ip}",
                    finding_summary=(
                        f"{ip}: {len(ports)} open port(s), {len(vulns)} known CVE(s)"
                    ),
                    supporting_details={
                        "ip": ip,
                        "ports": ports,
                        "cpes": cpes,
                        "vulns": vulns,
                        "tags": data.get("tags") or [],
                        "hostnames": data.get("hostnames") or [],
                    },
                    risk_indicators=(["public_cves_on_host"] if vulns else []),
                    severity=sev,
                )
            )
        if discovered_cves:
            ctx.shared["cves"] = sorted(set(discovered_cves))

        # --- Certificate transparency (crt.sh) -------------------------------
        if ctx.vendor_domain:
            try:
                certs = _json(ctx.fetch(f"https://crt.sh/?q={ctx.vendor_domain}&output=json"))
            except Exception:
                certs = None
            if isinstance(certs, list):
                names = {
                    n.strip().lower()
                    for c in certs
                    for n in str(c.get("name_value", "")).splitlines()
                    if n.strip()
                }
                findings.append(
                    make_finding(
                        category=CAT_SECURITY,
                        evidence_type="certificate_transparency",
                        source="crt.sh",
                        source_url=f"https://crt.sh/?q={ctx.vendor_domain}",
                        finding_summary=(
                            f"{len(certs)} CT log entries, {len(names)} unique names/subdomains"
                        ),
                        supporting_details={"unique_names": sorted(names)[:200], "entries": len(certs)},
                        severity=SEV_INFO,
                    )
                )

        # --- HTTP security headers -------------------------------------------
        if ctx.vendor_domain:
            try:
                resp = ctx.fetch(f"https://{ctx.vendor_domain}")
            except Exception:
                resp = None
            if resp is not None:
                present = {h for h in _SECURITY_HEADERS if h in {k.lower() for k in resp.headers}}
                missing = [_SECURITY_HEADERS[h] for h in _SECURITY_HEADERS if h not in present]
                sev = SEV_MEDIUM if "strict-transport-security" not in present else SEV_LOW
                findings.append(
                    make_finding(
                        category=CAT_SECURITY,
                        evidence_type="security_headers",
                        source="HTTP",
                        source_url=f"https://{ctx.vendor_domain}",
                        finding_summary=(
                            "All key security headers present"
                            if not missing
                            else f"Missing security headers: {', '.join(missing)}"
                        ),
                        supporting_details={"present": sorted(present), "missing": missing},
                        risk_indicators=[f"missing_header_{m}" for m in missing],
                        severity=SEV_INFO if not missing else sev,
                    )
                )
        return findings
