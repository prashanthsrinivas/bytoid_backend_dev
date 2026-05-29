"""Backfill Review & Revision History for already-published documents.

The publish hook only records a revision-history row at the moment a workflow
transitions to ``published``. Documents published *before* that hook existed have
an empty history and show "No history recorded" in the UI. This script walks
every published ``document_workflow`` row and reconstructs the missing entry from
that row (its ``published_at``, ``doc_version`` and approver), then stamps the
review-cycle metadata — exactly what the live publish hook does, but using the
original publish timestamp instead of "now".

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
