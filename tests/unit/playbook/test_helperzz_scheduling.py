"""§4e — helperzz scheduling / runbook-seam functions.

``assign_runbook_playbook`` (stamp runbook_id + persist) and
``update_playbook_schedule_and_runtime`` (update base workflow + playbooksconfig).
S3 I/O is mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.workflow_playbook import _wf_pb_stubs as stubs

stubs.bootstrap_sut()

import playbook.helperzz as h  # noqa: E402

pytestmark = pytest.mark.unit


def test_assign_runbook_playbook_stamps_and_saves():
    with patch.object(h, "read_json_from_s3", return_value={}), \
         patch.object(h, "save_playbook_to_s3", return_value="saved") as save:
        out = h.assign_runbook_playbook("rb-1", "f.json", "u1")
    assert out == "saved"
    saved_workflow = save.call_args.args[0]
    assert saved_workflow["runbook_id"] == "rb-1"


def test_update_playbook_schedule_creates_entry_and_persists():
    with patch.object(h, "returnconfigandpath", return_value=("pb", "cfg/path", "sub")), \
         patch.object(h, "read_json_from_s3", return_value={}), \
         patch.object(h, "upload_any_file", MagicMock()) as upload:
        out = h.update_playbook_schedule_and_runtime("u1", "f.json", status="Running")
    assert out is True
    assert upload.called          # playbooksconfig.json persisted
