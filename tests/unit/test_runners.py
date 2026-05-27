"""Unit tests for tests_routes/runners.py — subprocess fully mocked."""

import json
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

# Stub heavy deps before importing runners
for _mod in ("pymysql", "pymysql.cursors", "db", "db.rds_db", "db.db_checkers",
             "boto3", "dotenv", "dbutils", "dbutils.pooled_db"):
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

sys.modules.setdefault("utils.s3_utils", MagicMock())
sys.modules.setdefault("utils.base_logger",
                      MagicMock(get_logger=MagicMock(return_value=MagicMock())))

from tests_routes import runners  # noqa: E402
from tests_routes import result_store as rs  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_results(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "RESULTS_ROOT", str(tmp_path))
    monkeypatch.setattr(rs, "SUMMARY_PATH", str(tmp_path / "summary.json"))
    return tmp_path


# ── _project_root ───────────────────────────────────────────────────────────

@pytest.mark.unit
def test_project_root_is_repo_root():
    root = runners._project_root()
    assert os.path.isdir(root)
    # The repo root contains the tests_routes/ package
    assert os.path.isdir(os.path.join(root, "tests_routes"))

@pytest.mark.unit
def test_project_root_is_absolute():
    assert os.path.isabs(runners._project_root())


# ── _default_target_url ─────────────────────────────────────────────────────

@pytest.mark.unit
def test_default_target_url_returns_string():
    url = runners._default_target_url()
    assert isinstance(url, str)
    assert url.startswith("http")

@pytest.mark.unit
def test_default_target_url_uses_backurl(monkeypatch):
    monkeypatch.setitem(sys.modules, "utils.app_configs",
                       MagicMock(BACKURL="https://api.example.com"))
    assert runners._default_target_url() == "https://api.example.com"


# ── _parse_locust_run_time ──────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("input_val,expected", [
    (30, 30),
    (60, 60),
    (300, 300),
    (30.0, 30),
    ("30s", 30),
    ("60s", 60),
    ("2m", 120),
    ("1h", 3600),
    ("0.5h", 1800),
    ("90", 90),
])
def test_parse_locust_run_time(input_val, expected):
    assert runners._parse_locust_run_time(input_val) == expected

@pytest.mark.unit
@pytest.mark.parametrize("input_val", [None, [], {}, "abc", "garbage"])
def test_parse_locust_run_time_default_60(input_val):
    assert runners._parse_locust_run_time(input_val) == 60

@pytest.mark.unit
def test_parse_locust_run_time_ms_to_seconds():
    assert runners._parse_locust_run_time("1500ms") >= 1


# ── run_pytest_category ─────────────────────────────────────────────────────

@pytest.mark.unit
def test_run_pytest_category_success(tmp_path, monkeypatch):
    monkeypatch.setattr(runners, "_project_root", lambda: str(tmp_path))
    fake_proc = MagicMock(returncode=0, stdout="", stderr="")
    pytest_report = {
        "duration": 1.0,
        "summary": {"total": 1, "passed": 1, "failed": 0, "error": 0},
        "tests": [],
    }
    # Pre-create the report file that the runner expects pytest to write
    def fake_run(cmd, **kwargs):
        # Find the --json-report-file=PATH argument and write the report there
        for arg in cmd:
            if isinstance(arg, str) and arg.startswith("--json-report-file="):
                path = arg.split("=", 1)[1]
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as f:
                    json.dump(pytest_report, f)
        return fake_proc

    with patch("subprocess.run", side_effect=fake_run):
        result = runners.run_pytest_category(
            "backend_unit", "rid-1", ["tests/"], timeout_seconds=600,
        )
    assert result["category"] == "backend_unit"
    assert result["status"] == "passed"
    assert result["summary"]["passed"] == 1

@pytest.mark.unit
def test_run_pytest_category_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(runners, "_project_root", lambda: str(tmp_path))
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("pytest", 60)):
        result = runners.run_pytest_category("backend_unit", "rid-2", ["tests/"], timeout_seconds=60)
    assert result["status"] == "failed"
    # Result is persisted
    saved = rs.read_category_result("backend_unit")
    assert saved is not None
    assert "timed out" in str(saved).lower()

@pytest.mark.unit
def test_run_pytest_category_subprocess_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(runners, "_project_root", lambda: str(tmp_path))
    with patch("subprocess.run", side_effect=OSError("permission denied")):
        result = runners.run_pytest_category("backend_unit", "rid-3", ["tests/"])
    assert result["status"] == "failed"
    saved = rs.read_category_result("backend_unit")
    assert "Failed to launch pytest" in str(saved)

@pytest.mark.unit
def test_run_pytest_category_missing_json_report(tmp_path, monkeypatch):
    monkeypatch.setattr(runners, "_project_root", lambda: str(tmp_path))
    # subprocess succeeds but doesn't write the JSON report
    fake_proc = MagicMock(returncode=2, stdout="", stderr="")
    with patch("subprocess.run", return_value=fake_proc):
        result = runners.run_pytest_category("backend_unit", "rid-4", ["tests/"])
    # No JSON → parse_pytest_json returns failed envelope
    assert result["status"] == "failed"

@pytest.mark.unit
def test_run_pytest_category_corrupt_json_report(tmp_path, monkeypatch):
    monkeypatch.setattr(runners, "_project_root", lambda: str(tmp_path))
    def fake_run(cmd, **kwargs):
        for arg in cmd:
            if isinstance(arg, str) and arg.startswith("--json-report-file="):
                path = arg.split("=", 1)[1]
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as f:
                    f.write("not valid json")
        return MagicMock(returncode=2, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        result = runners.run_pytest_category("backend_unit", "rid-5", ["tests/"])
    assert result["status"] == "failed"


# ── run_locust_category ─────────────────────────────────────────────────────

@pytest.mark.unit
def test_run_locust_category_no_locust_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(runners, "_project_root", lambda: str(tmp_path))
    with patch("shutil.which", return_value=None):
        result = runners.run_locust_category(
            "backend_load", "rid-1", "scenario1", "http://x",
            users=1, spawn_rate=1, run_time="10s",
        )
    assert result["status"] == "failed"
    saved = rs.read_category_result("backend_load")
    assert "locust" in str(saved).lower()

@pytest.mark.unit
def test_run_locust_category_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(runners, "_project_root", lambda: str(tmp_path))
    with patch("shutil.which", return_value="/usr/bin/locust"):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("locust", 60)):
            result = runners.run_locust_category(
                "backend_load", "rid-2", "s", "http://x",
                users=1, spawn_rate=1, run_time="5s",
            )
    assert result["status"] == "failed"

@pytest.mark.unit
def test_run_locust_category_subprocess_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(runners, "_project_root", lambda: str(tmp_path))
    with patch("shutil.which", return_value="/usr/bin/locust"):
        with patch("subprocess.run", side_effect=OSError("oops")):
            result = runners.run_locust_category(
                "backend_load", "rid-3", "s", "http://x",
                users=1, spawn_rate=1, run_time="5s",
            )
    assert result["status"] == "failed"

@pytest.mark.unit
def test_run_locust_category_success(tmp_path, monkeypatch):
    monkeypatch.setattr(runners, "_project_root", lambda: str(tmp_path))
    locust_json = json.dumps([{
        "name": "Aggregated", "method": "",
        "num_requests": 100, "num_failures": 0,
        "median_response_time": 10, "ninety_fifth_response_time": 20,
        "ninety_ninth_response_time": 30, "current_rps": 50.0,
    }])
    fake_proc = MagicMock(returncode=0, stdout=locust_json, stderr="")
    with patch("shutil.which", return_value="/usr/bin/locust"):
        with patch("subprocess.run", return_value=fake_proc):
            result = runners.run_locust_category(
                "backend_load", "rid-4", "s", "http://x",
                users=10, spawn_rate=2, run_time="5s",
            )
    assert result["category"] == "backend_load"
    assert "metrics" in result


# ── parametrized: every locust run_time → reasonable timeout ─────────────────

@pytest.mark.unit
@pytest.mark.parametrize("run_time,floor", [
    ("10s", 10), ("60s", 60), ("2m", 120), ("5m", 300),
])
def test_parse_locust_run_time_returns_at_least(run_time, floor):
    assert runners._parse_locust_run_time(run_time) >= floor
