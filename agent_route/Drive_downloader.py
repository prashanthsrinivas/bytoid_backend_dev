from googleapiclient.errors import HttpError
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials as UserCredentials
from utils.base_logger import get_logger
from utils.normal import ensure_dir
import os
import io
import time
import requests
import json

load_dotenv()
DOWNLOAD_DIR = "data"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
SERVICE_ACCOUNT_FILE = "new_service_secrets.json"
SERVICE_ACCOUNT_EMAIL = os.getenv("SERVICE_CLIENT_EMAIL")  # Replace this
logger = get_logger(__name__)


# --- Export map for Google Docs types ---
export_map = {
    "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
}


def get_main_service():
    # --- Google Drive API Initialization ---
    try:
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES
        )
        Main_service = build("drive", "v3", credentials=creds)
        logger.info("Google Drive service initialized successfully.")
    except Exception as e:
        logger.info(f"Error initializing Google Drive service: {e}")
        Main_service = None
    return Main_service


def get_files_in_folder(service_instance, folder_id):
    if not service_instance:
        logger.info("Service instance not available for listing files.")
        return []
    try:
        query = f"'{folder_id}' in parents and trashed = false"
        results = (
            service_instance.files()
            .list(q=query, fields="files(id, name, mimeType)")
            .execute()
        )
        return results.get("files", [])
    except HttpError as error:
        logger.info(
            f"An error occurred while listing files in folder {folder_id}: {error}"
        )
        return []


def share_file_with_email(file_id, user_service):
    new_permission = {
        "type": "user",
        "role": "commenter",
        "emailAddress": SERVICE_ACCOUNT_EMAIL,
    }

    logger.info(
        f"📤 Sharing file {file_id} with {SERVICE_ACCOUNT_EMAIL} is being called"
    )

    try:
        # --- Get file metadata before attempting to share ---
        logger.info("📄 Fetching file metadata...")
        file_metadata = (
            user_service.files()
            .get(fileId=file_id, fields="id, name, owners, permissions, capabilities")
            .execute()
        )
        if not file_metadata.get("capabilities", {}).get("canShare", False):
            logger.info("🚫 This account does NOT have permission to share this file.")
            return None

        logger.info("🔄 Sharing file with email initiated...")
        request = user_service.permissions().create(
            fileId=file_id,
            body=new_permission,
            fields="id",
            sendNotificationEmail=False,
        )
        logger.info("📬 Prepared request object: %s", request)

        result = request.execute()
        permission_id = result.get("id")
        logger.info(
            f"🎯 Shared file {file_id} with {SERVICE_ACCOUNT_EMAIL}, permission ID: {permission_id}"
        )
        return permission_id

    except HttpError as error:
        logger.info(f"❌ HttpError while sharing file {file_id}: {error}")
        raise error
    except Exception as e:
        logger.info(f"❌ Unexpected error while sharing file {file_id}: {e}")
        raise e


def revoke_permission(file_id, permission_id, user_service):
    try:
        user_service.permissions().delete(
            fileId=file_id, permissionId=permission_id
        ).execute()
        logger.info(f"🗑️ Revoked permission {permission_id} from file {file_id}")
    except HttpError as error:
        logger.info(
            f"❌ Failed to revoke permission {permission_id} from file {file_id}: {error}"
        )


def download_file(f, userid, base_dir=None):
    service_instance = get_main_service()
    file_id = f.get("id")
    name = f.get("name")
    mime_type = f.get("mimeType")

    if not file_id or not name:
        logger.info(f"Skipping invalid file entry: {f}. Missing 'id' or 'name'.")
        return None

    if not service_instance:
        logger.info(f"Cannot download {name}: Google Drive service not initialized.")
        return None

    base_dir = base_dir or DOWNLOAD_DIR
    logger.info(f"Processing: '{name}' (ID: {file_id}, Type: {mime_type})")

    if (
        mime_type.startswith("application/vnd.google-apps")
        and mime_type != "application/vnd.google-apps.folder"
    ):
        export_info = export_map.get(mime_type)
        if not export_info:
            logger.info(
                f"Unsupported Google Docs type for export: {mime_type} for file '{name}'. Skipping."
            )
            return None

        export_mime, extension = export_info
        save_path = os.path.join(base_dir, userid, f"{name}{extension}")

        try:
            request_export = service_instance.files().export_media(
                fileId=file_id, mimeType=export_mime
            )
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request_export)
            done = False
            while not done:
                status, done = downloader.next_chunk()

            with open(save_path, "wb") as f_out:
                f_out.write(fh.getvalue())
            logger.info(
                f"✅ Successfully downloaded (exported) '{name}' to: {save_path}"
            )
            return save_path
        except HttpError as error:
            logger.info(f"❌ Error exporting '{name}' ({file_id}): {error}")
            return None
        except IOError as error:
            logger.info(f"❌ I/O error saving '{name}': {error}")
            return None

    elif mime_type == "application/vnd.google-apps.folder":
        folder_path = os.path.join(base_dir, userid, name)
        try:
            ensure_dir(folder_path)
        except Exception as e:
            logger.info(
                f"❌ Failed to create local directory for folder '{name}': {e}. Skipping contents."
            )
            return None

        children = get_files_in_folder(service_instance, file_id)

        if len(children) == 0:
            logger.info(
                f"⚠️ Folder '{name}' (ID: {file_id}) has 0 items. Retrying in 4 seconds..."
            )
            time.sleep(4)
            children = get_files_in_folder(service_instance, file_id)

        logger.info(
            f"📂 Folder '{name}' (ID: {file_id}) has {len(children)} items. Starting recursive download."
        )
        downloaded_files = []
        for child in children:
            result = result = download_file(
                child, userid=userid, base_dir=folder_path
            )  # 🔁 <== pass updated path!
            if isinstance(result, list):
                downloaded_files.extend(result)
            elif result:
                downloaded_files.append(result)
        return downloaded_files

    else:
        save_path = os.path.join(base_dir, userid, name)
        try:
            request_file = service_instance.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request_file)
            done = False
            while not done:
                status, done = downloader.next_chunk()

            with open(save_path, "wb") as f_out:
                f_out.write(fh.getvalue())
            logger.info(f"✅ Successfully downloaded '{name}' to: {save_path}")
            return save_path
        except HttpError as error:
            logger.info(f"❌ Error downloading '{name}' ({file_id}): {error}")
            return None
        except IOError as error:
            logger.info(f"❌ I/O error saving '{name}': {error}")
            return None


def GetEmailandDriveService(access_token):
    try:
        user_creds = UserCredentials(token=access_token)
        user_service = build("drive", "v3", credentials=user_creds)

        # Test the service by fetching user info
        about_info = user_service.about().get(fields="user").execute()
        logger.info(
            "✅ Drive service is working for: %s", about_info["user"]["emailAddress"]
        )

        return user_service

    except Exception as e:
        logger.info(f"Exception in GetEmailandDriveService: {e}")
        return None


def Mediatorservice(data, userid, user_service):
    files_to_download = data.get("files", [])
    all_downloaded_paths = []
    failed_downloads = []
    ensure_dir(f"data/{userid}/google")

    logger.info(
        f"\nReceived request to download {len(files_to_download)} files/folders."
    )
    perm = None

    for f_metadata in files_to_download:
        try:
            if user_service:
                logger.info("making a share request")
                try:
                    perm = share_file_with_email(f_metadata.get("id"), user_service)
                    logger.info(f"Permission: {perm}")
                except Exception as e:
                    logger.info(f"⚠ Skipping share due to error: {e}")
                    return

            result = download_file(f_metadata, userid)
            if isinstance(result, list):
                all_downloaded_paths.extend(result)
            elif result:
                all_downloaded_paths.append(result)
            else:
                failed_downloads.append(
                    f_metadata.get("name", f_metadata.get("id", "Unknown File"))
                )
        except Exception as e:
            logger.info(
                f"An unhandled error occurred during download of {f_metadata.get('name', f_metadata.get('id', 'Unknown'))}: {e}"
            )
            failed_downloads.append(
                f_metadata.get("name", f_metadata.get("id", "Unknown File"))
            )

    if failed_downloads:
        return None, False
    else:
        if perm:
            revoke_permission(f_metadata.get("id"), perm, user_service)
        return all_downloaded_paths, True
