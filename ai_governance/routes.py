"""AI Governance Flask Blueprint.

URL prefix: /ai-governance

RBAC tiers (enforced by @ai_governance_required):
  "guardrails" — any admin user OR service@bytoid.ca
  "superuser"  — ONLY service@bytoid.ca

Endpoints returning {"task_id", "status": "queued"} are non-blocking:
the actual work runs in a Celery task.  Poll GET /ai-governance/tasks/<id>
for results.
"""

from flask import Blueprint, g, jsonify, request

from ai_governance.middleware.rbac import ai_governance_required, _resolve_user_id
from services.audit_log_service import (
    AI_GUARDRAILS_CHECK,
    AI_GUARDRAILS_CONFIG_READ,
    AI_GUARDRAILS_RELOAD,
    AI_OBSERVABILITY_TRACES_READ,
    AI_OBSERVABILITY_SCORE_POSTED,
    AI_MLFLOW_RUNS_READ,
    AI_MLFLOW_RUN_LOGGED,
    AI_MLFLOW_EXPLAIN_RAN,
    AI_FAIRNESS_AIF360_ANALYZED,
    AI_FAIRNESS_FAIRLEARN_MITIGATED,
    AI_FAIRNESS_AEQUITAS_AUDITED,
    AI_GISKARD_SCAN_STARTED,
    AI_GISKARD_RESULTS_READ,
    AI_TRULENS_FEEDBACK_POSTED,
    AI_TRULENS_LEADERBOARD_READ,
    AI_DEEPEVAL_RUN,
    log_audit_event,
)

ai_governance_bp = Blueprint("ai_governance", __name__, url_prefix="/ai-governance")


# ── Helpers ───────────────────────────────────────────────────────────────────


def _audit(action, status="success", **kwargs):
    log_audit_event(
        action,
        endpoint=request.path,
        ip=request.remote_addr,
        status=status,
        actor_user_id=_resolve_user_id(),
        **kwargs,
    )
    g.audit_logged = True


# ── NeMo Guardrails ───────────────────────────────────────────────────────────


@ai_governance_bp.route("/guardrails/check", methods=["POST"])
@ai_governance_required(tier="guardrails")
def guardrails_check():
    """Run a prompt through NeMo Guardrails (returns immediately)."""
    import asyncio

    data = request.get_json(force=True) or {}
    prompt = data.get("prompt", "")
    if not prompt:
        return jsonify({"error": "prompt is required"}), 400

    from ai_governance.clients.guardrails_client import get_rails

    rails = get_rails()
    response = asyncio.run(
        rails.generate_async(messages=[{"role": "user", "content": prompt}])
    )
    _audit(AI_GUARDRAILS_CHECK)
    return jsonify({"response": response})


@ai_governance_bp.route("/guardrails/config", methods=["GET"])
@ai_governance_required(tier="guardrails")
def guardrails_config():
    """Return a summary of the active NeMo Guardrails configuration."""
    import os

    from ai_governance.clients.guardrails_client import NEMO_CONFIG_PATH

    config_path = os.path.abspath(NEMO_CONFIG_PATH)
    config_yml = os.path.join(config_path, "config.yml")
    flows_dir = os.path.join(config_path, "flows")

    flow_files = []
    if os.path.isdir(flows_dir):
        flow_files = [f for f in os.listdir(flows_dir) if f.endswith(".co")]

    _audit(AI_GUARDRAILS_CONFIG_READ)
    return jsonify({
        "config_path": config_path,
        "config_file_exists": os.path.isfile(config_yml),
        "flow_files": flow_files,
    })


@ai_governance_bp.route("/guardrails/reload", methods=["POST"])
@ai_governance_required(tier="superuser")
def guardrails_reload():
    """Hot-reload NeMo Guardrails config from disk (superuser only)."""
    from ai_governance.clients.guardrails_client import reload_rails

    reload_rails()
    _audit(AI_GUARDRAILS_RELOAD)
    return jsonify({"status": "reloaded"})


# ── Langfuse observability ────────────────────────────────────────────────────


@ai_governance_bp.route("/langfuse/traces", methods=["GET"])
@ai_governance_required(tier="superuser")
def langfuse_traces():
    """Fetch recent Langfuse traces."""
    from ai_governance.clients.langfuse_client import get_langfuse

    limit = min(int(request.args.get("limit", 20)), 100)
    lf = get_langfuse()
    traces = lf.fetch_traces(limit=limit)
    _audit(AI_OBSERVABILITY_TRACES_READ)
    return jsonify({"traces": [t.dict() for t in traces.data]})


@ai_governance_bp.route("/langfuse/score", methods=["POST"])
@ai_governance_required(tier="superuser")
def langfuse_score():
    """Post a manual evaluation score to a Langfuse trace."""
    from ai_governance.clients.langfuse_client import get_langfuse

    data = request.get_json(force=True) or {}
    required = {"trace_id", "name", "value"}
    if not required.issubset(data):
        return jsonify({"error": f"Required fields: {required}"}), 400

    lf = get_langfuse()
    lf.score(
        trace_id=data["trace_id"],
        name=data["name"],
        value=data["value"],
        comment=data.get("comment"),
    )
    _audit(AI_OBSERVABILITY_SCORE_POSTED, metadata={"trace_id": data["trace_id"]})
    return jsonify({"status": "scored"})


# ── MLflow / XAI ──────────────────────────────────────────────────────────────


@ai_governance_bp.route("/mlflow/runs", methods=["GET"])
@ai_governance_required(tier="superuser")
def mlflow_runs():
    """List MLflow experiment runs."""
    from ai_governance.clients.mlflow_client import get_mlflow

    mlflow = get_mlflow()
    experiment_name = request.args.get(
        "experiment", __import__("os").getenv("MLFLOW_EXPERIMENT_NAME", "ai_governance")
    )
    runs = mlflow.search_runs(experiment_names=[experiment_name])
    _audit(AI_MLFLOW_RUNS_READ)
    return jsonify({"runs": runs.to_dict(orient="records") if hasattr(runs, "to_dict") else []})


@ai_governance_bp.route("/mlflow/log", methods=["POST"])
@ai_governance_required(tier="superuser")
def mlflow_log():
    """Log a new MLflow run with parameters and metrics."""
    from ai_governance.clients.mlflow_client import get_mlflow

    data = request.get_json(force=True) or {}
    mlflow = get_mlflow()

    with mlflow.start_run(run_name=data.get("run_name", "ai_governance_run")) as run:
        if data.get("params"):
            mlflow.log_params(data["params"])
        if data.get("metrics"):
            mlflow.log_metrics(data["metrics"])
        if data.get("tags"):
            mlflow.set_tags(data["tags"])
        run_id = run.info.run_id

    _audit(AI_MLFLOW_RUN_LOGGED, metadata={"run_id": run_id})
    return jsonify({"run_id": run_id, "status": "logged"})


@ai_governance_bp.route("/mlflow/explain", methods=["POST"])
@ai_governance_required(tier="superuser")
def mlflow_explain():
    """Run SHAP explanation for a logged MLflow model (dispatched to Celery)."""
    from ai_governance.tasks import run_mlflow_explain

    data = request.get_json(force=True) or {}
    if not data.get("run_id") or not data.get("input_data"):
        return jsonify({"error": "run_id and input_data are required"}), 400

    task = run_mlflow_explain.delay(
        run_id=data["run_id"],
        input_data=data["input_data"],
        user_id=_resolve_user_id(),
    )
    _audit(AI_MLFLOW_EXPLAIN_RAN, status="queued")
    return jsonify({"task_id": task.id, "status": "queued"})


# ── Fairness / Bias / Equity ──────────────────────────────────────────────────


@ai_governance_bp.route("/fairness/aif360", methods=["POST"])
@ai_governance_required(tier="superuser")
def fairness_aif360():
    """Run an AIF360 bias analysis (dispatched to Celery)."""
    from ai_governance.tasks import run_fairness_aif360

    data = request.get_json(force=True) or {}
    required = {"dataset", "privileged_groups", "unprivileged_groups"}
    if not required.issubset(data):
        return jsonify({"error": f"Required fields: {required}"}), 400

    task = run_fairness_aif360.delay(
        dataset_dict=data["dataset"],
        privileged_groups=data["privileged_groups"],
        unprivileged_groups=data["unprivileged_groups"],
        user_id=_resolve_user_id(),
    )
    _audit(AI_FAIRNESS_AIF360_ANALYZED, status="queued")
    return jsonify({"task_id": task.id, "status": "queued"})


@ai_governance_bp.route("/fairness/fairlearn", methods=["POST"])
@ai_governance_required(tier="superuser")
def fairness_fairlearn():
    """Apply Fairlearn mitigation (dispatched to Celery)."""
    from ai_governance.tasks import run_fairness_fairlearn

    data = request.get_json(force=True) or {}
    required = {"X", "y", "sensitive_features"}
    if not required.issubset(data):
        return jsonify({"error": f"Required fields: {required}"}), 400

    task = run_fairness_fairlearn.delay(
        X_dict=data["X"],
        y=data["y"],
        sensitive_features=data["sensitive_features"],
        estimator_config=data.get("estimator_config", {}),
        user_id=_resolve_user_id(),
    )
    _audit(AI_FAIRNESS_FAIRLEARN_MITIGATED, status="queued")
    return jsonify({"task_id": task.id, "status": "queued"})


@ai_governance_bp.route("/fairness/aequitas", methods=["POST"])
@ai_governance_required(tier="superuser")
def fairness_aequitas():
    """Run an Aequitas bias audit (dispatched to Celery)."""
    from ai_governance.tasks import run_fairness_aequitas

    data = request.get_json(force=True) or {}
    required = {"rows", "score_col", "label_col", "attr_cols"}
    if not required.issubset(data):
        return jsonify({"error": f"Required fields: {required}"}), 400

    task = run_fairness_aequitas.delay(
        df_dict=data["rows"],
        score_col=data["score_col"],
        label_col=data["label_col"],
        attr_cols=data["attr_cols"],
        user_id=_resolve_user_id(),
    )
    _audit(AI_FAIRNESS_AEQUITAS_AUDITED, status="queued")
    return jsonify({"task_id": task.id, "status": "queued"})


# ── Giskard ───────────────────────────────────────────────────────────────────


@ai_governance_bp.route("/giskard/scan", methods=["POST"])
@ai_governance_required(tier="superuser")
def giskard_scan():
    """Launch a Giskard vulnerability scan (dispatched to Celery)."""
    from ai_governance.tasks import run_giskard_scan

    data = request.get_json(force=True) or {}
    if not data.get("project_key"):
        return jsonify({"error": "project_key is required"}), 400

    task = run_giskard_scan.delay(
        project_key=data["project_key"],
        model_config=data.get("model_config", {}),
        dataset_config=data.get("dataset_config", {}),
        user_id=_resolve_user_id(),
    )
    _audit(AI_GISKARD_SCAN_STARTED, status="queued")
    return jsonify({"task_id": task.id, "status": "queued"})


@ai_governance_bp.route("/giskard/results/<task_id>", methods=["GET"])
@ai_governance_required(tier="superuser")
def giskard_results(task_id):
    """Poll for Giskard scan results by Celery task ID."""
    from celery.result import AsyncResult

    from utils.celery_base import celery

    result = AsyncResult(task_id, app=celery)
    _audit(AI_GISKARD_RESULTS_READ, metadata={"task_id": task_id})
    return jsonify({
        "task_id": task_id,
        "status": result.status,
        "result": result.result if result.ready() else None,
    })


# ── TruLens ───────────────────────────────────────────────────────────────────


@ai_governance_bp.route("/trulens/feedback", methods=["POST"])
@ai_governance_required(tier="superuser")
def trulens_feedback():
    """Record a TruLens feedback score."""
    from ai_governance.clients.giskard_client import get_trulens_session
    from trulens.core.schema import feedback as tl_feedback

    data = request.get_json(force=True) or {}
    required = {"app_id", "record_id", "feedback_name", "result"}
    if not required.issubset(data):
        return jsonify({"error": f"Required fields: {required}"}), 400

    session = get_trulens_session()
    session.add_feedback(
        tl_feedback.FeedbackResult(
            feedback_definition_id=data["feedback_name"],
            record_id=data["record_id"],
            result=float(data["result"]),
            status=tl_feedback.FeedbackResultStatus.done,
        )
    )
    _audit(AI_TRULENS_FEEDBACK_POSTED, metadata={"record_id": data["record_id"]})
    return jsonify({"status": "recorded"})


@ai_governance_bp.route("/trulens/leaderboard", methods=["GET"])
@ai_governance_required(tier="superuser")
def trulens_leaderboard():
    """Fetch the TruLens app leaderboard."""
    from ai_governance.clients.giskard_client import get_trulens_session

    session = get_trulens_session()
    lb = session.get_leaderboard()
    _audit(AI_TRULENS_LEADERBOARD_READ)
    return jsonify({"leaderboard": lb.to_dict(orient="records") if hasattr(lb, "to_dict") else []})


# ── DeepEval ──────────────────────────────────────────────────────────────────


@ai_governance_bp.route("/deepeval/run", methods=["POST"])
@ai_governance_required(tier="superuser")
def deepeval_run():
    """Trigger a DeepEval evaluation suite (dispatched to Celery)."""
    from ai_governance.tasks import run_deepeval

    data = request.get_json(force=True) or {}
    if not data.get("test_cases") or not data.get("metric_names"):
        return jsonify({"error": "test_cases and metric_names are required"}), 400

    task = run_deepeval.delay(
        test_cases=data["test_cases"],
        metric_names=data["metric_names"],
        user_id=_resolve_user_id(),
    )
    _audit(AI_DEEPEVAL_RUN, status="queued")
    return jsonify({"task_id": task.id, "status": "queued"})


# ── Task status polling (generic) ─────────────────────────────────────────────


@ai_governance_bp.route("/tasks/<task_id>", methods=["GET"])
@ai_governance_required(tier="superuser")
def task_status(task_id):
    """Poll any AI governance Celery task by ID."""
    from celery.result import AsyncResult

    from utils.celery_base import celery

    result = AsyncResult(task_id, app=celery)
    payload: dict = {"task_id": task_id, "status": result.status}
    if result.ready():
        if result.successful():
            payload["result"] = result.result
        else:
            payload["error"] = str(result.result)
    return jsonify(payload)
