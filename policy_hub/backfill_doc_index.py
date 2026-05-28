"""One-shot backfill: populate ``policy_hub_documents`` from existing S3 YAMLs.

Usage (run from project root, on a host with Secrets Manager access):

    python -m policy_hub.backfill_doc_index --user-id <uid> [--user-id <uid> ...]
    python -m policy_hub.backfill_doc_index --all          # every user in `users`
    python -m policy_hub.backfill_doc_index --all --apply   # actually write

Dry-run by default — pass ``--apply`` to persist. Idempotent: re-running just
re-upserts the same row. After backfill, ``/policy-hub/list`` serves from the
index instead of one S3 GET per document.
"""

import argparse

from utils.base_logger import get_logger

logger = get_logger(__name__)


def backfill_user(
    user_id: str,
    *,
    apply: bool = False,
    upsert_fn=None,
    read_fn=None,
) -> dict:
    """Scan ``{user_id}/policies/*.yaml``, upsert each into the index.

    Reads with decryption so the index stores a freshly-encrypted ``title``
    under the current user key — same at-rest posture as the S3 blob.

    Returns ``{scanned, upserted, skipped, errors}``. Imports the S3 + index
    helpers lazily so the module stays importable without AWS credentials.
    """
    from utils.s3_utils import list_all_files

    if upsert_fn is None:
        from policy_hub.doc_index import upsert_document as upsert_fn  # type: ignore[assignment]
    if read_fn is None:
        from policy_hub.routes import _read_policy_yaml as read_fn  # type: ignore[assignment]

    summary = {"scanned": 0, "upserted": 0, "skipped": 0, "errors": 0}

    prefix = f"{user_id}/policies/"
    for obj in list_all_files(folder=prefix) or []:
        key = obj.get("Key", "")
        if not key.endswith(".yaml") or "/jobs/" in key or "/raw/" in key:
            continue
        summary["scanned"] += 1
        try:
            data = read_fn(user_id, key)
            if not data or not data.get("policy_id"):
                summary["skipped"] += 1
                continue
            if apply:
                upsert_fn(user_id, data)
            summary["upserted"] += 1
            logger.info(
                "backfill: %s key=%s policy_id=%s%s",
                "upserted" if apply else "would upsert",
                key, data.get("policy_id"), "" if apply else " (dry-run)",
            )
        except Exception as exc:
            summary["errors"] += 1
            logger.error("backfill: failed for key=%s: %s", key, exc)

    return summary


def _all_user_ids() -> list[str]:
    import pymysql.cursors

    from db.rds_db import connect_to_rds

    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT user_id FROM users")
            return [r["user_id"] for r in cur.fetchall() if r.get("user_id")]
    finally:
        conn.close()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Backfill policy_hub_documents from S3 YAMLs")
    parser.add_argument("--user-id", action="append", default=[], help="user_id to process (repeatable)")
    parser.add_argument("--all", action="store_true", help="process every user in the users table")
    parser.add_argument("--apply", action="store_true", help="persist changes (default: dry-run)")
    args = parser.parse_args(argv)

    user_ids = list(args.user_id)
    if args.all:
        user_ids = _all_user_ids()
    if not user_ids:
        parser.error("supply --user-id <uid> (repeatable) or --all")

    totals = {"scanned": 0, "upserted": 0, "skipped": 0, "errors": 0}
    for uid in user_ids:
        s = backfill_user(uid, apply=args.apply)
        for k in totals:
            totals[k] += s[k]

    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(
        f"[{mode}] users={len(user_ids)} scanned={totals['scanned']} "
        f"upserted={totals['upserted']} skipped={totals['skipped']} errors={totals['errors']}"
    )


if __name__ == "__main__":
    main()
