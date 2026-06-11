"""Azure Cloud Security Posture — a thin provider over ``cspm_core``.

All generic posture machinery (findings, scoring, compliance, dashboard, exports,
approvals, AI recommendations, storage) lives in ``cspm_core``. This package only
supplies the Azure specifics: rule metadata + CIS mapping (``metadata.py``), the
five domain analyzers (``domains/``), ARM/Graph REST plumbing (``rest.py``), and
the ``AZURE_PROVIDER`` descriptor (``provider.py``). Credentials reuse the existing
``azure_integration`` login (SAML → client-credentials), minting an ARM-scoped
token since the Graph token can't read the ARM posture surface.
"""
