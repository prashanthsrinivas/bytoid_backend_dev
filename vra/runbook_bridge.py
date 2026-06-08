"""Link a VRA to a runbook and request report (re)generation.

Additive by construction: regeneration is delegated to the EXISTING
``create_playbook_runbook_task`` celery task (which already holds its own
per-playbook lock and runs ``trigger_runbook_from_playbook``). We never touch
the runbook engine or its storage; we only associate ids and enqueue the
existing task.

Regeneration is **on-demand** (an endpoint), not auto-fired from the OSINT
callback — re-running the report invokes Bedrock and therefore costs credits, so
it stays a deliberate, user-initiated action (cost control).
"""

from __future__ import annotations

from utils.base_logger import get_logger
from vra.service import VraService

logger = get_logger(__name__)


def link_runbook(
    user_id: str,
    assessment_id: str,
    *,
    runbook_id: str | None = None,
    playbook_id: str | None = None,
    service: VraService | None = None,
) -> dict | None:
    """Associate a runbook/playbook with the assessment. Returns the record."""
    service = service or VraService()
    record = service.get_assessment(user_id, assessment_id)
    if not record:
        return None
    if runbook_id is not None:
        record["runbook_id"] = runbook_id
    if playbook_id is not None:
        record["playbook_id"] = playbook_id
    service.storage.save_assessment(user_id, record)
    return record


def request_regeneration(
    user_id: str,
    assessment_id: str,
    *,
    service: VraService | None = None,
    task=None,
) -> dict:
    """Enqueue report regeneration for a linked VRA via the existing task.

    Best-effort and never raises. Returns a status dict:
      not_found / not_linked / queued / error.
    """
    service = service or VraService()
    record = service.get_assessment(user_id, assessment_id)
    if not record:
        return {"status": "not_found"}

    runbook_id = record.get("runbook_id")
    playbook_id = record.get("playbook_id")
    if not (runbook_id and playbook_id):
        return {"status": "not_linked", "reason": "assessment has no runbook+playbook"}

    try:
        if task is None:
            from utils.celery_base import create_playbook_runbook_task as task
        task.delay(user_id, playbook_id, runbook_id)
    except Exception as exc:
        logger.warning("VRA regeneration enqueue failed: %s", exc, exc_info=True)
        return {"status": "error", "message": str(exc)}

    logger.info("Queued VRA report regeneration for %s", assessment_id)
    return {"status": "queued", "runbook_id": runbook_id, "playbook_id": playbook_id}
