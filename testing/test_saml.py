"""
Tests for SAML authentication endpoints:
  GET  /azure/saml/login
  POST /azure/saml/acs
  GET  /azure/saml/status
  POST /azure/saml/disconnect
"""

import json
from unittest.mock import patch, MagicMock

import pytest

ADMIN_ID = "admin-test-user-123"
RELAY_STATE = json.dumps({"user_id": ADMIN_ID, "redirect": "https://app.bytoid.ai/azure-integration"})


# ── GET /azure/saml/login ─────────────────────────────────────────────────────

def test_saml_login_redirects_to_idp(client, mock_idp_config, mock_saml_auth):
    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes._get_azure_idp_config", return_value=mock_idp_config), \
         patch("azure_integration.routes.prepare_flask_request_azure", return_value={}), \
         patch("azure_integration.routes._init_saml_auth_azure", return_value=mock_saml_auth):

        resp = client.get(f"/azure/saml/login?user_id={ADMIN_ID}&redirect=https://app.bytoid.ai/azure-integration")

    assert resp.status_code == 302
    assert "microsoftonline.com" in resp.headers["Location"]


def test_saml_login_no_idp_config_returns_400(client):
    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes._get_azure_idp_config", return_value=None):
        resp = client.get(f"/azure/saml/login?user_id={ADMIN_ID}")
    assert resp.status_code == 400
    assert "not configured" in resp.get_json()["error"].lower()


def test_saml_login_invalid_redirect_falls_back_to_default(client, mock_idp_config, mock_saml_auth):
    """Redirect URLs not in ALLOWED_ORIGINS are silently replaced with the default."""
    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes._get_azure_idp_config", return_value=mock_idp_config), \
         patch("azure_integration.routes.prepare_flask_request_azure", return_value={}), \
         patch("azure_integration.routes._init_saml_auth_azure", return_value=mock_saml_auth):

        resp = client.get(f"/azure/saml/login?user_id={ADMIN_ID}&redirect=https://evil.example.com/steal")

    assert resp.status_code == 302
    # The login URL still redirects to IdP — not to the evil URL
    location = resp.headers["Location"]
    assert "evil.example.com" not in location


def test_saml_login_non_admin_rejected(client, app):
    with app.test_request_context():
        from flask import jsonify
        mock_err = (jsonify({"error": "ADMIN_ONLY"}), 403)
    with patch("azure_integration.routes._admin_only_check", return_value=(False, mock_err)):
        resp = client.get(f"/azure/saml/login?user_id={ADMIN_ID}")
    assert resp.status_code == 403


# ── POST /azure/saml/acs ──────────────────────────────────────────────────────

def test_saml_acs_success(client, mock_conn, mock_idp_config, mock_saml_auth):
    conn, cur = mock_conn
    token = {"access_token": "test-token", "refresh_token": "refresh", "expires_in": 3600, "scope": "https://graph.microsoft.com/.default"}

    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes.prepare_flask_request_azure", return_value={}), \
         patch("azure_integration.routes._init_saml_auth_azure", return_value=mock_saml_auth), \
         patch("azure_integration.routes._get_azure_idp_config", return_value=mock_idp_config), \
         patch("azure_integration.routes.exchange_saml_for_azure_token", return_value=(token, None)), \
         patch("azure_integration.routes.connect_to_rds", return_value=conn), \
         patch("azure_integration.routes.log_audit_event"):

        resp = client.post(
            "/azure/saml/acs",
            data={"RelayState": RELAY_STATE, "SAMLResponse": "FAKE_B64_SAML_RESPONSE"},
            content_type="application/x-www-form-urlencoded",
        )

    assert resp.status_code == 302
    assert "status=success" in resp.headers["Location"]
    conn.commit.assert_called_once()


def test_saml_acs_saml_errors_returns_400(client, mock_idp_config, mock_saml_auth):
    mock_saml_auth.get_errors.return_value = ["invalid_signature"]
    mock_saml_auth.get_last_error_reason.return_value = "Signature validation failed"

    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes.prepare_flask_request_azure", return_value={}), \
         patch("azure_integration.routes._init_saml_auth_azure", return_value=mock_saml_auth), \
         patch("azure_integration.routes._get_azure_idp_config", return_value=mock_idp_config):

        resp = client.post(
            "/azure/saml/acs",
            data={"RelayState": RELAY_STATE, "SAMLResponse": "FAKE"},
            content_type="application/x-www-form-urlencoded",
        )

    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_saml_acs_not_authenticated_returns_401(client, mock_idp_config, mock_saml_auth):
    mock_saml_auth.get_errors.return_value = []
    mock_saml_auth.is_authenticated.return_value = False

    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes.prepare_flask_request_azure", return_value={}), \
         patch("azure_integration.routes._init_saml_auth_azure", return_value=mock_saml_auth), \
         patch("azure_integration.routes._get_azure_idp_config", return_value=mock_idp_config):

        resp = client.post(
            "/azure/saml/acs",
            data={"RelayState": RELAY_STATE, "SAMLResponse": "FAKE"},
            content_type="application/x-www-form-urlencoded",
        )

    assert resp.status_code == 401


def test_saml_acs_token_exchange_failure_returns_400(client, mock_conn, mock_idp_config, mock_saml_auth):
    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes.prepare_flask_request_azure", return_value={}), \
         patch("azure_integration.routes._init_saml_auth_azure", return_value=mock_saml_auth), \
         patch("azure_integration.routes._get_azure_idp_config", return_value=mock_idp_config), \
         patch("azure_integration.routes.exchange_saml_for_azure_token", return_value=(None, "Token endpoint unreachable")):

        resp = client.post(
            "/azure/saml/acs",
            data={"RelayState": RELAY_STATE, "SAMLResponse": "FAKE"},
            content_type="application/x-www-form-urlencoded",
        )

    assert resp.status_code == 400
    data = resp.get_json()
    assert "Token exchange failed" in data["error"]


# ── GET /azure/saml/status ────────────────────────────────────────────────────

def test_saml_status_connected(client, mock_active_session):
    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes._get_active_azure_session", return_value=mock_active_session):
        resp = client.get(f"/azure/saml/status?user_id={ADMIN_ID}")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["connected"] is True
    assert data["azure_upn"] == "user@example.com"
    assert data["azure_tenant_id"] == "test-tenant-id"


def test_saml_status_not_connected(client):
    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes._get_active_azure_session", return_value=None):
        resp = client.get(f"/azure/saml/status?user_id={ADMIN_ID}")

    assert resp.status_code == 200
    assert resp.get_json()["connected"] is False


# ── POST /azure/saml/disconnect ───────────────────────────────────────────────

def test_saml_disconnect_success(client, mock_conn):
    conn, cur = mock_conn
    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes.connect_to_rds", return_value=conn), \
         patch("azure_integration.routes.log_audit_event"):
        resp = client.post("/azure/saml/disconnect", json={"user_id": ADMIN_ID})

    assert resp.status_code == 200
    assert resp.get_json()["success"] is True
    conn.commit.assert_called_once()
    call_args = cur.execute.call_args_list
    assert any("DELETE" in str(c) and "azure_saml_sessions" in str(c) for c in call_args)


def test_saml_disconnect_non_admin_rejected(client, app):
    with app.test_request_context():
        from flask import jsonify
        mock_err = (jsonify({"error": "ADMIN_ONLY"}), 403)
    with patch("azure_integration.routes._admin_only_check", return_value=(False, mock_err)):
        resp = client.post("/azure/saml/disconnect", json={"user_id": ADMIN_ID})
    assert resp.status_code == 403
