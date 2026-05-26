"""Pytest + Locust runners shared by Celery tasks and the legacy
/azure/run-tests shim. Kept import-light so the legacy synchronous path
doesn't have to pull in all of utils/celery_base.py."""

import json
import os
import shutil
import subprocess
import sys

from tests_routes.normalizers import (
    make_failed_payload,
    parse_locust_json,
    parse_pytest_json,
    utcnow_iso,
)
from tests_routes.result_store import write_category_result


def _project_root():
    # tests_routes/runners.py → parent of tests_routes/ is the repo root.
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _default_target_url():
    try:
        from utils.app_configs import BACKURL

        return BACKURL
    except Exception:
        return "http://localhost:3000"


def run_pytest_category(category, run_id, pytest_targets, timeout_seconds=600):
    """Execute pytest, normalize the JSON report, and persist via result_store."""
    project_root = _project_root()
    results_dir = os.path.join(project_root, "testing", "results", category)
    os.makedirs(results_dir, exist_ok=True)
    json_report_path = os.path.join(results_dir, f"_raw_{run_id}.json")

    started_at = utcnow_iso()

    pytest_bin = shutil.which("pytest") or shutil.which("pytest3")
    cmd = [pytest_bin] if pytest_bin else [sys.executable, "-m", "pytest"]
    cmd += list(pytest_targets) + [
        "-v",
        "--tb=short",
        "--json-report",
        f"--json-report-file={json_report_path}",
    ]

    env = os.environ.copy()
    env["PYTHONPATH"] = project_root

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=project_root,
            env=env,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        finished_at = utcnow_iso()
        payload = make_failed_payload(
            category=category,
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            error=f"pytest timed out after {timeout_seconds}s",
            stdout_tail=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
            stderr_tail=(exc.stderr or "") if isinstance(exc.stderr, str) else "",
        )
        write_category_result(category, run_id, payload)
        return {"category": category, "run_id": run_id, "status": "failed"}
    except Exception as exc:  # noqa: BLE001
        finished_at = utcnow_iso()
        payload = make_failed_payload(
            category=category,
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            error=f"Failed to launch pytest: {exc}",
        )
        write_category_result(category, run_id, payload)
        return {"category": category, "run_id": run_id, "status": "failed"}

    finished_at = utcnow_iso()

    raw = None
    if os.path.exists(json_report_path):
        try:
            with open(json_report_path, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:  # noqa: BLE001
            raw = None

    payload = parse_pytest_json(
        category=category,
        run_id=run_id,
        raw_json=raw,
        started_at=started_at,
        finished_at=finished_at,
        returncode=proc.returncode,
        stdout_tail=proc.stdout or "",
        stderr_tail=proc.stderr or "",
    )
    write_category_result(category, run_id, payload)
    return {
        "category": category,
        "run_id": run_id,
        "status": payload["status"],
        "summary": payload["summary"],
    }


def _parse_locust_run_time(run_time):
    """Convert '30s' / '2m' / '1h' to seconds. Defaults to 60s on parse failure."""
    if isinstance(run_time, (int, float)):
        return int(run_time)
    if not isinstance(run_time, str):
        return 60
    s = run_time.strip().lower()
    try:
        if s.endswith("ms"):
            return max(1, int(float(s[:-2]) / 1000))
        if s.endswith("s"):
            return int(float(s[:-1]))
        if s.endswith("m"):
            return int(float(s[:-1]) * 60)
        if s.endswith("h"):
            return int(float(s[:-1]) * 3600)
        return int(float(s))
    except ValueError:
        return 60


def run_locust_category(
    category,
    run_id,
    scenario,
    target_url,
    users,
    spawn_rate,
    run_time,
):
    """Execute locust headless and persist normalized results."""
    project_root = _project_root()
    target = target_url or _default_target_url()
    results_dir = os.path.join(project_root, "testing", "results", category)
    os.makedirs(results_dir, exist_ok=True)
    csv_prefix = os.path.join(results_dir, f"raw_{run_id}")

    started_at = utcnow_iso()

    locust_bin = shutil.which("locust")
    if not locust_bin:
        finished_at = utcnow_iso()
        payload = make_failed_payload(
            category=category,
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            error="locust binary not found on PATH. Install with `pip install locust`.",
        )
        write_category_result(category, run_id, payload)
        return {"category": category, "run_id": run_id, "status": "failed"}

    cmd = [
        locust_bin,
        "-f",
        os.path.join(project_root, "testing", "load", "locustfile.py"),
        "--headless",
        "-u",
        str(users),
        "-r",
        str(spawn_rate),
        "-t",
        str(run_time),
        "-H",
        target,
        "--json",
        "--only-summary",
        "--csv",
        csv_prefix,
    ]
    env = os.environ.copy()
    env["LOCUST_SCENARIO"] = scenario
    env["PYTHONPATH"] = project_root

    timeout_seconds = _parse_locust_run_time(run_time) + 60

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=project_root,
            env=env,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        finished_at = utcnow_iso()
        payload = make_failed_payload(
            category=category,
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            error=f"locust timed out after {timeout_seconds}s",
            stdout_tail=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
            stderr_tail=(exc.stderr or "") if isinstance(exc.stderr, str) else "",
        )
        write_category_result(category, run_id, payload)
        return {"category": category, "run_id": run_id, "status": "failed"}
    except Exception as exc:  # noqa: BLE001
        finished_at = utcnow_iso()
        payload = make_failed_payload(
            category=category,
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            error=f"Failed to launch locust: {exc}",
        )
        write_category_result(category, run_id, payload)
        return {"category": category, "run_id": run_id, "status": "failed"}

    finished_at = utcnow_iso()
    payload = parse_locust_json(
        category=category,
        run_id=run_id,
        raw_text=proc.stdout or "",
        started_at=started_at,
        finished_at=finished_at,
        returncode=proc.returncode,
        num_users=users,
        stdout_tail=proc.stdout or "",
        stderr_tail=proc.stderr or "",
    )
    write_category_result(category, run_id, payload)
    return {
        "category": category,
        "run_id": run_id,
        "status": payload["status"],
        "metrics": payload.get("metrics"),
    }
