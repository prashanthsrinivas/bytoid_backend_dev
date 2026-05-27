import copy
import uuid
from datetime import datetime

from utils.normal import ensure_dir
import os
import json

from utils.s3_utils import (
    delete_file_from_s3,
    read_json_from_s3,
    upload_any_file,
    save_any_s3,
)
from utils.key_rotation_manager import SecureKMSService as _TrackerKMSService
_tracker_kms = _TrackerKMSService()


def _enc_val(user_id, v):
    """Encrypt a tracker cell/row value string."""
    if not isinstance(v, str) or not v:
        return v
    enc = _tracker_kms.encrypt(user_id, v)
    return {"ciphertext": enc["ciphertext"], "iv": enc["iv"], "encrypted_key": enc["encrypted_key"]}


def _dec_val(user_id, v):
    """Decrypt a tracker cell/row value; pass through plaintext."""
    if isinstance(v, dict) and "encrypted_key" in v:
        return _tracker_kms.decrypt(user_id, v["encrypted_key"], v["iv"], v["ciphertext"])
    return v


def _load_and_decrypt_tracker(user_id, tracker_id):
    """Load tracker JSON from S3 and decrypt all cell values. Saves back if lazily migrating."""
    tracker_path = f"{user_id}/tracker/{tracker_id}/tracker.json"
    tracker_data = read_json_from_s3(tracker_path)
    if not tracker_data:
        return tracker_data
    tracker_data, was_migrated = _decrypt_tracker_data(user_id, tracker_data)
    if was_migrated:
        try:
            save_tracker_file(user_id, tracker_id, tracker_data)
        except Exception:
            pass
    return tracker_data


def _decrypt_tracker_data(user_id, tracker_data):
    """Decrypt all cell/row values in tracker_data. Returns (data, was_migrated)."""
    if not tracker_data:
        return tracker_data, False
    was_migrated = False
    for row in tracker_data.get("rows", []):
        if isinstance(row.get("values"), dict):
            for k, v in row["values"].items():
                if isinstance(v, str) and v:
                    was_migrated = True
                row["values"][k] = _dec_val(user_id, v)
    for cell in tracker_data.get("cells", []):
        raw = cell.get("value")
        if isinstance(raw, str) and raw:
            was_migrated = True
        cell["value"] = _dec_val(user_id, raw) if raw is not None else raw
    for record in tracker_data.get("records", []):
        raw = record.get("value")
        if isinstance(raw, str) and raw:
            was_migrated = True
        record["value"] = _dec_val(user_id, raw) if raw is not None else raw
    return tracker_data, was_migrated


def _get_local_tmp_path():
    path = "data/tmp_json/config_tracker.json"
    ensure_dir(os.path.dirname(path))
    return path


def check_config_exist(user_id):
    config_path = f"{user_id}/tracker/config_tracker.json"
    config_data = read_json_from_s3(config_path)
    return config_path, config_data


def create_empty_tracker_config(user_id):
    config_data = {"user_id": user_id, "trackers": []}

    filename = "config_tracker.json"
    local_path = f"/tmp/{filename}"
    s3_key = f"{user_id}/tracker/{filename}"

    with open(local_path, "w") as f:
        json.dump(config_data, f, indent=2)

    upload_any_file(
        file_path=local_path, user_id=user_id, file_name=s3_key, type="tracker"
    )

    os.remove(local_path)

    return s3_key


def create_tracker_config(
    config_path, user_id, name, tracker_type, runbook_id, block_id=None
):
    local_path = _get_local_tmp_path()
    user_id = str(user_id)

    # Step 1: Read config
    config_data = read_json_from_s3(config_path)
    if not config_data:
        config_data = {"user_id": user_id, "trackers": []}

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
        "block_id": block_id,
        "file_path": file_path,
    }

    config_data["trackers"].append(new_tracker)

    # Step 4: Save locally
    with open(local_path, "w") as f:
        json.dump(config_data, f, indent=2)

    # Step 5: Upload
    upload_any_file(
        file_path=local_path, user_id=user_id, file_name=config_path, type="tracker"
    )

    os.remove(local_path)

    return tracker_id, file_path


def update_tracker_config(config_path, user_id, tracker_id, updates):
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
        file_path=local_path, user_id=user_id, file_name=config_path, type="tracker"
    )

    os.remove(local_path)

    return True


def delete_tracker_config(config_path, user_id, tracker_id):
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
    config_data["trackers"] = [t for t in trackers if t["tracker_id"] != tracker_id]

    # Step 2: Save config
    with open(local_path, "w") as f:
        json.dump(config_data, f, indent=2)

    upload_any_file(
        file_path=local_path, user_id=user_id, file_name=config_path, type="tracker"
    )

    os.remove(local_path)

    # Step 3: Delete tracker file (IMPORTANT)
    delete_file_from_s3(f"{user_id}/tracker/{tracker_id}/tracker.json")

    return True


def create_tracker_file(
    user_id,
    tracker_id,
    tracker_type,  # table | matrix | scorecard
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
        "source_blocks": [],
    }

    # Step 2: Type-specific initialization with schema from block_config

    # ✅ TABLE
    if tracker_type == "table":
        columns = []
        if block_config and "columns" in block_config:
            # Initialize columns from block schema
            for idx, col in enumerate(block_config["columns"]):
                columns.append(
                    {
                        "id": f"col_{idx + 1}",
                        "name": col.get("name", f"Column {idx + 1}"),
                        "type": col.get("type", "text"),
                        "source_column": col.get("name", f"Column {idx + 1}"),
                        "enum": col.get("enum", []),
                    }
                )

        tracker_data.update({"schema": {"columns": columns}, "rows": []})

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

        tracker_data.update(
            {
                "schema": {
                    "rows": rows,
                    "columns": columns,
                    "cell_value_label": cell_value_label,
                },
                "cells": [],
            }
        )

    # ✅ SCORECARD
    elif tracker_type == "scorecard":
        metrics = []
        if block_config and "metrics" in block_config:
            # Initialize metrics from block schema
            for metric in block_config["metrics"]:
                metric_name = (
                    metric if isinstance(metric, str) else metric.get("name", "")
                )
                metrics.append(metric_name)

        tracker_data.update({"schema": {"metrics": metrics}, "records": []})

    else:
        raise ValueError("Invalid tracker type")

    # Step 3: Write locally
    with open(local_path, "w") as f:
        json.dump(tracker_data, f, indent=2)

    # Step 4: Upload to S3
    upload_any_file(
        file_path=local_path, user_id=user_id, s3_key_C=s3_path, type="tracker"
    )

    # Step 5: Cleanup
    try:
        os.remove(local_path)
    except Exception:
        pass

    return s3_path


def normalize_block_for_append(block, tracker_type):
    """
    Transform LanceDB result block structure to expected append format.
    Handles nested micro_blocks structure from runbook execution results.
    """
    if not isinstance(block, dict):
        return block

    # Extract data from nested micro_blocks structure if present
    micro_blocks = block.get("micro_blocks", [])
    if micro_blocks and isinstance(micro_blocks, list) and len(micro_blocks) > 0:
        micro_block = micro_blocks[0]

        # ✅ TABLE: Extract from micro_blocks[0].data.rows
        if tracker_type == "table":
            if micro_block.get("type") == "table_schema":
                micro_data = micro_block.get("data", {})
                rows = micro_data.get("rows", [])
                headers = micro_data.get("headers", [])

                if rows:
                    # Case 1: rows are dicts (standard format)
                    if isinstance(rows[0], dict):
                        block["headers"] = list(rows[0].keys())
                        block["rows"] = rows
                        return block
                    # Case 2: rows are arrays, headers are separate
                    elif isinstance(rows[0], (list, tuple)) and headers:
                        converted_rows = []
                        for row_array in rows:
                            row_dict = {
                                headers[i]: row_array[i]
                                for i in range(min(len(headers), len(row_array)))
                            }
                            converted_rows.append(row_dict)
                        block["headers"] = headers
                        block["rows"] = converted_rows
                        return block

        # ✅ MATRIX: Extract from micro_blocks[0].data.matrix with axes
        elif tracker_type == "matrix":
            if micro_block.get("type") == "matrix_schema":
                micro_data = micro_block.get("data", {})
                matrix = micro_data.get("matrix", [])

                if matrix:
                    # Extract axes information
                    x_axis = micro_block.get("x_axis", {})
                    y_axis = micro_block.get("y_axis", {})

                    # Convert matrix array to cell format for append_matrix
                    cells = []
                    x_values = x_axis.get("values", [])
                    y_values = y_axis.get("values", [])

                    for row_idx, row in enumerate(matrix):
                        if row_idx < len(y_values):
                            row_label = y_values[row_idx]
                            for col_idx, cell_value in enumerate(row):
                                if col_idx < len(x_values):
                                    col_label = x_values[col_idx]
                                    cells.append(
                                        {
                                            "row": row_label,
                                            "column": col_label,
                                            "value": cell_value,
                                        }
                                    )

                    block["data"] = cells
                    block["x_axis"] = x_axis
                    block["y_axis"] = y_axis
                    return block

        # ✅ SCORECARD: Extract from micro_blocks[0].data.records or metrics
        elif tracker_type == "scorecard":
            if micro_block.get("type") == "scorecard_schema":
                micro_data = micro_block.get("data", {})
                records = micro_data.get("records", [])

                if records:
                    block["data"] = records
                    return block

    # Fallback: Handle alternative structures without micro_blocks
    if tracker_type == "table":
        if "headers" not in block and "rows" not in block:
            if "data" in block and isinstance(block["data"], list):
                if block["data"] and isinstance(block["data"][0], dict):
                    block["headers"] = list(block["data"][0].keys())
                    block["rows"] = block["data"]
            elif "columns" in block and "rows" in block:
                block["headers"] = [
                    c.get("name", c) if isinstance(c, dict) else c
                    for c in block.get("columns", [])
                ]

    elif tracker_type == "matrix":
        if "data" not in block and "cells" in block:
            block["data"] = block["cells"]

    elif tracker_type == "scorecard":
        if "data" not in block:
            if "records" in block:
                block["data"] = block["records"]
            elif "metrics" in block and isinstance(block["metrics"], dict):
                block["data"] = [
                    {"metric": k, "value": v} for k, v in block["metrics"].items()
                ]

    return block


def append_table(tracker, block, result_id):
    """
    block = {
        "block_id": "...",
        "block_title": "...",
        "headers": [...],
        "rows": [...]
    }

    Returns metadata about schema changes and data appended.
    """

    schema_cols = tracker["schema"]["columns"]

    # Build column map: source_column → col_id
    col_map = {col["source_column"]: col["id"] for col in schema_cols}
    existing_columns = set(col_map.keys())
    new_columns_created = []
    columns_matched = []

    # Track all columns from incoming data
    incoming_headers = set(block.get("headers", []))

    # Step 1: Identify column discrepancies and add missing columns (schema evolution)
    for header in block.get("headers", []):
        if header not in col_map:
            # New column - create it
            new_col_id = f"col_{len(schema_cols) + 1}"
            new_col = {"id": new_col_id, "name": header, "source_column": header}
            schema_cols.append(new_col)
            col_map[header] = new_col_id
            new_columns_created.append(
                {
                    "column_name": header,
                    "column_id": new_col_id,
                    "reason": "Not found in tracker schema",
                }
            )
        else:
            # Existing column
            columns_matched.append(header)

    # Step 2: Append rows
    rows_appended = 0
    rows_skipped_dedup = 0

    for idx, row in enumerate(block.get("rows", [])):

        micro_id = row.get("micro_id")

        # 🔒 Dedup check: Use row_index if micro_id is missing
        exists = any(
            r["result_id"] == result_id
            and r["source"]["block_id"] == block["block_id"]
            and (
                # If micro_id exists, use it for dedup
                (micro_id is not None and r["source"]["micro_id"] == micro_id)
                or
                # If micro_id is missing, use row_index as unique identifier
                (micro_id is None and r["source"]["row_index"] == idx)
            )
            for r in tracker["rows"]
        )
        if exists:
            rows_skipped_dedup += 1
            continue

        values = {}

        # Safe mapping (no index-based mapping)
        for key, val in row.items():
            if key in col_map:
                values[col_map[key]] = val
            else:
                # handle unexpected key (new column at row level)
                new_col_id = f"col_{len(schema_cols) + 1}"

                new_col = {"id": new_col_id, "name": key, "source_column": key}
                schema_cols.append(new_col)

                col_map[key] = new_col_id
                values[new_col_id] = val

                # Track if this is a new column not in headers
                if key not in new_columns_created:
                    new_columns_created.append(
                        {
                            "column_name": key,
                            "column_id": new_col_id,
                            "reason": "Found in row data but not in headers",
                        }
                    )

        tracker["rows"].append(
            {
                "row_id": f"trk_r_{uuid.uuid4().hex[:8]}",
                "result_id": result_id,
                "source": {
                    "block_id": block["block_id"],
                    "micro_id": micro_id,
                    "row_index": idx,
                },
                "values": values,
                "last_updated_from": "report",
            }
        )

        rows_appended += 1

    # Step 3: Update source_blocks
    if block["block_id"] not in [b["block_id"] for b in tracker["source_blocks"]]:
        tracker["source_blocks"].append(
            {"block_id": block["block_id"], "block_title": block.get("block_title", "")}
        )

    # Return metadata about changes
    return {
        "rows_appended": rows_appended,
        "rows_skipped_dedup": rows_skipped_dedup,
        "column_discrepancies": {
            "matched_columns": sorted(list(columns_matched)),
            "new_columns_created": new_columns_created,
            "total_columns_in_schema": len(schema_cols),
        },
    }


def append_matrix(tracker, block, result_id):
    """
    block = {
        "block_id": "...",
        "block_title": "...",
        "data": [
            {"row": "...", "column": "...", "value": ...}
        ]
    }

    Returns metadata about schema changes and cells appended.
    """

    schema = tracker["schema"]
    existing_rows = set(schema["rows"])
    existing_cols = set(schema["columns"])
    new_rows_created = []
    new_columns_created = []
    cells_appended = 0
    cells_skipped_dedup = 0

    for entry in block.get("data", []):
        row_key = entry.get("row")
        col_key = entry.get("column")
        value = entry.get("value")

        if row_key is None or col_key is None:
            continue  # skip invalid entries

        # Step 1: Schema evolution - track new axes
        if row_key not in existing_rows:
            schema["rows"].append(row_key)
            existing_rows.add(row_key)
            new_rows_created.append(row_key)

        if col_key not in existing_cols:
            schema["columns"].append(col_key)
            existing_cols.add(col_key)
            new_columns_created.append(col_key)

        # Step 2: Dedup check
        exists = any(
            c["row"] == row_key
            and c["column"] == col_key
            and c["result_id"] == result_id
            for c in tracker["cells"]
        )
        if exists:
            cells_skipped_dedup += 1
            continue

        # Step 3: Append
        tracker["cells"].append(
            {"row": row_key, "column": col_key, "value": value, "result_id": result_id}
        )

        cells_appended += 1

    # Step 4: Source tracking
    if block["block_id"] not in [b["block_id"] for b in tracker["source_blocks"]]:
        tracker["source_blocks"].append(
            {"block_id": block["block_id"], "block_title": block.get("block_title", "")}
        )

    # Return metadata about changes
    return {
        "cells_appended": cells_appended,
        "cells_skipped_dedup": cells_skipped_dedup,
        "axis_discrepancies": {
            "new_rows": new_rows_created,
            "new_columns": new_columns_created,
            "total_rows": len(schema["rows"]),
            "total_columns": len(schema["columns"]),
        },
    }


def append_scorecard(tracker, block, result_id):
    """
    block = {
        "block_id": "...",
        "block_title": "...",
        "data": [
            {"metric": "...", "value": ...}
        ]
    }

    Returns metadata about schema changes and records appended.
    """

    schema = tracker["schema"]
    existing_metrics = set(schema["metrics"])
    new_metrics_created = []
    records_appended = 0
    records_skipped_dedup = 0

    for entry in block.get("data", []):
        metric = entry.get("metric")
        value = entry.get("value")

        if not metric:
            continue

        # Step 1: Schema evolution - track new metrics
        metric_is_new = False
        if metric not in existing_metrics:
            schema["metrics"].append(metric)
            existing_metrics.add(metric)
            new_metrics_created.append(metric)
            metric_is_new = True

        # Step 2: Dedup check
        exists = any(
            r["metric"] == metric and r["result_id"] == result_id
            for r in tracker["records"]
        )
        if exists:
            records_skipped_dedup += 1
            continue

        # Step 3: Append
        tracker["records"].append(
            {"metric": metric, "value": value, "result_id": result_id}
        )

        records_appended += 1

    # Step 4: Source tracking
    if block["block_id"] not in [b["block_id"] for b in tracker["source_blocks"]]:
        tracker["source_blocks"].append(
            {"block_id": block["block_id"], "block_title": block.get("block_title", "")}
        )

    # Return metadata about changes
    return {
        "records_appended": records_appended,
        "records_skipped_dedup": records_skipped_dedup,
        "metric_discrepancies": {
            "new_metrics": new_metrics_created,
            "total_metrics": len(schema["metrics"]),
        },
    }


def append_to_tracker(tracker, block, result_id):
    """
    Append block data to tracker and return metadata about changes.

    Returns dict with:
    - rows_appended / cells_appended / records_appended: count of items added
    - column/axis/metric_discrepancies: details about schema changes
    """
    t_type = tracker.get("type")

    # Normalize block to expected structure
    block = normalize_block_for_append(block, t_type)

    metadata = {}

    if t_type == "table":
        metadata = append_table(tracker, block, result_id)

    elif t_type == "matrix":
        metadata = append_matrix(tracker, block, result_id)

    elif t_type == "scorecard":
        metadata = append_scorecard(tracker, block, result_id)

    else:
        raise ValueError("Unsupported tracker type")

    return metadata


def save_tracker_file(user_id, tracker_id, tracker_data):
    local_path = f"/tmp/{tracker_id}_tracker.json"
    s3_path = f"{user_id}/tracker/{tracker_id}/tracker.json"

    # Encrypt cell values before persisting (deep-copy to avoid mutating in-memory state)
    td = copy.deepcopy(tracker_data)
    for row in td.get("rows", []):
        if isinstance(row.get("values"), dict):
            row["values"] = {k: _enc_val(user_id, v) for k, v in row["values"].items()}
    for cell in td.get("cells", []):
        cell["value"] = _enc_val(user_id, cell.get("value", ""))
    for record in td.get("records", []):
        record["value"] = _enc_val(user_id, record.get("value", ""))

    with open(local_path, "w") as f:
        json.dump(td, f, indent=2)

    upload_any_file(
        file_path=local_path,
        user_id=user_id,
        s3_key_C=s3_path,  # Use custom key to bypass basename extraction
        type="tracker",
    )

    try:
        os.remove(local_path)
    except Exception:
        pass

    return s3_path


def ensure_tracker_file_exists(
    user_id, tracker_id, tracker_type, runbook_id, block_config=None
):
    """
    Check if tracker file exists. If not, create it with schema.
    Returns (exists: bool, tracker_data: dict)
    """
    tracker_data = _load_and_decrypt_tracker(user_id, tracker_id)

    if tracker_data:
        # Backfill missing schema/data keys so append functions don't KeyError
        if "schema" not in tracker_data:
            if tracker_type == "table":
                tracker_data["schema"] = {"columns": []}
            elif tracker_type == "matrix":
                tracker_data["schema"] = {
                    "rows": [],
                    "columns": [],
                    "cell_value_label": "Value",
                }
            elif tracker_type == "scorecard":
                tracker_data["schema"] = {"metrics": []}
        if tracker_type == "table" and "rows" not in tracker_data:
            tracker_data["rows"] = []
        elif tracker_type == "matrix" and "cells" not in tracker_data:
            tracker_data["cells"] = []
        elif tracker_type == "scorecard" and "records" not in tracker_data:
            tracker_data["records"] = []
        tracker_data.setdefault("source_blocks", [])
        return True, tracker_data

    # File doesn't exist - create it with schema
    tracker_data = {
        "tracker_id": tracker_id,
        "type": tracker_type,
        "runbook_id": runbook_id,
        "source_blocks": [],
    }

    # Initialize schema based on type and block_config
    if tracker_type == "table":
        columns = []
        if block_config and "columns" in block_config:
            for idx, col in enumerate(block_config["columns"]):
                columns.append(
                    {
                        "id": f"col_{idx + 1}",
                        "name": col.get("name", f"Column {idx + 1}"),
                        "type": col.get("type", "text"),
                        "source_column": col.get("name", f"Column {idx + 1}"),
                        "enum": col.get("enum", []),
                    }
                )

        tracker_data.update({"schema": {"columns": columns}, "rows": []})

    elif tracker_type == "matrix":
        rows = []
        columns = []
        cell_value_label = "Value"

        if block_config:
            if "x_axis" in block_config:
                columns = block_config["x_axis"].get("values", [])
            if "y_axis" in block_config:
                rows = block_config["y_axis"].get("values", [])
            if "cell_value" in block_config:
                cell_value_label = block_config["cell_value"]

        tracker_data.update(
            {
                "schema": {
                    "rows": rows,
                    "columns": columns,
                    "cell_value_label": cell_value_label,
                },
                "cells": [],
            }
        )

    elif tracker_type == "scorecard":
        metrics = []
        if block_config and "metrics" in block_config:
            for metric in block_config["metrics"]:
                metric_name = (
                    metric if isinstance(metric, str) else metric.get("name", "")
                )
                metrics.append(metric_name)

        tracker_data.update({"schema": {"metrics": metrics}, "records": []})

    # Save the newly created tracker
    save_tracker_file(user_id, tracker_id, tracker_data)

    return False, tracker_data


def apply_entry_updates(tracker_data, tracker_type, result_id, entry_updates):
    if tracker_type == "table":
        for update in entry_updates:
            row_id = update.get("row_id")
            new_values = update.get("values", {})
            for row in tracker_data.get("rows", []):
                if row.get("row_id") == row_id and row.get("result_id") == result_id:
                    row["values"].update(new_values)
                    row["last_updated_from"] = "manual"
                    break
    elif tracker_type == "matrix":
        for update in entry_updates:
            row_label = update.get("row")
            col_label = update.get("column")
            new_value = update.get("value")
            for cell in tracker_data.get("cells", []):
                if (
                    cell.get("result_id") == result_id
                    and cell.get("row") == row_label
                    and cell.get("column") == col_label
                ):
                    cell["value"] = new_value
                    break
    elif tracker_type == "scorecard":
        for update in entry_updates:
            metric = update.get("metric")
            new_value = update.get("value")
            for record in tracker_data.get("records", []):
                if (
                    record.get("result_id") == result_id
                    and record.get("metric") == metric
                ):
                    record["value"] = new_value
                    break


def sync_block_to_tracker(user_id, tracker_id, block, result_id):
    config_path, config_data = check_config_exist(user_id)
    if not config_data:
        return
    tracker_entry = next(
        (t for t in config_data.get("trackers", []) if t["tracker_id"] == tracker_id),
        None,
    )
    if not tracker_entry:
        return
    tracker_data = _load_and_decrypt_tracker(user_id, tracker_id)
    if not tracker_data:
        return
    _update_or_append_entries(tracker_data, block, result_id)
    save_tracker_file(user_id, tracker_id, tracker_data)


def _update_or_append_entries(tracker_data, block, result_id):
    tracker_type = tracker_data.get("type")
    block_id = block.get("block_id")
    normalized = normalize_block_for_append(block, tracker_type)
    if tracker_type == "table":
        _update_or_append_table(tracker_data, normalized, result_id, block_id)
    elif tracker_type == "matrix":
        _update_or_append_matrix(tracker_data, normalized, result_id, block_id)
    elif tracker_type == "scorecard":
        _update_or_append_scorecard(tracker_data, normalized, result_id, block_id)


def _update_or_append_table(tracker, block, result_id, block_id):
    # Check if rows for this result_id + block_id already exist in the tracker
    rows_to_delete = [
        r
        for r in tracker.get("rows", [])
        if r.get("result_id") == result_id
        and r.get("source", {}).get("block_id") == block_id
    ]

    if rows_to_delete:
        # Delete old rows for this result_id + block_id, then append new ones
        tracker["rows"] = [
            r
            for r in tracker.get("rows", [])
            if not (
                r.get("result_id") == result_id
                and r.get("source", {}).get("block_id") == block_id
            )
        ]
        append_table(tracker, block, result_id)
    else:
        # No existing rows found, just append normally
        append_table(tracker, block, result_id)


def _extract_normalized_rows(block, tracker_type):
    """
    Normalize a block and extract its rows as a list of dicts.
    Used for comparing blocks before and after editing.
    """
    normalized = normalize_block_for_append(block, tracker_type)
    return normalized.get("rows", [])


def _detect_row_changes(current_rows, new_rows):
    """
    Compare two lists of row dicts and return indices of changed and new rows.

    Returns:
        (changed_indices, new_indices)
        - changed_indices: list of indices where rows differ
        - new_indices: list of indices beyond len(current_rows)
    """
    changed_indices = [
        i
        for i in range(min(len(current_rows), len(new_rows)))
        if current_rows[i] != new_rows[i]
    ]
    new_indices = list(range(len(current_rows), len(new_rows)))
    return changed_indices, new_indices


def _apply_row_changes_to_tracker(
    tracker_data, new_rows, result_id, block_id, changed_indices, new_indices
):
    """
    Surgically update tracker rows: only update changed rows, append new ones.
    Preserves unchanged rows in the tracker.
    """
    schema_cols = tracker_data.get("schema", {}).get("columns", [])
    col_map = {col["source_column"]: col["id"] for col in schema_cols}

    # Step 1: Update changed rows
    for idx in changed_indices:
        if idx < len(new_rows):
            row_dict = new_rows[idx]
            # Find tracker row with matching row_index
            matching_rows = [
                r
                for r in tracker_data.get("rows", [])
                if r.get("result_id") == result_id
                and r.get("source", {}).get("row_index") == idx
            ]
            for tr in matching_rows:
                values = {}
                for key, val in row_dict.items():
                    if key in col_map:
                        values[col_map[key]] = val
                tr["values"] = values
                tr["last_updated_from"] = "sync"

    # Step 2: Append new rows
    for idx in new_indices:
        if idx < len(new_rows):
            row_dict = new_rows[idx]
            values = {}
            for key, val in row_dict.items():
                if key not in col_map:
                    # New column found in row, add to schema
                    new_col_id = f"col_{len(schema_cols) + 1}"
                    new_col = {"id": new_col_id, "name": key, "source_column": key}
                    schema_cols.append(new_col)
                    col_map[key] = new_col_id
                    values[new_col_id] = val
                else:
                    values[col_map[key]] = val

            tracker_data.get("rows", []).append(
                {
                    "row_id": f"trk_r_{uuid.uuid4().hex[:8]}",
                    "result_id": result_id,
                    "source": {
                        "block_id": block_id,
                        "micro_id": None,
                        "row_index": idx,
                    },
                    "values": values,
                    "last_updated_from": "sync",
                }
            )


def _update_or_append_matrix(tracker, block, result_id, block_id):
    cells_to_update = [
        c for c in tracker.get("cells", []) if c.get("result_id") == result_id
    ]
    if cells_to_update:
        tracker["cells"] = [
            c
            for c in tracker.get("cells", [])
            if not (
                c.get("result_id") == result_id
                and any(
                    cd.get("result_id") == result_id for cd in block.get("data", [])
                )
            )
        ]
        tracker["cells"].extend(
            [dict(c, result_id=result_id) for c in block.get("data", [])]
        )
    else:
        append_matrix(tracker, block, result_id)


def _update_or_append_scorecard(tracker, block, result_id, block_id):
    records_to_update = [
        rec for rec in tracker.get("records", []) if rec.get("result_id") == result_id
    ]
    if records_to_update:
        tracker["records"] = [
            rec
            for rec in tracker.get("records", [])
            if rec.get("result_id") != result_id
        ]
        tracker["records"].extend(
            [dict(rec, result_id=result_id) for rec in block.get("data", [])]
        )
    else:
        append_scorecard(tracker, block, result_id)


def sync_tracker_to_runbook_block(dbserver, user_id, result_id, block_id, tracker_data):
    import asyncio

    async def _sync():
        result_data = await dbserver.runbook_get_result(user_id, result_id)
        if result_data.get("status") != "completed":
            return
        result = result_data.get("data", {})
        blocks = result.get("result", {}).get("blocks", [])
        target_block = next((b for b in blocks if b.get("block_id") == block_id), None)
        if not target_block:
            return
        updated_micro = _rebuild_micro_blocks_from_tracker(
            tracker_data, result_id, block_id
        )
        target_block["micro_blocks"] = updated_micro
        await dbserver.update_runbook_result(user_id, result_id, {"blocks": blocks})

    asyncio.run(_sync())


def _rebuild_micro_blocks_from_tracker(tracker_data, result_id, block_id):
    tracker_type = tracker_data.get("type")
    if tracker_type == "table":
        rows = [
            r for r in tracker_data.get("rows", []) if r.get("result_id") == result_id
        ]
        if not rows:
            return []
        schema_cols = tracker_data.get("schema", {}).get("columns", [])
        col_id_to_name = {col["id"]: col["source_column"] for col in schema_cols}
        rebuilt_rows = []
        for row in rows:
            row_dict = {}
            for col_id, val in row.get("values", {}).items():
                col_name = col_id_to_name.get(col_id, col_id)
                row_dict[col_name] = val
            rebuilt_rows.append(row_dict)
        return [
            {
                "type": "table_schema",
                "data": {
                    "rows": rebuilt_rows,
                    "columns": [
                        {"name": col["source_column"], "id": col["id"]}
                        for col in schema_cols
                    ],
                },
            }
        ]
    elif tracker_type == "matrix":
        cells = [
            c for c in tracker_data.get("cells", []) if c.get("result_id") == result_id
        ]
        if not cells:
            return []
        schema = tracker_data.get("schema", {})
        rows = schema.get("rows", [])
        columns = schema.get("columns", [])
        matrix = [[None for _ in columns] for _ in rows]
        for cell in cells:
            row_idx = rows.index(cell["row"]) if cell["row"] in rows else -1
            col_idx = columns.index(cell["column"]) if cell["column"] in columns else -1
            if row_idx >= 0 and col_idx >= 0:
                matrix[row_idx][col_idx] = cell["value"]
        return [
            {
                "type": "matrix_schema",
                "data": {"matrix": matrix},
                "x_axis": {"values": columns},
                "y_axis": {"values": rows},
            }
        ]
    elif tracker_type == "scorecard":
        records = [
            rec
            for rec in tracker_data.get("records", [])
            if rec.get("result_id") == result_id
        ]
        if not records:
            return []
        return [{"type": "scorecard_schema", "data": {"records": records}}]
    return []


def update_tracker_from_block(tracker_data, block, result_id, block_id):
    """
    Update tracker rows/cells/records from a modified block.
    This is used when a block is edited in the report - we want to update
    the corresponding tracker data, not just append.

    For tables: Remove old rows from this result_id+block_id, add new ones
    For matrices: Remove old cells, add new ones
    For scorecards: Remove old records, add new ones

    Returns dict with update summary
    """
    tracker_type = tracker_data.get("type")
    summary = {
        "type": tracker_type,
        "rows_removed": 0,
        "rows_added": 0,
        "cells_removed": 0,
        "cells_added": 0,
        "records_removed": 0,
        "records_added": 0,
    }

    if tracker_type == "table":
        rows = tracker_data.get("rows", [])

        # Step 1: Remove existing rows from this result_id and block_id
        initial_count = len(rows)
        tracker_data["rows"] = [
            r
            for r in rows
            if not (
                r.get("result_id") == result_id
                and r.get("source", {}).get("block_id") == block_id
            )
        ]
        summary["rows_removed"] = initial_count - len(tracker_data["rows"])

        # Step 2: Add new rows from the updated block
        block_data = normalize_block_for_append(block, tracker_type)
        schema_cols = tracker_data["schema"]["columns"]
        col_map = {col["source_column"]: col["id"] for col in schema_cols}

        # Ensure all columns from block exist in tracker schema
        for header in block_data.get("headers", []):
            if header not in col_map:
                new_col_id = f"col_{len(schema_cols) + 1}"
                schema_cols.append(
                    {"id": new_col_id, "name": header, "source_column": header}
                )
                col_map[header] = new_col_id

        # Add new rows
        for idx, row_data in enumerate(block_data.get("rows", [])):
            values = {}
            for key, val in row_data.items():
                if key in col_map:
                    values[col_map[key]] = val

            tracker_data["rows"].append(
                {
                    "row_id": f"trk_r_{uuid.uuid4().hex[:8]}",
                    "result_id": result_id,
                    "source": {
                        "block_id": block_id,
                        "micro_id": row_data.get("micro_id"),
                        "row_index": idx,
                    },
                    "values": values,
                    "last_updated_from": "report",
                }
            )
            summary["rows_added"] += 1

    elif tracker_type == "matrix":
        cells = tracker_data.get("cells", [])

        # Step 1: Remove existing cells from this result_id
        initial_count = len(cells)
        tracker_data["cells"] = [c for c in cells if c.get("result_id") != result_id]
        summary["cells_removed"] = initial_count - len(tracker_data["cells"])

        # Step 2: Add new cells from the updated block
        block_data = normalize_block_for_append(block, tracker_type)
        schema = tracker_data["schema"]

        for entry in block_data.get("data", []):
            row_key = entry.get("row")
            col_key = entry.get("column")
            value = entry.get("value")

            if row_key is None or col_key is None:
                continue

            # Ensure schema has these axes
            if row_key not in schema["rows"]:
                schema["rows"].append(row_key)
            if col_key not in schema["columns"]:
                schema["columns"].append(col_key)

            tracker_data["cells"].append(
                {
                    "row": row_key,
                    "column": col_key,
                    "value": value,
                    "result_id": result_id,
                }
            )
            summary["cells_added"] += 1

    elif tracker_type == "scorecard":
        records = tracker_data.get("records", [])

        # Step 1: Remove existing records from this result_id
        initial_count = len(records)
        tracker_data["records"] = [
            r for r in records if r.get("result_id") != result_id
        ]
        summary["records_removed"] = initial_count - len(tracker_data["records"])

        # Step 2: Add new records from the updated block
        block_data = normalize_block_for_append(block, tracker_type)
        schema = tracker_data["schema"]

        for entry in block_data.get("data", []):
            metric = entry.get("metric")
            value = entry.get("value")

            if not metric:
                continue

            # Ensure metric exists in schema
            if metric not in schema["metrics"]:
                schema["metrics"].append(metric)

            tracker_data["records"].append(
                {"metric": metric, "value": value, "result_id": result_id}
            )
            summary["records_added"] += 1

    return summary


def upload_evidence_file(user_id, tracker_id, row_id, column_id, file_obj, filename):
    """
    Upload evidence file to S3 and return the S3 key.

    Args:
        user_id: User ID
        tracker_id: Tracker ID
        row_id: Row ID (or row_index if row_id not available)
        column_id: Column ID where evidence will be stored
        file_obj: File object from request.files
        filename: Original filename

    Returns:
        dict with s3_key, file_info, and metadata
    """
    try:
        # Generate unique filename with timestamp
        date = datetime.utcnow().strftime("%Y-%m-%d")
        file_uuid = uuid.uuid4().hex[:8]
        file_ext = filename.rsplit(".", 1)[-1] if "." in filename else "bin"
        safe_name = filename.replace(" ", "_")

        # S3 path structure: {user_id}/tracker/{tracker_id}/evidence/{row_id}/{date}_{uuid}_{filename}
        s3_key = f"{user_id}/tracker/{tracker_id}/evidence/{row_id}/{date}_{file_uuid}_{safe_name}"

        # Upload to S3
        local_path = f"/tmp/{file_uuid}_{safe_name}"
        file_obj.save(local_path)

        save_any_s3(local_path, s3_key)

        # Clean up local file
        try:
            os.remove(local_path)
        except Exception:
            pass

        return {
            "success": True,
            "s3_key": s3_key,
            "filename": safe_name,
            "file_size": (
                os.path.getsize(local_path) if os.path.exists(local_path) else 0
            ),
            "upload_timestamp": datetime.utcnow().isoformat(),
            "column_id": column_id,
            "row_id": row_id,
        }

    except Exception as e:
        return {"success": False, "error": str(e)}


def update_tracker_evidence(user_id, tracker_id, row_id, column_id, s3_key):
    """
    Update a tracker row's evidence column with the S3 key.

    Args:
        user_id: User ID
        tracker_id: Tracker ID
        row_id: Row ID (internal row_id from tracker, not row_index)
        column_id: Column ID to update
        s3_key: S3 key of the uploaded file

    Returns:
        dict with success status and updated tracker info
    """
    try:
        tracker_data = _load_and_decrypt_tracker(user_id, tracker_id)

        if not tracker_data:
            return {"success": False, "error": "Tracker not found"}

        tracker_type = tracker_data.get("type")

        if tracker_type != "table":
            return {
                "success": False,
                "error": "Evidence upload only supported for table trackers",
            }

        rows = tracker_data.get("rows", [])
        row_found = False

        for row in rows:
            if row.get("row_id") == row_id:
                # Update the column value with S3 key
                if "values" not in row:
                    row["values"] = {}

                row["values"][column_id] = s3_key
                row["last_updated_from"] = "evidence_upload"
                row_found = True
                break

        if not row_found:
            return {"success": False, "error": f"Row '{row_id}' not found in tracker"}

        # Save updated tracker
        save_tracker_file(user_id, tracker_id, tracker_data)

        return {
            "success": True,
            "message": "Evidence file linked to tracker row",
            "tracker_id": tracker_id,
            "row_id": row_id,
            "column_id": column_id,
            "s3_key": s3_key,
        }

    except Exception as e:
        return {"success": False, "error": str(e)}


def add_row_option(user_id, tracker_id, row_id, column_id, options):
    """
    Add options to a tracker row's column (typically framework columns).

    Args:
        user_id: User ID
        tracker_id: Tracker ID
        row_id: Row ID (internal row_id from tracker, not row_index)
        column_id: Column ID to update
        options: List of dicts with "requirement" and "section" keys

    Returns:
        dict with success status, added count, skipped count, and current options
    """
    try:
        tracker_data = _load_and_decrypt_tracker(user_id, tracker_id)

        if not tracker_data:
            return {"success": False, "error": "Tracker not found"}

        tracker_type = tracker_data.get("type")

        if tracker_type != "table":
            return {
                "success": False,
                "error": "Options can only be added to table trackers",
            }

        if not isinstance(options, list) or not options:
            return {"success": False, "error": "Options must be a non-empty list"}

        rows = tracker_data.get("rows", [])
        row_found = False
        added_count = 0
        skipped_count = 0
        updated_options = None

        for row in rows:
            if row.get("row_id") == row_id:
                if "values" not in row:
                    row["values"] = {}

                # Ensure column value is a list
                if column_id not in row["values"] or not isinstance(
                    row["values"][column_id], list
                ):
                    row["values"][column_id] = []

                current_list = row["values"][column_id]

                # Build set of existing options for dedup check
                existing = {
                    (opt.get("requirement"), opt.get("section")) for opt in current_list
                }

                # Add new options, deduplicating
                for opt in options:
                    opt_tuple = (opt.get("requirement"), opt.get("section"))
                    if opt_tuple not in existing:
                        current_list.append(opt)
                        existing.add(opt_tuple)
                        added_count += 1
                    else:
                        skipped_count += 1

                row["last_updated_from"] = "manual"
                updated_options = current_list
                row_found = True
                break

        if not row_found:
            return {"success": False, "error": f"Row '{row_id}' not found in tracker"}

        # Save updated tracker
        save_tracker_file(user_id, tracker_id, tracker_data)

        return {
            "success": True,
            "message": "Options added to tracker row",
            "tracker_id": tracker_id,
            "row_id": row_id,
            "column_id": column_id,
            "added": added_count,
        }

    except Exception as e:
        return {"success": False, "error": str(e)}


def remove_row_option(user_id, tracker_id, row_id, column_id, options):
    """
    Remove options from a tracker row's column (typically framework columns).

    Args:
        user_id: User ID
        tracker_id: Tracker ID
        row_id: Row ID (internal row_id from tracker, not row_index)
        column_id: Column ID to update
        options: List of dicts with "requirement" and "section" keys to remove

    Returns:
        dict with success status, removed count, and current options
    """
    try:
        tracker_data = _load_and_decrypt_tracker(user_id, tracker_id)

        if not tracker_data:
            return {"success": False, "error": "Tracker not found"}

        tracker_type = tracker_data.get("type")

        if tracker_type != "table":
            return {
                "success": False,
                "error": "Options can only be removed from table trackers",
            }

        if not isinstance(options, list) or not options:
            return {"success": False, "error": "Options must be a non-empty list"}

        rows = tracker_data.get("rows", [])
        row_found = False
        removed_count = 0
        updated_options = None

        for row in rows:
            if row.get("row_id") == row_id:
                if "values" not in row:
                    row["values"] = {}

                # Ensure column value is a list
                if column_id not in row["values"] or not isinstance(
                    row["values"][column_id], list
                ):
                    row["values"][column_id] = []

                # Build set of options to remove
                to_remove = {
                    (opt.get("requirement"), opt.get("section")) for opt in options
                }

                # Filter current list
                original_count = len(row["values"][column_id])
                row["values"][column_id] = [
                    opt
                    for opt in row["values"][column_id]
                    if (opt.get("requirement"), opt.get("section")) not in to_remove
                ]
                removed_count = original_count - len(row["values"][column_id])

                row["last_updated_from"] = "manual"
                updated_options = row["values"][column_id]
                row_found = True
                break

        if not row_found:
            return {"success": False, "error": f"Row '{row_id}' not found in tracker"}

        # Save updated tracker
        save_tracker_file(user_id, tracker_id, tracker_data)

        return {
            "success": True,
            "message": "Options removed from tracker row",
            "tracker_id": tracker_id,
            "row_id": row_id,
            "column_id": column_id,
            "removed": removed_count,
        }

    except Exception as e:
        return {"success": False, "error": str(e)}


def propagate_assessment_status_to_policy_cells(
    tracker_data: dict, result_id: str | None = None
) -> int:
    """Update pass/fail status on policy statement cells from assessment result blocks.

    Finds rows that reference result_id (or all rows if result_id is None), reads their
    verdict, and sets the status on every policy column cell in those rows. Superseded
    statement entries are never overwritten.

    Returns the count of cell entries updated.
    """
    schema_cols = tracker_data.get("schema", {}).get("columns", [])
    policy_col_ids = {col["id"] for col in schema_cols if col.get("source_column") == "policies"}
    if not policy_col_ids:
        return 0

    updated = 0
    for row in tracker_data.get("rows", []):
        row_result_id = row.get("result_id")
        if result_id and row_result_id != result_id:
            continue

        verdict = row.get("verdict")
        if not verdict:
            continue

        status = (
            "passed"
            if str(verdict).lower() in ("pass", "passed", "true", "yes")
            else "failed"
        )

        for col_id in policy_col_ids:
            cell = row["values"].get(col_id)
            if not isinstance(cell, list):
                continue
            for entry in cell:
                if isinstance(entry, dict) and entry.get("status") != "superseded":
                    entry["status"] = status
                    updated += 1

    return updated


# def get_block_data(user_id,runbook_id):
