"""
Tests for Azure IdP configuration endpoints:
  POST   /azure/idp/config
  GET    /azure/idp/config
  DELETE /azure/idp/config
"""

import json
from unittest.mock import patch, MagicMock

import pytest

ADMIN_ID = "admin-test-user-123"

IDP_PAYLOAD = {
    "user_id": ADMIN_ID,
    "entity_id": "https://sts.windows.net/tenant/",
    "sso_url": "https://login.microsoftonline.com/tenant/saml2",
    "x509_cert": "MIIC_FAKE_CERT",
    "tenant_id": "test-tenant-id",
    "client_id": "test-client-id",
    "client_secret": "test-client-secret",
}


# ── POST /azure/idp/config ────────────────────────────────────────────────────

def test_post_idp_config_success(client, mock_conn):
    conn, cur = mock_conn
    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes._get_azure_idp_config", return_value=None), \
         patch("azure_integration.routes.connect_to_rds", return_value=conn):

        resp = client.post("/azure/idp/config", json=IDP_PAYLOAD)

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    cur.execute.assert_called()
    conn.commit.assert_called_once()


def test_post_idp_config_update_reuses_existing_cert(client, mock_conn, mock_idp_config):
    """When updating, omitting x509_cert re-uses the existing cert from DB."""
    conn, cur = mock_conn
    payload = {k: v for k, v in IDP_PAYLOAD.items() if k != "x509_cert"}

    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes._get_azure_idp_config", return_value=mock_idp_config), \
         patch("azure_integration.routes.connect_to_rds", return_value=conn):

        resp = client.post("/azure/idp/config", json=payload)

    assert resp.status_code == 200
    assert resp.get_json()["success"] is True


def test_post_idp_config_missing_entity_id(client):
    payload = {k: v for k, v in IDP_PAYLOAD.items() if k != "entity_id"}
    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes._get_azure_idp_config", return_value=None):
        resp = client.post("/azure/idp/config", json=payload)
    assert resp.status_code == 400
    assert "entity_id" in resp.get_json()["error"]


def test_post_idp_config_missing_sso_url(client):
    payload = {k: v for k, v in IDP_PAYLOAD.items() if k != "sso_url"}
    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes._get_azure_idp_config", return_value=None):
        resp = client.post("/azure/idp/config", json=payload)
    assert resp.status_code == 400
    assert "sso_url" in resp.get_json()["error"]


def test_post_idp_config_missing_tenant_id(client):
    payload = {k: v for k, v in IDP_PAYLOAD.items() if k != "tenant_id"}
    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes._get_azure_idp_config", return_value=None):
        resp = client.post("/azure/idp/config", json=payload)
    assert resp.status_code == 400
    assert "tenant_id" in resp.get_json()["error"]


def test_post_idp_config_missing_client_id(client):
    payload = {k: v for k, v in IDP_PAYLOAD.items() if k != "client_id"}
    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes._get_azure_idp_config", return_value=None):
        resp = client.post("/azure/idp/config", json=payload)
    assert resp.status_code == 400
    assert "client_id" in resp.get_json()["error"]


def test_post_idp_config_new_missing_x509_cert(client):
    """New record without x509_cert is rejected."""
    payload = {k: v for k, v in IDP_PAYLOAD.items() if k != "x509_cert"}
    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes._get_azure_idp_config", return_value=None):
        resp = client.post("/azure/idp/config", json=payload)
    assert resp.status_code == 400
    assert "x509_cert" in resp.get_json()["error"]


def test_post_idp_config_non_admin_rejected(client, app):
    with app.test_request_context():
        from flask import jsonify
        mock_err = (jsonify({"error": "ADMIN_ONLY"}), 403)
    with patch("azure_integration.routes._admin_only_check", return_value=(False, mock_err)):
        resp = client.post("/azure/idp/config", json=IDP_PAYLOAD)
    assert resp.status_code == 403


# ── GET /azure/idp/config ─────────────────────────────────────────────────────

def test_get_idp_config_configured(client, mock_idp_config):
    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes._get_azure_idp_config", return_value=mock_idp_config):
        resp = client.get(f"/azure/idp/config?user_id={ADMIN_ID}")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["configured"] is True
    assert data["entity_id"] == mock_idp_config["entity_id"]
    assert data["tenant_id"] == mock_idp_config["tenant_id"]
    assert data["client_id"] == mock_idp_config["client_id"]


def test_get_idp_config_not_configured(client):
    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes._get_azure_idp_config", return_value=None):
        resp = client.get(f"/azure/idp/config?user_id={ADMIN_ID}")

    assert resp.status_code == 200
    assert resp.get_json()["configured"] is False


def test_get_idp_config_non_admin_rejected(client, app):
    with app.test_request_context():
        from flask import jsonify
        mock_err = (jsonify({"error": "ADMIN_ONLY"}), 403)
    with patch("azure_integration.routes._admin_only_check", return_value=(False, mock_err)):
        resp = client.get(f"/azure/idp/config?user_id={ADMIN_ID}")
    assert resp.status_code == 403


# ── DELETE /azure/idp/config ──────────────────────────────────────────────────

def test_delete_idp_config_success(client, mock_conn):
    conn, cur = mock_conn
    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes.connect_to_rds", return_value=conn):
        resp = client.delete("/azure/idp/config", json={"user_id": ADMIN_ID})

    assert resp.status_code == 200
    assert resp.get_json()["success"] is True
    conn.commit.assert_called_once()
    # Verify DELETE was executed
    call_args = cur.execute.call_args_list
    assert any("DELETE" in str(c) for c in call_args)


def test_delete_idp_config_non_admin_rejected(client, app):
    with app.test_request_context():
        from flask import jsonify
        mock_err = (jsonify({"error": "ADMIN_ONLY"}), 403)
    with patch("azure_integration.routes._admin_only_check", return_value=(False, mock_err)):
        resp = client.delete("/azure/idp/config", json={"user_id": ADMIN_ID})
    assert resp.status_code == 403
