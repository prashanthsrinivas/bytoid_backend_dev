"""SG-audit HTTP blueprint — all paths prefixed ``/sg-audit``.

Caller identity follows the repo convention: ``user_id`` (body for writes, query
for reads), parsed with ``parse_composite_user_id``. The HMAC-signed Lambda
callback is the one unauthenticated route (it is in EXEMPT_PATHS and verifies its
own signature). Mirrors the ``vra`` blueprint's structure.
"""

import asyncio

from flask import Blueprint, jsonify, request

from utils.base_logger import get_logger
from utils.normal import parse_composite_user_id
from utils.permission_required import permission_required_body
from sg_audit import config as sg_config
from sg_audit.service import SgAuditService

sg_audit_bp = Blueprint("sg_audit", __name__)
logger = get_logger(__name__)


def _run_async(coro):
    """Run an async coroutine from a sync (gunicorn) worker context."""
    return asyncio.run(coro)


def _user_id_from_request():
    """Resolve the caller's user_id (body for writes, query for reads), parsed
    with the repo's composite-id convention. Returns ``(base, user_id)``."""
    base = None
    if request.method in ("POST", "PUT", "DELETE"):
        base = (request.get_json(silent=True) or {}).get("user_id")
    if not base:
        base = request.args.get("user_id")
    if not base:
        return None, None
    _logged_in, user_id = parse_composite_user_id(base)
    return base, user_id


@sg_audit_bp.route("/sg-audit/health", methods=["GET"])
def sg_health():
    """Readiness probe: is automatic cross-account collection configured?"""
    return jsonify({
        "status": "ok",
        "module": "sg_audit",
        "collection_enabled": sg_config.collection_enabled(),
        "region": sg_config.AWS_REGION,
        "default_audit_role": sg_config.SG_DEFAULT_AUDIT_ROLE_NAME,
        "rescan_cadence_days": sg_config.SG_RESCAN_CADENCE_DAYS,
        "retention_days": sg_config.SG_RETENTION_DAYS,
    })


@sg_audit_bp.route("/sg-audit/audit", methods=["POST"])
@permission_required_body("sg_audit.audit.create")
def sg_create_audit():
    """Register an audit (scope: accounts/regions/role). Generates an ExternalId."""
    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    data = request.get_json(silent=True) or {}
    record = SgAuditService().create_audit(
        user_id,
        name=data.get("name", ""),
        account_ids=data.get("account_ids"),
        regions=data.get("regions"),
        role_name=data.get("role_name"),
        external_id=data.get("external_id"),
        discover=data.get("discover"),
    )
    return jsonify({"status": "success", "audit": record}), 201


@sg_audit_bp.route("/sg-audit/audits", methods=["GET"])
@permission_required_body("sg_audit.findings.read")
def sg_list_audits():
    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    try:
        audits = SgAuditService().list_audits(user_id)
    except Exception:
        # Never surface a 5xx for the list view — degrade to empty.
        logger.warning("sg_list_audits failed for %s", user_id, exc_info=True)
        audits = []
    return jsonify({"status": "success", "audits": audits})


@sg_audit_bp.route("/sg-audit/audit/<audit_id>", methods=["GET"])
@permission_required_body("sg_audit.findings.read")
def sg_get_audit(audit_id):
    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    record = SgAuditService().get_audit(user_id, audit_id)
    if not record:
        return jsonify({"status": "error", "message": "Not found"}), 404
    return jsonify({"status": "success", "audit": record})


@sg_audit_bp.route("/sg-audit/audit/<audit_id>/targets", methods=["POST"])
@permission_required_body("sg_audit.audit.create")
def sg_set_targets(audit_id):
    """Update audit scope (accounts/regions/role/externalId)."""
    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    data = request.get_json(silent=True) or {}
    record = SgAuditService().set_targets(
        user_id,
        audit_id,
        name=data.get("name"),
        account_ids=data.get("account_ids"),
        regions=data.get("regions"),
        role_name=data.get("role_name"),
        external_id=data.get("external_id"),
        discover=data.get("discover"),
    )
    if not record:
        return jsonify({"status": "error", "message": "Not found"}), 404
    return jsonify({"status": "success", "audit": record})


@sg_audit_bp.route("/sg-audit/audit/<audit_id>", methods=["DELETE"])
@permission_required_body("sg_audit.audit.create")
def sg_delete_audit(audit_id):
    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    if not SgAuditService().delete_audit(user_id, audit_id):
        return jsonify({"status": "error", "message": "Not found"}), 404
    return jsonify({"status": "success", "deleted": audit_id})


@sg_audit_bp.route("/sg-audit/audit/<audit_id>/collect", methods=["POST"])
@permission_required_body("sg_audit.audit.create")
def sg_collect(audit_id):
    """Launch a cross-account SG audit scan (async Lambda) for the audit."""
    from sg_audit.collect import trigger_collection

    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    force = bool((request.get_json(silent=True) or {}).get("force"))
    result = _run_async(trigger_collection(user_id, audit_id, force=force))
    status = result.get("status")
    if status in ("launched", "unchanged", "already_running"):
        code = 200
    elif status in ("no_session", "session_expiring", "disabled", "skipped"):
        code = 409
    elif status == "error":
        code = 400
    else:
        code = 202
    return jsonify(result), code


@sg_audit_bp.route("/sg-audit/callback", methods=["POST"])
def sg_callback():
    """Receive an HMAC-signed posture snapshot from the collector Lambda.

    Authenticated by signature + timestamp + nonce (NOT a user session — it is in
    EXEMPT_PATHS). Verification/persistence lives in ``process_callback``.
    """
    from sg_audit.collect import process_callback

    raw_body = request.get_data(cache=False) or b""
    status_code, body = _run_async(process_callback(raw_body, dict(request.headers)))
    return jsonify(body), status_code


@sg_audit_bp.route("/sg-audit/audit/<audit_id>/findings", methods=["GET"])
@permission_required_body("sg_audit.findings.read")
def sg_findings(audit_id):
    """Findings for the latest (or ?scan_id) posture snapshot."""
    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    storage = SgAuditService().storage
    scan_id = request.args.get("scan_id")
    snapshot = (
        storage.get_snapshot(user_id, audit_id, scan_id)
        if scan_id else storage.get_latest_snapshot(user_id, audit_id)
    )
    if not snapshot:
        return jsonify({"status": "error", "message": "No scan found"}), 404
    return jsonify({
        "status": "success",
        "scan_id": snapshot.get("scan_id"),
        "scanned_at": snapshot.get("scanned_at"),
        "counts": snapshot.get("counts", {}),
        "collector_status": snapshot.get("collector_status", {}),
        "findings": snapshot.get("findings", []),
    })


@sg_audit_bp.route("/sg-audit/audit/<audit_id>/dashboard", methods=["GET"])
@permission_required_body("sg_audit.dashboard.read")
def sg_dashboard(audit_id):
    """Full Security Posture Dashboard model (executive + drill-down)."""
    from sg_audit.dashboard import build_dashboard

    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    service = SgAuditService()
    record = service.get_audit(user_id, audit_id)
    if not record:
        return jsonify({"status": "error", "message": "Not found"}), 404
    storage = service.storage
    snapshot = storage.get_latest_snapshot(user_id, audit_id)
    points = storage.trend(user_id, audit_id)
    prior_scores = [p["risk_score"] for p in points[:-1]]
    dashboard = build_dashboard(record, snapshot, points, prior_scores=prior_scores)
    return jsonify({"status": "success", "dashboard": dashboard})


@sg_audit_bp.route("/sg-audit/audit/<audit_id>/recommend", methods=["POST"])
@permission_required_body("sg_audit.recommend.generate")
def sg_recommend(audit_id):
    """Launch on-demand grounded AI tightening recommendations (async + polled).

    Bedrock generation can exceed the request timeout (API gateway 29s / gunicorn),
    so this kicks off generation in the background and returns immediately; the
    frontend polls GET /recommendations. Returns a ready result straight away if
    one already exists for the latest scan (unless force).
    """
    from sg_audit.helpers import acquire_rec_inflight
    from sg_audit.recommend import launch_generation

    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    storage = SgAuditService().storage
    body = request.get_json(silent=True) or {}
    scan_id = body.get("scan_id")
    snapshot = (
        storage.get_snapshot(user_id, audit_id, scan_id)
        if scan_id else storage.get_latest_snapshot(user_id, audit_id)
    )
    if not snapshot:
        return jsonify({"status": "error", "message": "No scan found"}), 404
    scan_id = snapshot.get("scan_id")
    force = bool(body.get("force"))

    existing = storage.get_recommendation(user_id, audit_id, scan_id)
    if existing and existing.get("status") == "success" and not force:
        return jsonify({"status": "ready", "recommendation": existing, "scan_id": scan_id}), 200

    # Single-flight: if a generation is already running for this scan, just report it.
    if not _run_async(acquire_rec_inflight(f"{audit_id}:{scan_id}")):
        return jsonify({"status": "generating", "scan_id": scan_id}), 202

    storage.save_recommendation(user_id, audit_id, scan_id, {"status": "generating", "scan_id": scan_id})
    launch_generation(user_id, audit_id, scan_id, snapshot)
    return jsonify({"status": "generating", "scan_id": scan_id}), 202


@sg_audit_bp.route("/sg-audit/audit/<audit_id>/recommendations", methods=["GET"])
@permission_required_body("sg_audit.findings.read")
def sg_get_recommendations(audit_id):
    """Poll the stored AI recommendation for the latest (or ?scan_id) scan."""
    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    storage = SgAuditService().storage
    scan_id = request.args.get("scan_id")
    if not scan_id:
        index = storage.list_snapshot_index(user_id, audit_id)
        scan_id = index[0]["scan_id"] if index else None
    if not scan_id:
        return jsonify({"status": "none"})
    rec = storage.get_recommendation(user_id, audit_id, scan_id)
    if not rec:
        return jsonify({"status": "none", "scan_id": scan_id})
    status = rec.get("status")
    if status == "success":
        return jsonify({"status": "ready", "recommendation": rec, "scan_id": scan_id})
    if status == "generating":
        return jsonify({"status": "generating", "scan_id": scan_id})
    # insufficient_credits / blocked / error
    return jsonify({"status": status or "error", "message": rec.get("message"), "scan_id": scan_id})


# ── Cloud Security Posture: global / per-domain / compliance ────────────────

@sg_audit_bp.route("/sg-audit/audit/<audit_id>/global", methods=["GET"])
@permission_required_body("sg_audit.dashboard.read")
def sg_global(audit_id):
    """Overall account posture: global score + risk-by-domain + top-10 + queue."""
    from sg_audit.analysis import score

    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    snapshot = SgAuditService().storage.get_latest_snapshot(user_id, audit_id)
    if not snapshot:
        return jsonify({"status": "error", "message": "No scan found"}), 404
    return jsonify({"status": "success", "global": score.global_posture(snapshot)})


@sg_audit_bp.route("/sg-audit/audit/<audit_id>/domain/<domain>", methods=["GET"])
@permission_required_body("sg_audit.findings.read")
def sg_domain(audit_id, domain):
    """Per-domain view: score + entity findings + top critical + priority queue."""
    from sg_audit.analysis import score
    from sg_audit.schema import DOMAIN_LABELS

    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    snapshot = SgAuditService().storage.get_latest_snapshot(user_id, audit_id)
    if not snapshot:
        return jsonify({"status": "error", "message": "No scan found"}), 404
    findings = [f for f in (snapshot.get("findings") or []) if f.get("domain") == domain]
    rows = score.per_domain(findings)
    summary = rows[0] if rows else {"domain": domain, "label": DOMAIN_LABELS.get(domain, domain),
                                    "risk_score": 0.0, "posture_score": 100.0, "rating": "Low",
                                    "total": 0, "by_severity": {}}
    return jsonify({
        "status": "success",
        "domain": domain,
        "label": DOMAIN_LABELS.get(domain, domain),
        "summary": summary,
        "entities": score.per_entity(findings),
        "top_critical": score.top_critical(findings, 10),
        "priority_queue": score.remediation_priority_queue(findings, 50),
        "findings": findings,
    })


@sg_audit_bp.route("/sg-audit/audit/<audit_id>/compliance", methods=["GET"])
@permission_required_body("sg_audit.dashboard.read")
def sg_compliance(audit_id):
    """Coverage + family heatmap for the latest scan (CIS + SOC2 + ISO27001).

    Pass ?framework=CIS|SOC2|ISO27001 for one; default returns all three.
    """
    from sg_audit.compliance import FRAMEWORKS, all_frameworks, coverage_for

    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    snapshot = SgAuditService().storage.get_latest_snapshot(user_id, audit_id)
    if not snapshot:
        return jsonify({"status": "error", "message": "No scan found"}), 404
    fw = request.args.get("framework")
    if fw in FRAMEWORKS:
        return jsonify({"status": "success", "compliance": coverage_for(snapshot, fw)})
    return jsonify({"status": "success", "frameworks": all_frameworks(snapshot)})


@sg_audit_bp.route("/sg-audit/audit/<audit_id>/report", methods=["GET"])
@permission_required_body("sg_audit.dashboard.read")
def sg_report(audit_id):
    """Grounded multi-domain executive report (markdown) for the latest scan."""
    from sg_audit.report_inputs import build_report

    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    service = SgAuditService()
    record = service.get_audit(user_id, audit_id)
    if not record:
        return jsonify({"status": "error", "message": "Not found"}), 404
    storage = service.storage
    snapshot = storage.get_latest_snapshot(user_id, audit_id)
    if not snapshot:
        return jsonify({"status": "error", "message": "No scan found"}), 404
    points = storage.trend(user_id, audit_id)
    prior_scores = [p["risk_score"] for p in points[:-1]]
    return jsonify({"status": "success", "markdown": build_report(snapshot, record, prior_scores)})


# ── Remediation approval routing (reuses workflow_route) ────────────────────

@sg_audit_bp.route("/sg-audit/audit/<audit_id>/finding/<path:finding_id>/remediation", methods=["POST"])
@permission_required_body("sg_audit.remediation.request")
def sg_request_remediation(audit_id, finding_id):
    """Open an approval workflow for one finding (workflow_route)."""
    from sg_audit.remediation import request_remediation

    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    snapshot = SgAuditService().storage.get_latest_snapshot(user_id, audit_id)
    if not snapshot:
        return jsonify({"status": "error", "message": "No scan found"}), 404
    finding = next((f for f in (snapshot.get("findings") or []) if f.get("finding_id") == finding_id), None)
    if not finding:
        return jsonify({"status": "error", "message": "Finding not found"}), 404
    result = request_remediation(user_id, audit_id, finding)
    code = {"created": 201, "exists": 200, "no_org": 409, "not_found": 404, "error": 502}.get(
        result.get("status"), 200
    )
    return jsonify(result), code


@sg_audit_bp.route("/sg-audit/audit/<audit_id>/finding/<path:finding_id>/remediate", methods=["POST"])
@permission_required_body("sg_audit.remediation.request")
def sg_execute_remediation(audit_id, finding_id):
    """Execute (or, by default, dry-run) an approved fix. Gated + opt-in.

    Requires SG_AUTO_REMEDIATE_ENABLED, an approved remediation workflow, and an
    explicit body ``{"dry_run": false}`` to perform a real AWS write.
    """
    from sg_audit.autoremediate import execute_remediation

    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    snapshot = SgAuditService().storage.get_latest_snapshot(user_id, audit_id)
    if not snapshot:
        return jsonify({"status": "error", "message": "No scan found"}), 404
    finding = next((f for f in (snapshot.get("findings") or []) if f.get("finding_id") == finding_id), None)
    if not finding:
        return jsonify({"status": "error", "message": "Finding not found"}), 404
    body = request.get_json(silent=True) or {}
    dry_run = body.get("dry_run", True)  # default to dry-run
    result = execute_remediation(user_id, audit_id, finding, dry_run=bool(dry_run))
    code = {"executed": 200, "planned": 200, "disabled": 409, "not_approved": 409,
            "unsupported": 422, "error": 502}.get(result.get("status"), 200)
    return jsonify(result), code


@sg_audit_bp.route("/sg-audit/audit/<audit_id>/remediations", methods=["GET"])
@permission_required_body("sg_audit.findings.read")
def sg_list_remediations(audit_id):
    """All remediation approval links for an audit, with live workflow state."""
    from sg_audit.remediation import list_remediations

    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    return jsonify({"status": "success", "remediations": list_remediations(user_id, audit_id)})


# ── Per-finding drill-down (detail / chat / suppress / rescan / rec) ─────────
# Shares cspm_core.finding_detail with the Azure/GCP providers; only the
# adapter differs (Lambda-based collection means rescan relaunches the audit).

def _sg_detail_ctx():
    from cspm_core.finding_detail import DetailContext
    from sg_audit import metadata
    from sg_audit.autoremediate import supported

    storage = SgAuditService().storage

    def _snap(uid, audit_id, scan_id=None):
        return (storage.get_snapshot(uid, audit_id, scan_id) if scan_id
                else storage.get_latest_snapshot(uid, audit_id))

    def _rescan(uid, audit_id, _finding):
        from sg_audit.collect import trigger_collection

        res = _run_async(trigger_collection(uid, audit_id, force=True))
        if res.get("status") in ("launched", "already_running"):
            return {"status": "launched", "scan_id": res.get("scan_id"),
                    "message": "Full audit rescan launched — findings refresh when the scan completes."}
        return res

    return DetailContext(key="sg", label="AWS", namespace="sg_audit", redis_namespace="sg_audit",
                         meta=metadata.meta, get_snapshot=_snap, rescan=_rescan,
                         has_fixer=supported, scope_key="account_id")


@sg_audit_bp.route("/sg-audit/audit/<audit_id>/finding/<path:finding_id>", methods=["GET"])
@permission_required_body("sg_audit.findings.read")
def sg_finding_detail(audit_id, finding_id):
    from cspm_core.finding_detail import detail_payload

    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    body, code = detail_payload(_sg_detail_ctx(), user_id, audit_id, finding_id,
                                request.args.get("scan_id"))
    return jsonify(body), code


@sg_audit_bp.route("/sg-audit/audit/<audit_id>/finding/<path:finding_id>/chat", methods=["POST"])
@permission_required_body("sg_audit.recommend.generate")
def sg_finding_chat(audit_id, finding_id):
    from cspm_core.finding_detail import chat_payload

    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    message = (request.get_json(silent=True) or {}).get("message", "")
    body, code = chat_payload(_sg_detail_ctx(), user_id, audit_id, finding_id, message)
    return jsonify(body), code


@sg_audit_bp.route("/sg-audit/audit/<audit_id>/finding/<path:finding_id>/declarations", methods=["POST"])
@permission_required_body("sg_audit.recommend.generate")
def sg_finding_declarations(audit_id, finding_id):
    from cspm_core.finding_detail import declarations_payload

    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    declarations = (request.get_json(silent=True) or {}).get("declarations")
    body, code = declarations_payload(_sg_detail_ctx(), user_id, audit_id, finding_id, declarations)
    return jsonify(body), code


@sg_audit_bp.route("/sg-audit/audit/<audit_id>/finding/<path:finding_id>/suppress", methods=["POST"])
@permission_required_body("sg_audit.remediation.request")
def sg_finding_suppress(audit_id, finding_id):
    from cspm_core.finding_detail import suppress_payload

    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    reason = (request.get_json(silent=True) or {}).get("reason", "")
    body, code = suppress_payload(_sg_detail_ctx(), user_id, audit_id, finding_id, reason)
    return jsonify(body), code


@sg_audit_bp.route("/sg-audit/audit/<audit_id>/finding/<path:finding_id>/unsuppress", methods=["POST"])
@permission_required_body("sg_audit.remediation.request")
def sg_finding_unsuppress(audit_id, finding_id):
    from cspm_core.finding_detail import unsuppress_payload

    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    body, code = unsuppress_payload(_sg_detail_ctx(), user_id, audit_id, finding_id)
    return jsonify(body), code


@sg_audit_bp.route("/sg-audit/audit/<audit_id>/finding/<path:finding_id>/rescan", methods=["POST"])
@permission_required_body("sg_audit.audit.create")
def sg_finding_rescan(audit_id, finding_id):
    from cspm_core.finding_detail import rescan_payload

    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    body, code = rescan_payload(_sg_detail_ctx(), user_id, audit_id, finding_id)
    return jsonify(body), code


@sg_audit_bp.route("/sg-audit/audit/<audit_id>/finding/<path:finding_id>/recommendation", methods=["POST"])
@permission_required_body("sg_audit.recommend.generate")
def sg_finding_recommend(audit_id, finding_id):
    from cspm_core.finding_detail import recommend_launch_payload

    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    force = bool((request.get_json(silent=True) or {}).get("force"))
    body, code = recommend_launch_payload(_sg_detail_ctx(), user_id, audit_id, finding_id, force=force)
    return jsonify(body), code


@sg_audit_bp.route("/sg-audit/audit/<audit_id>/finding/<path:finding_id>/recommendation", methods=["GET"])
@permission_required_body("sg_audit.findings.read")
def sg_finding_recommendation(audit_id, finding_id):
    from cspm_core.finding_detail import recommendation_get_payload

    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    body, code = recommendation_get_payload(_sg_detail_ctx(), user_id, audit_id, finding_id)
    return jsonify(body), code


# ── Action plan (consolidated, approvable, never executed) + architecture ────

def _sg_plan_ctx():
    from cspm_core.action_plan import ActionPlanContext
    from sg_audit import metadata
    from sg_audit.cli_commands import CLI_BUILDERS

    storage = SgAuditService().storage

    def _snap(uid, audit_id, scan_id=None):
        return (storage.get_snapshot(uid, audit_id, scan_id) if scan_id
                else storage.get_latest_snapshot(uid, audit_id))

    return ActionPlanContext(key="sg", label="AWS", namespace="sg_audit",
                             redis_namespace="sg_audit", meta=metadata.meta,
                             get_snapshot=_snap,
                             get_recommendation=storage.get_recommendation,
                             cli_tool="aws", cli_builders=CLI_BUILDERS,
                             scope_key="account_id")


@sg_audit_bp.route("/sg-audit/audit/<audit_id>/action-plan", methods=["POST"])
@permission_required_body("sg_audit.action_plan.generate")
def sg_action_plan_generate(audit_id):
    from cspm_core.action_plan import plan_launch_payload

    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    force = bool((request.get_json(silent=True) or {}).get("force"))
    body, code = plan_launch_payload(_sg_plan_ctx(), user_id, audit_id, force=force)
    return jsonify(body), code


@sg_audit_bp.route("/sg-audit/audit/<audit_id>/action-plan", methods=["GET"])
@permission_required_body("sg_audit.findings.read")
def sg_action_plan_get(audit_id):
    from cspm_core.action_plan import plan_get_payload

    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    body, code = plan_get_payload(_sg_plan_ctx(), user_id, audit_id)
    return jsonify(body), code


@sg_audit_bp.route("/sg-audit/audit/<audit_id>/action-plan/point/<point_id>/command",
                   methods=["POST"])
@permission_required_body("sg_audit.action_plan.edit")
def sg_action_plan_edit_command(audit_id, point_id):
    from cspm_core.action_plan import edit_command_payload

    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    data = request.get_json(silent=True) or {}
    body, code = edit_command_payload(_sg_plan_ctx(), user_id, audit_id, point_id,
                                      data.get("index"), data.get("command", ""))
    return jsonify(body), code


@sg_audit_bp.route("/sg-audit/audit/<audit_id>/action-plan/point/<point_id>/request-approval",
                   methods=["POST"])
@permission_required_body("sg_audit.action_plan.request")
def sg_action_plan_request_approval(audit_id, point_id):
    from cspm_core.action_plan import request_point_approval_payload

    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    body, code = request_point_approval_payload(_sg_plan_ctx(), user_id, audit_id, point_id)
    return jsonify(body), code


@sg_audit_bp.route("/sg-audit/audit/<audit_id>/architecture", methods=["GET"])
@permission_required_body("sg_audit.findings.read")
def sg_architecture_view(audit_id):
    from cspm_core.architecture import architecture_payload

    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    body, code = architecture_payload(_sg_detail_ctx(), user_id, audit_id)
    return jsonify(body), code


# ── Reuse: tables → Trackers + runbook Responses & Evidence ─────────────────

@sg_audit_bp.route("/sg-audit/audit/<audit_id>/evidence", methods=["GET"])
@permission_required_body("sg_audit.findings.read")
def sg_table_evidence(audit_id):
    """A table's rows as canonical Responses & Evidence records (?table=&framework=)."""
    from sg_audit.exports import TABLES, load_and_build

    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    table = request.args.get("table", "findings")
    if table not in TABLES:
        return jsonify({"status": "error", "message": f"Unknown table: {table}"}), 400
    built = load_and_build(user_id, audit_id, table, request.args.get("framework"))
    if built is None:
        return jsonify({"status": "error", "message": "No scan found"}), 404
    records = built["evidence"]
    return jsonify({"status": "success", "evidence": {
        "table": table, "name": built["name"], "records": records,
        "total_findings": len(records),
    }})


@sg_audit_bp.route("/sg-audit/audit/<audit_id>/to-tracker", methods=["POST"])
@permission_required_body("trackers.table.create")
def sg_to_tracker(audit_id):
    """Materialize a table into a standalone Tracker (mirrors /tracker/from-risk)."""
    from tab_tracker.helper import (
        _decrypt_tracker_data,
        append_to_tracker,
        check_config_exist,
        create_empty_tracker_config,
        create_tracker_config,
        create_tracker_file,
        save_tracker_file,
    )
    from utils.s3_utils import read_json_from_s3
    from sg_audit.exports import TABLES, load_and_build

    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    body = request.get_json(silent=True) or {}
    table = body.get("table", "findings")
    if table not in TABLES:
        return jsonify({"status": "error", "message": f"Unknown table: {table}"}), 400
    built = load_and_build(user_id, audit_id, table, body.get("framework"))
    if built is None:
        return jsonify({"status": "error", "message": "No scan found"}), 404

    name = (body.get("name") or "").strip() or f"{built['name']} — {audit_id[:6]}"
    block_id = f"sg-audit-{audit_id}-{table}"
    try:
        config_path, config_data = check_config_exist(user_id)
        if not config_data:
            create_empty_tracker_config(user_id)
            config_path, config_data = check_config_exist(user_id)
        tracker_id, file_path = create_tracker_config(
            config_path=config_path, user_id=user_id, name=name,
            tracker_type="table", runbook_id=f"sg-audit:{audit_id}", block_id=block_id,
        )
        create_tracker_file(
            user_id=user_id, tracker_id=tracker_id, tracker_type="table",
            runbook_id=f"sg-audit:{audit_id}", block_config={"columns": built["columns"]},
        )
        tracker_data = read_json_from_s3(file_path)
        if tracker_data:
            tracker_data, _ = _decrypt_tracker_data(user_id, tracker_data)
            block = {
                "block_id": block_id, "block_title": name,
                "headers": [c["name"] for c in built["columns"]], "rows": built["rows"],
            }
            append_to_tracker(tracker_data, block, block_id)
            save_tracker_file(user_id, tracker_id, tracker_data)
    except ValueError as ve:
        return jsonify({"status": "error", "message": str(ve)}), 400
    except Exception as exc:
        logger.warning("sg_to_tracker failed: %s", exc, exc_info=True)
        return jsonify({"status": "error", "message": str(exc)}), 502
    return jsonify({"status": "success", "tracker_id": tracker_id, "name": name,
                    "rows": len(built["rows"])}), 201


@sg_audit_bp.route("/sg-audit/runbooks", methods=["GET"])
@permission_required_body("compliance.runbook.read")
def sg_runbooks():
    """Runbooks the posture evidence can be pushed into (have a linked playbook)."""
    from sg_audit.runbook_evidence import list_runbooks

    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    return jsonify({"status": "success", "runbooks": _run_async(list_runbooks(user_id))})


@sg_audit_bp.route("/sg-audit/audit/<audit_id>/push-to-runbook", methods=["POST"])
@permission_required_body("compliance.runbook.edit")
def sg_push_to_runbook(audit_id):
    """Push a table's evidence into a chosen runbook's Responses & Evidence."""
    from sg_audit.exports import TABLES, load_and_build
    from sg_audit.runbook_evidence import push_evidence_to_runbook

    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    body = request.get_json(silent=True) or {}
    runbook_id = body.get("runbook_id")
    if not runbook_id:
        return jsonify({"status": "error", "message": "Missing runbook_id"}), 400
    table = body.get("table", "findings")
    if table not in TABLES:
        return jsonify({"status": "error", "message": f"Unknown table: {table}"}), 400
    built = load_and_build(user_id, audit_id, table, body.get("framework"))
    if built is None:
        return jsonify({"status": "error", "message": "No scan found"}), 404
    result = _run_async(push_evidence_to_runbook(user_id, runbook_id, built["evidence"], built["name"]))
    code = {"queued": 200, "empty": 200, "not_found": 404, "not_linked": 409, "error": 502}.get(
        result.get("status"), 200
    )
    return jsonify(result), code
