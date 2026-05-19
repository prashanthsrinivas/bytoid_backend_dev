"""
Tests for execute and scheduling endpoints:
  POST /azure/connector/apps/<app_id>/execute
  POST /azure/connector/endpoints/<endpoint_id>/execute
  POST /azure/connector/endpoints/<endpoint_id>/schedule
"""

import json
from datetime import datetime
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

ADMIN_ID = "admin-test-user-123"


# ── POST /azure/connector/apps/<app_id>/execute ───────────────────────────────

def test_execute_app_success(client):
    mock_result = {"success": True, "status_code": 200, "body": {"value": []}}

    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes._execute_azure_app_internal", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_result
        resp = client.post("/azure/connector/apps/42/execute", json={"user_id": ADMIN_ID})

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["result"]["success"] is True
    mock_exec.assert_called_once_with(42, ADMIN_ID)


def test_execute_app_not_found_returns_400(client):
    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes._execute_azure_app_internal", new_callable=AsyncMock) as mock_exec:
        mock_exec.side_effect = ValueError("Azure app not found")
        resp = client.post("/azure/connector/apps/999/execute", json={"user_id": ADMIN_ID})

    assert resp.status_code == 400
    assert "Azure app not found" in resp.get_json()["error"]


# ── POST /azure/connector/endpoints/<endpoint_id>/execute ─────────────────────

def test_execute_endpoint_success(client):
    mock_result = {"success": True, "status_code": 200, "body": []}

    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes._execute_azure_endpoint_internal", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_result
        resp = client.post("/azure/connector/endpoints/99/execute", json={"user_id": ADMIN_ID})

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["result"]["success"] is True


def test_execute_endpoint_with_runtime_params(client):
    """Runtime params (headers, query_params, body) are passed through to the executor."""
    runtime = {
        "user_id": ADMIN_ID,
        "headers": {"X-Custom": "value"},
        "query_params": {"$top": "10"},
        "body": {"key": "val"},
    }

    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes._execute_azure_endpoint_internal", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = {"success": True}
        resp = client.post("/azure/connector/endpoints/99/execute", json=runtime)

    assert resp.status_code == 200
    call_kwargs = mock_exec.call_args
    _, called_user_id, called_params = call_kwargs[0]
    assert called_user_id == ADMIN_ID
    assert called_params["headers"] == {"X-Custom": "value"}
    assert called_params["query_params"] == {"$top": "10"}
    assert called_params["body"] == {"key": "val"}


# ── POST /azure/connector/endpoints/<endpoint_id>/schedule ────────────────────

def test_schedule_endpoint_one_time(client):
    activation = {"type": "one_time", "datetime": "2099-06-01T10:00:00"}
    schedule_data = {"datetime": "2099-06-01T10:00:00", "timezone": "UTC"}
    task_result = {"task_id": "celery-task-abc-123"}

    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes.resolve_schedule_from_activation", return_value=("one_time", schedule_data)), \
         patch("azure_integration.routes._get_azure_schedule_endpointdetails", return_value=None), \
         patch("azure_integration.routes.AzureAPIConnectorScheduler") as MockSched, \
         patch("azure_integration.routes._save_azure_endpoint_schedule"):

        MockSched.schedule_endpoint_once = AsyncMock(return_value=task_result)
        resp = client.post(
            "/azure/connector/endpoints/99/schedule",
            json={"user_id": ADMIN_ID, "scheduledActivation": activation},
        )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    sched = data["schedule"]
    assert sched["celery_type"] == "task"
    assert sched["celery_task_id"] == "celery-task-abc-123"
    assert sched["frequency"] == "one_time"


def test_schedule_endpoint_daily(client):
    activation = {"type": "daily", "startTime": "09:00", "timezone": "America/Toronto"}
    schedule_data = {"startTime": "09:00", "timezone": "America/Toronto"}
    beat_result = {"entry_name": "azure_endpoint_daily_99_admin"}

    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes.resolve_schedule_from_activation", return_value=("daily", schedule_data)), \
         patch("azure_integration.routes._get_azure_schedule_endpointdetails", return_value=None), \
         patch("azure_integration.routes.AzureAPIConnectorScheduler") as MockSched, \
         patch("azure_integration.routes._save_azure_endpoint_schedule"):

        MockSched.schedule_endpoint_daily = AsyncMock(return_value=beat_result)
        resp = client.post(
            "/azure/connector/endpoints/99/schedule",
            json={"user_id": ADMIN_ID, "scheduledActivation": activation},
        )

    assert resp.status_code == 200
    sched = resp.get_json()["schedule"]
    assert sched["celery_type"] == "beat"
    assert sched["celery_entry"] == "azure_endpoint_daily_99_admin"


def test_schedule_endpoint_revokes_existing_before_rescheduling(client):
    """When a schedule already exists, the old task is revoked before creating a new one."""
    existing_schedule = {
        "celery_type": "task",
        "celery_task_id": "old-task-id-xyz",
    }
    schedule_data = {"datetime": "2099-07-01T08:00:00", "timezone": "UTC"}
    task_result = {"task_id": "new-task-id"}

    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes.resolve_schedule_from_activation", return_value=("one_time", schedule_data)), \
         patch("azure_integration.routes._get_azure_schedule_endpointdetails", return_value=existing_schedule), \
         patch("azure_integration.routes.AzureAPIConnectorScheduler") as MockSched, \
         patch("azure_integration.routes._save_azure_endpoint_schedule"):

        MockSched.revoke_task = MagicMock()
        MockSched.schedule_endpoint_once = AsyncMock(return_value=task_result)
        resp = client.post(
            "/azure/connector/endpoints/99/schedule",
            json={"user_id": ADMIN_ID, "scheduledActivation": {"type": "one_time", "datetime": "2099-07-01T08:00:00"}},
        )

    assert resp.status_code == 200
    MockSched.revoke_task.assert_called_once_with("old-task-id-xyz")


def test_schedule_endpoint_unsupported_type_returns_400(client):
    with patch("azure_integration.routes._admin_only_check", return_value=(True, None)), \
         patch("azure_integration.routes.resolve_schedule_from_activation", return_value=("foobar", {})), \
         patch("azure_integration.routes._get_azure_schedule_endpointdetails", return_value=None):

        resp = client.post(
            "/azure/connector/endpoints/99/schedule",
            json={"user_id": ADMIN_ID, "scheduledActivation": {"type": "foobar"}},
        )

    assert resp.status_code == 400
    assert "Unsupported schedule type" in resp.get_json()["error"]
