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
  POST /workflow/comment
  POST /workflow/upload_attachment
  POST /workflow/reassign
  POST /workflow/cancel
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
    RoleResolutionError,
    WorkflowConflictError,
    WorkflowNotFoundError,
    WorkflowTransitionError,
    add_comment,
    bootstrap_schema,
    cancel_workflow,
    create_workflow,
    enrich_workflow_for_viewer,
    get_inbox,
    get_user_org_id,
    get_workflow,
    get_workflow_config,
    get_workflow_for_doc,
    get_workflow_for_doc_any_role,
    get_workflow_history,
    pick_user_for_role,
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
WORKFLOW_CANCELLED = "WORKFLOW_CANCELLED"
# Legacy constants (kept for back-compat in callers / DB audit logs)
WORKFLOW_REVIEW_APPROVED = WORKFLOW_QUALITY_APPROVED
WORKFLOW_CHANGES_REQUESTED = WORKFLOW_QUALITY_SENT_BACK

# ── Helpers ───────────────────────────────────────────────────────────────────


# Local alias preserved for in-file readability; canonical impl lives in
# workflow_route.state_machine so non-route callers (e.g. runbook.helper)
# can use the same resolution without importing this module.
_get_user_org = get_user_org_id


def _notify(workflow: dict, event_type: str, comment: str | None = None, **kwargs):
    try:
        from services.workflow_notifications_service import notify_workflow_event
        notify_workflow_event(workflow, event_type, comment=comment, **kwargs)
    except Exception as exc:
        logger.error("workflow notification failed for %s: %s", event_type, exc)


def _resolve_assignee(workflow: dict, to_state: str) -> str | None:
    """Return the user_id the work is being handed off to for a given destination state."""
    if to_state == "quality_review":
        return workflow.get("current_quality_reviewer")
    if to_state == "governance_review":
        return workflow.get("current_governance_reviewer")
    if to_state in ("approval", "published"):
        return workflow.get("current_approver")
    if to_state == "draft":
        return workflow.get("owner_user_id")
    return None


# Map auto-advanced destination state → audit action constant.
# Used when emitting one audit row per auto-advanced hop.
_STAGE_AUDIT_ACTION = {
    "governance_review": "WORKFLOW_QUALITY_APPROVED",
    "approval":          "WORKFLOW_GOVERNANCE_APPROVED",
    "published":         "WORKFLOW_APPROVED",
}


def _log_chain_audits(
    chain: list,
    *,
    first_action: str,
    workflow_id: str,
    endpoint: str,
    actor_uid: str | None,
    actor_email: str | None,
    behalf_uid: str | None,
    behalf_email: str | None,
    extra_metadata: dict | None = None,
):
    """Emit one log_audit_event per hop in an auto-advance chain.

    The first hop uses ``first_action`` (e.g. WORKFLOW_SUBMITTED for /submit,
    or the per-stage approval constant for /review). Auto-advanced hops look
    up their action by destination state and carry ``auto_advanced=True`` in
    metadata so analytics can distinguish manual approvals from chained ones.
    """
    extras = extra_metadata or {}
    for idx, hop in enumerate(chain or []):
        if idx == 0:
            action = first_action
        else:
            action = _STAGE_AUDIT_ACTION.get(hop.get("to_state"))
            if not action:
                continue
        meta = {
            "workflow_id": workflow_id,
            "from_state": hop.get("from_state"),
            "to_state": hop.get("to_state"),
            "assigned_to_user_id": hop.get("assigned_to"),
            "comment": hop.get("comment"),
            "auto_advanced": bool(hop.get("auto")),
        }
        meta.update(extras)
        log_audit_event(
            action=action,
            endpoint=endpoint,
            ip=request.remote_addr,
            status="success",
            actor_user_id=actor_uid,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=behalf_uid,
            acting_on_behalf_of_email=behalf_email,
            metadata=meta,
        )


# ── Assignable-users endpoint ─────────────────────────────────────────────────


@workflow_bp.route("/assignable-users", methods=["GET"])
def get_assignable_users():
    """Return all active org members eligible to be assigned as workflow reviewers/approvers.

    Works for both admin and user callers. For non-SAML orgs the org membership
    is tracked via the root admin's permissions.shared list (populated when an
    invite is accepted), so we resolve the root admin first and read from there.
    Returns both user_type='user' and user_type='admin' accounts.

    Query: user_id
    """
    baseuser = request.args.get("user_id", "")
    if not baseuser:
        return jsonify({"error": "user_id is required"}), 400
    logged_in, user_id = parse_composite_user_id(baseuser)

    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT user_id, user_type, company_name, launch_id_fk, permissions "
                "FROM users WHERE user_id=%s LIMIT 1",
                (user_id,),
            )
            caller = cur.fetchone()
        if not caller:
            return jsonify({"users": []}), 200

        company_name = (caller.get("company_name") or "").strip()
        launch_id = (caller.get("launch_id_fk") or "").strip()

        if company_name:
            # SAML org: everyone sharing the same company_name (any user type)
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(
                    "SELECT user_id, email, user_type FROM users "
                    "WHERE company_name=%s AND user_id != %s",
                    (company_name, user_id),
                )
                rows = cur.fetchall()
        else:
            # Non-SAML: org membership is tracked in the root admin's permissions.shared.
            # Resolve root admin via launch_id_fk, falling back to permissions.invited_by
            # for users invited before the launch_id_fk fix (where it was stored as NULL).
            admin_id = launch_id  # may be empty for legacy invited users

            if not admin_id and caller.get("user_type") != "admin":
                try:
                    caller_perms = json.loads(caller["permissions"]) if caller.get("permissions") else {}
                    invited_by_email = caller_perms.get("invited_by", "")
                    if invited_by_email:
                        with conn.cursor(pymysql.cursors.DictCursor) as cur:
                            cur.execute(
                                "SELECT user_id FROM users WHERE email=%s AND user_type='admin' LIMIT 1",
                                (invited_by_email,),
                            )
                            ref = cur.fetchone()
                            if ref:
                                admin_id = ref["user_id"]
                except (json.JSONDecodeError, TypeError):
                    pass

            if not admin_id:
                admin_id = user_id

            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(
                    "SELECT permissions FROM users WHERE user_id=%s LIMIT 1",
                    (admin_id,),
                )
                admin_row = cur.fetchone()

            admin_perms = {}
            if admin_row and admin_row.get("permissions"):
                try:
                    admin_perms = json.loads(admin_row["permissions"])
                    if not isinstance(admin_perms, dict):
                        admin_perms = {}
                except (ValueError, TypeError):
                    admin_perms = {}

            email_set = set()
            for entry in admin_perms.get("shared", []):
                if entry.get("email") and entry.get("status") != "revoked":
                    email_set.add(entry["email"].lower())
            for entry in admin_perms.get("invites", []):
                if entry.get("email") and entry.get("status") not in ("revoked", "pending"):
                    email_set.add(entry["email"].lower())

            if not email_set:
                return jsonify({"users": []}), 200

            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                placeholders = ",".join(["%s"] * len(email_set))
                cur.execute(
                    f"SELECT user_id, email, user_type, permissions FROM users "
                    f"WHERE email IN ({placeholders}) AND user_id != %s",
                    (*email_set, user_id),
                )
                rows = cur.fetchall()

    except Exception as exc:
        logger.exception("get_assignable_users error: %s", exc)
        return jsonify({"error": "Internal server error"}), 500
    finally:
        conn.close()

    result = []
    for row in rows:
        # Skip revoked users
        try:
            perms = json.loads(row["permissions"]) if row.get("permissions") else {}
            if isinstance(perms, dict) and perms.get("status") == "revoked":
                continue
        except (ValueError, TypeError):
            pass
        if row.get("email"):
            result.append({
                "user_id": row["user_id"],
                "email": row["email"],
                "user_type": row["user_type"],
            })

    return jsonify({"users": result}), 200


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
    # Per-slot role IDs — if supplied, the backend resolves to a concrete user
    # via least-loaded round-robin. user_id takes precedence over role_id.
    quality_reviewer_role_id = body.get("quality_reviewer_role_id")
    governance_reviewer_role_id = body.get("governance_reviewer_role_id")
    approver_role_id = body.get("approver_role_id")
    comment = body.get("comment")

    if not all([baseuser, doc_type, doc_id]):
        return jsonify({"error": "user_id, doc_type, and doc_id are required"}), 400

    logged_in, user_id = parse_composite_user_id(baseuser)
    org_id = _get_user_org(user_id)
    if not org_id:
        return jsonify({"error": "User org not found"}), 404

    # Resolve any per-slot role IDs to concrete user IDs. Tracks which slots
    # were role-resolved so the audit metadata can record it.
    role_resolved: dict[str, str] = {}
    try:
        if not quality_reviewer_user_id and quality_reviewer_role_id:
            quality_reviewer_user_id, _ = pick_user_for_role(quality_reviewer_role_id, user_id)
            role_resolved["quality_reviewer"] = quality_reviewer_role_id
        if not governance_reviewer_user_id and governance_reviewer_role_id:
            governance_reviewer_user_id, _ = pick_user_for_role(governance_reviewer_role_id, user_id)
            role_resolved["governance_reviewer"] = governance_reviewer_role_id
        if not approver_user_id and approver_role_id:
            approver_user_id, _ = pick_user_for_role(approver_role_id, user_id)
            role_resolved["approver"] = approver_role_id
    except RoleResolutionError as exc:
        return jsonify({"error": str(exc)}), 400

    # Resolve reviewer/approver for role_based mode (workflow_config defaults).
    config = get_workflow_config(org_id, doc_type)
    if config["assignment_mode"] == "role_based" and not quality_reviewer_user_id and config.get("reviewer_role_id"):
        try:
            quality_reviewer_user_id, _ = pick_user_for_role(config["reviewer_role_id"], user_id)
            role_resolved["quality_reviewer"] = config["reviewer_role_id"]
        except RoleResolutionError:
            pass
    if config["assignment_mode"] == "role_based" and not approver_user_id and config.get("approver_role_id"):
        try:
            approver_user_id, _ = pick_user_for_role(config["approver_role_id"], user_id)
            role_resolved["approver"] = config["approver_role_id"]
        except RoleResolutionError:
            pass

    # Per-document mode requires explicit assignments.
    if config["assignment_mode"] == "per_document":
        if not all([quality_reviewer_user_id, governance_reviewer_user_id, approver_user_id]):
            return jsonify({
                "error": "quality_reviewer_user_id, governance_reviewer_user_id, "
                         "and approver_user_id are required for per-document workflows "
                         "(supply *_role_id alternatives to pick by role)"
            }), 400

    # Create or resume workflow
    try:
        existing = get_workflow_for_doc(doc_type, doc_id, doc_version)
        if existing:
            if existing["state"] != "draft":
                return jsonify({"error": f"Document is already in state '{existing['state']}'"}), 409
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
            wf = transition(
                wf["workflow_id"], 1, "quality_review", user_id, comment=comment,
                quality_reviewer_user_id=quality_reviewer_user_id,
                governance_reviewer_user_id=governance_reviewer_user_id,
                approver_user_id=approver_user_id,
            )
    except (WorkflowConflictError, WorkflowTransitionError) as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:
        logger.exception("submit_for_review unexpected error: %s", exc)
        return jsonify({"error": "Internal server error"}), 500

    _notify(wf, WORKFLOW_SUBMITTED, comment=comment)
    # Auto-advanced into published in the same call (single-reviewer org case):
    # send one terminal notification on top of the submission notification.
    if wf.get("state") == "published":
        _notify(wf, WORKFLOW_PUBLISHED, comment=comment)

    chain = wf.get("_auto_advance_chain") or []
    final_assigned_to = _resolve_assignee(wf, wf.get("state"))
    logger.info(
        "workflow %s: draft → %s by actor=%s assigned_to=%s comment=%r hops=%d",
        wf["workflow_id"], wf.get("state"), user_id, final_assigned_to, comment, len(chain),
    )

    actor_uid, actor_email, behalf_uid, behalf_email = build_audit_actor(baseuser)
    extra_meta = {"doc_type": doc_type, "doc_id": doc_id}
    if role_resolved:
        extra_meta["role_resolved"] = role_resolved
    _log_chain_audits(
        chain,
        first_action=WORKFLOW_SUBMITTED,
        workflow_id=wf["workflow_id"],
        endpoint="/workflow/submit",
        actor_uid=actor_uid, actor_email=actor_email,
        behalf_uid=behalf_uid, behalf_email=behalf_email,
        extra_metadata=extra_meta,
    )
    g.audit_logged = True

    return jsonify({
        "workflow_id": wf["workflow_id"],
        "state": wf["state"],
        "state_version": wf.get("state_version"),
        "auto_advanced_hops": max(0, len(chain) - 1),
        "role_resolved": role_resolved,
    }), 200


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
      comment?,                    # required when decision='send_back'
      governance_reviewer_user_id? # only used when stage='quality' + decision='approve'
    }
    """
    body = request.get_json(silent=True) or {}
    baseuser = body.get("user_id", "")
    workflow_id = body.get("workflow_id")
    state_version = body.get("state_version")
    stage = body.get("stage")
    decision = body.get("decision")
    comment = body.get("comment")
    governance_reviewer_user_id = body.get("governance_reviewer_user_id")

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
    except Exception as exc:
        logger.exception("review_document get_workflow error: %s", exc)
        return jsonify({"error": "Internal server error"}), 500

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

    # Permission: the user must hold the role for this stage.
    # If no reviewer was assigned (NULL), the document owner may act to unblock the workflow.
    assigned = wf.get(role_col)
    if assigned and assigned != user_id:
        return jsonify({"error": f"You are not the current {stage} reviewer for this workflow"}), 403
    if not assigned and wf.get("owner_user_id") != user_id:
        return jsonify({"error": f"No {stage} reviewer is assigned; only the document owner can act"}), 403

    try:
        updated = transition(
            wf["workflow_id"], int(state_version), to_state, user_id, comment=comment,
            governance_reviewer_user_id=governance_reviewer_user_id,
        )
    except WorkflowConflictError as exc:
        return jsonify({"error": str(exc), "current_state_version": wf["state_version"]}), 409
    except WorkflowTransitionError as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:
        logger.exception("review_document transition error: %s", exc)
        return jsonify({"error": "Internal server error"}), 500

    _notify(updated, audit_action, comment=comment, previous_state=expected_state)
    if updated.get("state") == "published" and to_state != "published":
        # Auto-advanced past `approval` to `published` — send the terminal
        # publish notification on top of the manual approval notification.
        _notify(updated, WORKFLOW_PUBLISHED, comment=comment)

    chain = updated.get("_auto_advance_chain") or []
    final_assigned_to = _resolve_assignee(updated, updated.get("state"))
    logger.info(
        "workflow %s: %s → %s by actor=%s assigned_to=%s comment=%r hops=%d",
        workflow_id, expected_state, updated.get("state"), user_id, final_assigned_to, comment, len(chain),
    )

    actor_uid, actor_email, behalf_uid, behalf_email = build_audit_actor(baseuser)
    _log_chain_audits(
        chain,
        first_action=audit_action,
        workflow_id=workflow_id,
        endpoint="/workflow/review",
        actor_uid=actor_uid, actor_email=actor_email,
        behalf_uid=behalf_uid, behalf_email=behalf_email,
        extra_metadata={"stage": stage, "decision": decision},
    )
    g.audit_logged = True
    return jsonify({
        "workflow_id": workflow_id,
        "state": updated["state"],
        "state_version": updated["state_version"],
        "auto_advanced_hops": max(0, len(chain) - 1),
    }), 200


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
    governance_reviewer_user_id = body.get("governance_reviewer_user_id")

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
    except Exception as exc:
        logger.exception("_dispatch_review get_workflow error: %s", exc)
        return jsonify({"error": "Internal server error"}), 500

    expected_state = {"quality": "quality_review", "governance": "governance_review", "approval": "approval"}[stage]
    if wf.get("state") != expected_state:
        return jsonify({"error": f"Workflow is in '{wf.get('state')}'; cannot act on stage '{stage}'"}), 409

    mapping = _STAGE_TRANSITION_MAP.get((expected_state, decision))
    if not mapping:
        return jsonify({"error": "Unsupported transition"}), 400
    to_state, audit_action, role_col = mapping

    assigned = wf.get(role_col)
    if assigned and assigned != user_id:
        return jsonify({"error": f"You are not the current {stage} reviewer for this workflow"}), 403
    if not assigned and wf.get("owner_user_id") != user_id:
        return jsonify({"error": f"No {stage} reviewer is assigned; only the document owner can act"}), 403

    try:
        updated = transition(
            wf["workflow_id"], int(state_version), to_state, user_id, comment=comment,
            governance_reviewer_user_id=governance_reviewer_user_id,
        )
    except WorkflowConflictError as exc:
        return jsonify({"error": str(exc), "current_state_version": wf["state_version"]}), 409
    except WorkflowTransitionError as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:
        logger.exception("_dispatch_review transition error: %s", exc)
        return jsonify({"error": "Internal server error"}), 500

    _notify(updated, audit_action, comment=comment, previous_state=expected_state)
    if updated.get("state") == "published" and to_state != "published":
        _notify(updated, WORKFLOW_PUBLISHED, comment=comment)

    chain = updated.get("_auto_advance_chain") or []
    final_assigned_to = _resolve_assignee(updated, updated.get("state"))
    logger.info(
        "workflow %s: %s → %s by actor=%s assigned_to=%s comment=%r hops=%d",
        workflow_id, expected_state, updated.get("state"), user_id, final_assigned_to, comment, len(chain),
    )

    actor_uid, actor_email, behalf_uid, behalf_email = build_audit_actor(baseuser)
    _log_chain_audits(
        chain,
        first_action=audit_action,
        workflow_id=workflow_id,
        endpoint="/workflow/approve",
        actor_uid=actor_uid, actor_email=actor_email,
        behalf_uid=behalf_uid, behalf_email=behalf_email,
        extra_metadata={"stage": stage, "decision": decision},
    )
    g.audit_logged = True
    return jsonify({
        "workflow_id": workflow_id,
        "state": updated["state"],
        "state_version": updated["state_version"],
        "auto_advanced_hops": max(0, len(chain) - 1),
    }), 200


# ── By-doc lookup ─────────────────────────────────────────────────────────────


@workflow_bp.route("/by-doc/<doc_type>/<path:doc_id>", methods=["GET"])
@permission_required_body("workflow.review")
def workflow_by_doc(doc_type: str, doc_id: str):
    """Return the active WorkflowRow for a doc if the caller is owner/reviewer/approver.

    Also handles shared-access users: if the caller has been granted shared
    access to a runbook result (doc_type='runbook'), fetches the workflow on
    behalf of the result owner so the step statuses are visible.

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

    # Shared-access fallback: a user granted read access to a runbook result is
    # not a workflow party (owner/reviewer/approver), so the query above returns
    # null. Detect this case and re-fetch as the owner so step statuses are visible.
    if not row and doc_type == "runbook":
        try:
            from shared_configuration import get_user_shared_reports, get_admin_shared_config
            shared_reports = get_user_shared_reports(user_id) or {}
            entry = shared_reports.get(doc_id)
            if entry and entry.get("type") == "runbook":
                main_user_id = entry.get("mainuser_id")
                if main_user_id:
                    # Verify that access hasn't been revoked in the owner's config
                    admin_config = get_admin_shared_config(main_user_id)
                    report_meta = admin_config.get("reports", {}).get(doc_id, {})
                    sharing_access = report_meta.get("sharing_access", [])
                    user_access = next(
                        (e for e in sharing_access if e["id"] == user_id), None
                    )
                    if user_access and user_access.get("access"):
                        row = get_workflow_for_doc_any_role(doc_type, doc_id, main_user_id)
        except Exception as share_exc:
            logger.warning("workflow shared-access fallback failed: %s", share_exc)

    if not row:
        return jsonify({"workflow": None}), 200

    try:
        enriched = enrich_workflow_for_viewer(row, user_id)
    except Exception as enrich_exc:
        logger.warning(
            "workflow enrichment failed for workflow_id=%s viewer=%s: %s — returning raw row",
            row.get("workflow_id"), user_id, enrich_exc,
        )
        enriched = row

    from flask import Response
    import json as _json
    return Response(
        _json.dumps({"workflow": enriched}, default=str),
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
    except Exception as exc:
        logger.exception("publish_document get_workflow error: %s", exc)
        return jsonify({"error": "Internal server error"}), 500

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
    except Exception as exc:
        logger.exception("publish_document transition error: %s", exc)
        return jsonify({"error": "Internal server error"}), 500

    _notify(updated, WORKFLOW_PUBLISHED, comment=comment)

    assigned_to = updated.get("owner_user_id")
    logger.info(
        "workflow %s: approval → published by actor=%s assigned_to=%s comment=%r",
        workflow_id, user_id, assigned_to, comment,
    )

    actor_uid, actor_email, behalf_uid, behalf_email = build_audit_actor(baseuser)
    log_audit_event(
        action=WORKFLOW_PUBLISHED, endpoint="/workflow/publish", ip=request.remote_addr,
        status="success", actor_user_id=actor_uid, actor_email=actor_email,
        acting_on_behalf_of_user_id=behalf_uid, acting_on_behalf_of_email=behalf_email,
        metadata={
            "workflow_id": workflow_id,
            "from_state": "approval",
            "to_state": "published",
            "assigned_to_user_id": assigned_to,
            "comment": comment,
        },
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

    Query params: user_id, page, page_size, state?
    When ``state`` is provided (e.g. ``state=quality_review``), transition rows
    are filtered to those touching that stage; manual comments are always
    included regardless of state.
    """
    try:
        page = max(1, int(request.args.get("page") or 1))
        page_size = min(200, max(1, int(request.args.get("page_size") or 50)))
    except (TypeError, ValueError):
        page, page_size = 1, 50

    state = request.args.get("state") or None

    try:
        get_workflow(workflow_id)  # verify it exists
    except WorkflowNotFoundError:
        return jsonify({"error": "Workflow not found"}), 404

    events, total = get_workflow_history(
        workflow_id, page=page, page_size=page_size, state=state
    )

    from flask import Response
    import json as _json
    resp = Response(
        _json.dumps({"items": events, "page": page, "page_size": page_size, "total": total}, default=str),
        status=200,
        mimetype="application/json",
    )
    resp.headers["X-Total-Count"] = str(total)
    return resp


# ── Manual comments + screenshot attachments ─────────────────────────────────


_ALLOWED_IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp"}
_MAX_FILES_PER_REQUEST = 10
_MAX_COMMENT_CHARS = 4000


def _is_allowed_image(filename: str, content_type: str) -> bool:
    if not filename or not content_type:
        return False
    if not content_type.lower().startswith("image/"):
        return False
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in _ALLOWED_IMAGE_EXTS


@workflow_bp.route("/upload_attachment", methods=["POST"])
@permission_required_body("workflow.review")
def workflow_upload_attachment():
    """Issue presigned S3 PUT URLs for screenshot uploads on a workflow.

    Body: { user_id, workflow_id, files: [{filename, content_type}, ...] }
    Returns the same shape as /playbook/make_s3upload:
      { status, files: [{original_name, file_key, upload_url}] }
    """
    import os as _os
    import uuid as _uuid
    from datetime import datetime as _dt
    from utils.s3_utils import s3bucket

    body = request.get_json(silent=True) or {}
    baseuser = body.get("user_id", "")
    workflow_id = body.get("workflow_id")
    files = body.get("files") or []

    if not baseuser or not workflow_id or not isinstance(files, list) or not files:
        return jsonify({"error": "user_id, workflow_id, and files[] are required"}), 400
    if len(files) > _MAX_FILES_PER_REQUEST:
        return jsonify({"error": f"Too many files (max {_MAX_FILES_PER_REQUEST})"}), 400

    logged_in, user_id = parse_composite_user_id(baseuser)

    try:
        get_workflow(workflow_id)
    except WorkflowNotFoundError:
        return jsonify({"error": "Workflow not found"}), 404

    bucket = _os.getenv("S3_BUCKET")
    if not bucket:
        logger.error("upload_attachment: S3_BUCKET not configured")
        return jsonify({"error": "Upload not configured"}), 500

    s3 = s3bucket()
    response_files = []
    for entry in files:
        if not isinstance(entry, dict):
            return jsonify({"error": "each file must be an object"}), 400
        filename = (entry.get("filename") or "").strip()
        content_type = (entry.get("content_type") or "").strip()
        if not _is_allowed_image(filename, content_type):
            return jsonify({
                "error": f"file '{filename}' rejected: only images "
                         f"({', '.join(sorted(_ALLOWED_IMAGE_EXTS))}) are allowed"
            }), 400

        unique_id = _uuid.uuid4().hex
        timestamp = _dt.utcnow().strftime("%Y%m%d%H%M%S")
        s3_key = f"{user_id}/workflow_attachments/{workflow_id}/{timestamp}_{unique_id}_{filename}"
        upload_url = s3.generate_presigned_url(
            ClientMethod="put_object",
            Params={"Bucket": bucket, "Key": s3_key, "ContentType": content_type},
            ExpiresIn=3600,
        )
        response_files.append({
            "original_name": filename,
            "file_key": s3_key,
            "upload_url": upload_url,
        })

    return jsonify({"status": "success", "files": response_files}), 200


@workflow_bp.route("/comment", methods=["POST"])
@permission_required_body("workflow.review")
def workflow_comment():
    """Post a manual comment (with optional screenshot attachments) to a
    workflow's activity feed.

    Body: {
        user_id, workflow_id,
        comment: str,
        attachments?: [{s3_key, original_name, content_type, size}]
    }
    Attachment s3_keys must live under {user_id}/workflow_attachments/{workflow_id}/
    — the same prefix issued by /workflow/upload_attachment — to prevent
    posting arbitrary keys.
    """
    body = request.get_json(silent=True) or {}
    baseuser = body.get("user_id", "")
    workflow_id = body.get("workflow_id")
    comment = (body.get("comment") or "").strip()
    attachments = body.get("attachments") or []

    if not baseuser or not workflow_id:
        return jsonify({"error": "user_id and workflow_id are required"}), 400
    if not comment:
        return jsonify({"error": "comment is required"}), 400
    if len(comment) > _MAX_COMMENT_CHARS:
        return jsonify({"error": f"comment exceeds {_MAX_COMMENT_CHARS} characters"}), 400
    if not isinstance(attachments, list):
        return jsonify({"error": "attachments must be a list"}), 400
    if len(attachments) > _MAX_FILES_PER_REQUEST:
        return jsonify({"error": f"Too many attachments (max {_MAX_FILES_PER_REQUEST})"}), 400

    logged_in, user_id = parse_composite_user_id(baseuser)

    try:
        get_workflow(workflow_id)
    except WorkflowNotFoundError:
        return jsonify({"error": "Workflow not found"}), 404

    expected_prefix = f"{user_id}/workflow_attachments/{workflow_id}/"
    cleaned: list[dict] = []
    for att in attachments:
        if not isinstance(att, dict):
            return jsonify({"error": "each attachment must be an object"}), 400
        s3_key = (att.get("s3_key") or "").strip()
        if not s3_key.startswith(expected_prefix):
            return jsonify({
                "error": "attachment s3_key must be under your workflow upload prefix"
            }), 400
        cleaned.append({
            "s3_key": s3_key,
            "original_name": att.get("original_name"),
            "content_type": att.get("content_type"),
            "size": att.get("size"),
        })

    try:
        result = add_comment(workflow_id, user_id, comment, cleaned)
    except Exception as exc:
        logger.exception("workflow_comment insert failed: %s", exc)
        return jsonify({"error": "Failed to save comment"}), 500

    return jsonify({
        "status": "success",
        "event_id": result["event_id"],
        "created_at": str(result.get("created_at")) if result.get("created_at") else None,
    }), 200


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
    except Exception as exc:
        logger.exception("reassign_workflow get_workflow error: %s", exc)
        return jsonify({"error": "Internal server error"}), 500

    col = {
        "reviewer": "current_quality_reviewer",          # legacy alias
        "quality_reviewer": "current_quality_reviewer",
        "governance_reviewer": "current_governance_reviewer",
        "approver": "current_approver",
    }[role]
    previous_user_id = wf.get(col)
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
                "(event_id,workflow_id,from_state,to_state,actor_user_id,assigned_to_user_id,comment) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (event_id, workflow_id, wf["state"], "reassigned", user_id, new_user_id, comment),
            )
        conn.commit()
    except Exception as exc:
        logger.exception("reassign_workflow DB error: %s", exc)
        return jsonify({"error": "Internal server error"}), 500
    finally:
        conn.close()

    try:
        updated = get_workflow(workflow_id)
    except Exception as exc:
        logger.exception("reassign_workflow post-update get_workflow error: %s", exc)
        return jsonify({"error": "Internal server error"}), 500
    _notify(updated, WORKFLOW_REASSIGNED, comment=comment, new_user_id=new_user_id, role=role)

    logger.info(
        "workflow %s reassigned: role=%s previous=%s → new=%s by actor=%s comment=%r",
        workflow_id, role, previous_user_id, new_user_id, user_id, comment,
    )

    actor_uid, actor_email, behalf_uid, behalf_email = build_audit_actor(baseuser)
    log_audit_event(
        action=WORKFLOW_REASSIGNED, endpoint="/workflow/reassign", ip=request.remote_addr,
        status="success", actor_user_id=actor_uid, actor_email=actor_email,
        acting_on_behalf_of_user_id=behalf_uid, acting_on_behalf_of_email=behalf_email,
        metadata={
            "workflow_id": workflow_id,
            "role": role,
            "previous_user_id": previous_user_id,
            "assigned_to_user_id": new_user_id,
            "new_user_id": new_user_id,  # back-compat with existing audit consumers
            "comment": comment,
        },
    )
    g.audit_logged = True
    return jsonify({"workflow_id": workflow_id, "state": updated["state"], role: new_user_id}), 200


# ── Cancel (owner reset) ──────────────────────────────────────────────────────

@workflow_bp.route("/cancel", methods=["POST"])
@permission_required_body("workflow.submit")
def cancel_workflow_route():
    """Reset an active review workflow back to draft so the owner can resubmit.

    Body: { user_id, doc_type, doc_id, doc_version?, comment? }
    OR:   { user_id, workflow_id, comment? }
    """
    body = request.get_json(silent=True) or {}
    baseuser = body.get("user_id", "")
    workflow_id = body.get("workflow_id")
    doc_type = body.get("doc_type")
    doc_id = body.get("doc_id")
    doc_version = body.get("doc_version", "1.0")
    comment = body.get("comment")

    if not baseuser:
        return jsonify({"error": "user_id is required"}), 400
    if not workflow_id and not all([doc_type, doc_id]):
        return jsonify({"error": "Either workflow_id or (doc_type and doc_id) are required"}), 400

    logged_in, user_id = parse_composite_user_id(baseuser)

    try:
        if not workflow_id:
            wf = get_workflow_for_doc(doc_type, doc_id, doc_version)
            if not wf:
                return jsonify({"error": "No workflow found for this document"}), 404
            workflow_id = wf["workflow_id"]

        updated = cancel_workflow(workflow_id, user_id, comment)
    except WorkflowNotFoundError:
        return jsonify({"error": "Workflow not found"}), 404
    except WorkflowTransitionError as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:
        logger.exception("cancel_workflow unexpected error: %s", exc)
        return jsonify({"error": "Internal server error"}), 500

    logger.info("workflow %s cancelled by actor=%s comment=%r", workflow_id, user_id, comment)

    actor_uid, actor_email, behalf_uid, behalf_email = build_audit_actor(baseuser)
    log_audit_event(
        action=WORKFLOW_CANCELLED, endpoint="/workflow/cancel", ip=request.remote_addr,
        status="success", actor_user_id=actor_uid, actor_email=actor_email,
        acting_on_behalf_of_user_id=behalf_uid, acting_on_behalf_of_email=behalf_email,
        metadata={"workflow_id": workflow_id, "doc_type": doc_type, "doc_id": doc_id, "comment": comment},
    )
    g.audit_logged = True

    return jsonify({"workflow_id": workflow_id, "state": updated["state"]}), 200
