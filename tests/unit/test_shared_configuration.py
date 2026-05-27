"""Unit tests for shared_configuration.py — color allocator + permission check helpers.

Mocks pymysql, S3, and the permission_resolver.
"""

import asyncio
import json
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

for _mod in ("pymysql", "pymysql.cursors", "db", "db.rds_db", "db.db_checkers",
             "boto3", "dotenv", "dbutils", "dbutils.pooled_db"):
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

# Force-clear stale stubs so shared_configuration gets a proper s3 module
for _to_pop in ("shared_configuration", "utils.s3_utils"):
    sys.modules.pop(_to_pop, None)

_s3_mod = types.ModuleType("utils.s3_utils")
_s3_mod.S3_BUCKET = "test-bucket"
_s3_mod.s3bucket = MagicMock()
_s3_mod.read_json_from_s3 = MagicMock(return_value={})
_s3_mod.save_json_to_s3 = MagicMock(return_value=True)
_s3_mod.save_app_runbase_S3 = MagicMock(return_value=True)
_s3_mod.getallendpointdetails = MagicMock(return_value=[])
_s3_mod.get_filedata_endp = MagicMock(return_value=None)
sys.modules["utils.s3_utils"] = _s3_mod

sys.modules.setdefault("utils.base_logger",
                      MagicMock(get_logger=MagicMock(return_value=MagicMock())))

# Stub permission_resolver
sys.modules.setdefault("utils.permission_resolver",
                      MagicMock(resolve_permissions=lambda perms: set(perms)))

import shared_configuration as sc  # noqa: E402


# ── COLOR_PALETTE ────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_color_palette_is_list():
    assert isinstance(sc.COLOR_PALETTE, list)
    assert len(sc.COLOR_PALETTE) > 0

@pytest.mark.unit
def test_color_palette_has_six_colors():
    assert len(sc.COLOR_PALETTE) == 6

@pytest.mark.unit
@pytest.mark.parametrize("color", ["black", "blue", "green", "yellow", "pink", "orange"])
def test_color_palette_contains(color):
    assert color in sc.COLOR_PALETTE

@pytest.mark.unit
def test_color_palette_unique():
    assert len(set(sc.COLOR_PALETTE)) == len(sc.COLOR_PALETTE)


# ── get_next_color ───────────────────────────────────────────────────────────

@pytest.mark.unit
def test_get_next_color_empty_returns_first():
    assert sc.get_next_color([]) == sc.COLOR_PALETTE[0]

@pytest.mark.unit
@pytest.mark.parametrize("idx", range(6))
def test_get_next_color_skips_one_taken(idx):
    """If only one palette color is taken, return any other (first available)."""
    taken = [{"colorindication": sc.COLOR_PALETTE[idx]}]
    result = sc.get_next_color(taken)
    assert result != sc.COLOR_PALETTE[idx]
    assert result in sc.COLOR_PALETTE

@pytest.mark.unit
def test_get_next_color_skips_used_in_order():
    taken = [{"colorindication": "black"}, {"colorindication": "blue"}]
    assert sc.get_next_color(taken) == "green"

@pytest.mark.unit
def test_get_next_color_all_used_cycles_by_count():
    """When all palette colors are taken, cycle by len(existing) mod len(palette)."""
    existing = [{"colorindication": c} for c in sc.COLOR_PALETTE]
    # len(existing) = 6 → 6 % 6 = 0 → COLOR_PALETTE[0]
    assert sc.get_next_color(existing) == sc.COLOR_PALETTE[0]

@pytest.mark.unit
def test_get_next_color_handles_entry_without_colorindication():
    """Entries with missing colorindication should not break the allocator."""
    existing = [{"colorindication": "black"}, {}, {"other_field": "x"}]
    result = sc.get_next_color(existing)
    assert result != "black"

@pytest.mark.unit
@pytest.mark.parametrize("size", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
def test_get_next_color_returns_palette_member(size):
    """No matter the size of existing, the result is a known palette color."""
    existing = [{"colorindication": sc.COLOR_PALETTE[i % len(sc.COLOR_PALETTE)]}
                for i in range(size)]
    assert sc.get_next_color(existing) in sc.COLOR_PALETTE


# ── check_user_has_permission ────────────────────────────────────────────────

@pytest.mark.unit
def test_check_user_has_permission_active_with_perm():
    perms = json.dumps({
        "status": "active",
        "role": {"permissions": ["x.read", "y.write"]},
    })
    assert sc.check_user_has_permission(perms, "x.read") is True

@pytest.mark.unit
def test_check_user_has_permission_no_perm():
    perms = json.dumps({
        "status": "active",
        "role": {"permissions": ["a.b"]},
    })
    assert sc.check_user_has_permission(perms, "x.read") is False

@pytest.mark.unit
def test_check_user_has_permission_inactive_status():
    perms = json.dumps({
        "status": "revoked",
        "role": {"permissions": ["x.read"]},
    })
    assert sc.check_user_has_permission(perms, "x.read") is False

@pytest.mark.unit
def test_check_user_has_permission_missing_role():
    perms = json.dumps({"status": "active"})
    assert sc.check_user_has_permission(perms, "x.read") is False

@pytest.mark.unit
def test_check_user_has_permission_invalid_json():
    assert sc.check_user_has_permission("not-json", "x.read") is False

@pytest.mark.unit
def test_check_user_has_permission_dict_input():
    perms = {"status": "active", "role": {"permissions": ["x.read"]}}
    assert sc.check_user_has_permission(perms, "x.read") is True

@pytest.mark.unit
def test_check_user_has_permission_none_input():
    assert sc.check_user_has_permission(None, "x.read") is False

@pytest.mark.unit
@pytest.mark.parametrize("status", ["active", "ACTIVE", "Active"])
def test_check_user_has_permission_status_case_sensitive(status):
    """Implementation does == 'active' (lowercase only)."""
    perms = {"status": status, "role": {"permissions": ["x"]}}
    result = sc.check_user_has_permission(perms, "x")
    assert result is (status == "active")


# ── RESOURCE_CONFIG ──────────────────────────────────────────────────────────

@pytest.mark.unit
def test_resource_config_is_dict():
    assert isinstance(sc.RESOURCE_CONFIG, dict)

@pytest.mark.unit
def test_resource_config_has_tracker():
    assert "tracker" in sc.RESOURCE_CONFIG

@pytest.mark.unit
def test_resource_config_has_trust_center():
    assert "trust_center" in sc.RESOURCE_CONFIG


# ── _resource_config ─────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("resource_type", list(sc.RESOURCE_CONFIG.keys()))
def test_resource_config_lookup_known(resource_type):
    cfg = sc._resource_config(resource_type)
    assert cfg is not None
    assert isinstance(cfg, dict)

@pytest.mark.unit
@pytest.mark.parametrize("unknown", ["", "no-such-type", "TRACKER", "Unknown"])
def test_resource_config_unknown_raises(unknown):
    with pytest.raises(Exception):  # ValueError or KeyError depending on impl
        sc._resource_config(unknown)


# ── save_admin_shared_config / get_admin_shared_config ──────────────────────

@pytest.mark.unit
def test_save_admin_shared_config_uses_s3(monkeypatch):
    captured = {}
    def fake_save(data, s3_key):
        captured["data"] = data
        captured["key"] = s3_key
        return True
    monkeypatch.setattr(sc, "save_json_to_s3", fake_save)
    sc.save_admin_shared_config("admin-1", {"v": 1})
    assert captured["data"] == {"v": 1}
    assert "admin-1" in captured["key"]


# ── save_json_to_s3 ──────────────────────────────────────────────────────────

@pytest.mark.unit
def test_save_json_to_s3_success(monkeypatch):
    mock_s3_obj = MagicMock()
    monkeypatch.setattr(sc, "s3bucket", lambda: mock_s3_obj)
    monkeypatch.setattr(sc, "S3_BUCKET", "test-bucket")
    result = sc.save_json_to_s3({"a": 1}, "some/key.json")
    assert result is True
    mock_s3_obj.put_object.assert_called_once()

@pytest.mark.unit
def test_save_json_to_s3_exception_returns_false(monkeypatch):
    def _boom(): raise RuntimeError("S3 down")
    monkeypatch.setattr(sc, "s3bucket", _boom)
    result = sc.save_json_to_s3({"a": 1}, "some/key.json")
    assert result is False

@pytest.mark.unit
def test_save_json_to_s3_key_used(monkeypatch):
    captured = {}
    mock_s3_obj = MagicMock()
    mock_s3_obj.put_object = lambda **kwargs: captured.update(kwargs)
    monkeypatch.setattr(sc, "s3bucket", lambda: mock_s3_obj)
    monkeypatch.setattr(sc, "S3_BUCKET", "bucket-x")
    sc.save_json_to_s3({"x": 2}, "path/to/file.json")
    assert captured["Key"] == "path/to/file.json"
    assert captured["Bucket"] == "bucket-x"


# ── get_admin_shared_config ──────────────────────────────────────────────────

@pytest.mark.unit
def test_get_admin_shared_config_returns_data(monkeypatch):
    monkeypatch.setattr(sc, "read_json_from_s3", lambda key: {"users": {"u1": {}}})
    result = sc.get_admin_shared_config("admin-1")
    assert result == {"users": {"u1": {}}}

@pytest.mark.unit
def test_get_admin_shared_config_empty_returns_default(monkeypatch):
    monkeypatch.setattr(sc, "read_json_from_s3", lambda key: None)
    result = sc.get_admin_shared_config("admin-1")
    assert result == {"users": {}, "reports": {}}

@pytest.mark.unit
def test_get_admin_shared_config_key_contains_admin_id(monkeypatch):
    captured = {}
    def fake_read(key):
        captured["key"] = key
        return None
    monkeypatch.setattr(sc, "read_json_from_s3", fake_read)
    sc.get_admin_shared_config("admin-xyz")
    assert "admin-xyz" in captured["key"]


# ── get_user_shared_reports ──────────────────────────────────────────────────

@pytest.mark.unit
def test_get_user_shared_reports_returns_data(monkeypatch):
    monkeypatch.setattr(sc, "read_json_from_s3", lambda key: {"r1": True})
    assert sc.get_user_shared_reports("u-1") == {"r1": True}

@pytest.mark.unit
def test_get_user_shared_reports_empty_returns_empty_dict(monkeypatch):
    monkeypatch.setattr(sc, "read_json_from_s3", lambda key: None)
    assert sc.get_user_shared_reports("u-1") == {}

@pytest.mark.unit
def test_save_user_shared_reports_delegates(monkeypatch):
    captured = {}
    def fake_save(data, key):
        captured["data"] = data
        captured["key"] = key
        return True
    monkeypatch.setattr(sc, "save_json_to_s3", fake_save)
    sc.save_user_shared_reports("u-2", {"r": 1})
    assert captured["data"] == {"r": 1}
    assert "u-2" in captured["key"]


# ── get_role_users_from_db ───────────────────────────────────────────────────

def _make_conn(rows):
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchall.return_value = rows
    conn.cursor.return_value = cur
    return conn, cur

@pytest.mark.unit
def test_get_role_users_empty():
    conn, _ = _make_conn([])
    result = sc.get_role_users_from_db(conn, "a1", "role-1")
    assert result == []

@pytest.mark.unit
def test_get_role_users_returns_rows():
    rows = [{"user_id": "u1", "email": "u@x.com", "permissions": "{}"}]
    conn, _ = _make_conn(rows)
    result = sc.get_role_users_from_db(conn, "a1", "role-1")
    assert result == rows

@pytest.mark.unit
def test_get_role_users_exception_returns_empty():
    conn = MagicMock()
    conn.cursor.side_effect = RuntimeError("DB boom")
    result = sc.get_role_users_from_db(conn, "a1", "role-1")
    assert result == []


# ── check_role_has_permission ────────────────────────────────────────────────

def _make_conn_with_fetchone(row):
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = row
    conn.cursor.return_value = cur
    return conn

@pytest.mark.unit
def test_check_role_has_permission_no_row():
    conn = _make_conn_with_fetchone(None)
    assert sc.check_role_has_permission(conn, "a1", "role-1", "x.read") is False

@pytest.mark.unit
def test_check_role_has_permission_empty_roles_creation():
    conn = _make_conn_with_fetchone({"roles_creation": None})
    assert sc.check_role_has_permission(conn, "a1", "role-1", "x.read") is False

@pytest.mark.unit
def test_check_role_has_permission_role_not_found():
    roles = json.dumps([{"id": "other-role", "permissions": ["x.read"]}])
    conn = _make_conn_with_fetchone({"roles_creation": roles})
    assert sc.check_role_has_permission(conn, "a1", "role-1", "x.read") is False

@pytest.mark.unit
def test_check_role_has_permission_perm_present():
    roles = json.dumps([{"id": "role-1", "permissions": ["x.read", "y.write"]}])
    conn = _make_conn_with_fetchone({"roles_creation": roles})
    assert sc.check_role_has_permission(conn, "a1", "role-1", "x.read") is True

@pytest.mark.unit
def test_check_role_has_permission_perm_absent():
    roles = json.dumps([{"id": "role-1", "permissions": ["y.write"]}])
    conn = _make_conn_with_fetchone({"roles_creation": roles})
    assert sc.check_role_has_permission(conn, "a1", "role-1", "x.read") is False

@pytest.mark.unit
def test_check_role_has_permission_exception():
    conn = MagicMock()
    conn.cursor.side_effect = RuntimeError("DB")
    assert sc.check_role_has_permission(conn, "a1", "role-1", "x.read") is False


# ── get_round_robin_user ──────────────────────────────────────────────────────

@pytest.mark.unit
def test_get_round_robin_user_no_users(monkeypatch):
    monkeypatch.setattr(sc, "get_role_users_from_db", lambda *a: [])
    conn, _ = _make_conn([])
    user, err = sc.get_round_robin_user("a1", "role-1", "radar", conn, "kb.doc.view")
    assert user is None
    assert "No users" in err

@pytest.mark.unit
def test_get_round_robin_user_no_eligible(monkeypatch):
    perms = json.dumps({"status": "active", "role": {"permissions": ["other.perm"]}})
    monkeypatch.setattr(sc, "get_role_users_from_db",
                        lambda *a: [{"user_id": "u1", "permissions": perms}])
    monkeypatch.setattr(sc, "get_admin_shared_config", lambda *a: {"users": {}})
    conn, _ = _make_conn([])
    user, err = sc.get_round_robin_user("a1", "role-1", "radar", conn, "kb.doc.view")
    assert user is None
    assert "No users in this role" in err

@pytest.mark.unit
def test_get_round_robin_user_picks_least_loaded(monkeypatch):
    perms = json.dumps({"status": "active", "role": {"permissions": ["kb.doc.view"]}})
    users = [
        {"user_id": "u1", "permissions": perms},
        {"user_id": "u2", "permissions": perms},
    ]
    monkeypatch.setattr(sc, "get_role_users_from_db", lambda *a: users)
    monkeypatch.setattr(sc, "get_admin_shared_config",
                        lambda *a: {"users": {"u1": {"radar_count": 5}, "u2": {"radar_count": 1}}})
    conn, _ = _make_conn([])
    user, err = sc.get_round_robin_user("a1", "role-1", "radar", conn, "kb.doc.view")
    assert err is None
    assert user["user_id"] == "u2"


# ── resource config helpers ──────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("resource_type", list(sc.RESOURCE_CONFIG.keys()))
def test_get_admin_resource_config_default(resource_type, monkeypatch):
    monkeypatch.setattr(sc, "read_json_from_s3", lambda key: None)
    result = sc.get_admin_resource_config("admin-1", resource_type)
    assert result == {"users": {}, "resources": {}}

@pytest.mark.unit
@pytest.mark.parametrize("resource_type", list(sc.RESOURCE_CONFIG.keys()))
def test_get_admin_resource_config_with_data(resource_type, monkeypatch):
    monkeypatch.setattr(sc, "read_json_from_s3", lambda key: {"users": {"u1": {}}})
    result = sc.get_admin_resource_config("admin-1", resource_type)
    assert result == {"users": {"u1": {}}}

@pytest.mark.unit
@pytest.mark.parametrize("resource_type", list(sc.RESOURCE_CONFIG.keys()))
def test_save_admin_resource_config(resource_type, monkeypatch):
    captured = {}
    monkeypatch.setattr(sc, "save_json_to_s3", lambda data, key: captured.update({"data": data, "key": key}) or True)
    sc.save_admin_resource_config("admin-1", resource_type, {"users": {}})
    assert "admin-1" in captured["key"]

@pytest.mark.unit
@pytest.mark.parametrize("resource_type", list(sc.RESOURCE_CONFIG.keys()))
def test_get_user_shared_resources_empty(resource_type, monkeypatch):
    monkeypatch.setattr(sc, "read_json_from_s3", lambda key: None)
    assert sc.get_user_shared_resources("u-1", resource_type) == {}

@pytest.mark.unit
@pytest.mark.parametrize("resource_type", list(sc.RESOURCE_CONFIG.keys()))
def test_get_user_shared_resources_with_data(resource_type, monkeypatch):
    monkeypatch.setattr(sc, "read_json_from_s3", lambda key: {"r1": True})
    assert sc.get_user_shared_resources("u-1", resource_type) == {"r1": True}

@pytest.mark.unit
@pytest.mark.parametrize("resource_type", list(sc.RESOURCE_CONFIG.keys()))
def test_save_user_shared_resources(resource_type, monkeypatch):
    captured = {}
    monkeypatch.setattr(sc, "save_json_to_s3", lambda data, key: captured.update({"data": data}) or True)
    sc.save_user_shared_resources("u-1", resource_type, {"x": 1})
    assert captured["data"] == {"x": 1}


# ── get_round_robin_user_for_resource ─────────────────────────────────────────

@pytest.mark.unit
def test_get_round_robin_user_for_resource_no_users(monkeypatch):
    monkeypatch.setattr(sc, "get_role_users_from_db", lambda *a: [])
    conn, _ = _make_conn([])
    user, err = sc.get_round_robin_user_for_resource("a1", "role-1", "tracker", conn)
    assert user is None
    assert "No users" in err

@pytest.mark.unit
def test_get_round_robin_user_for_resource_picks_minimum(monkeypatch):
    perms = json.dumps({"status": "active", "role": {"permissions": ["trackers.table.view"]}})
    users = [
        {"user_id": "u1", "permissions": perms},
        {"user_id": "u2", "permissions": perms},
    ]
    monkeypatch.setattr(sc, "get_role_users_from_db", lambda *a: users)
    monkeypatch.setattr(sc, "get_admin_resource_config",
                        lambda *a, **kw: {"users": {"u1": {"count": 10}, "u2": {"count": 2}}})
    conn, _ = _make_conn([])
    user, err = sc.get_round_robin_user_for_resource("a1", "role-1", "tracker", conn)
    assert err is None
    assert user["user_id"] == "u2"

@pytest.mark.unit
def test_get_round_robin_user_for_resource_custom_permission(monkeypatch):
    perms = json.dumps({"status": "active", "role": {"permissions": ["custom.perm"]}})
    users = [{"user_id": "u1", "permissions": perms}]
    monkeypatch.setattr(sc, "get_role_users_from_db", lambda *a: users)
    monkeypatch.setattr(sc, "get_admin_resource_config", lambda *a, **kw: {"users": {}})
    conn, _ = _make_conn([])
    user, err = sc.get_round_robin_user_for_resource(
        "a1", "role-1", "tracker", conn, required_permission="custom.perm"
    )
    assert err is None
    assert user["user_id"] == "u1"


# ── get_user_resource_access ─────────────────────────────────────────────────

@pytest.mark.unit
def test_get_user_resource_access_no_user_id(monkeypatch):
    result = sc.get_user_resource_access("tracker", "admin-1", "r1", None)
    assert result == {"granted": False, "level": None}

@pytest.mark.unit
def test_get_user_resource_access_owner(monkeypatch):
    result = sc.get_user_resource_access("tracker", "admin-1", "r1", "admin-1")
    assert result == {"granted": True, "level": "edit"}

@pytest.mark.unit
def test_get_user_resource_access_shared_user(monkeypatch):
    sharing = [{"id": "u-2", "access": True, "level": "view"}]
    monkeypatch.setattr(sc, "core_list_resource_shares", lambda *a: (sharing, None))
    result = sc.get_user_resource_access("tracker", "admin-1", "r1", "u-2")
    assert result == {"granted": True, "level": "view"}

@pytest.mark.unit
def test_get_user_resource_access_not_shared(monkeypatch):
    monkeypatch.setattr(sc, "core_list_resource_shares", lambda *a: ([], None))
    result = sc.get_user_resource_access("tracker", "admin-1", "r1", "u-3")
    assert result == {"granted": False, "level": None}

@pytest.mark.unit
def test_get_user_resource_access_entry_no_access(monkeypatch):
    sharing = [{"id": "u-4", "access": False, "level": None}]
    monkeypatch.setattr(sc, "core_list_resource_shares", lambda *a: (sharing, None))
    result = sc.get_user_resource_access("tracker", "admin-1", "r1", "u-4")
    assert result == {"granted": False, "level": None}


# ── core_assign_resource ──────────────────────────────────────────────────────

def _user_conn(permissions_json):
    """Conn that returns a single user row from fetchone()."""
    conn, cur = _make_conn([])
    cur.fetchone.return_value = {"permissions": permissions_json}
    conn.cursor.return_value = cur
    return conn

@pytest.mark.unit
def test_core_assign_resource_user_not_found(monkeypatch):
    conn, cur = _make_conn([])
    cur.fetchone.return_value = None
    conn.cursor.return_value = cur
    result, err = sc.core_assign_resource(
        "tracker", "admin-1", "a@x.com", "u-1", "u@x.com", "r1", "R1", conn
    )
    assert result is None
    assert "not found" in err.lower()

@pytest.mark.unit
def test_core_assign_resource_no_permission(monkeypatch):
    perms = json.dumps({"status": "active", "role": {"permissions": ["other"]}})
    conn = _user_conn(perms)
    result, err = sc.core_assign_resource(
        "tracker", "admin-1", "a@x.com", "u-1", "u@x.com", "r1", "R1", conn
    )
    assert result is None
    assert "permission" in err.lower()

@pytest.mark.unit
def test_core_assign_resource_success_new_user(monkeypatch):
    perms = json.dumps({"status": "active", "role": {"permissions": ["trackers.table.view"]}})
    conn = _user_conn(perms)
    monkeypatch.setattr(sc, "get_admin_resource_config", lambda *a: {"users": {}, "resources": {}})
    saved = {}
    monkeypatch.setattr(sc, "save_admin_resource_config", lambda a, b, cfg: saved.update({"cfg": cfg}))
    monkeypatch.setattr(sc, "get_user_shared_resources", lambda *a: {})
    monkeypatch.setattr(sc, "save_user_shared_resources", lambda *a: None)
    sharing, err = sc.core_assign_resource(
        "tracker", "admin-1", "a@x.com", "u-1", "u@x.com", "r1", "R1", conn
    )
    assert err is None
    assert any(e["id"] == "u-1" for e in sharing)

@pytest.mark.unit
def test_core_assign_resource_existing_user_reactivated(monkeypatch):
    perms = json.dumps({"status": "active", "role": {"permissions": ["trackers.table.view"]}})
    conn = _user_conn(perms)
    existing_config = {
        "users": {"u-1": {"email": "u@x.com", "count": 1, "resources": [{"id": "r1", "type": "tracker"}]}},
        "resources": {
            "r1": {
                "sharing_access": [
                    {"id": "admin-1", "email": "a@x.com", "colorindication": "black", "access": True},
                    {"id": "u-1", "email": "u@x.com", "colorindication": "blue", "access": False},
                ],
                "name": "R1",
            }
        }
    }
    monkeypatch.setattr(sc, "get_admin_resource_config", lambda *a: existing_config)
    monkeypatch.setattr(sc, "save_admin_resource_config", lambda *a: None)
    monkeypatch.setattr(sc, "get_user_shared_resources", lambda *a: {})
    monkeypatch.setattr(sc, "save_user_shared_resources", lambda *a: None)
    sharing, err = sc.core_assign_resource(
        "tracker", "admin-1", "a@x.com", "u-1", "u@x.com", "r1", "R1", conn
    )
    assert err is None
    u1 = next(e for e in sharing if e["id"] == "u-1")
    assert u1["access"] is True

@pytest.mark.unit
def test_core_assign_resource_trust_center_requires_level(monkeypatch):
    perms = json.dumps({"status": "active", "role": {"permissions": ["trustcenter.view"]}})
    conn = _user_conn(perms)
    result, err = sc.core_assign_resource(
        "trust_center", "admin-1", "a@x.com", "u-1", "u@x.com", "r1", "R1", conn, level="bad"
    )
    assert result is None
    assert "level" in err.lower()

@pytest.mark.unit
def test_core_assign_resource_trust_center_valid_level(monkeypatch):
    perms = json.dumps({"status": "active", "role": {"permissions": ["trustcenter.view"]}})
    conn = _user_conn(perms)
    monkeypatch.setattr(sc, "get_admin_resource_config", lambda *a: {"users": {}, "resources": {}})
    monkeypatch.setattr(sc, "save_admin_resource_config", lambda *a: None)
    monkeypatch.setattr(sc, "get_user_shared_resources", lambda *a: {})
    monkeypatch.setattr(sc, "save_user_shared_resources", lambda *a: None)
    sharing, err = sc.core_assign_resource(
        "trust_center", "admin-1", "a@x.com", "u-1", "u@x.com", "r1", "R1", conn, level="view"
    )
    assert err is None
    u1 = next(e for e in sharing if e["id"] == "u-1")
    assert u1.get("level") == "view"

@pytest.mark.unit
def test_core_assign_resource_exception_returns_error(monkeypatch):
    monkeypatch.setattr(sc, "_resource_config", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    result, err = sc.core_assign_resource(
        "tracker", "admin-1", "a@x.com", "u-1", "u@x.com", "r1", "R1", MagicMock()
    )
    assert result is None
    assert err is not None


# ── core_revoke_resource ──────────────────────────────────────────────────────

@pytest.mark.unit
def test_core_revoke_resource_revokes_and_removes(monkeypatch):
    config = {
        "resources": {
            "r1": {
                "sharing_access": [
                    {"id": "u-1", "access": True},
                    {"id": "admin-1", "access": True},
                ]
            }
        },
        "users": {"u-1": {"count": 1, "resources": [{"id": "r1"}]}},
    }
    monkeypatch.setattr(sc, "get_admin_resource_config", lambda *a: config)
    saved = {}
    monkeypatch.setattr(sc, "save_admin_resource_config", lambda a, b, cfg: saved.update({"cfg": cfg}))
    monkeypatch.setattr(sc, "get_user_shared_resources", lambda *a: {"r1": {}})
    user_saved = {}
    monkeypatch.setattr(sc, "save_user_shared_resources", lambda u, t, idx: user_saved.update({"idx": idx}))
    sharing, err = sc.core_revoke_resource("tracker", "admin-1", "u-1", "r1")
    assert err is None
    u1 = next(e for e in sharing if e["id"] == "u-1")
    assert u1["access"] is False
    assert "r1" not in user_saved.get("idx", {"r1": True})
    assert saved["cfg"]["users"]["u-1"]["count"] == 0

@pytest.mark.unit
def test_core_revoke_resource_unknown_resource_id(monkeypatch):
    config = {"resources": {}, "users": {}}
    monkeypatch.setattr(sc, "get_admin_resource_config", lambda *a: config)
    monkeypatch.setattr(sc, "save_admin_resource_config", lambda *a: None)
    monkeypatch.setattr(sc, "get_user_shared_resources", lambda *a: {})
    monkeypatch.setattr(sc, "save_user_shared_resources", lambda *a: None)
    sharing, err = sc.core_revoke_resource("tracker", "admin-1", "u-1", "nonexistent")
    assert err is None
    assert sharing == []

@pytest.mark.unit
def test_core_revoke_resource_exception(monkeypatch):
    monkeypatch.setattr(sc, "_resource_config", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    result, err = sc.core_revoke_resource("tracker", "admin-1", "u-1", "r1")
    assert result is None
    assert err is not None


# ── core_list_resource_shares ─────────────────────────────────────────────────

@pytest.mark.unit
def test_core_list_resource_shares_returns_list(monkeypatch):
    sharing = [{"id": "u-1", "access": True}]
    config = {"resources": {"r1": {"sharing_access": sharing}}}
    monkeypatch.setattr(sc, "get_admin_resource_config", lambda *a: config)
    result, err = sc.core_list_resource_shares("tracker", "admin-1", "r1")
    assert err is None
    assert result == sharing

@pytest.mark.unit
def test_core_list_resource_shares_missing_resource(monkeypatch):
    monkeypatch.setattr(sc, "get_admin_resource_config", lambda *a: {"resources": {}})
    result, err = sc.core_list_resource_shares("tracker", "admin-1", "r-unknown")
    assert err is None
    assert result == []

@pytest.mark.unit
def test_core_list_resource_shares_exception(monkeypatch):
    monkeypatch.setattr(sc, "_resource_config", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    result, err = sc.core_list_resource_shares("tracker", "admin-1", "r1")
    assert result == []
    assert err is not None


# ── core_assign_resource branch gaps ─────────────────────────────────────────

@pytest.mark.unit
def test_core_assign_resource_resource_name_not_overwritten(monkeypatch):
    """Branch where resource entry already has a name — should not overwrite."""
    perms = json.dumps({"status": "active", "role": {"permissions": ["trackers.table.view"]}})
    conn = _user_conn(perms)
    existing = {
        "resources": {"r1": {"sharing_access": [], "name": "ExistingName"}},
        "users": {},
    }
    monkeypatch.setattr(sc, "get_admin_resource_config", lambda *a: existing)
    monkeypatch.setattr(sc, "save_admin_resource_config", lambda *a: None)
    monkeypatch.setattr(sc, "get_user_shared_resources", lambda *a: {})
    monkeypatch.setattr(sc, "save_user_shared_resources", lambda *a: None)
    sharing, err = sc.core_assign_resource(
        "tracker", "admin-1", "a@x.com", "u-1", "u@x.com", "r1", None, conn
    )
    assert err is None
    assert existing["resources"]["r1"]["name"] == "ExistingName"

@pytest.mark.unit
def test_core_assign_resource_trust_center_update_existing_level(monkeypatch):
    """Existing user in trust_center gets level updated."""
    perms = json.dumps({"status": "active", "role": {"permissions": ["trustcenter.view"]}})
    conn = _user_conn(perms)
    existing = {
        "resources": {
            "r1": {
                "sharing_access": [
                    {"id": "admin-1", "access": True, "level": "edit"},
                    {"id": "u-1", "email": "u@x.com", "access": True, "level": "view"},
                ],
                "name": "R1",
            }
        },
        "users": {
            "u-1": {"count": 1, "resources": [{"id": "r1", "level": "view"}]},
        },
    }
    monkeypatch.setattr(sc, "get_admin_resource_config", lambda *a: existing)
    monkeypatch.setattr(sc, "save_admin_resource_config", lambda *a: None)
    monkeypatch.setattr(sc, "get_user_shared_resources", lambda *a: {"r1": {"level": "view"}})
    monkeypatch.setattr(sc, "save_user_shared_resources", lambda *a: None)
    sharing, err = sc.core_assign_resource(
        "trust_center", "admin-1", "a@x.com", "u-1", "u@x.com", "r1", "R1", conn, level="edit"
    )
    assert err is None
    u1 = next(e for e in sharing if e["id"] == "u-1")
    assert u1["level"] == "edit"

@pytest.mark.unit
def test_get_round_robin_user_for_resource_no_eligible(monkeypatch):
    """Users exist but none have the required permission."""
    perms = json.dumps({"status": "active", "role": {"permissions": ["other.perm"]}})
    users = [{"user_id": "u1", "permissions": perms}]
    monkeypatch.setattr(sc, "get_role_users_from_db", lambda *a: users)
    conn, _ = _make_conn([])
    user, err = sc.get_round_robin_user_for_resource("a1", "role-1", "tracker", conn)
    assert user is None
    assert "No users in this role" in err

@pytest.mark.unit
def test_core_assign_resource_sets_name_when_entry_has_no_name(monkeypatch):
    """Line 558 branch: resource entry exists but has no name → name gets written."""
    perms = json.dumps({"status": "active", "role": {"permissions": ["trackers.table.view"]}})
    conn = _user_conn(perms)
    existing = {
        "resources": {"r1": {"sharing_access": []}},  # no "name" key
        "users": {},
    }
    monkeypatch.setattr(sc, "get_admin_resource_config", lambda *a: existing)
    monkeypatch.setattr(sc, "save_admin_resource_config", lambda *a: None)
    monkeypatch.setattr(sc, "get_user_shared_resources", lambda *a: {})
    monkeypatch.setattr(sc, "save_user_shared_resources", lambda *a: None)
    sc.core_assign_resource(
        "tracker", "admin-1", "a@x.com", "u-1", "u@x.com", "r1", "NewName", conn
    )
    assert existing["resources"]["r1"]["name"] == "NewName"

@pytest.mark.unit
def test_core_assign_resource_single_assignee_revokes_others(monkeypatch):
    """Lines 575-577: single_assignee=True → other users' access set to False."""
    perms = json.dumps({"status": "active", "role": {"permissions": ["req.perm"]}})
    conn = _user_conn(perms)
    single_cfg = {
        "admin_config_path": "a/b.json",
        "user_index_path": "u/b.json",
        "required_permission": "req.perm",
        "single_assignee": True,
        "supports_levels": False,
    }
    existing = {
        "resources": {
            "r1": {
                "sharing_access": [
                    {"id": "admin-1", "access": True},
                    {"id": "old-user", "access": True},  # should be revoked
                ],
                "name": "R1",
            }
        },
        "users": {},
    }
    monkeypatch.setattr(sc, "_resource_config", lambda *a: single_cfg)
    monkeypatch.setattr(sc, "get_admin_resource_config", lambda *a: existing)
    monkeypatch.setattr(sc, "save_admin_resource_config", lambda *a: None)
    monkeypatch.setattr(sc, "get_user_shared_resources", lambda *a: {})
    monkeypatch.setattr(sc, "save_user_shared_resources", lambda *a: None)
    sharing, err = sc.core_assign_resource(
        "tracker", "admin-1", "a@x.com", "new-user", "n@x.com", "r1", "R1", conn
    )
    assert err is None
    old = next(e for e in sharing if e["id"] == "old-user")
    assert old["access"] is False


# ── core_assign_report (async) ────────────────────────────────────────────────

def _run(coro):
    return asyncio.run(coro)

def _async_conn(perms_json):
    conn, cur = _make_conn([])
    cur.fetchone.return_value = {"permissions": perms_json}
    conn.cursor.return_value = cur
    return conn

@pytest.mark.unit
def test_core_assign_report_user_not_found(monkeypatch):
    conn, cur = _make_conn([])
    cur.fetchone.return_value = None
    conn.cursor.return_value = cur
    result, err = _run(sc.core_assign_report(
        "a1", "a@x.com", "u1", "u@x.com", "r1", "radar", "R1", conn, MagicMock()
    ))
    assert result is None
    assert "not found" in err.lower()

@pytest.mark.unit
def test_core_assign_report_no_permission(monkeypatch):
    perms = json.dumps({"status": "active", "role": {"permissions": ["other"]}})
    conn = _async_conn(perms)
    result, err = _run(sc.core_assign_report(
        "a1", "a@x.com", "u1", "u@x.com", "r1", "radar", "R1", conn, MagicMock()
    ))
    assert result is None
    assert "permission" in err.lower()

@pytest.mark.unit
def test_core_assign_report_success_new_user(monkeypatch):
    perms = json.dumps({"status": "active", "role": {"permissions": ["kb.doc.view"]}})
    conn = _async_conn(perms)
    monkeypatch.setattr(sc, "get_admin_shared_config", lambda *a: {"users": {}, "reports": {}})
    saved = {}
    monkeypatch.setattr(sc, "save_admin_shared_config", lambda a, cfg: saved.update({"cfg": cfg}))
    monkeypatch.setattr(sc, "get_user_shared_reports", lambda *a: {})
    monkeypatch.setattr(sc, "save_user_shared_reports", lambda *a: None)
    dbserver = AsyncMock()
    dbserver.radar_get_by_id = AsyncMock(return_value=None)
    sharing, err = _run(sc.core_assign_report(
        "a1", "a@x.com", "u1", "u@x.com", "r1", "radar", "R1", conn, dbserver
    ))
    assert err is None
    assert any(e["id"] == "u1" for e in sharing)

@pytest.mark.unit
def test_core_assign_report_runbook_type(monkeypatch):
    perms = json.dumps({"status": "active", "role": {"permissions": ["compliance.runbook.read"]}})
    conn = _async_conn(perms)
    monkeypatch.setattr(sc, "get_admin_shared_config", lambda *a: {"users": {}, "reports": {}})
    monkeypatch.setattr(sc, "save_admin_shared_config", lambda *a: None)
    monkeypatch.setattr(sc, "get_user_shared_reports", lambda *a: {})
    monkeypatch.setattr(sc, "save_user_shared_reports", lambda *a: None)
    dbserver = AsyncMock()
    dbserver.runbook_get_result = AsyncMock(return_value=None)
    sharing, err = _run(sc.core_assign_report(
        "a1", "a@x.com", "u1", "u@x.com", "r1", "runbook", "Runbook", conn, dbserver
    ))
    assert err is None

@pytest.mark.unit
def test_core_assign_report_exception(monkeypatch):
    monkeypatch.setattr(sc, "check_user_has_permission",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    conn, cur = _make_conn([])
    cur.fetchone.return_value = {"permissions": "{}"}
    conn.cursor.return_value = cur
    result, err = _run(sc.core_assign_report(
        "a1", "a@x.com", "u1", "u@x.com", "r1", "radar", "R1", conn, MagicMock()
    ))
    assert result is None
    assert err is not None

@pytest.mark.unit
def test_core_assign_report_with_parent_id(monkeypatch):
    perms = json.dumps({"status": "active", "role": {"permissions": ["compliance.runbook.read"]}})
    conn = _async_conn(perms)
    monkeypatch.setattr(sc, "get_admin_shared_config", lambda *a: {"users": {}, "reports": {}})
    monkeypatch.setattr(sc, "save_admin_shared_config", lambda *a: None)
    monkeypatch.setattr(sc, "get_user_shared_reports", lambda *a: {})
    monkeypatch.setattr(sc, "save_user_shared_reports", lambda *a: None)
    dbserver = AsyncMock()
    dbserver.runbook_get_result = AsyncMock(return_value=None)
    sharing, err = _run(sc.core_assign_report(
        "a1", "a@x.com", "u1", "u@x.com", "r1", "runbook", "Runbook",
        conn, dbserver, parent_id="rb-parent"
    ))
    assert err is None


# ── core_revoke_report (async) ────────────────────────────────────────────────

@pytest.mark.unit
def test_core_revoke_report_success(monkeypatch):
    config = {
        "reports": {
            "r1": {"sharing_access": [{"id": "u1", "access": True}]},
        },
        "users": {"u1": {"radar_count": 1, "reports": [{"id": "r1"}]}},
    }
    monkeypatch.setattr(sc, "get_admin_shared_config", lambda *a: config)
    monkeypatch.setattr(sc, "save_admin_shared_config", lambda *a: None)
    monkeypatch.setattr(sc, "get_user_shared_reports", lambda *a: {"r1": {}})
    monkeypatch.setattr(sc, "save_user_shared_reports", lambda *a: None)
    dbserver = AsyncMock()
    dbserver.radar_get_by_id = AsyncMock(return_value=None)
    sharing, err = _run(sc.core_revoke_report("a1", "u1", "r1", "radar", dbserver))
    assert err is None
    u1 = next(e for e in sharing if e["id"] == "u1")
    assert u1["access"] is False

@pytest.mark.unit
def test_core_revoke_report_report_not_in_config(monkeypatch):
    config = {"reports": {}, "users": {}}
    monkeypatch.setattr(sc, "get_admin_shared_config", lambda *a: config)
    monkeypatch.setattr(sc, "save_admin_shared_config", lambda *a: None)
    monkeypatch.setattr(sc, "get_user_shared_reports", lambda *a: {})
    monkeypatch.setattr(sc, "save_user_shared_reports", lambda *a: None)
    dbserver = AsyncMock()
    sharing, err = _run(sc.core_revoke_report("a1", "u1", "nonexistent", "radar", dbserver))
    assert err is None
    assert sharing == []

@pytest.mark.unit
def test_core_revoke_report_runbook_type(monkeypatch):
    config = {
        "reports": {"rb1": {"sharing_access": [{"id": "u1", "access": True}]}},
        "users": {"u1": {"runbook_count": 2, "reports": [{"id": "rb1"}]}},
    }
    monkeypatch.setattr(sc, "get_admin_shared_config", lambda *a: config)
    monkeypatch.setattr(sc, "save_admin_shared_config", lambda *a: None)
    monkeypatch.setattr(sc, "get_user_shared_reports", lambda *a: {})
    monkeypatch.setattr(sc, "save_user_shared_reports", lambda *a: None)
    dbserver = AsyncMock()
    dbserver.runbook_get_result = AsyncMock(return_value=None)
    sharing, err = _run(sc.core_revoke_report("a1", "u1", "rb1", "runbook", dbserver))
    assert err is None

@pytest.mark.unit
def test_core_revoke_report_lancedb_exception_swallowed(monkeypatch):
    config = {"reports": {"r1": {"sharing_access": [{"id": "u1", "access": True}]}}, "users": {}}
    monkeypatch.setattr(sc, "get_admin_shared_config", lambda *a: config)
    monkeypatch.setattr(sc, "save_admin_shared_config", lambda *a: None)
    monkeypatch.setattr(sc, "get_user_shared_reports", lambda *a: {})
    monkeypatch.setattr(sc, "save_user_shared_reports", lambda *a: None)
    dbserver = AsyncMock()
    dbserver.radar_get_by_id = AsyncMock(side_effect=RuntimeError("LanceDB down"))
    # Should not raise — exception swallowed
    sharing, err = _run(sc.core_revoke_report("a1", "u1", "r1", "radar", dbserver))
    assert err is None

@pytest.mark.unit
def test_core_revoke_report_outer_exception(monkeypatch):
    monkeypatch.setattr(sc, "get_admin_shared_config",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("config gone")))
    result, err = _run(sc.core_revoke_report("a1", "u1", "r1", "radar", AsyncMock()))
    assert result is None
    assert err is not None
