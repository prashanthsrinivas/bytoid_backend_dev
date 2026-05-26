"""Convert pytest-json-report and locust --json output into the canonical
test-result payload consumed by the frontend dashboard."""

import json
from datetime import datetime, timezone
from typing import Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _status_from_summary(summary: dict, returncode: Optional[int] = None) -> str:
    if summary.get("failed", 0) > 0 or summary.get("errors", 0) > 0:
        return "failed"
    if returncode is not None and returncode != 0 and summary.get("total", 0) == 0:
        return "failed"
    return "passed"


def parse_pytest_json(
    *,
    category: str,
    run_id: str,
    raw_json: Optional[dict],
    started_at: str,
    finished_at: str,
    returncode: int,
    stdout_tail: str = "",
    stderr_tail: str = "",
) -> dict:
    """Normalize pytest-json-report output."""
    if not raw_json:
        return {
            "category": category,
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": _duration(started_at, finished_at),
            "summary": {
                "total": 0,
                "passed": 0,
                "failed": 0,
                "skipped": 0,
                "errors": 0,
            },
            "status": "failed",
            "tests": [],
            "metrics": None,
            "stdout_tail": stdout_tail[-2000:] if stdout_tail else "",
            "stderr_tail": stderr_tail[-2000:] if stderr_tail else "",
        }

    raw_summary = raw_json.get("summary", {}) or {}
    summary = {
        "total": raw_summary.get("total", 0),
        "passed": raw_summary.get("passed", 0),
        "failed": raw_summary.get("failed", 0),
        "skipped": raw_summary.get("skipped", 0),
        "errors": raw_summary.get("error", 0),
    }
    tests = []
    for t in raw_json.get("tests", []) or []:
        call = t.get("call") or {}
        tests.append(
            {
                "name": t.get("nodeid"),
                "outcome": t.get("outcome"),
                "duration": round(t.get("duration", 0) or 0, 4),
                "message": call.get("longrepr"),
            }
        )

    return {
        "category": category,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": round(raw_json.get("duration", 0) or 0, 3),
        "summary": summary,
        "status": _status_from_summary(summary, returncode),
        "tests": tests,
        "metrics": None,
    }


def parse_locust_json(
    *,
    category: str,
    run_id: str,
    raw_text: str,
    started_at: str,
    finished_at: str,
    returncode: int,
    num_users: Optional[int] = None,
    stdout_tail: str = "",
    stderr_tail: str = "",
) -> dict:
    """Normalize Locust --json output (a JSON array of per-endpoint stats)."""
    try:
        stats = json.loads(raw_text) if raw_text else []
    except json.JSONDecodeError:
        stats = []

    tests = []
    total_reqs = 0
    total_fails = 0
    weighted_p50 = 0.0
    weighted_p95 = 0.0
    weighted_p99 = 0.0
    total_rps = 0.0

    for row in stats:
        name = row.get("name") or row.get("method", "")
        method = row.get("method", "")
        reqs = row.get("num_requests", 0) or 0
        fails = row.get("num_failures", 0) or 0
        p50 = row.get("median_response_time", 0) or 0
        # locust exposes percentiles in different shapes across versions; fall back gracefully.
        p95 = row.get("response_time_percentile_0.95")
        if p95 is None:
            p95 = row.get("ninety_fifth_response_time", 0) or 0
        p99 = row.get("response_time_percentile_0.99")
        if p99 is None:
            p99 = row.get("ninety_ninth_response_time", 0) or 0
        rps = row.get("current_rps") or row.get("total_rps", 0) or 0

        if name == "Aggregated" or method == "":
            total_reqs += reqs
            total_fails += fails
            total_rps = rps or total_rps
            weighted_p50 = p50
            weighted_p95 = p95
            weighted_p99 = p99
            continue

        tests.append(
            {
                "name": f"{method} {name}".strip(),
                "outcome": "failed" if fails > 0 else "passed",
                "duration": round((p95 or 0) / 1000.0, 4),
                "message": None,
                "metrics": {
                    "requests": reqs,
                    "failures": fails,
                    "p50": p50,
                    "p95": p95,
                    "p99": p99,
                    "rps": rps,
                },
            }
        )

    if total_reqs == 0:
        for row in stats:
            total_reqs += row.get("num_requests", 0) or 0
            total_fails += row.get("num_failures", 0) or 0

    failure_rate = (total_fails / total_reqs) if total_reqs else 0
    summary = {
        "total": len(tests),
        "passed": sum(1 for t in tests if t["outcome"] == "passed"),
        "failed": sum(1 for t in tests if t["outcome"] == "failed"),
        "skipped": 0,
        "errors": 0,
    }
    status = "passed" if failure_rate < 0.05 and returncode == 0 else "failed"

    return {
        "category": category,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": _duration(started_at, finished_at),
        "summary": summary,
        "status": status,
        "tests": tests,
        "metrics": {
            "requests": total_reqs,
            "failures": total_fails,
            "failure_rate": round(failure_rate, 4),
            "rps": round(total_rps, 2),
            "p50": weighted_p50,
            "p95": weighted_p95,
            "p99": weighted_p99,
            "num_users": num_users,
        },
        "stdout_tail": stdout_tail[-2000:] if stdout_tail else "",
        "stderr_tail": stderr_tail[-2000:] if stderr_tail else "",
    }


def make_failed_payload(
    *,
    category: str,
    run_id: str,
    started_at: str,
    finished_at: str,
    error: str,
    stdout_tail: str = "",
    stderr_tail: str = "",
) -> dict:
    """For when the runner couldn't start (binary missing, timeout, exception)."""
    return {
        "category": category,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": _duration(started_at, finished_at),
        "summary": {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 1},
        "status": "failed",
        "tests": [],
        "metrics": None,
        "error": error,
        "stdout_tail": stdout_tail[-2000:] if stdout_tail else "",
        "stderr_tail": stderr_tail[-2000:] if stderr_tail else "",
    }


def _duration(started_at: str, finished_at: str) -> float:
    try:
        s = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        f = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
        return round((f - s).total_seconds(), 3)
    except (ValueError, AttributeError):
        return 0.0


def utcnow_iso() -> str:
    return _now_iso()
