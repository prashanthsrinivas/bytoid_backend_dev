"""Unit tests for ai_governance/middleware/rbac.py.

These tests provide 100% branch coverage of the allow/deny logic so that
mutmut mutations on rbac.py are caught by the standard pytest runner
(the same runner used by the mutmut CI job).

Fixtures are defined in conftest.py:
  - mock_service_user  → service@bytoid.ca (super account)
  - mock_admin_user    → admin@tenant.com  (regular admin)
  - mock_regular_user  → user@tenant.com   (non-admin)
  - mock_db_failure    → _fetch_user_row returns None
"""

# ── Helper: inject a user_id via the request body ────────────────────────────
# The RBAC middleware resolves identity from g → session → body → params.
# In test_client requests we pass user_id in the JSON body.

ADMIN_UID = "admin-uid-001"
SERVICE_UID = "service-uid-999"
REGULAR_UID = "user-uid-123"
UNKNOWN_UID = "unknown-uid-000"


# ── Guardrails tier ───────────────────────────────────────────────────────────


class TestGuardrailsTier:
    """Tier="guardrails" — admins and service account are allowed."""

    def test_service_account_allowed(self, client, mock_service_user):
        resp = client.get(
            "/ai-governance/guardrails/config",
            json={"user_id": SERVICE_UID},
        )
        assert resp.status_code == 200

    def test_regular_admin_allowed(self, client, mock_admin_user):
        resp = client.get(
            "/ai-governance/guardrails/config",
            json={"user_id": ADMIN_UID},
        )
        assert resp.status_code == 200

    def test_non_admin_denied_403(self, client, mock_regular_user):
        resp = client.get(
            "/ai-governance/guardrails/config",
            json={"user_id": REGULAR_UID},
        )
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "Admin access required"

    def test_missing_user_id_returns_401(self, client):
        resp = client.get("/ai-governance/guardrails/config")
        assert resp.status_code == 401

    def test_db_failure_returns_404(self, client, mock_db_failure):
        resp = client.get(
            "/ai-governance/guardrails/config",
            json={"user_id": UNKNOWN_UID},
        )
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "User not found"


# ── Superuser tier ────────────────────────────────────────────────────────────


class TestSuperuserTier:
    """Tier="superuser" — only service@bytoid.ca allowed."""

    def test_service_account_allowed(self, client, mock_service_user):
        resp = client.get(
            "/ai-governance/langfuse/traces",
            json={"user_id": SERVICE_UID},
        )
        # May fail with 500 if Langfuse is not configured — that's fine;
        # the RBAC layer must pass (status != 401/403).
        assert resp.status_code not in (401, 403)

    def test_regular_admin_denied_403(self, client, mock_admin_user):
        resp = client.get(
            "/ai-governance/langfuse/traces",
            json={"user_id": ADMIN_UID},
        )
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "Access restricted to service account"

    def test_non_admin_denied_403(self, client, mock_regular_user):
        resp = client.get(
            "/ai-governance/langfuse/traces",
            json={"user_id": REGULAR_UID},
        )
        assert resp.status_code == 403

    def test_missing_user_id_returns_401(self, client):
        resp = client.get("/ai-governance/langfuse/traces")
        assert resp.status_code == 401

    def test_db_failure_returns_404(self, client, mock_db_failure):
        resp = client.get(
            "/ai-governance/langfuse/traces",
            json={"user_id": UNKNOWN_UID},
        )
        assert resp.status_code == 404


# ── TTL cache (Layer 2) ───────────────────────────────────────────────────────


class TestTTLCache:
    """Verify that _db_fetch_user_row honours the 30-second TTL."""

    def test_cache_returns_cached_value_within_ttl(self, monkeypatch):
        import time
        from ai_governance.middleware import rbac

        call_count = {"n": 0}
        real_row = {"user_type": "admin", "email": "admin@tenant.com"}

        def fake_raw_db(uid):
            call_count["n"] += 1
            return real_row

        # Patch the inner raw-DB function; the TTL wrapper should short-circuit before it.
        monkeypatch.setattr(rbac, "_raw_db_query_user_row", fake_raw_db)

        # Prime the cache manually (simulates a previous request having called it)
        now = time.monotonic()
        with rbac._CACHE_LOCK:
            rbac._USER_ROW_CACHE["ttl-test-uid"] = (real_row, now + 30.0)

        result = rbac._db_fetch_user_row("ttl-test-uid")
        assert result == real_row
        # Raw DB should not have been called because cache entry is still valid
        assert call_count["n"] == 0

    def test_cache_expires_after_ttl(self, monkeypatch):
        import time
        from ai_governance.middleware import rbac

        real_row = {"user_type": "admin", "email": "admin@tenant.com"}
        call_count = {"n": 0}

        def counting_raw_db(uid):
            call_count["n"] += 1
            return real_row

        # Patch the inner raw-DB function; cache is expired so it must be called.
        monkeypatch.setattr(rbac, "_raw_db_query_user_row", counting_raw_db)

        # Plant an expired cache entry
        now = time.monotonic()
        with rbac._CACHE_LOCK:
            rbac._USER_ROW_CACHE["expired-uid"] = (real_row, now - 1.0)  # already expired

        # Call the TTL wrapper — cache miss → raw DB should be called once
        rbac._db_fetch_user_row("expired-uid")
        assert call_count["n"] == 1


# ── g-layer cache (Layer 1) ───────────────────────────────────────────────────


class TestGCache:
    """Within a single request, _fetch_user_row must not call _db_fetch_user_row
    a second time if the result is already stored on g."""

    def test_g_cache_prevents_second_db_call(self, ai_gov_app, monkeypatch):
        from ai_governance.middleware import rbac

        call_count = {"n": 0}
        real_row = {"user_type": "admin", "email": "admin@tenant.com"}

        def counting_db(uid):
            call_count["n"] += 1
            return real_row

        monkeypatch.setattr(rbac, "_db_fetch_user_row", counting_db)

        with ai_gov_app.test_request_context("/", json={"user_id": "uid-g-test"}):
            # First call — should hit DB
            row1 = rbac._fetch_user_row("uid-g-test")
            # Second call — should hit g cache
            row2 = rbac._fetch_user_row("uid-g-test")

        assert row1 == real_row
        assert row2 == real_row
        assert call_count["n"] == 1  # DB called exactly once


# ── Audit logging on denial ───────────────────────────────────────────────────


class TestAuditOnDenial:
    """Verify that access denials emit an audit event."""

    def test_superuser_denial_logs_audit(self, client, mock_admin_user, monkeypatch):
        audit_calls = []

        import ai_governance.middleware.rbac as rbac_module

        monkeypatch.setattr(
            rbac_module,
            "log_audit_event",
            lambda *args, **kwargs: audit_calls.append(kwargs),
        )
        client.get("/ai-governance/langfuse/traces", json={"user_id": ADMIN_UID})
        assert any(
            c.get("status") == "denied" for c in audit_calls
        ), "Expected a denied audit event"

    def test_guardrails_denial_logs_audit(self, client, mock_regular_user, monkeypatch):
        audit_calls = []

        import ai_governance.middleware.rbac as rbac_module

        monkeypatch.setattr(
            rbac_module,
            "log_audit_event",
            lambda *args, **kwargs: audit_calls.append(kwargs),
        )
        client.get("/ai-governance/guardrails/config", json={"user_id": REGULAR_UID})
        assert any(c.get("status") == "denied" for c in audit_calls)
