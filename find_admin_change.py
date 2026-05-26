"""
Find who removed admin rights from a user, and who their current owning admin is.

Usage on EC2 (run from the same directory as app.py, where .env / venv are set up):

    python find_admin_change.py service@bytoid.ca
    python find_admin_change.py service@bytoid.ca --days 365
    python find_admin_change.py service@bytoid.ca --full-scan

Notes:
- "Current admin" = the row in `users` resolved via get_billing_user_id().
  If the target's user_type is still 'admin', they have no owning admin.
- "Who demoted them" = scans audit JSON in S3 for USER_TYPE_CHANGED events
  whose metadata.target_user_id matches the target. By default scans the
  target's own audit prefix; pass --full-scan to also scan every other
  user's audit prefix (slower; needed if the demoter wrote the entry only
  to their own workspace).
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

import pymysql

from db.rds_db import connect_to_rds
from db.db_checkers import get_billing_user_id, get_email_by_id
from utils.s3_utils import s3bucket, S3_BUCKET


def lookup_user(conn, email):
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(
            "SELECT user_id, user_type, email, permissions, launch_id_fk, "
            "created_in, updated_in "
            "FROM users WHERE email=%s",
            (email,),
        )
        return cur.fetchall()


def current_admin(conn, user_row):
    """Return (admin_user_id, admin_email, note) for the user's owning admin."""
    if user_row["user_type"] == "admin":
        return user_row["user_id"], user_row["email"], "target is themselves an admin"

    billing_id = get_billing_user_id(user_row["user_id"], conn=conn)
    if billing_id == user_row["user_id"]:
        return None, None, "no owning admin resolved (no invited_by, no launch_id_fk)"

    admin_email = get_email_by_id(billing_id, connection=conn)
    return billing_id, admin_email, "resolved via get_billing_user_id()"


def list_audit_dates(s3, prefix):
    """List all YYYY-MM-DD.json keys under a {user_id}/audit/ prefix."""
    keys = []
    token = None
    while True:
        kwargs = {"Bucket": S3_BUCKET, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            if obj["Key"].endswith(".json"):
                keys.append(obj["Key"])
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    return keys


def load_audit_file(s3, key):
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
        data = json.loads(obj["Body"].read())
        if isinstance(data, list):
            return data
        return []
    except Exception as e:
        print(f"  WARN: could not read {key}: {e}", file=sys.stderr)
        return []


def scan_prefix_for_target(s3, prefix, target_user_id, since_date):
    """Yield matching USER_TYPE_CHANGED entries under a single audit prefix."""
    for key in list_audit_dates(s3, prefix):
        date_part = key.rsplit("/", 1)[-1].removesuffix(".json")
        if since_date and date_part < since_date:
            continue
        for entry in load_audit_file(s3, key):
            if entry.get("action") != "USER_TYPE_CHANGED":
                continue
            meta = entry.get("metadata") or {}
            if meta.get("target_user_id") == target_user_id:
                yield key, entry


def list_top_level_audit_prefixes(s3):
    """List every {user_id}/ prefix that has an audit/ folder."""
    user_prefixes = set()
    token = None
    while True:
        kwargs = {"Bucket": S3_BUCKET, "Delimiter": "/"}
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kwargs)
        for cp in resp.get("CommonPrefixes", []):
            user_prefixes.add(cp["Prefix"])
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    return sorted(user_prefixes)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("email", nargs="?", default="service@bytoid.ca")
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Only scan audit files dated within the last N days (default: all dates)",
    )
    parser.add_argument(
        "--full-scan",
        action="store_true",
        help="Scan every user prefix in S3 for the event, not just the target's own. "
        "Use this if the demotion was written to the actor's workspace, not the target's.",
    )
    args = parser.parse_args()

    if not S3_BUCKET:
        print("ERROR: S3_BUCKET env var is not set. Aborting.", file=sys.stderr)
        sys.exit(1)

    since_date = None
    if args.days:
        since_date = (datetime.now(timezone.utc) - timedelta(days=args.days)).date().isoformat()

    print(f"\n=== Looking up {args.email} ===")
    conn = connect_to_rds()
    if conn is None:
        print("ERROR: could not get a DB connection.", file=sys.stderr)
        sys.exit(1)

    rows = lookup_user(conn, args.email)
    if not rows:
        print(f"No user found with email = {args.email}")
        conn.close()
        sys.exit(2)

    if len(rows) > 1:
        print(f"WARNING: {len(rows)} rows match {args.email}. Showing each.\n")

    for row in rows:
        print("--- users row ---")
        print(f"  user_id     : {row['user_id']}")
        print(f"  user_type   : {row['user_type']}")
        print(f"  email       : {row['email']}")
        print(f"  created_in  : {row['created_in']}")
        print(f"  updated_in  : {row['updated_in']}  <-- last row update")
        print(f"  launch_id_fk: {row['launch_id_fk']!r}")
        try:
            perms = json.loads(row["permissions"]) if row["permissions"] else {}
            invited_by = perms.get("invited_by") if isinstance(perms, dict) else None
        except Exception:
            invited_by = None
        print(f"  invited_by  : {invited_by!r}")

        print("\n--- current admin ---")
        admin_id, admin_email, note = current_admin(conn, row)
        print(f"  admin_user_id: {admin_id}")
        print(f"  admin_email  : {admin_email}")
        print(f"  note         : {note}")

        target_user_id = row["user_id"]

        print(
            f"\n--- audit scan for USER_TYPE_CHANGED on target_user_id={target_user_id} ---"
        )
        if since_date:
            print(f"  date filter: >= {since_date}")
        s3 = s3bucket()

        hits = []
        own_prefix = f"{target_user_id}/audit/"
        print(f"  scanning {own_prefix} ...")
        for key, entry in scan_prefix_for_target(s3, own_prefix, target_user_id, since_date):
            hits.append((key, entry))

        if args.full_scan:
            print("  full-scan: enumerating every */audit/ prefix ...")
            for top in list_top_level_audit_prefixes(s3):
                prefix = f"{top}audit/"
                if prefix == own_prefix:
                    continue
                for key, entry in scan_prefix_for_target(s3, prefix, target_user_id, since_date):
                    hits.append((key, entry))

        if not hits:
            print(
                "  No USER_TYPE_CHANGED events found for this target.\n"
                "  If the demotion happened before audit logging was added, or was done\n"
                "  via direct SQL, no event will exist. Try --full-scan if you only\n"
                "  scanned the target's own prefix."
            )
        else:
            hits.sort(key=lambda kv: kv[1].get("timestamp", ""))
            print(f"  Found {len(hits)} matching event(s):\n")
            for key, e in hits:
                meta = e.get("metadata") or {}
                print(f"  - {e.get('timestamp')}  [{key}]")
                print(f"      new_user_type : {meta.get('new_user_type')}")
                print(f"      actor_user_id : {e.get('actor_user_id')}")
                print(f"      actor_email   : {e.get('actor_email')}")
                print(f"      endpoint      : {e.get('endpoint')}")
                print(f"      ip            : {e.get('ip')}")
                print(f"      status        : {e.get('status')}")
                print()

    conn.close()


if __name__ == "__main__":
    main()
