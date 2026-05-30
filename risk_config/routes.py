"""Per-org risk-calculation config endpoints.

Lets an admin view and edit how report risk is scored: the Impact/Likelihood scales
(Risk Score = Impact x Likelihood), the aggregation method, and the score->level bands.
Config is stored as JSON on the owner/admin's `users` row and consumed by the runbook
risk engine (`runbook/risk_engine.py`).
"""

import json

import pymysql
from flask import Blueprint, g, jsonify, request

from db.rds_db import connect_to_rds
from runbook.risk_engine import (
    DEFAULT_RISK_CONFIG,
    ensure_risk_config_column,
    get_risk_config,
    validate_risk_config,
)
from utils.base_logger import get_logger
from utils.normal import parse_composite_user_id
from utils.permission_required import permission_required_body

risk_config_bp = Blueprint("risk_config", __name__)
logger = get_logger(__name__)

# Keys an admin is allowed to set; anything else in the body is ignored.
_ALLOWED_KEYS = {"impact_scale", "likelihood_scale", "formula", "aggregation", "bands"}


def _owner_id_from_request(supplied):
    """Resolve the owner/org id to read/write config for.

    Prefer the org the decorator already resolved (g.acting_on_behalf_of_user_id),
    falling back to the right side of a composite id, then the plain id.
    """
    behalf = getattr(g, "acting_on_behalf_of_user_id", None)
    if behalf:
        return behalf
    _, owner = parse_composite_user_id(supplied)
    return owner or supplied


@risk_config_bp.route("/risk-config", methods=["GET"])
@permission_required_body("compliance.runbook.read")
def get_risk_config_endpoint():
    supplied = request.args.get("user_id") or request.args.get("userid")
    if not supplied:
        return jsonify({"error": "user_id is required"}), 400
    owner_id = _owner_id_from_request(supplied)
    config = get_risk_config(owner_id)
    return jsonify({"status": "ok", "config": config, "defaults": DEFAULT_RISK_CONFIG})


@risk_config_bp.route("/risk-config", methods=["POST"])
@permission_required_body("admin.manage_admins")
def update_risk_config_endpoint():
    data = request.get_json(silent=True) or {}
    supplied = data.get("user_id") or data.get("userid")
    if not supplied:
        return jsonify({"error": "user_id is required"}), 400
    owner_id = _owner_id_from_request(supplied)

    # Accept either a nested "config" object or the fields at the top level.
    incoming = data.get("config") if isinstance(data.get("config"), dict) else data
    # Start from the org's current effective config so partial updates are safe.
    config = get_risk_config(owner_id)
    for key in _ALLOWED_KEYS:
        if key in incoming and incoming[key] is not None:
            config[key] = incoming[key]

    ok, err = validate_risk_config(config)
    if not ok:
        return jsonify({"error": err}), 400

    # Self-heal an unmigrated DB (code deployed, migration not yet run) so the save
    # doesn't 500 on a missing column. No-op once the column is confirmed present.
    ensure_risk_config_column()

    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                "UPDATE users SET risk_config=%s WHERE user_id=%s",
                (json.dumps(config), owner_id),
            )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("update_risk_config failed: %s", e, exc_info=True)
        return jsonify({"error": "Failed to save risk config"}), 500

    return jsonify({"status": "ok", "config": config})
