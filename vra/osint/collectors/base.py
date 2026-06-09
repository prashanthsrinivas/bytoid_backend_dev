"""Collector framework + orchestrator.

``run_collection`` resolves the vendor domain to SSRF-validated public IPs once,
shares them via a ``CollectorContext``, then runs each collector with per-
collector error isolation (a failing collector is recorded in
``collector_status`` and never aborts the scan). The aggregated findings become
a normalized snapshot via ``vra.osint.normalize.build_snapshot``.

Dependency-light (stdlib + requests via safe_fetch) so it runs in the Lambda.
"""

from __future__ import annotations

from vra.osint import safe_fetch
from vra.osint.normalize import build_snapshot
from vra.osint.safe_fetch import SafeFetchError, normalize_domain


class CollectorContext:
    """Shared, read-only-ish state passed to every collector.

    ``ips`` is the SSRF-validated public IP list for the domain (may be empty if
    resolution failed). ``shared`` lets an earlier collector hand structured
    signal (e.g. discovered CVEs) to a later one without re-fetching.
    """

    def __init__(self, vendor_name: str, vendor_domain: str, ips: list[str], fetch=None):
        self.vendor_name = vendor_name
        self.vendor_domain = vendor_domain
        self.ips = ips or []
        self.fetch = fetch or safe_fetch.safe_get
        self.shared: dict = {}


class BaseCollector:
    """Subclass and implement ``collect``. ``name`` keys collector_status."""

    name = "base"
    category = "domain"

    def collect(self, ctx: CollectorContext) -> list[dict]:  # pragma: no cover
        raise NotImplementedError


def default_collectors() -> list[BaseCollector]:
    """The standard free/keyless collector set, in execution order."""
    # Imported here (not at module top) to avoid import cycles and keep the
    # Lambda cold-start lean when a subset is used.
    from vra.osint.collectors.breach_intel import BreachIntel
    from vra.osint.collectors.compliance_intel import ComplianceIntel
    from vra.osint.collectors.dns_intel import DnsIntel
    from vra.osint.collectors.reputation_intel import ReputationIntel
    from vra.osint.collectors.security_intel import SecurityIntel
    from vra.osint.collectors.vuln_intel import VulnIntel

    # SecurityIntel runs before VulnIntel so discovered CVEs (Shodan InternetDB)
    # are in ctx.shared for KEV enrichment.
    return [
        DnsIntel(),
        SecurityIntel(),
        VulnIntel(),
        BreachIntel(),
        ComplianceIntel(),
        ReputationIntel(),
    ]


def run_collection(
    *,
    scan_id: str,
    assessment_id: str,
    vendor_name: str,
    vendor_domain: str,
    collectors: list[BaseCollector] | None = None,
    resolver=None,
    fetch=None,
) -> dict:
    """Run all collectors and return a normalized snapshot dict.

    ``resolver``/``fetch`` are injectable for testing. Never raises: domain
    resolution failure or any collector error is captured in
    ``collector_status`` and the scan still returns a (possibly empty) snapshot.
    """
    collectors = collectors if collectors is not None else default_collectors()
    resolver = resolver or safe_fetch.resolve_public_ips
    domain = normalize_domain(vendor_domain) or ""

    status: dict = {}
    ips: list[str] = []
    if domain:
        try:
            ips = resolver(domain)
        except SafeFetchError as exc:
            status["_resolve"] = f"error: {exc}"
    else:
        status["_resolve"] = "error: no valid domain"

    ctx = CollectorContext(vendor_name, domain, ips, fetch=fetch)

    findings: list[dict] = []
    for collector in collectors:
        try:
            produced = collector.collect(ctx) or []
            findings.extend(produced)
            status[collector.name] = f"ok ({len(produced)})"
        except Exception as exc:  # isolate — one collector never breaks the scan
            status[collector.name] = f"error: {type(exc).__name__}: {exc}"

    # Risk-relevance pass: score + classify every finding, suppress noise below
    # the threshold, dedup. Favors fewer high-value findings over many generic
    # ones. Never raises — on any error we fall back to the raw findings.
    raw_count = len(findings)
    try:
        from vra import config as vra_config
        from vra.osint.relevance import annotate_and_filter

        findings = annotate_and_filter(
            findings,
            vendor_name=vendor_name,
            vendor_domain=domain,
            threshold=vra_config.VRA_RELEVANCE_THRESHOLD,
        )
        status["_relevance"] = (
            f"kept {len(findings)}/{raw_count} (threshold {vra_config.VRA_RELEVANCE_THRESHOLD})"
        )
    except Exception as exc:
        status["_relevance"] = f"error: {type(exc).__name__}: {exc}"

    return build_snapshot(
        scan_id=scan_id,
        assessment_id=assessment_id,
        vendor_name=vendor_name,
        vendor_domain=domain,
        findings=findings,
        collector_status=status,
    )
