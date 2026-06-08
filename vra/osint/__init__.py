"""VRA OSINT collection internals.

``safe_fetch`` (SSRF guard) and ``normalize`` (the shared finding contract) are
imported by BOTH the Flask app and the Lambda collector, so they must stay
dependency-light (stdlib + requests only).
"""
