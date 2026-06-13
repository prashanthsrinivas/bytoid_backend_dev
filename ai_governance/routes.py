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
    AI_GUARDRAIL_RULE_CREATED,
    AI_GUARDRAIL_RULE_UPDATED,
    AI_GUARDRAIL_RULE_DELETED,
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
    AI_GOVSCAN_BATCH_STARTED,
    AI_GOVSCAN_RESULTS_READ,
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
    """Fetch recent Langfuse traces.

    When Langfuse is not configured (no LANGFUSE_PUBLIC_KEY/SECRET_KEY),
    returns an empty trace list with `configured: false` so the UI can
    render a "not configured" hint instead of an error toast."""
    from ai_governance.clients.langfuse_client import get_langfuse, is_configured

    if not is_configured():
        _audit(AI_OBSERVABILITY_TRACES_READ, status="not_configured")
        return jsonify({
            "traces": [],
            "configured": False,
            "message": "Langfuse not configured. Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY (and optionally LANGFUSE_HOST) to enable observability.",
        })

    limit = min(int(request.args.get("limit", 20)), 100)
    lf = get_langfuse()
    if lf is None:
        return jsonify({"traces": [], "configured": False, "message": "Langfuse client failed to initialise."})

    try:
        traces = lf.fetch_traces(limit=limit)
        items = [t.dict() for t in getattr(traces, "data", [])]
    except Exception as exc:
        return jsonify({"traces": [], "configured": True, "error": str(exc)}), 200

    _audit(AI_OBSERVABILITY_TRACES_READ)
    return jsonify({"traces": items, "configured": True})


@ai_governance_bp.route("/langfuse/score", methods=["POST"])
@ai_governance_required(tier="superuser")
def langfuse_score():
    """Post a manual evaluation score to a Langfuse trace."""
    try:
        from ai_governance.clients.langfuse_client import get_langfuse, is_configured
    except Exception:
        return jsonify({"available": False, "error": "Langfuse observability is not configured"}), 200

    if not is_configured():
        return jsonify({"available": False, "error": "Langfuse observability is not configured"}), 200

    data = request.get_json(silent=True) or {}
    required = {"trace_id", "name", "value"}
    if not required.issubset(data):
        return jsonify({"error": f"Required fields: {required}"}), 400

    lf = get_langfuse()
    if lf is None:
        return jsonify({"available": False, "error": "Langfuse client failed to initialise"}), 200

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
    """List MLflow experiment runs.

    Returns rows in the shape the frontend expects:
        {"runs": [{"run_id": str, "run_name": str, "status": str,
                   "start_time": str|None, ...all tags/metrics...}]}
    """
    from ai_governance.clients.mlflow_client import get_mlflow, get_experiment_name

    mlflow = get_mlflow()
    experiment_name = request.args.get("experiment", get_experiment_name())

    runs_df = mlflow.search_runs(experiment_names=[experiment_name])
    runs: list[dict] = []
    if hasattr(runs_df, "to_dict") and not runs_df.empty:
        for record in runs_df.to_dict(orient="records"):
            runs.append({
                "run_id": record.get("run_id", ""),
                "run_name": record.get("tags.mlflow.runName") or record.get("run_name") or "(unnamed)",
                "status": record.get("status", "UNKNOWN"),
                "start_time": str(record.get("start_time")) if record.get("start_time") is not None else None,
                **{k: v for k, v in record.items() if k.startswith("metrics.") or k.startswith("params.")},
            })

    _audit(AI_MLFLOW_RUNS_READ)
    return jsonify({"runs": runs, "experiment": experiment_name})


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
    """Run an AIF360 bias analysis (dispatched to Celery).

    When the request body is empty the built-in sample dataset is used
    (sex as the protected attribute, income as the label) so the
    frontend's "Submit aif360 job" button always produces real metrics.
    Caller may still pass {dataset, privileged_groups, unprivileged_groups}
    to scan their own data."""
    from ai_governance.clients.fairness_client import sample_fairness_dataset
    from ai_governance.tasks import run_fairness_aif360

    data = request.get_json(force=True) or {}
    if {"dataset", "privileged_groups", "unprivileged_groups"}.issubset(data):
        dataset_dict = data["dataset"]
        privileged = data["privileged_groups"]
        unprivileged = data["unprivileged_groups"]
    else:
        sample = sample_fairness_dataset()
        dataset_dict = {
            "df": sample["rows"],
            "label_col": sample["label_col"],
            "favorable_label": 1,
            "protected_attribute_names": [sample["protected_attribute"]],
        }
        privileged = [{sample["protected_attribute"]: 1}]
        unprivileged = [{sample["protected_attribute"]: 0}]

    task = run_fairness_aif360.delay(
        dataset_dict=dataset_dict,
        privileged_groups=privileged,
        unprivileged_groups=unprivileged,
        user_id=_resolve_user_id(),
    )
    _audit(AI_FAIRNESS_AIF360_ANALYZED, status="queued")
    return jsonify({"task_id": task.id, "status": "queued"})


@ai_governance_bp.route("/fairness/fairlearn", methods=["POST"])
@ai_governance_required(tier="superuser")
def fairness_fairlearn():
    """Apply Fairlearn mitigation (dispatched to Celery).

    Falls back to the built-in sample dataset when X/y/sensitive_features
    are not supplied."""
    from ai_governance.clients.fairness_client import sample_fairness_dataset
    from ai_governance.tasks import run_fairness_fairlearn

    data = request.get_json(force=True) or {}
    if {"X", "y", "sensitive_features"}.issubset(data):
        X = data["X"]
        y = data["y"]
        sensitive = data["sensitive_features"]
    else:
        sample = sample_fairness_dataset()
        feature_cols = sample["feature_cols"]
        X = [{c: row[c] for c in feature_cols} for row in sample["rows"]]
        y = [row[sample["label_col"]] for row in sample["rows"]]
        sensitive = [row[sample["protected_attribute"]] for row in sample["rows"]]

    task = run_fairness_fairlearn.delay(
        X_dict=X,
        y=y,
        sensitive_features=sensitive,
        estimator_config=data.get("estimator_config", {}),
        user_id=_resolve_user_id(),
    )
    _audit(AI_FAIRNESS_FAIRLEARN_MITIGATED, status="queued")
    return jsonify({"task_id": task.id, "status": "queued"})


@ai_governance_bp.route("/fairness/aequitas", methods=["POST"])
@ai_governance_required(tier="superuser")
def fairness_aequitas():
    """Run an Aequitas bias audit (dispatched to Celery).

    Falls back to the built-in sample dataset when rows/score/label/attr
    are not supplied."""
    from ai_governance.clients.fairness_client import sample_fairness_dataset
    from ai_governance.tasks import run_fairness_aequitas

    data = request.get_json(force=True) or {}
    if {"rows", "score_col", "label_col", "attr_cols"}.issubset(data):
        rows = data["rows"]
        score_col = data["score_col"]
        label_col = data["label_col"]
        attr_cols = data["attr_cols"]
    else:
        sample = sample_fairness_dataset()
        rows = sample["rows"]
        score_col = sample["score_col"]
        label_col = sample["label_col"]
        attr_cols = [sample["protected_attribute"]]

    task = run_fairness_aequitas.delay(
        df_dict=rows,
        score_col=score_col,
        label_col=label_col,
        attr_cols=attr_cols,
        user_id=_resolve_user_id(),
    )
    _audit(AI_FAIRNESS_AEQUITAS_AUDITED, status="queued")
    return jsonify({"task_id": task.id, "status": "queued"})


# ── Giskard ───────────────────────────────────────────────────────────────────


@ai_governance_bp.route("/giskard/scan", methods=["POST"])
@ai_governance_required(tier="superuser")
def giskard_scan():
    """Launch a Giskard OSS vulnerability scan (dispatched to Celery).

    Body (all optional):
      model_config:   see clients/giskard_client.run_local_giskard_scan
      dataset_config: same — when omitted a built-in sample is scanned.

    project_key was the old cloud-server field; it is now ignored if sent
    (kept for one release so older frontends do not 400)."""
    from ai_governance.tasks import run_giskard_scan

    data = request.get_json(force=True) or {}

    task = run_giskard_scan.delay(
        model_config=data.get("model_config"),
        dataset_config=data.get("dataset_config"),
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


# ── AI Governance scan (platform-wide sweep) ──────────────────────────────────


@ai_governance_bp.route("/scan/platform", methods=["POST"])
@ai_governance_required(tier="superuser")
def scan_platform():
    """Launch a platform-wide governance scan across all users (Celery chord).

    Body (all optional): ``modes`` (subset of
    ["tabular","prompt","raget","guardrail"]), ``sample_size``, ``max_questions``,
    ``user_limit``.  Rejected with 409 if a platform scan is already in flight."""
    from ai_governance.scan_orchestrator import ALL_MODES
    from ai_governance.scan_results_store import (
        create_run,
        has_active_platform_run,
        new_run_id,
    )
    from ai_governance.tasks import scan_platform_task

    if has_active_platform_run():
        return jsonify({"error": "a platform scan is already queued or running"}), 409

    data = request.get_json(force=True) or {}
    modes = data.get("modes") or ALL_MODES
    sample_size = int(data.get("sample_size", 200))
    max_questions = int(data.get("max_questions", 10))
    user_limit = data.get("user_limit")
    started_by = _resolve_user_id()

    run_id = new_run_id()
    create_run(run_id, scope="platform", modes=modes, started_by=started_by)
    task = scan_platform_task.delay(
        modes=modes,
        sample_size=sample_size,
        max_questions=max_questions,
        run_id=run_id,
        user_limit=user_limit,
        started_by=started_by or "system",
    )
    _audit(AI_GOVSCAN_BATCH_STARTED, status="queued", metadata={"run_id": run_id})
    return jsonify({"run_id": run_id, "task_id": task.id, "status": "queued"})


@ai_governance_bp.route("/scan/user/<user_id>", methods=["POST"])
@ai_governance_required(tier="superuser")
def scan_user(user_id):
    """Ad-hoc single-user scan (rollout / debugging). Same modes as the sweep."""
    from celery import chord

    from ai_governance.scan_orchestrator import ALL_MODES, enumerate_users
    from ai_governance.scan_results_store import create_run, new_run_id
    from ai_governance.tasks import aggregate_scan_task, scan_user_task

    data = request.get_json(force=True) or {}
    modes = data.get("modes") or ALL_MODES
    sample_size = int(data.get("sample_size", 200))
    max_questions = int(data.get("max_questions", 10))

    matches = enumerate_users(user_filter=[user_id])
    org_admin_id = matches[0]["org_admin_id"] if matches else user_id

    run_id = new_run_id()
    create_run(run_id, scope="user", modes=modes, started_by=_resolve_user_id())
    # One-element chord so the run is finalized with a summary like a sweep.
    result = chord(
        [scan_user_task.s(user_id, org_admin_id, modes, sample_size, max_questions, run_id)]
    )(aggregate_scan_task.s(run_id))
    _audit(
        AI_GOVSCAN_BATCH_STARTED,
        status="queued",
        metadata={"run_id": run_id, "user_id": user_id},
    )
    return jsonify({"run_id": run_id, "task_id": result.id, "status": "queued"})


@ai_governance_bp.route("/scan/runs", methods=["GET"])
@ai_governance_required(tier="superuser")
def scan_runs():
    """List recent scan runs (most recent first)."""
    from ai_governance.scan_results_store import list_runs

    runs = list_runs(limit=int(request.args.get("limit", 50)))
    _audit(AI_GOVSCAN_RESULTS_READ)
    return jsonify({"runs": runs})


@ai_governance_bp.route("/scan/runs/<run_id>", methods=["GET"])
@ai_governance_required(tier="superuser")
def scan_run_detail(run_id):
    """Aggregated platform/org rollup for one run."""
    from ai_governance.scan_results_store import get_run

    run = get_run(run_id)
    if not run:
        return jsonify({"error": "run not found"}), 404
    _audit(AI_GOVSCAN_RESULTS_READ, metadata={"run_id": run_id})
    return jsonify(run)


@ai_governance_bp.route("/scan/runs/<run_id>/users", methods=["GET"])
@ai_governance_required(tier="superuser")
def scan_run_users(run_id):
    """Per-user results for a run (paged; filter by ``org_admin_id``)."""
    from ai_governance.scan_results_store import list_user_results

    results = list_user_results(
        run_id,
        org_admin_id=request.args.get("org_admin_id"),
        limit=int(request.args.get("limit", 200)),
        offset=int(request.args.get("offset", 0)),
    )
    _audit(AI_GOVSCAN_RESULTS_READ, metadata={"run_id": run_id})
    return jsonify({"run_id": run_id, "results": results})


# ── TruLens ───────────────────────────────────────────────────────────────────


@ai_governance_bp.route("/trulens/feedback", methods=["POST"])
@ai_governance_required(tier="superuser")
def trulens_feedback():
    """Record a TruLens feedback score."""
    data = request.get_json(silent=True) or {}
    required = {"app_id", "record_id", "feedback_name", "result"}
    if not required.issubset(data):
        return jsonify({"error": f"Required fields: {required}"}), 400

    try:
        from ai_governance.clients.giskard_client import get_trulens_session
        from trulens.core.schema import feedback as tl_feedback

        session = get_trulens_session()
        session.add_feedback(
            tl_feedback.FeedbackResult(
                feedback_definition_id=data["feedback_name"],
                record_id=data["record_id"],
                result=float(data["result"]),
                status=tl_feedback.FeedbackResultStatus.done,
            )
        )
    except Exception:
        return jsonify({"available": False, "error": "TruLens is not configured"}), 200
    _audit(AI_TRULENS_FEEDBACK_POSTED, metadata={"record_id": data["record_id"]})
    return jsonify({"status": "recorded"})


@ai_governance_bp.route("/trulens/leaderboard", methods=["GET"])
@ai_governance_required(tier="superuser")
def trulens_leaderboard():
    """Fetch the TruLens app leaderboard."""
    try:
        from ai_governance.clients.giskard_client import get_trulens_session

        session = get_trulens_session()
        lb = session.get_leaderboard()
    except Exception:
        return jsonify({"available": False, "leaderboard": []}), 200
    _audit(AI_TRULENS_LEADERBOARD_READ)
    return jsonify({"available": True, "leaderboard": lb.to_dict(orient="records") if hasattr(lb, "to_dict") else []})


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


# ── Authoring metadata (rule types, directions, actions, PII/SPI entities) ────


@ai_governance_bp.route("/metadata", methods=["GET"])
@ai_governance_required(tier="guardrails")
def guardrail_metadata():
    """Return the catalog the frontend uses to render guardrail authoring
    controls.  All guardrail dropdowns and entity chips are driven by this
    response — adding a new rule type or PII/SPI entity is a backend-only
    change."""
    from ai_governance.metadata import get_catalog

    return jsonify(get_catalog())


# ── Structured guardrail rules (DB-backed, authored from the frontend) ────────


def _caller_org_id() -> str:
    """Org scope for rule CRUD — currently the caller's own user_id, which is
    how the audit log treats workspace ownership for self-access."""
    return _resolve_user_id() or "system"


@ai_governance_bp.route("/rules", methods=["GET"])
@ai_governance_required(tier="guardrails")
def list_guardrail_rules():
    from ai_governance.rules_store import list_rules

    rules = list_rules(_caller_org_id(), include_disabled=True)
    return jsonify({"rules": rules})


@ai_governance_bp.route("/rules", methods=["POST"])
@ai_governance_required(tier="guardrails")
def create_guardrail_rule():
    from ai_governance.rules_store import create_rule

    payload = request.get_json(force=True) or {}
    try:
        rule = create_rule(_caller_org_id(), payload, created_by=_resolve_user_id())
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    _audit(AI_GUARDRAIL_RULE_CREATED, metadata={"rule_id": rule.get("rule_id")})
    return jsonify(rule), 201


@ai_governance_bp.route("/rules/<rule_id>", methods=["GET"])
@ai_governance_required(tier="guardrails")
def get_guardrail_rule(rule_id):
    from ai_governance.rules_store import get_rule

    rule = get_rule(_caller_org_id(), rule_id)
    if not rule:
        return jsonify({"error": "not found"}), 404
    return jsonify(rule)


@ai_governance_bp.route("/rules/<rule_id>", methods=["PATCH"])
@ai_governance_required(tier="guardrails")
def update_guardrail_rule(rule_id):
    from ai_governance.rules_store import update_rule

    payload = request.get_json(force=True) or {}
    try:
        rule = update_rule(_caller_org_id(), rule_id, payload)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if rule is None:
        return jsonify({"error": "not found"}), 404
    _audit(AI_GUARDRAIL_RULE_UPDATED, metadata={"rule_id": rule_id})
    return jsonify(rule)


@ai_governance_bp.route("/rules/<rule_id>", methods=["DELETE"])
@ai_governance_required(tier="guardrails")
def delete_guardrail_rule(rule_id):
    from ai_governance.rules_store import delete_rule

    deleted = delete_rule(_caller_org_id(), rule_id)
    if not deleted:
        return jsonify({"error": "not found"}), 404
    _audit(AI_GUARDRAIL_RULE_DELETED, metadata={"rule_id": rule_id})
    return jsonify({"status": "deleted"})


@ai_governance_bp.route("/rules/test", methods=["POST"])
@ai_governance_required(tier="guardrails")
def test_guardrail_rule():
    """Dry-run the active rule set (or a provided rule) against a sample
    prompt without mutating anything.  Returns matched rules and what each
    would have done."""
    from ai_governance.enforcer import _EVALUATORS, _applies, build_ctx
    from ai_governance.rules_store import list_rules_cached

    data = request.get_json(force=True) or {}
    sample = data.get("prompt", "")
    direction = data.get("direction", "input")
    ad_hoc = data.get("rule")

    org = _caller_org_id()
    candidates = [ad_hoc] if ad_hoc else list_rules_cached(org)
    ctx = build_ctx(user_id=_resolve_user_id(), feature=data.get("feature"), model=data.get("model"))

    results = []
    for rule in candidates:
        if not rule:
            continue
        if not ad_hoc and not _applies(rule, direction, ctx.get("feature"), ctx.get("model")):
            continue
        evaluator = _EVALUATORS.get(rule.get("rule_type"))
        if evaluator is None:
            continue
        try:
            matches = evaluator(sample, rule, direction)
        except Exception as exc:
            matches = []
            results.append({"rule_id": rule.get("rule_id"), "error": str(exc)})
            continue
        if matches:
            results.append(
                {
                    "rule_id": rule.get("rule_id"),
                    "rule_name": rule.get("name"),
                    "action": rule.get("action", "audit"),
                    "matches": [{"excerpt": m.get("excerpt"), "replacement": m.get("replacement")} for m in matches],
                }
            )
    return jsonify({"direction": direction, "matched": results})


@ai_governance_bp.route("/violations", methods=["GET"])
@ai_governance_required(tier="guardrails")
def list_guardrail_violations():
    from ai_governance.rules_store import list_violations

    rows = list_violations(
        _caller_org_id(),
        limit=min(int(request.args.get("limit", 50)), 200),
        offset=int(request.args.get("offset", 0)),
        feature=request.args.get("feature"),
        rule_id=request.args.get("rule_id"),
    )
    return jsonify({"violations": rows})
