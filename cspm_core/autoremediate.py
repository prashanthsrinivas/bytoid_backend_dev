"""Approval-gated auto-remediation executor (opt-in, dry-run by default).

A real cloud write requires ALL of: the provider's auto-remediation enabled, an
approved remediation workflow for the finding, a supported fixer, and an explicit
``dry_run=False``. Fixers come from ``provider.fixers`` (signature
``fix(creds, finding, dry_run) -> (action_text, performed)``).
"""

from __future__ import annotations

from datetime import datetime, timezone

from utils.base_logger import get_logger

logger = get_logger(__name__)
_APPROVED_STATES = {"approval", "published"}


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_approved(provider, user_id, audit_id, finding_id, service) -> bool:
    link = service.storage.get_remediation_links(user_id, audit_id).get(finding_id)
    if not link or not link.get("workflow_id"):
        return False
    try:
        from workflow_route.state_machine import get_workflow
        return get_workflow(link["workflow_id"]).get("state") in _APPROVED_STATES
    except Exception:
        logger.debug("approval-state lookup failed", exc_info=True)
        return False


def execute_remediation(provider, user_id, audit_id, finding, *, dry_run=True, service=None) -> dict:
    if service is None:
        from cspm_core.service import CspmService
        service = CspmService(provider)
    rule_id = finding.get("rule_id", "")
    finding_id = finding.get("finding_id", "")

    enabled = bool(provider.auto_remediate_enabled and provider.auto_remediate_enabled())
    if not enabled:
        return {"status": "disabled", "reason": "auto-remediation is turned off"}
    fixer = provider.fixers.get(rule_id)
    if not fixer:
        return {"status": "unsupported", "rule_id": rule_id}
    if not _is_approved(provider, user_id, audit_id, finding_id, service):
        return {"status": "not_approved", "reason": "no approved remediation workflow for this finding"}

    try:
        if dry_run:
            action, _ = fixer(None, finding, True)
            return {"status": "planned", "dry_run": True, "action": action, "finding_id": finding_id}
        creds = provider.resolve_credentials(user_id)
        if not creds:
            return {"status": "error", "message": "could not obtain write credentials"}
        action, performed = fixer(creds, finding, False)
    except Exception as exc:
        logger.warning("%s auto-remediation failed for %s: %s", provider.key, finding_id, exc, exc_info=True)
        return {"status": "error", "message": str(exc), "finding_id": finding_id}

    try:
        links = service.storage.get_remediation_links(user_id, audit_id)
        link = links.get(finding_id, {"finding_id": finding_id})
        link.update({"executed_at": _now(), "executed_action": action, "executed": performed})
        service.storage.save_remediation_link(user_id, audit_id, finding_id, link)
    except Exception:
        logger.debug("failed to record execution", exc_info=True)
    logger.info("%s auto-remediated %s: %s", provider.key, finding_id, action)
    return {"status": "executed", "dry_run": False, "action": action, "finding_id": finding_id}
