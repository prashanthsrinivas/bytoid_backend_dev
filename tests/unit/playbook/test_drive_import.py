"""Phase 6 — Google Drive / OneDrive import into the auto-fill pipeline.

Covers login-provider detection, Drive/OneDrive listing normalization + the
OneDrive needs_reconsent path, and that the import job stages files to S3 and
feeds the SAME ``answer_ques_file_bk`` pipeline as a device upload.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock, patch

import pytest

from tests.workflow_playbook import _wf_pb_stubs as stubs

stubs.bootstrap_sut()

_rs = sys.modules.get("services.redis_service")
if _rs is not None and not hasattr(_rs, "RedisService"):
    _rs.RedisService = MagicMock(name="RedisService")

import playbook.drive_import as di  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture
def app():
    return stubs.make_app(di.drive_import_bp)


# ── provider detection ─────────────────────────────────────────────────────────

def test_providers_google(app):
    with stubs.allow_auth(), \
         patch.object(di, "_fetch_user_auth", return_value=("google", "tok", "ref", "exp")):
        resp = app.test_client().get("/workflow/import/providers?user_id=u1")
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["provider"] == "google"
    assert body["google_connected"] is True
    assert body["microsoft_connected"] is False


def test_providers_microsoft(app):
    with stubs.allow_auth(), \
         patch.object(di, "_fetch_user_auth", return_value=("microsoft", "tok", "r", "e")):
        resp = app.test_client().get("/workflow/import/providers?user_id=u1")
    assert resp.get_json()["provider"] == "microsoft"


def test_providers_none_for_password_user(app):
    with stubs.allow_auth(), \
         patch.object(di, "_fetch_user_auth", return_value=(None, None, None, None)):
        resp = app.test_client().get("/workflow/import/providers?user_id=u1")
    assert resp.get_json()["provider"] == "none"


def test_providers_user_not_found(app):
    with stubs.allow_auth(), patch.object(di, "_fetch_user_auth", return_value=None):
        resp = app.test_client().get("/workflow/import/providers?user_id=u1")
    assert resp.status_code == 404


# ── listing ────────────────────────────────────────────────────────────────────

def test_list_google_normalizes_entries(app):
    fake_service = MagicMock()
    fake_service.files.return_value.list.return_value.execute.return_value = {
        "files": [
            {"id": "f1", "name": "folder", "mimeType": "application/vnd.google-apps.folder"},
            {"id": "f2", "name": "policy.pdf", "mimeType": "application/pdf", "size": "100"},
            {"id": "f3", "name": "doc", "mimeType": "application/vnd.google-apps.document"},
            {"id": "f4", "name": "skip.exe", "mimeType": "application/octet-stream"},
        ]
    }
    with stubs.allow_auth(), \
         patch.object(di, "_fetch_user_auth", return_value=("google", "tok", "r", "e")), \
         patch("agent_route.Drive_downloader.GetEmailandDriveService", return_value=fake_service):
        resp = app.test_client().get("/workflow/import/drive/list?user_id=u1&provider=google")
    entries = resp.get_json()["entries"]
    names = {e["name"] for e in entries}
    assert "folder" in names and "policy.pdf" in names and "doc" in names
    assert "skip.exe" not in names  # disallowed extension filtered
    folder = next(e for e in entries if e["name"] == "folder")
    assert folder["is_folder"] is True


def test_list_onedrive_normalizes_entries(app):
    graph_resp = MagicMock(status_code=200)
    graph_resp.json.return_value = {
        "value": [
            {"id": "1", "name": "Docs", "folder": {"childCount": 2}},
            {"id": "2", "name": "report.docx", "file": {"mimeType": "application/msword"},
             "size": 50, "lastModifiedDateTime": "2026-06-01T00:00:00Z"},
            {"id": "3", "name": "bad.bin", "file": {"mimeType": "x"}},
        ]
    }
    with stubs.allow_auth(), \
         patch.object(di, "_fetch_user_auth", return_value=("microsoft", "tok", "r", "e")), \
         patch.object(di, "requests") as req:
        req.get.return_value = graph_resp
        resp = app.test_client().get("/workflow/import/drive/list?user_id=u1&provider=onedrive")
    entries = resp.get_json()["entries"]
    names = {e["name"] for e in entries}
    assert "Docs" in names and "report.docx" in names
    assert "bad.bin" not in names
    assert next(e for e in entries if e["name"] == "Docs")["is_folder"] is True


def test_list_onedrive_needs_reconsent(app):
    graph_resp = MagicMock(status_code=403)
    with stubs.allow_auth(), \
         patch.object(di, "_fetch_user_auth", return_value=("microsoft", "tok", "r", "e")), \
         patch.object(di, "requests") as req:
        req.get.return_value = graph_resp
        resp = app.test_client().get("/workflow/import/drive/list?user_id=u1&provider=onedrive")
    assert resp.status_code == 403
    assert resp.get_json()["needs_reconsent"] is True


def test_list_invalid_provider(app):
    with stubs.allow_auth():
        resp = app.test_client().get("/workflow/import/drive/list?user_id=u1&provider=dropbox")
    assert resp.status_code == 400


def test_list_login_required_when_no_token(app):
    with stubs.allow_auth(), \
         patch.object(di, "_fetch_user_auth", return_value=("google", None, "r", "e")):
        resp = app.test_client().get("/workflow/import/drive/list?user_id=u1&provider=google")
    assert resp.status_code == 401


# ── import endpoint ─────────────────────────────────────────────────────────

def test_import_endpoint_queues_job(app):
    with stubs.allow_auth(), \
         patch.object(di.JobManager, "submit_job", MagicMock(return_value="coro")), \
         patch.object(di, "_run_drive_async", return_value="job-1"):
        resp = app.test_client().post("/workflow/import/drive", json={
            "user_id": "u1", "wf_name": "wf", "step_id": "s1", "provider": "google",
            "items": [{"id": "f1", "name": "p.pdf", "is_folder": False}]})
    assert resp.status_code == 200
    assert resp.get_json()["job_id"] == "job-1"


def test_import_endpoint_requires_items(app):
    with stubs.allow_auth():
        resp = app.test_client().post("/workflow/import/drive", json={
            "user_id": "u1", "wf_name": "wf", "provider": "google", "items": []})
    assert resp.status_code == 400


def test_import_endpoint_rejects_bad_provider(app):
    with stubs.allow_auth():
        resp = app.test_client().post("/workflow/import/drive", json={
            "user_id": "u1", "wf_name": "wf", "provider": "dropbox",
            "items": [{"id": "1", "name": "a.pdf"}]})
    assert resp.status_code == 400


# ── import job → auto-fill parity ───────────────────────────────────────────

def test_import_job_feeds_answer_ques_file_bk():
    """Downloads → S3 upload → SAME extraction + answer_ques_file_bk pipeline."""
    captured = {}

    def fake_extract(file_keys):
        captured["file_keys"] = list(file_keys)
        return ([{"content": "x", "s3_key": file_keys[0]}], [], [], [])

    class FakeRunner:
        def __init__(self, **kw):
            captured["runner_kwargs"] = kw

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        async def answer_ques_file_bk(self, extracted, step_id, file_keys, inp_links, inp_link_keys):
            captured["answer_called"] = True
            captured["answer_file_keys"] = file_keys
            return {"status": "success", "answered_now": 2}

    fake_s3 = MagicMock()
    with patch.object(di, "_fetch_user_auth", return_value=("google", "tok", "r", "e")), \
         patch.object(di, "_download_google_item", return_value="downloaded_p.pdf"), \
         patch.object(di, "s3bucket", return_value=fake_s3), \
         patch("playbook.routes._extract_autofill_payload", side_effect=fake_extract), \
         patch("services.workflow_service.WorkflowRunnerV2", FakeRunner), \
         patch("credits_route.route.Credits", MagicMock()), \
         patch("os.path.exists", return_value=False):
        result = asyncio.run(di.import_drive_files_bk(
            "u1", "wf", "s1", "google",
            [{"id": "f1", "name": "p.pdf", "is_folder": False}]))

    assert result["status"] == "success"
    assert captured["answer_called"] is True
    # The S3 key staged from the download is what flows into the pipeline.
    assert captured["file_keys"] == captured["answer_file_keys"]
    assert captured["file_keys"][0].startswith("u1/uploads/")
    fake_s3.upload_file.assert_called_once()


def test_import_job_skips_folders_and_disallowed():
    fake_s3 = MagicMock()
    with patch.object(di, "_fetch_user_auth", return_value=("microsoft", "tok", "r", "e")), \
         patch.object(di, "_download_onedrive_item", return_value="downloaded_a.pdf"), \
         patch.object(di, "s3bucket", return_value=fake_s3):
        result = asyncio.run(di.import_drive_files_bk(
            "u1", "wf", "s1", "onedrive",
            [{"id": "d", "name": "folder", "is_folder": True},
             {"id": "x", "name": "malware.exe", "is_folder": False}]))
    # Nothing importable → no S3 upload, error result.
    assert result["status"] == "error"
    fake_s3.upload_file.assert_not_called()
