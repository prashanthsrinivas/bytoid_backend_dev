"""Manually enqueue create_playbook_runbook_task for a stuck playbook.

Useful when a runbook never generated a report because the questionnaire was
completed under the old (pre-fix) race-conditioned code path, or to retry a
known-failed report without re-answering questions.

Four modes:

1. Direct (you already know the filename + runbook_id):
   python3 manual_trigger_runbook.py <user_id> <playbook_filename> <runbook_id>

2. Playbook lookup (you only know the playbook name and/or runbook name):
   python3 manual_trigger_runbook.py --list <user_id>
       prints all playbook config files + linked runbook_id under the user's
       S3 workflow prefix.

3. Runbook lookup (check which runbook IDs actually exist in LanceDB):
   python3 manual_trigger_runbook.py --list-runbooks <user_id>
       prints every runbook record stored in LanceDB for this user, so you
       can detect stale runbook_id references in playbook configs.

4. Relink (fix a stale runbook_id pointer in a playbook config):
   python3 manual_trigger_runbook.py --relink <user_id> <playbook_filename> <new_runbook_id>
       updates the playbook's S3 JSON to point at <new_runbook_id> so future
       completed submissions trigger the right runbook automatically.

Example direct:
   python3 manual_trigger_runbook.py 109161866299858012556 \\
       config_playbook_a25c9a5e.json runbook_68eab4

Notes:
- playbook_filename ends in .json (e.g. "config_playbook_a25c9a5e.json").
- runbook_id is shown in the runbook URL (/runbook/<runbook_id>).
- The Celery worker must be running. Watch its journal:
      sudo journalctl -u celery.service -f
"""
import sys

from utils.celery_base import create_playbook_runbook_task


def _list_playbooks(user_id: str) -> int:
    """Print every canonical playbook config file under the user's workflow
    prefix, with the runbook_id it is linked to (if any) and the playbook
    title. Execution snapshots (`<basename>_ch_*.json`) are filtered out."""
    import logging
    from utils.s3_utils import read_json_from_s3, s3bucket, S3_BUCKET

    # Quiet the per-read S3 logger so the listing isn't drowned in noise.
    logging.getLogger("utils.s3_utils").setLevel(logging.WARNING)

    prefix = f"{user_id}/workflow/"
    s3 = s3bucket()
    paginator = s3.get_paginator("list_objects_v2")

    print(f"{'PLAYBOOK FILE':<32} {'RUNBOOK_ID':<32} TITLE")
    print("-" * 120)
    found = 0
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".json"):
                continue
            parts = key.split("/")
            # Canonical playbook path is {user}/workflow/{basename}/{basename}.json
            # (exactly 4 segments, filename stem == folder basename).
            if len(parts) != 4:
                continue
            filename = parts[3]
            stem = filename[:-5]  # strip .json
            if stem != parts[2]:
                continue
            try:
                data = read_json_from_s3(key) or {}
            except Exception as e:
                print(f"{filename:<32} <read error: {e}>")
                continue
            runbook_id = data.get("runbook_id") or "(none)"
            title = (
                (data.get("input_data") or {}).get("title")
                or (data.get("workflow") or {}).get("name")
                or (data.get("workflow") or {}).get("title")
                or data.get("title")
                or data.get("name")
                or ""
            )
            print(f"{filename:<32} {runbook_id:<32} {title}")
            found += 1
    if not found:
        print(f"No playbook .json files found under prefix s3://{S3_BUCKET}/{prefix}")
    return 0


def _list_runbooks(user_id: str) -> int:
    """Print every runbook record in LanceDB for this user — raw, unfiltered.
    Useful for detecting stale runbook_id references in playbook configs."""
    import asyncio
    import logging
    from db.lance_db_service import LanceDBServer

    logging.getLogger("utils.s3_utils").setLevel(logging.WARNING)

    async def _go():
        dbserver = LanceDBServer()
        return await dbserver.get_user_runbook(user_id)

    runbooks = asyncio.run(_go())
    if not runbooks:
        print(f"No runbooks found in LanceDB for user {user_id}")
        return 0

    print(f"{'RUNBOOK_ID':<32} {'CREATED':<25} NAME")
    print("-" * 120)
    for rb in runbooks:
        rid = rb.get("runbook_id") or "(none)"
        name = rb.get("name") or rb.get("runbook_name") or ""
        created = str(rb.get("created_at") or rb.get("createdAt") or "")[:24]
        print(f"{rid:<32} {created:<25} {name}")
    return 0


def _relink_playbook(user_id: str, playbook_filename: str, new_runbook_id: str) -> int:
    """Rewrite a playbook config's `runbook_id` field in S3."""
    import json
    import logging
    from utils.s3_utils import read_json_from_s3, s3bucket, S3_BUCKET

    logging.getLogger("utils.s3_utils").setLevel(logging.WARNING)

    if not playbook_filename.endswith(".json"):
        print(f"ERROR: playbook_filename must end with .json (got {playbook_filename!r})")
        return 2

    stem = playbook_filename[:-5]
    s3_key = f"{user_id}/workflow/{stem}/{playbook_filename}"

    data = read_json_from_s3(s3_key)
    if not data:
        print(f"ERROR: playbook config not found at s3://{S3_BUCKET}/{s3_key}")
        return 3

    old_runbook_id = data.get("runbook_id")
    if old_runbook_id == new_runbook_id:
        print(f"No change needed — runbook_id already {new_runbook_id!r}")
        return 0

    data["runbook_id"] = new_runbook_id
    s3 = s3bucket()
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=s3_key,
        Body=json.dumps(data, default=str),
        ContentType="application/json",
    )
    print(
        f"Relinked playbook config:\n"
        f"  s3://{S3_BUCKET}/{s3_key}\n"
        f"  runbook_id: {old_runbook_id!r} -> {new_runbook_id!r}\n"
        f"\nFuture completed submissions will now trigger {new_runbook_id}."
    )
    return 0


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--list":
        return _list_playbooks(sys.argv[2])
    if len(sys.argv) == 3 and sys.argv[1] == "--list-runbooks":
        return _list_runbooks(sys.argv[2])
    if len(sys.argv) == 5 and sys.argv[1] == "--relink":
        return _relink_playbook(sys.argv[2], sys.argv[3], sys.argv[4])

    if len(sys.argv) != 4:
        print(__doc__)
        return 1

    user_id, playbook_filename, runbook_id = sys.argv[1:4]

    if not playbook_filename.endswith(".json"):
        print(f"ERROR: playbook_filename must end with .json (got {playbook_filename!r})")
        return 2

    res = create_playbook_runbook_task.delay(user_id, playbook_filename, runbook_id)
    print(
        f"Enqueued task id={res.id}\n"
        f"  user_id={user_id}\n"
        f"  playbook_filename={playbook_filename}\n"
        f"  runbook_id={runbook_id}\n"
        f"\nWatch the Celery worker journal for:\n"
        f"  🔥 PLAYBOOK RUNBOOK TASK STARTED ...\n"
        f"  trigger_runbook_from_playbook started\n"
        f"  Runbook execution finished: ..."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
