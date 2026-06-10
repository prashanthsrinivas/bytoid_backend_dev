"""SG-audit service — create/track audits and their scope.

Orchestrates the S3 storage layer plus the shared schema. Holds no Flask/request
state so it is unit-testable in isolation. Cross-account collection (Lambda),
scoring, dashboard, and AI recommendation are layered on by other modules.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from utils.base_logger import get_logger
from sg_audit import config as sg_config
from sg_audit.schema import SCAN_PENDING
from sg_audit.storage import SgAuditStorage

logger = get_logger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _retention_until_iso() -> str:
    days = max(sg_config.SG_RETENTION_DAYS, 1)
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_list(values) -> list[str]:
    if not values:
        return []
    return [str(v).strip() for v in values if str(v).strip()]


class SgAuditService:
    def __init__(self, storage: SgAuditStorage | None = None):
        self.storage = storage or SgAuditStorage()

    # -- lifecycle ------------------------------------------------------------
    def create_audit(
        self,
        user_id: str,
        *,
        name: str = "",
        account_ids: list[str] | None = None,
        regions: list[str] | None = None,
        role_name: str | None = None,
        external_id: str | None = None,
        discover: bool | None = None,
    ) -> dict:
        """Create + persist an audit record (S3). Returns the record.

        A per-tenant ExternalId is generated when not supplied — it is the
        confused-deputy defense paired with the member-account trust policy.
        """
        audit_id = uuid.uuid4().hex
        now = _utc_now_iso()
        accounts = _clean_list(account_ids)
        record = {
            "audit_id": audit_id,
            "user_id": user_id,
            "name": (name or "").strip() or "AWS Security Group Audit",
            "account_ids": accounts,
            "regions": _clean_list(regions),
            "role_name": (role_name or "").strip() or sg_config.SG_DEFAULT_AUDIT_ROLE_NAME,
            "external_id": (external_id or "").strip() or uuid.uuid4().hex,
            # Discover via organizations:ListAccounts unless an explicit list is given.
            "discover": (not accounts) if discover is None else bool(discover),
            "scan_state": SCAN_PENDING,
            "latest_scan_id": None,
            "last_scan_at": None,
            "next_scan_at": None,
            "latest_risk_score": None,
            "latest_posture_score": None,
            "retention_until": _retention_until_iso(),
            "created_at": now,
            "updated_at": now,
        }
        self.storage.save_audit(user_id, record)
        logger.info("Created SG audit %s for user %s", audit_id, user_id)
        return record

    def get_audit(self, user_id: str, audit_id: str) -> dict | None:
        return self.storage.get_audit(user_id, audit_id)

    def list_audits(self, user_id: str) -> list[dict]:
        return self.storage.list_audits(user_id)

    def delete_audit(self, user_id: str, audit_id: str) -> bool:
        if not self.storage.get_audit(user_id, audit_id):
            return False
        self.storage.delete_audit(user_id, audit_id)
        return True

    # -- scope ----------------------------------------------------------------
    def set_targets(
        self,
        user_id: str,
        audit_id: str,
        *,
        name: str | None = None,
        account_ids: list[str] | None = None,
        regions: list[str] | None = None,
        role_name: str | None = None,
        external_id: str | None = None,
        discover: bool | None = None,
    ) -> dict | None:
        """Update an audit's scope. Returns the updated record, or None."""
        record = self.storage.get_audit(user_id, audit_id)
        if not record:
            return None
        if name is not None:
            record["name"] = name.strip() or record["name"]
        if account_ids is not None:
            record["account_ids"] = _clean_list(account_ids)
        if regions is not None:
            record["regions"] = _clean_list(regions)
        if role_name is not None and role_name.strip():
            record["role_name"] = role_name.strip()
        if external_id is not None and external_id.strip():
            record["external_id"] = external_id.strip()
        if discover is not None:
            record["discover"] = bool(discover)
        elif account_ids is not None:
            # Keep discover coherent with an edited account list.
            record["discover"] = not record["account_ids"]
        record["updated_at"] = _utc_now_iso()
        self.storage.save_audit(user_id, record)
        return record

    def scope_of(self, record: dict) -> dict:
        """The scope dict used for fingerprinting + passed to the collector."""
        return {
            "account_ids": record.get("account_ids") or [],
            "regions": record.get("regions") or [],
            "role_name": record.get("role_name") or sg_config.SG_DEFAULT_AUDIT_ROLE_NAME,
            "discover": bool(record.get("discover")),
        }

    def ready_for_collection(self, record: dict) -> bool:
        """True once the audit has a usable scope (explicit accounts or discovery)."""
        if not record:
            return False
        return bool(record.get("account_ids") or record.get("discover"))
