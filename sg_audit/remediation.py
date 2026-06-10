"""Per-finding remediation approval routing (reuses workflow_route).

A finding can be routed for approval as a ``posture_remediation`` document in the
existing, generic approval engine (``workflow_route.state_machine``). We create
the ``document_workflow`` assigned to the org admin/owner and SUBMIT it into the
review pipeline, so it lands in the admin's Reviews & Approvals inbox (rather than
sitting in an unassigned draft). The finding->workflow link is persisted in S3. No
AWS write actions are performed — this tracks the approval, not the execution.
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


def _resolve_org_admin(user_id: str) -> tuple[str | None, str | None]:
    """Resolve the org admin/owner (user_id, email) to route an approval to.

    org_id is ``launch:{admin_user_id}`` for an admin-root org, or a company name.
    Falls back to the requester when they are themselves an admin.
    """
    import pymysql

    from db.rds_db import connect_to_rds
    from workflow_route.state_machine import get_user_org_id

    org_id = get_user_org_id(user_id)
    conn = None
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            def _email(uid):
                cur.execute("SELECT email FROM users WHERE user_id=%s AND user_type='admin'", (uid,))
                r = cur.fetchone()
                return r["email"] if r else None

            # launch:{admin_user_id} — the suffix is the org-root admin.
            if org_id and org_id.startswith("launch:"):
                cand = org_id.split(":", 1)[1]
                em = _email(cand)
                if em is not None:
                    return cand, em
            elif org_id:
                cur.execute(
                    "SELECT user_id, email FROM users WHERE company_name=%s AND user_type='admin' "
                    "ORDER BY user_id LIMIT 1",
                    (org_id,),
                )
                r = cur.fetchone()
                if r:
                    return r["user_id"], r["email"]

            # Fallback: the requester themselves, if an admin.
            cur.execute("SELECT email FROM users WHERE user_id=%s AND user_type='admin'", (user_id,))
            r = cur.fetchone()
            if r:
                return user_id, r["email"]
    except Exception:
        logger.warning("resolve org admin failed", exc_info=True)
    finally:
        if conn:
            conn.close()
    return None, None


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
        "approver": None,
        "approver_email": None,
    }

    try:
        from workflow_route.state_machine import create_workflow, get_user_org_id, transition

        org_id = get_user_org_id(user_id)
        if not org_id:
            service.storage.save_remediation_link(user_id, audit_id, finding_id, link)
            return {"status": "no_org", **link}

        approver, approver_email = _resolve_org_admin(user_id)
        approver = approver or user_id  # last-resort: the requester
        link["org_id"] = org_id
        link["approver"] = approver
        link["approver_email"] = approver_email

        # Create assigned to the admin across all stages so wherever it lands it is owned.
        wf = create_workflow(
            org_id=org_id,
            doc_type=_DOC_TYPE,
            doc_id=finding_id,
            doc_version="1",
            owner_user_id=user_id,
            quality_reviewer_user_id=approver,
            governance_reviewer_user_id=approver,
            approver_user_id=approver,
        )
        link["workflow_id"] = wf.get("workflow_id")
        link["state"] = wf.get("state", "draft")

        # Submit it into the pipeline so it surfaces in the admin's inbox (not an
        # orphaned draft). Best-effort: a permission/config gap leaves it at draft
        # assigned to the admin rather than failing the request.
        try:
            updated = transition(
                wf["workflow_id"], wf.get("state_version", 1), "quality_review",
                actor_user_id=user_id, comment="Submitted for remediation approval",
                quality_reviewer_user_id=approver,
                governance_reviewer_user_id=approver, approver_user_id=approver,
            )
            link["state"] = updated.get("state", "quality_review")
        except Exception as exc:
            logger.warning("remediation submit left workflow at draft: %s", exc)
    except Exception as exc:
        logger.warning("SG-audit remediation workflow creation failed: %s", exc, exc_info=True)
        link["error"] = str(exc)
        service.storage.save_remediation_link(user_id, audit_id, finding_id, link)
        return {"status": "error", **link}

    service.storage.save_remediation_link(user_id, audit_id, finding_id, link)
    logger.info("Routed remediation workflow %s for finding %s to %s (state=%s)",
                link["workflow_id"], finding_id, approver, link["state"])
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
