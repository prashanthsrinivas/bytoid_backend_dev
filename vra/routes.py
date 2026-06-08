"""VRA HTTP blueprint.

Phase 0 ships the blueprint + a read-only status endpoint so registration is
wired and testable. The collection trigger, HMAC callback, and dashboard
endpoints are added in later phases on this same surface (all paths prefixed
``/vra``). Caller identity follows the repo convention: ``user_id`` (query or
body) parsed with ``parse_composite_user_id``.
"""

import asyncio

from flask import Blueprint, jsonify, request

from utils.base_logger import get_logger
from utils.normal import parse_composite_user_id
from utils.permission_required import permission_required_body
from vra import config as vra_config
from vra.service import VraService, build_default_question_items

vra_bp = Blueprint("vra", __name__)
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


@vra_bp.route("/vra/health", methods=["GET"])
def vra_health():
    """Lightweight readiness probe: is automatic OSINT collection configured?

    Never reveals secrets — only whether the Lambda + HMAC wiring is present so
    the frontend can decide whether to surface "collecting…" UI for VRAs.
    """
    return jsonify(
        {
            "status": "ok",
            "module": "vra",
            "collection_enabled": vra_config.collection_enabled(),
            "region": vra_config.AWS_REGION,
            "rescan_cadence_days": vra_config.VRA_RESCAN_CADENCE_DAYS,
            "retention_days": vra_config.VRA_RETENTION_DAYS,
        }
    )


@vra_bp.route("/vra/default-questions", methods=["GET"])
@permission_required_body("vra.assessment.create")
def vra_default_questions():
    """The two mandatory VRA questions for the questionnaire builder to prepend."""
    return jsonify({"status": "success", "questions": build_default_question_items()})


@vra_bp.route("/vra/assessment", methods=["POST"])
@permission_required_body("vra.assessment.create")
def vra_create_assessment():
    """Register a VRA assessment (vendor↔playbook↔runbook mapping) in S3."""
    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    data = request.get_json(silent=True) or {}
    record = VraService().create_assessment(
        user_id,
        playbook_id=data.get("playbook_id"),
        runbook_id=data.get("runbook_id"),
        vendor_name=data.get("vendor_name", ""),
        vendor_domain=data.get("vendor_domain", ""),
    )
    return jsonify(
        {
            "status": "success",
            "assessment": record,
            "default_questions": build_default_question_items(),
        }
    ), 201


@vra_bp.route("/vra/assessment/<assessment_id>", methods=["GET"])
@permission_required_body("vra.intelligence.read")
def vra_get_assessment(assessment_id):
    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    record = VraService().get_assessment(user_id, assessment_id)
    if not record:
        return jsonify({"status": "error", "message": "Not found"}), 404
    return jsonify({"status": "success", "assessment": record})


@vra_bp.route("/vra/assessments", methods=["GET"])
@permission_required_body("vra.intelligence.read")
def vra_list_assessments():
    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    return jsonify(
        {"status": "success", "assessments": VraService().list_assessments(user_id)}
    )


@vra_bp.route("/vra/assessment/<assessment_id>/vendor", methods=["POST"])
@permission_required_body("vra.assessment.create")
def vra_set_vendor(assessment_id):
    """Set/update vendor name+domain; keeps the report title in lockstep."""
    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    data = request.get_json(silent=True) or {}
    record = VraService().set_vendor(
        user_id,
        assessment_id,
        vendor_name=data.get("vendor_name"),
        vendor_domain=data.get("vendor_domain"),
    )
    if not record:
        return jsonify({"status": "error", "message": "Not found"}), 404
    return jsonify({"status": "success", "assessment": record})


@vra_bp.route("/vra/assessment/<assessment_id>", methods=["DELETE"])
@permission_required_body("vra.assessment.create")
def vra_delete_assessment(assessment_id):
    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    if not VraService().delete_assessment(user_id, assessment_id):
        return jsonify({"status": "error", "message": "Not found"}), 404
    return jsonify({"status": "success", "deleted": assessment_id})


@vra_bp.route("/vra/assessment/<assessment_id>/collect", methods=["POST"])
@permission_required_body("vra.assessment.create")
def vra_collect(assessment_id):
    """Launch an OSINT collection scan (async Lambda) for the assessment."""
    from vra.collect import trigger_collection

    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    force = bool((request.get_json(silent=True) or {}).get("force"))
    result = _run_async(trigger_collection(user_id, assessment_id, force=force))
    code = 200 if result.get("status") in ("launched", "unchanged", "already_running") else 202
    if result.get("status") == "error":
        code = 400
    return jsonify(result), code


@vra_bp.route("/vra/osint/callback", methods=["POST"])
def vra_osint_callback():
    """Receive an HMAC-signed OSINT snapshot from the collector Lambda.

    Authenticated by signature + timestamp + nonce (NOT the user session — it is
    in EXEMPT_PATHS). Verification/persistence lives in ``process_callback``.
    """
    from vra.collect import process_callback

    raw_body = request.get_data(cache=False) or b""
    status_code, body = _run_async(process_callback(raw_body, dict(request.headers)))
    return jsonify(body), status_code


@vra_bp.route("/vra/assessment/<assessment_id>/evidence", methods=["GET"])
@permission_required_body("vra.intelligence.read")
def vra_evidence(assessment_id):
    """Derived 'Responses & Evidence' records for the latest (or ?scan_id) scan."""
    from vra.evidence import snapshot_to_evidence

    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    storage = VraService().storage
    scan_id = request.args.get("scan_id")
    snapshot = (
        storage.get_snapshot(user_id, assessment_id, scan_id)
        if scan_id
        else storage.get_latest_snapshot(user_id, assessment_id)
    )
    if not snapshot:
        return jsonify({"status": "error", "message": "No scan found"}), 404
    return jsonify({"status": "success", "evidence": snapshot_to_evidence(snapshot)})


@vra_bp.route("/vra/assessment/<assessment_id>/analysis", methods=["GET"])
@permission_required_body("vra.intelligence.read")
def vra_analysis(assessment_id):
    """Grounded AI-analysis context (risk rating, key observations, trend)."""
    from vra.report_inputs import build_analysis_context, render_context_markdown

    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    storage = VraService().storage
    snapshot = storage.get_latest_snapshot(user_id, assessment_id)
    if not snapshot:
        return jsonify({"status": "error", "message": "No scan found"}), 404
    points = storage.trend(user_id, assessment_id)
    prior_scores = [p["risk_score"] for p in points[:-1]]  # exclude current
    context = build_analysis_context(snapshot, prior_scores)
    return jsonify(
        {"status": "success", "context": context, "markdown": render_context_markdown(context)}
    )


@vra_bp.route("/vra/assessment/<assessment_id>/dashboard", methods=["GET"])
@permission_required_body("vra.dashboard.read")
def vra_dashboard(assessment_id):
    """Full Vendor Intelligence Dashboard model (executive + drill-down)."""
    from vra.dashboard import build_dashboard

    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    service = VraService()
    record = service.get_assessment(user_id, assessment_id)
    if not record:
        return jsonify({"status": "error", "message": "Not found"}), 404
    storage = service.storage
    snapshot = storage.get_latest_snapshot(user_id, assessment_id)
    points = storage.trend(user_id, assessment_id)
    prior_scores = [p["risk_score"] for p in points[:-1]]
    dashboard = build_dashboard(record, snapshot, points, prior_scores=prior_scores)
    return jsonify({"status": "success", "dashboard": dashboard})


@vra_bp.route("/vra/assessment/<assessment_id>/link", methods=["POST"])
@permission_required_body("vra.assessment.create")
def vra_link_runbook(assessment_id):
    """Associate a runbook + playbook with the assessment."""
    from vra.runbook_bridge import link_runbook

    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    data = request.get_json(silent=True) or {}
    record = link_runbook(
        user_id,
        assessment_id,
        runbook_id=data.get("runbook_id"),
        playbook_id=data.get("playbook_id"),
    )
    if not record:
        return jsonify({"status": "error", "message": "Not found"}), 404
    return jsonify({"status": "success", "assessment": record})


@vra_bp.route("/vra/assessment/<assessment_id>/report", methods=["POST"])
@permission_required_body("vra.assessment.create")
def vra_regenerate_report(assessment_id):
    """On-demand: enqueue runbook report regeneration for a linked VRA."""
    from vra.runbook_bridge import request_regeneration

    _base, user_id = _user_id_from_request()
    if not user_id:
        return jsonify({"status": "error", "message": "Missing user_id"}), 400
    result = request_regeneration(user_id, assessment_id)
    code = {"queued": 200, "not_linked": 409, "not_found": 404, "error": 502}.get(
        result.get("status"), 200
    )
    return jsonify(result), code
