"""
Tests for the generic resource-share core (shared_configuration.py) and the
view/edit access helper used by the trust-center routes.

These cover:

- get_next_color (incl. B5 — palette exhaustion no longer collapses on "orange")
- core_assign_resource for tracker (single-level, multi-assignee)
- core_assign_resource for trust_center (with view/edit levels)
- core_revoke_resource
- get_user_resource_access (owner / granted / revoked / non-shared)
- Multi-assignee preservation (assigning B does not knock out A)

All S3 and RDS calls are mocked — no live AWS access is required.
"""

import json
from unittest.mock import patch, MagicMock

import pytest

import shared_configuration as sc


ADMIN_ID = "admin-001"
ADMIN_EMAIL = "admin@example.com"
USER_B_ID = "user-B"
USER_B_EMAIL = "b@example.com"
USER_C_ID = "user-C"
USER_C_EMAIL = "c@example.com"
TRACKER_ID = "trk_abc123"
TRACKER_NAME = "Risk Tracker"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_conn(user_permissions):
    """Build a mock pymysql connection whose cursor returns the given permissions.

    `user_permissions` is the JSON-encoded value of the users.permissions column.
    """
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = {"permissions": user_permissions}
    return conn, cur


def _perms_with(*permission_strings):
    return json.dumps(
        {
            "status": "active",
            "role": {"id": "r1", "permissions": list(permission_strings)},
        }
    )


# ── get_next_color (B5) ───────────────────────────────────────────────────────


def test_get_next_color_empty_returns_first_palette_color():
    assert sc.get_next_color([]) == sc.COLOR_PALETTE[0]


def test_get_next_color_skips_used_colors():
    existing = [
        {"colorindication": "black"},
        {"colorindication": "blue"},
    ]
    assert sc.get_next_color(existing) == "green"


def test_get_next_color_palette_exhaustion_cycles_b5():
    # All 6 palette colors taken; the 7th and 8th sharers must NOT both fall
    # back to the same colour as a previous entry.
    existing = [{"colorindication": c} for c in sc.COLOR_PALETTE]
    seventh = sc.get_next_color(existing)
    assert seventh in sc.COLOR_PALETTE  # still a valid palette colour
    # Deterministic-cycle fix: index % len(palette) where index = len(existing)
    assert seventh == sc.COLOR_PALETTE[len(existing) % len(sc.COLOR_PALETTE)]

    existing.append({"colorindication": seventh})
    eighth = sc.get_next_color(existing)
    # 8th sharer should also be deterministic and not identical to the 7th —
    # this is the regression: pre-fix both would be "orange".
    assert eighth == sc.COLOR_PALETTE[len(existing) % len(sc.COLOR_PALETTE)]
    assert eighth != seventh


# ── core_assign_resource — tracker (single-level, multi-assignee) ──────────────


def _patch_io():
    """Patch the S3 read/write helpers on shared_configuration for one test."""
    storage = {}

    def fake_read(key):
        return storage.get(key)

    def fake_save(data, key):
        storage[key] = json.loads(json.dumps(data, default=str))
        return True

    return storage, patch.object(sc, "read_json_from_s3", side_effect=fake_read), \
        patch.object(sc, "save_json_to_s3", side_effect=fake_save)


def test_core_assign_resource_tracker_manual_writes_sharing_access():
    storage, p_read, p_save = _patch_io()
    conn, _ = _make_conn(_perms_with("trackers.table.view"))

    with p_read, p_save:
        sharing_access, error = sc.core_assign_resource(
            "tracker",
            ADMIN_ID,
            ADMIN_EMAIL,
            USER_B_ID,
            USER_B_EMAIL,
            TRACKER_ID,
            TRACKER_NAME,
            conn,
        )

    assert error is None
    ids = {e["id"] for e in sharing_access}
    assert ADMIN_ID in ids
    assert USER_B_ID in ids

    admin_cfg = storage[f"{ADMIN_ID}/tracker/sharedconfig.json"]
    assert TRACKER_ID in admin_cfg["resources"]
    user_idx = storage[f"{USER_B_ID}/shared/tracker.json"]
    assert TRACKER_ID in user_idx
    assert user_idx[TRACKER_ID]["mainuser_id"] == ADMIN_ID
    assert user_idx[TRACKER_ID]["type"] == "tracker"


def test_core_assign_resource_rejects_user_without_permission():
    _, p_read, p_save = _patch_io()
    conn, _ = _make_conn(_perms_with("some.other.permission"))

    with p_read, p_save:
        sharing_access, error = sc.core_assign_resource(
            "tracker",
            ADMIN_ID,
            ADMIN_EMAIL,
            USER_B_ID,
            USER_B_EMAIL,
            TRACKER_ID,
            TRACKER_NAME,
            conn,
        )

    assert sharing_access is None
    assert error and "permission" in error.lower()


def test_core_assign_resource_rejects_missing_user():
    _, p_read, p_save = _patch_io()
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = None

    with p_read, p_save:
        sharing_access, error = sc.core_assign_resource(
            "tracker",
            ADMIN_ID,
            ADMIN_EMAIL,
            "nonexistent-user",
            "ghost@example.com",
            TRACKER_ID,
            TRACKER_NAME,
            conn,
        )

    assert sharing_access is None
    assert error == "Target user not found"


def test_core_assign_resource_multi_assignee_preserves_prior_users():
    """Multi-assignee resources: adding B must NOT revoke A's access."""
    storage, p_read, p_save = _patch_io()
    conn, _ = _make_conn(_perms_with("trackers.table.view"))

    with p_read, p_save:
        sc.core_assign_resource(
            "tracker", ADMIN_ID, ADMIN_EMAIL,
            USER_B_ID, USER_B_EMAIL,
            TRACKER_ID, TRACKER_NAME, conn,
        )
        sharing_access, _ = sc.core_assign_resource(
            "tracker", ADMIN_ID, ADMIN_EMAIL,
            USER_C_ID, USER_C_EMAIL,
            TRACKER_ID, TRACKER_NAME, conn,
        )

    # Both B and C should still have access=True after the second assignment.
    by_id = {e["id"]: e for e in sharing_access}
    assert by_id[USER_B_ID]["access"] is True
    assert by_id[USER_C_ID]["access"] is True
    assert by_id[ADMIN_ID]["access"] is True


def test_core_assign_resource_same_user_twice_keeps_count_at_one():
    storage, p_read, p_save = _patch_io()
    conn, _ = _make_conn(_perms_with("trackers.table.view"))

    with p_read, p_save:
        sc.core_assign_resource(
            "tracker", ADMIN_ID, ADMIN_EMAIL,
            USER_B_ID, USER_B_EMAIL,
            TRACKER_ID, TRACKER_NAME, conn,
        )
        sc.core_assign_resource(
            "tracker", ADMIN_ID, ADMIN_EMAIL,
            USER_B_ID, USER_B_EMAIL,
            TRACKER_ID, TRACKER_NAME, conn,
        )

    admin_cfg = storage[f"{ADMIN_ID}/tracker/sharedconfig.json"]
    assert admin_cfg["users"][USER_B_ID]["count"] == 1


# ── core_assign_resource — trust_center with view/edit levels ─────────────────


def test_core_assign_resource_trust_center_view():
    storage, p_read, p_save = _patch_io()
    conn, _ = _make_conn(_perms_with("trustcenter.view"))

    with p_read, p_save:
        sharing_access, error = sc.core_assign_resource(
            "trust_center",
            ADMIN_ID, ADMIN_EMAIL,
            USER_B_ID, USER_B_EMAIL,
            ADMIN_ID,  # resource_id = owner_user_id for trust_center
            "Trust Center",
            conn,
            level="view",
        )

    assert error is None
    b_entry = next(e for e in sharing_access if e["id"] == USER_B_ID)
    assert b_entry["level"] == "view"
    admin_entry = next(e for e in sharing_access if e["id"] == ADMIN_ID)
    assert admin_entry["level"] == "edit"  # owner is always edit


def test_core_assign_resource_trust_center_edit():
    _, p_read, p_save = _patch_io()
    conn, _ = _make_conn(_perms_with("trustcenter.view"))

    with p_read, p_save:
        sharing_access, error = sc.core_assign_resource(
            "trust_center",
            ADMIN_ID, ADMIN_EMAIL,
            USER_B_ID, USER_B_EMAIL,
            ADMIN_ID, "Trust Center", conn,
            level="edit",
        )

    assert error is None
    b_entry = next(e for e in sharing_access if e["id"] == USER_B_ID)
    assert b_entry["level"] == "edit"


def test_core_assign_resource_trust_center_rejects_invalid_level():
    _, p_read, p_save = _patch_io()
    conn, _ = _make_conn(_perms_with("trustcenter.view"))

    with p_read, p_save:
        sharing_access, error = sc.core_assign_resource(
            "trust_center",
            ADMIN_ID, ADMIN_EMAIL,
            USER_B_ID, USER_B_EMAIL,
            ADMIN_ID, "Trust Center", conn,
            level="admin",  # not "view"/"edit"
        )

    assert sharing_access is None
    assert error == "level must be 'view' or 'edit'"


def test_core_assign_resource_trust_center_rejects_missing_level():
    _, p_read, p_save = _patch_io()
    conn, _ = _make_conn(_perms_with("trustcenter.view"))

    with p_read, p_save:
        sharing_access, error = sc.core_assign_resource(
            "trust_center",
            ADMIN_ID, ADMIN_EMAIL,
            USER_B_ID, USER_B_EMAIL,
            ADMIN_ID, "Trust Center", conn,
        )

    assert sharing_access is None
    assert "level" in error.lower()


# ── core_revoke_resource ──────────────────────────────────────────────────────


def test_core_revoke_resource_flips_access_and_decrements_count():
    storage, p_read, p_save = _patch_io()
    conn, _ = _make_conn(_perms_with("trackers.table.view"))

    with p_read, p_save:
        sc.core_assign_resource(
            "tracker", ADMIN_ID, ADMIN_EMAIL,
            USER_B_ID, USER_B_EMAIL,
            TRACKER_ID, TRACKER_NAME, conn,
        )
        sharing_access, error = sc.core_revoke_resource(
            "tracker", ADMIN_ID, USER_B_ID, TRACKER_ID
        )

    assert error is None
    b_entry = next(e for e in sharing_access if e["id"] == USER_B_ID)
    assert b_entry["access"] is False

    admin_cfg = storage[f"{ADMIN_ID}/tracker/sharedconfig.json"]
    assert admin_cfg["users"][USER_B_ID]["count"] == 0

    # The user-side index should no longer carry the revoked resource.
    user_idx = storage[f"{USER_B_ID}/shared/tracker.json"]
    assert TRACKER_ID not in user_idx


def test_core_revoke_resource_idempotent_on_unshared_resource():
    _, p_read, p_save = _patch_io()
    with p_read, p_save:
        sharing_access, error = sc.core_revoke_resource(
            "tracker", ADMIN_ID, USER_B_ID, "nonexistent-tracker"
        )
    assert error is None
    assert sharing_access == []


# ── get_user_resource_access ──────────────────────────────────────────────────


def test_get_user_resource_access_owner_always_granted():
    _, p_read, p_save = _patch_io()
    with p_read, p_save:
        result = sc.get_user_resource_access(
            "tracker", ADMIN_ID, TRACKER_ID, ADMIN_ID
        )
    assert result == {"granted": True, "level": "edit"}


def test_get_user_resource_access_returns_granted_with_level_for_trust_center():
    _, p_read, p_save = _patch_io()
    conn, _ = _make_conn(_perms_with("trustcenter.view"))

    with p_read, p_save:
        sc.core_assign_resource(
            "trust_center",
            ADMIN_ID, ADMIN_EMAIL,
            USER_B_ID, USER_B_EMAIL,
            ADMIN_ID, "TC", conn,
            level="view",
        )
        access = sc.get_user_resource_access(
            "trust_center", ADMIN_ID, ADMIN_ID, USER_B_ID
        )
    assert access == {"granted": True, "level": "view"}


def test_get_user_resource_access_revoked_user_not_granted():
    _, p_read, p_save = _patch_io()
    conn, _ = _make_conn(_perms_with("trackers.table.view"))

    with p_read, p_save:
        sc.core_assign_resource(
            "tracker", ADMIN_ID, ADMIN_EMAIL,
            USER_B_ID, USER_B_EMAIL,
            TRACKER_ID, TRACKER_NAME, conn,
        )
        sc.core_revoke_resource("tracker", ADMIN_ID, USER_B_ID, TRACKER_ID)
        access = sc.get_user_resource_access(
            "tracker", ADMIN_ID, TRACKER_ID, USER_B_ID
        )
    assert access == {"granted": False, "level": None}


def test_get_user_resource_access_unknown_user_not_granted():
    _, p_read, p_save = _patch_io()
    with p_read, p_save:
        access = sc.get_user_resource_access(
            "tracker", ADMIN_ID, TRACKER_ID, "stranger"
        )
    assert access == {"granted": False, "level": None}


def test_get_user_resource_access_unknown_resource_type_raises():
    with pytest.raises(ValueError):
        sc._resource_config("garbage_type")


# ── core_list_resource_shares ─────────────────────────────────────────────────


def test_core_list_resource_shares_round_trips_through_storage():
    _, p_read, p_save = _patch_io()
    conn, _ = _make_conn(_perms_with("trackers.table.view"))

    with p_read, p_save:
        sc.core_assign_resource(
            "tracker", ADMIN_ID, ADMIN_EMAIL,
            USER_B_ID, USER_B_EMAIL,
            TRACKER_ID, TRACKER_NAME, conn,
        )
        sharing_access, error = sc.core_list_resource_shares(
            "tracker", ADMIN_ID, TRACKER_ID
        )

    assert error is None
    ids = {e["id"] for e in sharing_access}
    assert {ADMIN_ID, USER_B_ID}.issubset(ids)
