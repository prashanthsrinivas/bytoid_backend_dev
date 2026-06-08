"""S3-backed storage for VRA — the single persistence layer for this module.

By design ALL VRA state lives in S3 (no new MySQL tables, no LanceDB tables),
consistent with how the playbook module stores its config/workflow JSON. Two
kinds of object, both under a per-user ``{user_id}/vra/`` prefix:

  * **Assessment mapping** — ``{user_id}/vra/assessments/{assessment_id}.json``
    links a VRA questionnaire (playbook) to its runbook + the vendor, and tracks
    scan lifecycle/retention. Stored as clear JSON (no secrets).

  * **Intelligence snapshot** — one per OSINT scan, at
    ``{user_id}/vra/intelligence/{assessment_id}/{scan_id}.json``. The
    ``findings`` blob is KMS-encrypted at rest; lightweight metadata
    (risk_score, counts, scanned_at) is kept clear so the dashboard trend can be
    built without decrypting. A sibling ``_index.json`` holds just that metadata
    so listing/trend is a single read instead of N.

Everything here is per-user-scoped by S3 key prefix; cross-user access control
is enforced at the route layer (same sharing model as runbooks).
"""

from __future__ import annotations

import json

from utils.base_logger import get_logger
from utils.key_rotation_manager import SecureKMSService
from utils.s3_utils import (
    S3_BUCKET,
    delete_file_from_s3,
    delete_folder_from_s3,
    read_json_from_s3,
    s3bucket,
)

logger = get_logger(__name__)

_INDEX_NAME = "_index.json"


def _assessments_prefix(user_id: str) -> str:
    return f"{user_id}/vra/assessments/"


def _assessment_key(user_id: str, assessment_id: str) -> str:
    return f"{_assessments_prefix(user_id)}{assessment_id}.json"


def _intel_prefix(user_id: str, assessment_id: str) -> str:
    return f"{user_id}/vra/intelligence/{assessment_id}/"


def _snapshot_key(user_id: str, assessment_id: str, scan_id: str) -> str:
    return f"{_intel_prefix(user_id, assessment_id)}{scan_id}.json"


def _index_key(user_id: str, assessment_id: str) -> str:
    return f"{_intel_prefix(user_id, assessment_id)}{_INDEX_NAME}"


class VraStorage:
    """Thin S3 persistence facade. Stateless aside from the KMS client."""

    def __init__(self):
        self._kms = SecureKMSService()

    # -- low-level S3 ---------------------------------------------------------
    def _put_json(self, key: str, obj) -> None:
        s3 = s3bucket()
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=key,
            Body=json.dumps(obj, default=str).encode("utf-8"),
            ContentType="application/json",
        )

    def _enc(self, user_id: str, plaintext: str) -> dict:
        env = self._kms.encrypt(user_id, plaintext)
        return {
            "ciphertext": env["ciphertext"],
            "iv": env["iv"],
            "encrypted_key": env["encrypted_key"],
        }

    def _dec(self, user_id: str, env: dict) -> str:
        return self._kms.decrypt(
            user_id, env["encrypted_key"], env["iv"], env["ciphertext"]
        )

    # -- assessment mapping ---------------------------------------------------
    def save_assessment(self, user_id: str, record: dict) -> dict:
        """Upsert the assessment mapping. ``record`` must include assessment_id."""
        assessment_id = record["assessment_id"]
        self._put_json(_assessment_key(user_id, assessment_id), record)
        return record

    def get_assessment(self, user_id: str, assessment_id: str) -> dict | None:
        return read_json_from_s3(_assessment_key(user_id, assessment_id))

    def list_assessments(self, user_id: str) -> list[dict]:
        out = []
        for key in self._list_keys(_assessments_prefix(user_id)):
            if key.endswith(".json"):
                rec = read_json_from_s3(key)
                if rec:
                    out.append(rec)
        out.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return out

    def delete_assessment(self, user_id: str, assessment_id: str) -> None:
        """Delete the mapping AND all its intelligence snapshots (cascade)."""
        delete_file_from_s3(_assessment_key(user_id, assessment_id))
        delete_folder_from_s3(_intel_prefix(user_id, assessment_id))

    # -- intelligence snapshots ----------------------------------------------
    def save_snapshot(self, user_id: str, snapshot: dict) -> dict:
        """Persist one scan snapshot (findings encrypted) + update the index."""
        assessment_id = snapshot["assessment_id"]
        scan_id = snapshot["scan_id"]
        findings = snapshot.get("findings", [])
        stored = {
            "scan_id": scan_id,
            "assessment_id": assessment_id,
            "vendor_name": snapshot.get("vendor_name", ""),
            "vendor_domain": snapshot.get("vendor_domain", ""),
            "scanned_at": snapshot.get("scanned_at", ""),
            "risk_score": snapshot.get("risk_score", 0.0),
            "counts": snapshot.get("counts", {}),
            "collector_status": snapshot.get("collector_status", {}),
            "findings_enc": self._enc(user_id, json.dumps(findings)),
        }
        self._put_json(_snapshot_key(user_id, assessment_id, scan_id), stored)
        self._append_index(user_id, assessment_id, stored)
        return stored

    def get_snapshot(
        self, user_id: str, assessment_id: str, scan_id: str
    ) -> dict | None:
        """Read one snapshot with its findings decrypted."""
        stored = read_json_from_s3(_snapshot_key(user_id, assessment_id, scan_id))
        if not stored:
            return None
        return self._hydrate(user_id, stored)

    def list_snapshot_index(self, user_id: str, assessment_id: str) -> list[dict]:
        """Metadata for every snapshot, newest first (no findings, no decrypt)."""
        index = read_json_from_s3(_index_key(user_id, assessment_id)) or []
        index.sort(key=lambda r: r.get("scanned_at", ""), reverse=True)
        return index

    def get_latest_snapshot(self, user_id: str, assessment_id: str) -> dict | None:
        index = self.list_snapshot_index(user_id, assessment_id)
        if not index:
            return None
        return self.get_snapshot(user_id, assessment_id, index[0]["scan_id"])

    def trend(self, user_id: str, assessment_id: str) -> list[dict]:
        """Oldest->newest (scanned_at, risk_score) points for the dashboard."""
        index = self.list_snapshot_index(user_id, assessment_id)
        points = [
            {"scanned_at": r.get("scanned_at", ""), "risk_score": r.get("risk_score", 0.0)}
            for r in index
        ]
        points.sort(key=lambda p: p["scanned_at"])
        return points

    def delete_snapshots(self, user_id: str, assessment_id: str) -> None:
        delete_folder_from_s3(_intel_prefix(user_id, assessment_id))

    def delete_snapshot(self, user_id: str, assessment_id: str, scan_id: str) -> None:
        """Delete one snapshot file and drop it from the index."""
        delete_file_from_s3(_snapshot_key(user_id, assessment_id, scan_id))
        key = _index_key(user_id, assessment_id)
        index = read_json_from_s3(key) or []
        self._put_json(key, [e for e in index if e.get("scan_id") != scan_id])

    def purge_snapshots_before(
        self, user_id: str, assessment_id: str, cutoff_iso: str, keep_latest: bool = True
    ) -> int:
        """Delete snapshots scanned before ``cutoff_iso``. Returns count removed.

        Always keeps the most recent snapshot (so the dashboard never goes blank)
        when ``keep_latest`` is set, even if it is older than the cutoff.
        """
        index = self.list_snapshot_index(user_id, assessment_id)  # newest first
        if not index:
            return 0
        keep = {index[0]["scan_id"]} if keep_latest else set()
        stale = [
            e for e in index
            if e["scan_id"] not in keep and e.get("scanned_at", "") < cutoff_iso
        ]
        for e in stale:
            self.delete_snapshot(user_id, assessment_id, e["scan_id"])
        return len(stale)

    # -- internals ------------------------------------------------------------
    def _hydrate(self, user_id: str, stored: dict) -> dict:
        out = dict(stored)
        env = out.pop("findings_enc", None)
        try:
            out["findings"] = json.loads(self._dec(user_id, env)) if env else []
        except Exception:
            logger.warning("Failed to decrypt VRA findings; returning empty", exc_info=True)
            out["findings"] = []
        return out

    def _append_index(self, user_id: str, assessment_id: str, stored: dict) -> None:
        key = _index_key(user_id, assessment_id)
        index = read_json_from_s3(key) or []
        meta = {
            "scan_id": stored["scan_id"],
            "scanned_at": stored.get("scanned_at", ""),
            "risk_score": stored.get("risk_score", 0.0),
            "counts": stored.get("counts", {}),
        }
        # Idempotent: replace an existing entry for the same scan_id.
        index = [e for e in index if e.get("scan_id") != stored["scan_id"]]
        index.append(meta)
        self._put_json(key, index)

    def _list_keys(self, prefix: str) -> list[str]:
        from utils.s3_utils import list_all_files

        return [obj["Key"] for obj in list_all_files(prefix) if "Key" in obj]
