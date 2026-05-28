"""Celery tasks for the AI Governance module.

Heavy ML/LLM operations that would block Gunicorn workers are offloaded here.
Each task:
  - Imports AI governance packages inside the task body (lazy, so the main
    Flask app image does not need them installed)
  - Calls log_audit_event on success/failure
  - Retries up to 3 times with exponential backoff

Route handlers call these tasks with .delay() and return {"task_id": ...,
"status": "queued"} immediately.  Callers poll GET /ai-governance/tasks/<id>.

Discovery: utils/celery_base.py imports this module as a side effect so
Celery workers find these tasks on startup.
"""

from utils.celery_base import celery


# ── Fairness tasks ────────────────────────────────────────────────────────────


@celery.task(
    bind=True,
    max_retries=3,
    name="tasks.ai_governance.run_fairness_aif360",
)
def run_fairness_aif360(
    self,
    dataset_dict: dict,
    privileged_groups: list,
    unprivileged_groups: list,
    user_id: str,
) -> dict:
    from ai_governance.clients.fairness_client import run_aif360_metrics
    from services.audit_log_service import (
        AI_FAIRNESS_AIF360_ANALYZED,
        log_audit_event,
    )

    try:
        result = run_aif360_metrics(dataset_dict, privileged_groups, unprivileged_groups)
        log_audit_event(
            AI_FAIRNESS_AIF360_ANALYZED,
            endpoint="/ai-governance/fairness/aif360",
            ip="celery",
            status="success",
            actor_user_id=user_id,
        )
        return result
    except Exception as exc:
        raise self.retry(exc=exc, countdown=min(2 ** self.request.retries, 300)) from exc


@celery.task(
    bind=True,
    max_retries=3,
    name="tasks.ai_governance.run_fairness_fairlearn",
)
def run_fairness_fairlearn(
    self,
    X_dict: list,
    y: list,
    sensitive_features: list,
    estimator_config: dict,
    user_id: str,
) -> dict:
    from ai_governance.clients.fairness_client import run_fairlearn_mitigation
    from services.audit_log_service import (
        AI_FAIRNESS_FAIRLEARN_MITIGATED,
        log_audit_event,
    )

    try:
        result = run_fairlearn_mitigation(X_dict, y, sensitive_features, estimator_config)
        log_audit_event(
            AI_FAIRNESS_FAIRLEARN_MITIGATED,
            endpoint="/ai-governance/fairness/fairlearn",
            ip="celery",
            status="success",
            actor_user_id=user_id,
        )
        return result
    except Exception as exc:
        raise self.retry(exc=exc, countdown=min(2 ** self.request.retries, 300)) from exc


@celery.task(
    bind=True,
    max_retries=3,
    name="tasks.ai_governance.run_fairness_aequitas",
)
def run_fairness_aequitas(
    self,
    df_dict: list,
    score_col: str,
    label_col: str,
    attr_cols: list,
    user_id: str,
) -> dict:
    from ai_governance.clients.fairness_client import run_aequitas_audit
    from services.audit_log_service import (
        AI_FAIRNESS_AEQUITAS_AUDITED,
        log_audit_event,
    )

    try:
        result = run_aequitas_audit(df_dict, score_col, label_col, attr_cols)
        log_audit_event(
            AI_FAIRNESS_AEQUITAS_AUDITED,
            endpoint="/ai-governance/fairness/aequitas",
            ip="celery",
            status="success",
            actor_user_id=user_id,
        )
        return result
    except Exception as exc:
        raise self.retry(exc=exc, countdown=min(2 ** self.request.retries, 300)) from exc


# ── Giskard scan task ─────────────────────────────────────────────────────────


@celery.task(
    bind=True,
    max_retries=2,
    name="tasks.ai_governance.run_giskard_scan",
)
def run_giskard_scan(
    self,
    model_config: dict | None,
    dataset_config: dict | None,
    user_id: str,
) -> dict:
    """Run a Giskard OSS vulnerability scan locally.

    No GiskardClient / no project key / no cloud account required.  See
    `ai_governance.clients.giskard_client.run_local_giskard_scan` for the
    accepted shapes of model_config and dataset_config.  When either is None,
    a built-in sample dataset is used so the scan always has something to
    chew on.
    """
    from ai_governance.clients.giskard_client import run_local_giskard_scan
    from services.audit_log_service import AI_GISKARD_SCAN_STARTED, log_audit_event

    try:
        result = run_local_giskard_scan(
            model_config=model_config,
            dataset_config=dataset_config,
        )
        log_audit_event(
            AI_GISKARD_SCAN_STARTED,
            endpoint="/ai-governance/giskard/scan",
            ip="celery",
            status="success",
            actor_user_id=user_id,
        )
        return result
    except Exception as exc:
        raise self.retry(exc=exc, countdown=min(2 ** self.request.retries, 300)) from exc


# ── DeepEval task ─────────────────────────────────────────────────────────────


@celery.task(
    bind=True,
    max_retries=2,
    name="tasks.ai_governance.run_deepeval",
)
def run_deepeval(
    self,
    test_cases: list,
    metric_names: list,
    user_id: str,
) -> list:
    from ai_governance.clients.giskard_client import run_deepeval_suite
    from services.audit_log_service import AI_DEEPEVAL_RUN, log_audit_event

    try:
        results = run_deepeval_suite(test_cases, metric_names)
        log_audit_event(
            AI_DEEPEVAL_RUN,
            endpoint="/ai-governance/deepeval/run",
            ip="celery",
            status="success",
            actor_user_id=user_id,
        )
        return results
    except Exception as exc:
        raise self.retry(exc=exc, countdown=min(2 ** self.request.retries, 300)) from exc


# ── MLflow SHAP explanation task ──────────────────────────────────────────────


@celery.task(
    bind=True,
    max_retries=2,
    name="tasks.ai_governance.run_mlflow_explain",
)
def run_mlflow_explain(
    self,
    run_id: str,
    input_data: list,
    user_id: str,
) -> dict:
    """Compute SHAP values for a logged MLflow model and store as artifact."""
    import shap
    import pandas as pd
    import mlflow

    from services.audit_log_service import AI_MLFLOW_EXPLAIN_RAN, log_audit_event

    try:
        model_uri = f"runs:/{run_id}/model"
        model = mlflow.pyfunc.load_model(model_uri)

        X = pd.DataFrame(input_data)
        explainer = shap.Explainer(model.predict, X)
        shap_values = explainer(X)

        shap_dict = {
            "shap_values": shap_values.values.tolist(),
            "base_values": shap_values.base_values.tolist(),
            "feature_names": list(X.columns),
        }

        with mlflow.start_run(run_id=run_id):
            mlflow.log_dict(shap_dict, "shap_explanation.json")

        log_audit_event(
            AI_MLFLOW_EXPLAIN_RAN,
            endpoint="/ai-governance/mlflow/explain",
            ip="celery",
            status="success",
            actor_user_id=user_id,
            metadata={"run_id": run_id},
        )
        return shap_dict
    except Exception as exc:
        raise self.retry(exc=exc, countdown=min(2 ** self.request.retries, 300)) from exc
