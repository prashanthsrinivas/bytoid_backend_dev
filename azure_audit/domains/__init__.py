"""Azure domain collectors. Each module exposes pure ``analyze_*`` functions
(unit-testable on synthetic ARM payloads) plus a ``collect(creds, sub_id,
sub_name)`` that fetches via REST and runs the analyzers.
"""

from azure_audit.domains import compute, data, identity, logging_domain, network

DOMAIN_COLLECTORS = {
    "network": network.collect,
    "identity": identity.collect,
    "data": data.collect,
    "compute": compute.collect,
    "logging": logging_domain.collect,
}
