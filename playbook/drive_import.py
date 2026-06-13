"""Import files from a user's Google Drive or OneDrive into the workflow
auto-fill pipeline.

The user is offered Drive or OneDrive based on how they logged in
(``users.social``). Selected files are downloaded server-side using the
already-stored OAuth tokens, uploaded to S3 under ``{user_id}/uploads/...``
(the same scheme as /make_s3upload), then pushed through the SAME extraction +
``answer_ques_file_bk`` path as a device upload — so response-policy and
single-type classification enforcement apply automatically.
"""

from __future__ import annotations

import logging
import os
import uuid

import requests
from flask import Blueprint, jsonify, request

from utils.normal import parse_composite_user_id
from utils.permission_required import permission_required_body
from utils.s3_utils import s3bucket, S3_BUCKET
from db.rds_db import connect_to_rds
from playbook.background_worker import JobManager

drive_import_bp = Blueprint("drive_import", __name__)
logger = logging.getLogger(__name__)

# Mirror the device-upload allowlist (kept local to avoid a circular import).
ALLOWED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".txt", ".xlsx", ".xls", ".csv", ".pptx", ".ppt",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".json",
}

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

_GOOGLE_DOC_EXPORT = {
    "application/vnd.google-apps.document": (".pdf", "application/pdf"),
    "application/vnd.google-apps.spreadsheet": (
        ".xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    "application/vnd.google-apps.presentation": (".pdf", "application/pdf"),
}


def _ext_allowed(name: str) -> bool:
    return os.path.splitext(name or "")[1].lower() in ALLOWED_EXTENSIONS


def _fetch_user_auth(user_id: str):
    """Return (social, token, refresh_token, expiry) for the user, or None."""
    conn = connect_to_rds()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT social, token, refresh_token, expiry FROM users WHERE user_id=%s",
                (str(user_id),),
            )
            row = cur.fetchone()
    finally:
        try:
            conn.close()
        except Exception:  # noqa: S110
            pass
    if not row:
        return None
    # Tolerate dict- or tuple-style cursors.
    if isinstance(row, dict):
        return (
            row.get("social"),
            row.get("token"),
            row.get("refresh_token"),
            row.get("expiry"),
        )
    return (row[0], row[1], row[2], row[3])


# ── provider / login detection ────────────────────────────────────────────────

@drive_import_bp.route("/workflow/import/providers", methods=["GET"])
@permission_required_body("workflow.process.view")
def import_providers():
    """Report the user's login provider so the UI offers the right drive."""
    try:
        user_id = request.args.get("user_id")
        if not user_id:
            return jsonify({"status": "error", "message": "user_id required"}), 400
        _, user_id = parse_composite_user_id(user_id)
        auth = _fetch_user_auth(user_id)
        if not auth:
            return jsonify({"status": "error", "message": "user not found"}), 404
        social, token, _refresh, _expiry = auth
        social = (social or "").lower()
        provider = social if social in ("google", "microsoft") else "none"
        return jsonify(
            {
                "status": "success",
                "provider": provider,
                "google_connected": social == "google" and bool(token),
                "microsoft_connected": social == "microsoft" and bool(token),
            }
        )
    except Exception as e:
        logger.error("import_providers failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


# ── drive listing ────────────────────────────────────────────────────────────

def _list_google(access_token, folder_id):
    from agent_route.Drive_downloader import GetEmailandDriveService

    service = GetEmailandDriveService(access_token)
    if not service:
        return None  # signals auth failure to the caller
    parent = folder_id or "root"
    query = f"'{parent}' in parents and trashed = false"
    res = (
        service.files()
        .list(
            q=query,
            fields="files(id, name, mimeType, size, modifiedTime)",
            pageSize=200,
        )
        .execute()
    )
    entries = []
    for f in res.get("files", []):
        mime = f.get("mimeType", "")
        is_folder = mime == "application/vnd.google-apps.folder"
        name = f.get("name", "")
        # Google-native docs are exportable; flag them as importable too.
        exportable = mime in _GOOGLE_DOC_EXPORT
        if not is_folder and not exportable and not _ext_allowed(name):
            continue
        entries.append(
            {
                "id": f.get("id"),
                "name": name,
                "is_folder": is_folder,
                "mime_type": mime,
                "size": f.get("size"),
                "modified": f.get("modifiedTime"),
            }
        )
    return entries


def _list_onedrive(access_token, folder_id):
    if folder_id:
        url = f"{GRAPH_BASE}/me/drive/items/{folder_id}/children"
    else:
        url = f"{GRAPH_BASE}/me/drive/root/children"
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        params={"$select": "id,name,size,file,folder,lastModifiedDateTime"},
        timeout=30,
    )
    if resp.status_code == 403:
        return "needs_reconsent"
    if resp.status_code == 401:
        return None
    resp.raise_for_status()
    entries = []
    for item in resp.json().get("value", []):
        is_folder = "folder" in item
        name = item.get("name", "")
        if not is_folder and not _ext_allowed(name):
            continue
        entries.append(
            {
                "id": item.get("id"),
                "name": name,
                "is_folder": is_folder,
                "mime_type": (item.get("file") or {}).get("mimeType", ""),
                "size": item.get("size"),
                "modified": item.get("lastModifiedDateTime"),
            }
        )
    return entries


@drive_import_bp.route("/workflow/import/drive/list", methods=["GET"])
@permission_required_body("workflow.process.view")
def import_drive_list():
    try:
        user_id = request.args.get("user_id")
        provider = (request.args.get("provider") or "").lower()
        folder_id = request.args.get("folder_id")
        if not user_id or provider not in ("google", "onedrive"):
            return (
                jsonify({"status": "error", "message": "user_id and a valid provider required"}),
                400,
            )
        _, user_id = parse_composite_user_id(user_id)
        auth = _fetch_user_auth(user_id)
        if not auth:
            return jsonify({"status": "error", "message": "user not found"}), 404
        _social, token, _refresh, _expiry = auth
        if not token:
            return jsonify({"status": "error", "message": "login_required"}), 401

        entries = (
            _list_google(token, folder_id)
            if provider == "google"
            else _list_onedrive(token, folder_id)
        )
        if entries == "needs_reconsent":
            return (
                jsonify({"status": "error", "needs_reconsent": True,
                         "message": "Reconnect your Microsoft account to grant file access."}),
                403,
            )
        if entries is None:
            return jsonify({"status": "error", "message": "login_required"}), 401
        return jsonify({"status": "success", "entries": entries})
    except Exception as e:
        logger.error("import_drive_list failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


# ── import → auto-fill ────────────────────────────────────────────────────────

def _download_google_item(access_token, item, dest_dir):
    """Download one Google Drive file (exporting native docs). Returns local path."""
    import io
    from googleapiclient.http import MediaIoBaseDownload
    from agent_route.Drive_downloader import GetEmailandDriveService

    service = GetEmailandDriveService(access_token)
    file_id = item["id"]
    meta = service.files().get(fileId=file_id, fields="name, mimeType").execute()
    name = meta.get("name", file_id)
    mime = meta.get("mimeType", "")

    if mime in _GOOGLE_DOC_EXPORT:
        ext, export_mime = _GOOGLE_DOC_EXPORT[mime]
        req = service.files().export_media(fileId=file_id, mimeType=export_mime)
        if not name.lower().endswith(ext):
            name += ext
    else:
        req = service.files().get_media(fileId=file_id)

    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _status, done = downloader.next_chunk()
    path = os.path.join(dest_dir, os.path.basename(name))
    with open(path, "wb") as fh:
        fh.write(buf.getvalue())
    return path


def _download_onedrive_item(access_token, item, dest_dir):
    """Download one OneDrive file via Graph /content. Returns local path."""
    file_id = item["id"]
    url = f"{GRAPH_BASE}/me/drive/items/{file_id}/content"
    resp = requests.get(
        url, headers={"Authorization": f"Bearer {access_token}"},
        allow_redirects=True, timeout=120,
    )
    resp.raise_for_status()
    name = os.path.basename(item.get("name") or file_id)
    path = os.path.join(dest_dir, name)
    with open(path, "wb") as fh:
        fh.write(resp.content)
    return path


async def import_drive_files_bk(user_id, wf_name, step_id, provider, items, job_id=None):
    """Background worker: download selected drive files, stage to S3, then run the
    standard file auto-fill pipeline so policy + classification enforcement apply."""
    import tempfile
    from datetime import datetime

    from playbook.routes import _extract_autofill_payload
    from services.workflow_service import WorkflowRunnerV2
    from credits_route.route import Credits

    auth = _fetch_user_auth(user_id)
    if not auth:
        return {"status": "error", "message": "user not found", "answered_now": 0}
    _social, token, _refresh, _expiry = auth
    if not token:
        return {"status": "error", "message": "login_required", "answered_now": 0}

    dest_dir = tempfile.mkdtemp(prefix=f"drive_{user_id}_")
    s3 = s3bucket()
    file_keys = []
    local_paths = []
    try:
        for item in items or []:
            if item.get("is_folder"):
                continue  # folder expansion is resolved client-side via list()
            name = item.get("name", "")
            if not _ext_allowed(name):
                continue
            try:
                if provider == "google":
                    local_path = _download_google_item(token, item, dest_dir)
                else:
                    local_path = _download_onedrive_item(token, item, dest_dir)
            except Exception as e:
                logger.warning("Drive download failed for %s: %s", name, e)
                continue
            local_paths.append(local_path)

            # Stage to S3 with a server-built key (uuid + basename — no traversal).
            unique_id = uuid.uuid4().hex
            timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
            safe_name = os.path.basename(local_path)
            s3_key = f"{user_id}/uploads/{timestamp}_{unique_id}_{safe_name}"
            s3.upload_file(Filename=local_path, Bucket=S3_BUCKET, Key=s3_key)
            file_keys.append(s3_key)

        if not file_keys:
            return {"status": "error", "message": "No importable files", "answered_now": 0}

        (
            extracted_payload,
            inp_links,
            inp_link_keys,
            tmp_files,
        ) = _extract_autofill_payload(file_keys)
        local_paths.extend(tmp_files)

        if not extracted_payload and not inp_links:
            return {"status": "error", "message": "Could not extract content", "answered_now": 0}

        credits = Credits()
        filename = wf_name if wf_name.lower().endswith(".json") else f"{wf_name}.json"
        with WorkflowRunnerV2(
            userid=user_id, filename=filename, testing=True, credits=credits
        ) as runner:
            result = await runner.answer_ques_file_bk(
                extracted_payload, step_id, file_keys, inp_links, inp_link_keys
            )
        return result
    finally:
        for p in local_paths:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:  # noqa: S110
                pass


@drive_import_bp.route("/workflow/import/drive", methods=["POST"])
@permission_required_body("workflow.process.edit")
def import_drive():
    try:
        data = request.get_json(force=True) or {}
        user_id = data.get("user_id")
        wf_name = data.get("wf_name")
        step_id = data.get("step_id")
        provider = (data.get("provider") or "").lower()
        items = data.get("items") or []

        if not user_id or not wf_name or provider not in ("google", "onedrive"):
            return (
                jsonify({"status": "error",
                         "message": "user_id, wf_name and a valid provider are required"}),
                400,
            )
        if not items:
            return jsonify({"status": "error", "message": "no items selected"}), 400
        _, user_id = parse_composite_user_id(user_id)

        job_id = _run_drive_async(
            JobManager.submit_job(
                import_drive_files_bk, user_id, wf_name, step_id, provider, items
            )
        )
        return jsonify(
            {"status": "accepted", "job_id": job_id, "message": "Import started"}
        )
    except Exception as e:
        logger.error("import_drive failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


def _run_drive_async(coro):
    """Run a coroutine to completion from a sync Flask route (mirrors _run_async
    in playbook.routes); imported lazily to avoid an import cycle."""
    from playbook.routes import _run_async

    return _run_async(coro)
