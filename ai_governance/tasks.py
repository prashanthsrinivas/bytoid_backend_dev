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

import logging

from utils.celery_base import celery

logger = logging.getLogger(__name__)


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


# ── AI Governance scan (platform-wide sweep) ──────────────────────────────────
#
# Fan-out shape: scan_platform -> chord(group(scan_user x N), aggregate_scan).
# Failure isolation: scan_user NEVER re-raises (a bad user must not abort the
# chord), so the aggregate callback always fires.  These tasks run on the
# dedicated low-concurrency ``ai_governance_scan`` queue to protect the shared
# Bedrock quota the live RAG model also uses.


@celery.task(
    bind=True,
    max_retries=0,  # scan_one_user swallows its own errors; no retry storms
    name="tasks.ai_governance.scan_user",
    queue="ai_governance_scan",
    soft_time_limit=900,
    rate_limit="10/m",
)
def scan_user_task(
    self,
    user_id: str,
    org_admin_id: str,
    modes: list,
    sample_size: int,
    max_questions: int,
    run_id: str,
) -> dict:
    """Scan one user and persist the result. Returns the result dict for the
    chord callback; never raises (so the sweep always completes)."""
    from ai_governance.scan_orchestrator import scan_one_user
    from ai_governance.scan_results_store import record_user_result
    from services.audit_log_service import (
        AI_GOVSCAN_USER_COMPLETED,
        AI_GOVSCAN_USER_FAILED,
        log_audit_event,
    )

    try:
        result = scan_one_user(
            user_id,
            modes=modes,
            sample_size=sample_size,
            max_questions=max_questions,
            org_admin_id=org_admin_id,
        )
    except Exception as exc:  # defensive — scan_one_user is already isolated
        result = {
            "user_id": user_id,
            "org_admin_id": org_admin_id,
            "status": "error",
            "modes": {},
            "detail": str(exc),
        }

    try:
        record_user_result(run_id, result)
    except Exception:
        # persistence failure must not break the chord
        logger.warning("scan_user_task: record_user_result failed", exc_info=True)

    action = (
        AI_GOVSCAN_USER_FAILED
        if result.get("status") == "error"
        else AI_GOVSCAN_USER_COMPLETED
    )
    log_audit_event(
        action,
        endpoint="/ai-governance/scan/user",
        ip="celery",
        status=result.get("status", "ok"),
        actor_user_id=user_id,
        metadata={"run_id": run_id, "modes": modes},
    )
    return result


@celery.task(
    bind=True,
    name="tasks.ai_governance.aggregate_scan",
    queue="ai_governance_scan",
)
def aggregate_scan_task(self, per_user_results: list, run_id: str) -> dict:
    """Chord callback: roll up per-user results and finalize the run."""
    from ai_governance.scan_orchestrator import aggregate_results
    from ai_governance.scan_results_store import finalize_run
    from services.audit_log_service import AI_GOVSCAN_BATCH_COMPLETED, log_audit_event

    cleaned = [r for r in (per_user_results or []) if isinstance(r, dict)]
    summary = aggregate_results(cleaned)
    finalize_run(run_id, summary, status="completed")
    log_audit_event(
        AI_GOVSCAN_BATCH_COMPLETED,
        endpoint="/ai-governance/scan/platform",
        ip="celery",
        status="success",
        actor_user_id="system",
        metadata={"run_id": run_id, "user_count": summary.get("user_count", 0)},
    )
    return {"run_id": run_id, "summary": summary}


@celery.task(
    bind=True,
    name="tasks.ai_governance.scan_platform",
    queue="ai_governance_scan",
)
def scan_platform_task(
    self,
    modes: list,
    sample_size: int,
    max_questions: int,
    run_id: str,
    user_limit=None,
    started_by: str = "system",
) -> dict:
    """Enumerate users and fan out a per-user scan chord.

    ``run_id`` is created up-front by the caller (route) for idempotency; when a
    beat-scheduled run passes ``None`` we mint one here.
    """
    from celery import chord

    from ai_governance.scan_orchestrator import ALL_MODES, enumerate_users
    from ai_governance.scan_results_store import (
        create_run,
        finalize_run,
        new_run_id,
        set_run_status,
    )
    from services.audit_log_service import AI_GOVSCAN_BATCH_STARTED, log_audit_event

    modes = modes or ALL_MODES
    if not run_id:
        run_id = new_run_id()
        create_run(run_id, scope="platform", modes=modes, started_by=started_by)

    users = enumerate_users(limit=user_limit)
    set_run_status(run_id, "running", user_count=len(users))
    log_audit_event(
        AI_GOVSCAN_BATCH_STARTED,
        endpoint="/ai-governance/scan/platform",
        ip="celery",
        status="running",
        actor_user_id=started_by,
        metadata={"run_id": run_id, "user_count": len(users), "modes": modes},
    )

    if not users:
        finalize_run(run_id, {"user_count": 0, "note": "no_users"}, status="completed")
        return {"run_id": run_id, "user_count": 0}

    header = [
        scan_user_task.s(
            u["user_id"],
            u.get("org_admin_id"),
            modes,
            sample_size,
            max_questions,
            run_id,
        )
        for u in users
    ]
    chord(header)(aggregate_scan_task.s(run_id))
    return {"run_id": run_id, "user_count": len(users)}
