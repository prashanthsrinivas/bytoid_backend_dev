"""Manually enqueue create_playbook_runbook_task for a stuck playbook.

Useful when a runbook never generated a report because the questionnaire was
completed under the old (pre-fix) race-conditioned code path, or to retry a
known-failed report without re-answering questions.

Two modes:

1. Direct (you already know the filename + runbook_id):
   python3 manual_trigger_runbook.py <user_id> <playbook_filename> <runbook_id>

2. Lookup (you only know the playbook name and/or runbook name):
   python3 manual_trigger_runbook.py --list <user_id>
       prints all playbook files + linked runbook_id under that user's S3 prefix

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
    """Print every playbook config file under the user's workflow prefix,
    with the runbook_id it is linked to (if any)."""
    from utils.s3_utils import read_json_from_s3, s3bucket, S3_BUCKET

    prefix = f"{user_id}/workflow/"
    s3 = s3bucket()  # returns a boto3 s3 client
    paginator = s3.get_paginator("list_objects_v2")

    print(f"{'PLAYBOOK FILE':<60} {'RUNBOOK_ID':<40} TITLE")
    print("-" * 130)
    found = 0
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".json"):
                continue
            filename = key.rsplit("/", 1)[-1]
            # Only list root-level workflow config files (skip execution-id
            # snapshots and per-step artifacts that share the .json suffix).
            # Path shape: {user}/workflow/{basename}/{filename}.json — keep
            # entries where the parent folder basename matches the filename
            # stem (i.e. the canonical playbook file).
            parts = key.split("/")
            if len(parts) != 4:
                continue
            stem = filename[:-5]  # strip .json
            if not stem.startswith(parts[2]):
                continue
            try:
                data = read_json_from_s3(key) or {}
            except Exception as e:
                print(f"{filename:<60} <read error: {e}>")
                continue
            runbook_id = data.get("runbook_id") or "(none)"
            title = (
                (data.get("workflow") or {}).get("title")
                or data.get("title")
                or data.get("name")
                or ""
            )
            print(f"{filename:<60} {runbook_id:<40} {title}")
            found += 1
    if not found:
        print(f"No playbook .json files found under prefix s3://{S3_BUCKET}/{prefix}")
    return 0


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--list":
        return _list_playbooks(sys.argv[2])

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
