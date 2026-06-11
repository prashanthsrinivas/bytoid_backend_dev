"""Push Cloud Security Posture evidence into a runbook's Responses & Evidence.

Mirrors the VRA injection path exactly (``vra/workflow_inject`` +
``vra/runbook_bridge``): resolve the runbook's linked playbook, load it, append
the evidence — both as a "Cloud Security Posture" question block (the proven,
rendered path) and to the playbook's ``evidences_ques``/``evidence_overview``
fields — save, then enqueue the existing report-regeneration Celery task. The
write is additive and best-effort; regeneration is on-demand (it invokes Bedrock,
so it stays a deliberate user action, matching VRA's cost stance).
"""

from __future__ import annotations

import uuid

from db.lance_db_service import LanceDBServer
from utils.base_logger import get_logger

logger = get_logger(__name__)
dbserver = LanceDBServer()


def _runbook_name(row: dict) -> str:
    return row.get("runbook_name") or row.get("name") or row.get("title") or row.get("runbook_id", "")


async def list_runbooks(user_id: str) -> list[dict]:
    """Runbooks the evidence can be pushed into (must have a linked playbook)."""
    try:
        rows = await dbserver.get_all_runbooks(user_id)
    except Exception:
        logger.warning("list_runbooks failed", exc_info=True)
        return []
    out = []
    for r in rows or []:
        if r.get("playbook_id"):
            out.append({"runbook_id": r.get("runbook_id"), "name": _runbook_name(r),
                        "playbook_id": r.get("playbook_id")})
    out.sort(key=lambda x: (x["name"] or "").lower())
    return out


def _load_playbook(user_id: str, playbook_id: str):
    """Load + decrypt a playbook JSON by id (mirror vra/workflow_inject._load_workflow)."""
    from playbook.helperzz import _PLAYBOOK_CONTENT_FIELDS, _dec_pb, base_name
    from utils.s3_utils import read_json_from_s3

    filename = playbook_id if str(playbook_id).lower().endswith(".json") else f"{playbook_id}.json"
    loc = f"{user_id}/workflow/{base_name(filename=filename)}/{filename}"
    pb = read_json_from_s3(loc)
    if not pb:
        return None, filename
    for field in _PLAYBOOK_CONTENT_FIELDS:
        if field in pb:
            pb[field] = _dec_pb(user_id, pb[field])
    return pb, filename


def _evidence_questions(records: list[dict], label: str) -> list[dict]:
    """One assigned-question item per evidence record (the rendered path)."""
    section = f"Cloud Security Posture — {label}"
    items = []
    for r in records:
        items.append({
            "id": uuid.uuid4().hex,
            "question": r.get("finding_summary", ""),
            "options": {},
            "answer": None,
            "comment": None,
            "section": section,
            "evidence_required": [],
            "posture_evidence": True,
            "severity": r.get("severity", "info"),
            "source_url": r.get("source_url", ""),
        })
    return items


async def push_evidence_to_runbook(user_id: str, runbook_id: str, records: list[dict], label: str) -> dict:
    """Inject evidence records into a runbook + enqueue regeneration. Never raises."""
    if not records:
        return {"status": "empty", "message": "no evidence to push"}
    try:
        rows = await dbserver.get_runbook_by_id(user_id, runbook_id)
        if not rows:
            return {"status": "not_found", "message": "runbook not found"}
        runbook = rows[0]
        playbook_id = runbook.get("playbook_id")
        if not playbook_id:
            return {"status": "not_linked", "message": "runbook has no linked playbook"}

        pb, filename = _load_playbook(user_id, playbook_id)
        if pb is None:
            return {"status": "not_found", "message": "playbook not found"}

        # Additive: question block (rendered) + raw evidence fields the runbook reads.
        assigned = pb.get("assigned_questions") or []
        pb["assigned_questions"] = assigned + _evidence_questions(records, label)
        ev = pb.get("evidences_ques") or []
        pb["evidences_ques"] = ev + records
        overview = pb.get("evidence_overview") or {}
        overview.setdefault("posture", {})
        overview["posture"][label] = {"count": len(records), "source": "sg_audit"}
        pb["evidence_overview"] = overview

        from playbook.helperzz import save_playbook_to_s3

        save_playbook_to_s3(pb, user_id, "cloud security posture evidence injected", filename)

        from utils.celery_base import create_playbook_runbook_task

        create_playbook_runbook_task.delay(user_id, playbook_id, runbook_id)
    except Exception as exc:
        logger.warning("push_evidence_to_runbook failed: %s", exc, exc_info=True)
        return {"status": "error", "message": str(exc)}

    logger.info("Pushed %d posture evidence records to runbook %s (playbook %s)",
                len(records), runbook_id, playbook_id)
    return {"status": "queued", "runbook_id": runbook_id, "playbook_id": playbook_id, "injected": len(records)}
