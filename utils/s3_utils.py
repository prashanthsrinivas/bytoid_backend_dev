from datetime import datetime
import os
import uuid
from dotenv import load_dotenv
import boto3
import json
from botocore.exceptions import ClientError
import yaml
import io
from utils.base_logger import get_logger
from werkzeug.utils import secure_filename
from utils.app_configs import IS_DEV

load_dotenv()
S3_BUCKET = os.getenv("S3_BUCKET")
S3_REGION = os.getenv("S3_REGION")
logger = get_logger(__name__)


def s3bucket():
    s3 = boto3.client(
        "s3",
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        region_name=S3_REGION,
        config=boto3.session.Config(signature_version="s3v4"),
    )
    return s3


# ---------------------------------------------------
# LIST FILES
# ---------------------------------------------------
def list_all_files(folder=None):
    s3 = s3bucket()

    try:
        if folder:
            response = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=folder)
        else:
            response = s3.list_objects_v2(Bucket=S3_BUCKET)

        if "Contents" in response:
            logger.info(f"Files listed successfully. Count={len(response['Contents'])}")
            return response["Contents"]

        logger.info("No files found in the bucket.")
        return []

    except Exception as e:
        logger.error(f"Error listing files: {e}", exc_info=True)
        return []


# ---------------------------------------------------
# UPLOAD FILE
# ---------------------------------------------------
def upload_any_file(file_path, user_id, type="workflow", file_name=None, s3_key_C=None):
    s3 = s3bucket()

    try:
        if not os.path.isfile(file_path):
            logger.error(f"File not found: {file_path}")
            return {"status": "error", "message": "File not found"}

        if not s3_key_C:
            final_name = (
                os.path.basename(file_name)
                if file_name
                else os.path.basename(file_path)
            )

            if type == "workflow":
                if final_name.startswith("config"):
                    s3_key = f"{user_id}/workflow/{final_name}"
                else:
                    first_char = os.path.splitext(final_name)[0]
                    s3_key = f"{user_id}/workflow/{first_char}/{final_name}"

            elif type == "yaml":
                s3_key = f"{user_id}/yaml/{final_name}"

            elif type == "audio":
                s3_key = f"{user_id}/aud_scripts/{final_name}"

            elif type == "user":
                s3_key = f"{user_id}/media/{final_name}"

            elif type == "messages":
                s3_key = f"{user_id}/messages/{final_name}"

            else:
                s3_key = f"{user_id}/{final_name}"

        else:
            s3_key = s3_key_C

        s3.upload_file(file_path, S3_BUCKET, s3_key)

        logger.info(f"Upload successful: {file_path} -> {s3_key}")

        return {"status": "success", "s3_key": s3_key}

    except Exception as e:
        # logger.error(f"Upload failed: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


# ---------------------------------------------------
# UPLOAD FILE WITH CUSTOM PATH
# ---------------------------------------------------
def upload_exefileany_file(file_path, bfilepath=None):
    s3 = s3bucket()

    try:
        if not os.path.isfile(file_path):
            logger.error(f"File not found: {file_path}")
            return {"status": "error", "message": "File not found"}

        s3.upload_file(file_path, S3_BUCKET, bfilepath)

        logger.info(f"Upload successful: {file_path} -> {bfilepath}")

        return {"status": "success", "s3_key": bfilepath}

    except Exception as e:
        logger.error(f"Upload failed: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


# ---------------------------------------------------
# SAVE YAML
# ---------------------------------------------------
def save_yaml_to_s3(data, user_id, filename):
    s3 = s3bucket()
    s3_key = f"{user_id}/yaml/{filename}"

    try:
        yaml_bytes = yaml.safe_dump(data, sort_keys=False).encode("utf-8")
        s3.upload_fileobj(io.BytesIO(yaml_bytes), S3_BUCKET, s3_key)

        logger.info(f"YAML saved successfully: {s3_key}")

        return {"status": "success", "s3_key": s3_key}

    except Exception as e:
        logger.error(f"Error writing YAML: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


# ---------------------------------------------------
# READ JSON
# ---------------------------------------------------
def read_json_from_s3(filepath):
    s3 = s3bucket()

    try:
        response = s3.get_object(Bucket=S3_BUCKET, Key=filepath)
        content = response["Body"].read().decode("utf-8")

        data = json.loads(content)

        logger.info(f"JSON read successfully: {filepath}")

        return data

    except Exception as e:
        # logger.error(f"Error reading JSON: {e}", exc_info=True)
        return None


# ---------------------------------------------------
# READ BINARY
# ---------------------------------------------------
def read_binary_from_s3(filepath):
    s3 = s3bucket()

    try:
        response = s3.get_object(Bucket=S3_BUCKET, Key=filepath)
        content = response["Body"].read()

        logger.info(f"Binary file read successfully: {filepath}")

        return content

    except Exception as e:
        logger.error(f"Error reading binary file: {e}", exc_info=True)
        return None


# ---------------------------------------------------
# LOAD YAML
# ---------------------------------------------------
def load_yaml_from_s3(filepath):
    s3 = s3bucket()

    try:
        response = s3.get_object(Bucket=S3_BUCKET, Key=filepath)
        content = response["Body"].read().decode("utf-8")

        data = yaml.safe_load(content)

        logger.info(f"YAML loaded successfully: {filepath}")

        return data

    except ClientError as e:
        error_code = e.response["Error"]["Code"]

        if error_code == "NoSuchKey":
            logger.warning(f"YAML does not exist yet: {filepath}")
            return None

        logger.error(f"S3 ClientError: {e}", exc_info=True)
        return None

    except Exception as e:
        logger.error(f"Error loading YAML: {e}", exc_info=True)
        return None


# ---------------------------------------------------
# DELETE FILE
# ---------------------------------------------------
def delete_file_from_s3(filepath):
    s3 = s3bucket()

    try:
        s3.delete_object(Bucket=S3_BUCKET, Key=filepath)

        logger.info(f"File deleted successfully: {filepath}")

        return True

    except Exception as e:
        logger.error(f"Error deleting file: {e}", exc_info=True)
        return False


# ---------------------------------------------------
# DELETE FOLDER
# ---------------------------------------------------
def delete_folder_from_s3(folder_prefix):
    s3 = s3bucket()

    try:
        logger.info(f"Deleting folder: {folder_prefix}")

        response = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=folder_prefix)

        if "Contents" not in response:
            logger.info("No files found in folder.")
            return

        for obj in response["Contents"]:
            delete_file_from_s3(obj["Key"])

        logger.info("Folder deleted successfully.")

    except Exception as e:
        logger.error(f"Error deleting folder: {e}", exc_info=True)


def generate_presigned_url(s3_key, expiration=3600):
    s3_client = s3bucket()

    try:
        response = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET, "Key": s3_key},
            ExpiresIn=expiration,
        )
        return response
    except ClientError as e:
        # print("❌ Error generating signed URL:", e)
        return None


def attach_CLDFRNT_url(link):
    clrf = os.getenv("CLOUDFRNT")
    if IS_DEV:
        return f"{clrf}/dev/{link}"
    return f"{clrf}/{link}"


def upload_think_image_and_get_url(
    *, user_id: str, file_obj, filename: str, content_type: str
) -> str:
    """
    Upload THINK image to:
    think/<user_id>/photos/<date>_<uuid>.<ext>
    """
    ext = filename.rsplit(".", 1)[-1]
    date = datetime.utcnow().strftime("%Y-%m-%d")
    uid = uuid.uuid4().hex[:8]
    s3_client = s3bucket()

    key = f"think/{user_id}/photos/{date}_{uid}.{ext}"

    s3_client.upload_fileobj(
        file_obj,
        S3_BUCKET,
        key,
        ExtraArgs={
            "ContentType": content_type,
            "ACL": "private",
        },
    )
    clrf = os.getenv("CLOUDFRNT")
    print(f"clrf: {clrf}")

    return f"{clrf}/{key}"


def upload_any_file_and_get_url(
    *, user_id: str, file_obj, filename: str, content_type: str
) -> str:
    """
    Upload ANY file type to:
    uploads/<user_id>/<yyyy-mm-dd>/<uuid>_<filename>
    """

    date = datetime.utcnow().strftime("%Y-%m-%d")
    uid = uuid.uuid4().hex[:8]

    safe_name = secure_filename(filename)
    key = f"uploads/{user_id}/{date}/{uid}_{safe_name}"

    s3_client = s3bucket()

    s3_client.upload_fileobj(
        file_obj,
        S3_BUCKET,
        key,
        ExtraArgs={
            "ContentType": content_type,
            "ACL": "private",
        },
    )

    cloudfront = os.getenv("CLOUDFRNT")
    return f"{cloudfront}/{key}"


def save_app_runbase_S3(record, key):
    try:
        s3 = s3bucket()
        obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
        existing = json.loads(obj["Body"].read())
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            existing = []
        else:
            raise

    existing.append(record)
    try:
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=key,
            Body=json.dumps(existing, default=str),
            ContentType="application/json",
        )
        return key
    except Exception as e:
        return None


def getallendpointdetails(prefix):
    s3_client = s3bucket()
    resp = s3_client.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix)

    files = []
    for obj in resp.get("Contents", []):
        name = obj["Key"].replace(prefix, "")
        if name.endswith(".json"):
            files.append(
                {
                    "file": name,
                    "size": obj["Size"],
                    "last_modified": obj["LastModified"].isoformat(),
                }
            )

    files.sort(key=lambda x: x["file"], reverse=True)  # latest first

    return files


def get_filedata_endp(key):
    s3_client = s3bucket()
    obj = s3_client.get_object(Bucket=S3_BUCKET, Key=key)
    data = json.loads(obj["Body"].read())
    return data
