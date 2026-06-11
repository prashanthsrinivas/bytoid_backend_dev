"""CSPM audit service — create/track audits + their scope (per provider)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from utils.base_logger import get_logger
from cspm_core.schema import SCAN_PENDING
from cspm_core.storage import CspmStorage

logger = get_logger(__name__)
_RETENTION_DAYS = 365


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean(values):
    return [str(v).strip() for v in (values or []) if str(v).strip()]


class CspmService:
    def __init__(self, provider, storage=None):
        self.provider = provider
        self.storage = storage or CspmStorage(provider.s3_namespace)

    def _clean_domains(self, values):
        if not values:
            return list(self.provider.domains)
        picked = [str(v).strip() for v in values if str(v).strip() in self.provider.domains]
        return picked or list(self.provider.domains)

    def create_audit(self, user_id, *, name="", scope_ids=None, domains=None,
                     organization_id="", regions=None) -> dict:
        audit_id = uuid.uuid4().hex
        now = _now()
        record = {
            "audit_id": audit_id, "user_id": user_id, "provider": self.provider.key,
            "name": (name or "").strip() or self.provider.default_audit_name,
            "scope_ids": _clean(scope_ids), "domains": self._clean_domains(domains),
            "organization_id": (organization_id or "").strip(), "regions": _clean(regions),
            "scan_state": SCAN_PENDING, "latest_scan_id": None, "last_scan_at": None,
            "latest_risk_score": None, "latest_posture_score": None, "next_scan_at": None,
            "retention_until": (datetime.now(timezone.utc) + timedelta(days=_RETENTION_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "created_at": now, "updated_at": now,
        }
        self.storage.save_audit(user_id, record)
        logger.info("Created %s audit %s for %s", self.provider.key, audit_id, user_id)
        return record

    def get_audit(self, u, a): return self.storage.get_audit(u, a)
    def list_audits(self, u): return self.storage.list_audits(u)

    def delete_audit(self, u, a):
        if not self.storage.get_audit(u, a):
            return False
        self.storage.delete_audit(u, a)
        return True

    def set_targets(self, user_id, audit_id, *, name=None, scope_ids=None, domains=None,
                    organization_id=None, regions=None) -> dict | None:
        record = self.storage.get_audit(user_id, audit_id)
        if not record:
            return None
        if name is not None:
            record["name"] = name.strip() or record["name"]
        if scope_ids is not None:
            record["scope_ids"] = _clean(scope_ids)
        if domains is not None:
            record["domains"] = self._clean_domains(domains)
        if organization_id is not None:
            record["organization_id"] = organization_id.strip()
        if regions is not None:
            record["regions"] = _clean(regions)
        record["updated_at"] = _now()
        self.storage.save_audit(user_id, record)
        return record

    def scope_of(self, record) -> dict:
        return {
            "scope_ids": record.get("scope_ids") or [],
            "domains": self._clean_domains(record.get("domains")),
            "organization_id": record.get("organization_id") or "",
            "regions": record.get("regions") or [],
        }

    def ready_for_collection(self, record) -> bool:
        return bool(record)
