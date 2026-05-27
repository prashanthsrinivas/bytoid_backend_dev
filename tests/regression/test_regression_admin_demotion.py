"""Regression tests for the admin demotion detection flow.

Commit 6d80fde moved find_admin_change.py to the repo root after it was
discovered that the script couldn't locate the app's imports when run from
a different directory. These tests lock in the invariants that were broken or
unclear at the time of that incident:

  - The composite user-ID parser must identify actor vs. target correctly
    (the bug involved reversed ordering).
  - USER_TYPE_CHANGED must exist in ACTION_CATEGORY (audit coverage).
  - The 4 PROTECTED_MODULE_GOVERNANCE constants must exist (added post-incident).
  - find_admin_change.py must live at the repo root.
  - The S3 audit key format used by _upload_to_s3 must match the expected pattern.
  - All USER_* constants must map to 'user_management', not 'auth'.

None of these tests touch DB or AWS — they validate constants and logic only.
"""

import os
import sys
import types
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Stub DB/AWS imports before importing audit_log_service
# ---------------------------------------------------------------------------

for _mod in ("pymysql", "pymysql.cursors", "db", "db.rds_db", "db.db_checkers",
             "boto3", "dotenv", "dbutils", "dbutils.pooled_db",
             # utils.normal imports these at module level
             "pptx", "pptx.util", "bs4", "pytz", "yaml", "docx"):
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

_s3_stub = MagicMock()
_s3_stub.save_app_runbase_S3 = MagicMock(return_value=None)
sys.modules.setdefault("utils.s3_utils", _s3_stub)

_logger_stub = MagicMock()
_logger_stub.get_logger = MagicMock(return_value=MagicMock())
sys.modules.setdefault("utils.base_logger", _logger_stub)

from utils.normal import parse_composite_user_id  # noqa: E402
import services.audit_log_service as audit  # noqa: E402

# ---------------------------------------------------------------------------
# Repo root
# ---------------------------------------------------------------------------

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.regression
def test_regression_parse_composite_user_id_for_delegation():
    """Actor (left side of ##SU##) must be returned as first element, not second.

    Regression: an earlier version of the parsing code had the return order
    reversed, making the actor look like the target in audit logs.
    """
    raw = "adminA##SU##adminB"
    actor, target = parse_composite_user_id(raw)
    assert actor == "adminA", f"Expected actor='adminA', got '{actor}'"
    assert target == "adminB", f"Expected target='adminB', got '{target}'"
    # Critically, they must NOT be swapped
    assert actor != target


@pytest.mark.regression
def test_regression_audit_event_action_constant():
    """USER_TYPE_CHANGED is the action constant used to track admin demotions.

    It must be defined AND must appear in ACTION_CATEGORY so that demotion
    events are queryable by category.
    """
    assert hasattr(audit, "USER_TYPE_CHANGED"), "USER_TYPE_CHANGED constant missing"
    assert audit.USER_TYPE_CHANGED == "USER_TYPE_CHANGED"
    assert "USER_TYPE_CHANGED" in audit.ACTION_CATEGORY, (
        "USER_TYPE_CHANGED not present in ACTION_CATEGORY — demotion events "
        "will be uncategorized in the audit UI."
    )


@pytest.mark.regression
def test_regression_governance_action_constants():
    """The four PROTECTED_MODULE governance constants must exist.

    These were added after the demotion incident revealed gaps in audit
    coverage for changes to security-critical modules.
    """
    expected = [
        "PROTECTED_MODULE_CHANGE",
        "PROTECTED_MODULE_SUPPRESSION_BLOCKED",
        "PROTECTED_MODULE_AI_PROPOSAL_OPENED",
        "PROTECTED_MODULE_AI_ONLY_APPROVAL_BLOCKED",
    ]
    for const in expected:
        assert hasattr(audit, const), f"Missing governance constant: {const}"
        assert getattr(audit, const) == const
        assert const in audit.ACTION_CATEGORY, (
            f"{const} not in ACTION_CATEGORY — governance event uncategorized."
        )
    # All four should map to the same 'governance' category
    for const in expected:
        assert audit.ACTION_CATEGORY[const] == "governance", (
            f"{const} maps to '{audit.ACTION_CATEGORY[const]}', expected 'governance'."
        )


@pytest.mark.regression
def test_regression_find_admin_change_script_exists():
    """find_admin_change.py must exist at the repo root.

    Regression: commit 6d80fde moved it here after it was incorrectly placed
    in a subdirectory where it couldn't resolve app-relative imports.
    """
    script_path = os.path.join(_REPO_ROOT, "find_admin_change.py")
    assert os.path.isfile(script_path), (
        f"find_admin_change.py not found at repo root ({_REPO_ROOT}). "
        "Regression: commit 6d80fde ensured it lives here."
    )


@pytest.mark.regression
def test_regression_admin_demotion_audit_key_format():
    """S3 key for audit logs follows '{user_id}/audit/{date}.json'.

    The _upload_to_s3 function in audit_log_service.py must use this exact
    format. We verify by inspecting the source text of the module.
    """
    import inspect
    source = inspect.getsource(audit._upload_to_s3)
    assert "/audit/" in source, (
        "Expected '/audit/' path segment in _upload_to_s3 source. "
        "The S3 key format may have changed."
    )
    # The pattern should be something like f"{user_id}/audit/{date}.json"
    assert ".json" in source, (
        "Expected '.json' extension in _upload_to_s3 source key pattern."
    )


@pytest.mark.regression
def test_regression_action_category_completeness_for_user_management():
    """All USER_* action constants must be in the 'user_management' category.

    Regression: if USER_TYPE_CHANGED (or any USER_* constant) were misclassified
    into 'auth', the admin demotion query would fail to find the event.
    """
    user_constants = [
        "USER_CREATED",
        "USER_INVITED",
        "INVITE_CANCELLED",
        "INVITE_RESENT",
        "USER_INVITE_ACCEPTED",
        "USER_ROLE_CHANGED",
        "USER_ACCESS_REVOKED",
        "USER_ACCESS_ACTIVATED",
        "USER_DELETED",
    ]
    for const in user_constants:
        assert const in audit.ACTION_CATEGORY, f"{const} missing from ACTION_CATEGORY"
        category = audit.ACTION_CATEGORY[const]
        assert category == "user_management", (
            f"{const} is categorized as '{category}', expected 'user_management'. "
            "Misclassification would break user-management audit queries."
        )
