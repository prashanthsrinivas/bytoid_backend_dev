"""Per-field activity events for documents (runbook reports, policies, …).

Writes to the ``document_field_events`` table (created in
``workflow_route.state_machine.bootstrap_schema``). The UI surfaces these
alongside state-machine transitions in the unified ``/workflow/history``
feed.

Diff strategy: section-level whitelist, not recursive leaf walk. A single AI
rewrite of a large report can produce thousands of leaf deltas; instead we
compare the top-level "section" containers (title, risk_score, each section
under ``sections``, etc.) and emit one event per changed section with a
truncated snippet.
"""

import json
import uuid
from typing import Any, Iterable

import pymysql

from db.rds_db import connect_to_rds
from utils.base_logger import get_logger

logger = get_logger(__name__)

SNIPPET_MAX_CHARS = 500


def _to_text(value: Any) -> str:
    """Convert any value to a comparable/displayable text representation."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except Exception:
        return str(value)


def _truncate(text: str, limit: int = SNIPPET_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _section_paths(before: dict, after: dict) -> Iterable[str]:
    """Whitelist of top-level paths we diff for a runbook-like report.

    Anything outside this set is ignored to keep the activity feed signal-rich.
    """
    yield "title"
    yield "risk_score"
    yield "reference_sources"
    yield "properties.framework"
    yield "evidence_analysis"
    # Each top-level section under .sections (not recursive into leaves).
    section_keys = set()
    for src in (before, after):
        if isinstance(src, dict):
            sections = src.get("sections")
            if isinstance(sections, dict):
                section_keys.update(sections.keys())
            elif isinstance(sections, list):
                for s in sections:
                    if isinstance(s, dict) and s.get("id"):
                        section_keys.add(str(s["id"]))
    for k in sorted(section_keys):
        yield f"sections.{k}"


def _get_path(d: Any, path: str) -> Any:
    """Look up a dot-separated path on a nested dict, with a small special
    case for the ``sections.<id>`` form when ``sections`` is a list of
    ``{id, …}`` objects."""
    if not isinstance(d, dict):
        return None
    parts = path.split(".")
    cur: Any = d
    for i, key in enumerate(parts):
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list) and i > 0 and parts[i - 1] == "sections":
            cur = next(
                (item for item in cur if isinstance(item, dict) and str(item.get("id")) == key),
                None,
            )
        else:
            return None
        if cur is None:
            return None
    return cur


def emit_field_diff_events(
    *,
    doc_type: str,
    doc_id: str,
    previous_result_id: str | None,
    new_result_id: str | None,
    actor_user_id: str,
    before: dict | None,
    after: dict | None,
    workflow_id: str | None = None,
) -> int:
    """Emit one field-event row per changed whitelisted section.

    Returns the number of rows inserted. Never raises — diff tracking is
    nice-to-have, not a hard dependency of the save path.
    """
    if not isinstance(after, dict):
        return 0
    before = before if isinstance(before, dict) else {}

    # Resolve workflow_id from doc if not supplied.
    if not workflow_id:
        try:
            from workflow_route.state_machine import get_workflow_for_doc

            wf = get_workflow_for_doc(doc_type, doc_id, "1.0")
            workflow_id = wf.get("workflow_id") if wf else None
        except Exception:
            workflow_id = None

    rows: list[tuple] = []
    for path in _section_paths(before, after):
        old_val = _get_path(before, path)
        new_val = _get_path(after, path)
        if old_val == new_val:
            continue
        before_text = _to_text(old_val)
        after_text = _to_text(new_val)
        rows.append(
            (
                str(uuid.uuid4()),
                workflow_id,
                doc_type,
                doc_id,
                previous_result_id,
                new_result_id,
                actor_user_id,
                path[:512],
                _truncate(before_text),
                _truncate(after_text),
                len(after_text) - len(before_text),
            )
        )

    if not rows:
        return 0

    conn = connect_to_rds()
    if not conn:
        logger.warning("emit_field_diff_events: no DB connection")
        return 0
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.executemany(
                """INSERT INTO document_field_events
                   (event_id, workflow_id, doc_type, doc_id,
                    previous_result_id, new_result_id, actor_user_id,
                    field_path, before_snippet, after_snippet, delta_chars)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                rows,
            )
        conn.commit()
        return len(rows)
    except Exception as exc:
        logger.exception("emit_field_diff_events insert failed: %s", exc)
        try:
            conn.rollback()
        except Exception:
            pass
        return 0
    finally:
        conn.close()
