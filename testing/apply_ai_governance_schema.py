"""One-shot bootstrap: create the ai_guardrail_rules and
ai_guardrail_violations tables on the active DB (dev or prod, depending on
the DEV env var).  Idempotent — both statements use CREATE TABLE IF NOT EXISTS.

Run from the project root:
    DEV=true python3 testing/apply_ai_governance_schema.py
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from db.rds_db import connect_to_rds  # noqa: E402

SQL_PATH = os.path.join(HERE, "ai_governance_rules.sql")


def main() -> int:
    with open(SQL_PATH) as f:
        sql = f.read()
    sql = re.sub(r"--[^\n]*", "", sql)
    statements = [s.strip() for s in sql.split(";") if s.strip()]

    conn = connect_to_rds()
    if conn is None:
        print("ERROR: connect_to_rds() returned None — pool is down or creds missing.")
        return 1
    try:
        with conn.cursor() as cur:
            for s in statements:
                first = s.split("\n", 1)[0][:80]
                print(f"EXEC: {first} ...")
                cur.execute(s)
            cur.execute("SHOW TABLES LIKE 'ai_guardrail%'")
            print("tables now:", cur.fetchall())
        conn.commit()
    finally:
        conn.close()
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
