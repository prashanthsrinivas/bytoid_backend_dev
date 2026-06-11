"""Build a Flask blueprint with the full CSPM endpoint surface for a provider.

All route logic is generic — parameterized by the ``Provider`` and its permission
keys. Paths are ``/<provider.route_prefix>/...``. Mirrors ``sg_audit/routes.py``.
"""

from __future__ import annotations

import asyncio

from flask import Blueprint, jsonify, request

from utils.base_logger import get_logger
from utils.normal import parse_composite_user_id
from utils.permission_required import permission_required_body
from cspm_core.service import CspmService

logger = get_logger(__name__)


def _run_async(coro):
    return asyncio.run(coro)


def _user_id():
    base = None
    if request.method in ("POST", "PUT", "DELETE"):
        base = (request.get_json(silent=True) or {}).get("user_id")
    if not base:
        base = request.args.get("user_id")
    if not base:
        return None
    _logged_in, uid = parse_composite_user_id(base)
    return uid


def build_blueprint(provider) -> Blueprint:
    P = provider
    perms = P.perms
    prefix = P.route_prefix
    bp = Blueprint(f"{P.key}_audit", __name__)

    def svc():
        return CspmService(P)

    def need_user():
        uid = _user_id()
        return uid, (None if uid else (jsonify({"status": "error", "message": "Missing user_id"}), 400))

    # ── health + audit CRUD ─────────────────────────────────────────────────
    @bp.route(f"/{prefix}/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok", "provider": P.key, "label": P.label,
                        "domains": list(P.domains), "scope_label": P.scope_label})

    @bp.route(f"/{prefix}/audit", methods=["POST"])
    @permission_required_body(perms["create"])
    def create_audit():
        uid, err = need_user()
        if err:
            return err
        data = request.get_json(silent=True) or {}
        rec = svc().create_audit(uid, name=data.get("name", ""), scope_ids=data.get("scope_ids"),
                                 domains=data.get("domains"), organization_id=data.get("organization_id", ""),
                                 regions=data.get("regions"))
        return jsonify({"status": "success", "audit": rec, "role_hint": P.default_role_hint}), 201

    @bp.route(f"/{prefix}/audits", methods=["GET"])
    @permission_required_body(perms["findings_read"])
    def list_audits():
        uid, err = need_user()
        if err:
            return err
        try:
            audits = svc().list_audits(uid)
        except Exception:
            logger.warning("%s list_audits failed", P.key, exc_info=True)
            audits = []
        return jsonify({"status": "success", "audits": audits})

    @bp.route(f"/{prefix}/audit/<audit_id>", methods=["GET"])
    @permission_required_body(perms["findings_read"])
    def get_audit(audit_id):
        uid, err = need_user()
        if err:
            return err
        rec = svc().get_audit(uid, audit_id)
        return (jsonify({"status": "success", "audit": rec}) if rec
                else (jsonify({"status": "error", "message": "Not found"}), 404))

    @bp.route(f"/{prefix}/audit/<audit_id>/targets", methods=["POST"])
    @permission_required_body(perms["create"])
    def set_targets(audit_id):
        uid, err = need_user()
        if err:
            return err
        d = request.get_json(silent=True) or {}
        rec = svc().set_targets(uid, audit_id, name=d.get("name"), scope_ids=d.get("scope_ids"),
                                domains=d.get("domains"), organization_id=d.get("organization_id"),
                                regions=d.get("regions"))
        return (jsonify({"status": "success", "audit": rec}) if rec
                else (jsonify({"status": "error", "message": "Not found"}), 404))

    @bp.route(f"/{prefix}/audit/<audit_id>", methods=["DELETE"])
    @permission_required_body(perms["create"])
    def delete_audit(audit_id):
        uid, err = need_user()
        if err:
            return err
        return (jsonify({"status": "success", "deleted": audit_id}) if svc().delete_audit(uid, audit_id)
                else (jsonify({"status": "error", "message": "Not found"}), 404))

    @bp.route(f"/{prefix}/audit/<audit_id>/collect", methods=["POST"])
    @permission_required_body(perms["create"])
    def collect(audit_id):
        from cspm_core.collect import trigger_collection
        uid, err = need_user()
        if err:
            return err
        force = bool((request.get_json(silent=True) or {}).get("force"))
        res = _run_async(trigger_collection(P, uid, audit_id, force=force))
        st = res.get("status")
        code = 200 if st in ("launched", "unchanged", "already_running") else \
            409 if st in ("no_session", "session_expiring", "disabled", "skipped") else \
            400 if st == "error" else 202
        return jsonify(res), code

    # ── findings / dashboard / global / domain / compliance / report ────────
    @bp.route(f"/{prefix}/audit/<audit_id>/findings", methods=["GET"])
    @permission_required_body(perms["findings_read"])
    def findings(audit_id):
        uid, err = need_user()
        if err:
            return err
        storage = svc().storage
        scan_id = request.args.get("scan_id")
        snap = storage.get_snapshot(uid, audit_id, scan_id) if scan_id else storage.get_latest_snapshot(uid, audit_id)
        if not snap:
            return jsonify({"status": "error", "message": "No scan found"}), 404
        return jsonify({"status": "success", "scan_id": snap.get("scan_id"), "scanned_at": snap.get("scanned_at"),
                        "counts": snap.get("counts", {}), "collector_status": snap.get("collector_status", {}),
                        "findings": snap.get("findings", [])})

    @bp.route(f"/{prefix}/audit/<audit_id>/dashboard", methods=["GET"])
    @permission_required_body(perms["dashboard_read"])
    def dashboard(audit_id):
        from cspm_core.dashboard import build_dashboard
        uid, err = need_user()
        if err:
            return err
        service = svc()
        rec = service.get_audit(uid, audit_id)
        if not rec:
            return jsonify({"status": "error", "message": "Not found"}), 404
        snap = service.storage.get_latest_snapshot(uid, audit_id)
        pts = service.storage.trend(uid, audit_id)
        prior = [p["risk_score"] for p in pts[:-1]]
        return jsonify({"status": "success", "dashboard": build_dashboard(P, rec, snap, pts, prior_scores=prior)})

    @bp.route(f"/{prefix}/audit/<audit_id>/global", methods=["GET"])
    @permission_required_body(perms["dashboard_read"])
    def global_view(audit_id):
        from cspm_core import score
        uid, err = need_user()
        if err:
            return err
        snap = svc().storage.get_latest_snapshot(uid, audit_id)
        if not snap:
            return jsonify({"status": "error", "message": "No scan found"}), 404
        return jsonify({"status": "success", "global": score.global_posture(snap, P)})

    @bp.route(f"/{prefix}/audit/<audit_id>/domain/<domain>", methods=["GET"])
    @permission_required_body(perms["findings_read"])
    def domain_view(audit_id, domain):
        from cspm_core import score
        uid, err = need_user()
        if err:
            return err
        snap = svc().storage.get_latest_snapshot(uid, audit_id)
        if not snap:
            return jsonify({"status": "error", "message": "No scan found"}), 404
        f = [x for x in (snap.get("findings") or []) if x.get("domain") == domain]
        rows = score.per_domain(f, P)
        summary = rows[0] if rows else {"domain": domain, "label": P.domain_labels.get(domain, domain),
                                        "risk_score": 0.0, "posture_score": 100.0, "rating": "Low", "total": 0, "by_severity": {}}
        return jsonify({"status": "success", "domain": domain, "label": P.domain_labels.get(domain, domain),
                        "summary": summary, "entities": score.per_entity(f),
                        "top_critical": score.top_critical(f, P, 10),
                        "priority_queue": score.remediation_priority_queue(f, P, 50), "findings": f})

    @bp.route(f"/{prefix}/audit/<audit_id>/compliance", methods=["GET"])
    @permission_required_body(perms["dashboard_read"])
    def compliance(audit_id):
        from cspm_core.compliance import FRAMEWORKS, all_frameworks, coverage_for
        uid, err = need_user()
        if err:
            return err
        snap = svc().storage.get_latest_snapshot(uid, audit_id)
        if not snap:
            return jsonify({"status": "error", "message": "No scan found"}), 404
        fw = request.args.get("framework")
        if fw in FRAMEWORKS:
            return jsonify({"status": "success", "compliance": coverage_for(snap, P, fw)})
        return jsonify({"status": "success", "frameworks": all_frameworks(snap, P)})

    @bp.route(f"/{prefix}/audit/<audit_id>/report", methods=["GET"])
    @permission_required_body(perms["dashboard_read"])
    def report(audit_id):
        from cspm_core.report_inputs import build_report
        uid, err = need_user()
        if err:
            return err
        service = svc()
        rec = service.get_audit(uid, audit_id)
        snap = service.storage.get_latest_snapshot(uid, audit_id)
        if not (rec and snap):
            return jsonify({"status": "error", "message": "No scan found"}), 404
        pts = service.storage.trend(uid, audit_id)
        return jsonify({"status": "success", "markdown": build_report(P, snap, rec, [p["risk_score"] for p in pts[:-1]])})

    # ── AI recommendations (async + polled) ─────────────────────────────────
    @bp.route(f"/{prefix}/audit/<audit_id>/recommend", methods=["POST"])
    @permission_required_body(perms["recommend"])
    def recommend(audit_id):
        from cspm_core.helpers import acquire_rec_inflight
        from cspm_core.recommend import launch_generation
        uid, err = need_user()
        if err:
            return err
        service = svc()
        body = request.get_json(silent=True) or {}
        scan_id = body.get("scan_id")
        snap = (service.storage.get_snapshot(uid, audit_id, scan_id) if scan_id
                else service.storage.get_latest_snapshot(uid, audit_id))
        if not snap:
            return jsonify({"status": "error", "message": "No scan found"}), 404
        scan_id = snap.get("scan_id")
        existing = service.storage.get_recommendation(uid, audit_id, scan_id)
        if existing and existing.get("status") == "success" and not body.get("force"):
            return jsonify({"status": "ready", "recommendation": existing, "scan_id": scan_id}), 200
        if not _run_async(acquire_rec_inflight(P.redis_namespace, f"{audit_id}:{scan_id}")):
            return jsonify({"status": "generating", "scan_id": scan_id}), 202
        service.storage.save_recommendation(uid, audit_id, scan_id, {"status": "generating", "scan_id": scan_id})
        launch_generation(P, service, uid, audit_id, scan_id, snap)
        return jsonify({"status": "generating", "scan_id": scan_id}), 202

    @bp.route(f"/{prefix}/audit/<audit_id>/recommendations", methods=["GET"])
    @permission_required_body(perms["findings_read"])
    def recommendations(audit_id):
        uid, err = need_user()
        if err:
            return err
        storage = svc().storage
        scan_id = request.args.get("scan_id")
        if not scan_id:
            idx = storage.list_snapshot_index(uid, audit_id)
            scan_id = idx[0]["scan_id"] if idx else None
        if not scan_id:
            return jsonify({"status": "none"})
        rec = storage.get_recommendation(uid, audit_id, scan_id)
        if not rec:
            return jsonify({"status": "none", "scan_id": scan_id})
        st = rec.get("status")
        if st == "success":
            return jsonify({"status": "ready", "recommendation": rec, "scan_id": scan_id})
        if st == "generating":
            return jsonify({"status": "generating", "scan_id": scan_id})
        return jsonify({"status": st or "error", "message": rec.get("message"), "scan_id": scan_id})

    # ── remediation approval + (gated) execution ────────────────────────────
    @bp.route(f"/{prefix}/audit/<audit_id>/finding/<path:finding_id>/remediation", methods=["POST"])
    @permission_required_body(perms["remediation"])
    def request_remediation(audit_id, finding_id):
        from cspm_core.remediation import request_remediation as _req
        uid, err = need_user()
        if err:
            return err
        snap = svc().storage.get_latest_snapshot(uid, audit_id)
        if not snap:
            return jsonify({"status": "error", "message": "No scan found"}), 404
        f = next((x for x in (snap.get("findings") or []) if x.get("finding_id") == finding_id), None)
        if not f:
            return jsonify({"status": "error", "message": "Finding not found"}), 404
        res = _req(P, uid, audit_id, f)
        code = {"created": 201, "exists": 200, "no_org": 409, "not_found": 404, "error": 502}.get(res.get("status"), 200)
        return jsonify(res), code

    @bp.route(f"/{prefix}/audit/<audit_id>/finding/<path:finding_id>/remediate", methods=["POST"])
    @permission_required_body(perms["remediation"])
    def remediate(audit_id, finding_id):
        from cspm_core.autoremediate import execute_remediation
        uid, err = need_user()
        if err:
            return err
        snap = svc().storage.get_latest_snapshot(uid, audit_id)
        if not snap:
            return jsonify({"status": "error", "message": "No scan found"}), 404
        f = next((x for x in (snap.get("findings") or []) if x.get("finding_id") == finding_id), None)
        if not f:
            return jsonify({"status": "error", "message": "Finding not found"}), 404
        dry = bool((request.get_json(silent=True) or {}).get("dry_run", True))
        res = execute_remediation(P, uid, audit_id, f, dry_run=dry)
        code = {"executed": 200, "planned": 200, "disabled": 409, "not_approved": 409,
                "unsupported": 422, "error": 502}.get(res.get("status"), 200)
        return jsonify(res), code

    @bp.route(f"/{prefix}/audit/<audit_id>/remediations", methods=["GET"])
    @permission_required_body(perms["findings_read"])
    def list_remediations(audit_id):
        from cspm_core.remediation import list_remediations as _list
        uid, err = need_user()
        if err:
            return err
        return jsonify({"status": "success", "remediations": _list(P, uid, audit_id)})

    # ── exports: evidence / tracker / runbook push ──────────────────────────
    @bp.route(f"/{prefix}/audit/<audit_id>/evidence", methods=["GET"])
    @permission_required_body(perms["findings_read"])
    def evidence(audit_id):
        from cspm_core.exports import TABLES, load_and_build
        uid, err = need_user()
        if err:
            return err
        table = request.args.get("table", "findings")
        if table not in TABLES:
            return jsonify({"status": "error", "message": f"Unknown table: {table}"}), 400
        built = load_and_build(P, uid, audit_id, table, request.args.get("framework"))
        if built is None:
            return jsonify({"status": "error", "message": "No scan found"}), 404
        recs = built["evidence"]
        return jsonify({"status": "success", "evidence": {"table": table, "name": built["name"],
                                                          "records": recs, "total_findings": len(recs)}})

    @bp.route(f"/{prefix}/audit/<audit_id>/to-tracker", methods=["POST"])
    @permission_required_body("trackers.table.create")
    def to_tracker(audit_id):
        from tab_tracker.helper import (_decrypt_tracker_data, append_to_tracker, check_config_exist,
                                        create_empty_tracker_config, create_tracker_config, create_tracker_file,
                                        save_tracker_file)
        from utils.s3_utils import read_json_from_s3
        from cspm_core.exports import TABLES, load_and_build
        uid, err = need_user()
        if err:
            return err
        body = request.get_json(silent=True) or {}
        table = body.get("table", "findings")
        if table not in TABLES:
            return jsonify({"status": "error", "message": f"Unknown table: {table}"}), 400
        built = load_and_build(P, uid, audit_id, table, body.get("framework"))
        if built is None:
            return jsonify({"status": "error", "message": "No scan found"}), 404
        name = (body.get("name") or "").strip() or f"{built['name']} — {audit_id[:6]}"
        block_id = f"{P.key}-audit-{audit_id}-{table}"
        try:
            config_path, config_data = check_config_exist(uid)
            if not config_data:
                create_empty_tracker_config(uid)
                config_path, config_data = check_config_exist(uid)
            tracker_id, file_path = create_tracker_config(config_path=config_path, user_id=uid, name=name,
                                                          tracker_type="table", runbook_id=f"{P.key}-audit:{audit_id}",
                                                          block_id=block_id)
            create_tracker_file(user_id=uid, tracker_id=tracker_id, tracker_type="table",
                                runbook_id=f"{P.key}-audit:{audit_id}", block_config={"columns": built["columns"]})
            tracker_data = read_json_from_s3(file_path)
            if tracker_data:
                tracker_data, _ = _decrypt_tracker_data(uid, tracker_data)
                block = {"block_id": block_id, "block_title": name,
                         "headers": [c["name"] for c in built["columns"]], "rows": built["rows"]}
                append_to_tracker(tracker_data, block, block_id)
                save_tracker_file(uid, tracker_id, tracker_data)
        except ValueError as ve:
            return jsonify({"status": "error", "message": str(ve)}), 400
        except Exception as exc:
            logger.warning("%s to_tracker failed: %s", P.key, exc, exc_info=True)
            return jsonify({"status": "error", "message": str(exc)}), 502
        return jsonify({"status": "success", "tracker_id": tracker_id, "name": name, "rows": len(built["rows"])}), 201

    @bp.route(f"/{prefix}/runbooks", methods=["GET"])
    @permission_required_body("compliance.runbook.read")
    def runbooks():
        from cspm_core.runbook_evidence import list_runbooks
        uid, err = need_user()
        if err:
            return err
        return jsonify({"status": "success", "runbooks": _run_async(list_runbooks(uid))})

    @bp.route(f"/{prefix}/audit/<audit_id>/push-to-runbook", methods=["POST"])
    @permission_required_body("compliance.runbook.edit")
    def push_to_runbook(audit_id):
        from cspm_core.exports import TABLES, load_and_build
        from cspm_core.runbook_evidence import push_evidence_to_runbook
        uid, err = need_user()
        if err:
            return err
        body = request.get_json(silent=True) or {}
        runbook_id = body.get("runbook_id")
        if not runbook_id:
            return jsonify({"status": "error", "message": "Missing runbook_id"}), 400
        table = body.get("table", "findings")
        if table not in TABLES:
            return jsonify({"status": "error", "message": f"Unknown table: {table}"}), 400
        built = load_and_build(P, uid, audit_id, table, body.get("framework"))
        if built is None:
            return jsonify({"status": "error", "message": "No scan found"}), 404
        res = _run_async(push_evidence_to_runbook(uid, runbook_id, built["evidence"],
                                                  f"{P.label} {built['name']}", source=P.key))
        code = {"queued": 200, "empty": 200, "not_found": 404, "not_linked": 409, "error": 502}.get(res.get("status"), 200)
        return jsonify(res), code

    return bp
