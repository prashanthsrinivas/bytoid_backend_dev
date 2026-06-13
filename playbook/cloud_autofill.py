"""Cloud security-posture source for workflow auto-fill.

Resolves the LATEST CSPM posture snapshot per provider (AWS via sg_audit, Azure
& GCP via cspm_core) so the intake workflow can auto-answer questions from real
posture data. A provider with no posture snapshot is reported as unavailable —
the UI must not offer it ("never displayed → never available for ingress").
"""

from __future__ import annotations

import json

from utils.base_logger import get_logger

logger = get_logger(__name__)

POSTURE_PROVIDERS = ("aws", "azure", "gcp")


def _storage_for(provider: str):
    """Lazily build the storage facade for a provider (heavy imports deferred)."""
    provider = (provider or "").lower()
    if provider == "aws":
        from sg_audit.storage import SgAuditStorage

        return SgAuditStorage()
    if provider == "azure":
        from cspm_core.storage import CspmStorage
        from azure_audit.provider import AZURE_PROVIDER

        return CspmStorage(AZURE_PROVIDER.s3_namespace)
    if provider == "gcp":
        from cspm_core.storage import CspmStorage
        from gcp_audit.provider import GCP_PROVIDER

        return CspmStorage(GCP_PROVIDER.s3_namespace)
    raise ValueError(f"Unknown posture provider: {provider!r}")


def get_provider_availability(user_id: str) -> dict:
    """Per provider: whether ≥1 posture snapshot exists, plus the audits that
    have one (most-recent scan timestamp each). Each provider is isolated — a
    failure in one never blanks the others.
    """
    out = {}
    for provider in POSTURE_PROVIDERS:
        try:
            storage = _storage_for(provider)
            audits = storage.list_audits(user_id) or []
            available_audits = []
            for rec in audits:
                audit_id = rec.get("audit_id")
                if not audit_id:
                    continue
                index = storage.list_snapshot_index(user_id, audit_id) or []
                if not index:
                    continue  # audit exists but was never scanned → not ingestible
                available_audits.append(
                    {
                        "audit_id": audit_id,
                        "name": rec.get("name") or rec.get("display_name") or audit_id,
                        "latest_scanned_at": index[0].get("scanned_at", ""),
                    }
                )
            out[provider] = {
                "available": bool(available_audits),
                "audits": available_audits,
            }
        except Exception as e:
            logger.warning("Availability check failed for %s: %s", provider, e)
            out[provider] = {"available": False, "audits": []}
    return out


def _summarize_snapshot(snapshot: dict, max_chars: int) -> str:
    """Compact, JSON-serializable posture summary for the LLM prompt."""
    payload = {
        "scan_id": snapshot.get("scan_id"),
        "scanned_at": snapshot.get("scanned_at"),
        "risk_score": snapshot.get("risk_score"),
        "posture_score": snapshot.get("posture_score"),
        "counts": snapshot.get("counts", {}),
        "findings": snapshot.get("findings", []),
    }
    text = json.dumps(payload, default=str)
    return text[:max_chars]


def resolve_posture_payload(user_id: str, selections, max_chars: int = 16000):
    """Resolve the latest posture snapshot for each selected provider/audit.

    ``selections`` is a list of ``{"provider": "aws|azure|gcp", "audit_id"?: str}``.
    When ``audit_id`` is omitted, the most-recently-scanned audit is used.

    Returns ``(payload_str, raw_blob)`` mirroring the legacy connector path:
    ``payload_str`` is the concatenated, source-tagged text for the LLM;
    ``raw_blob`` is a list of per-source dicts (with ``error`` entries on failure)
    persisted for the UI "View source data" button.
    """
    payload_parts = []
    raw_blob = []
    for sel in selections or []:
        provider = (sel.get("provider") or "").lower()
        audit_id = sel.get("audit_id")
        try:
            storage = _storage_for(provider)
            if not audit_id:
                audits = storage.list_audits(user_id) or []
                # list_audits is sorted newest-first by created_at; prefer one
                # that actually has a snapshot.
                audit_id = next(
                    (
                        a.get("audit_id")
                        for a in audits
                        if a.get("audit_id")
                        and storage.list_snapshot_index(user_id, a["audit_id"])
                    ),
                    None,
                )
            snapshot = (
                storage.get_latest_snapshot(user_id, audit_id) if audit_id else None
            )
            if not snapshot:
                raw_blob.append(
                    {"provider": provider, "audit_id": audit_id, "error": "no snapshot"}
                )
                continue
            scanned_at = snapshot.get("scanned_at", "")
            payload_parts.append(
                f"[SOURCE provider={provider} audit={audit_id} scanned_at={scanned_at}]\n"
                + _summarize_snapshot(snapshot, max_chars)
            )
            raw_blob.append(
                {
                    "provider": provider,
                    "audit_id": audit_id,
                    "scanned_at": scanned_at,
                    "source": "posture",
                    "data": {
                        "risk_score": snapshot.get("risk_score"),
                        "posture_score": snapshot.get("posture_score"),
                        "counts": snapshot.get("counts", {}),
                        "findings": snapshot.get("findings", []),
                    },
                }
            )
        except Exception as e:
            logger.warning("Posture resolve failed for %s: %s", provider, e)
            raw_blob.append(
                {"provider": provider, "audit_id": audit_id, "error": str(e)}
            )
    return "\n\n".join(payload_parts), raw_blob
