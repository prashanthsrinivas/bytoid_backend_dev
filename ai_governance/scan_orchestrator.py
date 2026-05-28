"""Orchestration for the platform-wide AI governance scan.

Three responsibilities:
  * ``enumerate_users``   — list every user + resolve their org owner.
  * ``scan_one_user``     — run the selected modes for one user with strict
                            failure isolation (one bad user never aborts a sweep).
  * ``aggregate_results`` — roll per-user results up to platform + per-org views.

Kept free of giskard/Bedrock imports — those live behind ``scan_modes`` and are
imported lazily per mode, so this module is import-safe everywhere.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import pymysql.cursors

from db.rds_db import connect_to_rds

logger = logging.getLogger(__name__)

# All modes; "guardrail" must run last so it can replay attacks the other modes
# surfaced.  Ordering is enforced in ``scan_one_user``.
ALL_MODES = ["tabular", "prompt", "raget", "guardrail"]
_MODE_ORDER = {m: i for i, m in enumerate(ALL_MODES)}

_ADMIN_TYPES = {"admin", "superadmin"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_close(conn) -> None:
    if conn is None:
        return
    try:
        conn.close()
    except Exception:
        logger.debug("scan_orchestrator: connection close suppressed", exc_info=True)


# ── User enumeration ────────────────────────────────────────────────────────────


def _resolve_org_admin_id(row: dict, email_to_admin: dict[str, str]) -> str:
    """Best-effort org owner for a user row.

    admin/superadmin → themselves; an invited user → the admin who invited them
    (``permissions.invited_by`` email resolved against the admin map); otherwise
    fall back to ``launch_id_fk`` or self.
    """
    uid = str(row.get("user_id"))
    if row.get("user_type") in _ADMIN_TYPES:
        return uid
    perms = row.get("permissions")
    if isinstance(perms, str):
        try:
            perms = json.loads(perms)
        except (ValueError, TypeError):
            perms = {}
    invited_by = perms.get("invited_by") if isinstance(perms, dict) else None
    if invited_by and invited_by in email_to_admin:
        return str(email_to_admin[invited_by])
    if row.get("launch_id_fk"):
        return str(row["launch_id_fk"])
    return uid


def enumerate_users(
    *, limit: int | None = None, user_filter: list[str] | None = None
) -> list[dict]:
    """Return ``[{user_id, email, user_type, org_admin_id}]`` for the platform.

    Returns ``[]`` on DB failure (the sweep degrades to a no-op rather than
    crashing).  ``user_filter`` restricts to specific ids; ``limit`` caps count.
    """
    conn = connect_to_rds()
    if conn is None:
        logger.warning("scan_orchestrator: enumerate_users — no DB connection")
        return []
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT user_id, email, user_type, permissions, launch_id_fk FROM users"
            )
            rows = cur.fetchall()
        conn.commit()
    except Exception as exc:
        logger.warning("scan_orchestrator: enumerate_users query failed: %s", exc)
        return []
    finally:
        _safe_close(conn)

    email_to_admin = {
        r["email"]: str(r["user_id"])
        for r in rows
        if r.get("user_type") in _ADMIN_TYPES and r.get("email")
    }
    wanted = set(user_filter) if user_filter else None

    users: list[dict] = []
    for r in rows:
        uid = str(r.get("user_id"))
        if wanted is not None and uid not in wanted:
            continue
        users.append(
            {
                "user_id": uid,
                "email": r.get("email"),
                "user_type": r.get("user_type"),
                "org_admin_id": _resolve_org_admin_id(r, email_to_admin),
            }
        )
        if limit and len(users) >= limit:
            break
    return users


# ── Per-user scan (failure-isolated) ────────────────────────────────────────────


def _dispatch_mode(
    mode: str,
    user_id: str,
    sample_size: int,
    max_questions: int,
    org_admin_id: str | None,
    collected_attacks: list[str],
) -> dict:
    """Extract sources + run a single mode. Raises only giskard/availability
    errors; data shortfalls become a ``skipped`` status."""
    from ai_governance import scan_modes, scan_sources

    if mode == "tabular":
        ext = scan_sources.extract_risk_dataframe(user_id)
        if ext.get("dataframe") is None:
            return {"status": "skipped", "reason": ext["meta"].get("error", "no_data")}
        return scan_modes.run_tabular_scan(ext["dataframe"])

    if mode == "raget":
        kb = scan_sources.extract_knowledge_docs(
            user_id, sample_size=sample_size, anonymize=True
        )
        pii = scan_sources.extract_pii_docs(user_id, sample_size=sample_size)
        docs = (kb.get("docs") or []) + (pii.get("docs") or [])
        if not docs:
            return {"status": "skipped", "reason": "no_documents"}
        return scan_modes.run_raget_scan(user_id, docs, max_questions=max_questions)

    if mode == "prompt":
        pr = scan_sources.extract_prompt_templates()
        if not pr.get("prompts"):
            return {"status": "skipped", "reason": "no_prompts"}
        return scan_modes.run_prompt_scan(pr["prompts"])

    if mode == "guardrail":
        cfg = scan_sources.extract_guardrail_config(org_admin_id or user_id)
        return scan_modes.run_guardrail_harness(
            collected_attacks, org_admin_id or user_id, config=cfg
        )

    return {"status": "error", "reason": "unknown_mode"}


def _overall_status(modes_result: dict) -> str:
    statuses = {m.get("status", "ok") for m in modes_result.values()}
    if statuses == {"skipped"}:
        return "skipped"
    if "degraded" in statuses:
        return "degraded"
    if "error" in statuses and not (statuses - {"error", "skipped"}):
        return "error"
    return "ok"


def scan_one_user(
    user_id: str,
    *,
    modes: list[str],
    sample_size: int = 200,
    max_questions: int = 10,
    org_admin_id: str | None = None,
) -> dict:
    """Run ``modes`` for one user. NEVER raises — every mode failure is captured
    as a per-mode ``status=error`` entry so a platform sweep always completes."""
    ordered = sorted(modes, key=lambda m: _MODE_ORDER.get(m, 99))
    result: dict = {
        "user_id": user_id,
        "org_admin_id": org_admin_id,
        "started_at": _now_iso(),
        "modes": {},
    }
    collected_attacks: list[str] = []

    for mode in ordered:
        try:
            mres = _dispatch_mode(
                mode, user_id, sample_size, max_questions, org_admin_id, collected_attacks
            )
        except Exception as exc:
            # GiskardUnavailable and anything else degrade to a clean error dict.
            reason = (
                "giskard_unavailable"
                if exc.__class__.__name__ == "GiskardUnavailable"
                else "exception"
            )
            logger.warning(
                "scan_one_user: mode=%s user=%s failed: %s", mode, user_id, exc
            )
            mres = {"status": "error", "reason": reason, "detail": str(exc)}
        result["modes"][mode] = mres
        if mode in ("raget", "prompt"):
            collected_attacks.extend(mres.get("attacks") or [])

    result["ended_at"] = _now_iso()
    result["status"] = _overall_status(result["modes"])
    return result


# ── Aggregation ─────────────────────────────────────────────────────────────────


def aggregate_results(per_user: list[dict]) -> dict:
    """Roll per-user results into a platform + per-org summary."""
    summary: dict = {
        "user_count": len(per_user),
        "status_counts": {},
        "issues_by_level": {},
        "issues_by_mode": {},
        "coverage_gaps": [],
        "per_org": {},
        "errors": [],
    }
    for u in per_user:
        if not u:
            continue
        st = u.get("status", "ok")
        summary["status_counts"][st] = summary["status_counts"].get(st, 0) + 1

        org = u.get("org_admin_id") or "unknown"
        org_roll = summary["per_org"].setdefault(
            org, {"user_count": 0, "issue_count": 0}
        )
        org_roll["user_count"] += 1

        for mode, mres in (u.get("modes") or {}).items():
            if not isinstance(mres, dict):
                continue
            counts = mres.get("counts_by_level") or {}
            for lvl, n in counts.items():
                summary["issues_by_level"][lvl] = summary["issues_by_level"].get(lvl, 0) + n
            issue_count = mres.get("issue_count")
            if issue_count is None:
                issue_count = sum(counts.values())
            summary["issues_by_mode"][mode] = (
                summary["issues_by_mode"].get(mode, 0) + issue_count
            )
            org_roll["issue_count"] += issue_count
            for gap in mres.get("coverage_gaps") or []:
                summary["coverage_gaps"].append({"user_id": u.get("user_id"), **gap})
            if mres.get("status") == "error":
                summary["errors"].append(
                    {
                        "user_id": u.get("user_id"),
                        "mode": mode,
                        "reason": mres.get("reason"),
                    }
                )
    return summary
