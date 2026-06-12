"""Fixtures for CSPM action-plan / architecture / drill-down tests.

``bootstrap_sut()`` runs first: it installs the db/redis stub modules that
``cspm_core.helpers`` (and the route modules) import at module load. All S3
sidecar IO is redirected to an in-memory dict by patching the names
``cspm_core.finding_detail`` bound at import — action_plan and architecture
reuse those helpers, so one patch point covers everything.
"""

from __future__ import annotations

import json
import re
import sys
import types

import pytest

from tests.workflow_playbook import _wf_pb_stubs as stubs

stubs.bootstrap_sut()

import cspm_core.finding_detail as fd  # noqa: E402
from cspm_core.action_plan import ActionPlanContext  # noqa: E402
from cspm_core.finding_detail import DetailContext  # noqa: E402


class _Bucket:
    def __init__(self, store):
        self._store = store

    def put_object(self, Bucket=None, Key=None, Body=None, ContentType=None):
        self._store[Key] = json.loads(Body)


def _extract_json_safe(text):
    """Minimal, deterministic stand-in for utils.fireworkzz.extract_json_safe."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(json)?|```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
    return None


@pytest.fixture(autouse=True)
def _fireworkzz_shim(monkeypatch):
    """Other suites in this repo replace utils.fireworkzz in sys.modules with
    partial stubs; pin a known-good extract_json_safe for the duration of each
    cspm test so the deferred imports in cspm_core resolve deterministically."""
    mod = types.ModuleType("utils.fireworkzz")
    mod.extract_json_safe = _extract_json_safe
    monkeypatch.setitem(sys.modules, "utils.fireworkzz", mod)


@pytest.fixture
def s3_store(monkeypatch):
    """In-memory S3: keys -> parsed JSON, replacing the sidecar helpers' IO."""
    store: dict = {}
    monkeypatch.setattr(fd, "read_json_from_s3", store.get)
    monkeypatch.setattr(fd, "s3bucket", lambda: _Bucket(store))
    monkeypatch.setattr(fd, "S3_BUCKET", "test-bucket", raising=False)
    return store


def sg_finding(rule_id="SG_ADMIN_WORLD_INGRESS", *, fid=None, severity="critical",
               group_id="sg-0123456789abcdef0", cidr="0.0.0.0/0", protocol="tcp",
               from_port=22, to_port=22, account="394711685916", region="ca-central-1",
               entity_type="security_group", entity_id=None, entity_name=None,
               extra_details=None):
    eid = entity_id or group_id
    details = {
        "account_id": account, "account_name": "Test Account", "region": region,
        "group_id": group_id, "group_name": "test-sg", "vpc_id": "vpc-00000001",
        "rule_id": rule_id, "cidr": cidr, "protocol": protocol,
        "from_port": from_port, "to_port": to_port,
        "entity_type": entity_type, "entity_id": eid,
        "entity_name": entity_name or "test-sg", "in_use": True,
    }
    details.update(extra_details or {})
    return {
        "finding_id": fid or f"{account}:{group_id}:{rule_id}:{cidr}:{protocol}:{from_port}-{to_port}:",
        "domain": "security_groups", "category": "network_exposure",
        "rule_id": rule_id, "severity": severity,
        "finding_summary": f"{rule_id} on {eid}",
        "evidence_type": "sg_rule", "source": "test",
        "collected_at": "2026-06-12T00:00:00Z",
        "risk_indicators": [rule_id],
        "supporting_details": details,
    }


def snapshot(findings, scan_id="scan-1", audit_id="a1"):
    return {"scan_id": scan_id, "audit_id": audit_id,
            "scanned_at": "2026-06-12T00:00:00Z", "findings": findings}


@pytest.fixture
def sg_plan_ctx():
    """Factory: ActionPlanContext over a fixed snapshot (real sg metadata + builders)."""
    from sg_audit import metadata
    from sg_audit.cli_commands import CLI_BUILDERS

    def make(snap, rec=None):
        return ActionPlanContext(
            key="sg", label="AWS", namespace="sg_audit", redis_namespace="sg_audit",
            meta=metadata.meta,
            get_snapshot=lambda u, a, s=None: snap,
            get_recommendation=lambda u, a, s: rec,
            cli_tool="aws", cli_builders=CLI_BUILDERS, scope_key="account_id")

    return make


@pytest.fixture
def sg_detail_ctx():
    """Factory: DetailContext over a fixed snapshot (for the drill-down contract)."""
    from sg_audit import metadata

    def make(snap):
        return DetailContext(
            key="sg", label="AWS", namespace="sg_audit", redis_namespace="sg_audit",
            meta=metadata.meta,
            get_snapshot=lambda u, a, s=None: snap,
            rescan=lambda u, a, f: {"status": "launched"},
            has_fixer=lambda r: False, scope_key="account_id")

    return make
