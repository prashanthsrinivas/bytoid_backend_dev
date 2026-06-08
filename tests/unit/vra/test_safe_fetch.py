"""SSRF guard tests — vra/osint/safe_fetch.py.

The vendor domain is attacker-controllable, so these assert that private /
loopback / link-local / metadata / reserved targets are refused, that domain
normalization is strict, and that DNS answers mixing public + private addresses
fail closed.
"""

import socket
from unittest.mock import patch

import pytest

from vra.osint import safe_fetch
from vra.osint.safe_fetch import (
    SafeFetchError,
    SsrfError,
    is_ip_blocked,
    normalize_domain,
    resolve_public_ips,
)


def _gai(*ips):
    """Build a socket.getaddrinfo-style return for the given IPv4/IPv6 strings."""
    out = []
    for ip in ips:
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        out.append((fam, socket.SOCK_STREAM, 6, "", (ip, 0)))
    return out


# ── is_ip_blocked ────────────────────────────────────────────────────────────

@pytest.mark.security
@pytest.mark.api_security
@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",        # loopback
        "10.0.0.5",         # private
        "172.16.9.9",       # private
        "192.168.1.1",      # private
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",       # CGNAT
        "0.0.0.0",          # unspecified
        "224.0.0.1",        # multicast
        "240.0.0.1",        # reserved
        "::1",              # v6 loopback
        "fe80::1",          # v6 link-local
        "fc00::1",          # v6 ULA / private
        "::ffff:127.0.0.1", # v4-mapped loopback
        "not-an-ip",        # unparseable -> blocked
    ],
)
def test_blocked_ips(ip):
    assert is_ip_blocked(ip) is True


@pytest.mark.security
@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"])
def test_public_ips_allowed(ip):
    assert is_ip_blocked(ip) is False


# ── normalize_domain ─────────────────────────────────────────────────────────

@pytest.mark.security
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("microsoft.com", "microsoft.com"),
        ("https://AWS.Amazon.com/foo?x=1", "aws.amazon.com"),
        ("  Sigmoid.com.  ", "sigmoid.com"),
        ("http://user:pass@example.com:8443/path", "example.com"),
    ],
)
def test_normalize_domain_valid(raw, expected):
    assert normalize_domain(raw) == expected


@pytest.mark.security
@pytest.mark.parametrize(
    "raw",
    [
        "", "   ", None,
        "localhost",          # single label
        "127.0.0.1",          # bare IP
        "::1",                # bare v6
        "no_dot",             # no dot
        "bad_label.com",      # underscore not allowed in label
        "a..b.com",           # empty label
    ],
)
def test_normalize_domain_invalid(raw):
    assert normalize_domain(raw) is None


# ── resolve_public_ips ───────────────────────────────────────────────────────

@pytest.mark.security
def test_resolve_all_public_ok():
    with patch.object(safe_fetch.socket, "getaddrinfo", return_value=_gai("8.8.8.8", "1.1.1.1")):
        assert resolve_public_ips("example.com") == ["8.8.8.8", "1.1.1.1"]


@pytest.mark.security
@pytest.mark.api_security
def test_resolve_any_private_fails_closed():
    # One public + one private must reject the WHOLE resolution (rebinding guard).
    with patch.object(safe_fetch.socket, "getaddrinfo", return_value=_gai("8.8.8.8", "10.0.0.1")):
        with pytest.raises(SsrfError):
            resolve_public_ips("example.com")


@pytest.mark.security
def test_resolve_metadata_ip_blocked():
    with patch.object(safe_fetch.socket, "getaddrinfo", return_value=_gai("169.254.169.254")):
        with pytest.raises(SsrfError):
            resolve_public_ips("metadata.evil")


@pytest.mark.security
def test_resolve_dns_failure_raises_safefetch():
    with patch.object(safe_fetch.socket, "getaddrinfo", side_effect=socket.gaierror("nope")):
        with pytest.raises(SafeFetchError):
            resolve_public_ips("nx.example")


# ── safe_get URL validation (no network) ─────────────────────────────────────

@pytest.mark.security
@pytest.mark.api_security
def test_safe_get_rejects_bad_scheme():
    with pytest.raises(SafeFetchError):
        safe_fetch.safe_get("ftp://example.com/x")
    with pytest.raises(SafeFetchError):
        safe_fetch.safe_get("file:///etc/passwd")


@pytest.mark.security
@pytest.mark.api_security
def test_safe_get_rejects_ip_literal_host():
    # Even before any socket call, a bare-IP host is not a valid domain.
    with pytest.raises(SsrfError):
        safe_fetch.safe_get("http://169.254.169.254/latest/meta-data/")
