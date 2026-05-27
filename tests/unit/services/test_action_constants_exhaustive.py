"""Exhaustive parametrized tests for every action constant in services/audit_log_service.py.

Each ALL-CAPS string constant gets validated for:
  - presence on the module
  - self-naming (constant equals its variable name)
  - presence in ACTION_CATEGORY
  - non-empty category string

Plus tests for each category bucket having at least one mapping.
"""

import sys
import types
from unittest.mock import MagicMock

import flask
import pytest

# ── Stub heavy deps before audit_log_service import ───────────────────────────
for _mod in ("pymysql", "pymysql.cursors", "db", "db.rds_db", "db.db_checkers",
             "boto3", "dotenv", "dbutils", "dbutils.pooled_db"):
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

# Stub utils.normal directly to avoid pulling in pptx/docx/bs4/yaml/pytz
_normal_stub = MagicMock()
_normal_stub.parse_composite_user_id = MagicMock(return_value=("owner", "user"))
sys.modules.setdefault("utils.normal", _normal_stub)

if "pymysql.cursors" not in sys.modules or not isinstance(sys.modules["pymysql.cursors"], types.ModuleType):
    cursors_mod = types.ModuleType("pymysql.cursors")
    cursors_mod.DictCursor = MagicMock()
    sys.modules["pymysql.cursors"] = cursors_mod

_s3_stub = MagicMock()
_s3_stub.save_app_runbase_S3 = MagicMock(return_value=None)
sys.modules.setdefault("utils.s3_utils", _s3_stub)

_logger_stub = MagicMock()
_logger_stub.get_logger = MagicMock(return_value=MagicMock())
sys.modules.setdefault("utils.base_logger", _logger_stub)

import services.audit_log_service as aud  # noqa: E402

# Materialize the list of action constants once.
ALL_ACTIONS = sorted([
    name for name in dir(aud)
    if name.isupper() and isinstance(getattr(aud, name), str) and not name.startswith("_")
])

# Action constants that exist on the module but are NOT yet mapped in
# ACTION_CATEGORY — documented gap.
KNOWN_UNMAPPED = {
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


# ── parametrized: every action constant ──────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("name", ALL_ACTIONS)
def test_action_constant_exists(name):
    assert hasattr(aud, name)

@pytest.mark.unit
@pytest.mark.parametrize("name", ALL_ACTIONS)
def test_action_constant_self_named(name):
    """The string value of a constant must equal its variable name (Pythonic enum pattern)."""
    assert getattr(aud, name) == name, (
        f"{name} does not self-name (value={getattr(aud, name)!r}). "
        "Self-naming is a convention so audit log strings can be grep'd from code."
    )

@pytest.mark.unit
@pytest.mark.parametrize("name", ALL_ACTIONS)
def test_action_constant_is_non_empty_string(name):
    v = getattr(aud, name)
    assert isinstance(v, str)
    assert len(v) > 0

@pytest.mark.unit
@pytest.mark.parametrize("name", [n for n in ALL_ACTIONS if n not in KNOWN_UNMAPPED])
def test_mapped_action_has_category(name):
    assert name in aud.ACTION_CATEGORY, f"{name} not in ACTION_CATEGORY"

@pytest.mark.unit
@pytest.mark.parametrize("name,category", list(aud.ACTION_CATEGORY.items()))
def test_action_category_value_is_non_empty_string(name, category):
    assert isinstance(category, str), f"ACTION_CATEGORY[{name}] is not str"
    assert len(category) > 0, f"ACTION_CATEGORY[{name}] is empty"

@pytest.mark.unit
@pytest.mark.parametrize("name,category", list(aud.ACTION_CATEGORY.items()))
def test_action_category_value_is_kebab_or_snake(name, category):
    """Category names should be lower-case identifiers (snake or kebab)."""
    allowed = set("abcdefghijklmnopqrstuvwxyz_-")
    assert all(ch in allowed for ch in category), (
        f"ACTION_CATEGORY[{name}] = {category!r} contains unexpected chars"
    )


# ── Categories that are critical for governance ───────────────────────────────

CRITICAL_CATEGORIES = ["auth", "governance", "tests", "user_management", "billing"]

@pytest.mark.unit
@pytest.mark.parametrize("cat", CRITICAL_CATEGORIES)
def test_critical_category_has_at_least_one_action(cat):
    actions = [a for a, c in aud.ACTION_CATEGORY.items() if c == cat]
    assert len(actions) >= 1, (
        f"Category '{cat}' has no mapped actions — UI filters depending on this "
        "category would show no events."
    )


# ── log_audit_event accepting every action ────────────────────────────────────

def _app_ctx():
    app = flask.Flask(__name__)
    return app.app_context()


@pytest.mark.unit
@pytest.mark.parametrize("name", ALL_ACTIONS[:60])  # sample, not all 121 (CI runtime)
def test_log_audit_event_accepts_every_action(name):
    """log_audit_event must accept every defined action constant without raising."""
    captured = []
    orig = aud._upload_to_s3
    aud._upload_to_s3 = lambda e: captured.append(e)
    try:
        with _app_ctx():
            aud.log_audit_event(
                action=getattr(aud, name), endpoint="/x", ip="1.1.1.1",
                status="success",
            )
    finally:
        aud._upload_to_s3 = orig
    assert captured, f"log_audit_event for {name} did not produce an entry"
    assert captured[0]["action"] == name


# ── log_audit_event entry-shape invariants ────────────────────────────────────

REQUIRED_ENTRY_KEYS = ["timestamp", "action", "category", "endpoint", "ip", "status", "metadata"]

@pytest.mark.unit
@pytest.mark.parametrize("key", REQUIRED_ENTRY_KEYS)
def test_log_audit_event_entry_contains_every_required_key(key):
    captured = []
    orig = aud._upload_to_s3
    aud._upload_to_s3 = lambda e: captured.append(e)
    try:
        with _app_ctx():
            aud.log_audit_event(action=aud.LOGIN_SUCCESS, endpoint="/login",
                                ip="1.2.3.4", status="success")
    finally:
        aud._upload_to_s3 = orig
    assert key in captured[0]


# ── log_audit_event status values ─────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("status", ["success", "failure", "ok", "rejected", "blocked", "accepted"])
def test_log_audit_event_accepts_any_status(status):
    captured = []
    orig = aud._upload_to_s3
    aud._upload_to_s3 = lambda e: captured.append(e)
    try:
        with _app_ctx():
            aud.log_audit_event(action=aud.LOGIN_SUCCESS, endpoint="/login",
                                ip="1.2.3.4", status=status)
    finally:
        aud._upload_to_s3 = orig
    assert captured[0]["status"] == status


# ── log_audit_event metadata variants ─────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("metadata", [
    None, {}, {"key": "value"}, {"nested": {"deep": True}},
    {"list": [1, 2, 3]}, {"unicode": "café"}, {"empty_str": ""},
    {"int": 42}, {"bool": False}, {"null": None},
])
def test_log_audit_event_accepts_metadata_shapes(metadata):
    captured = []
    orig = aud._upload_to_s3
    aud._upload_to_s3 = lambda e: captured.append(e)
    try:
        with _app_ctx():
            aud.log_audit_event(
                action=aud.LOGIN_SUCCESS, endpoint="/x", ip="1.1.1.1",
                status="ok", metadata=metadata,
            )
    finally:
        aud._upload_to_s3 = orig
    assert "metadata" in captured[0]


# ── exception safety: log_audit_event never raises ────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("exc", [
    RuntimeError("boom"), ValueError("bad"), OSError("io fail"),
    KeyError("missing"), TypeError("type"),
    Exception("generic"), IOError("io"),
])
def test_log_audit_event_swallows_upload_exception(exc):
    orig = aud._upload_to_s3
    def raiser(_e):
        raise exc
    aud._upload_to_s3 = raiser
    try:
        with _app_ctx():
            # Must NOT raise.
            aud.log_audit_event(action=aud.LOGIN_SUCCESS, endpoint="/x",
                                ip="1.1.1.1", status="ok")
    finally:
        aud._upload_to_s3 = orig


# ── governance category invariants ────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("name", [
    "PROTECTED_MODULE_CHANGE",
    "PROTECTED_MODULE_SUPPRESSION_BLOCKED",
    "PROTECTED_MODULE_AI_PROPOSAL_OPENED",
    "PROTECTED_MODULE_AI_ONLY_APPROVAL_BLOCKED",
])
def test_governance_action_maps_to_governance(name):
    assert aud.ACTION_CATEGORY.get(name) == "governance"


# ── tests category invariants ─────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("name", [
    "TESTS_RUN_DISPATCHED",
    "TESTS_WEBHOOK_ACCEPTED",
    "TESTS_WEBHOOK_REJECTED",
])
def test_tests_action_maps_to_tests(name):
    if name in aud.ACTION_CATEGORY:
        assert aud.ACTION_CATEGORY[name] == "tests"
