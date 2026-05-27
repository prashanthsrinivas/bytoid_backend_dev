from utils.app_configs import IS_DEV
from utils.base_logger import get_logger

logger = get_logger(__name__)

# Doc types that the workflow state machine accepts (see
# workflow_route/routes.py config validator). Policies, procedures, and
# standards are all first-class review artifacts.
WORKFLOW_SUPPORTED_DOC_TYPES = ("policy", "procedure", "standard")


def auto_submit_policy(policy_id: str, doc_type: str, owner_user_id: str) -> None:
    """Programmatically submit a freshly-saved policy/procedure for review.

    Mirrors POST /workflow/submit, but only when the org is configured
    for role-based assignment. Per-document orgs (which require explicit
    per-policy reviewer selection) are left in draft so the existing
    "Send for review" UI flow still works.

    Idempotent: if a workflow row already exists in a non-draft state,
    skips entirely; if it exists in draft, transitions it forward.
    Any failure here is logged and swallowed — the policy YAML has
    already been written and must not be rolled back.
    """
    if not policy_id or not owner_user_id:
        logger.warning(
            "auto_submit_policy: missing policy_id=%s or owner=%s",
            policy_id, owner_user_id,
        )
        return

    if doc_type not in WORKFLOW_SUPPORTED_DOC_TYPES:
        logger.debug(
            "auto_submit_policy: skipping policy=%s — doc_type=%s not workflow-supported",
            policy_id, doc_type,
        )
        return

    try:
        from workflow_route.state_machine import (
            RoleResolutionError,
            create_workflow,
            get_user_org_id,
            get_workflow_config,
            get_workflow_for_doc,
            pick_user_for_role,
            transition,
        )
    except Exception as exc:
        logger.warning("auto_submit_policy: import failed for policy=%s: %s", policy_id, exc)
        return

    try:
        org_id = get_user_org_id(owner_user_id)
        if not org_id:
            logger.info(
                "auto_submit_policy: skipping policy=%s — owner=%s has no resolvable org",
                policy_id, owner_user_id,
            )
            return

        config = get_workflow_config(org_id, doc_type)
        if config.get("assignment_mode") != "role_based":
            logger.debug(
                "auto_submit_policy: skipping policy=%s — org=%s is per_document mode",
                policy_id, org_id,
            )
            return
        if not config.get("reviewer_role_id") and not config.get("approver_role_id"):
            logger.debug(
                "auto_submit_policy: skipping policy=%s — org=%s has no role IDs configured",
                policy_id, org_id,
            )
            return

        def _resolve(role_id):
            if not role_id:
                return None
            try:
                uid, _ = pick_user_for_role(role_id, owner_user_id)
                return uid
            except RoleResolutionError as rre:
                logger.warning(
                    "auto_submit_policy: role %s has no eligible user (policy=%s): %s",
                    role_id, policy_id, rre,
                )
                return None

        quality_reviewer_user_id = _resolve(config.get("reviewer_role_id"))
        # workflow_config currently only carries reviewer_role_id and
        # approver_role_id; governance reviewer reuses the quality slot
        # until a dedicated column is added.
        governance_reviewer_user_id = quality_reviewer_user_id
        approver_user_id = _resolve(config.get("approver_role_id"))

        doc_version = "1.0"
        existing = get_workflow_for_doc(doc_type, policy_id, doc_version)
        if existing and existing.get("state") != "draft":
            logger.info(
                "auto_submit_policy: skipping policy=%s — workflow %s already in state=%s",
                policy_id, existing.get("workflow_id"), existing.get("state"),
            )
            return

        if existing:
            wf = transition(
                existing["workflow_id"],
                existing["state_version"],
                "quality_review",
                owner_user_id,
                quality_reviewer_user_id=quality_reviewer_user_id,
                governance_reviewer_user_id=governance_reviewer_user_id,
                approver_user_id=approver_user_id,
            )
        else:
            wf = create_workflow(
                org_id=org_id,
                doc_type=doc_type,
                doc_id=policy_id,
                doc_version=doc_version,
                owner_user_id=owner_user_id,
                quality_reviewer_user_id=quality_reviewer_user_id,
                governance_reviewer_user_id=governance_reviewer_user_id,
                approver_user_id=approver_user_id,
            )
            wf = transition(
                wf["workflow_id"], 1, "quality_review", owner_user_id,
                quality_reviewer_user_id=quality_reviewer_user_id,
                governance_reviewer_user_id=governance_reviewer_user_id,
                approver_user_id=approver_user_id,
            )

        logger.info(
            "auto_submit_policy: policy=%s workflow=%s submitted to quality_review (QR=%s, GR=%s, AP=%s)",
            policy_id, wf.get("workflow_id"),
            quality_reviewer_user_id, governance_reviewer_user_id, approver_user_id,
        )

        try:
            from services.workflow_notifications_service import notify_workflow_event
            notify_workflow_event(wf, "WORKFLOW_SUBMITTED")
        except Exception as notify_exc:
            logger.warning(
                "auto_submit_policy: notification failed for policy=%s workflow=%s: %s",
                policy_id, wf.get("workflow_id"), notify_exc,
            )

    except Exception as exc:
        logger.warning(
            "auto_submit_policy: failed for policy=%s owner=%s (policy already saved): %s",
            policy_id, owner_user_id, exc, exc_info=IS_DEV,
        )
