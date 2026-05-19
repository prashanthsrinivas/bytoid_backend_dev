"""
Tests for connector endpoint CRUD + test endpoints:
  POST   /azure/connector/apps/<app_id>/endpoints
  GET    /azure/connector/apps/<app_id>/endpoints
  PUT    /azure/connector/endpoints/<endpoint_id>
  DELETE /azure/connector/endpoints/<endpoint_id>
  POST   /azure/connector/endpoints/<endpoint_id>/test
"""

import json
from unittest.mock import patch, MagicMock

import pytest

ADMIN_ID = "admin-test-user-123"

EP_PAYLOAD = {
    "user_id": ADMIN_ID,
    "name": "List Users",
    "path": "/users",
    "method": "GET",
}

# Simulates the JOIN row returned by the test endpoint query
EP_JOIN_ROW = {
    "id": 99,
    "app_id": 42,
    "user_id": ADMIN_ID,
    "name": "List Users",
    "path": "/users",
    "method": "GET",
    "headers": "{}",
    "query_params": "{}",
    "path_params": "{}",
    "body_template": None,
    "timeout_seconds": 10,
    "is_active": 1,
    "last_test_status": None,
    "last_tested_at": None,
    "created_at": "2026-01-01 00:00:00",
    "updated_at": "2026-01-01 00:00:00",
    # From app JOIN
    "base_url": "https://graph.microsoft.com/v1.0",
    "auth_config": "{}",
    "app_timeout": 10,
    "retry_count": 0,
    "retry_backoff_seconds": 0,
}


# ── POST /azure/connector/apps/<app_id>/endpoints ─────────────────────────────

def test_create_endpoint_success(client, mock_conn):
    conn, cur = mock_conn
    cur.fetchone.return_value = {"id": 42}   # app ownership check passes
    cur.lastrowid = 99

    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes.connect_to_rds", return_value=conn):
        resp = client.post("/azure/connector/apps/42/endpoints", json=EP_PAYLOAD)

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["endpoint_id"] == 99
    assert data["name"] == "List Users"
    conn.commit.assert_called_once()


def test_create_endpoint_app_not_found(client, mock_conn):
    conn, cur = mock_conn
    cur.fetchone.return_value = None   # app not found or not active

    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes.connect_to_rds", return_value=conn):
        resp = client.post("/azure/connector/apps/42/endpoints", json=EP_PAYLOAD)

    assert resp.status_code == 404


def test_create_endpoint_missing_name(client):
    payload = {k: v for k, v in EP_PAYLOAD.items() if k != "name"}
    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)):
        resp = client.post("/azure/connector/apps/42/endpoints", json=payload)
    assert resp.status_code == 400
    assert "name" in resp.get_json()["error"]


def test_create_endpoint_missing_path(client):
    payload = {k: v for k, v in EP_PAYLOAD.items() if k != "path"}
    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)):
        resp = client.post("/azure/connector/apps/42/endpoints", json=payload)
    assert resp.status_code == 400
    assert "path" in resp.get_json()["error"]


# ── GET /azure/connector/apps/<app_id>/endpoints ──────────────────────────────

def test_list_endpoints_success(client, mock_conn):
    conn, cur = mock_conn
    cur.fetchall.return_value = [dict(EP_JOIN_ROW)]

    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes.connect_to_rds", return_value=conn):
        resp = client.get(f"/azure/connector/apps/42/endpoints?user_id={ADMIN_ID}")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert len(data["endpoints"]) == 1
    ep = data["endpoints"][0]
    assert ep["name"] == "List Users"
    # JSON string fields should be parsed into dicts
    assert isinstance(ep["headers"], dict)
    assert isinstance(ep["query_params"], dict)


def test_list_endpoints_empty(client, mock_conn):
    conn, cur = mock_conn
    cur.fetchall.return_value = []

    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes.connect_to_rds", return_value=conn):
        resp = client.get(f"/azure/connector/apps/42/endpoints?user_id={ADMIN_ID}")

    assert resp.status_code == 200
    assert resp.get_json()["endpoints"] == []


# ── PUT /azure/connector/endpoints/<endpoint_id> ──────────────────────────────

def test_update_endpoint_success(client, mock_conn):
    conn, cur = mock_conn
    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes.connect_to_rds", return_value=conn):
        resp = client.put("/azure/connector/endpoints/99", json={"user_id": ADMIN_ID, "name": "Updated Name"})

    assert resp.status_code == 200
    assert resp.get_json()["success"] is True
    conn.commit.assert_called_once()


def test_update_endpoint_no_fields_returns_400(client, mock_conn):
    conn, cur = mock_conn
    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes.connect_to_rds", return_value=conn):
        resp = client.put("/azure/connector/endpoints/99", json={"user_id": ADMIN_ID})

    assert resp.status_code == 400
    assert "No fields" in resp.get_json()["error"]


# ── DELETE /azure/connector/endpoints/<endpoint_id> ───────────────────────────

def test_delete_endpoint_success(client, mock_conn):
    conn, cur = mock_conn
    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes.connect_to_rds", return_value=conn):
        resp = client.delete("/azure/connector/endpoints/99", json={"user_id": ADMIN_ID})

    assert resp.status_code == 200
    assert resp.get_json()["success"] is True
    conn.commit.assert_called_once()


# ── POST /azure/connector/endpoints/<endpoint_id>/test ────────────────────────

def test_test_endpoint_success(client, mock_conn, mock_api_result_success):
    conn, cur = mock_conn
    cur.fetchone.return_value = dict(EP_JOIN_ROW)

    mock_connector = MagicMock()
    mock_connector.execute.return_value = mock_api_result_success

    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes.connect_to_rds", return_value=conn), \
         patch("azure_integration.routes._resolve_azure_auth", return_value={"type": "azure_oauth", "access_token": "tok"}), \
         patch("azure_integration.routes.APIConnector", return_value=mock_connector):
        resp = client.post("/azure/connector/endpoints/99/test", json={"user_id": ADMIN_ID})

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["test_result"]["success"] is True

    update_calls = [str(c) for c in cur.execute.call_args_list]
    assert any("last_test_status" in c and "success" in c for c in update_calls)


def test_test_endpoint_not_found(client, mock_conn):
    conn, cur = mock_conn
    cur.fetchone.return_value = None

    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes.connect_to_rds", return_value=conn):
        resp = client.post("/azure/connector/endpoints/999/test", json={"user_id": ADMIN_ID})

    assert resp.status_code == 404
