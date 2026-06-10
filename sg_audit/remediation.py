"""Per-finding remediation approval routing (reuses workflow_route).

A finding can be routed for approval as a ``posture_remediation`` document in the
existing, generic approval engine (``workflow_route.state_machine``). We create a
draft ``document_workflow`` row, persist the finding->workflow link in S3, and let
the user advance/assign it via the existing Reviews & Approvals UI. No AWS write
actions are performed — this tracks the approval, not the execution.
"""

from __future__ import annotations

from datetime import datetime, timezone

from utils.base_logger import get_logger
from sg_audit import metadata
from sg_audit.service import SgAuditService

logger = get_logger(__name__)
_DOC_TYPE = "posture_remediation"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def request_remediation(user_id: str, audit_id: str, finding: dict, *, service=None) -> dict:
    """Open an approval workflow for one finding. Never raises.

    Returns a status dict: created | exists | no_org | not_found | error.
    """
    service = service or SgAuditService()
    finding_id = finding.get("finding_id")
    if not finding_id:
        return {"status": "not_found", "message": "finding has no id"}

    existing = service.storage.get_remediation_links(user_id, audit_id).get(finding_id)
    if existing and existing.get("workflow_id"):
        return {"status": "exists", **existing}

    rid = finding.get("rule_id", "")
    link = {
        "finding_id": finding_id,
        "rule_id": rid,
        "rule_label": metadata.rule_label(rid),
        "summary": finding.get("finding_summary", ""),
        "recommendation": metadata.remediation_for(rid),
        "requested_by": user_id,
        "requested_at": _now(),
        "workflow_id": None,
        "state": "requested",
        "doc_type": _DOC_TYPE,
    }

    try:
        from workflow_route.state_machine import create_workflow, get_user_org_id

        org_id = get_user_org_id(user_id)
        if not org_id:
            service.storage.save_remediation_link(user_id, audit_id, finding_id, link)
            return {"status": "no_org", **link}

        wf = create_workflow(
            org_id=org_id,
            doc_type=_DOC_TYPE,
            doc_id=finding_id,
            doc_version="1",
            owner_user_id=user_id,
        )
        link["workflow_id"] = wf.get("workflow_id")
        link["org_id"] = org_id
        link["state"] = wf.get("state", "draft")
    except Exception as exc:
        logger.warning("SG-audit remediation workflow creation failed: %s", exc, exc_info=True)
        link["error"] = str(exc)
        service.storage.save_remediation_link(user_id, audit_id, finding_id, link)
        return {"status": "error", **link}

    service.storage.save_remediation_link(user_id, audit_id, finding_id, link)
    logger.info("Opened remediation workflow %s for finding %s", link["workflow_id"], finding_id)
    return {"status": "created", **link}


def list_remediations(user_id: str, audit_id: str, *, service=None) -> list[dict]:
    """All remediation links for an audit, enriched with live workflow state."""
    service = service or SgAuditService()
    links = service.storage.get_remediation_links(user_id, audit_id)
    out = []
    for link in links.values():
        item = dict(link)
        wf_id = link.get("workflow_id")
        if wf_id:
            try:
                from workflow_route.state_machine import get_workflow

                wf = get_workflow(wf_id)
                item["state"] = wf.get("state", item.get("state"))
                item["state_version"] = wf.get("state_version")
            except Exception:
                logger.debug("workflow lookup failed for %s", wf_id, exc_info=True)
        out.append(item)
    out.sort(key=lambda x: x.get("requested_at", ""), reverse=True)
    return out
