from db.rds_db import connect_to_rds


"""
MySQL schema obfuscator (UUID4 suffixes) — rename names only, do NOT change datatypes.

- Uses truncated uuid4 hex suffixes (e.g., tbl_4a7b1f8c).
- Reads column metadata for TARGET_SCHEMA and builds mapping.
- Writes timestamped mapping CSV required for reversibility.
- Previews RENAME TABLE and ALTER TABLE ... RENAME COLUMN statements.
- Can execute DDL when APPLY_RENAMES = True.

Requirements:
- Your existing connect_to_rds() must be available and return a DB-API connection.
- MySQL 8.0+ is required for ALTER TABLE ... RENAME COLUMN.
- Test on a copy first. Backup before APPLY_RENAMES=True.
"""

import uuid
import csv
import datetime
from collections import defaultdict

# ------------------ CONFIG ------------------
APPLY_RENAMES = False  # False = preview only; True = execute DDL (use cautiously)
SUFFIX_LENGTH = (
    8  # number of hex chars taken from uuid4().hex (increase for more entropy)
)
TABLE_PREFIX = "tbl"
COLUMN_PREFIX = "col"
TARGET_SCHEMA = "bytoid_support_agent"
OUTPUT_MAPPING_CSV = f"table_column_mapping_{TARGET_SCHEMA}_{datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.csv"
# ------------------------------------------------


# ----------------- Helper functions -----------------


def uuid_suffix(length: int = SUFFIX_LENGTH) -> str:
    """Return the first `length` chars of a uuid4 hex (no dashes)."""
    return uuid.uuid4().hex[:length]


def random_name(prefix: str, used: set, length: int = SUFFIX_LENGTH) -> str:
    """Generate a name using uuid suffix with collision detection."""
    for _ in range(200):
        cand = f"{prefix}_{uuid_suffix(length)}"
        if cand not in used:
            used.add(cand)
            return cand
    raise RuntimeError(
        "Unable to generate unique obfuscated name - increase SUFFIX_LENGTH."
    )


def main():
    # obtain your connection using your function
    conn = connect_to_rds()
    cursor = conn.cursor()

    # Query columns for the target schema (only base tables)
    schema_query = """
    SELECT
      c.TABLE_SCHEMA,
      c.TABLE_NAME,
      c.COLUMN_NAME,
      c.DATA_TYPE,
      c.CHARACTER_MAXIMUM_LENGTH,
      c.NUMERIC_PRECISION,
      c.NUMERIC_SCALE,
      c.IS_NULLABLE,
      c.ORDINAL_POSITION
    FROM INFORMATION_SCHEMA.COLUMNS c
    JOIN INFORMATION_SCHEMA.TABLES t
      ON c.TABLE_SCHEMA = t.TABLE_SCHEMA
     AND c.TABLE_NAME   = t.TABLE_NAME
    WHERE t.TABLE_SCHEMA = %s
      AND t.TABLE_TYPE   = 'BASE TABLE'
    ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION;
    """

    cursor.execute(schema_query, (TARGET_SCHEMA,))
    rows = cursor.fetchall()
    cols_desc = [d[0] for d in cursor.description]
    rows_dicts = [dict(zip(cols_desc, r)) for r in rows]

    if not rows_dicts:
        # print(f"No tables/columns found in schema '{TARGET_SCHEMA}'. Exiting.")
        cursor.close()
        conn.close()
        return

    # group columns by (schema, table)
    cols_by_table = defaultdict(list)
    tables_seen = []
    for r in rows_dicts:
        key = (r["TABLE_SCHEMA"], r["TABLE_NAME"])
        cols_by_table[key].append(r)
        if key not in tables_seen:
            tables_seen.append(key)

    # print(f"Discovered {len(tables_seen)} tables in schema '{TARGET_SCHEMA}'.")

    # build mapping with random uuid-based names, ensure uniqueness across tables+cols
    used_names = set()
    mapping = (
        {}
    )  # (schema, table) -> {"new_table":name, "columns": {oldcol: newcol, ...}}
    for schema, tbl in tables_seen:
        new_table = random_name(TABLE_PREFIX, used_names)
        col_map = {}
        for col_row in cols_by_table[(schema, tbl)]:
            orig_col = col_row["COLUMN_NAME"]
            new_col = random_name(COLUMN_PREFIX, used_names)
            col_map[orig_col] = new_col
        mapping[(schema, tbl)] = {"new_table": new_table, "columns": col_map}

    # write mapping CSV
    with open(OUTPUT_MAPPING_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "schema",
                "original_table",
                "obfuscated_table",
                "original_column",
                "obfuscated_column",
            ]
        )
        for (schema, tbl), info in mapping.items():
            for orig_col, obf_col in info["columns"].items():
                w.writerow([schema, tbl, info["new_table"], orig_col, obf_col])
    # print(f"Mapping written to: {OUTPUT_MAPPING_CSV}  (KEEP THIS FILE SAFE!)")

    # build DDL commands: rename tables first, then rename columns using RENAME COLUMN (MySQL 8+)
    commands = []
    # 1) table renames
    for (schema, tbl), info in mapping.items():
        sql = f"RENAME TABLE `{schema}`.`{tbl}` TO `{schema}`.`{info['new_table']}`;"
        commands.append(
            ("TABLE_RENAME", f"{schema}.{tbl}", f"{schema}.{info['new_table']}", sql)
        )

    # 2) column renames (use ALTER TABLE `schema`.`<new_table>` RENAME COLUMN `old` TO `new`;)
    for (schema, tbl), info in mapping.items():
        new_tbl = info["new_table"]
        for orig_col, new_col in info["columns"].items():
            sql = f"ALTER TABLE `{schema}`.`{new_tbl}` RENAME COLUMN `{orig_col}` TO `{new_col}`;"
            commands.append(
                (
                    "COLUMN_RENAME",
                    f"{schema}.{tbl}.{orig_col}",
                    f"{schema}.{new_tbl}.{new_col}",
                    sql,
                )
            )

    # preview
    # print("\n=== PREVIEW DDL COMMANDS ===")
    # for kind, a, b, sql in commands:
    #     if kind == "TABLE_RENAME":
    #         #print(f"-- Table rename: {a} -> {b}")
    #     else:
    #         #print(f"-- Column rename: {a} -> {b}")
    #     #print(sql)
    # print("=== END PREVIEW ===\n")

    if not APPLY_RENAMES:
        # print("APPLY_RENAMES = False — no changes executed. Set APPLY_RENAMES = True to apply the above DDL.")
        cursor.close()
        conn.close()
        return

    # execute commands in order
    # print("Applying DDL changes...")
    try:
        for kind, a, b, sql in commands:
            # print("Executing:", sql)
            cursor.execute(sql)
        conn.commit()
    # print("All commands executed and committed.")
    except Exception as e:
        # rollback on error
        conn.rollback()
        # print("Error during execution. Rolled back. Error:", e)
        raise

    cursor.close()
    conn.close()


# print("Done.")

if __name__ == "__main__":
    main()
