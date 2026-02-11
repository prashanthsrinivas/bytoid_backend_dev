from datetime import datetime
import os
import uuid
from dotenv import load_dotenv
import boto3
import json
from botocore.exceptions import ClientError
import yaml
import io
from werkzeug.utils import secure_filename
from utils.app_configs import IS_DEV

load_dotenv()
S3_BUCKET = os.getenv("S3_BUCKET")
S3_REGION = os.getenv("S3_REGION")


def s3bucket():
    s3 = boto3.client(
        "s3",
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        region_name=S3_REGION,
        config=boto3.session.Config(signature_version="s3v4"),
    )
    return s3


def list_all_files(folder=None):
    s3 = s3bucket()
    if folder:
        response = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=folder)
    else:
        response = s3.list_objects_v2(Bucket=S3_BUCKET)

    if "Contents" in response:
        return response["Contents"]
    else:
        print("No files found in the bucket.")


# Call the function
def upload_any_file(file_path, user_id, type="workflow", file_name=None, s3_key_C=None):
    s3 = s3bucket()

    if not os.path.isfile(file_path):
        print(f"❌ File not found: {file_path}")
        return
    if not s3_key_C:
        # Use provided name or extract from file_path
        final_name = os.path.basename(file_name) or os.path.basename(file_path)

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
        s3_key = s3_key_C

    try:
        s3.upload_file(file_path, S3_BUCKET, s3_key)
        return {"status": "success", "s3_key": s3_key}
    except Exception as e:
        print(f"❌ Upload failed: {e}")
        return {"status": "error", "message": str(e)}


def upload_exefileany_file(file_path, bfilepath=None):
    s3 = s3bucket()

    if not os.path.isfile(file_path):
        print(f"❌ File not found: {file_path}")
        return
    s3_key = bfilepath

    try:
        s3.upload_file(file_path, S3_BUCKET, s3_key)
        # print(f"✅ Uploaded '{file_path}' to 's3://{S3_BUCKET}/{s3_key}'")
        return {"status": "success", "s3_key": s3_key}
    except Exception as e:
        print(f"❌ Upload failed: {e}")
        return {"status": "error", "message": str(e)}


def save_yaml_to_s3(data, user_id, filename):
    """Save YAML to S3 under {user_id}/yaml/{filename}."""
    s3 = s3bucket()
    s3_key = f"{user_id}/yaml/{filename}"
    try:
        yaml_bytes = yaml.safe_dump(data, sort_keys=False).encode("utf-8")
        s3.upload_fileobj(io.BytesIO(yaml_bytes), S3_BUCKET, s3_key)
        return {"status": "success", "s3_key": s3_key}
    except Exception as e:
        print(f"❌ Error writing YAML to S3: {e}")
        return {"status": "error", "message": str(e)}


# upload_any_file(file_path="cust_helpers/test/Daily_Email_Lead_Follow-up_2025-07-21_10-33-54.json",user_id="1234")
def read_json_from_s3(filepath):
    s3 = s3bucket()  # Full path in bucket
    ##print("path for reading is", filepath)

    try:
        response = s3.get_object(Bucket=S3_BUCKET, Key=filepath)
        content = response["Body"].read().decode("utf-8")
        data = json.loads(content)
        ##print("✅ JSON content loaded successfully", filepath)
        return data
    except Exception as e:
        print(f"❌ Error reading JSON file: {e}")
        return None


def read_binary_from_s3(filepath):
    """Read binary file from S3 and return the bytes content."""
    s3 = s3bucket()

    try:
        response = s3.get_object(Bucket=S3_BUCKET, Key=filepath)
        content = response["Body"].read()
        return content
    except Exception as e:
        print(f"❌ Error reading binary file from S3: {e}")
        return None


def load_yaml_from_s3(filepath):
    s3 = s3bucket()  # Full path in bucket
    # print("path loaded s3", filepath)

    try:
        response = s3.get_object(Bucket=S3_BUCKET, Key=filepath)
        content = response["Body"].read().decode("utf-8")
        data = yaml.safe_load(content)
        # print("✅ YAML content loaded successfully", filepath)
        return data
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code == "NoSuchKey":
            print(
                f"📝 YAML file does not exist yet: {filepath} (this is normal for new files)"
            )
            return None
        else:
            print(f"❌ S3 ClientError reading YAML file: {e}")
            return None
    except Exception as e:
        print(f"❌ Error reading YAML file: {e}")
        return None


def delete_file_from_s3(filepath):
    s3 = s3bucket()
    # print("🗑️ Deleting file from path:", filepath)

    try:
        s3.delete_object(Bucket=S3_BUCKET, Key=filepath)
        # print("✅ File deleted successfully")
        return True
    except Exception as e:
        print(f"❌ Error deleting file: {e}")
        return False


def delete_folder_from_s3(folder_prefix: str) -> None:
    """Delete all files under a given folder prefix (S3 is flat, so we delete by prefix)."""
    s3 = s3bucket()
    print(f"🗑️ Deleting all files under folder: {folder_prefix}")

    response = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=folder_prefix)

    if "Contents" not in response:
        # print("⚠️ No files found in this folder.")
        return

    for obj in response["Contents"]:
        key = obj["Key"]
        delete_file_from_s3(key)


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


# print(list_all_files("112359636982080060072/messages"))
# print(list_all_files())


# print(
#     read_json_from_s3(
#         filepath="109161866299858012556/aud_scripts/46d0f21a-8334-4c59-af4f-f25f75bf2912_transcript.json"
#     )
# )
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
