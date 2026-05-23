"""Manually enqueue create_playbook_runbook_task for a stuck playbook.

Useful when a runbook never generated a report because the questionnaire was
completed under the old (pre-fix) race-conditioned code path, or to retry a
known-failed report without re-answering questions.

Usage:
  python3 manual_trigger_runbook.py <user_id> <playbook_filename> <runbook_id>

Example (Privacy Risk Assessment):
  python3 manual_trigger_runbook.py \
      109161866299858012556 \
      config_playbook_a25c9a5e.json \
      runbook_68eab4

Notes:
- playbook_filename matches the S3 key under <user_id>/workflow/<basename>/
  (e.g. "config_playbook_a25c9a5e.json"). The .json suffix is required.
- runbook_id is the id shown in the URL of the runbook results page
  (e.g. /runbook/results/runbook_68eab4 → "runbook_68eab4").
- The Celery worker must be running for the task to actually execute.
  Watch its journal: sudo journalctl -u <celery-unit> -f
"""
import sys

from utils.celery_base import create_playbook_runbook_task


def main() -> int:
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
