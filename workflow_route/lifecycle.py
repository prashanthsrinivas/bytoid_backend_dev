"""Workflow lifecycle helpers — user deactivation, orphan reassignment."""

import uuid

import pymysql.cursors

from db.rds_db import connect_to_rds
from utils.base_logger import get_logger

logger = get_logger(__name__)


def reassign_orphaned_workflows(deactivated_user_id: str, org_id: str) -> int:
    """Reassign all in-flight workflows where the reviewer or approver left the org.

    For role_based configs: round-robin to the next eligible user.
    For per_document configs: nullify the column and notify the doc owner.

    Returns the number of workflows touched.
    """
    conn = connect_to_rds()
    touched = 0
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            # Find workflows where the deactivated user is the current reviewer/approver
            cur.execute(
                """SELECT w.*, c.assignment_mode, c.reviewer_role_id, c.approver_role_id
                   FROM document_workflow w
                   LEFT JOIN workflow_config c ON c.org_id=w.org_id AND c.doc_type=w.doc_type
                   WHERE w.org_id=%s
                     AND w.state NOT IN ('draft','published')
                     AND (w.current_reviewer=%s OR w.current_approver=%s)""",
                (org_id, deactivated_user_id, deactivated_user_id),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    for row in rows:
        try:
            _handle_orphan(row, deactivated_user_id, org_id)
            touched += 1
        except Exception as exc:
            logger.error(
                "reassign_orphaned_workflows failed for workflow=%s: %s",
                row.get("workflow_id"), exc,
            )

    return touched


def _handle_orphan(row: dict, deactivated_user_id: str, org_id: str):
    """Handle one orphaned workflow row."""
    workflow_id = row["workflow_id"]
    assignment_mode = row.get("assignment_mode") or "per_document"
    is_reviewer = row.get("current_reviewer") == deactivated_user_id
    role_to_reassign = "reviewer" if is_reviewer else "approver"

    if assignment_mode == "role_based":
        role_id = row.get("reviewer_role_id") if is_reviewer else row.get("approver_role_id")
        if role_id:
            try:
                from shared_configuration import get_round_robin_user_for_resource
                new_user_id = get_round_robin_user_for_resource(role_id, workflow_id)
            except Exception:
                new_user_id = None

            if new_user_id:
                _do_reassign(workflow_id, role_to_reassign, new_user_id, deactivated_user_id, "User deactivated — auto-reassigned via role round-robin")
                _notify_reassigned(workflow_id, row, new_user_id, role_to_reassign)
                return

    # per_document or no role found: notify owner + workflow.config.manage users
    _nullify_and_notify(workflow_id, row, role_to_reassign, org_id)


def _do_reassign(
    workflow_id: str,
    role: str,
    new_user_id: str,
    old_user_id: str,
    comment: str,
):
    col = "current_reviewer" if role == "reviewer" else "current_approver"
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                f"UPDATE document_workflow SET {col}=%s, state_version=state_version+1 WHERE workflow_id=%s",
                (new_user_id, workflow_id),
            )
            event_id = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO document_workflow_events (event_id,workflow_id,from_state,to_state,actor_user_id,comment) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (event_id, workflow_id, None, "reassigned", old_user_id, comment),
            )
        conn.commit()
    finally:
        conn.close()


def _nullify_and_notify(workflow_id: str, row: dict, role: str, org_id: str):
    col = "current_reviewer" if role == "reviewer" else "current_approver"
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                f"UPDATE document_workflow SET {col}=NULL, state_version=state_version+1 WHERE workflow_id=%s",
                (workflow_id,),
            )
        conn.commit()
    finally:
        conn.close()

    try:
        from services.workflow_notifications_service import notify_orphaned_workflow
        notify_orphaned_workflow(workflow_id, row, role, org_id)
    except Exception as exc:
        logger.error("notify_orphaned_workflow failed: %s", exc)


def _notify_reassigned(workflow_id: str, row: dict, new_user_id: str, role: str):
    try:
        from services.workflow_notifications_service import notify_workflow_reassigned
        notify_workflow_reassigned(workflow_id, row, new_user_id, role)
    except Exception as exc:
        logger.error("notify_workflow_reassigned failed: %s", exc)
