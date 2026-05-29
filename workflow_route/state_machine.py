"""Workflow state machine — transitions, permission checks, and DB helpers."""

import uuid
from datetime import datetime, timezone

import pymysql.cursors

from db.rds_db import connect_to_rds
from utils.base_logger import get_logger

logger = get_logger(__name__)

# ── Default state machine ─────────────────────────────────────────────────────

DEFAULT_STATES_JSON = {
    "states": ["draft", "quality_review", "governance_review", "approval", "published"],
    "transitions": {
        "draft": ["quality_review"],
        "quality_review": ["governance_review", "draft"],
        "governance_review": ["approval", "quality_review"],
        "approval": ["published", "governance_review"],
        "published": ["draft"],
    },
    "required_permission_per_transition": {
        "draft->quality_review": "workflow.submit",
        "quality_review->governance_review": "workflow.review",
        "quality_review->draft": "workflow.review",
        "governance_review->approval": "workflow.review",
        "governance_review->quality_review": "workflow.review",
        "approval->published": "workflow.approve",
        "approval->governance_review": "workflow.approve",
        "published->draft": "workflow.submit",
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


def get_org_review_frequency(org_id: str) -> str:
    """Return the org's document review cadence enum, or the default ('annual')."""
    from policy_hub.review_lifecycle import DEFAULT_REVIEW_FREQUENCY, normalize_frequency

    if not org_id:
        return DEFAULT_REVIEW_FREQUENCY
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT review_frequency FROM org_review_config WHERE org_id=%s",
                (org_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    return normalize_frequency(row["review_frequency"] if row else None)


def set_org_review_frequency(org_id: str, frequency: str) -> str:
    """Upsert the org's review cadence. Returns the stored (normalized) value."""
    from policy_hub.review_lifecycle import normalize_frequency

    freq = normalize_frequency(frequency)
    conn = connect_to_rds()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO org_review_config (org_id, review_frequency)
                   VALUES (%s, %s)
                   ON DUPLICATE KEY UPDATE review_frequency=VALUES(review_frequency)""",
                (org_id, freq),
            )
        conn.commit()
    finally:
        conn.close()
    return freq


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


def get_workflow_states_for_docs(doc_type: str, doc_ids: list[str]) -> dict[str, str]:
    """Return {doc_id: state} for the latest workflow row per doc_id.

    When multiple workflow rows exist for the same doc_id (different
    doc_version), the one with the most recent created_at wins. Missing
    doc_ids are simply absent from the result.
    """
    ids = [d for d in (doc_ids or []) if d]
    if not ids:
        return {}
    placeholders = ",".join(["%s"] * len(ids))
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                f"SELECT doc_id, state, created_at FROM document_workflow "
                f"WHERE doc_type=%s AND doc_id IN ({placeholders})",
                (doc_type, *ids),
            )
            rows = cur.fetchall() or []
    finally:
        conn.close()

    latest: dict[str, dict] = {}
    for r in rows:
        prev = latest.get(r["doc_id"])
        if not prev or (r.get("created_at") or 0) > (prev.get("created_at") or 0):
            latest[r["doc_id"]] = r
    return {doc_id: r["state"] for doc_id, r in latest.items()}


def get_docs_assigned_to_user(
    doc_type: str,
    user_id: str,
    include_published: bool = False,
) -> list[dict]:
    """Return active workflow rows where ``user_id`` is a QR/GR/Approver party.

    Used by list endpoints (e.g. ``/policy-hub/list``, ``/runbook/results_list``)
    to surface documents assigned to a reviewer/approver even though they are
    neither the owner nor an explicit share recipient.

    Each row in the returned list is a dict::

        {workflow_id, doc_id, doc_version, owner_user_id, state, role, created_at}

    where ``role`` is one of ``quality_reviewer``, ``governance_reviewer``,
    ``approver`` — the column on which the user matched.

    Excludes ``draft`` (not yet submitted) and ``cancelled`` rows. Includes
    ``published`` only when ``include_published=True``. When multiple rows
    exist for the same ``doc_id`` (e.g. different versions), the most recent
    by ``created_at`` wins so the result has one row per ``doc_id``.
    """
    if not doc_type or not user_id:
        return []

    excluded = ["draft", "cancelled"]
    if not include_published:
        excluded.append("published")
    placeholders = ",".join(["%s"] * len(excluded))

    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                f"""SELECT workflow_id, doc_id, doc_version, owner_user_id,
                           state, created_at,
                           CASE
                             WHEN current_quality_reviewer = %s THEN 'quality_reviewer'
                             WHEN current_governance_reviewer = %s THEN 'governance_reviewer'
                             WHEN current_approver = %s THEN 'approver'
                           END AS role
                    FROM document_workflow
                    WHERE doc_type = %s
                      AND state NOT IN ({placeholders})
                      AND (current_quality_reviewer = %s
                           OR current_governance_reviewer = %s
                           OR current_approver = %s)
                    ORDER BY created_at DESC""",
                (
                    user_id, user_id, user_id,
                    doc_type,
                    *excluded,
                    user_id, user_id, user_id,
                ),
            )
            rows = cur.fetchall() or []
    finally:
        conn.close()

    latest: dict[str, dict] = {}
    for r in rows:
        prev = latest.get(r["doc_id"])
        if not prev or (r.get("created_at") or 0) > (prev.get("created_at") or 0):
            latest[r["doc_id"]] = dict(r)
    return list(latest.values())


def get_user_org_id(user_id: str) -> str | None:
    """Resolve org identifier for a user — company_name first, launch_id_fk as fallback.

    Admin users who are the org root (no company_name, no launch_id_fk) use
    ``launch:{user_id}`` so their org_id matches what invited users carry in
    their own launch_id_fk field.

    Returns None if the user doesn't exist or has no resolvable org.
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


def create_workflow(
    org_id: str,
    doc_type: str,
    doc_id: str,
    doc_version: str,
    owner_user_id: str,
    quality_reviewer_user_id: str | None = None,
    governance_reviewer_user_id: str | None = None,
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
                    owner_user_id, state, current_reviewer,
                    current_quality_reviewer, current_governance_reviewer,
                    current_approver, state_version, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,'draft',%s,%s,%s,%s,1,%s)""",
                (
                    workflow_id, org_id, doc_type, doc_id, doc_version,
                    owner_user_id,
                    quality_reviewer_user_id,           # current_reviewer alias = QR
                    quality_reviewer_user_id,
                    governance_reviewer_user_id,
                    approver_user_id,
                    now,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    _append_event(workflow_id, None, "draft", owner_user_id, "Document created", assigned_to_user_id=owner_user_id)
    return get_workflow(workflow_id)


# ── Auto-advance helpers ──────────────────────────────────────────────────────
#
# Forward chain for the 3-stage review workflow. ``draft`` is included so the
# initial submit (draft → quality_review) is recognised as a forward hop and
# can trigger auto-advance for single-reviewer orgs. Send-back transitions
# (governance_review → quality_review, approval → governance_review, etc.)
# are deliberately absent — they must NOT chain.

_FORWARD_NEXT = {
    "draft":             "quality_review",
    "quality_review":    "governance_review",
    "governance_review": "approval",
    "approval":          "published",
}

AUTO_ADVANCE_COMMENT = "[auto-advance: same reviewer]"


def _next_forward_state(state: str) -> str | None:
    return _FORWARD_NEXT.get(state)


def _is_forward_hop(from_state: str, to_state: str) -> bool:
    return _FORWARD_NEXT.get(from_state) == to_state


def _user_col_for_state(state: str) -> str | None:
    if state == "quality_review":
        return "current_quality_reviewer"
    if state == "governance_review":
        return "current_governance_reviewer"
    if state in ("approval", "published"):
        return "current_approver"
    return None


def _role_col_for_state(state: str) -> str | None:
    # Quality review is always user-based (round-robin at submit), so it has no
    # role column. Governance and approval are role-broadcast capable.
    if state == "governance_review":
        return "current_governance_reviewer_role"
    if state in ("approval", "published"):
        return "current_approver_role"
    return None


def _assignee_for_state(row: dict, state: str) -> str | None:
    """Return the resolved user_id assignee for a state, or None if the stage
    is role-broadcast (no specific user) or unconfigured.
    """
    user_col = _user_col_for_state(state)
    if user_col:
        return row.get(user_col)
    if state == "draft":
        return row.get("owner_user_id")
    return None


def actor_eligible_for_state(
    row: dict,
    state: str,
    actor_user_id: str,
    actor_role_ids: set[str],
) -> bool:
    """True if ``actor_user_id`` can act on this stage.

    Eligible if EITHER:
      - the user-column for the stage matches actor (direct assignment), OR
      - the user-column is NULL AND the role-column is set AND the actor is
        a member of that role (broadcast assignment, first-to-act-wins).
    """
    user_col = _user_col_for_state(state)
    if user_col:
        direct = row.get(user_col)
        if direct and direct == actor_user_id:
            return True
        role_col = _role_col_for_state(state)
        if role_col and not direct:
            role_id = row.get(role_col)
            if role_id and role_id in actor_role_ids:
                return True
    if state == "draft" and row.get("owner_user_id") == actor_user_id:
        return True
    return False


def get_actor_role_ids(user_id: str) -> set[str]:
    """Return the set of role_ids the user belongs to.

    Reads ``permissions.role.id`` from the user's row. Non-admin users carry a
    single role today; the return type is a set so the eligibility check
    composes cleanly if/when multi-role membership is added.
    """
    import json
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT permissions FROM users WHERE user_id=%s",
                (user_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row or not row.get("permissions"):
        return set()
    try:
        perms = json.loads(row["permissions"]) if isinstance(row["permissions"], str) else row["permissions"]
    except Exception:
        return set()
    role = (perms or {}).get("role") or {}
    role_id = role.get("id")
    return {role_id} if role_id else set()


def _apply_single_transition(
    cur,
    config: dict,
    row: dict,
    to_state: str,
    actor_user_id: str,
    comment: str | None,
    *,
    quality_reviewer_user_id: str | None = None,
    governance_reviewer_user_id: str | None = None,
    approver_user_id: str | None = None,
    is_auto: bool,
) -> tuple[dict, dict]:
    """Apply one state-machine hop within an open cursor.

    Validates the transition, writes the UPDATE, and inserts the event row —
    all under the caller's transaction so a chained auto-advance commits atomically.
    Returns (updated_row_dict, hop_summary).
    """
    current_state = row["state"]
    current_version = row["state_version"]

    allowed = config["states_json"]["transitions"].get(current_state, [])
    if to_state not in allowed:
        raise WorkflowTransitionError(
            f"Transition {current_state!r} → {to_state!r} not allowed"
        )

    now = datetime.now(timezone.utc)
    updates: dict = {
        "state": to_state,
        "state_version": current_version + 1,
    }
    if to_state == "quality_review" and quality_reviewer_user_id:
        updates["current_quality_reviewer"] = quality_reviewer_user_id
        updates["current_reviewer"] = quality_reviewer_user_id  # legacy alias
    if to_state == "governance_review" and governance_reviewer_user_id:
        updates["current_governance_reviewer"] = governance_reviewer_user_id
    if to_state in ("approval", "published") and approver_user_id:
        updates["current_approver"] = approver_user_id
    if to_state == "quality_review" and not row.get("submitted_at"):
        updates["submitted_at"] = now
    if to_state == "approval":
        updates["approved_at"] = now
    if to_state == "published":
        updates["published_at"] = now

    # Claim role-broadcast stages on the way out. When transitioning AWAY from
    # a stage whose user-column is NULL but role-column is set, the actor (who
    # by the eligibility check is a role member) "claims" the slot: their id
    # gets written to the *_reviewer column and the *_role column is cleared.
    # Records who acted in the audit trail.
    from_state = current_state
    user_col = _user_col_for_state(from_state)
    role_col = _role_col_for_state(from_state)
    if user_col and role_col and not row.get(user_col) and row.get(role_col):
        updates[user_col] = actor_user_id
        updates[role_col] = None

    set_clause = ", ".join(f"{k}=%s" for k in updates)
    cur.execute(
        f"UPDATE document_workflow SET {set_clause} WHERE workflow_id=%s",
        (*updates.values(), row["workflow_id"]),
    )

    # Reflect the writes back into the in-memory row so the auto-advance loop
    # can read the freshly-assigned reviewer for the next stage.
    row = {**row, **updates}

    assigned_to = _assignee_for_state(row, to_state)

    event_id = str(uuid.uuid4())
    cur.execute(
        """INSERT INTO document_workflow_events
           (event_id, workflow_id, from_state, to_state,
            actor_user_id, assigned_to_user_id, comment)
           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
        (event_id, row["workflow_id"], current_state, to_state,
         actor_user_id, assigned_to, comment),
    )

    return row, {
        "from_state": current_state,
        "to_state": to_state,
        "event_id": event_id,
        "assigned_to": assigned_to,
        "auto": is_auto,
        "comment": comment,
    }


def transition(
    workflow_id: str,
    expected_state_version: int,
    to_state: str,
    actor_user_id: str,
    comment: str | None = None,
    quality_reviewer_user_id: str | None = None,
    governance_reviewer_user_id: str | None = None,
    approver_user_id: str | None = None,
) -> dict:
    """Perform a state transition with optimistic locking.

    After the requested (manual) transition completes, if the destination
    state's assignee equals ``actor_user_id`` the workflow auto-advances to
    the next forward stage. The chain repeats until the workflow reaches
    ``published``, hits a stage with a different assignee, or has no further
    forward state. All hops run under one ``FOR UPDATE`` lock and commit as
    a single DB transaction.

    Returns the updated workflow row plus two extra keys:
      ``_event_id`` — the first (manual) hop's event id (legacy callers).
      ``_auto_advance_chain`` — list of hop summaries; the first entry has
        ``auto=False``, subsequent hops have ``auto=True``.

    Raises WorkflowConflictError if state_version doesn't match.
    Raises WorkflowTransitionError if the transition isn't allowed.
    """
    chain: list[dict] = []

    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM document_workflow WHERE workflow_id=%s FOR UPDATE",
                (workflow_id,),
            )
            row = cur.fetchone()
            if not row:
                raise WorkflowNotFoundError(workflow_id)

            if row["state_version"] != expected_state_version:
                raise WorkflowConflictError(
                    f"State version mismatch: expected {expected_state_version}, "
                    f"got {row['state_version']}"
                )

            config = get_workflow_config(row["org_id"], row["doc_type"])
            row = dict(row)

            row, hop = _apply_single_transition(
                cur, config, row, to_state, actor_user_id, comment,
                quality_reviewer_user_id=quality_reviewer_user_id,
                governance_reviewer_user_id=governance_reviewer_user_id,
                approver_user_id=approver_user_id,
                is_auto=False,
            )
            chain.append(hop)

            # Auto-advance loop. Only enters for forward transitions — a
            # send_back must never chain into more approvals. Each iteration
            # asks: is the actor eligible to act at the stage we just entered
            # (either as direct assignee or as a member of the role-broadcast
            # set)? If yes, they would just click "Approve" again, so do it
            # for them. Role membership is fetched once per call.
            if _is_forward_hop(hop["from_state"], hop["to_state"]):
                actor_role_ids = get_actor_role_ids(actor_user_id)
                while True:
                    cur_state = row["state"]
                    if not actor_eligible_for_state(row, cur_state, actor_user_id, actor_role_ids):
                        break
                    next_state = _next_forward_state(cur_state)
                    if not next_state:
                        break
                    if next_state not in config["states_json"]["transitions"].get(cur_state, []):
                        break
                    row, hop = _apply_single_transition(
                        cur, config, row, next_state, actor_user_id,
                        AUTO_ADVANCE_COMMENT,
                        is_auto=True,
                    )
                    chain.append(hop)
        conn.commit()
    finally:
        conn.close()

    updated = get_workflow(workflow_id)
    updated["_event_id"] = chain[0]["event_id"]
    updated["_auto_advance_chain"] = chain
    return updated


def cancel_workflow(workflow_id: str, actor_user_id: str, comment: str | None = None) -> dict:
    """Reset an in-flight workflow back to draft state.

    Only the workflow owner may cancel. Resets state to 'draft', clears
    reviewer/approver assignments, and records an event for the audit trail.
    Raises WorkflowNotFoundError if not found, WorkflowTransitionError if the
    actor is not the owner or the workflow is already published.
    """
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM document_workflow WHERE workflow_id=%s FOR UPDATE",
                (workflow_id,),
            )
            row = cur.fetchone()
            if not row:
                raise WorkflowNotFoundError(workflow_id)

            row = dict(row)
            if row["owner_user_id"] != actor_user_id:
                raise WorkflowTransitionError("Only the workflow owner can cancel a review")
            if row["state"] == "published":
                raise WorkflowTransitionError("Cannot cancel a published workflow")
            if row["state"] == "draft":
                raise WorkflowTransitionError("Workflow is already in draft state")

            prev_state = row["state"]
            cur.execute(
                """UPDATE document_workflow
                   SET state='draft',
                       state_version=state_version+1,
                       current_reviewer=NULL,
                       current_quality_reviewer=NULL,
                       current_governance_reviewer=NULL,
                       current_approver=NULL,
                       submitted_at=NULL,
                       approved_at=NULL
                   WHERE workflow_id=%s""",
                (workflow_id,),
            )
            event_id = str(uuid.uuid4())
            cur.execute(
                """INSERT INTO document_workflow_events
                   (event_id, workflow_id, from_state, to_state,
                    actor_user_id, assigned_to_user_id, comment)
                   VALUES (%s,%s,%s,'draft',%s,%s,%s)""",
                (event_id, workflow_id, prev_state, actor_user_id, actor_user_id,
                 comment or "Review cancelled by owner"),
            )
        conn.commit()
    finally:
        conn.close()

    return get_workflow(workflow_id)


# ── Role-based assignee resolution ───────────────────────────────────────────


class RoleResolutionError(Exception):
    """Raised when a role_id cannot be resolved to an eligible user."""


def pick_user_for_role(role_id: str, requesting_user_id: str) -> tuple[str, str]:
    """Resolve a workflow role assignment to a concrete user via least-loaded
    round-robin.

    Picks the user with the role who has the fewest active (non-terminal)
    workflows assigned. Ties broken by ``user_id`` lexicographic order so
    repeated calls are deterministic.

    Returns (user_id, email). Raises RoleResolutionError if the role has no
    eligible members in the requester's org.
    """
    from shared_configuration import get_role_users_from_db

    conn = connect_to_rds()
    try:
        role_users = get_role_users_from_db(conn, requesting_user_id, role_id)
        if not role_users:
            raise RoleResolutionError(
                f"No active users found for role {role_id!r}"
            )

        user_ids = [u["user_id"] for u in role_users]
        counts: dict[str, int] = {uid: 0 for uid in user_ids}

        placeholders = ",".join(["%s"] * len(user_ids))
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            # Count active workflows (not draft, not published) where this user
            # is any of the three reviewer columns. Used as load signal so the
            # round-robin spreads new assignments fairly.
            cur.execute(
                f"""SELECT u AS user_id, COUNT(*) AS cnt FROM (
                       SELECT current_quality_reviewer AS u FROM document_workflow
                       WHERE state NOT IN ('draft','published')
                         AND current_quality_reviewer IN ({placeholders})
                       UNION ALL
                       SELECT current_governance_reviewer FROM document_workflow
                       WHERE state NOT IN ('draft','published')
                         AND current_governance_reviewer IN ({placeholders})
                       UNION ALL
                       SELECT current_approver FROM document_workflow
                       WHERE state NOT IN ('draft','published')
                         AND current_approver IN ({placeholders})
                    ) t
                    GROUP BY u""",
                (*user_ids, *user_ids, *user_ids),
            )
            for r in cur.fetchall():
                if r["user_id"] in counts:
                    counts[r["user_id"]] = int(r["cnt"])
    finally:
        conn.close()

    chosen = min(role_users, key=lambda u: (counts[u["user_id"]], u["user_id"]))
    return chosen["user_id"], chosen.get("email") or ""


def _append_event(
    workflow_id: str,
    from_state: str | None,
    to_state: str,
    actor_user_id: str,
    comment: str | None,
    assigned_to_user_id: str | None = None,
) -> str:
    event_id = str(uuid.uuid4())
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                """INSERT INTO document_workflow_events
                   (event_id, workflow_id, from_state, to_state,
                    actor_user_id, assigned_to_user_id, comment)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (event_id, workflow_id, from_state, to_state,
                 actor_user_id, assigned_to_user_id, comment),
            )
        conn.commit()
    finally:
        conn.close()
    return event_id


def add_comment(
    workflow_id: str,
    actor_user_id: str,
    comment: str,
    attachments: list[dict] | None = None,
) -> dict:
    """Insert a manual comment row into the workflow activity feed.

    ``attachments`` is a list of {s3_key, original_name, content_type, size}
    dicts; an ``uploaded_at`` timestamp is stamped server-side. Workflow-scoped
    (from_state/to_state left NULL) so the comment appears in every step's
    activity panel.
    """
    import json as _json
    event_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()
    attachments_payload = None
    if attachments:
        stamped = []
        for att in attachments:
            stamped.append({
                "s3_key": att.get("s3_key"),
                "original_name": att.get("original_name"),
                "content_type": att.get("content_type"),
                "size": att.get("size"),
                "uploaded_at": now_iso,
            })
        attachments_payload = _json.dumps(stamped)

    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                """INSERT INTO document_workflow_events
                   (event_id, workflow_id, from_state, to_state, kind,
                    actor_user_id, assigned_to_user_id, comment, attachments_json)
                   VALUES (%s,%s,NULL,NULL,'comment',%s,NULL,%s,%s)""",
                (event_id, workflow_id, actor_user_id, comment, attachments_payload),
            )
            cur.execute(
                "SELECT created_at FROM document_workflow_events WHERE event_id=%s",
                (event_id,),
            )
            created_row = cur.fetchone() or {}
        conn.commit()
    finally:
        conn.close()
    return {
        "event_id": event_id,
        "created_at": created_row.get("created_at"),
    }


def get_workflow_history(
    workflow_id: str,
    page: int = 1,
    page_size: int = 50,
    state: str | None = None,
) -> tuple[list, int]:
    """Unified per-workflow history: state transitions, comments, field edits.

    Each row carries a ``kind`` discriminator ('state_transition', 'comment',
    or 'field_edit') so the frontend can render them in one chronological feed
    without separate fetches. Rows are sorted newest-first.

    When ``state`` is provided, the feed is scoped to the given workflow stage:
    transition rows are filtered to those where ``from_state`` or ``to_state``
    matches; comment rows are always included (comments are workflow-wide);
    field-edit rows are always included (no per-state mapping).

    Attachment rows have presigned download URLs injected per item.
    """
    from utils.s3_utils import generate_presigned_url

    offset = (page - 1) * page_size

    if state:
        transition_where = (
            "workflow_id=%s AND (kind='comment' OR from_state=%s OR to_state=%s)"
        )
        transition_params: tuple = (workflow_id, state, state)
    else:
        transition_where = "workflow_id=%s"
        transition_params = (workflow_id,)

    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                f"SELECT COUNT(*) AS cnt FROM document_workflow_events WHERE {transition_where}",
                transition_params,
            )
            transition_total = cur.fetchone()["cnt"]
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM document_field_events WHERE workflow_id=%s",
                (workflow_id,),
            )
            field_total = cur.fetchone()["cnt"]
            total = transition_total + field_total

            query = (
                "SELECT event_id, workflow_id, from_state, to_state, kind, "
                "actor_user_id, assigned_to_user_id, comment, attachments_json, created_at, "
                "NULL AS field_path, NULL AS before_snippet, NULL AS after_snippet, "
                "NULL AS delta_chars, NULL AS previous_result_id, NULL AS new_result_id "
                f"FROM document_workflow_events WHERE {transition_where} "
                "UNION ALL "
                "SELECT event_id, workflow_id, NULL AS from_state, NULL AS to_state, "
                "'field_edit' AS kind, "
                "actor_user_id, NULL AS assigned_to_user_id, NULL AS comment, "
                "NULL AS attachments_json, created_at, "
                "field_path, before_snippet, after_snippet, delta_chars, "
                "previous_result_id, new_result_id "
                "FROM document_field_events WHERE workflow_id=%s "
                "ORDER BY created_at DESC LIMIT %s OFFSET %s"
            )
            cur.execute(query, (*transition_params, workflow_id, page_size, offset))
            rows = [dict(r) for r in cur.fetchall()]

        # Batched email lookup for actor + assignee — single query per page.
        user_ids = {
            uid
            for row in rows
            for uid in (row.get("actor_user_id"), row.get("assigned_to_user_id"))
            if uid
        }
        emails: dict[str, str] = {}
        if user_ids:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                placeholders = ",".join(["%s"] * len(user_ids))
                cur.execute(
                    f"SELECT user_id, email FROM users WHERE user_id IN ({placeholders})",
                    tuple(user_ids),
                )
                emails = {r["user_id"]: r.get("email") for r in cur.fetchall()}
    finally:
        conn.close()

    import json as _json
    for row in rows:
        row["actor_email"] = emails.get(row.get("actor_user_id"))
        row["assigned_to_email"] = emails.get(row.get("assigned_to_user_id"))

        raw_kind = row.get("kind")
        if raw_kind == "field_edit":
            pass
        elif raw_kind == "comment":
            row["kind"] = "comment"
        else:
            row["kind"] = "state_transition"

        att_raw = row.get("attachments_json")
        if not att_raw:
            row["attachments"] = []
        else:
            try:
                att_list = att_raw if isinstance(att_raw, list) else _json.loads(att_raw)
            except (TypeError, ValueError):
                att_list = []
            for att in att_list:
                s3_key = att.get("s3_key")
                if s3_key:
                    att["download_url"] = generate_presigned_url(s3_key, expiration=3600)
            row["attachments"] = att_list
        row.pop("attachments_json", None)

    return rows, total


def get_inbox(
    user_id: str,
    role: str,  # 'quality_reviewer' | 'governance_reviewer' | 'approver' | 'reviewer' (legacy)
    org_id: str,
    doc_type: str | None = None,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list, int]:
    """Return paginated inbox rows for a reviewer or approver by stage role."""
    if role in ("reviewer", "quality_reviewer"):
        col = "current_quality_reviewer"
        state_filter = "quality_review"
    elif role == "governance_reviewer":
        col = "current_governance_reviewer"
        state_filter = "governance_review"
    elif role == "approver":
        col = "current_approver"
        state_filter = "approval"
    else:
        raise ValueError(f"Unknown role: {role}")

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


def get_workflow_for_doc_any_role(
    doc_type: str,
    doc_id: str,
    user_id: str,
) -> dict | None:
    """Return the active WorkflowRow for a doc if the user is a party to it.

    Visible if the user is owner / quality_reviewer / governance_reviewer / approver.
    Returns None if no row exists or the user is not a party.
    """
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                """SELECT * FROM document_workflow
                   WHERE doc_type=%s AND doc_id=%s
                     AND (owner_user_id=%s OR current_reviewer=%s
                          OR current_quality_reviewer=%s OR current_governance_reviewer=%s
                          OR current_approver=%s)
                   ORDER BY created_at DESC LIMIT 1""",
                (doc_type, doc_id, user_id, user_id, user_id, user_id, user_id),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


# ── Workflow enrichment for client consumption ────────────────────────────────

# Stepper order shown to clients. Keep aligned with DEFAULT_STATES_JSON["states"].
_STEP_DEFINITIONS: list[tuple[str, str]] = [
    ("draft", "Draft"),
    ("quality_review", "Quality Review"),
    ("governance_review", "Governance Review"),
    ("approval", "Approval"),
    ("published", "Published"),
]
_STEP_ORDER: list[str] = [sid for sid, _ in _STEP_DEFINITIONS]

# State → column on document_workflow holding the user_id responsible for that step.
_STATE_ASSIGNEE_COL: dict[str, str] = {
    "draft": "owner_user_id",
    "quality_review": "current_quality_reviewer",
    "governance_review": "current_governance_reviewer",
    "approval": "current_approver",
    "published": "owner_user_id",
}

# State → (column, role label) for the viewer's role lookup.
_ROLE_COLUMNS: list[tuple[str, str]] = [
    ("owner_user_id", "owner"),
    ("current_quality_reviewer", "quality_reviewer"),
    ("current_governance_reviewer", "governance_reviewer"),
    ("current_approver", "approver"),
]

# State → (assignee column, viewer role that can act on it).
_STATE_ACTOR: dict[str, tuple[str, str]] = {
    "quality_review": ("current_quality_reviewer", "quality_reviewer"),
    "governance_review": ("current_governance_reviewer", "governance_reviewer"),
    "approval": ("current_approver", "approver"),
}


def enrich_workflow_for_viewer(workflow: dict, viewer_user_id: str) -> dict:
    """Augment a raw document_workflow row with assignee details, a per-step
    timeline, and the viewer's permitted actions on the current state.

    Adds these keys to the returned dict:
      - assignees: {owner, quality_reviewer, governance_reviewer, approver},
        each {user_id, email} or None.
      - steps: ordered list of {id, label, status, assignee, started_at,
        finished_at}, where status ∈ {complete, active, upcoming}.
      - viewer: {user_id, email, role, permitted_actions,
        is_assignee_for_current_step}. role is None for shared-access readers
        who aren't a workflow party.
    """
    enriched = dict(workflow)
    state = workflow.get("state") or "draft"

    user_ids: set[str] = set()
    for col, _ in _ROLE_COLUMNS:
        uid = workflow.get(col)
        if uid:
            user_ids.add(uid)
    if viewer_user_id:
        user_ids.add(viewer_user_id)

    emails: dict[str, str] = {}
    if user_ids:
        conn = connect_to_rds()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                placeholders = ",".join(["%s"] * len(user_ids))
                cur.execute(
                    f"SELECT user_id, email FROM users WHERE user_id IN ({placeholders})",
                    tuple(user_ids),
                )
                emails = {r["user_id"]: r.get("email") for r in cur.fetchall()}
        finally:
            conn.close()

    def _principal(col: str) -> dict | None:
        uid = workflow.get(col)
        if not uid:
            return None
        return {"user_id": uid, "email": emails.get(uid)}

    enriched["assignees"] = {
        "owner": _principal("owner_user_id"),
        "quality_reviewer": _principal("current_quality_reviewer"),
        "governance_reviewer": _principal("current_governance_reviewer"),
        "approver": _principal("current_approver"),
    }

    # Per-step timing from the events table: started_at = first event whose
    # to_state == step; finished_at = first event whose from_state == step.
    step_started: dict[str, object] = {}
    step_finished: dict[str, object] = {}
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT from_state, to_state, created_at FROM document_workflow_events "
                "WHERE workflow_id=%s ORDER BY created_at ASC",
                (workflow["workflow_id"],),
            )
            events = cur.fetchall() or []
    finally:
        conn.close()

    for ev in events:
        to_state = ev.get("to_state")
        from_state = ev.get("from_state")
        ts = ev.get("created_at")
        if to_state in _STEP_ORDER and to_state not in step_started:
            step_started[to_state] = ts
        if from_state in _STEP_ORDER and from_state not in step_finished:
            step_finished[from_state] = ts

    try:
        current_idx = _STEP_ORDER.index(state)
    except ValueError:
        current_idx = -1

    steps: list[dict] = []
    for idx, (sid, slabel) in enumerate(_STEP_DEFINITIONS):
        if current_idx < 0:
            status = "upcoming"
        elif idx < current_idx:
            status = "complete"
        elif idx == current_idx:
            status = "active"
        else:
            status = "upcoming"
        assignee_col = _STATE_ASSIGNEE_COL.get(sid)
        steps.append({
            "id": sid,
            "label": slabel,
            "status": status,
            "assignee": _principal(assignee_col) if assignee_col else None,
            "started_at": step_started.get(sid),
            "finished_at": step_finished.get(sid),
        })
    enriched["steps"] = steps

    # Viewer role: first matching column wins (owner takes precedence over
    # reviewer slots in the rare case a user holds both).
    role: str | None = None
    for col, label in _ROLE_COLUMNS:
        if workflow.get(col) == viewer_user_id:
            role = label
            break

    permitted_actions: list[str] = []
    is_current_assignee = False
    actor = _STATE_ACTOR.get(state)
    if actor:
        assignee_col, actor_role = actor
        assigned_uid = workflow.get(assignee_col)
        # Mirrors the assignment check in routes.py review_document(): the
        # named assignee may act; if the slot is empty, the owner can act
        # to unblock the workflow.
        if role == actor_role and assigned_uid == viewer_user_id:
            permitted_actions = ["approve", "send_back"]
            is_current_assignee = True
        elif not assigned_uid and role == "owner":
            permitted_actions = ["approve", "send_back"]
            is_current_assignee = True
    elif state == "draft" and role == "owner":
        permitted_actions = ["submit"]
        is_current_assignee = True

    # Owner can cancel any in-flight workflow (matches cancel_workflow() rules).
    if role == "owner" and state not in ("draft", "published") and "cancel" not in permitted_actions:
        permitted_actions.append("cancel")
    # Owner publishes from the approval stage after the approver acts; that
    # path is covered by the actor branch above (owner-as-fallback when no
    # approver is assigned). Once the workflow reaches 'published' there are
    # no further actions.

    enriched["viewer"] = {
        "user_id": viewer_user_id,
        "email": emails.get(viewer_user_id),
        "role": role,
        "permitted_actions": permitted_actions,
        "is_assignee_for_current_step": is_current_assignee,
    }

    return enriched


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
          workflow_id                   CHAR(36)     NOT NULL,
          org_id                        VARCHAR(64)  NOT NULL,
          doc_type                      VARCHAR(32)  NOT NULL,
          doc_id                        VARCHAR(64)  NOT NULL,
          doc_version                   VARCHAR(32)  NOT NULL,
          owner_user_id                 VARCHAR(64)  NOT NULL,
          state                         VARCHAR(32)  NOT NULL DEFAULT 'draft',
          current_reviewer              VARCHAR(64)  NULL,
          current_quality_reviewer         VARCHAR(64)  NULL,
          current_governance_reviewer      VARCHAR(64)  NULL,
          current_approver                 VARCHAR(64)  NULL,
          current_governance_reviewer_role VARCHAR(64)  NULL,
          current_approver_role            VARCHAR(64)  NULL,
          state_version                 INT          NOT NULL DEFAULT 1,
          submitted_at                  TIMESTAMP    NULL,
          approved_at                   TIMESTAMP    NULL,
          published_at                  TIMESTAMP    NULL,
          created_at                    TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (workflow_id),
          UNIQUE KEY uq_doc (doc_type, doc_id, doc_version),
          INDEX idx_reviewer (current_reviewer, state),
          INDEX idx_quality_reviewer (current_quality_reviewer, state),
          INDEX idx_governance_reviewer (current_governance_reviewer, state),
          INDEX idx_approver (current_approver, state),
          INDEX idx_governance_reviewer_role (current_governance_reviewer_role, state),
          INDEX idx_approver_role (current_approver_role, state),
          INDEX idx_org (org_id, doc_type, state)
        )""",
        """CREATE TABLE IF NOT EXISTS document_workflow_events (
          event_id            CHAR(36)     NOT NULL,
          workflow_id         CHAR(36)     NOT NULL,
          from_state          VARCHAR(32)  NULL,
          to_state            VARCHAR(32)  NULL,
          kind                VARCHAR(32)  NOT NULL DEFAULT 'transition',
          actor_user_id       VARCHAR(64)  NOT NULL,
          assigned_to_user_id VARCHAR(64)  NULL,
          comment             TEXT         NULL,
          attachments_json    JSON         NULL,
          created_at          TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (event_id),
          INDEX idx_wf (workflow_id, created_at),
          INDEX idx_assignee (assigned_to_user_id, created_at),
          INDEX idx_kind (workflow_id, kind, created_at)
        )""",
        # Field-level edit events for any document (runbook, policy, …). Lives
        # outside document_workflow_events because edits don't have a to_state
        # and can occur before a workflow row exists (workflow_id nullable).
        # Snippets cap at 500 chars to keep rows small; delta_chars records the
        # total length delta so the UI can summarize without inflating storage.
        """CREATE TABLE IF NOT EXISTS document_field_events (
          event_id           CHAR(36)     NOT NULL,
          workflow_id        CHAR(36)     NULL,
          doc_type           VARCHAR(32)  NOT NULL,
          doc_id             VARCHAR(64)  NOT NULL,
          previous_result_id VARCHAR(64)  NULL,
          new_result_id      VARCHAR(64)  NULL,
          actor_user_id      VARCHAR(64)  NOT NULL,
          field_path         VARCHAR(512) NOT NULL,
          before_snippet     VARCHAR(500) NULL,
          after_snippet      VARCHAR(500) NULL,
          delta_chars        INT          NULL,
          created_at         TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (event_id),
          INDEX idx_wf (workflow_id, created_at),
          INDEX idx_doc (doc_type, doc_id, created_at)
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
        # Org-wide document review cadence. One row per org; every document in
        # the org follows the same review cycle (frequency enum -> interval in
        # months is resolved in policy_hub.review_lifecycle).
        """CREATE TABLE IF NOT EXISTS org_review_config (
          org_id            VARCHAR(64)  NOT NULL,
          review_frequency  VARCHAR(32)  NOT NULL DEFAULT 'annual',
          updated_at        TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (org_id)
        )""",
    ]
    _notification_alters = [
        "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS doc_type VARCHAR(32) NULL",
        "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS doc_id VARCHAR(64) NULL",
        "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS workflow_id CHAR(36) NULL",
        "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS workflow_state VARCHAR(32) NULL",
        "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS action_required TINYINT(1) DEFAULT 0",
    ]
    # Migration: add multi-stage reviewer columns + backfill from legacy current_reviewer.
    # MySQL versions vary on IF NOT EXISTS for ADD COLUMN; we wrap each in try/except.
    _workflow_alters = [
        "ALTER TABLE document_workflow ADD COLUMN current_quality_reviewer VARCHAR(64) NULL",
        "ALTER TABLE document_workflow ADD COLUMN current_governance_reviewer VARCHAR(64) NULL",
        # Role-broadcast assignment columns. When set, ANY active member of the
        # role can act at that stage; whoever clicks Approve/Send-back first
        # claims the slot (the row's *_reviewer column is updated to their id
        # and the *_role column is cleared in the same transaction).
        "ALTER TABLE document_workflow ADD COLUMN current_governance_reviewer_role VARCHAR(64) NULL",
        "ALTER TABLE document_workflow ADD COLUMN current_approver_role VARCHAR(64) NULL",
        "ALTER TABLE document_workflow ADD INDEX idx_quality_reviewer (current_quality_reviewer, state)",
        "ALTER TABLE document_workflow ADD INDEX idx_governance_reviewer (current_governance_reviewer, state)",
        "ALTER TABLE document_workflow ADD INDEX idx_governance_reviewer_role (current_governance_reviewer_role, state)",
        "ALTER TABLE document_workflow ADD INDEX idx_approver_role (current_approver_role, state)",
        # Backfill quality reviewer from legacy column where empty.
        "UPDATE document_workflow SET current_quality_reviewer = current_reviewer "
        "WHERE current_quality_reviewer IS NULL AND current_reviewer IS NOT NULL",
        # State value rename: in_review → quality_review, approved → approval.
        "UPDATE document_workflow SET state='quality_review' WHERE state='in_review'",
        "UPDATE document_workflow SET state='approval' WHERE state='approved'",
        "UPDATE document_workflow SET state='draft' WHERE state='changes_requested'",
        # Per-event assignee tracking — captures who the work was handed off to on each transition.
        "ALTER TABLE document_workflow_events ADD COLUMN assigned_to_user_id VARCHAR(64) NULL",
        "ALTER TABLE document_workflow_events ADD INDEX idx_assignee (assigned_to_user_id, created_at)",
        # Manual comments + screenshot attachments on the workflow activity feed.
        # kind='comment' rows leave from_state/to_state NULL; kind='transition'
        # is the legacy state-change row and is the default for existing data.
        "ALTER TABLE document_workflow_events ADD COLUMN kind VARCHAR(32) NOT NULL DEFAULT 'transition'",
        "ALTER TABLE document_workflow_events ADD COLUMN attachments_json JSON NULL",
        "ALTER TABLE document_workflow_events MODIFY COLUMN to_state VARCHAR(32) NULL",
        "ALTER TABLE document_workflow_events ADD INDEX idx_kind (workflow_id, kind, created_at)",
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
            for stmt in _workflow_alters:
                try:
                    cur.execute(stmt)
                except Exception:
                    pass  # column/index may already exist; ignore
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
