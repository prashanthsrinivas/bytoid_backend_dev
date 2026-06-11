"""Azure audit blueprint — the full CSPM endpoint surface under ``/azure-audit``.

All logic lives in ``cspm_core.routes_factory.build_blueprint``; this module just
binds it to ``AZURE_PROVIDER``. Registered in ``app.py`` as ``azure_audit_bp``.
"""

from azure_audit.provider import AZURE_PROVIDER
from cspm_core.routes_factory import build_blueprint

azure_audit_bp = build_blueprint(AZURE_PROVIDER)
