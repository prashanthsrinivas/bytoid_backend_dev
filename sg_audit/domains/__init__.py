"""Cloud Security Posture domain collectors.

Each domain module exposes:
  * ``SCOPE`` — "account" (collect once per account) or "region" (per region).
  * ``collect(session, account_id, account_name, region) -> list[finding]`` —
    does its boto3 reads and runs its pure ``analyze_*`` functions, returning
    normalized findings (via ``analysis.normalize.make_domain_finding``).

The pure ``analyze_*`` functions take raw AWS dicts and are unit-testable without
boto3 (mirroring how Security Groups separates ``rules.py`` from ``runner.py``).
The Security Groups domain is NOT here — it stays in ``analysis/rules.py`` +
``collector_lambda/runner.py`` unchanged; the runner special-cases it.

``DOMAIN_COLLECTORS`` is the registry the runner fans out over. boto3 is imported
lazily inside each ``collect`` so importing this package stays dependency-light
(the analyzers can be imported and tested without boto3).
"""

from __future__ import annotations

from sg_audit.domains import (
    compute,
    containers,
    data,
    devops,
    external,
    iam,
    k8s_domain,
    logging_domain,
    network,
    vcs,
)

# domain key -> module (each has SCOPE + collect). Security Groups handled
# separately by the runner so its existing engine is untouched.
DOMAIN_COLLECTORS = {
    iam.DOMAIN: iam,
    network.DOMAIN: network,
    data.DOMAIN: data,
    compute.DOMAIN: compute,
    logging_domain.DOMAIN: logging_domain,
    devops.DOMAIN: devops,
    containers.DOMAIN: containers,
    external.DOMAIN: external,
    vcs.DOMAIN: vcs,
    k8s_domain.DOMAIN: k8s_domain,
}


def collector_for(domain: str):
    return DOMAIN_COLLECTORS.get(domain)
