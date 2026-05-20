"""Workflow notification fanout — email (via Celery), in-app, and WebSocket.

The compliance contract: in-app notification and audit log are written FIRST
(synchronously), before the async email task is queued. A failed email never
silently loses the approval signal.
"""

import base64
import json
import os
import uuid
from email.message import EmailMessage
from pathlib import Path

import pymysql.cursors

from jinja2 import Environment, FileSystemLoader

from db.rds_db import connect_to_rds
from utils.app_configs import IS_DEV
from utils.base_logger import get_logger

logger = get_logger(__name__, log_level="DEBUG" if IS_DEV else "INFO")

_TEMPLATE_DIR = Path(__file__).parent / "email_templates" / "workflow"
_jinja = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)), autoescape=True)

_BACKURL = os.getenv("BACKURL", "https://app.bytoid.ai")

# Event type → template name mapping
_TEMPLATE_MAP = {
    "WORKFLOW_SUBMITTED": "assigned_for_review",
    "WORKFLOW_QUALITY_APPROVED": "assigned_for_approval",
    "WORKFLOW_QUALITY_SENT_BACK": "changes_requested",
    "WORKFLOW_GOVERNANCE_APPROVED": "assigned_for_approval",
    "WORKFLOW_GOVERNANCE_SENT_BACK": "changes_requested",
    "WORKFLOW_APPROVED": "approved",
    "WORKFLOW_APPROVAL_SENT_BACK": "changes_requested",
    "WORKFLOW_PUBLISHED": "published",
    "WORKFLOW_REASSIGNED": "reassigned",
    # Legacy aliases kept for older audit records
    "WORKFLOW_REVIEW_APPROVED": "assigned_for_approval",
    "WORKFLOW_CHANGES_REQUESTED": "changes_requested",
}

# ── Public entry point ────────────────────────────────────────────────────────


def notify_workflow_event(
    workflow: dict,
    event_type: str,
    comment: str | None = None,
    **kwargs,
):
    """Fan out notifications for a workflow event.

    Order (compliance-safe):
      1. Write in-app notification row(s) to DB  ← always synchronous
      2. Queue email via Celery task              ← async, may fail
      3. Emit WebSocket update                    ← best-effort
    """
    try:
        recipients = _resolve_recipients(workflow, event_type, **kwargs)
    except Exception as exc:
        logger.error("notify_workflow_event: failed to resolve recipients: %s", exc)
        return

    context = _build_context(workflow, event_type, comment=comment, **kwargs)

    # 1. In-app notifications (written synchronously — never skipped)
    for uid in recipients:
        _insert_in_app_notification(workflow, event_type, uid, context)

    # 2. Email (queued via Celery — non-blocking)
    template_name = _TEMPLATE_MAP.get(event_type)
    if template_name:
        for uid in recipients:
            recipient_email = _get_user_email(uid)
            if recipient_email:
                _queue_email(workflow, event_type, recipient_email, uid, template_name, context)

    # 3. WebSocket (best-effort)
    for uid in recipients:
        _emit_ws(workflow, event_type, uid)


def notify_orphaned_workflow(workflow_id: str, row: dict, role: str, org_id: str):
    """Notify owner and workflow managers that a reviewer/approver left mid-flow."""
    owner_id = row.get("owner_user_id")
    managers = _get_org_workflow_managers(org_id)
    recipients = set(managers)
    if owner_id:
        recipients.add(owner_id)

    for uid in recipients:
        _insert_raw_in_app_notification(
            user_id=uid,
            workflow_id=workflow_id,
            workflow_state=row.get("state"),
            doc_type=row.get("doc_type"),
            doc_id=row.get("doc_id"),
            message=f"Reviewer/approver left the org. Please reassign the {role} role via the workflow inbox.",
            action_required=True,
        )


def notify_workflow_reassigned(workflow_id: str, row: dict, new_user_id: str, role: str):
    """Send in-app + email to the newly assigned reviewer/approver."""
    context = {
        "doc_title": row.get("doc_id", "Unknown"),
        "doc_version": row.get("doc_version", ""),
        "doc_type": row.get("doc_type", ""),
        "role": role,
        "admin_name": "Bytoid",
        "recipient_name": new_user_id,
        "comment": None,
        "link": f"{_BACKURL}/workflow/{workflow_id}",
    }
    _insert_raw_in_app_notification(
        user_id=new_user_id,
        workflow_id=workflow_id,
        workflow_state=row.get("state"),
        doc_type=row.get("doc_type"),
        doc_id=row.get("doc_id"),
        message=f"You have been assigned as {role} for this document.",
        action_required=True,
    )
    recipient_email = _get_user_email(new_user_id)
    if recipient_email:
        _queue_email(
            {"workflow_id": workflow_id, **row},
            "WORKFLOW_REASSIGNED",
            recipient_email,
            new_user_id,
            "reassigned",
            context,
        )


# ── Recipient resolution ──────────────────────────────────────────────────────


def _resolve_recipients(workflow: dict, event_type: str, **kwargs) -> list[str]:
    """Return the list of user_ids who should receive this notification.

    Send-back events notify BOTH the sender's stage holder AND the target stage holder
    so all impacted stakeholders see the comment with their notification + email.
    """
    owner = workflow.get("owner_user_id")
    quality = workflow.get("current_quality_reviewer") or workflow.get("current_reviewer")
    governance = workflow.get("current_governance_reviewer")
    approver = workflow.get("current_approver")
    new_user_id = kwargs.get("new_user_id")

    mapping = {
        # Forward
        "WORKFLOW_SUBMITTED":           [quality],                                  # → quality reviewer
        "WORKFLOW_QUALITY_APPROVED":    [governance],                               # → governance reviewer
        "WORKFLOW_GOVERNANCE_APPROVED": [approver],                                 # → approver
        "WORKFLOW_APPROVED":            [owner, quality, governance],               # FYI
        "WORKFLOW_PUBLISHED":           [owner, quality, governance, approver],     # FYI
        # Send back — notify BOTH stages (sender + target)
        "WORKFLOW_QUALITY_SENT_BACK":   [owner, quality],                           # back to intake
        "WORKFLOW_GOVERNANCE_SENT_BACK":[quality, governance],                      # back to quality
        "WORKFLOW_APPROVAL_SENT_BACK":  [governance, approver],                     # back to governance
        # Reassign
        "WORKFLOW_REASSIGNED":          [new_user_id],
        # Legacy aliases
        "WORKFLOW_REVIEW_APPROVED":     [governance or approver],
        "WORKFLOW_CHANGES_REQUESTED":   [owner, quality],
    }
    raw = mapping.get(event_type, [])
    return list(dict.fromkeys([r for r in raw if r]))


# ── Context builder ───────────────────────────────────────────────────────────


def _build_context(workflow: dict, event_type: str, comment: str | None = None, **kwargs) -> dict:
    return {
        "doc_title": workflow.get("doc_id", "Document"),
        "doc_type": workflow.get("doc_type", ""),
        "doc_version": workflow.get("doc_version", ""),
        "workflow_id": workflow.get("workflow_id", ""),
        "actor_name": "A team member",
        "reviewer_name": workflow.get("current_reviewer", "Reviewer"),
        "approver_name": workflow.get("current_approver", "Approver"),
        "admin_name": kwargs.get("admin_name", "Admin"),
        "recipient_name": "",
        "role": kwargs.get("role", ""),
        "comment": comment,
        "link": f"{_BACKURL}/workflow/{workflow.get('workflow_id', '')}",
        **{k: v for k, v in kwargs.items() if k not in ("admin_name", "role")},
    }


# ── In-app notifications ──────────────────────────────────────────────────────


def _insert_in_app_notification(
    workflow: dict, event_type: str, user_id: str, context: dict
):
    base_message = {
        "WORKFLOW_SUBMITTED": "A document has been submitted for quality review.",
        "WORKFLOW_QUALITY_APPROVED": "A document is ready for your governance review.",
        "WORKFLOW_QUALITY_SENT_BACK": "Changes have been requested on the intake.",
        "WORKFLOW_GOVERNANCE_APPROVED": "A document is ready for your approval.",
        "WORKFLOW_GOVERNANCE_SENT_BACK": "Document has been sent back for quality review.",
        "WORKFLOW_APPROVED": "A document has been approved and is ready to publish.",
        "WORKFLOW_APPROVAL_SENT_BACK": "Document has been sent back for governance review.",
        "WORKFLOW_PUBLISHED": "A document has been published.",
        "WORKFLOW_REASSIGNED": "You have been assigned to a document.",
        # Legacy
        "WORKFLOW_REVIEW_APPROVED": "A document you reviewed has been forwarded for approval.",
        "WORKFLOW_CHANGES_REQUESTED": "Changes have been requested on your document.",
    }.get(event_type, "Workflow update.")

    # Append the first 120 chars of the comment when present, so reviewers see context
    # without having to open the document.
    comment = (context.get("comment") or "").strip()
    if comment:
        snippet = comment if len(comment) <= 120 else comment[:117] + "..."
        message = f"{base_message} Reason: {snippet}"
    else:
        message = base_message

    action_required_events = {
        "WORKFLOW_SUBMITTED",
        "WORKFLOW_QUALITY_APPROVED",
        "WORKFLOW_GOVERNANCE_APPROVED",
        "WORKFLOW_QUALITY_SENT_BACK",
        "WORKFLOW_GOVERNANCE_SENT_BACK",
        "WORKFLOW_APPROVAL_SENT_BACK",
        "WORKFLOW_REASSIGNED",
        "WORKFLOW_REVIEW_APPROVED",
        "WORKFLOW_CHANGES_REQUESTED",
    }

    _insert_raw_in_app_notification(
        user_id=user_id,
        workflow_id=workflow.get("workflow_id"),
        workflow_state=workflow.get("state"),
        doc_type=workflow.get("doc_type"),
        doc_id=workflow.get("doc_id"),
        message=message,
        action_required=event_type in action_required_events,
    )


def _insert_raw_in_app_notification(
    user_id: str,
    workflow_id: str,
    workflow_state: str | None,
    doc_type: str | None,
    doc_id: str | None,
    message: str,
    action_required: bool = False,
):
    conn = connect_to_rds()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO notifications
                   (notification_id, user_id, message, doc_type, doc_id,
                    workflow_id, workflow_state, action_required, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW())""",
                (
                    str(uuid.uuid4()),
                    user_id,
                    message,
                    doc_type,
                    doc_id,
                    workflow_id,
                    workflow_state,
                    int(action_required),
                ),
            )
        conn.commit()
    except Exception as exc:
        logger.error("Failed to insert in-app notification for user=%s: %s", user_id, exc)
    finally:
        conn.close()


# ── Email (queued via Celery) ─────────────────────────────────────────────────


def _queue_email(
    workflow: dict,
    event_type: str,
    recipient_email: str,
    recipient_user_id: str,
    template_name: str,
    context: dict,
):
    try:
        from utils.celery_base import celery
        celery.send_task(
            "tasks.send_workflow_email",
            kwargs={
                "workflow_id": workflow.get("workflow_id"),
                "event_type": event_type,
                "recipient_email": recipient_email,
                "recipient_user_id": recipient_user_id,
                "template_name": template_name,
                "context": context,
            },
        )
    except Exception as exc:
        logger.error("Failed to queue workflow email for %s: %s", recipient_email, exc)
        _write_to_dlq(workflow, event_type, recipient_email, recipient_user_id, template_name, context, str(exc))


def _write_to_dlq(
    workflow: dict,
    event_type: str,
    recipient: str,
    recipient_user_id: str,
    template_name: str,
    context: dict,
    error: str,
):
    org_id = workflow.get("org_id", "")
    conn = connect_to_rds()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO workflow_email_dlq
                   (dlq_id, workflow_id, org_id, recipient, template_name, context_json, last_error)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (
                    str(uuid.uuid4()),
                    workflow.get("workflow_id"),
                    org_id,
                    recipient,
                    template_name,
                    json.dumps(context),
                    error[:2000],
                ),
            )
        conn.commit()
    except Exception as exc2:
        logger.error("DLQ write failed for %s: %s", recipient, exc2)
    finally:
        conn.close()


# ── Rendered email builder (used by Celery task) ──────────────────────────────


def render_email(template_name: str, context: dict) -> tuple[str, str, str]:
    """Render HTML and plaintext bodies. Returns (subject, html_body, text_body)."""
    subject_map = {
        "assigned_for_review": 'Review requested: {doc_type} "{doc_title}"',
        "assigned_for_approval": 'Approval requested: {doc_type} "{doc_title}"',
        "changes_requested": 'Sent back for changes: "{doc_title}"',
        "approved": '"{doc_title}" approved',
        "published": '"{doc_title}" is now published',
        "reassigned": 'You have been assigned to "{doc_title}"',
    }
    subject_template = subject_map.get(template_name, "Workflow update")
    # Defensive .get-fallback so missing context keys don't crash rendering
    safe_ctx = {**context, "doc_type": context.get("doc_type") or "document",
                "doc_title": context.get("doc_title") or "Untitled"}
    subject = subject_template.format(**safe_ctx)

    html_tpl = _jinja.get_template(f"{template_name}.html")
    txt_tpl = _jinja.get_template(f"{template_name}.txt")
    html_body = html_tpl.render(**context)
    text_body = txt_tpl.render(**context)
    return subject, html_body, text_body


def build_multipart_mime(
    from_email: str,
    to_email: str,
    subject: str,
    html_body: str,
    text_body: str,
) -> str:
    """Build a base64-encoded RFC 5322 multipart/alternative MIME message for Graph API."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_email
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    raw = msg.as_bytes()
    return base64.b64encode(raw).decode("ascii")


# ── WebSocket ─────────────────────────────────────────────────────────────────


def _emit_ws(workflow: dict, event_type: str, user_id: str):
    try:
        from websockets_custom.ws_instance import ws_service
        import asyncio
        loop = asyncio.new_event_loop()
        loop.run_until_complete(
            ws_service.emit(
                user_id=user_id,
                message=f"Workflow update: {event_type}",
                scope="user",
                feature="workflow",
                stage=workflow.get("state"),
            )
        )
        loop.close()
    except Exception as exc:
        logger.debug("ws emit failed for workflow event %s: %s", event_type, exc)


# ── DB helpers ────────────────────────────────────────────────────────────────


def _get_user_email(user_id: str) -> str | None:
    try:
        from db.db_checkers import get_email_by_id
        return get_email_by_id(user_id)
    except Exception:
        return None


def _get_org_workflow_managers(org_id: str) -> list[str]:
    """Return user_ids with workflow.config.manage permission in the org."""
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT user_id FROM users WHERE org_id=%s "
                "AND JSON_CONTAINS(roles_creation, '\"workflow.config.manage\"')",
                (org_id,),
            )
            return [r["user_id"] for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        conn.close()
