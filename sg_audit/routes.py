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
    return jsonify({"status": "success", "audits": SgAuditService().list_audits(user_id)})


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
