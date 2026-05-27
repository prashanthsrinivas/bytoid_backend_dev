"""Unit Test Results module — comprehensive testing dashboard backend.

Endpoints (no URL prefix; the blueprint registers full paths inline,
matching the project convention):

    POST /tests/run                  → dispatch one Celery task per backend category
    GET  /tests/summary              → aggregated latest run across all categories
    GET  /tests/results/<category>   → per-category latest detail
    GET  /tests/status/<task_id>     → Celery AsyncResult state
    GET  /tests/history/<category>   → list archived runs for a category
    POST /tests/webhook/frontend     → HMAC-protected ingest from bytoiddev CI
    GET  /tests/categories           → static metadata for the dashboard

Access control: GET/POST endpoints require the request user to be in
ACCESSIBLE_IDS (matching the legacy /azure/run-tests pattern). Webhook is
authenticated solely by HMAC signature — no user context.
"""

import logging
from datetime import datetime, timezone

from flask import Blueprint, g, jsonify, request, session

from services.audit_log_service import log_audit_event
from tests_routes.categories import (
    ALL_CATEGORIES,
    BACKEND_CATEGORIES,
    FRONTEND_CATEGORIES,
    is_backend_category,
    is_delegated,
    is_locally_dispatchable,
    is_valid_category,
)
from tests_routes.result_store import (
    list_history,
    new_run_id,
    read_category_result,
    read_summary,
    write_category_result,
)
from tests_routes.webhook_auth import verify_hmac
from utils.app_configs import ACCESSIBLE_IDS

logger = logging.getLogger(__name__)

tests_bp = Blueprint("tests", __name__)

# Default Locust knobs — overridable per /tests/run request.
DEFAULT_LOAD_OPTIONS = {
    "target_url": None,  # resolved from app config at task time when None
    "users": 25,
    "spawn_rate": 5,
    "run_time": "30s",
}
DEFAULT_STRESS_OPTIONS = {
    "target_url": None,
    "max_users": 200,
    "spawn_rate": 10,
    "run_time": "2m",
}
DEFAULT_PERF_OPTIONS = {
    "target_url": None,
    "run_time": "20s",
}


def _extract_user_id():
    uid = getattr(g, "user_id", None)
    if uid:
        return uid
    try:
        uid = session.get("user_id")
    except RuntimeError:
        uid = None
    if uid:
        return uid
    body = request.get_json(silent=True) or {}
    return body.get("user_id") or request.args.get("user_id")


def _require_authorized_user():
    """Returns (user_id, error_response). Mirrors the legacy /azure/run-tests gate.

    TODO: migrate to @permission_required("tests.run" / "tests.view") once those
    permissions are registered for the relevant admin roles. For now this matches
    the existing pattern in azure_integration/routes.py.
    """
    user_id = _extract_user_id()
    if not user_id or user_id not in ACCESSIBLE_IDS:
        return None, (
            jsonify(
                {
                    "success": False,
                    "error": "Access restricted. Contact an administrator.",
                }
            ),
            403,
        )
    return user_id, None


def _dispatch_backend_task(category: str, run_id: str, options: dict):
    """Import Celery tasks lazily to avoid circular imports at module load time."""
    from utils import celery_base  # lazy import to break the import cycle

    if category == "backend_unit":
        return celery_base.run_backend_unit.delay(run_id)
    if category == "backend_integration":
        return celery_base.run_backend_integration.delay(run_id)
    if category == "backend_regression":
        return celery_base.run_backend_regression.delay(run_id)
    if category == "backend_crypto":
        return celery_base.run_backend_crypto.delay(run_id)
    if category == "backend_load":
        opts = {**DEFAULT_LOAD_OPTIONS, **options}
        return celery_base.run_backend_load.delay(
            run_id,
            opts["target_url"],
            opts["users"],
            opts["spawn_rate"],
            opts["run_time"],
        )
    if category == "backend_stress":
        opts = {**DEFAULT_STRESS_OPTIONS, **options}
        return celery_base.run_backend_stress.delay(
            run_id,
            opts["target_url"],
            opts["max_users"],
            opts["spawn_rate"],
            opts["run_time"],
        )
    if category == "backend_performance":
        opts = {**DEFAULT_PERF_OPTIONS, **options}
        return celery_base.run_backend_performance.delay(
            run_id, opts["target_url"], opts["run_time"]
        )
    raise ValueError(f"Unknown backend category: {category}")


@tests_bp.route("/tests/categories", methods=["GET"])
def tests_categories():
    """Static metadata so the frontend can render category cards without hard-coding."""
    return jsonify(
        {
            "success": True,
            "backend": BACKEND_CATEGORIES,
            "frontend": FRONTEND_CATEGORIES,
        }
    )


@tests_bp.route("/tests/summary", methods=["GET"])
def tests_summary():
    _, err = _require_authorized_user()
    if err:
        return err
    return jsonify({"success": True, **read_summary()})


@tests_bp.route("/tests/results/<category>", methods=["GET"])
def tests_results_for(category):
    _, err = _require_authorized_user()
    if err:
        return err
    if not is_valid_category(category):
        return jsonify({"success": False, "error": "Unknown category"}), 404
    payload = read_category_result(category)
    if payload is None:
        return (
            jsonify(
                {
                    "success": False,
                    "error": f"No results yet for category '{category}'",
                }
            ),
            404,
        )
    return jsonify({"success": True, "result": payload})


@tests_bp.route("/tests/history/<category>", methods=["GET"])
def tests_history(category):
    _, err = _require_authorized_user()
    if err:
        return err
    if not is_valid_category(category):
        return jsonify({"success": False, "error": "Unknown category"}), 404
    try:
        limit = int(request.args.get("limit", 25))
    except (TypeError, ValueError):
        limit = 25
    return jsonify(
        {"success": True, "category": category, "runs": list_history(category, limit)}
    )


@tests_bp.route("/tests/status/<task_id>", methods=["GET"])
def tests_status(task_id):
    _, err = _require_authorized_user()
    if err:
        return err
    from utils.celery_base import celery as celery_app  # lazy import

    try:
        result = celery_app.AsyncResult(task_id)
        state = result.state
        info = result.info if isinstance(result.info, (dict, str, int, float)) else None
    except Exception as exc:  # noqa: BLE001
        return jsonify({"success": False, "error": str(exc)}), 500
    return jsonify(
        {
            "success": True,
            "task_id": task_id,
            "state": state,
            "info": info,
        }
    )


@tests_bp.route("/tests/run", methods=["POST"])
def tests_run():
    user_id, err = _require_authorized_user()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    categories = data.get("categories") or []
    options = data.get("options") or {}

    if not isinstance(categories, list) or not categories:
        return (
            jsonify(
                {
                    "success": False,
                    "error": "Body must include 'categories' (non-empty list)",
                }
            ),
            400,
        )

    invalid = [c for c in categories if not is_backend_category(c)]
    if invalid:
        return (
            jsonify(
                {
                    "success": False,
                    "error": (
                        "Only backend categories can be dispatched from this "
                        "endpoint. Frontend categories run via GitHub Actions in "
                        "the bytoiddev repo and post results to /tests/webhook/"
                        "frontend."
                    ),
                    "invalid": invalid,
                }
            ),
            400,
        )

    run_id = new_run_id()
    dispatched = []
    failures = []
    for category in categories:
        # Phase 1: backend_security_* and backend_coverage are delegated to
        # the .github/workflows/security.yml CI workflow. They cannot be
        # triggered from this endpoint; the frontend should surface a
        # message indicating the workflow runs on push/PR.
        if is_delegated(category):
            failures.append(
                {
                    "category": category,
                    "error": (
                        "This category runs in the security.yml GitHub "
                        "Actions workflow. Trigger via a PR or "
                        "`gh workflow run security.yml`."
                    ),
                }
            )
            continue
        if not is_locally_dispatchable(category):
            failures.append(
                {
                    "category": category,
                    "error": "Category is not locally dispatchable.",
                }
            )
            continue
        try:
            async_result = _dispatch_backend_task(category, run_id, options)
            dispatched.append({"category": category, "task_id": async_result.id})
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to dispatch %s", category)
            failures.append({"category": category, "error": str(exc)})

    log_audit_event(
        action="TESTS_RUN_DISPATCHED",
        endpoint="/tests/run",
        ip=request.remote_addr,
        status="success" if not failures else "partial",
        actor_user_id=user_id,
        metadata={
            "run_id": run_id,
            "categories": categories,
            "dispatched": len(dispatched),
            "failed": len(failures),
        },
    )

    # success=True means "the request was accepted and processed". Per-category
    # dispatch failures live in `failures[]` so the frontend can keep polling
    # the tasks that DID start. If every category failed, dispatched=[] tells
    # the frontend nothing is running.
    return jsonify(
        {
            "success": True,
            "run_id": run_id,
            "dispatched": dispatched,
            "failures": failures,
        }
    )


@tests_bp.route("/tests/webhook/frontend", methods=["POST"])
@tests_bp.route("/tests/webhook/ci", methods=["POST"])
def tests_webhook_frontend():
    if not verify_hmac(request):
        log_audit_event(
            action="TESTS_WEBHOOK_REJECTED",
            endpoint=request.path,
            ip=request.remote_addr,
            status="failure",
            metadata={"reason": "invalid_signature"},
        )
        return jsonify({"success": False, "error": "Invalid signature"}), 401

    payload = request.get_json(silent=True) or {}
    category = payload.get("category")
    # Accept ANY delegated category — frontend_* from bytoiddev plus the
    # Phase 1 backend_security_* / backend_coverage runs from security.yml.
    # The path /tests/webhook/frontend is preserved as an alias for the
    # existing bytoiddev workflow; new callers should use /tests/webhook/ci.
    if not category or not is_delegated(category):
        return (
            jsonify(
                {"success": False, "error": "Invalid or non-delegated category"}
            ),
            400,
        )

    run_id = payload.get("run_id") or new_run_id()

    # Ensure baseline fields are present even if the frontend forgot them.
    payload.setdefault("category", category)
    payload.setdefault("run_id", run_id)
    payload.setdefault("started_at", datetime.now(timezone.utc).isoformat())
    payload.setdefault("finished_at", datetime.now(timezone.utc).isoformat())
    payload.setdefault("summary", {})
    payload.setdefault("status", "passed" if payload["summary"].get("failed", 0) == 0 else "failed")
    payload.setdefault("tests", [])
    payload.setdefault("metrics", None)

    try:
        write_category_result(category, run_id, payload)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to persist frontend webhook payload")
        return jsonify({"success": False, "error": str(exc)}), 500

    log_audit_event(
        action="TESTS_WEBHOOK_ACCEPTED",
        endpoint="/tests/webhook/frontend",
        ip=request.remote_addr,
        status="success",
        metadata={
            "category": category,
            "run_id": run_id,
            "summary": payload.get("summary"),
        },
    )
    return jsonify(
        {"success": True, "category": category, "run_id": run_id, "accepted": True}
    )


# Surface the full known-category set on import so a smoke check (`python -c
# "from tests_routes.routes import tests_bp"`) catches missing dependencies.
_ = ALL_CATEGORIES
