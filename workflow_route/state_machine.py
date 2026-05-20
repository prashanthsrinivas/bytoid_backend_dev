"""Workflow state machine — transitions, permission checks, and DB helpers."""

import uuid
from datetime import datetime, timezone

import pymysql.cursors

from db.rds_db import connect_to_rds
from utils.base_logger import get_logger

logger = get_logger(__name__)

# ── Default state machine ─────────────────────────────────────────────────────

DEFAULT_STATES_JSON = {
    "states": ["draft", "in_review", "changes_requested", "approved", "published"],
    "transitions": {
        "draft": ["in_review"],
        "in_review": ["approved", "changes_requested"],
        "changes_requested": ["draft", "in_review"],
        "approved": ["published", "changes_requested"],
        "published": ["draft"],
    },
    "required_permission_per_transition": {
        "draft->in_review": "workflow.submit",
        "in_review->approved": "workflow.review",
        "in_review->changes_requested": "workflow.review",
        "approved->published": "workflow.approve",
        "approved->changes_requested": "workflow.approve",
        "published->draft": "workflow.submit",
        "changes_requested->draft": "workflow.submit",
        "changes_requested->in_review": "workflow.submit",
    },
}


class WorkflowConflictError(Exception):
    """Raised when state_version doesn't match (optimistic lock)."""


class WorkflowTransitionError(Exception):
    """Raised when the requested transition is not allowed."""


class WorkflowNotFoundError(Exception):
    pass


# ── DB helpers ────────────────────────────────────────────────────────────────


def get_workflow_config(org_id: str, doc_type: str) -> dict:
    """Return the workflow_config row for (org_id, doc_type), or the default."""
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT states_json, assignment_mode, reviewer_role_id, approver_role_id "
                "FROM workflow_config WHERE org_id=%s AND doc_type=%s",
                (org_id, doc_type),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if row:
        import json
        states = row["states_json"] if isinstance(row["states_json"], dict) else json.loads(row["states_json"])
        return {
            "states_json": states,
            "assignment_mode": row["assignment_mode"],
            "reviewer_role_id": row["reviewer_role_id"],
            "approver_role_id": row["approver_role_id"],
        }
    return {
        "states_json": DEFAULT_STATES_JSON,
        "assignment_mode": "per_document",
        "reviewer_role_id": None,
        "approver_role_id": None,
    }


def get_workflow(workflow_id: str) -> dict:
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM document_workflow WHERE workflow_id=%s", (workflow_id,)
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        raise WorkflowNotFoundError(workflow_id)
    return dict(row)


def get_workflow_for_doc(doc_type: str, doc_id: str, doc_version: str) -> dict | None:
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM document_workflow WHERE doc_type=%s AND doc_id=%s AND doc_version=%s",
                (doc_type, doc_id, doc_version),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def create_workflow(
    org_id: str,
    doc_type: str,
    doc_id: str,
    doc_version: str,
    owner_user_id: str,
    reviewer_user_id: str | None = None,
    approver_user_id: str | None = None,
) -> dict:
    """Insert a new document_workflow row at state='draft' and return it."""
    workflow_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                """INSERT INTO document_workflow
                   (workflow_id, org_id, doc_type, doc_id, doc_version,
                    owner_user_id, state, current_reviewer, current_approver,
                    state_version, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,'draft',%s,%s,1,%s)""",
                (
                    workflow_id, org_id, doc_type, doc_id, doc_version,
                    owner_user_id, reviewer_user_id, approver_user_id, now,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    _append_event(workflow_id, None, "draft", owner_user_id, "Document created")
    return get_workflow(workflow_id)


def transition(
    workflow_id: str,
    expected_state_version: int,
    to_state: str,
    actor_user_id: str,
    comment: str | None = None,
    reviewer_user_id: str | None = None,
    approver_user_id: str | None = None,
) -> dict:
    """Perform a state transition with optimistic locking.

    Raises WorkflowConflictError if state_version doesn't match.
    Raises WorkflowTransitionError if the transition isn't allowed.
    Returns the updated workflow row.
    """
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            # Lock the row
            cur.execute(
                "SELECT * FROM document_workflow WHERE workflow_id=%s FOR UPDATE",
                (workflow_id,),
            )
            row = cur.fetchone()
            if not row:
                raise WorkflowNotFoundError(workflow_id)

            current_state = row["state"]
            current_version = row["state_version"]

            if current_version != expected_state_version:
                raise WorkflowConflictError(
                    f"State version mismatch: expected {expected_state_version}, got {current_version}"
                )

            config = get_workflow_config(row["org_id"], row["doc_type"])
            allowed_nexts = config["states_json"]["transitions"].get(current_state, [])
            if to_state not in allowed_nexts:
                raise WorkflowTransitionError(
                    f"Transition {current_state!r} → {to_state!r} not allowed"
                )

            now = datetime.now(timezone.utc)
            updates = {
                "state": to_state,
                "state_version": current_version + 1,
            }
            if to_state == "in_review" and reviewer_user_id:
                updates["current_reviewer"] = reviewer_user_id
            if to_state in ("approved", "published") and approver_user_id:
                updates["current_approver"] = approver_user_id
            if to_state == "in_review":
                updates["submitted_at"] = now
            if to_state == "approved":
                updates["approved_at"] = now
            if to_state == "published":
                updates["published_at"] = now

            set_clause = ", ".join(f"{k}=%s" for k in updates)
            cur.execute(
                f"UPDATE document_workflow SET {set_clause} WHERE workflow_id=%s",
                (*updates.values(), workflow_id),
            )
        conn.commit()
    finally:
        conn.close()

    event_id = _append_event(workflow_id, current_state, to_state, actor_user_id, comment)
    updated = get_workflow(workflow_id)
    updated["_event_id"] = event_id
    return updated


def _append_event(
    workflow_id: str,
    from_state: str | None,
    to_state: str,
    actor_user_id: str,
    comment: str | None,
) -> str:
    event_id = str(uuid.uuid4())
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                """INSERT INTO document_workflow_events
                   (event_id, workflow_id, from_state, to_state, actor_user_id, comment)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (event_id, workflow_id, from_state, to_state, actor_user_id, comment),
            )
        conn.commit()
    finally:
        conn.close()
    return event_id


def get_workflow_history(workflow_id: str, page: int = 1, page_size: int = 50) -> tuple[list, int]:
    offset = (page - 1) * page_size
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM document_workflow_events WHERE workflow_id=%s",
                (workflow_id,),
            )
            total = cur.fetchone()["cnt"]
            cur.execute(
                "SELECT * FROM document_workflow_events WHERE workflow_id=%s "
                "ORDER BY created_at DESC LIMIT %s OFFSET %s",
                (workflow_id, page_size, offset),
            )
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return rows, total


def get_inbox(
    user_id: str,
    role: str,  # 'reviewer' | 'approver'
    org_id: str,
    doc_type: str | None = None,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list, int]:
    """Return paginated inbox rows for a reviewer or approver."""
    col = "current_reviewer" if role == "reviewer" else "current_approver"
    state_filter = "in_review" if role == "reviewer" else "approved"

    params: list = [user_id, state_filter]
    extra = ""
    if doc_type:
        extra = " AND doc_type=%s"
        params.append(doc_type)

    offset = (page - 1) * page_size
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                f"SELECT COUNT(*) AS cnt FROM document_workflow "
                f"WHERE {col}=%s AND state=%s{extra}",
                params,
            )
            total = cur.fetchone()["cnt"]
            cur.execute(
                f"SELECT * FROM document_workflow "
                f"WHERE {col}=%s AND state=%s{extra} "
                f"ORDER BY submitted_at DESC LIMIT %s OFFSET %s",
                params + [page_size, offset],
            )
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return rows, total


def bootstrap_schema() -> None:
    """Create workflow tables if they don't exist. Idempotent — safe to call on every startup."""
    _ddl = [
        """CREATE TABLE IF NOT EXISTS workflow_config (
          org_id            VARCHAR(64)  NOT NULL,
          doc_type          VARCHAR(32)  NOT NULL,
          assignment_mode   VARCHAR(32)  NOT NULL DEFAULT 'per_document',
          reviewer_role_id  VARCHAR(64)  NULL,
          approver_role_id  VARCHAR(64)  NULL,
          states_json       JSON         NOT NULL,
          updated_at        TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (org_id, doc_type)
        )""",
        """CREATE TABLE IF NOT EXISTS document_workflow (
          workflow_id       CHAR(36)     NOT NULL,
          org_id            VARCHAR(64)  NOT NULL,
          doc_type          VARCHAR(32)  NOT NULL,
          doc_id            VARCHAR(64)  NOT NULL,
          doc_version       VARCHAR(32)  NOT NULL,
          owner_user_id     VARCHAR(64)  NOT NULL,
          state             VARCHAR(32)  NOT NULL DEFAULT 'draft',
          current_reviewer  VARCHAR(64)  NULL,
          current_approver  VARCHAR(64)  NULL,
          state_version     INT          NOT NULL DEFAULT 1,
          submitted_at      TIMESTAMP    NULL,
          approved_at       TIMESTAMP    NULL,
          published_at      TIMESTAMP    NULL,
          created_at        TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (workflow_id),
          UNIQUE KEY uq_doc (doc_type, doc_id, doc_version),
          INDEX idx_reviewer (current_reviewer, state),
          INDEX idx_approver (current_approver, state),
          INDEX idx_org (org_id, doc_type, state)
        )""",
        """CREATE TABLE IF NOT EXISTS document_workflow_events (
          event_id          CHAR(36)     NOT NULL,
          workflow_id       CHAR(36)     NOT NULL,
          from_state        VARCHAR(32)  NULL,
          to_state          VARCHAR(32)  NOT NULL,
          actor_user_id     VARCHAR(64)  NOT NULL,
          comment           TEXT         NULL,
          created_at        TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (event_id),
          INDEX idx_wf (workflow_id, created_at)
        )""",
        """CREATE TABLE IF NOT EXISTS workflow_email_dlq (
          dlq_id            CHAR(36)     NOT NULL,
          workflow_id       CHAR(36)     NULL,
          event_id          CHAR(36)     NULL,
          org_id            VARCHAR(64)  NOT NULL,
          recipient         VARCHAR(255) NOT NULL,
          template_name     VARCHAR(64)  NOT NULL,
          context_json      TEXT         NOT NULL,
          last_error        TEXT         NULL,
          retry_count       INT          NOT NULL DEFAULT 0,
          last_retry_at     TIMESTAMP    NULL,
          status            VARCHAR(32)  NOT NULL DEFAULT 'pending',
          created_at        TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (dlq_id),
          INDEX idx_pending (status, created_at),
          INDEX idx_org (org_id, status)
        )""",
        """CREATE TABLE IF NOT EXISTS org_feature_flags (
          org_id      VARCHAR(64)  NOT NULL,
          flag_name   VARCHAR(64)  NOT NULL,
          flag_value  VARCHAR(255) NOT NULL DEFAULT 'false',
          updated_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (org_id, flag_name)
        )""",
    ]
    _notification_alters = [
        "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS doc_type VARCHAR(32) NULL",
        "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS doc_id VARCHAR(64) NULL",
        "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS workflow_id CHAR(36) NULL",
        "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS workflow_state VARCHAR(32) NULL",
        "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS action_required TINYINT(1) DEFAULT 0",
    ]
    conn = connect_to_rds()
    if not conn:
        logger.warning("bootstrap_schema: no DB connection available")
        return
    try:
        with conn.cursor() as cur:
            for stmt in _ddl:
                cur.execute(stmt)
            for stmt in _notification_alters:
                try:
                    cur.execute(stmt)
                except Exception:
                    pass
        conn.commit()
        logger.info("workflow schema bootstrap complete")
    except Exception as exc:
        logger.error("bootstrap_schema failed: %s", exc)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()
