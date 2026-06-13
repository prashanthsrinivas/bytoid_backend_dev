import json
import os
import uuid
import base64
import traceback
import logging
from flask import Blueprint, jsonify, request, g
from utils.normal import parse_composite_user_id
from utils.permission_required import permission_required_body
from db.lance_db_service import LanceDBServer
from db.db_checkers import get_email_by_id
from services.audit_log_service import (
    build_audit_actor,
    log_audit_event,
    EVIDENCE_CONFIG_ADDED,
    EVIDENCE_CONFIG_UPDATED,
    EVIDENCE_CONFIG_DELETED,
)
from playbook.background_worker import JobManager
from services.redis_service import RedisService
from utils.s3_utils import save_any_s3, read_json_from_s3, s3bucket, S3_BUCKET
from utils.app_configs import ACCESSIBLE_IDS
from config_evidences.evidence_helpers import (
    _load_default_evidence,
    _get_user_evidence,
    _save_user_evidence,
    _update_entry_by_id,
    _delete_entry_by_id,
    _validate_evidence_entry,
    _add_entry,
    run_evidence_check_job,
    VALID_RESPONSE_POLICIES,
)

config_evidences_bp = Blueprint("config_evidences", __name__)
logger = logging.getLogger(__name__)
dbserver = LanceDBServer()


# ============================================================
# Evidence CRUD Routes
# ============================================================
@config_evidences_bp.route("/runbook/evidence/config", methods=["GET"])
@permission_required_body("evidence.view")
def get_evidence_config():
    try:
        user_id = request.args.get("user_id")
        if not user_id:
            return jsonify({"error": "user_id is required"}), 400

        is_super_admin = user_id in ACCESSIBLE_IDS

        if is_super_admin:
            evidence = _load_default_evidence()
            is_custom = False
        else:
            evidence, is_custom = _get_user_evidence(user_id)

        return (
            jsonify(
                {"status": "success", "evidence": evidence, "is_custom": is_custom}
            ),
            200,
        )

    except Exception as e:
        logger.error(f"Error in get_evidence_config: {e}", exc_info=True)
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


# ============================================================
# Evidence Configure + Check Routes
# ============================================================
@config_evidences_bp.route("/runbook_evidence_config", methods=["GET"])
@permission_required_body("evidence.view")
async def get_runbook_evidence_config():
    try:
        user_id = request.args.get("user_id")
        runbook_id = request.args.get("runbook_id")

        if not all([user_id, runbook_id]):
            return jsonify({"error": "user_id and runbook_id are required"}), 400

        runbook_list = await dbserver.get_runbook_by_id(user_id, runbook_id)
        if not runbook_list:
            return jsonify({"error": "Runbook not found"}), 404

        runbook = runbook_list[0]
        raw_config = runbook.get("runbook_evidence_config", "") or ""
        evidence_config = json.loads(raw_config) if raw_config else {}

        return (
            jsonify(evidence_config),
            200,
        )

    except Exception as e:
        logger.error(f"Error in get_runbook_evidence_config: {e}", exc_info=True)
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@config_evidences_bp.route("/runbook/evidence/config", methods=["POST"])
@permission_required_body("evidence.edit")
def update_evidence_config():

    try:
        data = request.get_json()
        user_id = data.get("user_id")
        entry_id = data.get("id")
        expectations = data.get("expectations")
        response_policy = data.get("responsePolicy")

        if not user_id or not entry_id:
            return (
                jsonify({"error": "user_id and id are required"}),
                400,
            )
        if expectations is None and response_policy is None:
            return (
                jsonify(
                    {"error": "at least one of expectations or responsePolicy is required"}
                ),
                400,
            )
        if response_policy is not None and response_policy not in VALID_RESPONSE_POLICIES:
            return (
                jsonify(
                    {
                        "error": "invalid responsePolicy; must be one of "
                        + ", ".join(VALID_RESPONSE_POLICIES)
                    }
                ),
                400,
            )

        is_super_admin = user_id in ACCESSIBLE_IDS

        if is_super_admin:
            evidence = _load_default_evidence()
            updated = _update_entry_by_id(
                evidence, entry_id, expectations, response_policy
            )

            file_path = os.path.join(os.path.dirname(__file__), "evidence_default.json")
            with open(file_path, "w") as f:
                json.dump(updated, f, indent=2)

        else:
            evidence, _ = _get_user_evidence(user_id)
            updated = _update_entry_by_id(
                evidence, entry_id, expectations, response_policy
            )
            result = _save_user_evidence(user_id, updated)

            if result.get("status") != "success":
                return jsonify(result), 500

        actor_email = get_email_by_id(user_id)
        log_audit_event(
            action=EVIDENCE_CONFIG_UPDATED,
            endpoint="/runbook/evidence/config",
            ip=request.remote_addr,
            status="success",
            actor_user_id=user_id,
            actor_email=actor_email,
            metadata={"entry_id": entry_id, "responsePolicy": response_policy},
        )
        g.audit_logged = True

        return (
            jsonify(
                {
                    "status": "success",
                    "message": "Entry updated",
                    "updated": {
                        "id": entry_id,
                        "expectations": expectations,
                        "responsePolicy": response_policy,
                    },
                }
            ),
            200,
        )

    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.error(f"Error in update_evidence_config: {e}", exc_info=True)
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@config_evidences_bp.route("/runbook/evidence/config", methods=["DELETE"])
@permission_required_body("evidence.delete")
def delete_evidence_config():
    try:
        data = request.get_json() or {}
        user_id = data.get("user_id")
        entry_id = data.get("id")

        if not all([user_id, entry_id]):
            return jsonify({"error": "user_id and id are required"}), 400

        is_super_admin = user_id in ACCESSIBLE_IDS

        if is_super_admin:
            evidence = _load_default_evidence()
            updated, deleted_entry = _delete_entry_by_id(evidence, entry_id)

            file_path = os.path.join(os.path.dirname(__file__), "evidence_default.json")
            with open(file_path, "w") as f:
                json.dump(updated, f, indent=2)

        else:
            evidence, _ = _get_user_evidence(user_id)
            updated, deleted_entry = _delete_entry_by_id(evidence, entry_id)
            result = _save_user_evidence(user_id, updated)

            if result.get("status") != "success":
                return jsonify(result), 500

        actor_email = get_email_by_id(user_id)
        log_audit_event(
            action=EVIDENCE_CONFIG_DELETED,
            endpoint="/runbook/evidence/config",
            ip=request.remote_addr,
            status="success",
            actor_user_id=user_id,
            actor_email=actor_email,
            metadata={"entry_id": entry_id, "artifact": deleted_entry.get("artifact")},
        )
        g.audit_logged = True

        return (
            jsonify(
                {
                    "status": "success",
                    "message": "Entry deleted",
                    "deleted": {
                        "id": entry_id,
                        "artifact": deleted_entry.get("artifact"),
                    },
                }
            ),
            200,
        )

    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.error(f"Error in delete_evidence_config: {e}", exc_info=True)
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@config_evidences_bp.route("/runbook/evidence/add", methods=["POST"])
@permission_required_body("evidence.create")
def add_evidence_entry():
    try:
        data = request.get_json()
        baseuser = data.get("user_id")

        if not baseuser:
            return jsonify({"error": "user_id is required"}), 400
        logged_in_user_id, user_id = parse_composite_user_id(baseuser)

        entry_data = {
            "type": data.get("type"),
            "number": data.get("number"),
            "artifact": data.get("artifact"),
            "nature": data.get("nature"),
            "primaryUse": data.get("primaryUse"),
            "expectations": data.get("expectations"),
        }
        if data.get("responsePolicy") is not None:
            entry_data["responsePolicy"] = data.get("responsePolicy")

        try:
            _validate_evidence_entry(entry_data)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        is_super_admin = user_id in ACCESSIBLE_IDS

        if is_super_admin:
            evidence = _load_default_evidence()
            updated, new_entry = _add_entry(evidence, entry_data)

            file_path = os.path.join(os.path.dirname(__file__), "evidence_default.json")
            with open(file_path, "w") as f:
                json.dump(updated, f, indent=2)

        else:
            evidence, _ = _get_user_evidence(user_id)
            updated, new_entry = _add_entry(evidence, entry_data)
            result = _save_user_evidence(user_id, updated)

            if result.get("status") != "success":
                return jsonify(result), 500

        actor_uid, actor_email, behalf_uid, behalf_email = build_audit_actor(baseuser)
        log_audit_event(
            action=EVIDENCE_CONFIG_ADDED,
            endpoint="/runbook/evidence/add",
            ip=request.remote_addr,
            status="success",
            actor_user_id=actor_uid,
            actor_email=actor_email,
            acting_on_behalf_of_email=behalf_email,
            acting_on_behalf_of_user_id=behalf_uid,
            metadata={
                "evidence_type": entry_data.get("type"),
                "artifact": entry_data.get("artifact"),
            },
        )
        g.audit_logged = True

        return (
            jsonify(
                {"status": "success", "message": "Entry added", "entry": new_entry}
            ),
            201,
        )

    except Exception as e:
        logger.error(f"Error in add_evidence_entry: {e}", exc_info=True)
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


# ============================================================
# Evidence Configure + Check Routes
# ============================================================
@config_evidences_bp.route("/runbook_evidence_configure", methods=["POST"])
@permission_required_body("evidence.edit")
async def runbook_evidence_configure():
    try:
        data = request.get_json()
        user_id = data.get("user_id")
        runbook_id = data.get("runbook_id")
        configurations_list = data.get("configurations_list")

        if not all([user_id, runbook_id, configurations_list]):
            return (
                jsonify(
                    {
                        "error": "user_id, runbook_id, and configurations_list are required"
                    }
                ),
                400,
            )
        logged_in_user_id, user_id = parse_composite_user_id(user_id)

        await dbserver.update_runbook(
            user_id,
            runbook_id,
            {"runbook_evidence_config": json.dumps(configurations_list)},
        )

        return jsonify({"status": "success", "runbook_id": runbook_id}), 200

    except Exception as e:
        logger.error(f"Error in runbook_evidence_configure: {e}", exc_info=True)
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@config_evidences_bp.route("/evidence_check", methods=["POST"])
@permission_required_body("evidence.execute")
async def evidence_check():
    try:
        user_id = request.form.get("user_id")
        logged_in_user_id, user_id = parse_composite_user_id(user_id)
        runbook_id = request.form.get("runbook_id")
        file = request.files.get("file")

        if not all([user_id, runbook_id, file]):
            return jsonify({"error": "user_id, runbook_id, and file are required"}), 400

        file_data = {
            "filename": file.filename,
            "content_type": file.content_type,
            "data_base64": base64.b64encode(file.read()).decode("utf-8"),
        }

        data = {"user_id": user_id, "runbook_id": runbook_id, "file": file_data}

        job_id = await JobManager.submit_job(run_evidence_check_job, data)

        return jsonify({"job_id": job_id, "status": "queued"}), 200

    except Exception as e:
        logger.error(f"Error in evidence_check: {e}", exc_info=True)
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500
