"""GCP domain collectors. Each module exposes pure ``analyze_*`` functions
(unit-testable on synthetic REST payloads) plus a ``collect(creds, project_id,
project_name)`` that fetches via REST and runs the analyzers.
"""

from gcp_audit.domains import compute, data, iam, logging_domain, network

DOMAIN_COLLECTORS = {
    "network": network.collect,
    "iam": iam.collect,
    "data": data.collect,
    "compute": compute.collect,
    "logging": logging_domain.collect,
}
