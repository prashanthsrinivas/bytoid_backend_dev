import logging
import os
import traceback
import json

from db.lance_db_service import LanceDBServer
from flask import Blueprint, jsonify, request, session
from services.redis_service import RedisService
from tab_tracker.helper import (
    check_config_exist,
    create_empty_tracker_config,
    create_tracker_config,
    create_tracker_file,
    delete_tracker_config,
)
from utils.s3_utils import upload_any_file, read_json_from_s3


tracker_bp = Blueprint("tracker", __name__)
dbserver = LanceDBServer()


def extract_block_schema(block, tracker_type):
    """Extract schema from runbook block based on tracker type."""
    if not block or "micro_blocks" not in block:
        return None

    micro_blocks = block.get("micro_blocks", [])
    if not micro_blocks:
        return None

    micro_block = micro_blocks[0]

    # ✅ TABLE: extract columns from table_schema
    if tracker_type == "table":
        if micro_block.get("type") == "table_schema":
            return {"columns": micro_block.get("columns", [])}

    # ✅ MATRIX: extract axes from matrix_schema
    elif tracker_type == "matrix":
        if micro_block.get("type") == "matrix_schema":
            return {
                "x_axis": micro_block.get("x_axis", {}),
                "y_axis": micro_block.get("y_axis", {}),
                "cell_value": micro_block.get("cell_value", "Value"),
            }

    # ✅ SCORECARD: extract metrics from scorecard_schema
    elif tracker_type == "scorecard":
        if micro_block.get("type") == "scorecard_schema":
            return {"metrics": micro_block.get("metrics", [])}

    return None


@tracker_bp.route("/tracker/create", methods=["POST"])
async def create_tracker_api():
    try:
        data = request.json

        user_id = str(data.get("user_id"))
        name = data.get("name")
        tracker_type = data.get("type")  # table | matrix | scorecard
        runbook_id = data.get("runbook_id")
        block_id = data.get("block_id")  # Required: which block to use as template

        if not all([user_id, name, tracker_type, runbook_id, block_id]):
            return (
                jsonify(
                    {
                        "error": "Missing required fields: user_id, name, type, runbook_id, block_id"
                    }
                ),
                400,
            )

        # 🔹 STEP 1: FETCH RUNBOOK AND GET BLOCK SCHEMA
        runbook = await dbserver.get_runbook_by_id(user_id=user_id,runbook_id=runbook_id)

        if not runbook:
            return jsonify({"error": f"Runbook not found: {runbook_id}"}), 404
        
        if isinstance(runbook, list):
            if not runbook:
                return jsonify({"error": f"Runbook not found: {runbook_id}"}), 404
            runbook = runbook[0]

        # Find the specified block in the runbook
        raw = runbook.get("structure_theme")

        if not raw:
            return jsonify({"error": "Missing structure_theme"}), 400

        # Step 1: First parse
        try:
            parsed = json.loads(raw)
        except Exception:
            return jsonify({"error": "Invalid JSON (level 1)"}), 500

        # Step 2: If still string → parse again
        if isinstance(parsed, str):
            try:
                parsed = json.loads(parsed)
            except Exception:
                return jsonify({"error": "Invalid JSON (level 2)"}), 500

        # Step 3: Handle dict OR list
        if isinstance(parsed, dict):
            blocks = parsed.get("blocks", [])
        elif isinstance(parsed, list):
            blocks = parsed
        else:
            return jsonify({"error": "Invalid structure_theme format"}), 400

        # Validate blocks
        if not isinstance(blocks, list):
            return jsonify({"error": "blocks should be a list"}), 400
        # blocks = structure_theme.get("blocks", [])
        if not blocks:
            return jsonify({"error": "No blocks found in runbook"}), 400
        target_block = None

        for block in blocks:
            if isinstance(block, dict)  and block.get("block_id") == block_id:
                target_block = block
                break

        if not target_block:
            return jsonify({"error": f"Block not found in runbook: {block_id}"}), 404

        # Extract schema from the block
        block_config = extract_block_schema(target_block, tracker_type)

        if not block_config:
            return (
                jsonify(
                    {
                        "error": f"Block '{block_id}' does not have a valid schema for tracker type '{tracker_type}'"
                    }
                ),
                400,
            )

        # 🔹 STEP 2: CHECK/CREATE CONFIG
        config_path, config_data = check_config_exist(user_id)
        config_created = False

        if not config_data:
            create_empty_tracker_config(user_id)
            config_created = True
            config_path, config_data = check_config_exist(user_id)

        # # 🔹 STEP 3: CREATE CONFIG ENTRY (returns tracker_id + file_path)
        tracker_id, file_path = create_tracker_config(
            config_path=config_path,
            user_id=user_id,
            name=name,
            tracker_type=tracker_type,
            runbook_id=runbook_id,
        )

        # # 🔹 STEP 4: CREATE TRACKER FILE WITH SCHEMA
        create_tracker_file(
            user_id=user_id,
            tracker_id=tracker_id,
            tracker_type=tracker_type,
            runbook_id=runbook_id,
            block_config=block_config,
        )

        return (
            jsonify(
                {
                    "message": "Tracker created successfully",
                    "tracker_id": tracker_id,
                    "file_path": file_path,
                    "block_id": block_id,
                    "config_created": config_created,
                    "config_path": config_path,
                    "block_config": block_config
                }
            ),
            201,
        )

    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400

    except Exception as e:
        logging.error(f"Tracker creation error: {traceback.format_exc()}")
        return jsonify({"error": str(e),
                        "trace":traceback.format_exc()}), 500

@tracker_bp.route("/tracker/delete",methods=["DELETE"])
def delete_tracker():
    try:
        data = request.get_json()
        user_id = data.get("user_id")
        config_path = data.get("config_path")
        tracker_id = data.get("tracker_id")
        res = delete_tracker_config(user_id=user_id,
                                    config_path=config_path,
                                    tracker_id=tracker_id)

        return jsonify({"message":"deleted successfully",
                        "success":res}),200

    except Exception as e:
        logging.error(f"Tracker creation error: {traceback.format_exc()}")
        return jsonify({"error": str(e),
                        "trace":traceback.format_exc()}), 500


@tracker_bp.route("/tracker/list", methods=["GET"])
def list_trackers_api():
    try:
        user_id = str(request.args.get("user_id"))

        if not user_id:
            return jsonify({"error": "Missing required parameter: user_id"}), 400

        # Fetch tracker config
        config_path, config_data = check_config_exist(user_id)

        if not config_data:
            return jsonify({
                "user_id": user_id,
                "trackers": [],
                "count": 0,
                "message": "No tracker config found for this user"
            }), 200

        trackers = config_data.get("trackers", [])

        return jsonify({
            "user_id": user_id,
            "trackers": trackers,
            "count": len(trackers)
        }), 200

    except Exception as e:
        logging.error(f"List trackers error: {traceback.format_exc()}")
        return jsonify({"error": str(e),
                        "trace": traceback.format_exc()}), 500


@tracker_bp.route("/tracker/details", methods=["GET"])
def get_tracker_details_api():
    try:
        user_id = str(request.args.get("user_id"))
        tracker_id = request.args.get("tracker_id")

        if not all([user_id, tracker_id]):
            return jsonify({"error": "Missing required parameters: user_id, tracker_id"}), 400

        # Look up tracker metadata from config first
        config_path, config_data = check_config_exist(user_id)
        tracker_meta = None

        if config_data:
            for t in config_data.get("trackers", []):
                if t.get("tracker_id") == tracker_id:
                    tracker_meta = t
                    break

        if not tracker_meta:
            return jsonify({"error": f"Tracker not found in config: {tracker_id}"}), 404

        # Use file_path from config (source of truth)
        tracker_path = tracker_meta.get("file_path", f"{user_id}/tracker/{tracker_id}/tracker.json")

        # Fetch tracker data from S3
        tracker_data = read_json_from_s3(tracker_path)

        if not tracker_data:
            return jsonify({
                "error": "Tracker file not initialized. Please recreate the tracker.",
                "tracker_id": tracker_id,
                "tracker_meta": tracker_meta
            }), 404

        tracker_type = tracker_data.get("type")

        data_count = {"source_blocks": len(tracker_data.get("source_blocks", []))}
        if tracker_type == "table":
            data_count["rows"] = len(tracker_data.get("rows", []))
        elif tracker_type == "matrix":
            data_count["cells"] = len(tracker_data.get("cells", []))
        elif tracker_type == "scorecard":
            data_count["records"] = len(tracker_data.get("records", []))

        return jsonify({
            "user_id": user_id,
            "tracker_id": tracker_id,
            "tracker_path": tracker_path,
            "type": tracker_type,
            "schema": tracker_data.get("schema", {}),
            "data_count": data_count,
            "tracker": tracker_data,
        }), 200

    except Exception as e:
        logging.error(f"Get tracker details error: {traceback.format_exc()}")
        return jsonify({"error": str(e),
                        "trace": traceback.format_exc()}), 500

