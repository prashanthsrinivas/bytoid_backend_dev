"""
conftest.py — Bootstrap stubs and shared fixtures for the Azure integration test suite.

IMPORTANT: sys.modules injection MUST happen before any application import.
db/rds_db.py calls AWS Secrets Manager and PooledDB at import time (lines 29-48).
"""

import sys
from unittest.mock import MagicMock

# ── Stub out db.rds_db before anything imports it ─────────────────────────────
_rds_stub = MagicMock()
_rds_stub.connect_to_rds = MagicMock(return_value=None)
_rds_stub.pool = MagicMock()
sys.modules["db"] = MagicMock()
sys.modules["db.rds_db"] = _rds_stub
sys.modules["db.db_checkers"] = MagicMock()

# ── Stub out agent_route (helpers.py imports LanceClient) ─────────────────────
sys.modules["agent_route"] = MagicMock()
sys.modules["agent_route.lance_agent"] = MagicMock()

# ── Stub out credits_route (helpers.py imports Credits) ───────────────────────
sys.modules["credits_route"] = MagicMock()
sys.modules["credits_route.route"] = MagicMock()

# ── Stub out OneLogin SAML (helpers.py imports OneLogin_Saml2_Auth) ───────────
sys.modules["onelogin"] = MagicMock()
sys.modules["onelogin.saml2"] = MagicMock()
sys.modules["onelogin.saml2.auth"] = MagicMock()

# ── Stub out services.apiconnectors (routes + helpers import APIConnector) ─────
sys.modules["services.apiconnectors"] = MagicMock()

# ── Stub out services.audit_log_service — must set constants as real strings ──
_audit_stub = MagicMock()
_audit_stub.AZURE_SAML_CONNECTED = "AZURE_SAML_CONNECTED"
_audit_stub.AZURE_SAML_DISCONNECTED = "AZURE_SAML_DISCONNECTED"
_audit_stub.log_audit_event = MagicMock(return_value=None)
sys.modules["services.audit_log_service"] = _audit_stub

# ── Stub out services.scheduler_service ───────────────────────────────────────
sys.modules["services.scheduler_service"] = MagicMock()

# ── Stub out apiConnector.helpers (routes import schedule helpers) ─────────────
sys.modules["apiConnector"] = MagicMock()
sys.modules["apiConnector.helpers"] = MagicMock()

# ── Stub out utils.s3_utils ───────────────────────────────────────────────────
_s3_stub = MagicMock()
_s3_stub.save_app_runbase_S3 = MagicMock(return_value=True)
_s3_stub.getallendpointdetails = MagicMock(return_value=[])
_s3_stub.get_filedata_endp = MagicMock(return_value={})
sys.modules["utils.s3_utils"] = _s3_stub

# ─────────────────────────────────────────────────────────────────────────────
# Now it's safe to import application modules
# ─────────────────────────────────────────────────────────────────────────────

import json
import pytest
from flask import Flask
from azure_integration.routes import azure_integration_bp


# ── Flask app fixture (session-scoped: blueprint registered once) ──────────────

@pytest.fixture(scope="session")
def app():
    flask_app = Flask(__name__)
    flask_app.secret_key = "test-secret-key"
    flask_app.config.update(TESTING=True)
    flask_app.register_blueprint(azure_integration_bp)
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


# ── Core DB mock ───────────────────────────────────────────────────────────────

@pytest.fixture
def mock_conn():
    """
    Returns (conn, cur) where:
    - Context-manager cursor pattern: with conn.cursor(DictCursor) as c  →  c is cur
    - Plain cursor pattern: c = conn.cursor()  →  c is cur
    Both patterns use the same cur object.
    """
    conn = MagicMock()
    cur = MagicMock()
    # Make cursor() return cur, and cur itself is a valid context manager
    conn.cursor.return_value = cur
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    return conn, cur


# ── Shared data fixtures ───────────────────────────────────────────────────────

ADMIN_ID = "admin-test-user-123"


@pytest.fixture
def admin_id():
    return ADMIN_ID


@pytest.fixture
def mock_idp_config():
    return {
        "entity_id": "https://sts.windows.net/test-tenant/",
        "sso_url": "https://login.microsoftonline.com/test-tenant/saml2",
        "x509_cert": "MIIC_FAKE_CERT",
        "azure_region": "eastus",
        "tenant_id": "test-tenant-id",
        "client_id": "test-client-id",
        "client_secret": "test-client-secret",
        "default_scope": "https://graph.microsoft.com/.default",
    }


@pytest.fixture
def mock_active_session():
    return {
        "access_token": "mock-access-token",
        "refresh_token": "mock-refresh-token",
        "scope": "https://graph.microsoft.com/.default",
        "azure_region": "eastus",
        "azure_tenant_id": "test-tenant-id",
        "azure_object_id": "obj-123",
        "azure_upn": "user@example.com",
        "expires_at": "2099-01-01 00:00:00",
    }


@pytest.fixture
def mock_saml_auth():
    auth = MagicMock()
    auth.login.return_value = "https://login.microsoftonline.com/saml/redirect?SAMLRequest=abc"
    auth.process_response.return_value = None
    auth.get_errors.return_value = []
    auth.is_authenticated.return_value = True
    auth.get_last_error_reason.return_value = None
    auth.get_attributes.return_value = {
        "http://schemas.microsoft.com/identity/claims/objectidentifier": ["obj-123"],
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/upn": ["user@example.com"],
    }
    auth.get_nameid.return_value = "user@example.com"
    return auth


@pytest.fixture
def mock_api_result_success():
    return {"success": True, "status_code": 200, "body": {"value": []}}


@pytest.fixture
def mock_api_result_failure():
    return {"success": False, "status_code": 401, "error": "Unauthorized"}
