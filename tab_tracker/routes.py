import logging
import os
import traceback
import json
import uuid

from db.lance_db_service import LanceDBServer
from flask import Blueprint, jsonify, request, session
from services.redis_service import RedisService
from tab_tracker.helper import (
    check_config_exist,
    create_empty_tracker_config,
    create_tracker_config,
    create_tracker_file,
    delete_tracker_config,
    append_to_tracker,
    save_tracker_file,
    ensure_tracker_file_exists,
    apply_entry_updates,
    _update_or_append_entries,
    _rebuild_micro_blocks_from_tracker,
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
        result_id = data.get("result_id")  # Optional: if provided, populate with data

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
        runbook = await dbserver.get_runbook_by_id(user_id=user_id, runbook_id=runbook_id)

        if not runbook:
            return jsonify({"error": f"Runbook not found: {runbook_id}"}), 404

        if isinstance(runbook, list):
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
                result_row = await dbserver.runbook_get_result(user_id=user_id, result_id=result_id)

                if result_row and result_row.get("status") != "not_found" and result_row.get("status") != "running":
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
                            append_metadata = append_to_tracker(tracker_data, result_block, result_id)

                            # Record after state
                            after = {
                                "rows": len(tracker_data.get("rows", [])),
                                "cells": len(tracker_data.get("cells", [])),
                                "records": len(tracker_data.get("records", [])),
                            }

                            data_appended = {k: after[k] - before[k] for k in before}

                            # Save updated tracker
                            save_tracker_file(user_id, tracker_id, tracker_data)

                            logging.info(f"Tracker {tracker_id} created with {data_appended} data from result {result_id}")
                        else:
                            logging.warning(f"Block {block_id} not found in result {result_id}")
                    else:
                        logging.warning(f"Result {result_id} has no content")
                else:
                    logging.warning(f"Result {result_id} not found or still running")
            except Exception as e:
                logging.error(f"Failed to append data during tracker creation: {str(e)}")
                # Don't fail the tracker creation, just log the error

        updates_runbook = {
                    "tracker_configuration": {
                        block_id : tracker_id
                    }
                }
        update = await dbserver.update_runbook(user_id=user_id,
                                               runbook_id=runbook_id,
                                               updates=updates_runbook)
        print("tracker_configuration updated")
        if not update:
            return jsonify({"error": f"Not able to update tracker_id in runbook: {runbook_id}"}), 404

        response_data = {
            "message": "Tracker created successfully",
            "tracker_id": tracker_id,
        }

        # Include data appended information if result_id was provided
        if result_id:
            response_data["data_appended"] = data_appended
            response_data["result_id"] = result_id

        return jsonify(response_data), 200

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
async def get_tracker_details_api():
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
                runbook = await dbserver.get_runbook_by_id(user_id=user_id, runbook_id=runbook_id)

                if runbook:
                    if isinstance(runbook, list):
                        runbook = runbook[0]

                    raw = runbook.get("structure_theme")
                    if raw:
                        try:
                            parsed = json.loads(raw) if isinstance(raw, str) else raw
                            if isinstance(parsed, str):
                                parsed = json.loads(parsed)

                            blocks = parsed.get("blocks", []) if isinstance(parsed, dict) else parsed

                            target_block = None
                            for b in blocks:
                                if isinstance(b, dict) and b.get("block_id") == block_id:
                                    target_block = b
                                    break

                            if target_block:
                                block_config = extract_block_schema(target_block, tracker_type)
                                _, tracker_data = ensure_tracker_file_exists(
                                    user_id=user_id,
                                    tracker_id=tracker_id,
                                    tracker_type=tracker_type,
                                    runbook_id=runbook_id,
                                    block_config=block_config
                                )
                                logging.info(f"Auto-initialized tracker {tracker_id}")
                        except Exception as e:
                            logging.warning(f"Failed to extract block schema: {e}")
            except Exception as e:
                logging.warning(f"Failed to auto-initialize tracker from runbook: {e}")

        if not tracker_data:
            return jsonify({
                "error": "Tracker file not initialized. Use /tracker/append to initialize it.",
                "tracker_id": tracker_id,
                "tracker_meta": tracker_meta,
                "file_initialized": file_initialized
            }), 404

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
            "file_initialized": file_initialized,
        }), 200

    except Exception as e:
        logging.error(f"Get tracker details error: {traceback.format_exc()}")
        return jsonify({"error": str(e),
                        "trace": traceback.format_exc()}), 500


@tracker_bp.route("/tracker/check-duplicate", methods=["GET"])
def check_duplicate_result_api():
    try:
        user_id = str(request.args.get("user_id"))
        result_id = request.args.get("result_id")
        block_id = request.args.get("block_id")

        if not all([user_id, result_id, block_id]):
            return jsonify({"error": "Missing required parameters: user_id, result_id, block_id"}), 400

        # Fetch tracker config
        config_path, config_data = check_config_exist(user_id)

        if not config_data:
            return jsonify({
                "found": False,
                "message": "No trackers found for this user",
                "result_id": result_id,
                "block_id": block_id
            }), 200

        trackers = config_data.get("trackers", [])
        matches = []

        # Check each tracker to see if it has data from this result_id and block_id
        for tracker_meta in trackers:
            tracker_id = tracker_meta.get("tracker_id")
            tracker_path = tracker_meta.get("file_path", f"{user_id}/tracker/{tracker_id}/tracker.json")
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
                matches.append({
                    "tracker_id": tracker_id,
                    "tracker_name": tracker_meta.get("name"),
                    "tracker_type": tracker_type,
                    "runbook_id": tracker_meta.get("runbook_id"),
                    "block_id": block_id,
                    "result_id": result_id,
                    "tracker_path": tracker_path
                })

        if matches:
            return jsonify({
                "found": True,
                "message": f"Found {len(matches)} tracker(s) with this result and block",
                "result_id": result_id,
                "block_id": block_id,
                "trackers": matches
            }), 200
        else:
            return jsonify({
                "found": False,
                "message": "No trackers found with this result_id and block_id combination",
                "result_id": result_id,
                "block_id": block_id
            }), 200

    except Exception as e:
        logging.error(f"Check duplicate error: {traceback.format_exc()}")
        return jsonify({"error": str(e),
                        "trace": traceback.format_exc()}), 500


@tracker_bp.route("/tracker/view", methods=["GET"])
def view_tracker_content_api():
    try:
        user_id = str(request.args.get("user_id"))
        tracker_id = request.args.get("tracker_id")
        limit = int(request.args.get("limit", 100))
        offset = int(request.args.get("offset", 0))

        if not all([user_id, tracker_id]):
            return jsonify({"error": "Missing required parameters: user_id, tracker_id"}), 400

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

        tracker_path = tracker_meta.get("file_path", f"{user_id}/tracker/{tracker_id}/tracker.json")
        tracker_type = tracker_meta.get("type")

        # Fetch tracker data
        tracker_data = read_json_from_s3(tracker_path)

        if not tracker_data:
            return jsonify({"error": f"Tracker file not found: {tracker_id}"}), 404

        schema = tracker_data.get("schema", {})
        source_blocks = tracker_data.get("source_blocks", [])

        # Format response based on tracker type
        if tracker_type == "table":
            rows = tracker_data.get("rows", [])
            total_rows = len(rows)

            # Pagination
            paginated_rows = rows[offset:offset + limit]

            return jsonify({
                "user_id": user_id,
                "tracker_id": tracker_id,
                "type": "table",
                "schema": {
                    "columns": schema.get("columns", [])
                },
                "data": {
                    "rows": paginated_rows,
                    "total": total_rows,
                    "offset": offset,
                    "limit": limit,
                    "has_more": (offset + limit) < total_rows
                },
                "metadata": {
                    "source_blocks": source_blocks,
                    "source_block_count": len(source_blocks)
                }
            }), 200

        elif tracker_type == "matrix":
            cells = tracker_data.get("cells", [])
            total_cells = len(cells)

            # Pagination
            paginated_cells = cells[offset:offset + limit]

            return jsonify({
                "user_id": user_id,
                "tracker_id": tracker_id,
                "type": "matrix",
                "schema": {
                    "rows": schema.get("rows", []),
                    "columns": schema.get("columns", []),
                    "cell_value_label": schema.get("cell_value_label", "Value")
                },
                "data": {
                    "cells": paginated_cells,
                    "total": total_cells,
                    "offset": offset,
                    "limit": limit,
                    "has_more": (offset + limit) < total_cells
                },
                "metadata": {
                    "source_blocks": source_blocks,
                    "source_block_count": len(source_blocks)
                }
            }), 200

        elif tracker_type == "scorecard":
            records = tracker_data.get("records", [])
            total_records = len(records)

            # Pagination
            paginated_records = records[offset:offset + limit]

            return jsonify({
                "user_id": user_id,
                "tracker_id": tracker_id,
                "type": "scorecard",
                "schema": {
                    "metrics": schema.get("metrics", [])
                },
                "data": {
                    "records": paginated_records,
                    "total": total_records,
                    "offset": offset,
                    "limit": limit,
                    "has_more": (offset + limit) < total_records
                },
                "metadata": {
                    "source_blocks": source_blocks,
                    "source_block_count": len(source_blocks)
                }
            }), 200

        else:
            return jsonify({"error": f"Unknown tracker type: {tracker_type}"}), 400

    except ValueError as ve:
        return jsonify({"error": f"Invalid parameter: {str(ve)}"}), 400

    except Exception as e:
        logging.error(f"View tracker error: {traceback.format_exc()}")
        return jsonify({"error": str(e),
                        "trace": traceback.format_exc()}), 500


@tracker_bp.route("/tracker/append", methods=["POST"])
async def append_tracker_api():
    try:
        data = request.json

        user_id = str(data.get("user_id"))
        tracker_id = data.get("tracker_id")
        result_id = data.get("result_id")
        block_id = data.get("block_id")
        block_title = data.get("block_title", "")

        if not all([user_id, tracker_id, result_id, block_id]):
            return jsonify({"error": "Missing required fields: user_id, tracker_id, result_id, block_id"}), 400

        # STEP 1: Fetch the runbook result from LanceDB
        result_row = await dbserver.runbook_get_result(user_id=user_id, result_id=result_id)

        if not result_row or result_row.get("status") == "not_found":
            return jsonify({"error": f"Result not found or not completed: {result_id}"}), 404

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
            return jsonify({
                "error": f"Block '{block_id}' not found in result '{result_id}'",
                "available_blocks": [b.get("block_id") for b in result_blocks]
            }), 404

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
            block_config=block_config
        )

        if not file_existed:
            logging.info(f"Tracker file auto-initialized for {tracker_id}")

        # STEP 6: Validate block structure before append
        logging.info(f"Block structure for {block_id}: {json.dumps({k: v for k, v in target_block.items() if k not in ['content', 'text', 'narrative']}, indent=2)}")

        if tracker_type == "table":
            headers = target_block.get("headers")
            rows = target_block.get("rows")
            logging.info(f"Table block - Headers: {headers}, Row count: {len(rows) if rows else 0}")
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

        # STEP 8: Save updated tracker back to S3
        save_tracker_file(user_id, tracker_id, tracker_data)

        #STEP 9: save the corresponding blockid and tracked id in runbook table
        updates_runbook = {
                    "tracker_configuration": {
                        block_id : tracker_id
                    }
                }
        update = await dbserver.update_runbook(user_id=user_id,
                                               runbook_id=runbook_id,
                                               updates=updates_runbook)
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
                **{k: v for k, v in after.items() if v > 0}
            }
        }

        # Include discrepancy information
        if tracker_type == "table" and "column_discrepancies" in append_metadata:
            discrepancies = append_metadata["column_discrepancies"]
            response_data["schema_changes"] = {
                "type": "column_discrepancies",
                "matched_columns": discrepancies.get("matched_columns", []),
                "new_columns_created": discrepancies.get("new_columns_created", []),
                "total_columns_in_schema": discrepancies.get("total_columns_in_schema"),
                "summary": f"Matched {len(discrepancies.get('matched_columns', []))} existing column(s), created {len(discrepancies.get('new_columns_created', []))} new column(s)"
            }
            response_data["deduplication"] = {
                "rows_appended": append_metadata.get("rows_appended", 0),
                "rows_skipped_dedup": append_metadata.get("rows_skipped_dedup", 0)
            }

        elif tracker_type == "matrix" and "axis_discrepancies" in append_metadata:
            discrepancies = append_metadata["axis_discrepancies"]
            response_data["schema_changes"] = {
                "type": "axis_changes",
                "new_rows": discrepancies.get("new_rows", []),
                "new_columns": discrepancies.get("new_columns", []),
                "total_rows": discrepancies.get("total_rows"),
                "total_columns": discrepancies.get("total_columns"),
                "summary": f"Added {len(discrepancies.get('new_rows', []))} row(s), added {len(discrepancies.get('new_columns', []))} column(s) to matrix axes"
            }
            response_data["deduplication"] = {
                "cells_appended": append_metadata.get("cells_appended", 0),
                "cells_skipped_dedup": append_metadata.get("cells_skipped_dedup", 0)
            }

        elif tracker_type == "scorecard" and "metric_discrepancies" in append_metadata:
            discrepancies = append_metadata["metric_discrepancies"]
            response_data["schema_changes"] = {
                "type": "metric_changes",
                "new_metrics": discrepancies.get("new_metrics", []),
                "total_metrics": discrepancies.get("total_metrics"),
                "summary": f"Created {len(discrepancies.get('new_metrics', []))} new metric(s)"
            }
            response_data["deduplication"] = {
                "records_appended": append_metadata.get("records_appended", 0),
                "records_skipped_dedup": append_metadata.get("records_skipped_dedup", 0)
            }

        return jsonify(response_data), 200

    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400

    except Exception as e:
        logging.error(f"Append tracker error: {traceback.format_exc()}")
        return jsonify({"error": str(e),
                        "trace": traceback.format_exc()}), 500

@tracker_bp.route("/tracker/sync-from-block", methods=["POST"])
async def sync_block_to_tracker_api():
    """
    Direction 1: After a runbook block is edited, call this endpoint to update
    the linked tracker with the new block content. Updates existing entries for
    the same result_id instead of duplicating them. Falls back to append if no
    existing entries are found.

    Body: { user_id, tracker_id, result_id, block_id }
    """
    try:
        data = request.json
        user_id = str(data.get("user_id"))
        tracker_id = data.get("tracker_id")
        result_id = data.get("result_id")
        block_id = data.get("block_id")

        if not all([user_id, tracker_id, result_id, block_id]):
            return jsonify({"error": "Missing required fields: user_id, tracker_id, result_id, block_id"}), 400

        result_row = await dbserver.runbook_get_result(user_id=user_id, result_id=result_id)
        if not result_row or result_row.get("status") == "not_found":
            return jsonify({"error": f"Result not found or not completed: {result_id}"}), 404

        result_content = result_row.get("result")
        if not result_content:
            return jsonify({"error": "Result has no content"}), 404

        target_block = next((b for b in result_content.get("blocks", []) if b.get("block_id") == block_id), None)
        if not target_block:
            return jsonify({"error": f"Block '{block_id}' not found in result '{result_id}'"}), 404

        config_path, config_data = check_config_exist(user_id)
        tracker_meta = next(
            (t for t in (config_data or {}).get("trackers", []) if t["tracker_id"] == tracker_id),
            None
        )
        if not tracker_meta:
            return jsonify({"error": f"Tracker not found: {tracker_id}"}), 404

        tracker_data = read_json_from_s3(tracker_meta["file_path"])
        if not tracker_data:
            return jsonify({"error": "Tracker file not found on storage"}), 404

        _update_or_append_entries(tracker_data, target_block, result_id)
        save_tracker_file(user_id, tracker_id, tracker_data)

        return jsonify({
            "message": "Tracker synced from block successfully",
            "tracker_id": tracker_id,
            "result_id": result_id,
            "block_id": block_id,
        }), 200

    except Exception as e:
        logging.error(f"Sync block to tracker error: {traceback.format_exc()}")
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@tracker_bp.route("/tracker/modify", methods=["POST"])
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
        user_id = str(data.get("user_id"))
        tracker_id = data.get("tracker_id")
        result_id = data.get("result_id")
        block_id = data.get("block_id")
        entry_updates = data.get("entry_updates", [])

        if not all([user_id, tracker_id, result_id, block_id]):
            return jsonify({"error": "Missing required fields: user_id, tracker_id, result_id, block_id"}), 400

        config_path, config_data = check_config_exist(user_id)
        tracker_meta = next(
            (t for t in (config_data or {}).get("trackers", []) if t["tracker_id"] == tracker_id),
            None
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
        result_row = await dbserver.runbook_get_result(user_id=user_id, result_id=result_id)
        if result_row and result_row.get("status") == "completed":
            result_content = result_row.get("result", {})
            blocks = result_content.get("blocks", [])
            target_block = next((b for b in blocks if b.get("block_id") == block_id), None)
            if target_block:
                target_block["micro_blocks"] = _rebuild_micro_blocks_from_tracker(tracker_data, result_id, block_id)
                await dbserver.update_runbook_result(user_id, result_id, result_content)

        return jsonify({
            "message": "Tracker modified and runbook block updated",
            "tracker_id": tracker_id,
            "result_id": result_id,
            "block_id": block_id,
        }), 200

    except Exception as e:
        logging.error(f"Modify tracker error: {traceback.format_exc()}")
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@tracker_bp.route("/tracker/add-entry", methods=["POST"])
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
        user_id = str(data.get("user_id"))
        tracker_id = data.get("tracker_id")
        result_id = data.get("result_id")

        if not all([user_id, tracker_id, result_id]):
            return jsonify({"error": "Missing required fields: user_id, tracker_id, result_id"}), 400

        config_path, config_data = check_config_exist(user_id)
        tracker_meta = next(
            (t for t in (config_data or {}).get("trackers", []) if t["tracker_id"] == tracker_id),
            None
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
                return jsonify({"error": "Table requires row_data (dict of col_id: value)"}), 400

            row_id = f"trk_r_{uuid.uuid4().hex[:8]}"
            new_row = {
                "row_id": row_id,
                "result_id": result_id,
                "source": {
                    "block_id": "manual",
                    "micro_id": None,
                    "row_index": len(tracker_data.get("rows", []))
                },
                "values": row_data,
                "last_updated_from": "manual"
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
                (c for c in cells if c.get("result_id") == result_id and c.get("row") == row_label and c.get("column") == col_label),
                None
            )

            if existing_cell:
                existing_cell["value"] = value
                entry_added = {"type": "cell", "action": "updated", "row": row_label, "column": col_label}
            else:
                new_cell = {
                    "row": row_label,
                    "column": col_label,
                    "value": value,
                    "result_id": result_id
                }
                tracker_data.setdefault("cells", []).append(new_cell)
                entry_added = {"type": "cell", "action": "created", "row": row_label, "column": col_label}

        # Scorecard: add or update record
        elif tracker_type == "scorecard":
            metric = data.get("metric")
            value = data.get("value")

            if not metric or value is None:
                return jsonify({"error": "Scorecard requires metric and value"}), 400

            records = tracker_data.get("records", [])
            existing_record = next(
                (r for r in records if r.get("result_id") == result_id and r.get("metric") == metric),
                None
            )

            if existing_record:
                existing_record["value"] = value
                entry_added = {"type": "record", "action": "updated", "metric": metric}
            else:
                new_record = {
                    "metric": metric,
                    "value": value,
                    "result_id": result_id
                }
                tracker_data.setdefault("records", []).append(new_record)
                entry_added = {"type": "record", "action": "created", "metric": metric}

        else:
            return jsonify({"error": f"Unsupported tracker type: {tracker_type}"}), 400

        save_tracker_file(user_id, tracker_id, tracker_data)

        return jsonify({
            "message": "Entry added to tracker successfully",
            "tracker_id": tracker_id,
            "result_id": result_id,
            "entry": entry_added
        }), 200

    except Exception as e:
        logging.error(f"Add tracker entry error: {traceback.format_exc()}")
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


