"""S3-backed storage for a CSPM provider (namespaced by ``s3_namespace``).

Same design as ``sg_audit/storage.py``: audit records + KMS-encrypted posture
snapshots + an index + AI-recommendation artifacts + remediation links, all under
``{user_id}/{namespace}/...``.
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
_INDEX = "_index.json"


class CspmStorage:
    def __init__(self, namespace: str):
        self.ns = namespace
        self._kms = SecureKMSService()

    # -- key layout -----------------------------------------------------------
    def _audits_prefix(self, u): return f"{u}/{self.ns}/audits/"
    def _audit_key(self, u, a): return f"{self._audits_prefix(u)}{a}.json"
    def _posture_prefix(self, u, a): return f"{u}/{self.ns}/posture/{a}/"
    def _snap_key(self, u, a, s): return f"{self._posture_prefix(u, a)}{s}.json"
    def _index_key(self, u, a): return f"{self._posture_prefix(u, a)}{_INDEX}"
    def _rec_key(self, u, a, s): return f"{self._posture_prefix(u, a)}{s}.rec.json"
    def _rem_key(self, u, a): return f"{self._audits_prefix(u)}{a}.remediations.json"

    # -- low level ------------------------------------------------------------
    def _put_json(self, key, obj):
        s3bucket().put_object(Bucket=S3_BUCKET, Key=key,
                              Body=json.dumps(obj, default=str).encode("utf-8"),
                              ContentType="application/json")

    def _enc(self, u, plaintext):
        env = self._kms.encrypt(u, plaintext)
        return {"ciphertext": env["ciphertext"], "iv": env["iv"], "encrypted_key": env["encrypted_key"]}

    def _dec(self, u, env):
        return self._kms.decrypt(u, env["encrypted_key"], env["iv"], env["ciphertext"])

    def _list_keys(self, prefix):
        from utils.s3_utils import list_all_files
        return [o["Key"] for o in list_all_files(prefix) if "Key" in o]

    # -- audit records --------------------------------------------------------
    def save_audit(self, u, record):
        self._put_json(self._audit_key(u, record["audit_id"]), record)
        return record

    def get_audit(self, u, a):
        return read_json_from_s3(self._audit_key(u, a))

    def list_audits(self, u):
        out = []
        for key in self._list_keys(self._audits_prefix(u)):
            if not key.endswith(".json") or key.endswith(".remediations.json"):
                continue
            rec = read_json_from_s3(key)
            if isinstance(rec, dict) and rec.get("audit_id"):
                out.append(rec)
        out.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return out

    def delete_audit(self, u, a):
        delete_file_from_s3(self._audit_key(u, a))
        delete_folder_from_s3(self._posture_prefix(u, a))

    # -- snapshots ------------------------------------------------------------
    def save_snapshot(self, u, snapshot):
        a, s = snapshot["audit_id"], snapshot["scan_id"]
        stored = {
            "scan_id": s, "audit_id": a, "scanned_at": snapshot.get("scanned_at", ""),
            "risk_score": snapshot.get("risk_score", 0.0), "posture_score": snapshot.get("posture_score", 0.0),
            "counts": snapshot.get("counts", {}), "scopes_scanned": snapshot.get("scopes_scanned", []),
            "scope": snapshot.get("scope", {}), "collector_status": snapshot.get("collector_status", {}),
            "findings_enc": self._enc(u, json.dumps(snapshot.get("findings", []))),
        }
        self._put_json(self._snap_key(u, a, s), stored)
        self._append_index(u, a, stored)
        return stored

    def get_snapshot(self, u, a, s):
        stored = read_json_from_s3(self._snap_key(u, a, s))
        return self._hydrate(u, stored) if stored else None

    def list_snapshot_index(self, u, a):
        idx = read_json_from_s3(self._index_key(u, a)) or []
        idx.sort(key=lambda r: r.get("scanned_at", ""), reverse=True)
        return idx

    def get_latest_snapshot(self, u, a):
        idx = self.list_snapshot_index(u, a)
        return self.get_snapshot(u, a, idx[0]["scan_id"]) if idx else None

    def trend(self, u, a):
        pts = [{"scanned_at": r.get("scanned_at", ""), "risk_score": r.get("risk_score", 0.0),
                "posture_score": r.get("posture_score", 0.0)} for r in self.list_snapshot_index(u, a)]
        pts.sort(key=lambda p: p["scanned_at"])
        return pts

    def delete_snapshot(self, u, a, s):
        delete_file_from_s3(self._snap_key(u, a, s))
        key = self._index_key(u, a)
        idx = read_json_from_s3(key) or []
        self._put_json(key, [e for e in idx if e.get("scan_id") != s])

    def purge_snapshots_before(self, u, a, cutoff_iso, keep_latest=True):
        idx = self.list_snapshot_index(u, a)
        if not idx:
            return 0
        keep = {idx[0]["scan_id"]} if keep_latest else set()
        stale = [e for e in idx if e["scan_id"] not in keep and e.get("scanned_at", "") < cutoff_iso]
        for e in stale:
            self.delete_snapshot(u, a, e["scan_id"])
        return len(stale)

    # -- AI recommendation + remediation links --------------------------------
    def save_recommendation(self, u, a, s, rec):
        self._put_json(self._rec_key(u, a, s), rec)
        return rec

    def get_recommendation(self, u, a, s):
        return read_json_from_s3(self._rec_key(u, a, s))

    def get_remediation_links(self, u, a):
        return read_json_from_s3(self._rem_key(u, a)) or {}

    def save_remediation_link(self, u, a, finding_id, link):
        links = self.get_remediation_links(u, a)
        links[finding_id] = link
        self._put_json(self._rem_key(u, a), links)
        return link

    # -- internals ------------------------------------------------------------
    def _hydrate(self, u, stored):
        out = dict(stored)
        env = out.pop("findings_enc", None)
        try:
            out["findings"] = json.loads(self._dec(u, env)) if env else []
        except Exception:
            logger.warning("Failed to decrypt findings; returning empty", exc_info=True)
            out["findings"] = []
        return out

    def _append_index(self, u, a, stored):
        key = self._index_key(u, a)
        idx = read_json_from_s3(key) or []
        meta = {"scan_id": stored["scan_id"], "scanned_at": stored.get("scanned_at", ""),
                "risk_score": stored.get("risk_score", 0.0), "posture_score": stored.get("posture_score", 0.0),
                "counts": stored.get("counts", {})}
        idx = [e for e in idx if e.get("scan_id") != stored["scan_id"]]
        idx.append(meta)
        self._put_json(key, idx)
