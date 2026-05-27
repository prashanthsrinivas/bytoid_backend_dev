"""Subprocess safety tests. Verifies that subprocess calls in this codebase use
list-form commands (never shell=True) to prevent command injection."""

import inspect
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub heavy transitive dependencies BEFORE importing anything that touches them
# ---------------------------------------------------------------------------

_HEAVY_MODS = [
    "pymysql",
    "pymysql.cursors",
    "db",
    "db.rds_db",
    "db.db_checkers",
    "boto3",
    "dotenv",
    "dbutils",
    "dbutils.pooled_db",
    "pptx",
    "pptx.util",
    "bs4",
    "pytz",
    "yaml",
    "docx",
]
for _mod in _HEAVY_MODS:
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

_s3_stub = sys.modules.get("utils.s3_utils") or MagicMock(name="s3_utils_stub")
_s3_stub.save_app_runbase_S3 = MagicMock(return_value=None)
sys.modules["utils.s3_utils"] = _s3_stub

sys.modules.setdefault("utils.base_logger", MagicMock(name="base_logger_stub"))

_celery_base = sys.modules.get("utils.celery_base") or MagicMock(name="celery_base_stub")
sys.modules["utils.celery_base"] = _celery_base

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent.parent  # tests/security/infra → repo root
_SEMGREP_PROTECTED = _REPO_ROOT / ".semgrep" / "protected"


# ---------------------------------------------------------------------------
# Tests — source inspection
# ---------------------------------------------------------------------------

@pytest.mark.security
@pytest.mark.infra
def test_run_pytest_category_uses_list_not_shell_string():
    """run_pytest_category source must not contain shell=True."""
    from tests_routes.runners import run_pytest_category
    source = inspect.getsource(run_pytest_category)
    assert "shell=True" not in source


@pytest.mark.security
@pytest.mark.infra
def test_run_locust_category_uses_list_not_shell_string():
    """run_locust_category source must not contain shell=True."""
    from tests_routes.runners import run_locust_category
    source = inspect.getsource(run_locust_category)
    assert "shell=True" not in source


@pytest.mark.security
@pytest.mark.infra
def test_runners_module_source_has_no_shell_equals_true():
    """The entire runners.py module source must not contain shell=True anywhere."""
    runners_path = _REPO_ROOT / "tests_routes" / "runners.py"
    source = runners_path.read_text(encoding="utf-8")
    assert "shell=True" not in source


@pytest.mark.security
@pytest.mark.infra
def test_subprocess_cmd_is_always_a_list():
    """run_pytest_category passes a list (not a string) as the first arg to subprocess.run."""
    import subprocess

    from tests_routes.runners import run_pytest_category

    captured_cmd = []

    def _fake_run(cmd, **kwargs):
        captured_cmd.append(cmd)
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=_fake_run), \
         patch("tests_routes.runners.write_category_result"), \
         patch("os.makedirs"), \
         patch("os.path.exists", return_value=False):
        run_pytest_category(
            category="backend_unit",
            run_id="test-run-001",
            pytest_targets=["tests/"],
            timeout_seconds=10,
        )

    assert len(captured_cmd) == 1, "subprocess.run should have been called exactly once"
    cmd = captured_cmd[0]
    assert isinstance(cmd, list), (
        f"subprocess.run first arg must be a list, got {type(cmd).__name__}: {cmd!r}"
    )


# ---------------------------------------------------------------------------
# Tests — Semgrep rule file existence
# ---------------------------------------------------------------------------

@pytest.mark.security
@pytest.mark.infra
def test_protected_semgrep_rule_for_serializer_pickle_exists():
    """The no_serializer_pickle.yml Semgrep rule must exist and contain 'pickle'."""
    rule_path = _SEMGREP_PROTECTED / "no_serializer_pickle.yml"
    assert rule_path.exists(), f"Missing Semgrep rule: {rule_path}"
    content = rule_path.read_text(encoding="utf-8")
    assert "pickle" in content


@pytest.mark.security
@pytest.mark.infra
def test_protected_semgrep_rule_for_crypto_downgrade_exists():
    """The no_crypto_downgrade.yml Semgrep rule must exist and contain 'MD5' or 'md5'."""
    rule_path = _SEMGREP_PROTECTED / "no_crypto_downgrade.yml"
    assert rule_path.exists(), f"Missing Semgrep rule: {rule_path}"
    content = rule_path.read_text(encoding="utf-8")
    assert "MD5" in content or "md5" in content


@pytest.mark.security
@pytest.mark.infra
def test_protected_semgrep_rule_no_authz_removal_exists():
    """The no_authz_removal.yml Semgrep rule must exist and reference 'permission_required'."""
    rule_path = _SEMGREP_PROTECTED / "no_authz_removal.yml"
    assert rule_path.exists(), f"Missing Semgrep rule: {rule_path}"
    content = rule_path.read_text(encoding="utf-8")
    assert "permission_required" in content


@pytest.mark.security
@pytest.mark.infra
def test_protected_semgrep_rule_no_audit_log_removal_exists():
    """The no_audit_log_removal.yml Semgrep rule must exist and reference 'log_audit_event'."""
    rule_path = _SEMGREP_PROTECTED / "no_audit_log_removal.yml"
    assert rule_path.exists(), f"Missing Semgrep rule: {rule_path}"
    content = rule_path.read_text(encoding="utf-8")
    assert "log_audit_event" in content
