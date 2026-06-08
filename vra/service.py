"""VRA assessment service — create/track VRAs and keep the report title synced.

Orchestrates the S3 storage layer plus the shared schema. Holds no Flask/request
state so it is unit-testable in isolation. The actual OSINT collection (Lambda),
runbook wiring, and dashboard are layered on in later phases.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from utils.base_logger import get_logger
from vra import config as vra_config
from vra.osint.safe_fetch import normalize_domain
from vra.schema import (
    ASSESSMENT_VRA,
    DEFAULT_VRA_QUESTIONS,
    SCAN_PENDING,
    report_title_for,
)
from vra.storage import VraStorage

logger = get_logger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _retention_until_iso() -> str:
    days = max(vra_config.VRA_RETENTION_DAYS, 1)
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def build_default_question_items() -> list[dict]:
    """The two mandatory VRA questions as runbook ``chat[].output[]`` items.

    Shaped to match the playbook question schema (``id``/``question``/
    ``user_answer``/``options``/``comment``/``section``) so they slot straight
    into a workflow; the extra ``vra_role``/``locked``/``required`` keys are
    additive and ignored by the existing question extractor.
    """
    items = []
    for spec in DEFAULT_VRA_QUESTIONS:
        items.append(
            {
                "id": uuid.uuid4().hex,
                "question": spec["question"],
                "user_answer": "",
                "options": {},
                "comment": "",
                "section": "Vendor Identification",
                "help_text": spec["help_text"],
                "vra_role": spec["vra_role"],
                "required": spec["required"],
                "locked": spec["locked"],
            }
        )
    return items


class VraService:
    def __init__(self, storage: VraStorage | None = None):
        self.storage = storage or VraStorage()

    # -- lifecycle ------------------------------------------------------------
    def create_assessment(
        self,
        user_id: str,
        *,
        playbook_id: str | None = None,
        runbook_id: str | None = None,
        vendor_name: str = "",
        vendor_domain: str = "",
        assessment_type: str = ASSESSMENT_VRA,
    ) -> dict:
        """Create + persist a VRA assessment mapping (S3). Returns the record."""
        assessment_id = uuid.uuid4().hex
        now = _utc_now_iso()
        record = {
            "assessment_id": assessment_id,
            "user_id": user_id,
            "playbook_id": playbook_id,
            "runbook_id": runbook_id,
            "vendor_name": (vendor_name or "").strip(),
            "vendor_domain": normalize_domain(vendor_domain) or "",
            "assessment_type": assessment_type,
            "report_title": report_title_for(vendor_name),
            "scan_state": SCAN_PENDING,
            "latest_scan_id": None,
            "last_scan_at": None,
            "next_scan_at": None,
            "retention_until": _retention_until_iso(),
            "created_at": now,
            "updated_at": now,
        }
        self.storage.save_assessment(user_id, record)
        logger.info("Created VRA assessment %s for user %s", assessment_id, user_id)
        return record

    def get_assessment(self, user_id: str, assessment_id: str) -> dict | None:
        return self.storage.get_assessment(user_id, assessment_id)

    def list_assessments(self, user_id: str) -> list[dict]:
        return self.storage.list_assessments(user_id)

    def delete_assessment(self, user_id: str, assessment_id: str) -> bool:
        if not self.storage.get_assessment(user_id, assessment_id):
            return False
        self.storage.delete_assessment(user_id, assessment_id)
        return True

    # -- vendor / title sync --------------------------------------------------
    def set_vendor(
        self,
        user_id: str,
        assessment_id: str,
        *,
        vendor_name: str | None = None,
        vendor_domain: str | None = None,
    ) -> dict | None:
        """Update vendor name/domain and keep the report title in lockstep.

        The report title is always "Vendor Risk Assessment - <Vendor Name>",
        so editing the vendor name re-derives it. Returns the updated record, or
        None if the assessment doesn't exist.
        """
        record = self.storage.get_assessment(user_id, assessment_id)
        if not record:
            return None
        if vendor_name is not None:
            record["vendor_name"] = vendor_name.strip()
            record["report_title"] = report_title_for(vendor_name)
        if vendor_domain is not None:
            record["vendor_domain"] = normalize_domain(vendor_domain) or ""
        record["updated_at"] = _utc_now_iso()
        self.storage.save_assessment(user_id, record)
        return record

    def ready_for_collection(self, record: dict) -> bool:
        """True once both vendor name and a valid domain are present."""
        return bool((record or {}).get("vendor_name") and (record or {}).get("vendor_domain"))
