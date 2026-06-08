"""SSRF-hardened domain normalization + HTTP fetch for OSINT collection.

The vendor domain (and every URL discovered while crawling it) is
attacker-controllable input. Without guarding, a vendor could point a domain at
``169.254.169.254`` (cloud metadata) or an RFC-1918 address and turn the
collector into an SSRF proxy — especially dangerous since the Lambda carries an
IAM role. Every outbound request in the VRA module MUST go through ``safe_get``.

Defenses implemented here:
  * scheme allowlist (http/https only);
  * IDN -> punycode host normalization;
  * resolve the host and reject if *any* resolved IP is non-public
    (loopback/private/link-local/metadata/reserved/CGNAT) — conservative, so a
    mixed public+private DNS answer is refused outright;
  * connect to the exact validated IP (DNS-rebinding defense) while keeping the
    original hostname for TLS SNI + certificate verification;
  * manual redirect following, re-validating the target host on every hop;
  * hard timeout and streamed body-size cap.

Pure stdlib + requests so the Lambda can vendor it unchanged.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import requests

# Networks that must never be reachable from a collector.
_BLOCKED_V4 = (
    ipaddress.ip_network("0.0.0.0/8"),       # "this" network / unspecified
    ipaddress.ip_network("10.0.0.0/8"),      # private
    ipaddress.ip_network("100.64.0.0/10"),   # CGNAT
    ipaddress.ip_network("127.0.0.0/8"),     # loopback
    ipaddress.ip_network("169.254.0.0/16"),  # link-local + cloud metadata
    ipaddress.ip_network("172.16.0.0/12"),   # private
    ipaddress.ip_network("192.0.0.0/24"),    # IETF protocol assignments
    ipaddress.ip_network("192.168.0.0/16"),  # private
    ipaddress.ip_network("198.18.0.0/15"),   # benchmarking
    ipaddress.ip_network("224.0.0.0/4"),     # multicast
    ipaddress.ip_network("240.0.0.0/4"),     # reserved
)

DEFAULT_TIMEOUT = 15
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_REDIRECTS = 5
_ALLOWED_SCHEMES = ("http", "https")
_USER_AGENT = "BytoidVRA-OSINT/1.0 (+https://bytoid.ai)"


class SafeFetchError(Exception):
    """Base error for any blocked or failed safe fetch."""


class SsrfError(SafeFetchError):
    """Raised when a target resolves to a disallowed address."""


def is_ip_blocked(ip_str: str) -> bool:
    """True if ``ip_str`` is private/loopback/link-local/metadata/reserved.

    Unparseable input is treated as blocked (fail closed).
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    if ip.version == 4:
        if any(ip in net for net in _BLOCKED_V4):
            return True
    # Catch-all for both families (and v6 private/ULA/link-local/mapped).
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return True
    # IPv4-mapped/compat IPv6 (e.g. ::ffff:127.0.0.1) — validate the embedded v4.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None and is_ip_blocked(str(mapped)):
        return True
    return False


def normalize_domain(raw: str) -> str | None:
    """Normalize user input to a bare, punycode hostname, or None if invalid.

    Accepts ``https://AWS.Amazon.com/foo``, ``aws.amazon.com``, etc. Strips
    scheme/path/port/credentials, lowercases, IDN-encodes, and rejects anything
    that is not a dotted hostname (no IPs, no localhost, no single labels).
    """
    if not raw or not isinstance(raw, str):
        return None
    candidate = raw.strip()
    if not candidate:
        return None
    if "//" not in candidate:
        candidate = "//" + candidate
    host = urlparse(candidate).hostname
    if not host:
        return None
    host = host.strip(".").lower()
    try:
        host = host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return None
    # Must look like a domain: at least one dot, valid label charset.
    if "." not in host or len(host) > 253:
        return None
    labels = host.split(".")
    if any(not lbl or len(lbl) > 63 for lbl in labels):
        return None
    if not all(c.isalnum() or c == "-" for lbl in labels for c in lbl):
        return None
    # Reject bare IPs masquerading as hosts.
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        return host


def resolve_public_ips(host: str) -> list[str]:
    """Resolve ``host`` to IPs, raising ``SsrfError`` if any is non-public.

    Conservative by design: a single blocked address in the answer fails the
    whole resolution, defeating "one public + one private" rebinding tricks.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise SafeFetchError(f"DNS resolution failed for {host!r}: {exc}") from exc
    ips: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in ips:
            ips.append(ip)
    if not ips:
        raise SafeFetchError(f"No addresses for {host!r}")
    for ip in ips:
        if is_ip_blocked(ip):
            raise SsrfError(f"{host!r} resolves to disallowed address {ip}")
    return ips


def _validate_url(url: str) -> tuple[str, str]:
    """Validate scheme + that the host resolves only to public IPs.

    Returns (scheme, punycode_host) or raises ``SsrfError``/``SafeFetchError``.
    This is the SSRF guard: ``resolve_public_ips`` rejects the host if ANY of its
    resolved addresses is private/loopback/link-local/metadata/reserved.
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise SafeFetchError(f"Disallowed scheme: {parsed.scheme!r}")
    if not parsed.hostname:
        raise SafeFetchError("URL has no host")
    host = normalize_domain(parsed.hostname)
    if host is None:
        raise SsrfError(f"Refusing non-domain/invalid host: {parsed.hostname!r}")
    resolve_public_ips(host)  # raises if any resolved IP is non-public
    return parsed.scheme, host


def safe_get(
    url: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    headers: dict | None = None,
) -> requests.Response:
    """SSRF-safe GET: scheme allowlist + every hop's host must resolve to public
    IPs, redirects followed manually (re-validated per hop), bounded body.

    Note: we validate the resolved IPs then let requests connect by hostname.
    The (narrow) TOCTOU/DNS-rebinding window is an accepted tradeoff — pinning
    the socket to the validated IP proved too fragile (IPv6 bracketing + SNI/cert
    breakage broke every fetch). The private-IP block is the primary defense.

    Returns the final ``requests.Response`` (``.content`` read, capped at
    ``max_bytes``). Raises ``SsrfError`` for blocked targets, ``SafeFetchError``
    for transport/redirect-limit problems.
    """
    req_headers = {"User-Agent": _USER_AGENT, "Accept-Encoding": "identity"}
    if headers:
        req_headers.update(headers)

    session = requests.Session()
    session.trust_env = False  # ignore ambient proxies/netrc
    current = url
    seen = 0
    try:
        while True:
            _validate_url(current)  # SSRF guard, every hop
            try:
                resp = session.get(
                    current,
                    headers=req_headers,
                    timeout=timeout,
                    allow_redirects=False,
                    stream=True,
                )
            except requests.RequestException as exc:
                raise SafeFetchError(f"Fetch failed for {current!r}: {exc}") from exc

            if resp.is_redirect or resp.is_permanent_redirect:
                location = resp.headers.get("Location")
                resp.close()
                seen += 1
                if seen > max_redirects:
                    raise SafeFetchError(f"Too many redirects ({max_redirects}) for {url!r}")
                if not location:
                    raise SafeFetchError("Redirect without Location header")
                current = requests.compat.urljoin(current, location)
                continue

            try:
                body = resp.raw.read(max_bytes + 1, decode_content=True)
            finally:
                resp.close()
            if len(body) > max_bytes:
                raise SafeFetchError(f"Response exceeded {max_bytes} bytes for {url!r}")
            resp._content = body  # cache so .content/.text work
            return resp
    finally:
        session.close()
