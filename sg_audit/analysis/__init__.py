"""Deterministic Security Group analysis — the dependency-light core.

Imported by BOTH the Flask app and the Lambda collector (stdlib only), so it
must never import boto3/Flask/DB. ``rules`` turns raw security groups into
findings; ``normalize`` is the shared finding/snapshot contract; ``score``
derives posture scores + rollups.
"""
