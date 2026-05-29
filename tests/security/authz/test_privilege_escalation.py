"""Privilege escalation tests. Verifies the decorator and route logic prevent cross-user and cross-admin access without proper grants.

Identity model under test: the ACTING user comes from the authenticated session
(set here via ``session_transaction``); the request-supplied ``user_id`` is only
ever the *target/owner* being accessed. A request with no session is
unauthenticated and must be rejected.
"""

import json
import sys
import urllib.parse
from unittest.mock import MagicMock, patch

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


def _do_request(app, conn, *, actor_id, target_id, method="get", patches=None, totp_pending=False):
    """Drive a request through the protected route.

    `actor_id` is stamped into the session (the authenticated caller). `target_id`
    is the owner being accessed, passed as the request's `user_id` (None to omit).
    `totp_pending` simulates a password-authenticated session that hasn't yet
    completed 2FA.
    """
    ctx_patches = [patch("utils.permission_required.connect_to_rds", return_value=conn)]
    for p in patches or []:
        ctx_patches.append(p)

    import contextlib

    with contextlib.ExitStack() as stack:
        for p in ctx_patches:
            stack.enter_context(p)
        with app.test_client() as client:
            if actor_id is not None:
                with client.session_transaction() as sess:
                    sess["user_id"] = actor_id
                    if totp_pending:
                        sess["totp_pending"] = True
            path_owner = target_id if target_id is not None else "some-owner-id"
            url = f"/protected/{urllib.parse.quote(str(path_owner), safe='')}"
            if target_id is not None:
                url += f"?user_id={urllib.parse.quote(str(target_id), safe='')}"
            kwargs = {}
            if method == "post":
                kwargs = {"data": "{}", "content_type": "application/json"}
            return getattr(client, method)(url, **kwargs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.security
@pytest.mark.authz
def test_admin_self_access_allowed():
    """Admin accessing their own data (target == session actor) must get 200."""
    admin_id = "admin-001"
    conn = _make_conn([
        {"user_id": admin_id, "user_type": "admin", "launch_id_fk": "org-1"},
    ])
    app = _build_app()
    resp = _do_request(app, conn, actor_id=admin_id, target_id=admin_id)
    assert resp.status_code == 200


@pytest.mark.security
@pytest.mark.authz
def test_admin_cross_normal_user_allowed():
    """Admin accessing a normal user's data (same org) must get 200."""
    admin_id = "admin-001"
    normal_id = "user-002"
    conn = _make_conn([
        # Logged-in user row (admin)
        {"user_id": admin_id, "user_type": "admin", "launch_id_fk": "org-1"},
        # Target owner row (normal user)
        {"user_type": "user", "launch_id_fk": "org-1", "email": "user@example.com"},
    ])
    app = _build_app()
    resp = _do_request(app, conn, actor_id=admin_id, target_id=normal_id)
    assert resp.status_code == 200


@pytest.mark.security
@pytest.mark.authz
def test_admin_cross_org_normal_user_forbidden():
    """Admin accessing a normal user in a DIFFERENT org must get 403."""
    admin_id = "admin-001"
    normal_id = "user-002"
    conn = _make_conn([
        {"user_id": admin_id, "user_type": "admin", "launch_id_fk": "org-1"},
        {"user_type": "user", "launch_id_fk": "org-2", "email": "user@example.com"},
    ])
    app = _build_app()
    resp = _do_request(app, conn, actor_id=admin_id, target_id=normal_id)
    assert resp.status_code == 403


@pytest.mark.security
@pytest.mark.authz
def test_admin_cross_admin_without_grant_forbidden():
    """Admin accessing another admin without a special_access grant must get 403."""
    admin_id = "admin-001"
    target_admin_id = "admin-002"
    conn = _make_conn([
        # Logged-in user row
        {"user_id": admin_id, "user_type": "admin", "launch_id_fk": "org-1"},
        # Target owner row (also admin)
        {"user_type": "admin", "launch_id_fk": "org-1", "email": "other@example.com"},
        # special_access check → no row
        None,
    ])
    app = _build_app()
    resp = _do_request(app, conn, actor_id=admin_id, target_id=target_admin_id)
    assert resp.status_code == 403
    data = resp.get_json()
    assert "error" in data


@pytest.mark.security
@pytest.mark.authz
def test_admin_cross_admin_with_grant_allowed():
    """Admin accessing another admin WITH a special_access grant must get 200."""
    admin_id = "admin-001"
    target_admin_id = "admin-002"
    conn = _make_conn([
        # Logged-in user row
        {"user_id": admin_id, "user_type": "admin", "launch_id_fk": "org-1"},
        # Target owner row (also admin)
        {"user_type": "admin", "launch_id_fk": "org-1", "email": "other@example.com"},
        # special_access row exists with full access
        {"access_level": "full"},
    ])
    app = _build_app()
    resp = _do_request(app, conn, actor_id=admin_id, target_id=target_admin_id)
    assert resp.status_code == 200


@pytest.mark.security
@pytest.mark.authz
def test_normal_user_with_active_role_and_permission_allowed():
    """Normal user with an active role that includes the required permission must get 200."""
    user_id = "user-100"
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
    resp = _do_request(
        app, conn, actor_id=user_id, target_id=user_id,
        patches=[patch("utils.permission_required.resolve_permissions", return_value=[required_perm])],
    )
    assert resp.status_code == 200


@pytest.mark.security
@pytest.mark.authz
def test_normal_user_cross_user_without_share_forbidden():
    """Normal user accessing ANOTHER user's workspace without a share must get 403."""
    attacker_id = "user-100"
    victim_id = "user-999"
    conn = _make_conn([
        # Logged-in user row (non-admin)
        {"user_id": attacker_id, "user_type": "user", "launch_id_fk": "org-1"},
    ])
    app = _build_app()
    # No active share → cross-user access is denied before any permission check.
    resp = _do_request(
        app, conn, actor_id=attacker_id, target_id=victim_id,
        patches=[patch("utils.permission_required._actor_has_share_with", return_value=False)],
    )
    assert resp.status_code == 403
    data = resp.get_json()
    assert "error" in data


@pytest.mark.security
@pytest.mark.authz
def test_normal_user_without_permission_denied():
    """Normal user whose role doesn't include the required permission must get 403."""
    user_id = "user-101"
    perms_json = json.dumps({
        "role": {"permissions": ["other.permission"]},
        "status": "active",
    })
    conn = _make_conn([
        {"user_id": user_id, "user_type": "user", "launch_id_fk": "org-1"},
        {"permissions": perms_json},
    ])
    app = _build_app()
    resp = _do_request(
        app, conn, actor_id=user_id, target_id=user_id,
        patches=[patch("utils.permission_required.resolve_permissions", return_value=["other.permission"])],
    )
    assert resp.status_code == 403
    data = resp.get_json()
    assert "error" in data


@pytest.mark.security
@pytest.mark.authz
def test_normal_user_inactive_role_denied():
    """Normal user with a role that has status != 'active' must get 403."""
    user_id = "user-102"
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
    resp = _do_request(
        app, conn, actor_id=user_id, target_id=user_id,
        patches=[patch("utils.permission_required.resolve_permissions", return_value=[required_perm])],
    )
    assert resp.status_code == 403


@pytest.mark.security
@pytest.mark.authz
def test_no_session_returns_401():
    """Request with no authenticated session must get 401 — even if it supplies a user_id.

    This is the authentication gate: a caller that has not completed login
    (e.g. password ok but 2FA pending) has no session user_id, so passing a
    user_id in the URL/body must NOT grant access.
    """
    conn = _make_conn([])
    app = _build_app()
    resp = _do_request(app, conn, actor_id=None, target_id="some-owner-id")
    assert resp.status_code == 401
    data = resp.get_json()
    assert "error" in data


@pytest.mark.security
@pytest.mark.authz
def test_totp_pending_session_blocked():
    """A password-authenticated session with 2FA still pending must be blocked
    from protected routes. Returned as 403 (not 401) so the frontend keeps the
    user on the 2FA page instead of hard-redirecting to login."""
    admin_id = "admin-001"
    conn = _make_conn([
        {"user_id": admin_id, "user_type": "admin", "launch_id_fk": "org-1"},
    ])
    app = _build_app()
    resp = _do_request(app, conn, actor_id=admin_id, target_id=admin_id, totp_pending=True)
    assert resp.status_code == 403
    data = resp.get_json()
    assert "error" in data
    assert data.get("totp_required") is True


@pytest.mark.security
@pytest.mark.authz
def test_viewer_access_blocks_write_methods():
    """Admin with viewer-level special_access must be blocked from POST/PUT/PATCH/DELETE."""
    admin_id = "admin-001"
    target_admin_id = "admin-002"
    conn = _make_conn([
        {"user_id": admin_id, "user_type": "admin", "launch_id_fk": "org-1"},
        {"user_type": "admin", "launch_id_fk": "org-1", "email": "other@example.com"},
        {"access_level": "viewer"},
    ])
    app = _build_app()
    resp = _do_request(app, conn, actor_id=admin_id, target_id=target_admin_id, method="post")
    assert resp.status_code == 403
    data = resp.get_json()
    assert "viewer" in data.get("error", "").lower() or "modify" in data.get("error", "").lower()


@pytest.mark.security
@pytest.mark.authz
def test_viewer_access_allows_read_methods():
    """Admin with viewer-level special_access must be allowed to GET."""
    admin_id = "admin-001"
    target_admin_id = "admin-002"
    conn = _make_conn([
        {"user_id": admin_id, "user_type": "admin", "launch_id_fk": "org-1"},
        {"user_type": "admin", "launch_id_fk": "org-1", "email": "other@example.com"},
        {"access_level": "viewer"},
    ])
    app = _build_app()
    resp = _do_request(app, conn, actor_id=admin_id, target_id=target_admin_id)
    assert resp.status_code == 200
