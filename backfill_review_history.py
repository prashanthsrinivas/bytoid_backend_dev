"""Backfill Review & Revision History for already-published documents.

Documents that completed their review workflow *before* the per-stage recorder
existed have an empty history and show "No history recorded" in the UI. This
script walks every published ``document_workflow`` row and, for each:

  1. Reconstructs the full per-stage trail (submit / quality / governance /
     send-back) from ``document_workflow_events`` — only when the document has no
     history yet, so it is non-destructive.
  2. Stamps the review-cycle metadata and appends the terminal ``published`` row
     from the workflow row (``published_at``, ``doc_version``, approver) — exactly
     what the live publish hook does, but using the original publish timestamp.

Covers every workflow doc type:
  - policy / procedure / standard -> policy_hub.routes.apply_publication_to_policy
  - runbook (assessment report)   -> runbook.routes.apply_publication_to_runbook_result
  - report  (standalone report)   -> ai_reporting.routes.apply_publication_to_report

Idempotent: the apply_publication_to_* functions skip a doc whose latest history
row already records the same version as published, so re-running is safe.

Usage:
    python3 backfill_review_history.py --dry-run          # report only, no writes
    python3 backfill_review_history.py                     # apply to all doc types
    python3 backfill_review_history.py --doc-type runbook  # restrict to one type
    python3 backfill_review_history.py --owner <user_id>   # restrict to one owner
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

import pymysql.cursors

from db.rds_db import connect_to_rds
from utils.base_logger import get_logger

logger = get_logger(__name__, log_level="INFO")

POLICY_HUB_DOC_TYPES = {"policy", "procedure", "standard"}
ALL_DOC_TYPES = POLICY_HUB_DOC_TYPES | {"runbook", "report"}


def _fetch_published_workflows(doc_type: str | None, owner: str | None) -> list[dict]:
    """Return the latest published workflow row per (doc_type, doc_id).

    Ordering by published_at/created_at DESC and de-duping on (doc_type, doc_id)
    means a document re-published across multiple versions only backfills its most
    recent publish — matching what the per-doc backfill endpoint does.
    """
    clauses = ["state = 'published'"]
    params: list = []
    if doc_type:
        clauses.append("doc_type = %s")
        params.append(doc_type)
    if owner:
        clauses.append("owner_user_id = %s")
        params.append(owner)
    where = " AND ".join(clauses)

    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                f"SELECT * FROM document_workflow WHERE {where} "
                "ORDER BY published_at DESC, created_at DESC",
                params,
            )
            rows = cur.fetchall() or []
    finally:
        conn.close()

    latest: dict[tuple, dict] = {}
    for row in rows:
        key = (row.get("doc_type"), row.get("doc_id"))
        latest.setdefault(key, dict(row))
    return list(latest.values())


def _reconstruct_entries_from_events(wf: dict) -> list[dict]:
    """Rebuild the per-stage Review & Revision History from workflow events.

    Reads every ``document_workflow_events`` row for the workflow in chronological
    order and maps each transition (submit / quality / governance / send-back) to
    a revision entry, using the same descriptions the live recorder produces. The
    terminal ``published`` hop is intentionally excluded — the publish backfill
    (apply_publication_to_*) appends that row, with the review-cadence metadata.
    """
    from policy_hub.routes import _resolve_email
    from policy_hub.review_lifecycle import build_revision_entry
    from workflow_route.routes import _milestone_for_hop
    from workflow_route.state_machine import AUTO_ADVANCE_COMMENT

    workflow_id = wf.get("workflow_id")
    if not workflow_id:
        return []
    doc_version = wf.get("doc_version") or "1.0"

    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT from_state, to_state, actor_user_id, comment, created_at "
                "FROM document_workflow_events WHERE workflow_id=%s "
                "ORDER BY created_at ASC, event_id ASC",
                (workflow_id,),
            )
            events = cur.fetchall() or []
    finally:
        conn.close()

    entries: list[dict] = []
    email_cache: dict = {}
    for ev in events:
        actor = ev.get("actor_user_id")
        if actor not in email_cache:
            email_cache[actor] = _resolve_email(actor) or ""
        is_auto = ev.get("comment") == AUTO_ADVANCE_COMMENT
        milestone = _milestone_for_hop({
            "from_state": ev.get("from_state"),
            "to_state": ev.get("to_state"),
            "comment": "" if is_auto else ev.get("comment"),
            "auto": is_auto,
        })
        if not milestone:
            continue
        action, summary = milestone
        entries.append(build_revision_entry(
            version=doc_version,
            author=email_cache[actor],
            summary=summary,
            action=action,
            published_at=ev.get("created_at"),
        ))
    return entries


def _backfill_perstage_history(wf: dict) -> bool:
    """Write the reconstructed per-stage history (idempotent, only-if-empty)."""
    entries = _reconstruct_entries_from_events(wf)
    if not entries:
        return False
    doc_type = wf.get("doc_type")
    owner_id = wf.get("owner_user_id")
    doc_id = wf.get("doc_id")
    if doc_type in POLICY_HUB_DOC_TYPES:
        from policy_hub.routes import append_revision_entries_to_policy

        return bool(append_revision_entries_to_policy(
            owner_id, doc_id, doc_type, entries, only_if_empty=True))
    if doc_type == "runbook":
        from runbook.routes import append_revision_entries_to_runbook_result

        return bool(append_revision_entries_to_runbook_result(
            owner_id, doc_id, entries, only_if_empty=True))
    if doc_type == "report":
        from ai_reporting.routes import append_revision_entries_to_report

        return bool(append_revision_entries_to_report(
            owner_id, doc_id, entries, only_if_empty=True))
    return False


def _apply_one(wf: dict, dry_run: bool) -> str:
    """Backfill one published workflow row. Returns a status string for tallying."""
    # Lazy imports keep this script importable without dragging in every blueprint
    # at module load, and mirror how the publish hook resolves its helpers.
    from policy_hub.routes import _resolve_email, apply_publication_to_policy
    from workflow_route.state_machine import get_org_review_frequency, get_user_org_id

    doc_type = wf.get("doc_type")
    doc_id = wf.get("doc_id")
    owner_id = wf.get("owner_user_id")
    if not doc_type or not doc_id or not owner_id:
        return "skipped_incomplete"

    doc_version = wf.get("doc_version") or "1.0"
    published_at = wf.get("published_at") or wf.get("approved_at")
    approver_email = _resolve_email(wf.get("current_approver")) or ""
    org_id = wf.get("org_id") or get_user_org_id(owner_id)
    frequency = get_org_review_frequency(org_id, doc_type)

    if dry_run:
        logger.info(
            "[dry-run] would backfill %s %s (owner=%s, version=%s, published_at=%s)",
            doc_type, doc_id, owner_id, doc_version, published_at,
        )
        return "would_update"

    # Rebuild the full per-stage trail from workflow events first (only when the
    # document has no history yet), then stamp cadence + the published row below.
    _backfill_perstage_history(wf)

    if doc_type in POLICY_HUB_DOC_TYPES:
        updated = apply_publication_to_policy(
            owner_id=owner_id,
            policy_id=doc_id,
            doc_type=doc_type,
            doc_version=doc_version,
            author_email=approver_email,
            frequency=frequency,
            published_at=published_at,
        )
    elif doc_type == "runbook":
        from runbook.routes import apply_publication_to_runbook_result

        updated = apply_publication_to_runbook_result(
            owner_id=owner_id,
            result_id=doc_id,
            doc_version=doc_version,
            author_email=approver_email,
            frequency=frequency,
            published_at=published_at,
        )
    elif doc_type == "report":
        from ai_reporting.routes import apply_publication_to_report

        updated = apply_publication_to_report(
            owner_id=owner_id,
            report_id=doc_id,
            doc_version=doc_version,
            author_email=approver_email,
            frequency=frequency,
            published_at=published_at,
        )
    else:
        logger.warning("unsupported doc_type=%s for doc=%s; skipping", doc_type, doc_id)
        return "skipped_unsupported"

    return "updated" if updated else "noop_or_already_present"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report only; make no writes")
    parser.add_argument(
        "--doc-type",
        choices=sorted(ALL_DOC_TYPES),
        help="restrict to a single doc type (default: all)",
    )
    parser.add_argument("--owner", help="restrict to a single owner_user_id")
    args = parser.parse_args(argv)

    workflows = _fetch_published_workflows(args.doc_type, args.owner)
    logger.info(
        "found %d published document(s) to consider (doc_type=%s, owner=%s, dry_run=%s)",
        len(workflows), args.doc_type or "all", args.owner or "all", args.dry_run,
    )

    tally: Counter[str] = Counter()
    for wf in workflows:
        try:
            tally[_apply_one(wf, args.dry_run)] += 1
        except Exception as exc:  # best-effort: one bad doc must not stop the batch
            tally["error"] += 1
            logger.error(
                "backfill failed for doc_type=%s doc=%s: %s",
                wf.get("doc_type"), wf.get("doc_id"), exc,
            )

    logger.info("backfill complete: %s", dict(tally))
    return 0


if __name__ == "__main__":
    sys.exit(main())
