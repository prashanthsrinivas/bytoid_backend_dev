"""GRC overview dashboard summary endpoints.

`GET /governance/summary`, `GET /risk/summary`, `GET /compliance/summary` — each
returns best-effort KPI metrics for its dashboard. Gated by
`grc.dashboard.view`; user resolved via `parse_composite_user_id`. The helper
computes every metric defensively, so these always return a (possibly partial)
object, never 500 on a single failing source.
"""

from functools import wraps

from flask import Blueprint, jsonify, request

from grc_dashboards import helper
from utils.base_logger import get_logger
from utils.normal import parse_composite_user_id
from utils.permission_required import permission_required_body

logger = get_logger(__name__)

grc_bp = Blueprint("grc_dashboards", __name__)


def _workspace_user_id():
    body = request.get_json(silent=True) or {}
    raw = body.get("user_id") or request.args.get("user_id")
    _, owner = parse_composite_user_id(raw)
    return owner


def _safe(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:  # pragma: no cover - defensive
            logger.exception("grc dashboard route error: %s", request.path)
            return jsonify({"error": "Internal error"}), 500

    return wrapper


@grc_bp.route("/governance/summary", methods=["GET"])
@permission_required_body("grc.dashboard.view")
@_safe
def governance_summary():
    user_id = _workspace_user_id()
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
    return jsonify(helper.governance_summary(user_id)), 200


@grc_bp.route("/risk/summary", methods=["GET"])
@permission_required_body("grc.dashboard.view")
@_safe
def risk_summary():
    user_id = _workspace_user_id()
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
    return jsonify(helper.risk_summary(user_id)), 200


@grc_bp.route("/compliance/summary", methods=["GET"])
@permission_required_body("grc.dashboard.view")
@_safe
def compliance_summary():
    user_id = _workspace_user_id()
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
    return jsonify(helper.compliance_summary(user_id)), 200
