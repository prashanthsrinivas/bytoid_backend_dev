"""GCP audit blueprint — the full CSPM endpoint surface under ``/gcp-audit``.

All logic lives in ``cspm_core.routes_factory.build_blueprint``; this module just
binds it to ``GCP_PROVIDER``. Registered in ``app.py`` as ``gcp_audit_bp``.
"""

from cspm_core.routes_factory import build_blueprint
from gcp_audit.provider import GCP_PROVIDER

gcp_audit_bp = build_blueprint(GCP_PROVIDER)
