"""S3-backed storage for SG-audit — the single persistence layer for this module.

By design ALL state lives in S3 (no new MySQL/LanceDB tables), consistent with
how the ``vra`` module stores its config/snapshots. Two kinds of object, both
under a per-user ``{user_id}/sg_audit/`` prefix:

  * **Audit record** — ``{user_id}/sg_audit/audits/{audit_id}.json`` holds the
    scope (target accounts/regions/role), the per-tenant ExternalId, and the
    scan lifecycle/retention. Stored as clear JSON (the ExternalId is a
    confused-deputy nonce, not a credential; it is paired with an
    account-bound trust policy).

  * **Posture snapshot** — one per audit run, at
    ``{user_id}/sg_audit/posture/{audit_id}/{scan_id}.json``. The ``findings``
    blob is KMS-encrypted at rest; lightweight metadata (risk/posture score,
    counts, scanned_at) is kept clear so the dashboard trend can be built
    without decrypting. A sibling ``_index.json`` holds just that metadata.

Everything here is per-user-scoped by S3 key prefix; cross-user access control is
enforced at the route layer (same sharing model as runbooks/VRA).
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


def _audits_prefix(user_id: str) -> str:
    return f"{user_id}/sg_audit/audits/"


def _audit_key(user_id: str, audit_id: str) -> str:
    return f"{_audits_prefix(user_id)}{audit_id}.json"


def _posture_prefix(user_id: str, audit_id: str) -> str:
    return f"{user_id}/sg_audit/posture/{audit_id}/"


def _snapshot_key(user_id: str, audit_id: str, scan_id: str) -> str:
    return f"{_posture_prefix(user_id, audit_id)}{scan_id}.json"


def _index_key(user_id: str, audit_id: str) -> str:
    return f"{_posture_prefix(user_id, audit_id)}{_INDEX_NAME}"


class SgAuditStorage:
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

    def _list_keys(self, prefix: str) -> list[str]:
        from utils.s3_utils import list_all_files

        return [obj["Key"] for obj in list_all_files(prefix) if "Key" in obj]

    # -- audit record ---------------------------------------------------------
    def save_audit(self, user_id: str, record: dict) -> dict:
        """Upsert the audit record. ``record`` must include audit_id."""
        self._put_json(_audit_key(user_id, record["audit_id"]), record)
        return record

    def get_audit(self, user_id: str, audit_id: str) -> dict | None:
        return read_json_from_s3(_audit_key(user_id, audit_id))

    def list_audits(self, user_id: str) -> list[dict]:
        out = []
        for key in self._list_keys(_audits_prefix(user_id)):
            if key.endswith(".json"):
                rec = read_json_from_s3(key)
                if rec:
                    out.append(rec)
        out.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return out

    def delete_audit(self, user_id: str, audit_id: str) -> None:
        """Delete the record AND all its posture snapshots (cascade)."""
        delete_file_from_s3(_audit_key(user_id, audit_id))
        delete_folder_from_s3(_posture_prefix(user_id, audit_id))

    # -- posture snapshots ----------------------------------------------------
    def save_snapshot(self, user_id: str, snapshot: dict) -> dict:
        """Persist one audit snapshot (findings encrypted) + update the index."""
        audit_id = snapshot["audit_id"]
        scan_id = snapshot["scan_id"]
        findings = snapshot.get("findings", [])
        stored = {
            "scan_id": scan_id,
            "audit_id": audit_id,
            "scanned_at": snapshot.get("scanned_at", ""),
            "risk_score": snapshot.get("risk_score", 0.0),
            "posture_score": snapshot.get("posture_score", 0.0),
            "counts": snapshot.get("counts", {}),
            "accounts_scanned": snapshot.get("accounts_scanned", []),
            "scope": snapshot.get("scope", {}),
            "collector_status": snapshot.get("collector_status", {}),
            "findings_enc": self._enc(user_id, json.dumps(findings)),
        }
        self._put_json(_snapshot_key(user_id, audit_id, scan_id), stored)
        self._append_index(user_id, audit_id, stored)
        return stored

    def get_snapshot(self, user_id: str, audit_id: str, scan_id: str) -> dict | None:
        """Read one snapshot with its findings decrypted."""
        stored = read_json_from_s3(_snapshot_key(user_id, audit_id, scan_id))
        if not stored:
            return None
        return self._hydrate(user_id, stored)

    def list_snapshot_index(self, user_id: str, audit_id: str) -> list[dict]:
        """Metadata for every snapshot, newest first (no findings, no decrypt)."""
        index = read_json_from_s3(_index_key(user_id, audit_id)) or []
        index.sort(key=lambda r: r.get("scanned_at", ""), reverse=True)
        return index

    def get_latest_snapshot(self, user_id: str, audit_id: str) -> dict | None:
        index = self.list_snapshot_index(user_id, audit_id)
        if not index:
            return None
        return self.get_snapshot(user_id, audit_id, index[0]["scan_id"])

    def trend(self, user_id: str, audit_id: str) -> list[dict]:
        """Oldest->newest (scanned_at, risk_score, posture_score) for the chart."""
        index = self.list_snapshot_index(user_id, audit_id)
        points = [
            {
                "scanned_at": r.get("scanned_at", ""),
                "risk_score": r.get("risk_score", 0.0),
                "posture_score": r.get("posture_score", 0.0),
            }
            for r in index
        ]
        points.sort(key=lambda p: p["scanned_at"])
        return points

    def delete_snapshot(self, user_id: str, audit_id: str, scan_id: str) -> None:
        delete_file_from_s3(_snapshot_key(user_id, audit_id, scan_id))
        key = _index_key(user_id, audit_id)
        index = read_json_from_s3(key) or []
        self._put_json(key, [e for e in index if e.get("scan_id") != scan_id])

    def purge_snapshots_before(
        self, user_id: str, audit_id: str, cutoff_iso: str, keep_latest: bool = True
    ) -> int:
        """Delete snapshots scanned before ``cutoff_iso``. Returns count removed.

        Always keeps the most recent snapshot when ``keep_latest`` is set, so the
        dashboard never goes blank even if it is older than the cutoff.
        """
        index = self.list_snapshot_index(user_id, audit_id)  # newest first
        if not index:
            return 0
        keep = {index[0]["scan_id"]} if keep_latest else set()
        stale = [
            e for e in index
            if e["scan_id"] not in keep and e.get("scanned_at", "") < cutoff_iso
        ]
        for e in stale:
            self.delete_snapshot(user_id, audit_id, e["scan_id"])
        return len(stale)

    # -- internals ------------------------------------------------------------
    def _hydrate(self, user_id: str, stored: dict) -> dict:
        out = dict(stored)
        env = out.pop("findings_enc", None)
        try:
            out["findings"] = json.loads(self._dec(user_id, env)) if env else []
        except Exception:
            logger.warning("Failed to decrypt SG-audit findings; returning empty", exc_info=True)
            out["findings"] = []
        return out

    def _append_index(self, user_id: str, audit_id: str, stored: dict) -> None:
        key = _index_key(user_id, audit_id)
        index = read_json_from_s3(key) or []
        meta = {
            "scan_id": stored["scan_id"],
            "scanned_at": stored.get("scanned_at", ""),
            "risk_score": stored.get("risk_score", 0.0),
            "posture_score": stored.get("posture_score", 0.0),
            "counts": stored.get("counts", {}),
        }
        # Idempotent: replace an existing entry for the same scan_id.
        index = [e for e in index if e.get("scan_id") != stored["scan_id"]]
        index.append(meta)
        self._put_json(key, index)
