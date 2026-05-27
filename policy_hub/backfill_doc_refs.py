"""One-shot backfill: mint a ``doc_ref`` for every policy/procedure/standard
that predates the reference-number feature.

Usage (run from project root, on a host with Secrets Manager access):

    python -m policy_hub.backfill_doc_refs --user-id <uid> [--user-id <uid> ...]
    python -m policy_hub.backfill_doc_refs --all          # every user in `users`
    python -m policy_hub.backfill_doc_refs --all --apply   # actually write

Dry-run by default — pass ``--apply`` to persist. Idempotent: documents that
already carry a ``doc_ref`` are skipped, so re-running never re-mints.
"""

import argparse

from policy_hub.doc_ref import mint_doc_ref
from utils.base_logger import get_logger

logger = get_logger(__name__)


def needs_doc_ref(item: dict) -> bool:
    """True when the document has no usable reference number yet."""
    return not (item or {}).get("doc_ref")


def assign_doc_ref(item: dict, org_id: str, mint_fn=mint_doc_ref) -> dict:
    """Return a copy of *item* with a freshly minted ``doc_ref``.

    Preserves all existing keys and their order so the rewritten YAML stays
    structurally identical apart from the added field.
    """
    doc_type = (item.get("type") or "policy").strip()
    title = item.get("title") or ""
    ref = mint_fn(org_id, doc_type, title)
    updated = dict(item)
    updated["doc_ref"] = ref
    return updated


def backfill_user(
    user_id: str,
    *,
    apply: bool = False,
    mint_fn=mint_doc_ref,
    org_resolver=None,
) -> dict:
    """Scan ``{user_id}/policies/*.yaml``, mint missing refs.

    Returns a summary dict ``{scanned, minted, skipped, errors}``. Imports the
    S3 + org helpers lazily so the module is importable (and unit-testable)
    without AWS credentials.
    """
    from policy_hub.routes import _write_yaml_to_s3
    from utils.s3_utils import list_all_files, load_yaml_from_s3

    if org_resolver is None:
        from workflow_route.state_machine import get_user_org_id
        org_resolver = get_user_org_id

    org_id = org_resolver(user_id)
    summary = {"scanned": 0, "minted": 0, "skipped": 0, "errors": 0}
    if not org_id:
        logger.warning("backfill: user=%s has no resolvable org — skipping", user_id)
        return summary

    prefix = f"{user_id}/policies/"
    for obj in list_all_files(folder=prefix) or []:
        key = obj.get("Key", "")
        if not key.endswith(".yaml") or "/jobs/" in key or "/raw/" in key:
            continue
        summary["scanned"] += 1
        try:
            data = load_yaml_from_s3(key)
            if not data:
                continue
            if not needs_doc_ref(data):
                summary["skipped"] += 1
                continue
            updated = assign_doc_ref(data, org_id, mint_fn=mint_fn)
            if apply:
                _write_yaml_to_s3(key, updated)
            summary["minted"] += 1
            logger.info(
                "backfill: %s key=%s -> doc_ref=%s%s",
                "minted" if apply else "would mint",
                key, updated["doc_ref"], "" if apply else " (dry-run)",
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
    parser = argparse.ArgumentParser(description="Backfill policy_hub doc_ref values")
    parser.add_argument("--user-id", action="append", default=[], help="user_id to process (repeatable)")
    parser.add_argument("--all", action="store_true", help="process every user in the users table")
    parser.add_argument("--apply", action="store_true", help="persist changes (default: dry-run)")
    args = parser.parse_args(argv)

    user_ids = list(args.user_id)
    if args.all:
        user_ids = _all_user_ids()
    if not user_ids:
        parser.error("supply --user-id <uid> (repeatable) or --all")

    totals = {"scanned": 0, "minted": 0, "skipped": 0, "errors": 0}
    for uid in user_ids:
        s = backfill_user(uid, apply=args.apply)
        for k in totals:
            totals[k] += s[k]

    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(
        f"[{mode}] users={len(user_ids)} scanned={totals['scanned']} "
        f"minted={totals['minted']} skipped={totals['skipped']} errors={totals['errors']}"
    )


if __name__ == "__main__":
    main()
