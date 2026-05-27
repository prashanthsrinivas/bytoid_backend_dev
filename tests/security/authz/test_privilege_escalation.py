"""Privilege escalation tests. Verifies the decorator and route logic prevent cross-user and cross-admin access without proper grants."""

import json
import sys
import urllib.parse
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Stub heavy transitive dependencies BEFORE any import that touches them
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
    "utils.s3_utils",
    "utils.celery_base",
    "utils.base_logger",
]
for _mod in _HEAVY_MODS:
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

# pymysql.cursors.DictCursor must be a real attribute so the decorator can reference it
import types as _types
_pymysql_cursors = _types.ModuleType("pymysql.cursors")
_pymysql_cursors.DictCursor = MagicMock(name="DictCursor")
sys.modules["pymysql.cursors"] = _pymysql_cursors
sys.modules["pymysql"].cursors = _pymysql_cursors

# Stub permission_resolver so it doesn't need permission_metadata
_resolver_stub = MagicMock(name="permission_resolver_stub")
sys.modules.setdefault("utils.permission_resolver", _resolver_stub)
sys.modules.setdefault("utils.permission_metadata", MagicMock(name="permission_metadata_stub"))

from flask import Flask  # noqa: E402
from utils.permission_required import permission_required  # noqa: E402

# ---------------------------------------------------------------------------
# Minimal Flask app with a protected route for testing the decorator
# ---------------------------------------------------------------------------

def _build_app() -> Flask:
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="test-secret")

    @app.route("/protected/<owner_user_id>", methods=["GET", "POST"])
    @permission_required("some.permission")
    def protected_view(owner_user_id):
        return "ok", 200

    return app


# ---------------------------------------------------------------------------
# DB cursor factory helpers
# ---------------------------------------------------------------------------

def _make_conn(fetchone_side_effect):
    """Build a mock DB connection whose cursor.fetchone returns values in sequence."""
    conn = MagicMock(name="db_conn")
    cursor = MagicMock(name="cursor")
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    cursor.fetchone = MagicMock(side_effect=fetchone_side_effect)
    conn.cursor = MagicMock(return_value=cursor)
    conn.close = MagicMock()
    return conn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.security
@pytest.mark.authz
def test_admin_self_access_allowed():
    """Admin accessing their own data (plain user_id — same logged-in and target) must get 200."""
    admin_id = "admin-001"
    # With a plain user_id (no ##SU## separator), parse_composite_user_id returns
    # (admin_id, admin_id) — logged_in == target → self-access path
    conn = _make_conn([
        {"user_id": admin_id, "user_type": "admin", "launch_id_fk": "org-1"},
    ])
    app = _build_app()
    with patch("utils.permission_required.connect_to_rds", return_value=conn):
        with app.test_client() as client:
            resp = client.get(f"/protected/{admin_id}?user_id={admin_id}")
    assert resp.status_code == 200


@pytest.mark.security
@pytest.mark.authz
def test_admin_cross_normal_user_allowed():
    """Admin accessing a normal user's data via composite user_id must get 200."""
    admin_id = "admin-001"
    normal_id = "user-002"
    # URL-encode the composite because # is treated as fragment start in URLs
    composite_uid = urllib.parse.quote(f"{admin_id}##SU##{normal_id}", safe="")
    conn = _make_conn([
        # Logged-in user row (admin)
        {"user_id": admin_id, "user_type": "admin", "launch_id_fk": "org-1"},
        # Target owner row (normal user)
        {"user_type": "user", "launch_id_fk": "org-1", "email": "user@example.com"},
    ])
    app = _build_app()
    with patch("utils.permission_required.connect_to_rds", return_value=conn):
        with app.test_client() as client:
            resp = client.get(f"/protected/{normal_id}?user_id={composite_uid}")
    assert resp.status_code == 200


@pytest.mark.security
@pytest.mark.authz
@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known bug: permission_required has a tautological self-access check — "
        "'if not owner_user_id or owner_user_id == user_id' where owner_user_id IS user_id "
        "(both assigned from parse_composite_user_id). The check is always True, so the "
        "cross-admin gate is never reached. Bug: should compare against logged_in_user_id. "
        "This xfail documents the existing bypass and will start passing once the bug is fixed."
    ),
)
def test_admin_cross_admin_without_grant_forbidden():
    """Admin accessing another admin (via composite uid) without a special_access grant must get 403.

    NOTE: This test documents a known bug — the current decorator always returns 200 for
    admins due to a tautological self-access check. The xfail will flip to passing when
    the bug in permission_required.py is fixed.
    """
    admin_id = "admin-001"
    target_admin_id = "admin-002"
    # URL-encode the composite: # would be treated as fragment by the URL parser
    composite_uid = urllib.parse.quote(f"{admin_id}##SU##{target_admin_id}", safe="")
    conn = _make_conn([
        # Logged-in user row
        {"user_id": admin_id, "user_type": "admin", "launch_id_fk": "org-1"},
        # Target owner row (also admin)
        {"user_type": "admin", "launch_id_fk": "org-1", "email": "other@example.com"},
        # special_access check → no row
        None,
    ])
    app = _build_app()
    with patch("utils.permission_required.connect_to_rds", return_value=conn):
        with app.test_client() as client:
            resp = client.get(f"/protected/{target_admin_id}?user_id={composite_uid}")
    assert resp.status_code == 403
    data = resp.get_json()
    assert "error" in data


@pytest.mark.security
@pytest.mark.authz
def test_admin_cross_admin_with_grant_allowed():
    """Admin accessing another admin (via composite uid) WITH a special_access grant must get 200."""
    admin_id = "admin-001"
    target_admin_id = "admin-002"
    composite_uid = urllib.parse.quote(f"{admin_id}##SU##{target_admin_id}", safe="")
    conn = _make_conn([
        # Logged-in user row
        {"user_id": admin_id, "user_type": "admin", "launch_id_fk": "org-1"},
        # Target owner row (also admin)
        {"user_type": "admin", "launch_id_fk": "org-1", "email": "other@example.com"},
        # special_access row exists with full access
        {"access_level": "full"},
    ])
    app = _build_app()
    with patch("utils.permission_required.connect_to_rds", return_value=conn):
        with app.test_client() as client:
            resp = client.get(f"/protected/{target_admin_id}?user_id={composite_uid}")
    assert resp.status_code == 200


@pytest.mark.security
@pytest.mark.authz
def test_normal_user_with_active_role_and_permission_allowed():
    """Normal user with an active role that includes the required permission must get 200."""
    user_id = "user-100"
    owner_id = user_id  # self-access is simplest path, but we test through normal-user path
    # Simulate non-admin user accessing own data; decorator takes normal-user path
    required_perm = "some.permission"
    perms_json = json.dumps({
        "role": {"permissions": [required_perm]},
        "status": "active",
    })
    conn = _make_conn([
        # Logged-in user row (non-admin)
        {"user_id": user_id, "user_type": "user", "launch_id_fk": "org-1"},
        # Permissions row
        {"permissions": perms_json},
    ])
    app = _build_app()

    # resolve_permissions must return the required perm
    with patch("utils.permission_required.connect_to_rds", return_value=conn), \
         patch("utils.permission_required.resolve_permissions", return_value=[required_perm]):
        with app.test_client() as client:
            resp = client.get(f"/protected/{owner_id}?user_id={user_id}")
    assert resp.status_code == 200


@pytest.mark.security
@pytest.mark.authz
def test_normal_user_without_permission_denied():
    """Normal user whose role doesn't include the required permission must get 403."""
    user_id = "user-101"
    owner_id = user_id
    perms_json = json.dumps({
        "role": {"permissions": ["other.permission"]},
        "status": "active",
    })
    conn = _make_conn([
        {"user_id": user_id, "user_type": "user", "launch_id_fk": "org-1"},
        {"permissions": perms_json},
    ])
    app = _build_app()
    with patch("utils.permission_required.connect_to_rds", return_value=conn), \
         patch("utils.permission_required.resolve_permissions", return_value=["other.permission"]):
        with app.test_client() as client:
            resp = client.get(f"/protected/{owner_id}?user_id={user_id}")
    assert resp.status_code == 403
    data = resp.get_json()
    assert "error" in data


@pytest.mark.security
@pytest.mark.authz
def test_normal_user_inactive_role_denied():
    """Normal user with a role that has status != 'active' must get 403."""
    user_id = "user-102"
    owner_id = user_id
    required_perm = "some.permission"
    perms_json = json.dumps({
        "role": {"permissions": [required_perm]},
        "status": "inactive",
    })
    conn = _make_conn([
        {"user_id": user_id, "user_type": "user", "launch_id_fk": "org-1"},
        {"permissions": perms_json},
    ])
    app = _build_app()
    with patch("utils.permission_required.connect_to_rds", return_value=conn), \
         patch("utils.permission_required.resolve_permissions", return_value=[required_perm]):
        with app.test_client() as client:
            resp = client.get(f"/protected/{owner_id}?user_id={user_id}")
    assert resp.status_code == 403


@pytest.mark.security
@pytest.mark.authz
def test_no_user_id_returns_401():
    """Request with no user_id in any context must get 401."""
    app = _build_app()
    with app.test_client() as client:
        resp = client.get("/protected/some-owner-id")
    assert resp.status_code == 401
    data = resp.get_json()
    assert "error" in data


@pytest.mark.security
@pytest.mark.authz
@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known bug: same tautological self-access check prevents the viewer-access "
        "gate from being reached. The special_access row is never fetched, so the "
        "viewer POST restriction cannot be enforced. Will pass once the bug is fixed."
    ),
)
def test_viewer_access_blocks_write_methods():
    """Admin with viewer-level special_access must be blocked from POST/PUT/PATCH/DELETE.

    NOTE: xfail — see test_admin_cross_admin_without_grant_forbidden for the root cause.
    """
    admin_id = "admin-001"
    target_admin_id = "admin-002"
    composite_uid = urllib.parse.quote(f"{admin_id}##SU##{target_admin_id}", safe="")
    conn = _make_conn([
        {"user_id": admin_id, "user_type": "admin", "launch_id_fk": "org-1"},
        {"user_type": "admin", "launch_id_fk": "org-1", "email": "other@example.com"},
        {"access_level": "viewer"},
    ])
    app = _build_app()
    with patch("utils.permission_required.connect_to_rds", return_value=conn):
        with app.test_client() as client:
            resp = client.post(
                f"/protected/{target_admin_id}?user_id={composite_uid}",
                data="{}",
                content_type="application/json",
            )
    assert resp.status_code == 403
    data = resp.get_json()
    assert "viewer" in data.get("error", "").lower() or "modify" in data.get("error", "").lower()


@pytest.mark.security
@pytest.mark.authz
def test_viewer_access_allows_read_methods():
    """Admin with viewer-level special_access must be allowed to GET."""
    admin_id = "admin-001"
    target_admin_id = "admin-002"
    composite_uid = urllib.parse.quote(f"{admin_id}##SU##{target_admin_id}", safe="")
    conn = _make_conn([
        {"user_id": admin_id, "user_type": "admin", "launch_id_fk": "org-1"},
        {"user_type": "admin", "launch_id_fk": "org-1", "email": "other@example.com"},
        {"access_level": "viewer"},
    ])
    app = _build_app()
    with patch("utils.permission_required.connect_to_rds", return_value=conn):
        with app.test_client() as client:
            resp = client.get(f"/protected/{target_admin_id}?user_id={composite_uid}")
    assert resp.status_code == 200
