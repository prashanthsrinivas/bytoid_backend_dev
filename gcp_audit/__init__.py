"""GCP Cloud Security Posture — a thin provider over ``cspm_core``.

All generic posture machinery lives in ``cspm_core``. This package supplies the
GCP specifics: rule metadata + CIS GCP Foundation mapping (``metadata.py``), the
five domain analyzers (``domains/``), REST plumbing (``rest.py``), and the
``GCP_PROVIDER`` descriptor (``provider.py``). Credentials reuse the existing
``gcp_integration`` login (a service-account JSON key minting a fresh
``cloud-platform`` Bearer). Scopes are the org's projects (enumerated via Cloud
Resource Manager when an organization_id is in the audit scope, else the
configured project).
"""
