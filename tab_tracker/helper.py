import uuid

from utils.normal import ensure_dir
import os
import json

from utils.s3_utils import delete_file_from_s3, read_json_from_s3, upload_any_file

def _get_local_tmp_path():
    path = "data/tmp_json/config_tracker.json"
    ensure_dir(os.path.dirname(path))
    return path

def check_config_exist(user_id):
    config_path = f"{user_id}/tracker/config_tracker.json"
    config_data = read_json_from_s3(config_path)
    return config_path,config_data

def create_empty_tracker_config(user_id):
    config_data = {
        "user_id": user_id,
        "trackers": []
    }

    filename = "config_tracker.json"
    local_path = f"/tmp/{filename}"
    s3_key = f"{user_id}/tracker/{filename}"

    with open(local_path, "w") as f:
        json.dump(config_data, f, indent=2)

    upload_any_file(
        file_path=local_path,
        user_id=user_id,
        file_name=s3_key,
        type="tracker"
    )

    os.remove(local_path)

    return s3_key 

def create_tracker_config(
    config_path,
    user_id,
    name,
    tracker_type,
    runbook_id
):
    local_path = _get_local_tmp_path()
    user_id = str(user_id)

    # Step 1: Read config
    config_data = read_json_from_s3(config_path)
    if not config_data:
        config_data = {
            "user_id": user_id,
            "trackers": []
        }

    # Step 2: Prevent duplicate name
    for t in config_data["trackers"]:
        if t["name"] == name:
            raise ValueError("Tracker with same name already exists")

    # Step 3: Create tracker entry
    tracker_id = f"trk_{uuid.uuid4().hex[:6]}"
    file_path = f"{user_id}/tracker/{tracker_id}/tracker.json"

    new_tracker = {
        "tracker_id": tracker_id,
        "name": name,
        "type": tracker_type,  # table | matrix | scorecard
        "runbook_id": runbook_id,
        "file_path": file_path
    }

    config_data["trackers"].append(new_tracker)

    # Step 4: Save locally
    with open(local_path, "w") as f:
        json.dump(config_data, f, indent=2)

    # Step 5: Upload
    upload_any_file(
        file_path=local_path,
        user_id=user_id,
        file_name=config_path,
        type="tracker"
    )

    os.remove(local_path)

    return tracker_id, file_path

def update_tracker_config(
    config_path,
    user_id,
    tracker_id,
    updates
):
    local_path = _get_local_tmp_path()
    user_id = str(user_id)

    config_data = read_json_from_s3(config_path)
    if not config_data:
        raise ValueError("Config not found")

    found = False

    for tracker in config_data.get("trackers", []):
        if tracker["tracker_id"] == tracker_id:
            tracker.update(updates)
            found = True
            break

    if not found:
        raise ValueError("Tracker not found")

    with open(local_path, "w") as f:
        json.dump(config_data, f, indent=2)

    upload_any_file(
        file_path=local_path,
        user_id=user_id,
        file_name=config_path,
        type="tracker"
    )

    os.remove(local_path)

    return True

def delete_tracker_config(
    config_path,
    user_id,
    tracker_id
):
    local_path = _get_local_tmp_path()
    user_id = str(user_id)

    config_data = read_json_from_s3(config_path)
    if not config_data:
        raise ValueError("Config not found")

    trackers = config_data.get("trackers", [])

    tracker_to_delete = None

    for t in trackers:
        if t["tracker_id"] == tracker_id:
            tracker_to_delete = t
            break

    if not tracker_to_delete:
        raise ValueError("Tracker not found")

    # Step 1: Remove from config
    config_data["trackers"] = [
        t for t in trackers if t["tracker_id"] != tracker_id
    ]

    # Step 2: Save config
    with open(local_path, "w") as f:
        json.dump(config_data, f, indent=2)

    upload_any_file(
        file_path=local_path,
        user_id=user_id,
        file_name=config_path,
        type="tracker"
    )

    os.remove(local_path)

    # Step 3: Delete tracker file (IMPORTANT)
    delete_file_from_s3(f"{user_id}/tracker/{tracker_id}.json")

    return True


def create_tracker_file(
    user_id,
    tracker_id,
    tracker_type,   # table | matrix | scorecard
    runbook_id,
    block_config=None,  # Optional: block schema from runbook
):
    """
    Create tracker file with schema initialized from block configuration.

    block_config structure varies by tracker_type:
    - table: {"columns": [...]}
    - matrix: {"x_axis": {...}, "y_axis": {...}, "cell_value": "..."}
    - scorecard: {"metrics": [...]}
    """
    local_path = f"/tmp/{tracker_id}_tracker.json"
    s3_path = f"{user_id}/tracker/{tracker_id}/tracker.json"

    # Step 1: Base structure
    tracker_data = {
        "tracker_id": tracker_id,
        "type": tracker_type,
        "runbook_id": runbook_id,
        "source_blocks": []
    }

    # Step 2: Type-specific initialization with schema from block_config

    # ✅ TABLE
    if tracker_type == "table":
        columns = []
        if block_config and "columns" in block_config:
            # Initialize columns from block schema
            for idx, col in enumerate(block_config["columns"]):
                columns.append({
                    "id": f"col_{idx + 1}",
                    "name": col.get("name", f"Column {idx + 1}"),
                    "type": col.get("type", "text"),
                    "source_column": col.get("name", f"Column {idx + 1}"),
                    "enum": col.get("enum", [])
                })

        tracker_data.update({
            "schema": {
                "columns": columns
            },
            "rows": []
        })

    # ✅ MATRIX
    elif tracker_type == "matrix":
        rows = []
        columns = []
        cell_value_label = "Value"

        if block_config:
            # Extract x_axis (columns) and y_axis (rows) from matrix schema
            if "x_axis" in block_config:
                columns = block_config["x_axis"].get("values", [])
            if "y_axis" in block_config:
                rows = block_config["y_axis"].get("values", [])
            if "cell_value" in block_config:
                cell_value_label = block_config["cell_value"]

        tracker_data.update({
            "schema": {
                "rows": rows,
                "columns": columns,
                "cell_value_label": cell_value_label
            },
            "cells": []
        })

    # ✅ SCORECARD
    elif tracker_type == "scorecard":
        metrics = []
        if block_config and "metrics" in block_config:
            # Initialize metrics from block schema
            for metric in block_config["metrics"]:
                metric_name = metric if isinstance(metric, str) else metric.get("name", "")
                metrics.append(metric_name)

        tracker_data.update({
            "schema": {
                "metrics": metrics
            },
            "records": []
        })

    else:
        raise ValueError("Invalid tracker type")

    # Step 3: Write locally
    with open(local_path, "w") as f:
        json.dump(tracker_data, f, indent=2)

    # Step 4: Upload to S3
    upload_any_file(
        file_path=local_path,
        user_id=user_id,
        file_name=s3_path,
        type="tracker"
    )

    # Step 5: Cleanup
    try:
        os.remove(local_path)
    except Exception as e:
        print(f"⚠️ Failed to delete temp file: {e}")

    return s3_path

def append_table(tracker, block, result_id):
    """
    block = {
        "block_id": "...",
        "block_title": "...",
        "headers": [...],
        "rows": [...]
    }
    """

    schema_cols = tracker["schema"]["columns"]

    # Build column map: source_column → col_id
    col_map = {col["source_column"]: col["id"] for col in schema_cols}

    # Step 1: Add missing columns (schema evolution)
    for header in block.get("headers", []):
        if header not in col_map:
            new_col_id = f"col_{len(schema_cols) + 1}"

            new_col = {
                "id": new_col_id,
                "name": header,
                "source_column": header
            }

            schema_cols.append(new_col)
            col_map[header] = new_col_id

    # Step 2: Append rows
    for idx, row in enumerate(block.get("rows", [])):

        micro_id = row.get("micro_id")

        # 🔒 Dedup check
        exists = any(
            r["result_id"] == result_id and
            r["source"]["block_id"] == block["block_id"] and
            r["source"]["micro_id"] == micro_id
            for r in tracker["rows"]
        )
        if exists:
            continue

        values = {}

        # Safe mapping (no index-based mapping)
        for key, val in row.items():
            if key in col_map:
                values[col_map[key]] = val
            else:
                # handle unexpected key (new column)
                new_col_id = f"col_{len(schema_cols) + 1}"

                schema_cols.append({
                    "id": new_col_id,
                    "name": key,
                    "source_column": key
                })

                col_map[key] = new_col_id
                values[new_col_id] = val

        tracker["rows"].append({
            "row_id": f"trk_r_{uuid.uuid4().hex[:8]}",
            "result_id": result_id,
            "source": {
                "block_id": block["block_id"],
                "micro_id": micro_id,
                "row_index": idx
            },
            "values": values,
            "last_updated_from": "report"
        })

    # Step 3: Update source_blocks
    if block["block_id"] not in [b["block_id"] for b in tracker["source_blocks"]]:
        tracker["source_blocks"].append({
            "block_id": block["block_id"],
            "block_title": block.get("block_title", "")
        })

def append_matrix(tracker, block, result_id):
    """
    block = {
        "block_id": "...",
        "block_title": "...",
        "data": [
            {"row": "...", "column": "...", "value": ...}
        ]
    }
    """

    schema = tracker["schema"]

    for entry in block.get("data", []):
        row_key = entry.get("row")
        col_key = entry.get("column")
        value = entry.get("value")

        if row_key is None or col_key is None:
            continue  # skip invalid entries

        # Step 1: Schema evolution
        if row_key not in schema["rows"]:
            schema["rows"].append(row_key)

        if col_key not in schema["columns"]:
            schema["columns"].append(col_key)

        # Step 2: Dedup check
        exists = any(
            c["row"] == row_key and
            c["column"] == col_key and
            c["result_id"] == result_id
            for c in tracker["cells"]
        )
        if exists:
            continue

        # Step 3: Append
        tracker["cells"].append({
            "row": row_key,
            "column": col_key,
            "value": value,
            "result_id": result_id
        })

    # Step 4: Source tracking
    if block["block_id"] not in [b["block_id"] for b in tracker["source_blocks"]]:
        tracker["source_blocks"].append({
            "block_id": block["block_id"],
            "block_title": block.get("block_title", "")
        })


def append_scorecard(tracker, block, result_id):
    """
    block = {
        "block_id": "...",
        "block_title": "...",
        "data": [
            {"metric": "...", "value": ...}
        ]
    }
    """

    schema = tracker["schema"]

    for entry in block.get("data", []):
        metric = entry.get("metric")
        value = entry.get("value")

        if not metric:
            continue

        # Step 1: Schema evolution
        if metric not in schema["metrics"]:
            schema["metrics"].append(metric)

        # Step 2: Dedup check
        exists = any(
            r["metric"] == metric and
            r["result_id"] == result_id
            for r in tracker["records"]
        )
        if exists:
            continue

        # Step 3: Append
        tracker["records"].append({
            "metric": metric,
            "value": value,
            "result_id": result_id
        })

    # Step 4: Source tracking
    if block["block_id"] not in [b["block_id"] for b in tracker["source_blocks"]]:
        tracker["source_blocks"].append({
            "block_id": block["block_id"],
            "block_title": block.get("block_title", "")
        })

def append_to_tracker(tracker, block, result_id):

    t_type = tracker.get("type")

    if t_type == "table":
        append_table(tracker, block, result_id)

    elif t_type == "matrix":
        append_matrix(tracker, block, result_id)

    elif t_type == "scorecard":
        append_scorecard(tracker, block, result_id)

    else:
        raise ValueError("Unsupported tracker type")