"""Pure, DB-free strategy health-rollup and drill-down helpers.

These functions are deliberately import-light (no ``db`` / ``boto3`` / AWS) so
they are trivially unit-testable and so importing this module never triggers
the RDS connection pool. All DB access lives in ``strategy/helper.py``.

Status semantics (verified against ``tab_tracker/refs_sync.py``):
``statement_tracker_refs.status`` mirrors the tracker cell's assessment status.
Only ``passed`` / ``failed`` count toward the pass-rate. Everything else
(``not_assessed``, ``superseded``, the legacy ``'active'`` default, or anything
unknown) is treated as "not assessed" and excluded from the denominator.
"""


def normalize_status(status) -> str:
    """Collapse any raw ref status into one of passed | failed | not_assessed."""
    s = (status or "").strip().lower()
    if s == "passed":
        return "passed"
    if s == "failed":
        return "failed"
    return "not_assessed"


def classify_health(failed: int, score):
    """Map (#failures, pass-rate) to a coarse health label for the exec UI."""
    if score is None:
        return "not_assessed"
    if failed > 0 and score < 0.5:
        return "failing"
    if failed > 0:
        return "at_risk"
    return "healthy"


def _summary(passed: int, failed: int, not_assessed: int) -> dict:
    assessed = passed + failed
    score = round(passed / assessed, 4) if assessed else None
    return {
        "passed": passed,
        "failed": failed,
        "not_assessed": not_assessed,
        "total": passed + failed + not_assessed,
        "score": score,
        "health": classify_health(failed, score),
    }


def rollup_status(refs) -> dict:
    """Aggregate ref dicts (each with a ``status``) into a health summary.

    ``refs`` items may be dicts (``{"status": ...}``) or raw status strings.
    Returns ``{passed, failed, not_assessed, total, score, health}`` where
    ``score`` is ``passed/(passed+failed)`` (None when nothing is assessed).
    """
    passed = failed = not_assessed = 0
    for r in refs or []:
        raw = r.get("status") if isinstance(r, dict) else r
        st = normalize_status(raw)
        if st == "passed":
            passed += 1
        elif st == "failed":
            failed += 1
        else:
            not_assessed += 1
    return _summary(passed, failed, not_assessed)


def aggregate_rollups(rollups) -> dict:
    """Combine child summaries (from :func:`rollup_status`) into one summary.

    Used to roll Project → Program → Objective. An objective aggregates BOTH
    its programs and any projects attached directly to it.
    """
    passed = failed = not_assessed = 0
    for r in rollups or []:
        if not r:
            continue
        passed += int(r.get("passed", 0) or 0)
        failed += int(r.get("failed", 0) or 0)
        not_assessed += int(r.get("not_assessed", 0) or 0)
    return _summary(passed, failed, not_assessed)


def build_drilldown_paths(project: dict, doc_index: dict, refs) -> list:
    """Build ordered root-cause paths for the *failing* refs of a project.

    ``project``   : ``{id, name, objective_id, objective_title, program_id, program_name}``
    ``doc_index`` : ``{policy_id: {title, doc_ref, doc_type}}`` (best-effort; may be empty —
                    the frontend enriches titles/doc_refs it already has cached).
    ``refs``      : list of ``{policy_id, doc_type, tracker_id, tracker_abbrev,
                    row_id, column_id, statement_id, status}``.

    Returns one path per failing ref, ordered objective → program → project →
    doc → tracker, ready for the drill-down graph.
    """
    doc_index = doc_index or {}
    paths = []
    for r in refs or []:
        if normalize_status(r.get("status")) != "failed":
            continue
        pid = r.get("policy_id")
        doc = doc_index.get(pid, {})
        paths.append(
            {
                "objective_id": project.get("objective_id"),
                "objective_title": project.get("objective_title"),
                "program_id": project.get("program_id"),
                "program_name": project.get("program_name"),
                "project_id": project.get("id"),
                "project_name": project.get("name"),
                "policy_id": pid,
                "doc_type": r.get("doc_type") or doc.get("doc_type"),
                "doc_title": doc.get("title"),
                "doc_ref": doc.get("doc_ref"),
                "tracker_id": r.get("tracker_id"),
                "tracker_abbrev": r.get("tracker_abbrev"),
                "row_id": r.get("row_id"),
                "column_id": r.get("column_id"),
                "statement_id": r.get("statement_id"),
                "status": "failed",
            }
        )
    return paths
