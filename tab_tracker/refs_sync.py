"""Derive the statement↔tracker reverse-lookup refs from a tracker's data and
sync them into RDS.

``extract_policy_cell_refs`` is pure (no DB) so it is unit-testable; it is the
canonical interpretation of "which statements does each policy cell map to".
Both ``/tracker/sync-policy-status`` and the nightly reconcile cron use it so
RDS and the S3 tracker blob agree on one definition.
"""

from utils.base_logger import get_logger

logger = get_logger(__name__)


def extract_policy_cell_refs(tracker_data: dict) -> list[dict]:
    """Return one entry per policy cell in *tracker_data*.

    Each entry: ``{policy_id, doc_type, row_id, column_id, statements}`` where
    ``statements`` is ``[{statement_id, status}, ...]``. Cells with no mapped
    statements still appear (with an empty list) so a sync can clear stale refs.
    """
    schema_cols = (tracker_data or {}).get("schema", {}).get("columns", []) or []
    policy_cols = [c for c in schema_cols if c.get("source_column") == "policies"]
    rows = (tracker_data or {}).get("rows", []) or []

    out: list[dict] = []
    for col in policy_cols:
        col_id = col.get("id")
        policy_id = col.get("policy_id")
        doc_type = col.get("doc_type", "policy")
        if not col_id or not policy_id:
            continue
        for row in rows:
            entries = (row.get("values", {}) or {}).get(col_id) or []
            statements = [
                {"statement_id": e.get("statement_id"), "status": e.get("status", "not_assessed")}
                for e in entries
                if isinstance(e, dict) and e.get("statement_id")
            ]
            out.append({
                "policy_id": policy_id,
                "doc_type": doc_type,
                "row_id": row.get("row_id"),
                "column_id": col_id,
                "statements": statements,
            })
    return out


def sync_tracker_refs(tracker_id: str, tracker_abbrev: str | None, tracker_data: dict) -> int:
    """Make RDS refs match *tracker_data* exactly for this tracker.

    Returns the number of cells synced. Drops any RDS refs for policies no
    longer linked, then replaces each live cell's refs. Best-effort per cell —
    a single cell failure is logged and does not abort the rest.
    """
    from services.statement_tracker_refs import (
        delete_refs_for_tracker,
        replace_cell_refs,
    )

    refs = extract_policy_cell_refs(tracker_data)

    # Clear everything for this tracker first so policies/cells that were
    # removed don't leave orphans, then re-insert the current set.
    try:
        delete_refs_for_tracker(tracker_id)
    except Exception as exc:
        logger.warning("sync_tracker_refs: clear failed for tracker=%s: %s", tracker_id, exc)

    synced = 0
    for cell in refs:
        try:
            replace_cell_refs(
                tracker_id,
                cell["row_id"],
                cell["column_id"],
                cell["policy_id"],
                cell["doc_type"],
                tracker_abbrev,
                cell["statements"],
            )
            synced += 1
        except Exception as exc:
            logger.warning(
                "sync_tracker_refs: cell sync failed tracker=%s row=%s col=%s: %s",
                tracker_id, cell.get("row_id"), cell.get("column_id"), exc,
            )
    return synced
