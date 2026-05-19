import logging
import os
import traceback
import json
import uuid
import re

import pymysql

from utils.normal import parse_composite_user_id
from utils.app_configs import IS_DEV, FRAMEWORK_OWNER
from db.lance_db_service import LanceDBServer
from db.rds_db import connect_to_rds
from flask import Blueprint, jsonify, request, g
from utils.permission_required import permission_required_body
from services.audit_log_service import (
    log_audit_event,
    build_audit_actor,
    TRACKER_CREATED,
    TRACKER_DELETED,
    TRACKER_MODIFIED,
    TRACKER_ENTRY_ADDED,
    TRACKER_COLUMN_ADDED,
    TRACKER_COLUMN_DELETED,
    TRACKER_EVIDENCE_UPLOADED,
    TRACKER_FRAMEWORK_ADDED,
    TRACKER_FRAMEWORK_UPDATED,
    TRACKER_FRAMEWORK_REMOVED,
    TRACKER_SHARED,
    TRACKER_SHARE_REVOKED,
)
from db.db_checkers import get_email_by_id
from shared_configuration import (
    check_role_has_permission,
    core_assign_resource,
    core_list_resource_shares,
    core_revoke_resource,
    get_round_robin_user_for_resource,
    get_user_resource_access,
    get_user_shared_resources,
)
from tab_tracker.helper import (
    check_config_exist,
    create_empty_tracker_config,
    create_tracker_config,
    create_tracker_file,
    append_to_tracker,
    save_tracker_file,
    ensure_tracker_file_exists,
    apply_entry_updates,
    _rebuild_micro_blocks_from_tracker,
)
from utils.s3_utils import (
    upload_any_file,
    read_json_from_s3,
    delete_file_from_s3,
    load_yaml_from_s3,
    list_all_files,
)
from utils.fireworkzz import (
    analyze_tracker_framework_policies,
    analyze_tracker_framework_rows,
    quality_review_framework_assignments,
)
from credits_route.route import Credits
from playbook.background_worker import JobManager
from services.redis_service import get_redis
from websockets_custom.ws_instance import ws_service, msg_builder_main
from utils.base_logger import get_logger

tracker_bp = Blueprint("tracker", __name__)
dbserver = LanceDBServer()
logger = get_logger(__name__, log_level="DEBUG" if IS_DEV else "INFO")


def _check_tracker_share_access(baseuser, tracker_id):
    """
    Resolve owner and ensure the requester has access.

    Returns (owner_user_id, error_tuple). When error_tuple is non-None, the
    caller should return it directly (jsonify, status).
    """
    logged_in_user_id, owner_id = parse_composite_user_id(baseuser)
    if not owner_id:
        return None, (jsonify({"error": "Invalid user_id"}), 400)

    if not logged_in_user_id or logged_in_user_id == owner_id:
        return owner_id, None

    access = get_user_resource_access("tracker", owner_id, tracker_id, logged_in_user_id)
    if not access.get("granted"):
        return None, (
            jsonify({"error": "Access to this tracker has not been granted"}),
            403,
        )
    return owner_id, None


def extract_block_schema(block, tracker_type):
    """Extract schema from runbook block based on tracker type."""
    if not block or "micro_blocks" not in block:
        return None

    micro_blocks = block.get("micro_blocks", [])
    if not micro_blocks:
        return None

    micro_block = micro_blocks[0]
    if not isinstance(micro_block, dict):
        return None

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
@permission_required_body("trackers.table.create")
async def create_tracker_api():
    try:
        data = request.json

        baseuser = str(data.get("user_id"))
        name = data.get("name")
        tracker_type = data.get("type")  # table | matrix | scorecard
        runbook_id = data.get("runbook_id")
        block_id = data.get("block_id")  # Required: which block to use as template
        result_id = data.get("result_id")  # Optional: if provided, populate with data

        if not all([baseuser, name, tracker_type, runbook_id, block_id]):
            return (
                jsonify(
                    {
                        "error": "Missing required fields: user_id, name, type, runbook_id, block_id"
                    }
                ),
                400,
            )
        logged_in_user_id, user_id = parse_composite_user_id(baseuser)

        # 🔹 STEP 1: FETCH RUNBOOK AND GET BLOCK SCHEMA
        runbook = await dbserver.get_runbook_by_id(
            user_id=user_id, runbook_id=runbook_id
        )

        if not runbook:
            return jsonify({"error": f"Runbook not found: {runbook_id}"}), 404

        if isinstance(runbook, list):
            runbook = runbook[0]

        # Find the specified block in the runbook
        raw = runbook.get("structure_theme")

        if not raw:
            return jsonify({"error": "Missing structure_theme"}), 400

        # Step 1: Parse if string (handle single or double-encoded JSON)
        if isinstance(raw, (dict, list)):
            parsed = raw
        else:
            try:
                parsed = json.loads(raw)
            except Exception:
                return jsonify({"error": "Invalid JSON in structure_theme"}), 400

        # Step 2: If still string → parse again (double-encoded case)
        if isinstance(parsed, str):
            try:
                parsed = json.loads(parsed)
            except Exception:
                return jsonify({"error": "Invalid JSON in structure_theme (nested)"}), 400

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
            if isinstance(block, dict) and block.get("block_id") == block_id:
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
            block_id=block_id,
        )

        # # 🔹 STEP 4: CREATE TRACKER FILE WITH SCHEMA
        create_tracker_file(
            user_id=user_id,
            tracker_id=tracker_id,
            tracker_type=tracker_type,
            runbook_id=runbook_id,
            block_config=block_config,
        )

        # Read the newly created tracker
        tracker_data = read_json_from_s3(file_path)
        data_appended = {"rows": 0, "cells": 0, "records": 0}

        # 🔹 STEP 5: OPTIONAL - APPEND DATA IF RESULT_ID PROVIDED
        if result_id:
            try:
                # Fetch the runbook result
                result_row = await dbserver.runbook_get_result(
                    user_id=user_id, result_id=result_id
                )

                if (
                    result_row
                    and result_row.get("status") != "not_found"
                    and result_row.get("status") != "running"
                ):
                    result_content = result_row.get("result")

                    if result_content:
                        # Find the block in the result
                        result_blocks = result_content.get("blocks", [])
                        result_block = None

                        for b in result_blocks:
                            if b.get("block_id") == block_id:
                                result_block = b
                                break

                        if result_block:
                            # Ensure block_title
                            if not result_block.get("block_title"):
                                result_block["block_title"] = name

                            # Record before state
                            before = {
                                "rows": len(tracker_data.get("rows", [])),
                                "cells": len(tracker_data.get("cells", [])),
                                "records": len(tracker_data.get("records", [])),
                            }

                            # Append data and capture metadata
                            append_metadata = append_to_tracker(
                                tracker_data, result_block, result_id
                            )

                            # Record after state
                            after = {
                                "rows": len(tracker_data.get("rows", [])),
                                "cells": len(tracker_data.get("cells", [])),
                                "records": len(tracker_data.get("records", [])),
                            }

                            data_appended = {k: after[k] - before[k] for k in before}

                            # Save updated tracker
                            save_tracker_file(user_id, tracker_id, tracker_data)

                            logging.info(
                                f"Tracker {tracker_id} created with {data_appended} data from result {result_id}"
                            )
                        else:
                            logging.warning(
                                f"Block {block_id} not found in result {result_id}"
                            )
                    else:
                        logging.warning(f"Result {result_id} has no content")
                else:
                    logging.warning(f"Result {result_id} not found or still running")
            except Exception as e:
                logging.error(
                    f"Failed to append data during tracker creation: {str(e)}"
                )
                # Don't fail the tracker creation, just log the error

        updates_runbook = {"tracker_configuration": {block_id: tracker_id}}
        update = await dbserver.update_runbook(
            user_id=user_id, runbook_id=runbook_id, updates=updates_runbook
        )
        print("tracker_configuration updated")
        if not update:
            return (
                jsonify(
                    {"error": f"Not able to update tracker_id in runbook: {runbook_id}"}
                ),
                404,
            )

        response_data = {
            "message": "Tracker created successfully",
            "tracker_id": tracker_id,
        }

        # Include data appended information if result_id was provided
        if result_id:
            response_data["data_appended"] = data_appended
            response_data["result_id"] = result_id

        # Audit logging
        (
            actor_user_id,
            actor_email,
            acting_on_behalf_of_user_id,
            acting_on_behalf_of_email,
        ) = build_audit_actor(baseuser)
        log_audit_event(
            action=TRACKER_CREATED,
            endpoint="/tracker/create",
            ip=request.remote_addr,
            status="success",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=acting_on_behalf_of_user_id,
            acting_on_behalf_of_email=acting_on_behalf_of_email,
            metadata={
                "tracker_id": tracker_id,
                "tracker_name": name,
                "tracker_type": tracker_type,
                "runbook_id": runbook_id,
            },
        )
        g.audit_logged = True

        return jsonify(response_data), 200

    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400

    except Exception as e:
        logging.error(f"Tracker creation error: {traceback.format_exc()}")
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@tracker_bp.route("/tracker/delete", methods=["DELETE"])
@permission_required_body("trackers.table.delete")
async def delete_tracker():
    """
    Delete a tracker: removes it from the config, deletes its S3 file,
    and removes the block_id entry from the linked runbook's tracker_configuration.

    Body: { user_id, tracker_id }
    """
    try:
        data = request.get_json()
        baseuser = str(data.get("user_id"))
        tracker_id = data.get("tracker_id")

        if not all([baseuser, tracker_id]):
            return (
                jsonify({"error": "Missing required fields: user_id, tracker_id"}),
                400,
            )
        logged_in_user_id, user_id = parse_composite_user_id(baseuser)

        config_path, config_data = check_config_exist(user_id)
        if not config_data:
            return jsonify({"error": "No tracker config found for this user"}), 404

        tracker_meta = next(
            (
                t
                for t in config_data.get("trackers", [])
                if t["tracker_id"] == tracker_id
            ),
            None,
        )
        if not tracker_meta:
            return jsonify({"error": f"Tracker not found: {tracker_id}"}), 404

        runbook_id = tracker_meta.get("runbook_id")
        block_id = tracker_meta.get("block_id")

        # Step 1: Remove tracker entry from config using already-loaded config_data
        # (avoids a redundant S3 re-read that delete_tracker_config would do internally)
        config_data["trackers"] = [
            t for t in config_data.get("trackers", []) if t["tracker_id"] != tracker_id
        ]
        local_path = f"/tmp/config_tracker_{user_id}.json"
        with open(local_path, "w") as f:
            json.dump(config_data, f, indent=2)
        upload_any_file(
            file_path=local_path, user_id=user_id, file_name=config_path, type="tracker"
        )
        os.remove(local_path)

        # Step 2: Delete the tracker data file from S3
        delete_file_from_s3(f"{user_id}/tracker/{tracker_id}/tracker.json")

        # Step 2: Remove the block_id entry from the runbook's tracker_configuration
        if runbook_id and block_id:
            try:
                runbook = await dbserver.get_runbook_by_id(
                    user_id=user_id, runbook_id=runbook_id
                )
                if runbook:
                    if isinstance(runbook, list):
                        runbook = runbook[0]
                    tracker_conf = runbook.get("tracker_configuration") or "{}"
                    if isinstance(tracker_conf, str):
                        tracker_conf = json.loads(tracker_conf)
                    tracker_conf.pop(block_id, None)
                    await dbserver.update_runbook(
                        user_id=user_id,
                        runbook_id=runbook_id,
                        updates={"tracker_configuration": tracker_conf},
                    )
            except Exception:
                logging.warning(
                    f"Could not clean up tracker_configuration for runbook {runbook_id}: {traceback.format_exc()}"
                )

        # Audit logging
        (
            actor_user_id,
            actor_email,
            acting_on_behalf_of_user_id,
            acting_on_behalf_of_email,
        ) = build_audit_actor(baseuser)
        log_audit_event(
            action=TRACKER_DELETED,
            endpoint="/tracker/delete",
            ip=request.remote_addr,
            status="success",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=acting_on_behalf_of_user_id,
            acting_on_behalf_of_email=acting_on_behalf_of_email,
            metadata={
                "tracker_id": tracker_id,
                "runbook_id": runbook_id,
            },
        )
        g.audit_logged = True

        return (
            jsonify(
                {
                    "message": "Tracker deleted successfully",
                    "tracker_id": tracker_id,
                    "block_id": block_id,
                    "runbook_id": runbook_id,
                }
            ),
            200,
        )

    except Exception as e:
        logging.error(f"Tracker delete error: {traceback.format_exc()}")
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@tracker_bp.route("/tracker/list", methods=["GET"])
@permission_required_body("trackers.table.view")
def list_trackers_api():
    try:
        user_id = str(request.args.get("user_id"))

        if not user_id:
            return jsonify({"error": "Missing required parameter: user_id"}), 400
        logged_in_user_id, user_id = parse_composite_user_id(user_id)
        # Fetch tracker config
        config_path, config_data = check_config_exist(user_id)

        trackers = (config_data or {}).get("trackers", [])

        # Union trackers shared TO `user_id`. Always runs — the parsed
        # user_id is the right entity to look up shared resources for in
        # both plain and composite cases. (Mirrors /policy-hub/list.)
        try:
            shared_index = get_user_shared_resources(user_id, "tracker") or {}
        except Exception:
            shared_index = {}
        for tracker_id, entry in shared_index.items():
            owner_id = entry.get("mainuser_id")
            if not owner_id or owner_id == user_id:
                continue
            _, owner_config = check_config_exist(owner_id)
            owner_meta = next(
                (
                    t
                    for t in (owner_config or {}).get("trackers", [])
                    if t.get("tracker_id") == tracker_id
                ),
                None,
            )
            if not owner_meta:
                continue
            trackers.append(
                {**owner_meta, "owner_user_id": owner_id, "shared": True}
            )

        return (
            jsonify({"user_id": user_id, "trackers": trackers, "count": len(trackers)}),
            200,
        )

    except Exception as e:
        logging.error(f"List trackers error: {traceback.format_exc()}")
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@tracker_bp.route("/tracker/details", methods=["GET"])
@permission_required_body("trackers.table.view")
async def get_tracker_details_api():
    try:
        baseuser = str(request.args.get("user_id"))
        tracker_id = request.args.get("tracker_id")

        if not all([baseuser, tracker_id]):
            return (
                jsonify({"error": "Missing required parameters: user_id, tracker_id"}),
                400,
            )
        user_id, err = _check_tracker_share_access(baseuser, tracker_id)
        if err:
            return err

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
        tracker_path = tracker_meta.get(
            "file_path", f"{user_id}/tracker/{tracker_id}/tracker.json"
        )
        tracker_type = tracker_meta.get("type")
        runbook_id = tracker_meta.get("runbook_id")
        block_id = tracker_meta.get("block_id")

        # Fetch tracker data from S3
        tracker_data = read_json_from_s3(tracker_path)

        file_initialized = tracker_data is not None

        # If file doesn't exist, try to auto-initialize it using block schema from runbook
        if not tracker_data and block_id and runbook_id:
            try:
                # Fetch runbook to extract block schema
                runbook = await dbserver.get_runbook_by_id(
                    user_id=user_id, runbook_id=runbook_id
                )

                if runbook:
                    if isinstance(runbook, list):
                        runbook = runbook[0]

                    raw = runbook.get("structure_theme")
                    if raw:
                        try:
                            parsed = json.loads(raw) if isinstance(raw, str) else raw
                            if isinstance(parsed, str):
                                parsed = json.loads(parsed)

                            blocks = (
                                parsed.get("blocks", [])
                                if isinstance(parsed, dict)
                                else parsed
                            )

                            target_block = None
                            for b in blocks:
                                if (
                                    isinstance(b, dict)
                                    and b.get("block_id") == block_id
                                ):
                                    target_block = b
                                    break

                            if target_block:
                                block_config = extract_block_schema(
                                    target_block, tracker_type
                                )
                                _, tracker_data = ensure_tracker_file_exists(
                                    user_id=user_id,
                                    tracker_id=tracker_id,
                                    tracker_type=tracker_type,
                                    runbook_id=runbook_id,
                                    block_config=block_config,
                                )
                                logging.info(f"Auto-initialized tracker {tracker_id}")
                        except Exception as e:
                            logging.warning(f"Failed to extract block schema: {e}")
            except Exception as e:
                logging.warning(f"Failed to auto-initialize tracker from runbook: {e}")

        if not tracker_data:
            return (
                jsonify(
                    {
                        "error": "Tracker file not initialized. Use /tracker/append to initialize it.",
                        "tracker_id": tracker_id,
                        "tracker_meta": tracker_meta,
                        "file_initialized": file_initialized,
                    }
                ),
                404,
            )

        data_count = {"source_blocks": len(tracker_data.get("source_blocks", []))}
        if tracker_type == "table":
            data_count["rows"] = len(tracker_data.get("rows", []))
        elif tracker_type == "matrix":
            data_count["cells"] = len(tracker_data.get("cells", []))
        elif tracker_type == "scorecard":
            data_count["records"] = len(tracker_data.get("records", []))

        return (
            jsonify(
                {
                    "user_id": user_id,
                    "tracker_id": tracker_id,
                    "tracker_path": tracker_path,
                    "type": tracker_type,
                    "schema": tracker_data.get("schema", {}),
                    "data_count": data_count,
                    "tracker": tracker_data,
                    "file_initialized": file_initialized,
                }
            ),
            200,
        )

    except Exception as e:
        logging.error(f"Get tracker details error: {traceback.format_exc()}")
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@tracker_bp.route("/tracker/check-duplicate", methods=["GET"])
@permission_required_body("trackers.table.view")
def check_duplicate_result_api():
    try:
        user_id = str(request.args.get("user_id"))
        result_id = request.args.get("result_id")
        block_id = request.args.get("block_id")

        if not all([user_id, result_id, block_id]):
            return (
                jsonify(
                    {
                        "error": "Missing required parameters: user_id, result_id, block_id"
                    }
                ),
                400,
            )
        logged_in_user_id, user_id = parse_composite_user_id(user_id)

        # Fetch tracker config
        config_path, config_data = check_config_exist(user_id)

        if not config_data:
            return (
                jsonify(
                    {
                        "found": False,
                        "message": "No trackers found for this user",
                        "result_id": result_id,
                        "block_id": block_id,
                    }
                ),
                200,
            )

        trackers = config_data.get("trackers", [])
        matches = []

        # Check each tracker to see if it has data from this result_id and block_id
        for tracker_meta in trackers:
            tracker_id = tracker_meta.get("tracker_id")
            tracker_path = tracker_meta.get(
                "file_path", f"{user_id}/tracker/{tracker_id}/tracker.json"
            )
            tracker_type = tracker_meta.get("type")

            # Read tracker file
            tracker_data = read_json_from_s3(tracker_path)

            if not tracker_data:
                continue

            # Check if this tracker has data from the given result_id and block_id
            source_blocks = tracker_data.get("source_blocks", [])

            # Check if block_id is in source_blocks
            has_block = any(b.get("block_id") == block_id for b in source_blocks)

            if not has_block:
                continue

            # Double-check by looking at actual data rows/cells/records with this result_id
            data_found = False

            if tracker_type == "table":
                rows = tracker_data.get("rows", [])
                for row in rows:
                    if row.get("result_id") == result_id:
                        # Verify block_id in source
                        if row.get("source", {}).get("block_id") == block_id:
                            data_found = True
                            break

            elif tracker_type == "matrix":
                cells = tracker_data.get("cells", [])
                for cell in cells:
                    if cell.get("result_id") == result_id:
                        data_found = True
                        break

            elif tracker_type == "scorecard":
                records = tracker_data.get("records", [])
                for record in records:
                    if record.get("result_id") == result_id:
                        data_found = True
                        break

            if data_found:
                matches.append(
                    {
                        "tracker_id": tracker_id,
                        "tracker_name": tracker_meta.get("name"),
                        "tracker_type": tracker_type,
                        "runbook_id": tracker_meta.get("runbook_id"),
                        "block_id": block_id,
                        "result_id": result_id,
                        "tracker_path": tracker_path,
                    }
                )

        if matches:
            return (
                jsonify(
                    {
                        "found": True,
                        "message": f"Found {len(matches)} tracker(s) with this result and block",
                        "result_id": result_id,
                        "block_id": block_id,
                        "trackers": matches,
                    }
                ),
                200,
            )
        else:
            return (
                jsonify(
                    {
                        "found": False,
                        "message": "No trackers found with this result_id and block_id combination",
                        "result_id": result_id,
                        "block_id": block_id,
                    }
                ),
                200,
            )

    except Exception as e:
        logging.error(f"Check duplicate error: {traceback.format_exc()}")
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@tracker_bp.route("/tracker/view", methods=["GET"])
@permission_required_body("trackers.table.view")
def view_tracker_content_api():
    try:
        baseuser = str(request.args.get("user_id"))
        tracker_id = request.args.get("tracker_id")
        limit = int(request.args.get("limit", 100))
        offset = int(request.args.get("offset", 0))

        if not all([baseuser, tracker_id]):
            return (
                jsonify({"error": "Missing required parameters: user_id, tracker_id"}),
                400,
            )
        user_id, err = _check_tracker_share_access(baseuser, tracker_id)
        if err:
            return err

        if limit < 1 or limit > 1000:
            limit = 100
        if offset < 0:
            offset = 0

        # Fetch tracker config for metadata
        config_path, config_data = check_config_exist(user_id)
        tracker_meta = None

        if config_data:
            for t in config_data.get("trackers", []):
                if t.get("tracker_id") == tracker_id:
                    tracker_meta = t
                    break

        if not tracker_meta:
            return jsonify({"error": f"Tracker not found in config: {tracker_id}"}), 404

        tracker_path = tracker_meta.get(
            "file_path", f"{user_id}/tracker/{tracker_id}/tracker.json"
        )
        tracker_type = tracker_meta.get("type")

        # Fetch tracker data
        tracker_data = read_json_from_s3(tracker_path)

        if not tracker_data:
            return jsonify({"error": f"Tracker file not found: {tracker_id}"}), 404

        schema = tracker_data.get("schema", {})
        source_blocks = tracker_data.get("source_blocks", [])
        frameworks = tracker_data.get("frameworks", [])

        # Format response based on tracker type
        if tracker_type == "table":
            rows = tracker_data.get("rows", [])
            total_rows = len(rows)

            # Pagination
            paginated_rows = rows[offset : offset + limit]

            return (
                jsonify(
                    {
                        "user_id": user_id,
                        "tracker_id": tracker_id,
                        "type": "table",
                        "schema": {"columns": schema.get("columns", [])},
                        "data": {
                            "rows": paginated_rows,
                            "total": total_rows,
                            "offset": offset,
                            "limit": limit,
                            "has_more": (offset + limit) < total_rows,
                        },
                        "metadata": {
                            "source_blocks": source_blocks,
                            "source_block_count": len(source_blocks),
                        },
                        "frameworks": frameworks,
                    }
                ),
                200,
            )

        elif tracker_type == "matrix":
            cells = tracker_data.get("cells", [])
            total_cells = len(cells)

            # Pagination
            paginated_cells = cells[offset : offset + limit]

            return (
                jsonify(
                    {
                        "user_id": user_id,
                        "tracker_id": tracker_id,
                        "type": "matrix",
                        "schema": {
                            "rows": schema.get("rows", []),
                            "columns": schema.get("columns", []),
                            "cell_value_label": schema.get("cell_value_label", "Value"),
                        },
                        "data": {
                            "cells": paginated_cells,
                            "total": total_cells,
                            "offset": offset,
                            "limit": limit,
                            "has_more": (offset + limit) < total_cells,
                        },
                        "metadata": {
                            "source_blocks": source_blocks,
                            "source_block_count": len(source_blocks),
                        },
                    }
                ),
                200,
            )

        elif tracker_type == "scorecard":
            records = tracker_data.get("records", [])
            total_records = len(records)

            # Pagination
            paginated_records = records[offset : offset + limit]

            return (
                jsonify(
                    {
                        "user_id": user_id,
                        "tracker_id": tracker_id,
                        "type": "scorecard",
                        "schema": {"metrics": schema.get("metrics", [])},
                        "data": {
                            "records": paginated_records,
                            "total": total_records,
                            "offset": offset,
                            "limit": limit,
                            "has_more": (offset + limit) < total_records,
                        },
                        "metadata": {
                            "source_blocks": source_blocks,
                            "source_block_count": len(source_blocks),
                        },
                    }
                ),
                200,
            )

        else:
            return jsonify({"error": f"Unknown tracker type: {tracker_type}"}), 400

    except ValueError as ve:
        return jsonify({"error": f"Invalid parameter: {str(ve)}"}), 400

    except Exception as e:
        logging.error(f"View tracker error: {traceback.format_exc()}")
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@tracker_bp.route("/tracker/append", methods=["POST"])
@permission_required_body("trackers.row.add")
async def append_tracker_api():
    try:
        data = request.json

        baseuser = str(data.get("user_id"))
        tracker_id = data.get("tracker_id")
        result_id = data.get("result_id")
        block_id = data.get("block_id")
        block_title = data.get("block_title", "")

        if not all([baseuser, tracker_id, result_id, block_id]):
            return (
                jsonify(
                    {
                        "error": "Missing required fields: user_id, tracker_id, result_id, block_id"
                    }
                ),
                400,
            )
        logged_in_user_id, user_id = parse_composite_user_id(baseuser)

        # STEP 1: Fetch the runbook result from LanceDB
        result_row = await dbserver.runbook_get_result(
            user_id=user_id, result_id=result_id
        )

        if not result_row or result_row.get("status") == "not_found":
            return (
                jsonify({"error": f"Result not found or not completed: {result_id}"}),
                404,
            )

        if result_row.get("status") == "running":
            return jsonify({"error": "Result is still running, try again later"}), 202

        result_content = result_row.get("result")
        if not result_content:
            return jsonify({"error": "Result has no content"}), 404

        # STEP 2: Find the matching block in the result
        result_blocks = result_content.get("blocks", [])
        target_block = None

        for b in result_blocks:
            if b.get("block_id") == block_id:
                target_block = b
                break

        if not target_block:
            return (
                jsonify(
                    {
                        "error": f"Block '{block_id}' not found in result '{result_id}'",
                        "available_blocks": [b.get("block_id") for b in result_blocks],
                    }
                ),
                404,
            )

        # Ensure block_title is present
        if not target_block.get("block_title"):
            target_block["block_title"] = block_title

        # STEP 3: Get tracker metadata from config
        config_path, config_data = check_config_exist(user_id)
        tracker_meta = None

        if config_data:
            for t in config_data.get("trackers", []):
                if t.get("tracker_id") == tracker_id:
                    tracker_meta = t
                    break

        if not tracker_meta:
            return jsonify({"error": f"Tracker config not found: {tracker_id}"}), 404

        tracker_type = tracker_meta.get("type")
        runbook_id = tracker_meta.get("runbook_id")

        # STEP 4: Extract block schema for auto-initialization (if needed)
        block_config = extract_block_schema(target_block, tracker_type)

        # STEP 5: Ensure tracker file exists (create if missing, auto-initialize with schema)
        file_existed, tracker_data = ensure_tracker_file_exists(
            user_id=user_id,
            tracker_id=tracker_id,
            tracker_type=tracker_type,
            runbook_id=runbook_id,
            block_config=block_config,
        )

        if not file_existed:
            logging.info(f"Tracker file auto-initialized for {tracker_id}")

        # STEP 6: Validate block structure before append
        logging.info(
            f"Block structure for {block_id}: {json.dumps({k: v for k, v in target_block.items() if k not in ['content', 'text', 'narrative']}, indent=2)}"
        )

        if tracker_type == "table":
            headers = target_block.get("headers")
            rows = target_block.get("rows")
            logging.info(
                f"Table block - Headers: {headers}, Row count: {len(rows) if rows else 0}"
            )
            if not headers or not rows:
                logging.warning(f"Block missing headers or rows for table tracker")
        elif tracker_type == "matrix":
            data = target_block.get("data")
            logging.info(f"Matrix block - Data entries: {len(data) if data else 0}")
        elif tracker_type == "scorecard":
            data = target_block.get("data")
            logging.info(f"Scorecard block - Data entries: {len(data) if data else 0}")

        # Snapshot counts before append
        before = {
            "rows": len(tracker_data.get("rows", [])),
            "cells": len(tracker_data.get("cells", [])),
            "records": len(tracker_data.get("records", [])),
        }

        # STEP 7: Append block into tracker (mutates in place) and capture metadata
        append_metadata = append_to_tracker(tracker_data, target_block, result_id)

        # STEP 7.5: For table trackers with linked frameworks, initialize and analyze new rows
        if tracker_type == "table":
            linked_frameworks = tracker_data.get("frameworks", [])
            if linked_frameworks:
                schema_cols = tracker_data.get("schema", {}).get("columns", [])
                new_rows = tracker_data.get("rows", [])[before["rows"] :]

                if new_rows:
                    credits = Credits(user_id)
                    for fw_entry in linked_frameworks:
                        fw_id = fw_entry.get("id")
                        fw_name = fw_entry.get("name")

                        # Find per-framework column
                        fw_col = next(
                            (
                                col
                                for col in schema_cols
                                if col.get("source_column") == "frameworks"
                                and col.get("name") == fw_name
                            ),
                            None,
                        )
                        if not fw_col:
                            continue
                        fw_col_id = fw_col["id"]

                        for row in new_rows:
                            row["values"].setdefault(fw_col_id, [])

                        fw_s3_key = f"{FRAMEWORK_OWNER}/frameworks/{fw_id}.yaml"
                        fw_data = load_yaml_from_s3(fw_s3_key)
                        if not fw_data:
                            continue

                        fw_rows_data = fw_data.get("rows", [])
                        fw_cols = fw_data.get("columns", [])
                        req_col = fw_cols[0] if fw_cols else "REQUIREMENT/TASK"
                        sec_col = fw_cols[1] if len(fw_cols) > 1 else "SECTION/CATEGORY"

                        if not fw_rows_data:
                            continue

                        rows_analysis_input = [
                            {
                                "row_id": row.get("row_id"),
                                "col_values": {
                                    col.get("name"): row["values"].get(
                                        col.get("id"), ""
                                    )
                                    for col in schema_cols
                                    if col.get("source_column") != "frameworks"
                                },
                            }
                            for row in new_rows
                        ]

                        ai_result = await analyze_tracker_framework_rows(
                            rows=rows_analysis_input,
                            fw_rows=fw_rows_data,
                            framework_id=fw_id,
                            framework_name=fw_name,
                            user_id=user_id,
                            credits=credits,
                        )

                        reviewed_assignments = (
                            await quality_review_framework_assignments(
                                rows=rows_analysis_input,
                                fw_rows=fw_rows_data,
                                assignments=ai_result.get("assignments", []),
                                framework_name=fw_name,
                                user_id=user_id,
                                credits=credits,
                            )
                        )

                        for assignment in reviewed_assignments:
                            row_id = assignment.get("row_id")
                            fw_indices = assignment.get("fw_row_indices", [])
                            if isinstance(fw_indices, int):
                                fw_indices = [fw_indices] if fw_indices >= 0 else []

                            matched_row = next(
                                (r for r in new_rows if r.get("row_id") == row_id),
                                None,
                            )
                            if matched_row:
                                new_entries = [
                                    {
                                        "requirement": fw_rows_data[idx].get(
                                            req_col, ""
                                        ),
                                        "section": fw_rows_data[idx].get(sec_col, ""),
                                    }
                                    for idx in fw_indices
                                    if 0 <= idx < len(fw_rows_data)
                                ]
                                matched_row["values"][fw_col_id] = new_entries

        # STEP 8: Save updated tracker back to S3
        save_tracker_file(user_id, tracker_id, tracker_data)

        # STEP 9: save the corresponding blockid and tracked id in runbook table
        updates_runbook = {"tracker_configuration": {block_id: tracker_id}}
        update = await dbserver.update_runbook(
            user_id=user_id, runbook_id=runbook_id, updates=updates_runbook
        )
        print("tracker_configuration updated")

        after = {
            "rows": len(tracker_data.get("rows", [])),
            "cells": len(tracker_data.get("cells", [])),
            "records": len(tracker_data.get("records", [])),
        }

        added = {k: after[k] - before[k] for k in before}

        # Build response with discrepancy information
        response_data = {
            "message": "Tracker updated successfully",
            "tracker_id": tracker_id,
            "type": tracker_type,
            "result_id": result_id,
            "block_id": block_id,
            "added": added,
            "total": {
                "source_blocks": len(tracker_data.get("source_blocks", [])),
                **{k: v for k, v in after.items() if v > 0},
            },
        }

        # Include discrepancy information
        if tracker_type == "table" and "column_discrepancies" in append_metadata:
            discrepancies = append_metadata["column_discrepancies"]
            response_data["schema_changes"] = {
                "type": "column_discrepancies",
                "matched_columns": discrepancies.get("matched_columns", []),
                "new_columns_created": discrepancies.get("new_columns_created", []),
                "total_columns_in_schema": discrepancies.get("total_columns_in_schema"),
                "summary": f"Matched {len(discrepancies.get('matched_columns', []))} existing column(s), created {len(discrepancies.get('new_columns_created', []))} new column(s)",
            }
            response_data["deduplication"] = {
                "rows_appended": append_metadata.get("rows_appended", 0),
                "rows_skipped_dedup": append_metadata.get("rows_skipped_dedup", 0),
            }

        elif tracker_type == "matrix" and "axis_discrepancies" in append_metadata:
            discrepancies = append_metadata["axis_discrepancies"]
            response_data["schema_changes"] = {
                "type": "axis_changes",
                "new_rows": discrepancies.get("new_rows", []),
                "new_columns": discrepancies.get("new_columns", []),
                "total_rows": discrepancies.get("total_rows"),
                "total_columns": discrepancies.get("total_columns"),
                "summary": f"Added {len(discrepancies.get('new_rows', []))} row(s), added {len(discrepancies.get('new_columns', []))} column(s) to matrix axes",
            }
            response_data["deduplication"] = {
                "cells_appended": append_metadata.get("cells_appended", 0),
                "cells_skipped_dedup": append_metadata.get("cells_skipped_dedup", 0),
            }

        elif tracker_type == "scorecard" and "metric_discrepancies" in append_metadata:
            discrepancies = append_metadata["metric_discrepancies"]
            response_data["schema_changes"] = {
                "type": "metric_changes",
                "new_metrics": discrepancies.get("new_metrics", []),
                "total_metrics": discrepancies.get("total_metrics"),
                "summary": f"Created {len(discrepancies.get('new_metrics', []))} new metric(s)",
            }
            response_data["deduplication"] = {
                "records_appended": append_metadata.get("records_appended", 0),
                "records_skipped_dedup": append_metadata.get(
                    "records_skipped_dedup", 0
                ),
            }

        # Audit logging
        (
            actor_user_id,
            actor_email,
            acting_on_behalf_of_user_id,
            acting_on_behalf_of_email,
        ) = build_audit_actor(baseuser)
        log_audit_event(
            action=TRACKER_MODIFIED,
            endpoint="/tracker/append",
            ip=request.remote_addr,
            status="success",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=acting_on_behalf_of_user_id,
            acting_on_behalf_of_email=acting_on_behalf_of_email,
            metadata={
                "tracker_id": tracker_id,
                "result_id": result_id,
                "tracker_type": tracker_type,
                "append_count": append_metadata.get("rows_appended")
                or append_metadata.get("cells_appended")
                or append_metadata.get("records_appended"),
            },
        )
        g.audit_logged = True

        return jsonify(response_data), 200

    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400

    except Exception as e:
        logging.error(f"Append tracker error: {traceback.format_exc()}")
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


STRUCTURED_BLOCK_TYPES = {"table", "matrix", "scorecard"}


@tracker_bp.route("/tracker/sync-from-block", methods=["POST"])
@permission_required_body("trackers.table.edit")
async def sync_block_to_tracker_api():
    """
    Frontend sends an edited block from the report. This endpoint:
      1. Updates the runbook result/review with the new block data (similar to /radar/changeblock/confirm)
      2. If block_id is linked to a tracker in tracker_configuration, also updates the tracker
      3. If not linked to a tracker, only updates the result
      4. Handles table, matrix, and scorecard block types

    Request format (from frontend):
      {
        "user_id": "...",
        "runbook_id": "...",
        "result_id": "..." OR "review_id": "...",
        "block_id": "findings",
        "block_type": "table",
        "changed_block": { "micro_id": "...", "data": {...}, "type": "table" },
        "micro_id": "findings-table" (optional)
      }
    """
    try:
        data = request.json
        baseuser = str(data.get("user_id") or data.get("userid"))
        result_id = data.get("result_id") or data.get("review_id")
        block_id = data.get("block_id")
        micro_id = data.get("micro_id")
        changed_block = data.get("changed_block")

        # Validate required fields
        if not all([baseuser, result_id, block_id, changed_block]):
            return (
                jsonify(
                    {
                        "error": "Missing required fields: user_id, result_id (or review_id), block_id, and changed_block"
                    }
                ),
                400,
            )
        logged_in_user_id, user_id = parse_composite_user_id(baseuser)
        # Step 1: Fetch the result (similar to /radar/changeblock/confirm)
        record = await dbserver.runbook_get_result(user_id=user_id, result_id=result_id)

        if not record or not record.get("result"):
            return jsonify({"error": f"Result not found: {result_id}"}), 404

        updated_json = record["result"]

        # Step 2: Find and update the block in the result
        # ⚠️ IMPORTANT: Only update micro_blocks content, preserve block structure (block_id, block_type, etc.)
        block_found = False
        current_block = None

        for block in updated_json.get("blocks", []):
            if block.get("block_id") == block_id:
                current_block = block

                # ✅ Safe update: Only update micro_blocks, preserve block structure
                if "micro_blocks" in changed_block:
                    if micro_id:
                        # Update specific micro_block by micro_id
                        for micro_block in block.get("micro_blocks", []):
                            if micro_block.get("micro_id") == micro_id:
                                # 🔒 Only update data fields, preserve micro_block structure
                                if "data" in changed_block:
                                    micro_block["data"] = changed_block["data"]
                                if (
                                    "type" in changed_block
                                    and micro_block.get("type") != ""
                                ):
                                    # Validate type matches before updating
                                    original_type = micro_block.get("type")
                                    if original_type == changed_block.get("type"):
                                        micro_block["type"] = changed_block["type"]

                                block_found = True
                                logging.info(
                                    f"Updated micro_block {micro_id} in block {block_id}"
                                )
                                break
                    else:
                        # Update entire micro_blocks array, but validate structure
                        block["micro_blocks"] = changed_block.get(
                            "micro_blocks", block.get("micro_blocks", [])
                        )
                        block_found = True
                        logging.info(f"Updated micro_blocks for block {block_id}")
                else:
                    # If changed_block doesn't have micro_blocks, it might be direct data update
                    # Safely merge only data fields
                    if "data" in changed_block:
                        if block.get("micro_blocks"):
                            for micro_block in block.get("micro_blocks", []):
                                if (
                                    not micro_id
                                    or micro_block.get("micro_id") == micro_id
                                ):
                                    micro_block["data"] = changed_block["data"]
                        block_found = True
                        logging.info(f"Updated data in block {block_id}")

            if block_found:
                break

        if not block_found:
            return (
                jsonify(
                    {
                        "error": f"Block '{block_id}' or micro_block '{micro_id}' not found in result"
                    }
                ),
                404,
            )

        # Verify block structure is preserved
        if current_block:
            if not current_block.get("block_id"):
                logging.error(f"⚠️ WARNING: block_id was removed during update!")
                return (
                    jsonify(
                        {
                            "error": "Block structure validation failed: block_id was lost during update",
                            "block_id": block_id,
                        }
                    ),
                    500,
                )
            if not current_block.get("block_type"):
                logging.warning(
                    f"⚠️ WARNING: block_type missing in updated block {block_id}"
                )

        # Step 3: Save the updated result back to the database
        await dbserver.update_runbook_result(user_id, result_id, updated_json)
        logging.info(f"Result {result_id} updated successfully for block {block_id}")

        # Step 4: Check if this block is linked to a tracker
        tracker_updated = False
        linked_tracker_id = None
        tracker_debug_info = {}

        try:
            runbook_id = data.get("runbook_id")
            logging.info(
                f"[TRACKER_UPDATE] Step 4: Checking tracker link for block {block_id}, runbook_id={runbook_id}"
            )

            if not runbook_id:
                logging.info(
                    f"[TRACKER_UPDATE] No runbook_id provided, skipping tracker update"
                )
                tracker_debug_info["reason"] = "No runbook_id provided"
            else:
                runbook = await dbserver.get_runbook_by_id(
                    user_id=user_id, runbook_id=runbook_id
                )
                logging.info(
                    f"[TRACKER_UPDATE] Runbook fetch result: runbook exists = {bool(runbook)}"
                )

                if not runbook:
                    logging.warning(f"[TRACKER_UPDATE] Runbook {runbook_id} not found")
                    tracker_debug_info["reason"] = "Runbook not found"
                else:
                    if isinstance(runbook, list):
                        runbook = runbook[0]

                    raw_conf = runbook.get("tracker_configuration") or "{}"
                    tracker_conf = (
                        json.loads(raw_conf) if isinstance(raw_conf, str) else raw_conf
                    )
                    logging.info(
                        f"[TRACKER_UPDATE] Tracker configuration: {tracker_conf}"
                    )

                    linked_tracker_id = tracker_conf.get(block_id)
                    logging.info(
                        f"[TRACKER_UPDATE] Linked tracker_id for block {block_id}: {linked_tracker_id}"
                    )

                    # Step 5: If linked to tracker, update the tracker as well
                    if not linked_tracker_id:
                        logging.info(
                            f"[TRACKER_UPDATE] Block {block_id} is not linked to any tracker"
                        )
                        tracker_debug_info["reason"] = "Block not linked to tracker"
                    elif not current_block:
                        logging.error(f"[TRACKER_UPDATE] current_block is None!")
                        tracker_debug_info["reason"] = "current_block is None"
                    else:
                        config_path, config_data = check_config_exist(user_id)
                        logging.info(
                            f"[TRACKER_UPDATE] Config found: {bool(config_data)}"
                        )

                        tracker_meta = next(
                            (
                                t
                                for t in (config_data or {}).get("trackers", [])
                                if t["tracker_id"] == linked_tracker_id
                            ),
                            None,
                        )
                        logging.info(
                            f"[TRACKER_UPDATE] Tracker metadata found: {bool(tracker_meta)}"
                        )

                        if not tracker_meta:
                            logging.error(
                                f"[TRACKER_UPDATE] Tracker {linked_tracker_id} not in config"
                            )
                            tracker_debug_info["reason"] = "Tracker not in config"
                        else:
                            tracker_file_path = tracker_meta.get("file_path")
                            tracker_type = tracker_meta.get("type")
                            logging.info(
                                f"[TRACKER_UPDATE] Tracker type: {tracker_type}, file_path: {tracker_file_path}"
                            )

                            try:
                                tracker_data = read_json_from_s3(tracker_file_path)
                                logging.info(
                                    f"[TRACKER_UPDATE] Tracker data loaded: {bool(tracker_data)}"
                                )

                                if not tracker_data:
                                    logging.error(
                                        f"[TRACKER_UPDATE] Failed to read tracker file from {tracker_file_path}"
                                    )
                                    tracker_debug_info["reason"] = (
                                        "Tracker file not found on S3"
                                    )
                                else:
                                    logging.info(
                                        f"[TRACKER_UPDATE] Tracker structure: rows={len(tracker_data.get('rows', []))}, "
                                        f"cells={len(tracker_data.get('cells', []))}, "
                                        f"records={len(tracker_data.get('records', []))}"
                                    )

                                    # Update tracker with the new block data (not append, but replace)
                                    from tab_tracker.helper import (
                                        update_tracker_from_block,
                                    )

                                    logging.info(
                                        f"[TRACKER_UPDATE] Calling update_tracker_from_block with block_id={block_id}, result_id={result_id}"
                                    )
                                    logging.info(
                                        f"[TRACKER_UPDATE] Current block micro_blocks count: {len(current_block.get('micro_blocks', []))}"
                                    )

                                    update_summary = update_tracker_from_block(
                                        tracker_data, current_block, result_id, block_id
                                    )

                                    logging.info(
                                        f"[TRACKER_UPDATE] Update summary: {update_summary}"
                                    )

                                    # Save updated tracker
                                    save_result = save_tracker_file(
                                        user_id, linked_tracker_id, tracker_data
                                    )
                                    logging.info(
                                        f"[TRACKER_UPDATE] Tracker file saved: {save_result}"
                                    )

                                    tracker_updated = True
                                    tracker_debug_info["updated"] = True
                                    tracker_debug_info["summary"] = update_summary

                                    logging.info(
                                        f"✅ Tracker {linked_tracker_id} updated from block {block_id}: "
                                        f"removed {update_summary.get('rows_removed', 0)} rows, "
                                        f"added {update_summary.get('rows_added', 0)} rows"
                                    )
                            except Exception as e:
                                logging.error(
                                    f"[TRACKER_UPDATE] Exception during tracker update: {str(e)}"
                                )
                                logging.error(traceback.format_exc())
                                tracker_debug_info["error"] = str(e)
                                # Don't fail the API if tracker update fails - result update is primary

        except Exception as e:
            logging.error(f"[TRACKER_UPDATE] Outer exception: {str(e)}")
            logging.error(traceback.format_exc())
            tracker_debug_info["error"] = str(e)
            # Don't fail the API if tracker checking/updating fails

        # Step 6: Return success response
        response_data = {
            "status": "ok",
            "message": "Block updated successfully",
            "block_id": block_id,
            "result_id": result_id,
            "result_updated": True,
            "tracker_updated": tracker_updated,
        }

        if linked_tracker_id:
            response_data["tracker_id"] = linked_tracker_id

        # Include debug info for troubleshooting
        response_data["tracker_debug"] = {
            "runbook_id": data.get("runbook_id"),
            "linked_tracker_id": linked_tracker_id,
            **tracker_debug_info,
        }

        # Audit logging
        (
            actor_user_id,
            actor_email,
            acting_on_behalf_of_user_id,
            acting_on_behalf_of_email,
        ) = build_audit_actor(baseuser)
        log_audit_event(
            action=TRACKER_MODIFIED,
            endpoint="/tracker/sync-from-block",
            ip=request.remote_addr,
            status="success",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=acting_on_behalf_of_user_id,
            acting_on_behalf_of_email=acting_on_behalf_of_email,
            metadata={
                "result_id": result_id,
                "block_id": block_id,
                "linked_tracker_id": linked_tracker_id,
                "tracker_updated": tracker_updated,
            },
        )
        g.audit_logged = True

        return jsonify(response_data), 200

    except Exception as e:
        logging.error(f"Sync block to tracker error: {traceback.format_exc()}")
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@tracker_bp.route("/tracker/modify", methods=["POST"])
@permission_required_body("trackers.table.edit")
async def modify_tracker_details():
    """
    Direction 2: Edit specific entries in a tracker, then sync the updated values
    back to the corresponding runbook result block.

    Body:
      {
        user_id, tracker_id, result_id, block_id,
        entry_updates: [
          // table:     { "row_id": "...", "values": { "col_id": "new_val" } }
          // matrix:    { "row": "...", "column": "...", "value": "new_val" }
          // scorecard: { "metric": "...", "value": "new_val" }
        ]
      }
    """
    try:
        data = request.json
        baseuser = str(data.get("user_id"))
        tracker_id = data.get("tracker_id")
        result_id = data.get("result_id")
        block_id = data.get("block_id")
        entry_updates = data.get("entry_updates", [])

        if not all([baseuser, tracker_id, result_id, block_id]):
            return (
                jsonify(
                    {
                        "error": "Missing required fields: user_id, tracker_id, result_id, block_id"
                    }
                ),
                400,
            )
        user_id, err = _check_tracker_share_access(baseuser, tracker_id)
        if err:
            return err
        config_path, config_data = check_config_exist(user_id)
        tracker_meta = next(
            (
                t
                for t in (config_data or {}).get("trackers", [])
                if t["tracker_id"] == tracker_id
            ),
            None,
        )
        if not tracker_meta:
            return jsonify({"error": f"Tracker not found: {tracker_id}"}), 404

        tracker_type = tracker_meta.get("type")
        tracker_data = read_json_from_s3(tracker_meta["file_path"])
        if not tracker_data:
            return jsonify({"error": "Tracker file not found on storage"}), 404

        # Step 1: Apply manual edits to tracker entries
        apply_entry_updates(tracker_data, tracker_type, result_id, entry_updates)
        save_tracker_file(user_id, tracker_id, tracker_data)

        # Step 2: Sync updated tracker data back to the runbook result block
        result_row = await dbserver.runbook_get_result(
            user_id=user_id, result_id=result_id
        )
        if result_row and result_row.get("status") == "completed":
            result_content = result_row.get("result", {})
            blocks = result_content.get("blocks", [])
            target_block = next(
                (b for b in blocks if b.get("block_id") == block_id), None
            )
            if target_block:
                target_block["micro_blocks"] = _rebuild_micro_blocks_from_tracker(
                    tracker_data, result_id, block_id
                )
                await dbserver.update_runbook_result(user_id, result_id, result_content)

        # Audit logging
        (
            actor_user_id,
            actor_email,
            acting_on_behalf_of_user_id,
            acting_on_behalf_of_email,
        ) = build_audit_actor(baseuser)
        log_audit_event(
            action=TRACKER_MODIFIED,
            endpoint="/tracker/modify",
            ip=request.remote_addr,
            status="success",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=acting_on_behalf_of_user_id,
            acting_on_behalf_of_email=acting_on_behalf_of_email,
            metadata={
                "tracker_id": tracker_id,
                "result_id": result_id,
                "entries_updated": len(entry_updates),
            },
        )
        g.audit_logged = True

        return (
            jsonify(
                {
                    "message": "Tracker modified and runbook block updated",
                    "tracker_id": tracker_id,
                    "result_id": result_id,
                    "block_id": block_id,
                }
            ),
            200,
        )

    except Exception as e:
        logging.error(f"Modify tracker error: {traceback.format_exc()}")
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@tracker_bp.route("/tracker/add-entry", methods=["POST"])
@permission_required_body("trackers.row.add")
async def add_tracker_entry():
    """
    Add a new entry (row/cell/record) to an existing tracker.

    Body depends on tracker type:
      Table:
        { user_id, tracker_id, result_id, row_data: { col_id: value, ... } }
      Matrix:
        { user_id, tracker_id, result_id, row: "...", column: "...", value: ... }
      Scorecard:
        { user_id, tracker_id, result_id, metric: "...", value: ... }
    """
    try:
        data = request.json
        baseuser = str(data.get("user_id"))
        tracker_id = data.get("tracker_id")
        result_id = data.get("result_id")

        if not all([baseuser, tracker_id, result_id]):
            return (
                jsonify(
                    {"error": "Missing required fields: user_id, tracker_id, result_id"}
                ),
                400,
            )
        user_id, err = _check_tracker_share_access(baseuser, tracker_id)
        if err:
            return err
        config_path, config_data = check_config_exist(user_id)
        tracker_meta = next(
            (
                t
                for t in (config_data or {}).get("trackers", [])
                if t["tracker_id"] == tracker_id
            ),
            None,
        )
        if not tracker_meta:
            return jsonify({"error": f"Tracker not found: {tracker_id}"}), 404

        tracker_type = tracker_meta.get("type")
        tracker_data = read_json_from_s3(tracker_meta["file_path"])
        if not tracker_data:
            return jsonify({"error": "Tracker file not found on storage"}), 404

        entry_added = None

        # Table: add new row
        if tracker_type == "table":
            row_data = data.get("row_data")
            if not row_data or not isinstance(row_data, dict):
                return (
                    jsonify(
                        {"error": "Table requires row_data (dict of col_id: value)"}
                    ),
                    400,
                )

            row_id = f"trk_r_{uuid.uuid4().hex[:8]}"
            new_row = {
                "row_id": row_id,
                "result_id": result_id,
                "source": {
                    "block_id": "manual",
                    "micro_id": None,
                    "row_index": len(tracker_data.get("rows", [])),
                },
                "values": row_data,
                "last_updated_from": "manual",
            }
            tracker_data.setdefault("rows", []).append(new_row)
            entry_added = {"type": "row", "row_id": row_id}

        # Matrix: add or update cell
        elif tracker_type == "matrix":
            row_label = data.get("row")
            col_label = data.get("column")
            value = data.get("value")

            if not all([row_label, col_label, value is not None]):
                return jsonify({"error": "Matrix requires row, column, value"}), 400

            cells = tracker_data.get("cells", [])
            existing_cell = next(
                (
                    c
                    for c in cells
                    if c.get("result_id") == result_id
                    and c.get("row") == row_label
                    and c.get("column") == col_label
                ),
                None,
            )

            if existing_cell:
                existing_cell["value"] = value
                entry_added = {
                    "type": "cell",
                    "action": "updated",
                    "row": row_label,
                    "column": col_label,
                }
            else:
                new_cell = {
                    "row": row_label,
                    "column": col_label,
                    "value": value,
                    "result_id": result_id,
                }
                tracker_data.setdefault("cells", []).append(new_cell)
                entry_added = {
                    "type": "cell",
                    "action": "created",
                    "row": row_label,
                    "column": col_label,
                }

        # Scorecard: add or update record
        elif tracker_type == "scorecard":
            metric = data.get("metric")
            value = data.get("value")

            if not metric or value is None:
                return jsonify({"error": "Scorecard requires metric and value"}), 400

            records = tracker_data.get("records", [])
            existing_record = next(
                (
                    r
                    for r in records
                    if r.get("result_id") == result_id and r.get("metric") == metric
                ),
                None,
            )

            if existing_record:
                existing_record["value"] = value
                entry_added = {"type": "record", "action": "updated", "metric": metric}
            else:
                new_record = {"metric": metric, "value": value, "result_id": result_id}
                tracker_data.setdefault("records", []).append(new_record)
                entry_added = {"type": "record", "action": "created", "metric": metric}

        else:
            return jsonify({"error": f"Unsupported tracker type: {tracker_type}"}), 400

        save_tracker_file(user_id, tracker_id, tracker_data)

        # Audit logging
        (
            actor_user_id,
            actor_email,
            acting_on_behalf_of_user_id,
            acting_on_behalf_of_email,
        ) = build_audit_actor(baseuser)
        log_audit_event(
            action=TRACKER_ENTRY_ADDED,
            endpoint="/tracker/add-entry",
            ip=request.remote_addr,
            status="success",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=acting_on_behalf_of_user_id,
            acting_on_behalf_of_email=acting_on_behalf_of_email,
            metadata={
                "tracker_id": tracker_id,
                "result_id": result_id,
                "entry_type": entry_added.get("type") if entry_added else None,
            },
        )
        g.audit_logged = True

        return (
            jsonify(
                {
                    "message": "Entry added to tracker successfully",
                    "tracker_id": tracker_id,
                    "result_id": result_id,
                    "entry": entry_added,
                }
            ),
            200,
        )

    except Exception as e:
        logging.error(f"Add tracker entry error: {traceback.format_exc()}")
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@tracker_bp.route("/tracker/add-column", methods=["POST"])
@permission_required_body("trackers.column.add")
async def add_tracker_column():
    """
    Add a new column/axis/metric to an existing tracker schema.

    Body depends on tracker type:
      Table:
        { user_id, tracker_id, column_name, default_value? }
        — Adds column to schema; fills default_value in all existing rows (optional).

      Matrix:
        { user_id, tracker_id, axis: "row"|"column", value: "label" }
        — Adds a new row-label or column-label to the matrix axes.

      Scorecard:
        { user_id, tracker_id, metric: "metric_name" }
        — Adds a new metric to the schema.
    """
    try:
        data = request.json
        baseuser = str(data.get("user_id"))
        tracker_id = data.get("tracker_id")

        if not all([baseuser, tracker_id]):
            return (
                jsonify({"error": "Missing required fields: user_id, tracker_id"}),
                400,
            )
        logged_in_user_id, user_id = parse_composite_user_id(baseuser)
        config_path, config_data = check_config_exist(user_id)
        tracker_meta = next(
            (
                t
                for t in (config_data or {}).get("trackers", [])
                if t["tracker_id"] == tracker_id
            ),
            None,
        )
        if not tracker_meta:
            return jsonify({"error": f"Tracker not found: {tracker_id}"}), 404

        tracker_type = tracker_meta.get("type")
        tracker_data = read_json_from_s3(tracker_meta["file_path"])
        if not tracker_data:
            return jsonify({"error": "Tracker file not found on storage"}), 404

        schema_change = None

        if tracker_type == "table":
            column_name = data.get("column_name")
            column_type = data.get("column_type")
            if not column_name:
                return jsonify({"error": "Table requires column_name"}), 400

            schema_cols = tracker_data["schema"]["columns"]

            # Prevent duplicate column names
            existing_names = {col["source_column"] for col in schema_cols}
            if column_name in existing_names:
                return jsonify({"error": f"Column '{column_name}' already exists"}), 409

            new_col_id = f"col_{len(schema_cols) + 1}"
            new_col = {
                "id": new_col_id,
                "name": column_name,
                "source_column": column_name,
                "type": column_type,
            }
            schema_cols.append(new_col)

            # Backfill default_value into all existing rows
            default_value = data.get("default_value", None)
            if default_value is not None:
                for row in tracker_data.get("rows", []):
                    row["values"].setdefault(new_col_id, default_value)

            schema_change = {
                "type": "column_added",
                "column_id": new_col_id,
                "column_name": column_name,
                "rows_backfilled": (
                    len(tracker_data.get("rows", []))
                    if default_value is not None
                    else 0
                ),
            }

        elif tracker_type == "matrix":
            axis = data.get("axis")
            value = data.get("value")

            if not axis or axis not in ("row", "column"):
                return (
                    jsonify({"error": "Matrix requires axis ('row' or 'column')"}),
                    400,
                )
            if not value:
                return jsonify({"error": "Matrix requires value (axis label)"}), 400

            schema = tracker_data["schema"]
            axis_key = "rows" if axis == "row" else "columns"

            if value in schema.get(axis_key, []):
                return (
                    jsonify({"error": f"{axis.capitalize()} '{value}' already exists"}),
                    409,
                )

            schema.setdefault(axis_key, []).append(value)
            schema_change = {"type": f"{axis}_added", "value": value}

        elif tracker_type == "scorecard":
            metric = data.get("metric")
            if not metric:
                return jsonify({"error": "Scorecard requires metric"}), 400

            existing_metrics = tracker_data["schema"].get("metrics", [])
            if metric in existing_metrics:
                return jsonify({"error": f"Metric '{metric}' already exists"}), 409

            tracker_data["schema"].setdefault("metrics", []).append(metric)
            schema_change = {"type": "metric_added", "metric": metric}

        else:
            return jsonify({"error": f"Unsupported tracker type: {tracker_type}"}), 400

        save_tracker_file(user_id, tracker_id, tracker_data)

        # Audit logging
        (
            actor_user_id,
            actor_email,
            acting_on_behalf_of_user_id,
            acting_on_behalf_of_email,
        ) = build_audit_actor(baseuser)
        log_audit_event(
            action=TRACKER_COLUMN_ADDED,
            endpoint="/tracker/add-column",
            ip=request.remote_addr,
            status="success",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=acting_on_behalf_of_user_id,
            acting_on_behalf_of_email=acting_on_behalf_of_email,
            metadata={
                "tracker_id": tracker_id,
                "tracker_type": tracker_type,
                "schema_change": schema_change,
            },
        )
        g.audit_logged = True

        return (
            jsonify(
                {
                    "message": "Schema updated successfully",
                    "tracker_id": tracker_id,
                    "tracker_type": tracker_type,
                    "schema_change": schema_change,
                }
            ),
            200,
        )

    except Exception as e:
        logging.error(f"Add tracker column error: {traceback.format_exc()}")
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@tracker_bp.route("/tracker/delete-column", methods=["DELETE"])
@permission_required_body("trackers.column.delete")
async def delete_tracker_column():
    """
    Delete a column/axis/metric from an existing tracker schema and
    removes all associated data entries.

    Body depends on tracker type:
      Table:
        { user_id, tracker_id, column_id }
        — Removes the column from schema and strips it from all row values.

      Matrix:
        { user_id, tracker_id, axis: "row"|"column", value: "label" }
        — Removes the axis label and all cells on that row/column.

      Scorecard:
        { user_id, tracker_id, metric: "metric_name" }
        — Removes the metric from schema and all matching records.
    """
    try:
        data = request.json
        baseuser = str(data.get("user_id"))
        tracker_id = data.get("tracker_id")

        if not all([baseuser, tracker_id]):
            return (
                jsonify({"error": "Missing required fields: user_id, tracker_id"}),
                400,
            )
        logged_in_user_id, user_id = parse_composite_user_id(baseuser)

        config_path, config_data = check_config_exist(user_id)
        tracker_meta = next(
            (
                t
                for t in (config_data or {}).get("trackers", [])
                if t["tracker_id"] == tracker_id
            ),
            None,
        )
        if not tracker_meta:
            return jsonify({"error": f"Tracker not found: {tracker_id}"}), 404

        tracker_type = tracker_meta.get("type")
        tracker_data = read_json_from_s3(tracker_meta["file_path"])
        if not tracker_data:
            return jsonify({"error": "Tracker file not found on storage"}), 404

        schema_change = None

        if tracker_type == "table":
            column_id = data.get("column_id")
            if not column_id:
                return jsonify({"error": "Table requires column_id"}), 400

            schema_cols = tracker_data["schema"]["columns"]
            col_to_remove = next((c for c in schema_cols if c["id"] == column_id), None)
            if not col_to_remove:
                return (
                    jsonify({"error": f"Column '{column_id}' not found in schema"}),
                    404,
                )

            tracker_data["schema"]["columns"] = [
                c for c in schema_cols if c["id"] != column_id
            ]

            # Strip the deleted column from all existing row values
            rows_affected = 0
            for row in tracker_data.get("rows", []):
                if column_id in row.get("values", {}):
                    del row["values"][column_id]
                    rows_affected += 1

            schema_change = {
                "type": "column_deleted",
                "column_id": column_id,
                "column_name": col_to_remove.get("name"),
                "rows_affected": rows_affected,
            }

        elif tracker_type == "matrix":
            axis = data.get("axis")
            value = data.get("value")

            if not axis or axis not in ("row", "column"):
                return (
                    jsonify({"error": "Matrix requires axis ('row' or 'column')"}),
                    400,
                )
            if not value:
                return (
                    jsonify({"error": "Matrix requires value (axis label to delete)"}),
                    400,
                )

            schema = tracker_data["schema"]
            axis_key = "rows" if axis == "row" else "columns"

            if value not in schema.get(axis_key, []):
                return (
                    jsonify(
                        {"error": f"{axis.capitalize()} '{value}' not found in schema"}
                    ),
                    404,
                )

            schema[axis_key] = [v for v in schema[axis_key] if v != value]

            # Remove all cells on the deleted axis label
            cell_field = "row" if axis == "row" else "column"
            before_count = len(tracker_data.get("cells", []))
            tracker_data["cells"] = [
                c for c in tracker_data.get("cells", []) if c.get(cell_field) != value
            ]
            cells_removed = before_count - len(tracker_data["cells"])

            schema_change = {
                "type": f"{axis}_deleted",
                "value": value,
                "cells_removed": cells_removed,
            }

        elif tracker_type == "scorecard":
            metric = data.get("metric")
            if not metric:
                return jsonify({"error": "Scorecard requires metric"}), 400

            existing_metrics = tracker_data["schema"].get("metrics", [])
            if metric not in existing_metrics:
                return jsonify({"error": f"Metric '{metric}' not found in schema"}), 404

            tracker_data["schema"]["metrics"] = [
                m for m in existing_metrics if m != metric
            ]

            before_count = len(tracker_data.get("records", []))
            tracker_data["records"] = [
                r for r in tracker_data.get("records", []) if r.get("metric") != metric
            ]
            records_removed = before_count - len(tracker_data["records"])

            schema_change = {
                "type": "metric_deleted",
                "metric": metric,
                "records_removed": records_removed,
            }

        else:
            return jsonify({"error": f"Unsupported tracker type: {tracker_type}"}), 400

        # Save tracker changes
        save_tracker_file(user_id, tracker_id, tracker_data)

        # Step: Also delete the column/axis/metric from the corresponding result blocks
        # Get unique result_ids and block_ids from tracker
        result_ids_to_update = set()
        block_ids_to_update = set()

        if tracker_type == "table":
            for row in tracker_data.get("rows", []):
                if result_id := row.get("result_id"):
                    result_ids_to_update.add(result_id)
                if block_id := row.get("source", {}).get("block_id"):
                    block_ids_to_update.add(block_id)

        elif tracker_type == "matrix":
            for cell in tracker_data.get("cells", []):
                if result_id := cell.get("result_id"):
                    result_ids_to_update.add(result_id)

        elif tracker_type == "scorecard":
            for record in tracker_data.get("records", []):
                if result_id := record.get("result_id"):
                    result_ids_to_update.add(result_id)

        # Also get block_ids from source_blocks if available
        for src_block in tracker_data.get("source_blocks", []):
            if block_id := src_block.get("block_id"):
                block_ids_to_update.add(block_id)

        # Update each result to remove the deleted column/axis/metric
        results_updated = []
        results_failed = []
        runbook_id = tracker_meta.get("runbook_id")

        for result_id_val in result_ids_to_update:
            try:
                # Fetch the result
                result_row = await dbserver.runbook_get_result(
                    user_id=user_id, result_id=result_id_val
                )

                if not result_row or result_row.get("status") == "not_found":
                    logging.warning(f"Result {result_id_val} not found, skipping")
                    results_failed.append(
                        {"result_id": result_id_val, "reason": "Result not found"}
                    )
                    continue

                result_content = result_row.get("result", {})
                blocks = result_content.get("blocks", [])
                blocks_modified = 0

                # Update blocks matching the tracker's source_blocks
                for block in blocks:
                    if block.get("block_id") not in block_ids_to_update:
                        continue

                    block_type_result = block.get("block_type", "")
                    micro_blocks = block.get("micro_blocks", [])

                    # For table: remove column from micro_blocks data
                    if block_type_result == "table" and tracker_type == "table":
                        column_id = data.get("column_id")
                        for micro_block in micro_blocks:
                            if micro_block.get("type") == "table_schema":
                                data_rows = micro_block.get("data", {}).get("rows", [])
                                for row_data in data_rows:
                                    if column_id in row_data:
                                        del row_data[column_id]
                                blocks_modified += 1

                    # For matrix: remove axis label from micro_blocks
                    elif block_type_result == "matrix" and tracker_type == "matrix":
                        axis = data.get("axis")
                        value = data.get("value")
                        axis_key = "x_axis" if axis == "column" else "y_axis"

                        for micro_block in micro_blocks:
                            if micro_block.get("type") == "matrix_schema":
                                # Remove from axis values
                                axis_data = micro_block.get(axis_key, {})
                                if "values" in axis_data:
                                    axis_data["values"] = [
                                        v for v in axis_data["values"] if v != value
                                    ]

                                # Remove matrix cells for this axis
                                matrix = micro_block.get("data", {}).get("matrix", [])
                                if axis == "row":
                                    # Remove entire row
                                    row_idx = next(
                                        (
                                            i
                                            for i, label in enumerate(
                                                micro_block.get("y_axis", {}).get(
                                                    "values", []
                                                )
                                            )
                                            if label == value
                                        ),
                                        -1,
                                    )
                                    if row_idx >= 0 and row_idx < len(matrix):
                                        del matrix[row_idx]
                                else:
                                    # Remove column from each row
                                    col_idx = next(
                                        (
                                            i
                                            for i, label in enumerate(
                                                micro_block.get("x_axis", {}).get(
                                                    "values", []
                                                )
                                            )
                                            if label == value
                                        ),
                                        -1,
                                    )
                                    if col_idx >= 0:
                                        for row in matrix:
                                            if col_idx < len(row):
                                                del row[col_idx]

                                blocks_modified += 1

                    # For scorecard: remove metric from micro_blocks
                    elif (
                        block_type_result == "scorecard" and tracker_type == "scorecard"
                    ):
                        metric = data.get("metric")
                        for micro_block in micro_blocks:
                            if micro_block.get("type") == "scorecard_schema":
                                data_records = micro_block.get("data", {}).get(
                                    "records", []
                                )
                                micro_block["data"]["records"] = [
                                    r for r in data_records if r.get("metric") != metric
                                ]
                                blocks_modified += 1

                # Update the result in the database
                if blocks_modified > 0:
                    await dbserver.update_runbook_result(
                        user_id, result_id_val, result_content
                    )
                    results_updated.append(
                        {"result_id": result_id_val, "blocks_modified": blocks_modified}
                    )

            except Exception as e:
                logging.error(
                    f"Failed to update result {result_id_val}: {traceback.format_exc()}"
                )
                results_failed.append({"result_id": result_id_val, "reason": str(e)})

        # Build response with result update information
        response_data = {
            "message": "Column deleted successfully",
            "tracker_id": tracker_id,
            "tracker_type": tracker_type,
            "schema_change": schema_change,
            "result_synchronization": {
                "tracker_updated": True,
                "results_updated": len(results_updated),
                "results_failed": len(results_failed),
                "details": {"updated": results_updated, "failed": results_failed},
            },
        }

        # Audit logging
        (
            actor_user_id,
            actor_email,
            acting_on_behalf_of_user_id,
            acting_on_behalf_of_email,
        ) = build_audit_actor(baseuser)
        log_audit_event(
            action=TRACKER_COLUMN_DELETED,
            endpoint="/tracker/delete-column",
            ip=request.remote_addr,
            status="success",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=acting_on_behalf_of_user_id,
            acting_on_behalf_of_email=acting_on_behalf_of_email,
            metadata={
                "tracker_id": tracker_id,
                "tracker_type": tracker_type,
                "schema_change": schema_change,
                "results_updated": len(results_updated),
            },
        )
        g.audit_logged = True

        return jsonify(response_data), 200

    except Exception as e:
        logging.error(f"Delete tracker column error: {traceback.format_exc()}")
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@tracker_bp.route("/tracker/upload-evidence", methods=["POST"])
@permission_required_body("trackers.table.edit")
def upload_evidence_api():
    """
    Upload an evidence file and link it to a tracker row's evidence column.

    Request:
        - user_id: User ID
        - tracker_id: Tracker ID
        - row_id: Row ID (internal row_id, not row_index)
        - column_id: Column ID (evidence type column)
        - file: File object (multipart/form-data)

    Returns:
        - s3_key: S3 path of uploaded file
        - file_info: Metadata about the uploaded file
    """
    try:
        from tab_tracker.helper import upload_evidence_file, update_tracker_evidence

        baseuser = str(request.form.get("user_id"))
        tracker_id = request.form.get("tracker_id")
        row_id = request.form.get("row_id")
        column_id = request.form.get("column_id")

        if not all([baseuser, tracker_id, row_id, column_id]):
            return (
                jsonify(
                    {
                        "error": "Missing required fields: user_id, tracker_id, row_id, column_id"
                    }
                ),
                400,
            )

        if "file" not in request.files:
            return jsonify({"error": "No file provided"}), 400

        file_obj = request.files["file"]
        if file_obj.filename == "":
            return jsonify({"error": "No file selected"}), 400
        logged_in_user_id, user_id = parse_composite_user_id(baseuser)

        # Step 1: Upload file to S3
        upload_result = upload_evidence_file(
            user_id=user_id,
            tracker_id=tracker_id,
            row_id=row_id,
            column_id=column_id,
            file_obj=file_obj,
            filename=file_obj.filename,
        )

        if not upload_result.get("success"):
            return (
                jsonify({"error": f"File upload failed: {upload_result.get('error')}"}),
                500,
            )

        s3_key = upload_result.get("s3_key")

        # Step 2: Update tracker row with S3 key
        update_result = update_tracker_evidence(
            user_id=user_id,
            tracker_id=tracker_id,
            row_id=row_id,
            column_id=column_id,
            s3_key=s3_key,
        )

        if not update_result.get("success"):
            return (
                jsonify(
                    {
                        "error": f"Failed to link evidence to tracker: {update_result.get('error')}"
                    }
                ),
                500,
            )

        # Audit logging
        (
            actor_user_id,
            actor_email,
            acting_on_behalf_of_user_id,
            acting_on_behalf_of_email,
        ) = build_audit_actor(baseuser)
        log_audit_event(
            action=TRACKER_EVIDENCE_UPLOADED,
            endpoint="/tracker/upload-evidence",
            ip=request.remote_addr,
            status="success",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=acting_on_behalf_of_user_id,
            acting_on_behalf_of_email=acting_on_behalf_of_email,
            metadata={
                "tracker_id": tracker_id,
                "row_id": row_id,
                "column_id": column_id,
                "s3_key": s3_key,
                "filename": upload_result.get("filename"),
            },
        )
        g.audit_logged = True

        return (
            jsonify(
                {
                    "message": "Evidence file uploaded and linked successfully",
                    "tracker_id": tracker_id,
                    "row_id": row_id,
                    "column_id": column_id,
                    "file_info": {
                        "s3_key": s3_key,
                        "filename": upload_result.get("filename"),
                        "upload_timestamp": upload_result.get("upload_timestamp"),
                    },
                    "tracker_update": update_result,
                }
            ),
            200,
        )

    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400

    except Exception as e:
        logging.error(f"Evidence upload error: {traceback.format_exc()}")
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


def _make_framework_col_id(schema_cols: list) -> str:
    """Generate a unique col_N ID that doesn't collide with existing column IDs."""
    existing_ids = {col["id"] for col in schema_cols}
    n = len(schema_cols) + 1
    while f"col_{n}" in existing_ids:
        n += 1
    return f"col_{n}"


async def _add_framework_worker(data: dict, job_id: str = None) -> dict:
    """Background worker: creates a per-framework column and AI-populates it row by row."""
    baseuser = str(data.get("user_id", ""))
    tracker_id = data.get("tracker_id")
    framework_id = data.get("framework_id")
    logged_in_user_id, user_id = parse_composite_user_id(baseuser)
    session_id = data.get("session_id")
    should_emit = bool(job_id and session_id)

    async def emit(msg):
        if should_emit:
            try:
                await ws_service.emit(
                    user_id=user_id,
                    message=msg.get("message"),
                    scope=msg.get("scope", "job"),
                    session_id=msg.get("session_id"),
                    job_id=msg.get("job_id"),
                    msg_type=msg.get("type"),
                    stage=msg.get("stage"),
                    progress=msg.get("progress"),
                    feature="tracker_framework",
                )
            except Exception:
                pass

    try:
        await emit(
            msg_builder_main.job_progress(
                job_id, session_id, "init", "Starting framework analysis…", 5
            )
        )

        # Load framework YAML
        fw_s3_key = f"{FRAMEWORK_OWNER}/frameworks/{framework_id}.yaml"
        fw_data = load_yaml_from_s3(fw_s3_key)
        if not fw_data:
            raise Exception(f"Framework not found in policyhub: {framework_id}")
        framework_name = fw_data.get("name")
        if not framework_name:
            raise Exception(f"Framework missing name in policyhub: {framework_id}")

        await emit(
            msg_builder_main.job_progress(
                job_id,
                session_id,
                "loading",
                f"Loading {framework_name} requirements",
                10,
            )
        )

        fw_rows = fw_data.get("rows", [])
        fw_cols = fw_data.get("columns", [])
        req_col = fw_cols[0] if fw_cols else "REQUIREMENT/TASK"
        sec_col = fw_cols[1] if len(fw_cols) > 1 else "SECTION/CATEGORY"

        # Load tracker data
        await emit(
            msg_builder_main.job_progress(
                job_id, session_id, "loading", "Preparing tracker data", 15
            )
        )

        config_path, config_data = check_config_exist(user_id)
        tracker_meta = next(
            (
                t
                for t in (config_data or {}).get("trackers", [])
                if t["tracker_id"] == tracker_id
            ),
            None,
        )
        if not tracker_meta:
            raise Exception(f"Tracker not found: {tracker_id}")
        tracker_data = read_json_from_s3(tracker_meta["file_path"])
        if not tracker_data:
            raise Exception("Tracker file not found on storage")

        # Safety re-check for duplicate
        if any(fw["id"] == framework_id for fw in tracker_data.get("frameworks", [])):
            raise Exception("Framework already linked to this tracker")

        # Create per-framework column
        await emit(
            msg_builder_main.job_progress(
                job_id, session_id, "setup", f"Creating {framework_name} column", 20
            )
        )

        schema_cols = tracker_data.get("schema", {}).get("columns", [])
        new_col_id = _make_framework_col_id(schema_cols)
        new_col = {
            "id": new_col_id,
            "name": framework_name,
            "source_column": "frameworks",
            "type": "text",
        }
        schema_cols.append(new_col)
        tracker_data["schema"]["columns"] = schema_cols

        # Initialize column to [] in all existing rows
        for row in tracker_data.get("rows", []):
            row["values"].setdefault(new_col_id, [])

        rows_for_analysis = tracker_data.get("rows", [])
        rows_assigned = 0
        rows_empty = 0

        if rows_for_analysis and fw_rows:
            credits = Credits(user_id)
            # Exclude ALL framework columns from AI input
            rows_analysis_input = [
                {
                    "row_id": row.get("row_id"),
                    "col_values": {
                        col.get("name"): row["values"].get(col.get("id"), "")
                        for col in schema_cols
                        if col.get("source_column") != "frameworks"
                    },
                }
                for row in rows_for_analysis
            ]

            await emit(
                msg_builder_main.job_progress(
                    job_id,
                    session_id,
                    "analysis",
                    f"Analyzing {len(rows_analysis_input)} rows against {framework_name}…",
                    25,
                )
            )

            # Per-row analysis with progress callback
            total_rows = len(rows_analysis_input)
            analyzed_count = 0

            async def on_row_analyzed():
                nonlocal analyzed_count
                analyzed_count += 1
                pct = 25 + int(analyzed_count / total_rows * 40)  # 25 → 65
                await emit(
                    msg_builder_main.job_progress(
                        job_id,
                        session_id,
                        "analysis",
                        f"Analyzing row {analyzed_count}/{total_rows}…",
                        pct,
                    )
                )

            ai_result = await analyze_tracker_framework_rows(
                rows=rows_analysis_input,
                fw_rows=fw_rows,
                framework_id=framework_id,
                framework_name=framework_name,
                user_id=user_id,
                credits=credits,
                on_row_done=on_row_analyzed,
            )

            await emit(
                msg_builder_main.job_progress(
                    job_id, session_id, "review", "Running quality review…", 68
                )
            )

            # Per-row review with progress callback
            rows_to_review = len(
                [a for a in ai_result.get("assignments", []) if a.get("fw_row_indices")]
            )
            reviewed_count = 0

            async def on_row_reviewed():
                nonlocal reviewed_count
                reviewed_count += 1
                pct = 68 + int(reviewed_count / max(rows_to_review, 1) * 17)  # 68 → 85
                await emit(
                    msg_builder_main.job_progress(
                        job_id,
                        session_id,
                        "review",
                        f"Reviewing row {reviewed_count}/{rows_to_review}…",
                        pct,
                    )
                )

            assignments = await quality_review_framework_assignments(
                rows=rows_analysis_input,
                fw_rows=fw_rows,
                assignments=ai_result.get("assignments", []),
                framework_name=framework_name,
                user_id=user_id,
                credits=credits,
                on_row_done=on_row_reviewed,
            )

            await emit(
                msg_builder_main.job_progress(
                    job_id, session_id, "saving", "Saving framework assignments…", 88
                )
            )

            for assignment in assignments:
                row_id = assignment.get("row_id")
                fw_indices = assignment.get("fw_row_indices", [])
                if isinstance(fw_indices, int):
                    fw_indices = [fw_indices] if fw_indices >= 0 else []
                row = next(
                    (r for r in rows_for_analysis if r.get("row_id") == row_id), None
                )
                if row:
                    new_entries = [
                        {
                            "requirement": fw_rows[idx].get(req_col, ""),
                            "section": fw_rows[idx].get(sec_col, ""),
                        }
                        for idx in fw_indices
                        if 0 <= idx < len(fw_rows)
                    ]
                    row["values"][new_col_id] = new_entries
                    if new_entries:
                        rows_assigned += 1
                    else:
                        rows_empty += 1

        # Append framework to tracker list and save
        tracker_data.setdefault("frameworks", []).append(
            {"id": framework_id, "name": framework_name}
        )
        save_tracker_file(user_id, tracker_id, tracker_data)

        # Audit log
        (
            actor_user_id,
            actor_email,
            acting_on_behalf_of_user_id,
            acting_on_behalf_of_email,
        ) = build_audit_actor(baseuser)
        log_audit_event(
            action=TRACKER_FRAMEWORK_ADDED,
            endpoint="/tracker/add-framework",
            ip=data.get("_ip", "background"),
            status="success",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=acting_on_behalf_of_user_id,
            acting_on_behalf_of_email=acting_on_behalf_of_email,
            metadata={
                "tracker_id": tracker_id,
                "framework_id": framework_id,
                "framework_name": framework_name,
                "rows_assigned": rows_assigned,
                "rows_empty": rows_empty,
                "job_id": job_id,
            },
        )

        await emit(
            msg_builder_main.job_success(
                job_id, session_id, f"Framework linked: {rows_assigned} rows assigned"
            )
        )

        return {
            "message": "Framework linked and analyzed successfully",
            "tracker_id": tracker_id,
            "framework": {"id": framework_id, "name": framework_name},
            "frameworks_column_id": new_col_id,
            "rows_assigned": rows_assigned,
            "rows_empty": rows_empty,
        }

    except Exception as e:
        await emit(msg_builder_main.job_error(job_id, session_id, str(e)))
        raise


@tracker_bp.route("/tracker/add-framework", methods=["POST"])
@permission_required_body("trackers.framework.add")
async def add_tracker_framework():
    """
    Add a policyhub framework link and auto-populate framework assignments.
    Body: { user_id, tracker_id, framework_id }
    Returns 202 with job_id immediately; AI analysis runs in background.
    """
    try:
        data = request.json
        baseuser = str(data.get("user_id", ""))
        tracker_id = data.get("tracker_id")
        framework_id = data.get("framework_id")

        if not all([baseuser, tracker_id, framework_id]):
            return (
                jsonify(
                    {
                        "error": "Missing required fields: user_id, tracker_id, framework_id"
                    }
                ),
                400,
            )

        # Quick validation: framework exists in S3
        fw_s3_key = f"{FRAMEWORK_OWNER}/frameworks/{framework_id}.yaml"
        fw_data = load_yaml_from_s3(fw_s3_key)
        if not fw_data:
            return (
                jsonify({"error": f"Framework not found in policyhub: {framework_id}"}),
                404,
            )
        framework_name = fw_data.get("name")
        if not framework_name:
            return (
                jsonify(
                    {"error": f"Framework missing name in policyhub: {framework_id}"}
                ),
                500,
            )

        logged_in_user_id, user_id = parse_composite_user_id(baseuser)

        # Quick validation: tracker exists and file loads
        config_path, config_data = check_config_exist(user_id)
        tracker_meta = next(
            (
                t
                for t in (config_data or {}).get("trackers", [])
                if t["tracker_id"] == tracker_id
            ),
            None,
        )
        if not tracker_meta:
            return jsonify({"error": f"Tracker not found: {tracker_id}"}), 404
        tracker_data = read_json_from_s3(tracker_meta["file_path"])
        if not tracker_data:
            return jsonify({"error": "Tracker file not found on storage"}), 404

        # Quick validation: not a duplicate
        if any(fw["id"] == framework_id for fw in tracker_data.get("frameworks", [])):
            return jsonify({"error": "Framework already linked to this tracker"}), 409

        data["_ip"] = request.remote_addr
        job_id = await JobManager.submit_job(_add_framework_worker, data)

        actor_uid, actor_email, behalf_uid, behalf_email = build_audit_actor(baseuser)
        log_audit_event(
            action=TRACKER_FRAMEWORK_ADDED,
            endpoint="/tracker/add-framework",
            ip=request.remote_addr,
            status="accepted",
            actor_user_id=actor_uid,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=behalf_uid,
            acting_on_behalf_of_email=behalf_email,
            metadata={
                "tracker_id": tracker_id,
                "framework_id": framework_id,
                "framework_name": framework_name,
                "job_id": job_id,
            },
        )
        g.audit_logged = True

        return jsonify({"status": "accepted", "job_id": job_id}), 202

    except Exception as e:
        logging.error(f"Add tracker framework error: {traceback.format_exc()}")
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


async def _update_framework_worker(data: dict, job_id: str = None) -> dict:
    """Background worker: re-analyzes and replaces per-framework column data."""
    baseuser = str(data.get("user_id", ""))
    tracker_id = data.get("tracker_id")
    framework_id = data.get("framework_id")
    logged_in_user_id, user_id = parse_composite_user_id(baseuser)
    session_id = data.get("session_id")
    should_emit = bool(job_id and session_id)

    async def emit(msg):
        if should_emit:
            try:
                await ws_service.emit(
                    user_id=user_id,
                    message=msg.get("message"),
                    scope=msg.get("scope", "job"),
                    session_id=msg.get("session_id"),
                    job_id=msg.get("job_id"),
                    msg_type=msg.get("type"),
                    stage=msg.get("stage"),
                    progress=msg.get("progress"),
                    feature="tracker_framework",
                )
            except Exception:
                pass

    try:
        await emit(
            msg_builder_main.job_progress(
                job_id, session_id, "init", "Starting framework re-analysis…", 5
            )
        )

        # Load tracker data
        config_path, config_data = check_config_exist(user_id)
        tracker_meta = next(
            (
                t
                for t in (config_data or {}).get("trackers", [])
                if t["tracker_id"] == tracker_id
            ),
            None,
        )
        if not tracker_meta:
            raise Exception(f"Tracker not found: {tracker_id}")
        tracker_data = read_json_from_s3(tracker_meta["file_path"])
        if not tracker_data:
            raise Exception("Tracker file not found on storage")

        # Verify framework is linked
        framework_entry = next(
            (
                fw
                for fw in tracker_data.get("frameworks", [])
                if fw["id"] == framework_id
            ),
            None,
        )
        if not framework_entry:
            raise Exception(f"Framework not linked to this tracker: {framework_id}")
        old_framework_name = framework_entry["name"]

        # Load framework YAML
        await emit(
            msg_builder_main.job_progress(
                job_id,
                session_id,
                "loading",
                f"Loading {old_framework_name} requirements",
                10,
            )
        )

        fw_s3_key = f"{FRAMEWORK_OWNER}/frameworks/{framework_id}.yaml"
        fw_data = load_yaml_from_s3(fw_s3_key)
        if not fw_data:
            raise Exception(f"Framework not found in policyhub: {framework_id}")
        framework_name = fw_data.get("name")
        if not framework_name:
            raise Exception(f"Framework missing name in policyhub: {framework_id}")

        fw_rows = fw_data.get("rows", [])
        fw_cols = fw_data.get("columns", [])
        req_col = fw_cols[0] if fw_cols else "REQUIREMENT/TASK"
        sec_col = fw_cols[1] if len(fw_cols) > 1 else "SECTION/CATEGORY"

        # Find per-framework column by old name (in case it was renamed in policyhub)
        await emit(
            msg_builder_main.job_progress(
                job_id, session_id, "setup", f"Locating {old_framework_name} column", 20
            )
        )

        schema_cols = tracker_data.get("schema", {}).get("columns", [])
        fw_col = next(
            (
                col
                for col in schema_cols
                if col.get("source_column") == "frameworks"
                and col.get("name") == old_framework_name
            ),
            None,
        )
        if not fw_col:
            raise Exception(f"Framework column not found for: {old_framework_name}")
        fw_col_id = fw_col["id"]

        # Build AI input — exclude all framework columns
        rows_for_analysis = tracker_data.get("rows", [])
        rows_assigned = 0
        rows_empty = 0

        if rows_for_analysis and fw_rows:
            credits = Credits(user_id)
            rows_analysis_input = [
                {
                    "row_id": row.get("row_id"),
                    "col_values": {
                        col.get("name"): row["values"].get(col.get("id"), "")
                        for col in schema_cols
                        if col.get("source_column") != "frameworks"
                    },
                }
                for row in rows_for_analysis
            ]

            await emit(
                msg_builder_main.job_progress(
                    job_id,
                    session_id,
                    "analysis",
                    f"Re-analyzing {len(rows_analysis_input)} rows…",
                    25,
                )
            )

            # Per-row analysis with progress callback
            total_rows = len(rows_analysis_input)
            analyzed_count = 0

            async def on_row_analyzed():
                nonlocal analyzed_count
                analyzed_count += 1
                pct = 25 + int(analyzed_count / total_rows * 55)  # 25 → 80
                await emit(
                    msg_builder_main.job_progress(
                        job_id,
                        session_id,
                        "analysis",
                        f"Analyzing row {analyzed_count}/{total_rows}…",
                        pct,
                    )
                )

            ai_result = await analyze_tracker_framework_rows(
                rows=rows_analysis_input,
                fw_rows=fw_rows,
                framework_id=framework_id,
                framework_name=framework_name,
                user_id=user_id,
                credits=credits,
                on_row_done=on_row_analyzed,
            )

            await emit(
                msg_builder_main.job_progress(
                    job_id, session_id, "saving", "Saving updated assignments…", 88
                )
            )

            for assignment in ai_result.get("assignments", []):
                row_id = assignment.get("row_id")
                fw_indices = assignment.get("fw_row_indices", [])
                if isinstance(fw_indices, int):
                    fw_indices = [fw_indices] if fw_indices >= 0 else []
                row = next(
                    (r for r in rows_for_analysis if r.get("row_id") == row_id), None
                )
                if row:
                    new_entries = [
                        {
                            "requirement": fw_rows[idx].get(req_col, ""),
                            "section": fw_rows[idx].get(sec_col, ""),
                        }
                        for idx in fw_indices
                        if 0 <= idx < len(fw_rows)
                    ]
                    row["values"][fw_col_id] = new_entries
                    if new_entries:
                        rows_assigned += 1
                    else:
                        rows_empty += 1

        # Update column name and framework entry name if policyhub renamed the framework
        if fw_col["name"] != framework_name:
            fw_col["name"] = framework_name
        framework_entry["name"] = framework_name

        save_tracker_file(user_id, tracker_id, tracker_data)

        # Audit log
        (
            actor_user_id,
            actor_email,
            acting_on_behalf_of_user_id,
            acting_on_behalf_of_email,
        ) = build_audit_actor(baseuser)
        log_audit_event(
            action=TRACKER_FRAMEWORK_UPDATED,
            endpoint="/tracker/update-framework",
            ip=data.get("_ip", "background"),
            status="success",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=acting_on_behalf_of_user_id,
            acting_on_behalf_of_email=acting_on_behalf_of_email,
            metadata={
                "tracker_id": tracker_id,
                "framework_id": framework_id,
                "old_name": old_framework_name,
                "new_name": framework_name,
                "rows_reassigned": rows_assigned,
                "rows_empty": rows_empty,
                "job_id": job_id,
            },
        )

        await emit(
            msg_builder_main.job_success(
                job_id,
                session_id,
                f"Framework updated: {rows_assigned} rows reassigned",
            )
        )

        return {
            "message": "Framework reassigned successfully",
            "tracker_id": tracker_id,
            "framework": {"id": framework_id, "name": framework_name},
            "rows_reassigned": rows_assigned,
            "rows_empty": rows_empty,
        }

    except Exception as e:
        await emit(msg_builder_main.job_error(job_id, session_id, str(e)))
        raise


@tracker_bp.route("/tracker/update-framework", methods=["POST"])
@permission_required_body("trackers.framework.edit")
async def update_tracker_framework():
    """
    Re-analyze and sync framework assignments (re-sync with policyhub).
    Body: { user_id, tracker_id, framework_id }
    Returns 202 with job_id immediately; AI analysis runs in background.
    """
    try:
        data = request.json
        baseuser = str(data.get("user_id", ""))
        tracker_id = data.get("tracker_id")
        framework_id = data.get("framework_id")

        if not all([baseuser, tracker_id, framework_id]):
            return (
                jsonify(
                    {
                        "error": "Missing required fields: user_id, tracker_id, framework_id"
                    }
                ),
                400,
            )

        logged_in_user_id, user_id = parse_composite_user_id(baseuser)

        # Quick validation: tracker exists
        config_path, config_data = check_config_exist(user_id)
        tracker_meta = next(
            (
                t
                for t in (config_data or {}).get("trackers", [])
                if t["tracker_id"] == tracker_id
            ),
            None,
        )
        if not tracker_meta:
            return jsonify({"error": f"Tracker not found: {tracker_id}"}), 404
        tracker_data = read_json_from_s3(tracker_meta["file_path"])
        if not tracker_data:
            return jsonify({"error": "Tracker file not found on storage"}), 404

        # Quick validation: framework is linked
        framework_entry = next(
            (
                fw
                for fw in tracker_data.get("frameworks", [])
                if fw["id"] == framework_id
            ),
            None,
        )
        if not framework_entry:
            return (
                jsonify(
                    {"error": f"Framework not linked to this tracker: {framework_id}"}
                ),
                404,
            )

        # Quick validation: framework YAML exists
        fw_s3_key = f"{FRAMEWORK_OWNER}/frameworks/{framework_id}.yaml"
        fw_data = load_yaml_from_s3(fw_s3_key)
        if not fw_data:
            return (
                jsonify({"error": f"Framework not found in policyhub: {framework_id}"}),
                404,
            )

        data["_ip"] = request.remote_addr
        job_id = await JobManager.submit_job(_update_framework_worker, data)

        actor_uid, actor_email, behalf_uid, behalf_email = build_audit_actor(baseuser)
        log_audit_event(
            action=TRACKER_FRAMEWORK_UPDATED,
            endpoint="/tracker/update-framework",
            ip=request.remote_addr,
            status="accepted",
            actor_user_id=actor_uid,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=behalf_uid,
            acting_on_behalf_of_email=behalf_email,
            metadata={
                "tracker_id": tracker_id,
                "framework_id": framework_id,
                "job_id": job_id,
            },
        )
        g.audit_logged = True

        return jsonify({"status": "accepted", "job_id": job_id}), 202

    except Exception as e:
        logging.error(f"Update tracker framework error: {traceback.format_exc()}")
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@tracker_bp.route("/tracker/remove-framework", methods=["DELETE"])
@permission_required_body("trackers.framework.delete")
def remove_tracker_framework():
    """
    Remove a framework link from a tracker and clean up all assignments.
    Body: { user_id, tracker_id, framework_id }
    """
    try:
        data = request.json
        baseuser = str(data.get("user_id"))
        tracker_id = data.get("tracker_id")
        framework_id = data.get("framework_id")

        if not all([baseuser, tracker_id, framework_id]):
            return (
                jsonify(
                    {
                        "error": "Missing required fields: user_id, tracker_id, framework_id"
                    }
                ),
                400,
            )
        logged_in_user_id, user_id = parse_composite_user_id(baseuser)
        # Check tracker exists
        config_path, config_data = check_config_exist(user_id)
        tracker_meta = next(
            (
                t
                for t in (config_data or {}).get("trackers", [])
                if t["tracker_id"] == tracker_id
            ),
            None,
        )
        if not tracker_meta:
            return jsonify({"error": f"Tracker not found: {tracker_id}"}), 404

        tracker_data = read_json_from_s3(tracker_meta["file_path"])
        if not tracker_data:
            return jsonify({"error": "Tracker file not found on storage"}), 404

        # Check framework exists in tracker
        frameworks = tracker_data.get("frameworks", [])
        framework = next((fw for fw in frameworks if fw["id"] == framework_id), None)
        if not framework:
            return (
                jsonify(
                    {"error": f"Framework not linked to this tracker: {framework_id}"}
                ),
                404,
            )

        framework_name = framework.get("name")

        # Remove from tracker-level frameworks list
        tracker_data["frameworks"] = [
            fw for fw in frameworks if fw["id"] != framework_id
        ]

        # Find and remove the per-framework column
        schema_cols = tracker_data.get("schema", {}).get("columns", [])
        fw_col = next(
            (
                col
                for col in schema_cols
                if col.get("source_column") == "frameworks"
                and col.get("name") == framework_name
            ),
            None,
        )
        rows_affected = 0
        column_removed = False

        if fw_col:
            fw_col_id = fw_col["id"]
            for row in tracker_data.get("rows", []):
                if fw_col_id in row["values"]:
                    if row["values"][fw_col_id]:
                        rows_affected += 1
                    del row["values"][fw_col_id]
            tracker_data["schema"]["columns"] = [
                col for col in schema_cols if col.get("id") != fw_col_id
            ]
            column_removed = True

        save_tracker_file(user_id, tracker_id, tracker_data)

        # Audit logging
        (
            actor_user_id,
            actor_email,
            acting_on_behalf_of_user_id,
            acting_on_behalf_of_email,
        ) = build_audit_actor(baseuser)
        log_audit_event(
            action=TRACKER_FRAMEWORK_REMOVED,
            endpoint="/tracker/remove-framework",
            ip=request.remote_addr,
            status="success",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=acting_on_behalf_of_user_id,
            acting_on_behalf_of_email=acting_on_behalf_of_email,
            metadata={
                "tracker_id": tracker_id,
                "framework_id": framework_id,
                "framework_name": framework_name,
                "rows_affected": rows_affected,
                "column_removed": column_removed,
            },
        )
        g.audit_logged = True

        return (
            jsonify(
                {
                    "message": "Framework removed successfully",
                    "tracker_id": tracker_id,
                    "framework_id": framework_id,
                    "rows_affected": rows_affected,
                    "column_removed": column_removed,
                }
            ),
            200,
        )

    except Exception as e:
        logging.error(f"Remove tracker framework error: {traceback.format_exc()}")
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@tracker_bp.route("/tracker/jbs/<job_id>", methods=["GET"])
@permission_required_body("trackers.table.view")
async def tracker_job_status(job_id):
    """
    Poll the status of a background tracker job.
    Returns: { status: queued|processing|completed|failed, data: {...} }
    """
    redisservice = get_redis()
    job = await redisservice.get(f"job:{job_id}")
    if not job:
        return jsonify({"status": "not_found"}), 404
    return jsonify(job)


# ─────────────────────────────────────────────────────────────
# Tracker share / assign access
# ─────────────────────────────────────────────────────────────


def _lookup_tracker_name(owner_id, tracker_id):
    config_path, config_data = check_config_exist(owner_id)
    if not config_data:
        return None
    for t in config_data.get("trackers", []):
        if t.get("tracker_id") == tracker_id:
            return t.get("name")
    return None


@tracker_bp.route("/tracker/share", methods=["POST"])
@permission_required_body("trackers.table.edit")
def share_tracker():
    """Assign a tracker to a user (manual) or pick one via round-robin (role)."""
    data = request.get_json() or {}
    baseuser = data.get("user_id")
    tracker_id = data.get("tracker_id")
    tracker_name = data.get("tracker_name")
    assignment_type = data.get("assignment_type")
    client_user_id = data.get("client_user_id")
    role_id = data.get("role_id")

    if not baseuser or not tracker_id or not assignment_type:
        return (
            jsonify({"error": "user_id, tracker_id, assignment_type required"}),
            400,
        )

    _, admin_id = parse_composite_user_id(baseuser)
    if not admin_id:
        return jsonify({"error": "Invalid user_id"}), 400

    if not tracker_name:
        tracker_name = _lookup_tracker_name(admin_id, tracker_id) or tracker_id

    conn = None
    try:
        conn = connect_to_rds()
        required_permission = "trackers.table.view"

        if assignment_type == "manual":
            if not client_user_id:
                return jsonify({"error": "client_user_id required for manual"}), 400
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute("SELECT email FROM users WHERE user_id=%s", (client_user_id,))
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "User not found"}), 404
                user_email = row["email"]

        elif assignment_type == "role":
            if not role_id:
                return jsonify({"error": "role_id required for role"}), 400
            if not check_role_has_permission(conn, admin_id, role_id, required_permission):
                return (
                    jsonify({"error": "Role does not have tracker view permission"}),
                    403,
                )
            user_obj, error_msg = get_round_robin_user_for_resource(
                admin_id, role_id, "tracker", conn, required_permission
            )
            if not user_obj:
                return jsonify({"error": error_msg or "No eligible users"}), 400
            client_user_id = user_obj["user_id"]
            user_email = user_obj["email"]
        else:
            return jsonify({"error": "assignment_type must be 'manual' or 'role'"}), 400

        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT email FROM users WHERE user_id=%s", (admin_id,))
            admin_row = cur.fetchone()
            if not admin_row:
                return jsonify({"error": "Admin not found"}), 404
            admin_email = admin_row["email"]

        sharing_access, error = core_assign_resource(
            "tracker",
            admin_id,
            admin_email,
            client_user_id,
            user_email,
            tracker_id,
            tracker_name,
            conn,
        )
        if error:
            return (
                jsonify({"error": error}),
                403 if "permission" in error.lower() else 400,
            )

        (
            actor_user_id,
            actor_email,
            acting_on_behalf_of_user_id,
            acting_on_behalf_of_email,
        ) = build_audit_actor(baseuser)
        log_audit_event(
            action=TRACKER_SHARED,
            endpoint="/tracker/share",
            ip=request.remote_addr,
            status="success",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=acting_on_behalf_of_user_id,
            acting_on_behalf_of_email=acting_on_behalf_of_email,
            metadata={
                "tracker_id": tracker_id,
                "target_user_id": client_user_id,
                "assignment_type": assignment_type,
                "role_id": role_id,
            },
        )
        g.audit_logged = True

        return (
            jsonify(
                {
                    "success": True,
                    "tracker_id": tracker_id,
                    "client_user_id": client_user_id,
                    "sharing_access": sharing_access,
                }
            ),
            200,
        )

    except Exception as e:
        logging.error(f"share_tracker error: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@tracker_bp.route("/tracker/revoke-share", methods=["POST"])
@permission_required_body("trackers.table.edit")
def revoke_tracker_share():
    data = request.get_json() or {}
    baseuser = data.get("user_id")
    client_user_id = data.get("client_user_id")
    tracker_id = data.get("tracker_id")

    if not baseuser or not client_user_id or not tracker_id:
        return (
            jsonify({"error": "user_id, client_user_id, tracker_id required"}),
            400,
        )

    _, admin_id = parse_composite_user_id(baseuser)
    if not admin_id:
        return jsonify({"error": "Invalid user_id"}), 400

    try:
        sharing_access, error = core_revoke_resource(
            "tracker", admin_id, client_user_id, tracker_id
        )
        if error:
            return jsonify({"error": error}), 400

        (
            actor_user_id,
            actor_email,
            acting_on_behalf_of_user_id,
            acting_on_behalf_of_email,
        ) = build_audit_actor(baseuser)
        log_audit_event(
            action=TRACKER_SHARE_REVOKED,
            endpoint="/tracker/revoke-share",
            ip=request.remote_addr,
            status="success",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=acting_on_behalf_of_user_id,
            acting_on_behalf_of_email=acting_on_behalf_of_email,
            metadata={
                "tracker_id": tracker_id,
                "target_user_id": client_user_id,
            },
        )
        g.audit_logged = True

        return (
            jsonify({"success": True, "sharing_access": sharing_access}),
            200,
        )
    except Exception as e:
        logging.error(f"revoke_tracker_share error: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


@tracker_bp.route("/tracker/sharing/<tracker_id>", methods=["GET"])
@permission_required_body("trackers.table.view")
def get_tracker_sharing(tracker_id):
    baseuser = request.args.get("user_id")
    if not baseuser:
        return jsonify({"error": "user_id query param required"}), 400

    _, admin_id = parse_composite_user_id(baseuser)
    if not admin_id:
        return jsonify({"error": "Invalid user_id"}), 400

    try:
        sharing_access, _ = core_list_resource_shares("tracker", admin_id, tracker_id)
        return jsonify({"sharing_access": sharing_access}), 200
    except Exception as e:
        logging.error(f"get_tracker_sharing error: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


@tracker_bp.route("/tracker/shared", methods=["GET"])
@permission_required_body("trackers.table.view")
def list_shared_trackers():
    """List trackers shared TO the requesting user."""
    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id required"}), 400

    logged_in_user_id, target_user_id = parse_composite_user_id(user_id)
    requester = logged_in_user_id or target_user_id

    try:
        shared = get_user_shared_resources(requester, "tracker")
        return (
            jsonify({"user_id": requester, "shared_trackers": list(shared.values())}),
            200,
        )
    except Exception as e:
        logging.error(f"list_shared_trackers error: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500
