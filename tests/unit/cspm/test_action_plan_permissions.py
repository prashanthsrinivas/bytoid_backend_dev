"""Unit — role-management registration for the action-plan permissions.

Guards the known trap: a permission key missing from PERMISSION_METADATA is
silently dropped by resolve_permissions and 403s everyone.
"""

from __future__ import annotations

import pytest

from utils.permission_metadata import PERMISSION_METADATA
from utils.permissions_map import PERMISSIONS as PERMISSIONS_MAP

NAMESPACES = ("sg_audit", "azure_audit", "gcp_audit")
ACTIONS = ("generate", "edit", "request")


@pytest.mark.parametrize("ns", NAMESPACES)
@pytest.mark.parametrize("action", ACTIONS)
def test_action_plan_keys_registered_everywhere(ns, action):
    key = f"{ns}.action_plan.{action}"
    assert key in PERMISSION_METADATA, f"{key} missing from PERMISSION_METADATA"
    assert key in PERMISSIONS_MAP, f"{key} missing from PERMISSIONS_MAP (role-management label)"
    meta = PERMISSION_METADATA[key]
    assert meta["label"] and meta["module"] == "Compliance"


@pytest.mark.parametrize("ns", NAMESPACES)
@pytest.mark.parametrize("action", ACTIONS)
def test_dependencies_are_themselves_registered(ns, action):
    deps = PERMISSION_METADATA[f"{ns}.action_plan.{action}"]["dependencies"]
    assert f"{ns}.findings.read" in deps
    for dep in deps:
        assert dep in PERMISSION_METADATA, f"unknown dependency {dep} would 403 everyone"


def test_provider_perms_expose_action_plan_keys():
    from azure_audit.provider import PERMS as AZ
    from gcp_audit.provider import PERMS as GCP

    for perms, ns in ((AZ, "azure_audit"), (GCP, "gcp_audit")):
        assert perms["action_plan_generate"] == f"{ns}.action_plan.generate"
        assert perms["action_plan_edit"] == f"{ns}.action_plan.edit"
        assert perms["action_plan_request"] == f"{ns}.action_plan.request"
