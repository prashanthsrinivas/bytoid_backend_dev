"""
One-shot script: force-reset the governance_review runbook workflow back to quality_review.
Run this on the EC2 instance (where it has Secrets Manager access):

    python reset_to_quality_review.py

It will print all matching workflows and ask which one to reset before making any change.
"""

import sys
import uuid
import argparse
from datetime import datetime, timezone

import pymysql
import pymysql.cursors

# Must run from the project root so the app imports resolve.
sys.path.insert(0, ".")

from db.rds_db import connect_to_rds


def main(apply_mode: bool = False):
    conn = connect_to_rds()
    if not conn:
        print("ERROR: Could not connect to RDS.")
        sys.exit(1)

    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                """
                SELECT workflow_id, doc_type, doc_id, doc_version,
                       state, state_version, owner_user_id,
                       current_quality_reviewer, current_governance_reviewer,
                       created_at
                FROM document_workflow
                WHERE state = 'governance_review'
                ORDER BY created_at DESC
                """
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        print("No workflows currently in governance_review state.")
        return

    print(f"\nFound {len(rows)} workflow(s) in governance_review:\n")
    for i, r in enumerate(rows):
        print(
            f"  [{i}] workflow_id={r['workflow_id']}"
            f"\n      doc_type={r['doc_type']}  doc_id={r['doc_id']}"
            f"\n      state_version={r['state_version']}"
            f"\n      owner={r['owner_user_id']}"
            f"\n      quality_reviewer={r['current_quality_reviewer']}"
            f"\n      governance_reviewer={r['current_governance_reviewer']}"
            f"\n      created_at={r['created_at']}\n"
        )

    choice = input("Enter index to reset (or 'q' to quit): ").strip()
    if choice.lower() == "q":
        print("Aborted.")
        return

    try:
        idx = int(choice)
        target = rows[idx]
    except (ValueError, IndexError):
        print("Invalid choice.")
        sys.exit(1)

    actor = input("Enter your user_id (for the audit log): ").strip()
    if not actor:
        print("user_id required.")
        sys.exit(1)

    confirm = input(
        f"\nAbout to reset workflow {target['workflow_id']}\n"
        f"  {target['state']} (v{target['state_version']}) → quality_review\n"
        "Type 'yes' to confirm: "
    ).strip()
    if confirm.lower() != "yes":
        print("Aborted.")
        return
    # Dry-run mode by default: require explicit --apply to make DB changes.
    if not apply_mode:
        print(
            f"\nDry-run: would reset workflow {target['workflow_id']}"
            f" from {target['state']} (v{target['state_version']}) → quality_review."
        )
        print("Run this script with --apply to perform the change.")
        return

    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM document_workflow WHERE workflow_id=%s FOR UPDATE",
                (target["workflow_id"],),
            )
            row = cur.fetchone()
            if not row or row["state"] != "governance_review":
                print("Row changed since we read it — aborting.")
                conn.rollback()
                return

            new_version = row["state_version"] + 1
            cur.execute(
                """
                UPDATE document_workflow
                SET state = 'quality_review',
                    state_version = %s
                WHERE workflow_id = %s
                """,
                (new_version, target["workflow_id"]),
            )

            event_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO document_workflow_events
                  (event_id, workflow_id, from_state, to_state, actor_user_id, comment, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    event_id,
                    target["workflow_id"],
                    "governance_review",
                    "quality_review",
                    actor,
                    "Admin reset: restarting from quality review",
                    datetime.now(timezone.utc),
                ),
            )
        conn.commit()
        print(
            f"\nDone. Workflow {target['workflow_id']} is now quality_review (v{new_version})."
        )
    except Exception as e:
        conn.rollback()
        print(f"ERROR: {e}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Reset workflows from governance_review to quality_review."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually perform DB updates. Without this flag the script runs in dry-run mode.",
    )
    args = parser.parse_args()
    main(apply_mode=args.apply)
