"""Per-finding remediation approval routing (reuses workflow_route, per provider).

Resolves the org admin/owner, creates a ``{provider}_posture_remediation``
workflow assigned to them, and submits it so it lands in their Reviews &
Approvals inbox. Mirrors ``sg_audit/remediation.py``. No cloud writes here.
"""

from __future__ import annotations

from datetime import datetime, timezone

from utils.base_logger import get_logger

logger = get_logger(__name__)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_org_admin(user_id):
    import pymysql

    from db.rds_db import connect_to_rds
    from workflow_route.state_machine import get_user_org_id

    org_id = get_user_org_id(user_id)
    conn = None
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            if org_id and org_id.startswith("launch:"):
                cand = org_id.split(":", 1)[1]
                cur.execute("SELECT email FROM users WHERE user_id=%s AND user_type='admin'", (cand,))
                r = cur.fetchone()
                if r:
                    return cand, r["email"]
            elif org_id:
                cur.execute("SELECT user_id, email FROM users WHERE company_name=%s AND user_type='admin' "
                            "ORDER BY user_id LIMIT 1", (org_id,))
                r = cur.fetchone()
                if r:
                    return r["user_id"], r["email"]
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


def request_remediation(provider, user_id, audit_id, finding, *, service=None) -> dict:
    if service is None:
        from cspm_core.service import CspmService
        service = CspmService(provider)
    finding_id = finding.get("finding_id")
    if not finding_id:
        return {"status": "not_found", "message": "finding has no id"}
    existing = service.storage.get_remediation_links(user_id, audit_id).get(finding_id)
    if existing and existing.get("workflow_id"):
        return {"status": "exists", **existing}

    rid = finding.get("rule_id", "")
    doc_type = f"{provider.key}_posture_remediation"
    link = {"finding_id": finding_id, "rule_id": rid, "rule_label": provider.rule_label(rid),
            "summary": finding.get("finding_summary", ""), "recommendation": provider.remediation_for(rid),
            "requested_by": user_id, "requested_at": _now(), "workflow_id": None, "state": "requested",
            "doc_type": doc_type, "approver": None, "approver_email": None}
    try:
        from workflow_route.state_machine import create_workflow, get_user_org_id, transition

        org_id = get_user_org_id(user_id)
        if not org_id:
            service.storage.save_remediation_link(user_id, audit_id, finding_id, link)
            return {"status": "no_org", **link}
        approver, approver_email = _resolve_org_admin(user_id)
        approver = approver or user_id
        link.update({"org_id": org_id, "approver": approver, "approver_email": approver_email})
        wf = create_workflow(org_id=org_id, doc_type=doc_type, doc_id=finding_id, doc_version="1",
                             owner_user_id=user_id, quality_reviewer_user_id=approver,
                             governance_reviewer_user_id=approver, approver_user_id=approver)
        link["workflow_id"] = wf.get("workflow_id")
        link["state"] = wf.get("state", "draft")
        try:
            updated = transition(wf["workflow_id"], wf.get("state_version", 1), "quality_review",
                                 actor_user_id=user_id, comment="Submitted for remediation approval",
                                 quality_reviewer_user_id=approver, governance_reviewer_user_id=approver,
                                 approver_user_id=approver)
            link["state"] = updated.get("state", "quality_review")
        except Exception as exc:
            logger.warning("remediation submit left workflow at draft: %s", exc)
    except Exception as exc:
        logger.warning("%s remediation workflow creation failed: %s", provider.key, exc, exc_info=True)
        link["error"] = str(exc)
        service.storage.save_remediation_link(user_id, audit_id, finding_id, link)
        return {"status": "error", **link}
    service.storage.save_remediation_link(user_id, audit_id, finding_id, link)
    logger.info("Routed %s remediation %s for %s to %s", provider.key, link["workflow_id"], finding_id, approver)
    return {"status": "created", **link}


def list_remediations(provider, user_id, audit_id, *, service=None) -> list:
    if service is None:
        from cspm_core.service import CspmService
        service = CspmService(provider)
    out = []
    for link in service.storage.get_remediation_links(user_id, audit_id).values():
        item = dict(link)
        if link.get("workflow_id"):
            try:
                from workflow_route.state_machine import get_workflow
                wf = get_workflow(link["workflow_id"])
                item["state"] = wf.get("state", item.get("state"))
                item["state_version"] = wf.get("state_version")
            except Exception:
                logger.debug("workflow lookup failed", exc_info=True)
        out.append(item)
    out.sort(key=lambda x: x.get("requested_at", ""), reverse=True)
    return out
