#!/usr/bin/env python3
"""Create the Strategy module database tables — idempotent and strategy-only.

Why: the Strategy tables are defined inside the big create_db.py setup script,
which evidently was never run on some environments (e.g. the demo DB) — so
`POST /strategy/objectives` 500s on a missing `strategic_objectives` table.
This creates ONLY the 6 strategy tables (CREATE TABLE IF NOT EXISTS), so it is
safe to re-run and won't touch anything else.

Run it on a host that can reach RDS (the EC2 backend box):

    cd /home/ec2-user/bytoid_python
    python scripts/create_strategy_tables.py

It prints which tables exist afterwards. (The same DDL also runs lazily at
runtime via strategy/helper.py once that change is deployed — this script just
lets you create them explicitly without waiting for a request.)
"""

import sys

from db.rds_db import connect_to_rds

# Mirrors create_db.py / strategy/helper.py. Order matters only for readability;
# there are no hard FK constraints between them.
STRATEGY_TABLES = [
    (
        "strategic_objectives",
        """
        CREATE TABLE IF NOT EXISTS strategic_objectives (
            id VARCHAR(36) PRIMARY KEY,
            owner_user_id VARCHAR(36) NOT NULL,
            org_id VARCHAR(36),
            created_by VARCHAR(36),
            title VARCHAR(255) NOT NULL,
            description TEXT,
            status VARCHAR(50) DEFAULT 'draft',
            start_date DATE,
            target_date DATE,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            KEY idx_objective_owner (owner_user_id),
            KEY idx_objective_org (org_id, status)
        )
        """,
    ),
    (
        "programs",
        """
        CREATE TABLE IF NOT EXISTS programs (
            id VARCHAR(36) PRIMARY KEY,
            objective_id VARCHAR(36) NOT NULL,
            owner_user_id VARCHAR(36) NOT NULL,
            org_id VARCHAR(36),
            created_by VARCHAR(36),
            name VARCHAR(255) NOT NULL,
            description TEXT,
            status VARCHAR(50) DEFAULT 'draft',
            start_date DATE,
            target_date DATE,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            KEY idx_program_objective (objective_id),
            KEY idx_program_owner (owner_user_id),
            KEY idx_program_org (org_id, status)
        )
        """,
    ),
    (
        "projects",
        """
        CREATE TABLE IF NOT EXISTS projects (
            id VARCHAR(36) PRIMARY KEY,
            objective_id VARCHAR(36) NOT NULL,
            program_id VARCHAR(36),
            owner_user_id VARCHAR(36) NOT NULL,
            org_id VARCHAR(36),
            created_by VARCHAR(36),
            name VARCHAR(255) NOT NULL,
            description TEXT,
            status VARCHAR(50) DEFAULT 'draft',
            start_date DATE,
            target_date DATE,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            KEY idx_project_objective (objective_id),
            KEY idx_project_program (program_id),
            KEY idx_project_owner (owner_user_id),
            KEY idx_project_org (org_id, status)
        )
        """,
    ),
    (
        "project_doc_links",
        """
        CREATE TABLE IF NOT EXISTS project_doc_links (
            id VARCHAR(36) PRIMARY KEY,
            project_id VARCHAR(36) NOT NULL,
            policy_id VARCHAR(64) NOT NULL,
            doc_type ENUM('policy', 'procedure', 'standard') NOT NULL DEFAULT 'policy',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_project_doc (project_id, policy_id),
            KEY idx_doc_project (project_id),
            KEY idx_doc_policy (policy_id)
        )
        """,
    ),
    (
        "project_tracker_links",
        """
        CREATE TABLE IF NOT EXISTS project_tracker_links (
            id VARCHAR(36) PRIMARY KEY,
            project_id VARCHAR(36) NOT NULL,
            tracker_id VARCHAR(64) NOT NULL,
            pinned TINYINT(1) DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_project_tracker (project_id, tracker_id),
            KEY idx_tracker_project (project_id),
            KEY idx_tracker_tracker (tracker_id)
        )
        """,
    ),
    (
        "strategy_milestones",
        """
        CREATE TABLE IF NOT EXISTS strategy_milestones (
            id VARCHAR(36) PRIMARY KEY,
            parent_type ENUM('objective', 'program', 'project') NOT NULL,
            parent_id VARCHAR(36) NOT NULL,
            title VARCHAR(255) NOT NULL,
            due_date DATE,
            status VARCHAR(50) DEFAULT 'planned',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            KEY idx_milestone_parent (parent_type, parent_id)
        )
        """,
    ),
]


def main() -> int:
    conn = connect_to_rds()
    if conn is None:
        print("ERROR: could not get an RDS connection.", file=sys.stderr)
        return 1
    try:
        with conn.cursor() as cur:
            for name, ddl in STRATEGY_TABLES:
                cur.execute(ddl)
                print(f"  ✓ ensured table: {name}")
        conn.commit()

        # Verify
        present = []
        with conn.cursor() as cur:
            for name, _ in STRATEGY_TABLES:
                cur.execute("SHOW TABLES LIKE %s", (name,))
                if cur.fetchone():
                    present.append(name)
        missing = [n for n, _ in STRATEGY_TABLES if n not in present]
        print(f"\nStrategy tables present ({len(present)}/{len(STRATEGY_TABLES)}): {', '.join(present)}")
        if missing:
            print(f"STILL MISSING: {', '.join(missing)}", file=sys.stderr)
            return 2
        print("All strategy tables are in place.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
