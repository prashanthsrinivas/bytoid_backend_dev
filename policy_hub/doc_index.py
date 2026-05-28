"""RDS document-metadata index for fast Policy Hub list reads.

``/policy-hub/list`` used to do one S3 GET per document — deserializing and
decrypting the full HTML ``content`` and ``sections`` just to render a list
row. This table is a lightweight, queryable projection of the fields the
list/header views need, so listing N documents becomes a single indexed
query instead of N S3 round-trips.

S3 remains the source of truth for full document content; this index is
derived. Callers write-through on every persist (``upsert_document``) and
delete-through on every delete (``delete_document``); a nightly reconcile
heals any drift, mirroring ``services.statement_tracker_refs``.

``title`` is stored *encrypted* (same per-user KMS scheme as the S3 blob) so
the at-rest posture is unchanged; it is decrypted in the app layer on read.
Encryption is reached via a lazy import of ``policy_hub.routes`` to avoid an
import cycle (routes imports this module for the write-through hook).
"""

import json
from datetime import datetime, timezone

import pymysql.cursors

from db.rds_db import connect_to_rds
from utils.base_logger import get_logger

logger = get_logger(__name__)


def _ensure_table(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS policy_hub_documents (
            policy_id         VARCHAR(64)  NOT NULL,
            user_id           VARCHAR(64)  NOT NULL,
            org_id            VARCHAR(255) NULL,
            title_enc         TEXT         NULL,
            doc_ref           VARCHAR(16)  NULL,
            doc_type          VARCHAR(16)  NOT NULL,
            frameworks_json   TEXT         NULL,
            validation_status VARCHAR(32)  NULL,
            etag              VARCHAR(64)  NULL,
            created_at        VARCHAR(40)  NULL,
            updated_at        VARCHAR(40)  NULL,
            PRIMARY KEY (policy_id),
            KEY idx_owner (user_id, doc_type, created_at)
        )
        """
    )


def _encrypt_title(user_id: str, title: str | None) -> str | None:
    """Encrypt a title with the same scheme the S3 blob uses.

    Falls back to storing plaintext only if the encryption layer is genuinely
    unavailable (logged) — in production the lazy import always resolves.
    """
    if not title:
        return title
    try:
        from policy_hub.routes import _enc_ph
        return _enc_ph(user_id, title)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("doc_index title encryption unavailable, storing plaintext: %s", exc)
        return title


def _decrypt_title(user_id: str, value) -> str:
    if not value:
        return value or ""
    try:
        from policy_hub.routes import _dec_ph
        return _dec_ph(user_id, value)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("doc_index title decryption unavailable: %s", exc)
        return value if isinstance(value, str) else ""


def _resolve_org(user_id: str) -> str | None:
    try:
        from workflow_route.state_machine import get_user_org_id
        return get_user_org_id(user_id)
    except Exception:
        return None


def _row_to_item(row: dict) -> dict:
    """Shape a table row into the list-item dict the API returns."""
    user_id = row.get("user_id")
    frameworks = []
    raw_fw = row.get("frameworks_json")
    if raw_fw:
        try:
            frameworks = json.loads(raw_fw)
        except Exception:
            frameworks = []
    return {
        "policy_id": row.get("policy_id"),
        "title": _decrypt_title(user_id, row.get("title_enc")),
        "type": row.get("doc_type"),
        "doc_ref": row.get("doc_ref"),
        "frameworks": frameworks,
        "validation_status": row.get("validation_status"),
        "created_at": row.get("created_at"),
        "etag": row.get("etag"),
        "owner_user_id": user_id,
    }


def upsert_document(user_id: str, item: dict) -> None:
    """Insert or update the index row for *item* (a plaintext policy dict)."""
    policy_id = item.get("policy_id")
    if not policy_id or not user_id:
        return
    now = datetime.now(timezone.utc).isoformat()
    title_enc = _encrypt_title(user_id, item.get("title"))
    frameworks_json = json.dumps(item.get("frameworks") or [])
    org_id = _resolve_org(user_id)

    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            _ensure_table(cur)
            cur.execute(
                """
                INSERT INTO policy_hub_documents
                    (policy_id, user_id, org_id, title_enc, doc_ref, doc_type,
                     frameworks_json, validation_status, etag, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    user_id=VALUES(user_id),
                    org_id=VALUES(org_id),
                    title_enc=VALUES(title_enc),
                    doc_ref=VALUES(doc_ref),
                    doc_type=VALUES(doc_type),
                    frameworks_json=VALUES(frameworks_json),
                    validation_status=VALUES(validation_status),
                    etag=VALUES(etag),
                    created_at=VALUES(created_at),
                    updated_at=VALUES(updated_at)
                """,
                (
                    policy_id, user_id, org_id, title_enc,
                    item.get("doc_ref"), item.get("type") or "policy",
                    frameworks_json, item.get("validation_status"),
                    item.get("etag"), item.get("created_at"), now,
                ),
            )
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception as rb_exc:
            logger.debug("upsert_document rollback failed: %s", rb_exc)
        raise
    finally:
        conn.close()


def delete_document(policy_id: str) -> int:
    if not policy_id:
        return 0
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            _ensure_table(cur)
            cur.execute("DELETE FROM policy_hub_documents WHERE policy_id=%s", (policy_id,))
            deleted = cur.rowcount
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception as rb_exc:
            logger.debug("delete_document rollback failed: %s", rb_exc)
        raise
    finally:
        conn.close()
    return deleted


def list_documents(user_id: str) -> list[dict]:
    """Return the owner's documents as list-item dicts (one indexed query)."""
    if not user_id:
        return []
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            _ensure_table(cur)
            cur.execute(
                "SELECT * FROM policy_hub_documents WHERE user_id=%s "
                "ORDER BY created_at DESC",
                (user_id,),
            )
            rows = cur.fetchall() or []
    finally:
        conn.close()
    return [_row_to_item(dict(r)) for r in rows]


def get_documents(policy_ids: list[str]) -> dict[str, dict]:
    """Return ``{policy_id: list-item dict}`` for the given ids (one query).

    Powers the shared and workflow-assigned union blocks in ``/policy-hub/list``.
    """
    ids = [p for p in (policy_ids or []) if p]
    if not ids:
        return {}
    placeholders = ",".join(["%s"] * len(ids))
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            _ensure_table(cur)
            cur.execute(
                f"SELECT * FROM policy_hub_documents WHERE policy_id IN ({placeholders})",
                tuple(ids),
            )
            rows = cur.fetchall() or []
    finally:
        conn.close()
    return {r["policy_id"]: _row_to_item(dict(r)) for r in rows}


def scan_policies_from_s3(
    user_id: str,
    *,
    list_fn=None,
    read_fn=None,
    upsert_fn=None,
) -> list[dict]:
    """S3-scan fallback for ``/policy-hub/list`` when the index is empty.

    Reads every YAML under ``{user_id}/policies/`` via decryption and lazily
    populates the metadata index for next time — self-healing without a flag
    day. Deps are injectable so unit tests don't have to import the routes
    blueprint or hit S3.
    """
    if list_fn is None:
        from utils.s3_utils import list_all_files as list_fn  # type: ignore[assignment]
    if read_fn is None:
        from policy_hub.routes import _read_policy_yaml as read_fn  # type: ignore[assignment]
    if upsert_fn is None:
        upsert_fn = upsert_document

    items: list[dict] = []
    for obj in list_fn(folder=f"{user_id}/policies/") or []:
        key = obj.get("Key", "") if isinstance(obj, dict) else str(obj)
        if not key.endswith(".yaml") or "/jobs/" in key:
            continue
        try:
            data = read_fn(user_id, key)
        except Exception as exc:
            logger.warning("scan_policies_from_s3: read failed key=%s: %s", key, exc)
            continue
        if not data:
            continue
        items.append(data)
        try:
            upsert_fn(user_id, data)
        except Exception:
            pass
    return items


def list_document_ids(user_id: str) -> set[str]:
    """Return the set of indexed policy_ids for an owner (used by reconcile)."""
    if not user_id:
        return set()
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            _ensure_table(cur)
            cur.execute(
                "SELECT policy_id FROM policy_hub_documents WHERE user_id=%s",
                (user_id,),
            )
            return {r["policy_id"] for r in (cur.fetchall() or [])}
    finally:
        conn.close()
