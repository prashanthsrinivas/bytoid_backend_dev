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

WORKFLOW_SUBMITTED = "WORKFLOW_SUBMITTED"
WORKFLOW_REVIEW_APPROVED = "WORKFLOW_REVIEW_APPROVED"
WORKFLOW_CHANGES_REQUESTED = "WORKFLOW_CHANGES_REQUESTED"
WORKFLOW_APPROVED = "WORKFLOW_APPROVED"
WORKFLOW_PUBLISHED = "WORKFLOW_PUBLISHED"
WORKFLOW_REASSIGNED = "WORKFLOW_REASSIGNED"

# ── Helpers ───────────────────────────────────────────────────────────────────


def _get_user_org(user_id: str) -> str | None:
    """Resolve org_id for a user from the DB."""
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT org_id FROM users WHERE user_id=%s LIMIT 1", (user_id,))
            row = cur.fetchone()
        return row["org_id"] if row else None
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
    configs = {}
    for dt in doc_types:
        configs[dt] = get_workflow_config(org_id, dt)

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
    """Submit a document for review. Transitions draft → in_review.

    Body: { user_id, doc_type, doc_id, doc_version, reviewer_user_id?,
            approver_user_id?, comment? }
    """
    body = request.get_json(silent=True) or {}
    baseuser = body.get("user_id", "")
    doc_type = body.get("doc_type")
    doc_id = body.get("doc_id")
    doc_version = body.get("doc_version", "1.0")
    reviewer_user_id = body.get("reviewer_user_id")
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
    if config["assignment_mode"] == "role_based" and not reviewer_user_id:
        try:
            from shared_configuration import get_round_robin_user_for_resource
            reviewer_user_id = get_round_robin_user_for_resource(
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

    # Create or resume workflow
    existing = get_workflow_for_doc(doc_type, doc_id, doc_version)
    if existing:
        if existing["state"] not in ("draft", "changes_requested"):
            return jsonify({"error": f"Document is already in state '{existing['state']}'"}), 409
        try:
            wf = transition(
                existing["workflow_id"],
                existing["state_version"],
                "in_review",
                user_id,
                comment=comment,
                reviewer_user_id=reviewer_user_id,
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
            reviewer_user_id=reviewer_user_id,
            approver_user_id=approver_user_id,
        )
        try:
            wf = transition(
                wf["workflow_id"], 1, "in_review", user_id, comment=comment,
                reviewer_user_id=reviewer_user_id, approver_user_id=approver_user_id,
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


# ── Review ────────────────────────────────────────────────────────────────────


@workflow_bp.route("/review", methods=["POST"])
@permission_required_body("workflow.review")
def review_document():
    """Reviewer approves or requests changes.

    Body: { user_id, workflow_id, state_version, decision ('approved'|'changes_requested'), comment? }
    """
    body = request.get_json(silent=True) or {}
    baseuser = body.get("user_id", "")
    workflow_id = body.get("workflow_id")
    state_version = body.get("state_version")
    decision = body.get("decision")
    comment = body.get("comment")

    if not all([baseuser, workflow_id, state_version is not None, decision]):
        return jsonify({"error": "user_id, workflow_id, state_version, decision are required"}), 400

    if decision not in ("approved", "changes_requested"):
        return jsonify({"error": "decision must be 'approved' or 'changes_requested'"}), 400

    logged_in, user_id = parse_composite_user_id(baseuser)

    try:
        wf = get_workflow(workflow_id)
    except WorkflowNotFoundError:
        return jsonify({"error": "Workflow not found"}), 404

    if wf.get("current_reviewer") != user_id:
        return jsonify({"error": "You are not the current reviewer for this workflow"}), 403

    try:
        updated = transition(wf["workflow_id"], int(state_version), decision, user_id, comment=comment)
    except WorkflowConflictError as exc:
        return jsonify({"error": str(exc), "current_state_version": wf["state_version"]}), 409
    except WorkflowTransitionError as exc:
        return jsonify({"error": str(exc)}), 409

    audit_action = WORKFLOW_REVIEW_APPROVED if decision == "approved" else WORKFLOW_CHANGES_REQUESTED
    _notify(updated, audit_action, comment=comment)

    actor_uid, actor_email, behalf_uid, behalf_email = build_audit_actor(baseuser)
    log_audit_event(
        action=audit_action, endpoint="/workflow/review", ip=request.remote_addr,
        status="success", actor_user_id=actor_uid, actor_email=actor_email,
        acting_on_behalf_of_user_id=behalf_uid, acting_on_behalf_of_email=behalf_email,
        metadata={"workflow_id": workflow_id, "decision": decision},
    )
    g.audit_logged = True
    return jsonify({"workflow_id": workflow_id, "state": updated["state"]}), 200


# ── Approve ───────────────────────────────────────────────────────────────────


@workflow_bp.route("/approve", methods=["POST"])
@permission_required_body("workflow.approve")
def approve_document():
    """Approver approves or requests changes.

    Body: { user_id, workflow_id, state_version, decision ('published'|'changes_requested'), comment? }
    """
    body = request.get_json(silent=True) or {}
    baseuser = body.get("user_id", "")
    workflow_id = body.get("workflow_id")
    state_version = body.get("state_version")
    decision = body.get("decision")
    comment = body.get("comment")

    if not all([baseuser, workflow_id, state_version is not None, decision]):
        return jsonify({"error": "user_id, workflow_id, state_version, decision are required"}), 400

    if decision not in ("published", "changes_requested"):
        return jsonify({"error": "decision must be 'published' or 'changes_requested'"}), 400

    logged_in, user_id = parse_composite_user_id(baseuser)

    try:
        wf = get_workflow(workflow_id)
    except WorkflowNotFoundError:
        return jsonify({"error": "Workflow not found"}), 404

    if wf.get("current_approver") != user_id:
        return jsonify({"error": "You are not the current approver for this workflow"}), 403

    try:
        updated = transition(wf["workflow_id"], int(state_version), decision, user_id, comment=comment)
    except WorkflowConflictError as exc:
        return jsonify({"error": str(exc), "current_state_version": wf["state_version"]}), 409
    except WorkflowTransitionError as exc:
        return jsonify({"error": str(exc)}), 409

    audit_action = WORKFLOW_APPROVED if decision == "published" else WORKFLOW_CHANGES_REQUESTED
    _notify(updated, audit_action, comment=comment)

    actor_uid, actor_email, behalf_uid, behalf_email = build_audit_actor(baseuser)
    log_audit_event(
        action=audit_action, endpoint="/workflow/approve", ip=request.remote_addr,
        status="success", actor_user_id=actor_uid, actor_email=actor_email,
        acting_on_behalf_of_user_id=behalf_uid, acting_on_behalf_of_email=behalf_email,
        metadata={"workflow_id": workflow_id, "decision": decision},
    )
    g.audit_logged = True
    return jsonify({"workflow_id": workflow_id, "state": updated["state"]}), 200


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

    if wf.get("state") != "approved":
        return jsonify({"error": f"Document must be in 'approved' state to publish (currently '{wf.get('state')}')"}), 409

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

    if role not in ("reviewer", "approver"):
        return jsonify({"error": "role must be 'reviewer' or 'approver'"}), 400

    logged_in, user_id = parse_composite_user_id(baseuser)
    org_id = _get_user_org(user_id)
    if not org_id:
        return jsonify({"error": "User org not found"}), 404

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

    if role not in ("reviewer", "approver"):
        return jsonify({"error": "role must be 'reviewer' or 'approver'"}), 400

    logged_in, user_id = parse_composite_user_id(baseuser)

    try:
        wf = get_workflow(workflow_id)
    except WorkflowNotFoundError:
        return jsonify({"error": "Workflow not found"}), 404

    col = "current_reviewer" if role == "reviewer" else "current_approver"
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
