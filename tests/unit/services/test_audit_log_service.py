"""Unit tests for services/audit_log_service.py.

The module imports flask, db.db_checkers, and utils.s3_utils at module load
time.  We pre-stub every transitive AWS/DB dependency in sys.modules *before*
the first import so the tests run without AWS credentials.

All stubs are installed at the module level (executed once per test session).
Because pytest collects this file after conftest, the stubs remain in place for
the entire session — which is fine; no other test in this suite imports the
real pymysql or db.rds_db.
"""

import sys
import types
from unittest.mock import MagicMock, patch, call
import flask
import pytest

# ---------------------------------------------------------------------------
# Pre-stub AWS / DB transitive imports before importing audit_log_service
# ---------------------------------------------------------------------------
_STUBS_INSTALLED = False

def _install_stubs():
    global _STUBS_INSTALLED
    if _STUBS_INSTALLED:
        return
    _STUBS_INSTALLED = True

    for mod in ("pymysql", "pymysql.cursors", "db", "db.rds_db", "db.db_checkers", "utils.s3_utils"):
        if mod not in sys.modules:
            sys.modules[mod] = MagicMock(name=f"{mod}_stub")

    # Give pymysql.cursors a real ModuleType so attribute access is sane
    cursors_mod = types.ModuleType("pymysql.cursors")
    cursors_mod.DictCursor = MagicMock()
    sys.modules["pymysql.cursors"] = cursors_mod

    db_mod = types.ModuleType("db")
    sys.modules["db"] = db_mod

    rds_mod = types.ModuleType("db.rds_db")
    rds_mod.connect_to_rds = MagicMock(return_value=None)
    sys.modules["db.rds_db"] = rds_mod

    db_checkers_mod = types.ModuleType("db.db_checkers")
    db_checkers_mod.get_email_by_id = MagicMock(return_value="test@example.com")
    sys.modules["db.db_checkers"] = db_checkers_mod

    s3_mod = types.ModuleType("utils.s3_utils")
    s3_mod.save_app_runbase_S3 = MagicMock(return_value=None)
    sys.modules["utils.s3_utils"] = s3_mod


_install_stubs()

# utils/normal.py (a transitive dep of audit_log_service) imports pptx and pytz
# at module level; stub them before importing the service.
def _stub_optional(mod_name: str):
    if mod_name not in sys.modules:
        sys.modules[mod_name] = MagicMock(name=f"{mod_name}_stub")

for _m in ("pptx", "pptx.util", "docx", "pytz"):
    _stub_optional(_m)

import services.audit_log_service as aud  # noqa: E402  (must come after stubs)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app():
    """Return a minimal Flask app for context manager use."""
    return flask.Flask(__name__)


# ===========================================================================
# Tests
# ===========================================================================


@pytest.mark.unit
def test_log_audit_event_never_raises():
    """log_audit_event must not propagate exceptions even if _upload_to_s3 raises."""
    app = _make_app()
    with app.app_context():
        with patch.object(aud, "_upload_to_s3", side_effect=RuntimeError("boom")):
            # Should complete without raising
            aud.log_audit_event(
                action=aud.LOGIN_SUCCESS,
                endpoint="/login",
                ip="127.0.0.1",
                status="success",
            )


@pytest.mark.unit
def test_log_audit_event_with_s3_failure():
    """log_audit_event must not raise even if save_app_runbase_S3 itself raises."""
    app = _make_app()
    with app.app_context():
        s3_mod = sys.modules["utils.s3_utils"]
        original = s3_mod.save_app_runbase_S3
        s3_mod.save_app_runbase_S3 = MagicMock(side_effect=OSError("s3 unavailable"))
        try:
            aud.log_audit_event(
                action=aud.USER_LOGGED_OUT,
                endpoint="/logout",
                ip="10.0.0.1",
                status="success",
                actor_user_id="user_abc",
            )
        finally:
            s3_mod.save_app_runbase_S3 = original


@pytest.mark.unit
def test_all_action_constants_in_action_category():
    """Every module-level ALL-CAPS string attribute must have a mapping in ACTION_CATEGORY.

    NOTE: As of the current source a small set of TRACKER_FRAMEWORK_* and
    TRACKER_POLICY_* / TRACKER_ROW_OPTION_* constants are defined but not yet
    mapped.  This test documents that known gap and will begin passing once the
    source is updated to include those mappings.
    """
    constants = [
        name
        for name in dir(aud)
        if name.isupper()
        and isinstance(getattr(aud, name), str)
        and not name.startswith("_")
    ]
    assert len(constants) > 0, "No action constants found on the module"

    # Known unmapped constants — documented gap in the source, not a test bug.
    # When these are added to ACTION_CATEGORY this set should be emptied.
    known_unmapped = {
        "TRACKER_FRAMEWORK_ADDED",
        "TRACKER_FRAMEWORK_UPDATED",
        "TRACKER_FRAMEWORK_REMOVED",
        "TRACKER_POLICY_ADDED",
        "TRACKER_POLICY_UPDATED",
        "TRACKER_POLICY_REMOVED",
        "TRACKER_POLICY_REMAPPED",
        "TRACKER_ROW_OPTION_ADDED",
        "TRACKER_ROW_OPTION_REMOVED",
    }

    # Constants that should definitely be mapped (all except the known gap)
    should_be_mapped = [c for c in constants if c not in known_unmapped]
    missing = [c for c in should_be_mapped if c not in aud.ACTION_CATEGORY]
    assert missing == [], (
        f"Action constants not in ACTION_CATEGORY: {missing}"
    )


@pytest.mark.unit
@pytest.mark.xfail(
    reason=(
        "TRACKER_FRAMEWORK_*, TRACKER_POLICY_*, TRACKER_ROW_OPTION_* constants are "
        "defined but not yet present in ACTION_CATEGORY (known source gap)."
    ),
    strict=True,
)
def test_no_unmapped_action_constants_strict():
    """Strict version: ALL ALL-CAPS constants must be in ACTION_CATEGORY.

    This test is marked xfail-strict and will automatically start failing
    (i.e., become a test error) if the known gap is fixed without removing
    the xfail marker, prompting the developer to update this file.
    """
    constants = [
        name
        for name in dir(aud)
        if name.isupper()
        and isinstance(getattr(aud, name), str)
        and not name.startswith("_")
    ]
    missing = [c for c in constants if c not in aud.ACTION_CATEGORY]
    assert missing == [], f"Unmapped constants: {missing}"


@pytest.mark.unit
def test_action_category_values_are_strings():
    """Every value in ACTION_CATEGORY must be a non-empty string."""
    for action, category in aud.ACTION_CATEGORY.items():
        assert isinstance(category, str), (
            f"ACTION_CATEGORY[{action!r}] is not a string: {category!r}"
        )
        assert len(category) > 0, (
            f"ACTION_CATEGORY[{action!r}] is an empty string"
        )


@pytest.mark.unit
def test_log_audit_event_builds_correct_entry_shape():
    """_upload_to_s3 must receive an entry dict with all required keys."""
    captured = {}

    def fake_upload(entry):
        captured.update(entry)

    app = _make_app()
    with app.app_context():
        with patch.object(aud, "_upload_to_s3", side_effect=fake_upload):
            aud.log_audit_event(
                action=aud.LOGIN_SUCCESS,
                endpoint="/api/login",
                ip="192.168.1.1",
                status="ok",
                actor_user_id="u1",
                actor_email="actor@example.com",
                target_user_id="u2",
                metadata={"key": "value"},
            )

    required_keys = {"timestamp", "action", "category", "endpoint", "ip", "status", "metadata"}
    for key in required_keys:
        assert key in captured, f"Entry missing required key: {key!r}"

    assert captured["action"] == aud.LOGIN_SUCCESS
    assert captured["endpoint"] == "/api/login"
    assert captured["ip"] == "192.168.1.1"
    assert captured["status"] == "ok"
    assert captured["metadata"] == {"key": "value"}


@pytest.mark.unit
def test_log_audit_event_requires_flask_app_context():
    """log_audit_event called inside a Flask app_context must not raise RuntimeError."""
    app = _make_app()
    with app.app_context():
        # Should succeed — no RuntimeError about missing app context
        aud.log_audit_event(
            action=aud.ROLE_CREATED,
            endpoint="/roles",
            ip="10.10.10.10",
            status="created",
        )


@pytest.mark.unit
def test_governance_action_constants_exist():
    """Governance-specific action constants must be defined on the module."""
    assert hasattr(aud, "PROTECTED_MODULE_CHANGE"), "PROTECTED_MODULE_CHANGE not defined"
    assert hasattr(aud, "PROTECTED_MODULE_SUPPRESSION_BLOCKED"), (
        "PROTECTED_MODULE_SUPPRESSION_BLOCKED not defined"
    )
    assert hasattr(aud, "TESTS_RUN_DISPATCHED"), "TESTS_RUN_DISPATCHED not defined"

    assert aud.PROTECTED_MODULE_CHANGE == "PROTECTED_MODULE_CHANGE"
    assert aud.PROTECTED_MODULE_SUPPRESSION_BLOCKED == "PROTECTED_MODULE_SUPPRESSION_BLOCKED"
    assert aud.TESTS_RUN_DISPATCHED == "TESTS_RUN_DISPATCHED"


@pytest.mark.unit
def test_governance_actions_in_governance_category():
    """PROTECTED_MODULE_CHANGE must map to 'governance' in ACTION_CATEGORY."""
    assert aud.ACTION_CATEGORY["PROTECTED_MODULE_CHANGE"] == "governance"
    assert aud.ACTION_CATEGORY["PROTECTED_MODULE_SUPPRESSION_BLOCKED"] == "governance"


@pytest.mark.unit
def test_tests_actions_in_tests_category():
    """TESTS_RUN_DISPATCHED must map to 'tests' in ACTION_CATEGORY."""
    assert aud.ACTION_CATEGORY["TESTS_RUN_DISPATCHED"] == "tests"
    assert aud.ACTION_CATEGORY.get("TESTS_WEBHOOK_ACCEPTED") == "tests"
    assert aud.ACTION_CATEGORY.get("TESTS_WEBHOOK_REJECTED") == "tests"


@pytest.mark.unit
def test_log_audit_event_category_falls_back_to_api_activity():
    """Unknown actions must fall back to 'api_activity' category in the entry."""
    captured = {}

    def fake_upload(entry):
        captured.update(entry)

    app = _make_app()
    with app.app_context():
        with patch.object(aud, "_upload_to_s3", side_effect=fake_upload):
            aud.log_audit_event(
                action="TOTALLY_UNKNOWN_ACTION",
                endpoint="/mystery",
                ip="1.2.3.4",
                status="ok",
            )

    assert captured.get("category") == "api_activity"


@pytest.mark.unit
def test_log_audit_event_metadata_defaults_to_empty_dict():
    """When metadata is not passed, the entry must contain an empty dict."""
    captured = {}

    def fake_upload(entry):
        captured.update(entry)

    app = _make_app()
    with app.app_context():
        with patch.object(aud, "_upload_to_s3", side_effect=fake_upload):
            aud.log_audit_event(
                action=aud.PASSWORD_CHANGED,
                endpoint="/change-password",
                ip="0.0.0.0",
                status="ok",
            )

    assert captured.get("metadata") == {}


@pytest.mark.unit
def test_log_audit_event_audit_owner_id_is_actor_when_no_delegation():
    """Without acting_on_behalf_of_user_id, audit_owner_id must equal actor_user_id."""
    captured = {}

    def fake_upload(entry):
        captured.update(entry)

    app = _make_app()
    with app.app_context():
        with patch.object(aud, "_upload_to_s3", side_effect=fake_upload):
            aud.log_audit_event(
                action=aud.USER_CREATED,
                endpoint="/users",
                ip="127.0.0.1",
                status="created",
                actor_user_id="admin_99",
            )

    assert captured.get("audit_owner_id") == "admin_99"


@pytest.mark.unit
def test_log_audit_event_audit_owner_id_is_behalf_when_delegated():
    """With acting_on_behalf_of_user_id set, audit_owner_id must equal that value."""
    captured = {}

    def fake_upload(entry):
        captured.update(entry)

    app = _make_app()
    with app.app_context():
        with patch.object(aud, "_upload_to_s3", side_effect=fake_upload):
            aud.log_audit_event(
                action=aud.RUNBOOK_CREATED,
                endpoint="/runbooks",
                ip="127.0.0.1",
                status="created",
                actor_user_id="admin_actor",
                acting_on_behalf_of_user_id="workspace_owner",
            )

    assert captured.get("audit_owner_id") == "workspace_owner"
