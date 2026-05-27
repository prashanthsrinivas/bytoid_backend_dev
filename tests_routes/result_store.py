"""Atomic per-category result storage for the Unit Test Results module.

Layout:
    testing/results/
      summary.json
      <category>/latest.json
      <category>/history/<run_id>.json
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Optional

from tests_routes.categories import ALL_CATEGORIES, is_valid_category

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_ROOT = os.path.join(_PROJECT_ROOT, "testing", "results")
SUMMARY_PATH = os.path.join(RESULTS_ROOT, "summary.json")


def _ensure_category_dirs(category: str) -> None:
    os.makedirs(os.path.join(RESULTS_ROOT, category, "history"), exist_ok=True)


def _atomic_write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".tmp_", dir=os.path.dirname(path), suffix=".json"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def _summary_entry(payload: dict) -> dict:
    summary = payload.get("summary") or {}
    entry = {
        "run_id": payload.get("run_id"),
        "timestamp": payload.get("finished_at") or payload.get("started_at"),
        "duration": payload.get("duration_seconds"),
        "status": payload.get("status"),
    }
    for key in ("total", "passed", "failed", "skipped", "errors"):
        if key in summary:
            entry[key] = summary[key]
    metrics = payload.get("metrics") or {}
    for key in ("rps", "p50", "p95", "p99", "num_users", "failures"):
        if key in metrics:
            entry[key] = metrics[key]
    return entry


def _load_summary() -> dict:
    if not os.path.exists(SUMMARY_PATH):
        return {"updated_at": None, "categories": {}}
    try:
        with open(SUMMARY_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"updated_at": None, "categories": {}}
    data.setdefault("categories", {})
    return data


def write_category_result(category: str, run_id: str, payload: dict) -> None:
    """Persist a run: latest.json + history/<run_id>.json + summary.json merge."""
    if not is_valid_category(category):
        raise ValueError(f"Unknown category: {category}")

    _ensure_category_dirs(category)

    latest_path = os.path.join(RESULTS_ROOT, category, "latest.json")
    history_path = os.path.join(RESULTS_ROOT, category, "history", f"{run_id}.json")

    _atomic_write_json(latest_path, payload)
    _atomic_write_json(history_path, payload)

    if payload.get("status") == "failed":
        from tests_routes.ai_summary import generate_failure_summary  # noqa: PLC0415
        payload = {**payload, **generate_failure_summary(category, payload)}
        _atomic_write_json(latest_path, payload)
        _atomic_write_json(history_path, payload)

    summary = _load_summary()
    summary["categories"][category] = _summary_entry(payload)
    summary["updated_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(SUMMARY_PATH, summary)


def read_category_result(category: str) -> Optional[dict]:
    if not is_valid_category(category):
        return None
    latest_path = os.path.join(RESULTS_ROOT, category, "latest.json")
    if not os.path.exists(latest_path):
        return None
    with open(latest_path, encoding="utf-8") as f:
        return json.load(f)


def read_summary() -> dict:
    summary = _load_summary()
    # Ensure every known category appears in the response, even if never run.
    for category in ALL_CATEGORIES:
        summary["categories"].setdefault(
            category, {"status": "never_run", "run_id": None, "timestamp": None}
        )
    return summary


def list_history(category: str, limit: int = 25) -> list:
    if not is_valid_category(category):
        return []
    hist_dir = os.path.join(RESULTS_ROOT, category, "history")
    if not os.path.isdir(hist_dir):
        return []
    files = [
        f for f in os.listdir(hist_dir) if f.endswith(".json") and not f.startswith(".")
    ]
    files.sort(reverse=True)  # run_id starts with ISO timestamp → lexicographic = chrono
    out = []
    for fname in files[:limit]:
        try:
            with open(os.path.join(hist_dir, fname), encoding="utf-8") as f:
                payload = json.load(f)
            out.append(
                {
                    "run_id": payload.get("run_id") or fname[:-5],
                    "timestamp": payload.get("finished_at")
                    or payload.get("started_at"),
                    "summary": payload.get("summary"),
                    "status": payload.get("status"),
                }
            )
        except (json.JSONDecodeError, OSError):
            continue
    return out


def new_run_id() -> str:
    """ISO timestamp-prefixed run id so history dir sorts chronologically."""
    import uuid

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}-{uuid.uuid4().hex[:8]}"
