"""Strategy governance API.

Hierarchy: Strategic Objective → Program → Project. Projects link to policies/
procedures/standards and trackers; health rolls up from ``statement_tracker_refs``
so a CISO can drill from a failing objective to the exact failing tracker
row/statement.

Auth: every route is gated by ``@permission_required_body("strategy.*")`` which
authenticates the session caller and enforces org scope. Handlers additionally
validate that an assigned ``owner_user_id`` is within the workspace's org.
"""

from functools import wraps

from flask import Blueprint, g, jsonify, request, session

from strategy import helper
from utils.base_logger import get_logger
from utils.normal import parse_composite_user_id
from utils.permission_required import permission_required_body

logger = get_logger(__name__)

strategy_bp = Blueprint("strategy", __name__, url_prefix="/strategy")


# ── small request helpers ─────────────────────────────────────────────────────

def _json_error(msg, code):
    return jsonify({"error": msg}), code


def _body():
    return request.get_json(silent=True) or {}


def _raw_user_id():
    return _body().get("user_id") or request.args.get("user_id")


def _workspace_user_id():
    """The workspace/owner account being operated on (right side of ##SU##)."""
    _, owner = parse_composite_user_id(_raw_user_id())
    return owner


def _actor_id(default=None):
    """The authenticated caller (session), used for created_by / owner default."""
    return getattr(g, "user_id", None) or session.get("user_id") or default


def _safe(fn):
    """Translate exceptions into clean HTTP responses (never an unhandled 500)."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ValueError as exc:
            return _json_error(str(exc), 400)
        except ConnectionError:
            return _json_error("Service temporarily unavailable", 503)
        except Exception:  # pragma: no cover - defensive catch-all
            logger.exception("strategy route error: %s", request.path)
            return _json_error("Internal error", 500)

    return wrapper


def _resolve_context():
    """Return (workspace_user_id, org_id, actor_id) or raise ValueError(400-text)."""
    workspace = _workspace_user_id()
    if not workspace:
        raise ValueError("user_id is required")
    org_id = helper.resolve_org_key(workspace)
    actor = _actor_id(default=workspace)
    return workspace, org_id, actor


def _validated_owner(body, workspace):
    """Resolve and validate the assigned owner; defaults to the workspace user."""
    owner = (body.get("owner_user_id") or "").strip() or workspace
    if owner != workspace and not helper.user_in_scope(workspace, owner):
        return None
    return owner


# ── Objectives ────────────────────────────────────────────────────────────────

@strategy_bp.route("/objectives", methods=["GET"])
@permission_required_body("strategy.view")
@_safe
def list_objectives():
    _, org_id, _ = _resolve_context()
    return jsonify({"objectives": helper.list_objectives(org_id)}), 200


@strategy_bp.route("/objectives", methods=["POST"])
@permission_required_body("strategy.create")
@_safe
def create_objective():
    workspace, org_id, actor = _resolve_context()
    body = _body()
    if not (body.get("title") or "").strip():
        return _json_error("title is required", 400)
    owner = _validated_owner(body, workspace)
    if owner is None:
        return _json_error("owner_user_id is not within your organization", 403)
    obj = helper.create_objective(org_id, owner, actor, body)
    return jsonify(obj), 201


@strategy_bp.route("/objective/<oid>", methods=["GET"])
@permission_required_body("strategy.view")
@_safe
def get_objective(oid):
    _, org_id, _ = _resolve_context()
    obj = helper.get_objective(oid, org_id)
    return (jsonify(obj), 200) if obj else _json_error("Objective not found", 404)


@strategy_bp.route("/objective/<oid>", methods=["PUT", "PATCH"])
@permission_required_body("strategy.edit")
@_safe
def update_objective(oid):
    workspace, org_id, _ = _resolve_context()
    body = _body()
    if "owner_user_id" in body and body.get("owner_user_id"):
        if _validated_owner(body, workspace) is None:
            return _json_error("owner_user_id is not within your organization", 403)
    if not helper.get_objective(oid, org_id):
        return _json_error("Objective not found", 404)
    return jsonify(helper.update_objective(oid, org_id, body)), 200


@strategy_bp.route("/objective/<oid>", methods=["DELETE"])
@permission_required_body("strategy.delete")
@_safe
def delete_objective(oid):
    _, org_id, _ = _resolve_context()
    deleted = helper.delete_objective(oid, org_id)
    return jsonify({"deleted": deleted}), 200


@strategy_bp.route("/objective/<oid>/health", methods=["GET"])
@permission_required_body("strategy.view")
@_safe
def objective_health(oid):
    _, org_id, _ = _resolve_context()
    return jsonify(helper.compute_objective_health(oid, org_id)), 200


# ── Programs ──────────────────────────────────────────────────────────────────

@strategy_bp.route("/programs", methods=["GET"])
@permission_required_body("strategy.view")
@_safe
def list_programs():
    _, org_id, _ = _resolve_context()
    objective_id = request.args.get("objective_id")
    return jsonify({"programs": helper.list_programs(org_id, objective_id)}), 200


@strategy_bp.route("/programs", methods=["POST"])
@permission_required_body("strategy.create")
@_safe
def create_program():
    workspace, org_id, actor = _resolve_context()
    body = _body()
    if not (body.get("name") or "").strip():
        return _json_error("name is required", 400)
    if not (body.get("objective_id") or "").strip():
        return _json_error("objective_id is required", 400)
    owner = _validated_owner(body, workspace)
    if owner is None:
        return _json_error("owner_user_id is not within your organization", 403)
    return jsonify(helper.create_program(org_id, owner, actor, body)), 201


@strategy_bp.route("/program/<pid>", methods=["GET"])
@permission_required_body("strategy.view")
@_safe
def get_program(pid):
    _, org_id, _ = _resolve_context()
    prog = helper.get_program(pid, org_id)
    return (jsonify(prog), 200) if prog else _json_error("Program not found", 404)


@strategy_bp.route("/program/<pid>", methods=["PUT", "PATCH"])
@permission_required_body("strategy.edit")
@_safe
def update_program(pid):
    workspace, org_id, _ = _resolve_context()
    body = _body()
    if "owner_user_id" in body and body.get("owner_user_id"):
        if _validated_owner(body, workspace) is None:
            return _json_error("owner_user_id is not within your organization", 403)
    if not helper.get_program(pid, org_id):
        return _json_error("Program not found", 404)
    return jsonify(helper.update_program(pid, org_id, body)), 200


@strategy_bp.route("/program/<pid>", methods=["DELETE"])
@permission_required_body("strategy.delete")
@_safe
def delete_program(pid):
    _, org_id, _ = _resolve_context()
    return jsonify({"deleted": helper.delete_program(pid, org_id)}), 200


# Program links (policies & standards) -----------------------------------------

@strategy_bp.route("/program/<pid>/links", methods=["GET"])
@permission_required_body("strategy.view")
@_safe
def program_links(pid):
    _, org_id, _ = _resolve_context()
    if not helper.get_program(pid, org_id):
        return _json_error("Program not found", 404)
    return jsonify(helper.get_program_links(pid)), 200


@strategy_bp.route("/program/<pid>/link-doc", methods=["POST"])
@permission_required_body("strategy.edit")
@_safe
def link_program_doc(pid):
    """Link a policy or standard to a program. Procedures attach to projects."""
    _, org_id, _ = _resolve_context()
    body = _body()
    policy_id = (body.get("policy_id") or "").strip()
    if not policy_id:
        return _json_error("policy_id is required", 400)
    if not helper.get_program(pid, org_id):
        return _json_error("Program not found", 404)
    return jsonify(helper.link_program_doc(pid, policy_id, body.get("doc_type", "policy"))), 200


@strategy_bp.route("/program/<pid>/unlink-doc", methods=["POST"])
@permission_required_body("strategy.edit")
@_safe
def unlink_program_doc(pid):
    _, org_id, _ = _resolve_context()
    body = _body()
    policy_id = (body.get("policy_id") or "").strip()
    if not policy_id:
        return _json_error("policy_id is required", 400)
    return jsonify({"deleted": helper.unlink_program_doc(pid, policy_id)}), 200


# ── Projects ──────────────────────────────────────────────────────────────────

@strategy_bp.route("/projects", methods=["GET"])
@permission_required_body("strategy.view")
@_safe
def list_projects():
    _, org_id, _ = _resolve_context()
    return jsonify({
        "projects": helper.list_projects(
            org_id,
            request.args.get("objective_id"),
            request.args.get("program_id"),
        )
    }), 200


@strategy_bp.route("/projects", methods=["POST"])
@permission_required_body("strategy.create")
@_safe
def create_project():
    workspace, org_id, actor = _resolve_context()
    body = _body()
    if not (body.get("name") or "").strip():
        return _json_error("name is required", 400)
    if not (body.get("objective_id") or "").strip():
        return _json_error("objective_id is required", 400)
    owner = _validated_owner(body, workspace)
    if owner is None:
        return _json_error("owner_user_id is not within your organization", 403)
    return jsonify(helper.create_project(org_id, owner, actor, body)), 201


@strategy_bp.route("/project/<pid>", methods=["GET"])
@permission_required_body("strategy.view")
@_safe
def get_project(pid):
    _, org_id, _ = _resolve_context()
    proj = helper.get_project(pid, org_id)
    return (jsonify(proj), 200) if proj else _json_error("Project not found", 404)


@strategy_bp.route("/project/<pid>", methods=["PUT", "PATCH"])
@permission_required_body("strategy.edit")
@_safe
def update_project(pid):
    workspace, org_id, _ = _resolve_context()
    body = _body()
    if "owner_user_id" in body and body.get("owner_user_id"):
        if _validated_owner(body, workspace) is None:
            return _json_error("owner_user_id is not within your organization", 403)
    if not helper.get_project(pid, org_id):
        return _json_error("Project not found", 404)
    return jsonify(helper.update_project(pid, org_id, body)), 200


@strategy_bp.route("/project/<pid>", methods=["DELETE"])
@permission_required_body("strategy.delete")
@_safe
def delete_project(pid):
    _, org_id, _ = _resolve_context()
    return jsonify({"deleted": helper.delete_project(pid, org_id)}), 200


# Project links ----------------------------------------------------------------

@strategy_bp.route("/project/<pid>/links", methods=["GET"])
@permission_required_body("strategy.view")
@_safe
def project_links(pid):
    _, org_id, _ = _resolve_context()
    if not helper.get_project(pid, org_id):
        return _json_error("Project not found", 404)
    return jsonify(helper.get_project_links(pid)), 200


@strategy_bp.route("/project/<pid>/link-doc", methods=["POST"])
@permission_required_body("strategy.edit")
@_safe
def link_doc(pid):
    """Link a procedure to a project. Policies/standards attach to programs."""
    _, org_id, _ = _resolve_context()
    body = _body()
    policy_id = (body.get("policy_id") or "").strip()
    if not policy_id:
        return _json_error("policy_id is required", 400)
    if not helper.get_project(pid, org_id):
        return _json_error("Project not found", 404)
    return jsonify(helper.link_doc(pid, policy_id, body.get("doc_type", "procedure"))), 200


@strategy_bp.route("/project/<pid>/unlink-doc", methods=["POST"])
@permission_required_body("strategy.edit")
@_safe
def unlink_doc(pid):
    _, org_id, _ = _resolve_context()
    body = _body()
    policy_id = (body.get("policy_id") or "").strip()
    if not policy_id:
        return _json_error("policy_id is required", 400)
    return jsonify({"deleted": helper.unlink_doc(pid, policy_id)}), 200


@strategy_bp.route("/project/<pid>/link-tracker", methods=["POST"])
@permission_required_body("strategy.edit")
@_safe
def link_tracker(pid):
    _, org_id, _ = _resolve_context()
    body = _body()
    tracker_id = (body.get("tracker_id") or "").strip()
    if not tracker_id:
        return _json_error("tracker_id is required", 400)
    if not helper.get_project(pid, org_id):
        return _json_error("Project not found", 404)
    return jsonify(helper.link_tracker(pid, tracker_id)), 200


@strategy_bp.route("/project/<pid>/unlink-tracker", methods=["POST"])
@permission_required_body("strategy.edit")
@_safe
def unlink_tracker(pid):
    _, org_id, _ = _resolve_context()
    body = _body()
    tracker_id = (body.get("tracker_id") or "").strip()
    if not tracker_id:
        return _json_error("tracker_id is required", 400)
    return jsonify({"deleted": helper.unlink_tracker(pid, tracker_id)}), 200


@strategy_bp.route("/project/<pid>/health", methods=["GET"])
@permission_required_body("strategy.view")
@_safe
def project_health(pid):
    _, org_id, _ = _resolve_context()
    if not helper.get_project(pid, org_id):
        return _json_error("Project not found", 404)
    return jsonify(helper.compute_project_health(pid)), 200


@strategy_bp.route("/project/<pid>/drilldown", methods=["GET"])
@permission_required_body("strategy.view")
@_safe
def project_drilldown(pid):
    _, org_id, _ = _resolve_context()
    result = helper.get_drilldown(pid, org_id)
    return (jsonify(result), 200) if result else _json_error("Project not found", 404)


# ── Roadmap ───────────────────────────────────────────────────────────────────

@strategy_bp.route("/roadmap", methods=["GET"])
@permission_required_body("strategy.view")
@_safe
def roadmap():
    _, org_id, _ = _resolve_context()
    return jsonify(helper.get_roadmap(org_id)), 200


# ── Milestones ────────────────────────────────────────────────────────────────

@strategy_bp.route("/milestones", methods=["POST"])
@permission_required_body("strategy.edit")
@_safe
def create_milestone():
    _resolve_context()
    body = _body()
    parent_type = (body.get("parent_type") or "").strip()
    parent_id = (body.get("parent_id") or "").strip()
    if parent_type not in ("objective", "program", "project"):
        return _json_error("parent_type must be objective|program|project", 400)
    if not parent_id:
        return _json_error("parent_id is required", 400)
    if not (body.get("title") or "").strip():
        return _json_error("title is required", 400)
    return jsonify(helper.create_milestone(parent_type, parent_id, body)), 201


@strategy_bp.route("/milestone/<mid>", methods=["DELETE"])
@permission_required_body("strategy.edit")
@_safe
def delete_milestone(mid):
    _resolve_context()
    return jsonify({"deleted": helper.delete_milestone(mid)}), 200
