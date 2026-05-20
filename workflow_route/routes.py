"""Review and Approval Workflow — cross-artifact blueprint.

Endpoints:
  GET  /workflow/config
  PUT  /workflow/config/<doc_type>
  POST /workflow/submit
  POST /workflow/review
  POST /workflow/approve
  POST /workflow/publish
  GET  /workflow/inbox
  GET  /workflow/history/<workflow_id>
  POST /workflow/reassign
"""

import json
import uuid

import pymysql.cursors

from flask import Blueprint, g, jsonify, request

from services.audit_log_service import build_audit_actor, log_audit_event
from utils.app_configs import IS_DEV
from utils.base_logger import get_logger
from utils.normal import parse_composite_user_id
from utils.permission_required import permission_required_body
from workflow_route.state_machine import (
    DEFAULT_STATES_JSON,
    WorkflowConflictError,
    WorkflowNotFoundError,
    WorkflowTransitionError,
    bootstrap_schema,
    create_workflow,
    get_inbox,
    get_workflow,
    get_workflow_config,
    get_workflow_for_doc,
    get_workflow_for_doc_any_role,
    get_workflow_history,
    transition,
)
from db.rds_db import connect_to_rds

logger = get_logger(__name__, log_level="DEBUG" if IS_DEV else "INFO")
workflow_bp = Blueprint("workflow", __name__, url_prefix="/workflow")

try:
    bootstrap_schema()
except Exception as _bs_exc:
    logger.warning("workflow schema bootstrap failed: %s", _bs_exc)

# ── Audit action constants ────────────────────────────────────────────────────

WORKFLOW_SUBMITTED = "WORKFLOW_SUBMITTED"  # draft → quality_review
WORKFLOW_QUALITY_APPROVED = "WORKFLOW_QUALITY_APPROVED"  # quality_review → governance_review
WORKFLOW_QUALITY_SENT_BACK = "WORKFLOW_QUALITY_SENT_BACK"  # quality_review → draft
WORKFLOW_GOVERNANCE_APPROVED = "WORKFLOW_GOVERNANCE_APPROVED"  # governance_review → approval
WORKFLOW_GOVERNANCE_SENT_BACK = "WORKFLOW_GOVERNANCE_SENT_BACK"  # governance_review → quality_review
WORKFLOW_APPROVED = "WORKFLOW_APPROVED"  # approval → published
WORKFLOW_APPROVAL_SENT_BACK = "WORKFLOW_APPROVAL_SENT_BACK"  # approval → governance_review
WORKFLOW_PUBLISHED = "WORKFLOW_PUBLISHED"
WORKFLOW_REASSIGNED = "WORKFLOW_REASSIGNED"
# Legacy constants (kept for back-compat in callers / DB audit logs)
WORKFLOW_REVIEW_APPROVED = WORKFLOW_QUALITY_APPROVED
WORKFLOW_CHANGES_REQUESTED = WORKFLOW_QUALITY_SENT_BACK

# ── Helpers ───────────────────────────────────────────────────────────────────


def _get_user_org(user_id: str) -> str | None:
    """Resolve org identifier for a user — company_name first, launch_id_fk as fallback.

    Admin users who are the org root (no company_name, no launch_id_fk) use
    "launch:{user_id}" so their org_id matches what invited users carry in
    their own launch_id_fk field.
    """
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT company_name, launch_id_fk, user_type FROM users WHERE user_id=%s LIMIT 1",
                (user_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        company = (row["company_name"] or "").strip()
        if company:
            return company
        launch = (row["launch_id_fk"] or "").strip()
        if launch:
            return f"launch:{launch}"
        if row.get("user_type") == "admin":
            return f"launch:{user_id}"
        return None
    finally:
        conn.close()


def _notify(workflow: dict, event_type: str, comment: str | None = None, **kwargs):
    try:
        from services.workflow_notifications_service import notify_workflow_event
        notify_workflow_event(workflow, event_type, comment=comment, **kwargs)
    except Exception as exc:
        logger.error("workflow notification failed for %s: %s", event_type, exc)


# ── Config endpoints ──────────────────────────────────────────────────────────


@workflow_bp.route("/config", methods=["GET"])
@permission_required_body("workflow.config.manage")
def get_workflow_config_all():
    """List workflow config for all artifact types for the caller's org."""
    baseuser = request.args.get("user_id", "")
    logged_in, user_id = parse_composite_user_id(baseuser)
    org_id = _get_user_org(user_id)
    if not org_id:
        return jsonify({"error": "User org not found"}), 404

    doc_types = ["policy", "procedure", "runbook", "report"]
    configs = []
    for dt in doc_types:
        entry = get_workflow_config(org_id, dt)
        entry["doc_type"] = dt
        configs.append(entry)

    return jsonify({"org_id": org_id, "configs": configs}), 200


@workflow_bp.route("/config/<doc_type>", methods=["PUT"])
@permission_required_body("workflow.config.manage")
def update_workflow_config(doc_type: str):
    """Update workflow configuration for one artifact type."""
    body = request.get_json(silent=True) or {}
    baseuser = body.get("user_id", "")
    logged_in, user_id = parse_composite_user_id(baseuser)
    org_id = _get_user_org(user_id)
    if not org_id:
        return jsonify({"error": "User org not found"}), 404

    if doc_type not in ("policy", "procedure", "runbook", "report"):
        return jsonify({"error": f"Unknown doc_type: {doc_type}"}), 400

    assignment_mode = body.get("assignment_mode", "per_document")
    reviewer_role_id = body.get("reviewer_role_id")
    approver_role_id = body.get("approver_role_id")
    states_json = body.get("states_json", DEFAULT_STATES_JSON)

    conn = connect_to_rds()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO workflow_config
                   (org_id, doc_type, assignment_mode, reviewer_role_id, approver_role_id, states_json)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE
                     assignment_mode=VALUES(assignment_mode),
                     reviewer_role_id=VALUES(reviewer_role_id),
                     approver_role_id=VALUES(approver_role_id),
                     states_json=VALUES(states_json)""",
                (org_id, doc_type, assignment_mode, reviewer_role_id, approver_role_id, json.dumps(states_json)),
            )
        conn.commit()
    finally:
        conn.close()

    return jsonify({"status": "ok", "org_id": org_id, "doc_type": doc_type}), 200


# ── Submit ────────────────────────────────────────────────────────────────────


@workflow_bp.route("/submit", methods=["POST"])
@permission_required_body("workflow.submit")
def submit_for_review():
    """Submit a document for review. Transitions draft → quality_review.

    Body: { user_id, doc_type, doc_id, doc_version,
            quality_reviewer_user_id, governance_reviewer_user_id, approver_user_id,
            comment? }
    Legacy alias: reviewer_user_id → quality_reviewer_user_id.
    """
    body = request.get_json(silent=True) or {}
    baseuser = body.get("user_id", "")
    doc_type = body.get("doc_type")
    doc_id = body.get("doc_id")
    doc_version = body.get("doc_version", "1.0")
    quality_reviewer_user_id = body.get("quality_reviewer_user_id") or body.get("reviewer_user_id")
    governance_reviewer_user_id = body.get("governance_reviewer_user_id")
    approver_user_id = body.get("approver_user_id")
    comment = body.get("comment")

    if not all([baseuser, doc_type, doc_id]):
        return jsonify({"error": "user_id, doc_type, and doc_id are required"}), 400

    logged_in, user_id = parse_composite_user_id(baseuser)
    org_id = _get_user_org(user_id)
    if not org_id:
        return jsonify({"error": "User org not found"}), 404

    # Resolve reviewer/approver for role_based mode
    config = get_workflow_config(org_id, doc_type)
    if config["assignment_mode"] == "role_based" and not quality_reviewer_user_id:
        try:
            from shared_configuration import get_round_robin_user_for_resource
            quality_reviewer_user_id = get_round_robin_user_for_resource(
                config["reviewer_role_id"], doc_id
            )
        except Exception:
            pass
    if config["assignment_mode"] == "role_based" and not approver_user_id:
        try:
            from shared_configuration import get_round_robin_user_for_resource
            approver_user_id = get_round_robin_user_for_resource(
                config["approver_role_id"], doc_id
            )
        except Exception:
            pass

    # Per-document mode requires explicit assignments.
    if config["assignment_mode"] == "per_document":
        if not all([quality_reviewer_user_id, governance_reviewer_user_id, approver_user_id]):
            return jsonify({
                "error": "quality_reviewer_user_id, governance_reviewer_user_id, "
                         "and approver_user_id are required for per-document workflows"
            }), 400

    # Create or resume workflow
    existing = get_workflow_for_doc(doc_type, doc_id, doc_version)
    if existing:
        if existing["state"] != "draft":
            return jsonify({"error": f"Document is already in state '{existing['state']}'"}), 409
        try:
            wf = transition(
                existing["workflow_id"],
                existing["state_version"],
                "quality_review",
                user_id,
                comment=comment,
                quality_reviewer_user_id=quality_reviewer_user_id,
                governance_reviewer_user_id=governance_reviewer_user_id,
                approver_user_id=approver_user_id,
            )
        except (WorkflowConflictError, WorkflowTransitionError) as exc:
            return jsonify({"error": str(exc)}), 409
    else:
        wf = create_workflow(
            org_id=org_id,
            doc_type=doc_type,
            doc_id=doc_id,
            doc_version=doc_version,
            owner_user_id=user_id,
            quality_reviewer_user_id=quality_reviewer_user_id,
            governance_reviewer_user_id=governance_reviewer_user_id,
            approver_user_id=approver_user_id,
        )
        try:
            wf = transition(
                wf["workflow_id"], 1, "quality_review", user_id, comment=comment,
                quality_reviewer_user_id=quality_reviewer_user_id,
                governance_reviewer_user_id=governance_reviewer_user_id,
                approver_user_id=approver_user_id,
            )
        except (WorkflowConflictError, WorkflowTransitionError) as exc:
            return jsonify({"error": str(exc)}), 409

    _notify(wf, WORKFLOW_SUBMITTED, comment=comment)

    actor_uid, actor_email, behalf_uid, behalf_email = build_audit_actor(baseuser)
    log_audit_event(
        action=WORKFLOW_SUBMITTED,
        endpoint="/workflow/submit",
        ip=request.remote_addr,
        status="success",
        actor_user_id=actor_uid,
        actor_email=actor_email,
        acting_on_behalf_of_user_id=behalf_uid,
        acting_on_behalf_of_email=behalf_email,
        metadata={"workflow_id": wf["workflow_id"], "doc_type": doc_type, "doc_id": doc_id},
    )
    g.audit_logged = True

    return jsonify({"workflow_id": wf["workflow_id"], "state": wf["state"]}), 200


# ── Review (stage-aware) ──────────────────────────────────────────────────────

# Map (current_state, decision) → (next_state, audit_action, role_column)
_STAGE_TRANSITION_MAP = {
    ("quality_review", "approve"):    ("governance_review", "WORKFLOW_QUALITY_APPROVED",     "current_quality_reviewer"),
    ("quality_review", "send_back"):  ("draft",              "WORKFLOW_QUALITY_SENT_BACK",    "current_quality_reviewer"),
    ("governance_review", "approve"): ("approval",           "WORKFLOW_GOVERNANCE_APPROVED",  "current_governance_reviewer"),
    ("governance_review", "send_back"): ("quality_review",   "WORKFLOW_GOVERNANCE_SENT_BACK", "current_governance_reviewer"),
    ("approval", "approve"):          ("published",          "WORKFLOW_APPROVED",             "current_approver"),
    ("approval", "send_back"):        ("governance_review",  "WORKFLOW_APPROVAL_SENT_BACK",   "current_approver"),
}


@workflow_bp.route("/review", methods=["POST"])
@permission_required_body("workflow.review")
def review_document():
    """Stage-aware review action.

    Body: {
      user_id,
      workflow_id,
      state_version,
      stage: 'quality' | 'governance' | 'approval',
      decision: 'approve' | 'send_back',
      comment?     # required when decision='send_back'
    }
    """
    body = request.get_json(silent=True) or {}
    baseuser = body.get("user_id", "")
    workflow_id = body.get("workflow_id")
    state_version = body.get("state_version")
    stage = body.get("stage")
    decision = body.get("decision")
    comment = body.get("comment")

    if not all([baseuser, workflow_id, state_version is not None, stage, decision]):
        return jsonify({"error": "user_id, workflow_id, state_version, stage, decision are required"}), 400

    if stage not in ("quality", "governance", "approval"):
        return jsonify({"error": "stage must be 'quality', 'governance', or 'approval'"}), 400
    if decision not in ("approve", "send_back"):
        return jsonify({"error": "decision must be 'approve' or 'send_back'"}), 400
    if decision == "send_back" and not (comment or "").strip():
        return jsonify({"error": "comment is required when decision='send_back'"}), 400

    logged_in, user_id = parse_composite_user_id(baseuser)

    try:
        wf = get_workflow(workflow_id)
    except WorkflowNotFoundError:
        return jsonify({"error": "Workflow not found"}), 404

    # Map stage to the state the workflow must currently be in
    expected_state = {"quality": "quality_review", "governance": "governance_review", "approval": "approval"}[stage]
    if wf.get("state") != expected_state:
        return jsonify({
            "error": f"Workflow is in '{wf.get('state')}'; cannot act on stage '{stage}'"
        }), 409

    mapping = _STAGE_TRANSITION_MAP.get((expected_state, decision))
    if not mapping:
        return jsonify({"error": f"Unsupported transition: {expected_state} + {decision}"}), 400
    to_state, audit_action, role_col = mapping

    # Permission: the user must hold the role for this stage
    if wf.get(role_col) != user_id:
        return jsonify({"error": f"You are not the current {stage} reviewer for this workflow"}), 403

    try:
        updated = transition(wf["workflow_id"], int(state_version), to_state, user_id, comment=comment)
    except WorkflowConflictError as exc:
        return jsonify({"error": str(exc), "current_state_version": wf["state_version"]}), 409
    except WorkflowTransitionError as exc:
        return jsonify({"error": str(exc)}), 409

    _notify(updated, audit_action, comment=comment, previous_state=expected_state)

    actor_uid, actor_email, behalf_uid, behalf_email = build_audit_actor(baseuser)
    log_audit_event(
        action=audit_action, endpoint="/workflow/review", ip=request.remote_addr,
        status="success", actor_user_id=actor_uid, actor_email=actor_email,
        acting_on_behalf_of_user_id=behalf_uid, acting_on_behalf_of_email=behalf_email,
        metadata={"workflow_id": workflow_id, "stage": stage, "decision": decision},
    )
    g.audit_logged = True
    return jsonify({"workflow_id": workflow_id, "state": updated["state"], "state_version": updated["state_version"]}), 200


# ── Approve (legacy alias for stage=approval) ────────────────────────────────


@workflow_bp.route("/approve", methods=["POST"])
@permission_required_body("workflow.approve")
def approve_document():
    """Legacy approver endpoint. Internally routes through stage-aware /review logic.

    Body: { user_id, workflow_id, state_version, decision ('published'|'changes_requested'), comment? }
    Maps: decision='published' → stage=approval, decision=approve
          decision='changes_requested' → stage=approval, decision=send_back
    """
    body = request.get_json(silent=True) or {}
    legacy_decision = body.get("decision")
    if legacy_decision == "published":
        body["decision"] = "approve"
    elif legacy_decision == "changes_requested":
        body["decision"] = "send_back"
    body["stage"] = "approval"
    # Re-inject for downstream parsing
    request_json_override = body
    # Hack: call review_document by reconstructing the request handler logic inline.
    # Easier path: duplicate the dispatch.
    return _dispatch_review(body)


def _dispatch_review(body: dict):
    """Shared dispatch used by both /review and /approve legacy endpoint."""
    baseuser = body.get("user_id", "")
    workflow_id = body.get("workflow_id")
    state_version = body.get("state_version")
    stage = body.get("stage")
    decision = body.get("decision")
    comment = body.get("comment")

    if not all([baseuser, workflow_id, state_version is not None, stage, decision]):
        return jsonify({"error": "user_id, workflow_id, state_version, stage, decision are required"}), 400
    if stage not in ("quality", "governance", "approval"):
        return jsonify({"error": "stage must be 'quality', 'governance', or 'approval'"}), 400
    if decision not in ("approve", "send_back"):
        return jsonify({"error": "decision must be 'approve' or 'send_back'"}), 400

    logged_in, user_id = parse_composite_user_id(baseuser)
    try:
        wf = get_workflow(workflow_id)
    except WorkflowNotFoundError:
        return jsonify({"error": "Workflow not found"}), 404

    expected_state = {"quality": "quality_review", "governance": "governance_review", "approval": "approval"}[stage]
    if wf.get("state") != expected_state:
        return jsonify({"error": f"Workflow is in '{wf.get('state')}'; cannot act on stage '{stage}'"}), 409

    mapping = _STAGE_TRANSITION_MAP.get((expected_state, decision))
    if not mapping:
        return jsonify({"error": "Unsupported transition"}), 400
    to_state, audit_action, role_col = mapping
    if wf.get(role_col) != user_id:
        return jsonify({"error": f"You are not the current {stage} reviewer for this workflow"}), 403

    try:
        updated = transition(wf["workflow_id"], int(state_version), to_state, user_id, comment=comment)
    except WorkflowConflictError as exc:
        return jsonify({"error": str(exc), "current_state_version": wf["state_version"]}), 409
    except WorkflowTransitionError as exc:
        return jsonify({"error": str(exc)}), 409

    _notify(updated, audit_action, comment=comment, previous_state=expected_state)
    actor_uid, actor_email, behalf_uid, behalf_email = build_audit_actor(baseuser)
    log_audit_event(
        action=audit_action, endpoint="/workflow/approve", ip=request.remote_addr,
        status="success", actor_user_id=actor_uid, actor_email=actor_email,
        acting_on_behalf_of_user_id=behalf_uid, acting_on_behalf_of_email=behalf_email,
        metadata={"workflow_id": workflow_id, "stage": stage, "decision": decision},
    )
    g.audit_logged = True
    return jsonify({"workflow_id": workflow_id, "state": updated["state"], "state_version": updated["state_version"]}), 200


# ── By-doc lookup ─────────────────────────────────────────────────────────────


@workflow_bp.route("/by-doc/<doc_type>/<path:doc_id>", methods=["GET"])
@permission_required_body("workflow.review")
def workflow_by_doc(doc_type: str, doc_id: str):
    """Return the active WorkflowRow for a doc if the caller is owner/reviewer/approver.

    Query: user_id
    """
    baseuser = request.args.get("user_id", "")
    if not baseuser:
        return jsonify({"error": "user_id is required"}), 400
    logged_in, user_id = parse_composite_user_id(baseuser)

    try:
        from workflow_route.state_machine import get_workflow_for_doc_any_role
        row = get_workflow_for_doc_any_role(doc_type, doc_id, user_id)
    except Exception as exc:
        logger.error("get_workflow_for_doc_any_role failed: %s", exc)
        return jsonify({"error": "Failed to fetch workflow"}), 500

    if not row:
        return jsonify({"workflow": None}), 200

    from flask import Response
    import json as _json
    return Response(
        _json.dumps({"workflow": row}, default=str),
        status=200,
        mimetype="application/json",
    )


# ── Publish ───────────────────────────────────────────────────────────────────


@workflow_bp.route("/publish", methods=["POST"])
@permission_required_body("workflow.approve")
def publish_document():
    """Owner publishes an approved document.

    Body: { user_id, workflow_id, state_version, comment? }
    """
    body = request.get_json(silent=True) or {}
    baseuser = body.get("user_id", "")
    workflow_id = body.get("workflow_id")
    state_version = body.get("state_version")
    comment = body.get("comment")

    if not all([baseuser, workflow_id, state_version is not None]):
        return jsonify({"error": "user_id, workflow_id, state_version are required"}), 400

    logged_in, user_id = parse_composite_user_id(baseuser)

    try:
        wf = get_workflow(workflow_id)
    except WorkflowNotFoundError:
        return jsonify({"error": "Workflow not found"}), 404

    if wf.get("owner_user_id") != user_id:
        return jsonify({"error": "Only the document owner can publish"}), 403

    if wf.get("state") != "approval":
        return jsonify({"error": f"Document must be in 'approval' state to publish (currently '{wf.get('state')}')"}), 409

    try:
        updated = transition(wf["workflow_id"], int(state_version), "published", user_id, comment=comment)
    except WorkflowConflictError as exc:
        return jsonify({"error": str(exc), "current_state_version": wf["state_version"]}), 409
    except WorkflowTransitionError as exc:
        return jsonify({"error": str(exc)}), 409

    _notify(updated, WORKFLOW_PUBLISHED, comment=comment)

    actor_uid, actor_email, behalf_uid, behalf_email = build_audit_actor(baseuser)
    log_audit_event(
        action=WORKFLOW_PUBLISHED, endpoint="/workflow/publish", ip=request.remote_addr,
        status="success", actor_user_id=actor_uid, actor_email=actor_email,
        acting_on_behalf_of_user_id=behalf_uid, acting_on_behalf_of_email=behalf_email,
        metadata={"workflow_id": workflow_id},
    )
    g.audit_logged = True
    return jsonify({"workflow_id": workflow_id, "state": "published"}), 200


# ── Inbox ─────────────────────────────────────────────────────────────────────


@workflow_bp.route("/inbox", methods=["GET"])
@permission_required_body("workflow.review")
def workflow_inbox():
    """Paginated inbox for reviewers and approvers.

    Query params: user_id, role (reviewer|approver), doc_type?, page, page_size
    """
    baseuser = request.args.get("user_id", "")
    role = request.args.get("role", "reviewer")
    doc_type = request.args.get("doc_type")
    try:
        page = max(1, int(request.args.get("page") or 1))
        page_size = min(100, max(1, int(request.args.get("page_size") or 25)))
    except (TypeError, ValueError):
        page, page_size = 1, 25

    if role not in ("reviewer", "quality_reviewer", "governance_reviewer", "approver"):
        return jsonify({"error": "role must be 'quality_reviewer', 'governance_reviewer', or 'approver'"}), 400

    logged_in, user_id = parse_composite_user_id(baseuser)
    org_id = _get_user_org(user_id)

    try:
        rows, total = get_inbox(user_id, role, org_id, doc_type=doc_type, page=page, page_size=page_size)
    except Exception as exc:
        logger.error("get_inbox failed for user=%s role=%s: %s", user_id, role, exc)
        return jsonify({"error": "Failed to fetch inbox"}), 500

    from flask import Response
    import json as _json
    resp = Response(
        _json.dumps({"items": rows, "page": page, "page_size": page_size, "total": total}, default=str),
        status=200,
        mimetype="application/json",
    )
    resp.headers["X-Total-Count"] = str(total)
    return resp


# ── History ───────────────────────────────────────────────────────────────────


@workflow_bp.route("/history/<workflow_id>", methods=["GET"])
@permission_required_body("workflow.review")
def workflow_history(workflow_id: str):
    """Paginated event history for a workflow.

    Query params: user_id, page, page_size
    """
    try:
        page = max(1, int(request.args.get("page") or 1))
        page_size = min(200, max(1, int(request.args.get("page_size") or 50)))
    except (TypeError, ValueError):
        page, page_size = 1, 50

    try:
        get_workflow(workflow_id)  # verify it exists
    except WorkflowNotFoundError:
        return jsonify({"error": "Workflow not found"}), 404

    events, total = get_workflow_history(workflow_id, page=page, page_size=page_size)

    from flask import Response
    import json as _json
    resp = Response(
        _json.dumps({"items": events, "page": page, "page_size": page_size, "total": total}, default=str),
        status=200,
        mimetype="application/json",
    )
    resp.headers["X-Total-Count"] = str(total)
    return resp


# ── Reassign ──────────────────────────────────────────────────────────────────


@workflow_bp.route("/reassign", methods=["POST"])
@permission_required_body("workflow.config.manage")
def reassign_workflow():
    """Manually reassign the reviewer or approver role on an in-flight workflow.

    Body: { user_id, workflow_id, new_user_id, role ('reviewer'|'approver'), comment? }
    """
    body = request.get_json(silent=True) or {}
    baseuser = body.get("user_id", "")
    workflow_id = body.get("workflow_id")
    new_user_id = body.get("new_user_id")
    role = body.get("role", "reviewer")
    comment = body.get("comment")

    if not all([baseuser, workflow_id, new_user_id]):
        return jsonify({"error": "user_id, workflow_id, new_user_id are required"}), 400

    if role not in ("reviewer", "quality_reviewer", "governance_reviewer", "approver"):
        return jsonify({"error": "role must be 'quality_reviewer', 'governance_reviewer', or 'approver'"}), 400

    logged_in, user_id = parse_composite_user_id(baseuser)

    try:
        wf = get_workflow(workflow_id)
    except WorkflowNotFoundError:
        return jsonify({"error": "Workflow not found"}), 404

    col = {
        "reviewer": "current_quality_reviewer",          # legacy alias
        "quality_reviewer": "current_quality_reviewer",
        "governance_reviewer": "current_governance_reviewer",
        "approver": "current_approver",
    }[role]
    conn = connect_to_rds()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE document_workflow SET {col}=%s, state_version=state_version+1 WHERE workflow_id=%s",
                (new_user_id, workflow_id),
            )
            event_id = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO document_workflow_events "
                "(event_id,workflow_id,from_state,to_state,actor_user_id,comment) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (event_id, workflow_id, wf["state"], "reassigned", user_id, comment),
            )
        conn.commit()
    finally:
        conn.close()

    updated = get_workflow(workflow_id)
    _notify(updated, WORKFLOW_REASSIGNED, comment=comment, new_user_id=new_user_id, role=role)

    actor_uid, actor_email, behalf_uid, behalf_email = build_audit_actor(baseuser)
    log_audit_event(
        action=WORKFLOW_REASSIGNED, endpoint="/workflow/reassign", ip=request.remote_addr,
        status="success", actor_user_id=actor_uid, actor_email=actor_email,
        acting_on_behalf_of_user_id=behalf_uid, acting_on_behalf_of_email=behalf_email,
        metadata={"workflow_id": workflow_id, "role": role, "new_user_id": new_user_id},
    )
    g.audit_logged = True
    return jsonify({"workflow_id": workflow_id, "state": updated["state"], role: new_user_id}), 200
