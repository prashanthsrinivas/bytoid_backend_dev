"""Provider-agnostic Cloud Security Posture (CSPM) engine.

The AWS posture module (``sg_audit/``) is the reference implementation; this
package generalizes its provider-agnostic layers (finding contract, scoring,
compliance, storage/collect spine, dashboard, AI recommendation, exports,
runbook evidence, retention, the route surface) so that Azure (``azure_audit/``)
and GCP (``gcp_audit/``) can share one engine.

A cloud is plugged in via a ``Provider`` (see ``provider.py``): it supplies the
rule metadata, domain collectors, credential resolution, and scope enumeration;
``cspm_core`` owns everything else. AWS stays on its own copies (untouched).
"""
