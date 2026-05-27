"""Unit tests for NeMo Guardrails endpoints.

POST /ai-governance/guardrails/check  — guardrails tier
GET  /ai-governance/guardrails/config — guardrails tier
POST /ai-governance/guardrails/reload — superuser tier
"""

from unittest.mock import AsyncMock, MagicMock


ADMIN_UID = "admin-uid-001"
SERVICE_UID = "service-uid-999"


class TestGuardrailsCheck:
    """POST /ai-governance/guardrails/check"""

    def test_check_succeeds_for_admin(self, client, mock_admin_user, monkeypatch):
        fake_rails = MagicMock()
        fake_rails.generate_async = AsyncMock(return_value="Safe response")

        import ai_governance.clients.guardrails_client as gc

        monkeypatch.setattr(gc, "get_rails", lambda: fake_rails)

        resp = client.post(
            "/ai-governance/guardrails/check",
            json={"user_id": ADMIN_UID, "prompt": "Hello world"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["response"] == "Safe response"

    def test_check_succeeds_for_service_account(self, client, mock_service_user, monkeypatch):
        fake_rails = MagicMock()
        fake_rails.generate_async = AsyncMock(return_value="OK")

        import ai_governance.clients.guardrails_client as gc

        monkeypatch.setattr(gc, "get_rails", lambda: fake_rails)

        resp = client.post(
            "/ai-governance/guardrails/check",
            json={"user_id": SERVICE_UID, "prompt": "test"},
        )
        assert resp.status_code == 200

    def test_check_denied_for_regular_user(self, client, mock_regular_user):
        resp = client.post(
            "/ai-governance/guardrails/check",
            json={"user_id": "user-uid", "prompt": "Hello"},
        )
        assert resp.status_code == 403

    def test_check_requires_prompt(self, client, mock_admin_user):
        resp = client.post(
            "/ai-governance/guardrails/check",
            json={"user_id": ADMIN_UID},
        )
        assert resp.status_code == 400
        assert "prompt" in resp.get_json()["error"]


class TestGuardrailsConfig:
    """GET /ai-governance/guardrails/config"""

    def test_config_returns_path_info(self, client, mock_admin_user):
        resp = client.get(
            "/ai-governance/guardrails/config",
            json={"user_id": ADMIN_UID},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert "config_path" in body
        assert "flow_files" in body

    def test_config_denied_for_regular_user(self, client, mock_regular_user):
        resp = client.get(
            "/ai-governance/guardrails/config",
            json={"user_id": "uid"},
        )
        assert resp.status_code == 403


class TestGuardrailsReload:
    """POST /ai-governance/guardrails/reload — superuser only"""

    def test_reload_succeeds_for_service_account(self, client, mock_service_user, monkeypatch):
        import ai_governance.clients.guardrails_client as gc

        reload_calls = []
        monkeypatch.setattr(gc, "reload_rails", lambda: reload_calls.append(True))

        resp = client.post(
            "/ai-governance/guardrails/reload",
            json={"user_id": SERVICE_UID},
        )
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "reloaded"
        assert len(reload_calls) == 1

    def test_reload_denied_for_admin(self, client, mock_admin_user):
        resp = client.post(
            "/ai-governance/guardrails/reload",
            json={"user_id": ADMIN_UID},
        )
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "Access restricted to service account"
