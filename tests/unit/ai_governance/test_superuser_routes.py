"""Unit tests for superuser-tier endpoints.

Covers: Langfuse, MLflow, fairness, Giskard, TruLens, and DeepEval routes.
For Celery-dispatched endpoints the test verifies that:
  1. RBAC passes for service@bytoid.ca
  2. RBAC blocks regular admins with 403
  3. The response body contains {"task_id", "status": "queued"}

For synchronous endpoints the handler itself is patched.
"""

import pytest
from unittest.mock import MagicMock


SERVICE_UID = "service-uid-999"
ADMIN_UID = "admin-uid-001"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _fake_task():
    task = MagicMock()
    task.id = "fake-task-id-abc"
    return task


# ── Langfuse ──────────────────────────────────────────────────────────────────


class TestLangfuseTraces:
    def test_allowed_for_service(self, client, mock_service_user, monkeypatch):
        fake_lf = MagicMock()
        fake_trace = MagicMock()
        fake_trace.dict.return_value = {"id": "t1"}
        fake_lf.fetch_traces.return_value = MagicMock(data=[fake_trace])

        import ai_governance.clients.langfuse_client as lc

        monkeypatch.setattr(lc, "is_configured", lambda: True)
        monkeypatch.setattr(lc, "get_langfuse", lambda: fake_lf)

        resp = client.get("/ai-governance/langfuse/traces", json={"user_id": SERVICE_UID})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["traces"] == [{"id": "t1"}]
        assert body.get("configured") is True

    def test_not_configured_returns_empty_with_flag(
        self, client, mock_service_user, monkeypatch
    ):
        import ai_governance.clients.langfuse_client as lc

        monkeypatch.setattr(lc, "is_configured", lambda: False)

        resp = client.get("/ai-governance/langfuse/traces", json={"user_id": SERVICE_UID})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["traces"] == []
        assert body["configured"] is False

    def test_denied_for_admin(self, client, mock_admin_user):
        resp = client.get("/ai-governance/langfuse/traces", json={"user_id": ADMIN_UID})
        assert resp.status_code == 403


class TestLangfuseScore:
    def test_score_allowed_for_service(self, client, mock_service_user, monkeypatch):
        fake_lf = MagicMock()

        import ai_governance.clients.langfuse_client as lc

        monkeypatch.setattr(lc, "is_configured", lambda: True)
        monkeypatch.setattr(lc, "get_langfuse", lambda: fake_lf)

        resp = client.post(
            "/ai-governance/langfuse/score",
            json={
                "user_id": SERVICE_UID,
                "trace_id": "tid1",
                "name": "relevance",
                "value": 0.9,
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "scored"

    def test_score_missing_fields_returns_400(self, client, mock_service_user, monkeypatch):
        fake_lf = MagicMock()

        import ai_governance.clients.langfuse_client as lc

        monkeypatch.setattr(lc, "is_configured", lambda: True)
        monkeypatch.setattr(lc, "get_langfuse", lambda: fake_lf)

        resp = client.post(
            "/ai-governance/langfuse/score",
            json={"user_id": SERVICE_UID, "trace_id": "tid1"},
        )
        assert resp.status_code == 400

    def test_score_denied_for_admin(self, client, mock_admin_user):
        resp = client.post(
            "/ai-governance/langfuse/score",
            json={"user_id": ADMIN_UID, "trace_id": "t", "name": "n", "value": 1},
        )
        assert resp.status_code == 403


# ── MLflow ────────────────────────────────────────────────────────────────────


class TestMLflowLog:
    def test_log_run_returns_run_id(self, client, mock_service_user, monkeypatch):
        fake_run = MagicMock()
        fake_run.__enter__ = lambda s: s
        fake_run.__exit__ = MagicMock(return_value=False)
        fake_run.info.run_id = "run-123"

        fake_mlflow = MagicMock()
        fake_mlflow.start_run.return_value = fake_run

        import ai_governance.clients.mlflow_client as mc

        monkeypatch.setattr(mc, "get_mlflow", lambda: fake_mlflow)

        resp = client.post(
            "/ai-governance/mlflow/log",
            json={
                "user_id": SERVICE_UID,
                "run_name": "test-run",
                "params": {"lr": "0.01"},
                "metrics": {"accuracy": 0.95},
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()["run_id"] == "run-123"

    def test_log_denied_for_admin(self, client, mock_admin_user):
        resp = client.post(
            "/ai-governance/mlflow/log",
            json={"user_id": ADMIN_UID},
        )
        assert resp.status_code == 403


class TestMLflowExplain:
    def test_explain_dispatches_celery(self, client, mock_service_user, monkeypatch):
        fake_task = _fake_task()

        import ai_governance.tasks as tasks_mod

        monkeypatch.setattr(
            tasks_mod.run_mlflow_explain,
            "delay",
            lambda **kw: fake_task,
        )

        resp = client.post(
            "/ai-governance/mlflow/explain",
            json={
                "user_id": SERVICE_UID,
                "run_id": "run-abc",
                "input_data": [{"f1": 1.0}],
            },
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["task_id"] == "fake-task-id-abc"
        assert body["status"] == "queued"

    def test_explain_missing_run_id_returns_400(self, client, mock_service_user):
        resp = client.post(
            "/ai-governance/mlflow/explain",
            json={"user_id": SERVICE_UID, "input_data": []},
        )
        assert resp.status_code == 400


# ── Fairness ──────────────────────────────────────────────────────────────────


class TestFairnessEndpoints:
    @pytest.mark.parametrize("path,payload", [
        (
            "/ai-governance/fairness/aif360",
            {
                "dataset": {"df": [], "label_col": "y", "protected_attribute_names": ["race"]},
                "privileged_groups": [{"race": 1}],
                "unprivileged_groups": [{"race": 0}],
            },
        ),
        (
            "/ai-governance/fairness/fairlearn",
            {"X": [{"age": 30}], "y": [1], "sensitive_features": [0]},
        ),
        (
            "/ai-governance/fairness/aequitas",
            {"rows": [], "score_col": "pred", "label_col": "actual", "attr_cols": ["race"]},
        ),
    ])
    def test_fairness_dispatches_celery_for_service(
        self, path, payload, client, mock_service_user, monkeypatch
    ):
        fake_task = _fake_task()

        import ai_governance.tasks as tasks_mod

        for attr in ("run_fairness_aif360", "run_fairness_fairlearn", "run_fairness_aequitas"):
            monkeypatch.setattr(
                getattr(tasks_mod, attr),
                "delay",
                lambda *a, **kw: fake_task,
            )

        resp = client.post(path, json={"user_id": SERVICE_UID, **payload})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "queued"

    @pytest.mark.parametrize("path", [
        "/ai-governance/fairness/aif360",
        "/ai-governance/fairness/fairlearn",
        "/ai-governance/fairness/aequitas",
    ])
    def test_fairness_denied_for_admin(self, path, client, mock_admin_user):
        resp = client.post(path, json={"user_id": ADMIN_UID})
        assert resp.status_code == 403


# ── Giskard ───────────────────────────────────────────────────────────────────


class TestGiskardScan:
    def test_scan_dispatches_celery_without_project_key(
        self, client, mock_service_user, monkeypatch
    ):
        """OSS scan: no project_key required — empty body dispatches a task
        that runs against the built-in sample dataset."""
        fake_task = _fake_task()

        import ai_governance.tasks as tasks_mod

        monkeypatch.setattr(
            tasks_mod.run_giskard_scan,
            "delay",
            lambda **kw: fake_task,
        )

        resp = client.post("/ai-governance/giskard/scan", json={"user_id": SERVICE_UID})
        assert resp.status_code == 200
        assert resp.get_json()["task_id"] == "fake-task-id-abc"

    def test_scan_accepts_legacy_project_key_field(
        self, client, mock_service_user, monkeypatch
    ):
        """Old frontends may still send project_key — it should be ignored,
        not rejected, so a deploy can roll forward backend-first."""
        fake_task = _fake_task()

        import ai_governance.tasks as tasks_mod

        monkeypatch.setattr(
            tasks_mod.run_giskard_scan,
            "delay",
            lambda **kw: fake_task,
        )

        resp = client.post(
            "/ai-governance/giskard/scan",
            json={"user_id": SERVICE_UID, "project_key": "legacy-ignored"},
        )
        assert resp.status_code == 200

    def test_scan_denied_for_admin(self, client, mock_admin_user):
        resp = client.post(
            "/ai-governance/giskard/scan",
            json={"user_id": ADMIN_UID},
        )
        assert resp.status_code == 403


# ── DeepEval ──────────────────────────────────────────────────────────────────


class TestDeepEvalRun:
    def test_run_dispatches_celery(self, client, mock_service_user, monkeypatch):
        fake_task = _fake_task()

        import ai_governance.tasks as tasks_mod

        monkeypatch.setattr(
            tasks_mod.run_deepeval,
            "delay",
            lambda **kw: fake_task,
        )

        resp = client.post(
            "/ai-governance/deepeval/run",
            json={
                "user_id": SERVICE_UID,
                "test_cases": [{"input": "q", "actual_output": "a"}],
                "metric_names": ["answer_relevancy"],
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "queued"

    def test_run_missing_fields_returns_400(self, client, mock_service_user):
        resp = client.post(
            "/ai-governance/deepeval/run",
            json={"user_id": SERVICE_UID, "test_cases": []},
        )
        assert resp.status_code == 400

    def test_run_denied_for_admin(self, client, mock_admin_user):
        resp = client.post(
            "/ai-governance/deepeval/run",
            json={"user_id": ADMIN_UID, "test_cases": [], "metric_names": []},
        )
        assert resp.status_code == 403
