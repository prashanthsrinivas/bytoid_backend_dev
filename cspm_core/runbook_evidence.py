"""Push CSPM evidence into a runbook's Responses & Evidence (provider-agnostic).

Mirrors the VRA injection path: resolve the runbook's linked playbook, append the
evidence (as a question block + the playbook's ``evidences_ques``/
``evidence_overview`` fields), save, then enqueue the existing report-regeneration
task. Additive + best-effort; regeneration is on-demand (Bedrock cost).
"""

from __future__ import annotations

import uuid

from db.lance_db_service import LanceDBServer
from utils.base_logger import get_logger

logger = get_logger(__name__)
dbserver = LanceDBServer()


def _runbook_name(row):
    return row.get("runbook_name") or row.get("name") or row.get("title") or row.get("runbook_id", "")


async def list_runbooks(user_id: str) -> list:
    try:
        rows = await dbserver.get_all_runbooks(user_id)
    except Exception:
        logger.warning("list_runbooks failed", exc_info=True)
        return []
    out = [{"runbook_id": r.get("runbook_id"), "name": _runbook_name(r), "playbook_id": r.get("playbook_id")}
           for r in (rows or []) if r.get("playbook_id")]
    out.sort(key=lambda x: (x["name"] or "").lower())
    return out


def _load_playbook(user_id, playbook_id):
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


def _evidence_questions(records, label):
    section = f"Cloud Security Posture — {label}"
    return [{"id": uuid.uuid4().hex, "question": r.get("finding_summary", ""), "options": {},
             "answer": None, "comment": None, "section": section, "evidence_required": [],
             "posture_evidence": True, "severity": r.get("severity", "info"),
             "source_url": r.get("source_url", "")} for r in records]


async def push_evidence_to_runbook(user_id, runbook_id, records, label, source="cspm") -> dict:
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

        pb["assigned_questions"] = (pb.get("assigned_questions") or []) + _evidence_questions(records, label)
        pb["evidences_ques"] = (pb.get("evidences_ques") or []) + records
        overview = pb.get("evidence_overview") or {}
        overview.setdefault("posture", {})
        overview["posture"][label] = {"count": len(records), "source": source}
        pb["evidence_overview"] = overview

        from playbook.helperzz import save_playbook_to_s3
        save_playbook_to_s3(pb, user_id, "cloud security posture evidence injected", filename)

        from utils.celery_base import create_playbook_runbook_task
        create_playbook_runbook_task.delay(user_id, playbook_id, runbook_id)
    except Exception as exc:
        logger.warning("push_evidence_to_runbook failed: %s", exc, exc_info=True)
        return {"status": "error", "message": str(exc)}
    logger.info("Pushed %d posture evidence records to runbook %s (playbook %s)", len(records), runbook_id, playbook_id)
    return {"status": "queued", "runbook_id": runbook_id, "playbook_id": playbook_id, "injected": len(records)}
