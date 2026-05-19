"""
Tests for connector app CRUD endpoints:
  POST   /azure/connector/apps
  GET    /azure/connector/apps/<user_id>
  PUT    /azure/connector/apps/<app_id>
  DELETE /azure/connector/apps/<app_id>
  POST   /azure/connector/apps/<app_id>/test
"""

import json
from unittest.mock import patch, MagicMock, call

import pytest

ADMIN_ID = "admin-test-user-123"

APP_PAYLOAD = {
    "user_id": ADMIN_ID,
    "app_name": "Test Graph App",
    "base_url": "https://graph.microsoft.com/v1.0",
}

APP_ROW = {
    "id": 42,
    "user_id": ADMIN_ID,
    "app_name": "Test Graph App",
    "provider": "azure",
    "base_url": "https://graph.microsoft.com/v1.0",
    "auth_type": "azure_oauth",
    "auth_config": "{}",
    "headers": "{}",
    "query_params": "{}",
    "status": "active",
    "is_universal": False,
    "source_global_azure_app_id": None,
    "last_test_status": None,
    "last_tested_at": None,
    "created_at": "2026-01-01 00:00:00",
    "updated_at": "2026-01-01 00:00:00",
    "timeout_seconds": 10,
    "retry_count": 0,
    "retry_backoff_seconds": 0,
}


# ── POST /azure/connector/apps ────────────────────────────────────────────────

def test_create_app_success(client, mock_conn):
    conn, cur = mock_conn
    cur.fetchone.return_value = None   # no existing app
    cur.lastrowid = 42

    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes.connect_to_rds", return_value=conn):
        resp = client.post("/azure/connector/apps", json=APP_PAYLOAD)

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["already_exists"] is False
    assert data["app_id"] == 42
    conn.commit.assert_called_once()


def test_create_app_already_exists(client, mock_conn):
    conn, cur = mock_conn
    # First fetchone: existing id check → returns existing row
    # Second fetchone: full app details
    existing_app = {k: v for k, v in APP_ROW.items()}
    cur.fetchone.side_effect = [{"id": 42}, existing_app]

    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes.connect_to_rds", return_value=conn):
        resp = client.post("/azure/connector/apps", json=APP_PAYLOAD)

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["already_exists"] is True
    assert "app" in data


def test_create_app_missing_app_name(client):
    payload = {k: v for k, v in APP_PAYLOAD.items() if k != "app_name"}
    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)):
        resp = client.post("/azure/connector/apps", json=payload)
    assert resp.status_code == 400
    assert "app_name" in resp.get_json()["error"]


def test_create_app_missing_base_url(client):
    payload = {k: v for k, v in APP_PAYLOAD.items() if k != "base_url"}
    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)):
        resp = client.post("/azure/connector/apps", json=payload)
    assert resp.status_code == 400
    assert "base_url" in resp.get_json()["error"]


def test_create_app_non_admin_rejected(client, app):
    with app.test_request_context():
        from flask import jsonify
        mock_err = (jsonify({"error": "ADMIN_ONLY"}), 403)
    with patch("azure_integration.routes._admin_only_check", return_value=(False, mock_err)):
        resp = client.post("/azure/connector/apps", json=APP_PAYLOAD)
    assert resp.status_code == 403


# ── GET /azure/connector/apps/<user_id> ───────────────────────────────────────

def test_list_apps_success(client, mock_conn):
    conn, cur = mock_conn
    cur.fetchall.return_value = [dict(APP_ROW)]

    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes.connect_to_rds", return_value=conn):
        resp = client.get(f"/azure/connector/apps/{ADMIN_ID}")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert len(data["apps"]) == 1
    assert data["apps"][0]["app_name"] == "Test Graph App"


def test_list_apps_empty(client, mock_conn):
    conn, cur = mock_conn
    cur.fetchall.return_value = []

    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes.connect_to_rds", return_value=conn):
        resp = client.get(f"/azure/connector/apps/{ADMIN_ID}")

    assert resp.status_code == 200
    assert resp.get_json()["apps"] == []


# ── PUT /azure/connector/apps/<app_id> ────────────────────────────────────────

def test_update_app_success(client, mock_conn):
    conn, cur = mock_conn
    cur.fetchone.return_value = {"user_id": ADMIN_ID}

    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes.connect_to_rds", return_value=conn):
        resp = client.put("/azure/connector/apps/42", json={"user_id": ADMIN_ID, "app_name": "Renamed App"})

    assert resp.status_code == 200
    assert resp.get_json()["success"] is True
    conn.commit.assert_called_once()


def test_update_app_not_owned_returns_404(client, mock_conn):
    conn, cur = mock_conn
    cur.fetchone.return_value = {"user_id": "someone-else"}

    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes.connect_to_rds", return_value=conn):
        resp = client.put("/azure/connector/apps/42", json={"user_id": ADMIN_ID, "app_name": "X"})

    assert resp.status_code == 404


def test_update_app_no_fields_returns_400(client, mock_conn):
    conn, cur = mock_conn
    cur.fetchone.return_value = {"user_id": ADMIN_ID}

    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes.connect_to_rds", return_value=conn):
        resp = client.put("/azure/connector/apps/42", json={"user_id": ADMIN_ID})

    assert resp.status_code == 400
    assert "No fields" in resp.get_json()["error"]


# ── DELETE /azure/connector/apps/<app_id> ─────────────────────────────────────

def test_delete_app_success(client, mock_conn):
    conn, cur = mock_conn
    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes.connect_to_rds", return_value=conn):
        resp = client.delete("/azure/connector/apps/42", json={"user_id": ADMIN_ID})

    assert resp.status_code == 200
    assert resp.get_json()["success"] is True
    conn.commit.assert_called_once()
    call_args = cur.execute.call_args_list
    assert any("DELETE" in str(c) for c in call_args)


# ── POST /azure/connector/apps/<app_id>/test ──────────────────────────────────

def test_test_app_success(client, mock_conn, mock_api_result_success):
    conn, cur = mock_conn
    cur.fetchone.return_value = dict(APP_ROW)

    mock_connector = MagicMock()
    mock_connector.execute.return_value = mock_api_result_success

    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes.connect_to_rds", return_value=conn), \
         patch("azure_integration.routes._resolve_azure_auth", return_value={"type": "azure_oauth", "access_token": "tok"}), \
         patch("azure_integration.routes.APIConnector", return_value=mock_connector):
        resp = client.post("/azure/connector/apps/42/test", json={"user_id": ADMIN_ID})

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["test_result"]["success"] is True

    # Verify UPDATE was called with "success" status
    update_calls = [str(c) for c in cur.execute.call_args_list]
    assert any("last_test_status" in c and "success" in c for c in update_calls)


def test_test_app_not_found(client, mock_conn):
    conn, cur = mock_conn
    cur.fetchone.return_value = None

    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes.connect_to_rds", return_value=conn):
        resp = client.post("/azure/connector/apps/99/test", json={"user_id": ADMIN_ID})

    assert resp.status_code == 404


def test_test_app_connector_failure_updates_status(client, mock_conn, mock_api_result_failure):
    conn, cur = mock_conn
    cur.fetchone.return_value = dict(APP_ROW)

    mock_connector = MagicMock()
    mock_connector.execute.return_value = mock_api_result_failure

    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes.connect_to_rds", return_value=conn), \
         patch("azure_integration.routes._resolve_azure_auth", return_value={"type": "azure_oauth", "access_token": "tok"}), \
         patch("azure_integration.routes.APIConnector", return_value=mock_connector):
        resp = client.post("/azure/connector/apps/42/test", json={"user_id": ADMIN_ID})

    assert resp.status_code == 200
    update_calls = [str(c) for c in cur.execute.call_args_list]
    assert any("last_test_status" in c and "failed" in c for c in update_calls)
