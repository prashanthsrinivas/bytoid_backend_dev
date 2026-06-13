"""Aggregation for the GRC overview dashboards (Governance / Risk / Compliance).

Each metric is computed independently and defensively: a single failing source
(missing table, S3 hiccup, un-connected cloud provider, absent module) returns a
null/empty default rather than raising, so a summary endpoint always responds
with a partial object instead of 500-ing. Heavy/optional modules are imported
lazily inside the metric functions.

Reuses: strategy health rollup (strategy/helper + strategy/rollup),
policy_hub_documents (policy_hub/doc_index), document_workflow
(workflow_route/state_machine), statement_tracker_refs, the cloud-posture audit
services (sg_audit / cspm_core), and config_evidences.
"""

from datetime import datetime, timezone

import pymysql.cursors

from db.rds_db import connect_to_rds
from utils.base_logger import get_logger

logger = get_logger(__name__)


def _safe(fn, default):
    """Run a metric fn, returning *default* on any failure (best-effort)."""
    try:
        return fn()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("grc_dashboards metric failed: %s", exc)
        return default


def _now():
    return datetime.now(timezone.utc).isoformat()


# ── org / id resolution ───────────────────────────────────────────────────────

def _wf_org(user_id):
    """Org key as written into policy_hub_documents / document_workflow."""
    from workflow_route.state_machine import get_user_org_id

    return get_user_org_id(user_id)


def _strategy_org(user_id):
    from strategy.helper import resolve_org_key

    return resolve_org_key(user_id)


# ── Governance metrics ─────────────────────────────────────────────────────────

def _policy_counts(user_id) -> dict:
    org = _safe(lambda: _wf_org(user_id), None)
    where, param = ("org_id=%s", org) if org else ("user_id=%s", user_id)
    out = {"policy": 0, "procedure": 0, "standard": 0, "total": 0, "needs_review": 0}
    conn = connect_to_rds()
    if conn is None:
        return out
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                f"SELECT doc_type, COUNT(*) AS c FROM policy_hub_documents WHERE {where} GROUP BY doc_type",
                (param,),
            )
            for r in cur.fetchall() or []:
                dt = (r.get("doc_type") or "").lower()
                c = int(r.get("c", 0) or 0)
                if dt in out:
                    out[dt] = c
                out["total"] += c
            cur.execute(
                f"SELECT COUNT(*) AS c FROM policy_hub_documents WHERE {where} AND validation_status=%s",
                (param, "needs_review"),
            )
            out["needs_review"] = int((cur.fetchone() or {}).get("c", 0) or 0)
    finally:
        conn.close()
    return out


def _approval_counts(user_id) -> dict:
    org = _safe(lambda: _wf_org(user_id), None)
    out = {"quality_review": 0, "governance_review": 0, "approval": 0, "total": 0}
    if not org:
        return out
    conn = connect_to_rds()
    if conn is None:
        return out
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT state, COUNT(*) AS c FROM document_workflow "
                "WHERE org_id=%s AND state IN ('quality_review','governance_review','approval') "
                "GROUP BY state",
                (org,),
            )
            for r in cur.fetchall() or []:
                st = r.get("state")
                c = int(r.get("c", 0) or 0)
                if st in out:
                    out[st] = c
                out["total"] += c
    finally:
        conn.close()
    return out


def _strategy_rollup(user_id) -> dict:
    from strategy.helper import get_roadmap
    from strategy.rollup import aggregate_rollups

    org = _strategy_org(user_id)
    roadmap = get_roadmap(org)
    objectives = roadmap.get("objectives", []) or []
    n_programs = sum(len(o.get("programs", []) or []) for o in objectives)
    n_projects = sum(
        len(o.get("direct_projects", []) or [])
        + sum(len(p.get("projects", []) or []) for p in (o.get("programs", []) or []))
        for o in objectives
    )
    health = aggregate_rollups([o.get("health") for o in objectives])
    return {
        "objectives": len(objectives),
        "programs": n_programs,
        "projects": n_projects,
        "health": health,
    }


def governance_summary(user_id) -> dict:
    return {
        "generated_at": _now(),
        "policies": _safe(lambda: _policy_counts(user_id), {}),
        "approvals": _safe(lambda: _approval_counts(user_id), {}),
        "strategy": _safe(lambda: _strategy_rollup(user_id), None),
    }


# ── Risk metrics ───────────────────────────────────────────────────────────────

def _org_policy_ids(user_id, cur) -> list:
    org = _safe(lambda: _wf_org(user_id), None)
    if org:
        cur.execute("SELECT policy_id FROM policy_hub_documents WHERE org_id=%s", (org,))
    else:
        cur.execute("SELECT policy_id FROM policy_hub_documents WHERE user_id=%s", (user_id,))
    return [r["policy_id"] for r in (cur.fetchall() or [])]


def _control_status(user_id) -> dict:
    """Pass/fail/not_assessed rollup + top failing trackers, from statement_tracker_refs."""
    from strategy.rollup import rollup_status

    out = {"rollup": rollup_status([]), "top_failing": []}
    conn = connect_to_rds()
    if conn is None:
        return out
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            policy_ids = _org_policy_ids(user_id, cur)
            if not policy_ids:
                return out
            placeholders = ",".join(["%s"] * len(policy_ids))
            cur.execute(
                f"SELECT status FROM statement_tracker_refs WHERE policy_id IN ({placeholders})",
                tuple(policy_ids),
            )
            out["rollup"] = rollup_status([dict(r) for r in (cur.fetchall() or [])])
            cur.execute(
                "SELECT tracker_id, tracker_abbrev, COUNT(*) AS failing "
                f"FROM statement_tracker_refs WHERE policy_id IN ({placeholders}) AND status='failed' "
                "GROUP BY tracker_id, tracker_abbrev ORDER BY failing DESC LIMIT 5",
                tuple(policy_ids),
            )
            out["top_failing"] = [dict(r) for r in (cur.fetchall() or [])]
    finally:
        conn.close()
    return out


def _tracker_count(user_id) -> int:
    """Distinct trackers referencing the org's policies (from the refs graph)."""
    conn = connect_to_rds()
    if conn is None:
        return 0
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            policy_ids = _org_policy_ids(user_id, cur)
            if not policy_ids:
                return 0
            placeholders = ",".join(["%s"] * len(policy_ids))
            cur.execute(
                f"SELECT COUNT(DISTINCT tracker_id) AS c FROM statement_tracker_refs "
                f"WHERE policy_id IN ({placeholders})",
                tuple(policy_ids),
            )
            return int((cur.fetchone() or {}).get("c", 0) or 0)
    finally:
        conn.close()


def risk_summary(user_id) -> dict:
    posture = _safe(lambda: _latest_posture_by_provider(user_id), {})
    return {
        "generated_at": _now(),
        "controls": _safe(lambda: _control_status(user_id), None),
        "tracker_count": _safe(lambda: _tracker_count(user_id), 0),
        "posture": posture,
    }


# ── Cloud posture (shared by Risk + Compliance) ────────────────────────────────

def _keep_latest(acc: dict, provider: str, rec: dict) -> None:
    cur = acc.get(provider)
    if cur is None or (rec.get("last_scan_at") or "") > (cur.get("last_scan_at") or ""):
        acc[provider] = rec


def _provider_card(rec: dict, provider: str) -> dict:
    return {
        "provider": provider,
        "name": rec.get("name"),
        "scan_state": rec.get("scan_state"),
        "last_scan_at": rec.get("last_scan_at"),
        "posture_score": rec.get("latest_posture_score"),
        "risk_score": rec.get("latest_risk_score"),
        "audit_id": rec.get("audit_id"),
    }


def _latest_posture_by_provider(user_id) -> dict:
    """Latest audit record per cloud provider (cheap — metadata only, no snapshot
    decryption). Each provider is attempted independently; failures are skipped."""
    latest: dict = {}

    def _aws():
        from sg_audit.service import SgAuditService
        for rec in SgAuditService().list_audits(user_id) or []:
            _keep_latest(latest, "aws", rec)

    def _cspm(provider_key, import_path, attr):
        from cspm_core.service import CspmService
        mod = __import__(import_path, fromlist=[attr])
        provider = getattr(mod, attr)
        for rec in CspmService(provider).list_audits(user_id) or []:
            _keep_latest(latest, provider_key, rec)

    _safe(_aws, None)
    _safe(lambda: _cspm("azure", "azure_audit.provider", "AZURE_PROVIDER"), None)
    _safe(lambda: _cspm("gcp", "gcp_audit.provider", "GCP_PROVIDER"), None)

    return {p: _provider_card(rec, p) for p, rec in latest.items()}


# ── Compliance metrics ─────────────────────────────────────────────────────────

def _evidence_count(user_id) -> int:
    from config_evidences.evidence_helpers import get_only_evidence

    return len(get_only_evidence(user_id) or [])


def compliance_summary(user_id) -> dict:
    return {
        "generated_at": _now(),
        "posture": _safe(lambda: _latest_posture_by_provider(user_id), {}),
        "evidence_count": _safe(lambda: _evidence_count(user_id), 0),
    }
