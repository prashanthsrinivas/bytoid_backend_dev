import os
from dotenv import load_dotenv
import boto3
import json
from botocore.exceptions import ClientError

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
            s3_key = f"{user_id}/workflow/{final_name}"
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
        # print(f"✅ Uploaded '{file_path}' to 's3://{S3_BUCKET}/{s3_key}'")
        return {"status": "success", "s3_key": s3_key}
    except Exception as e:
        print(f"❌ Upload failed: {e}")
        return {"status": "error", "message": str(e)}


# upload_any_file(file_path="cust_helpers/test/Daily_Email_Lead_Follow-up_2025-07-21_10-33-54.json",user_id="1234")
def read_json_from_s3(filepath):
    s3 = s3bucket()  # Full path in bucket
    # print("path for reading is", filepath)

    try:
        response = s3.get_object(Bucket=S3_BUCKET, Key=filepath)
        content = response["Body"].read().decode("utf-8")
        data = json.loads(content)
        # print("✅ JSON content loaded successfully")
        return data
    except Exception as e:
        print(f"❌ Error reading JSON file: {e}")
        return None


def delete_file_from_s3(filepath):
    s3 = s3bucket()
    print("🗑️ Deleting file from path:", filepath)

    try:
        s3.delete_object(Bucket=S3_BUCKET, Key=filepath)
        print("✅ File deleted successfully")
        return True
    except Exception as e:
        print(f"❌ Error deleting file: {e}")
        return False


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
        print("❌ Error generating signed URL:", e)
        return None


def attach_CLDFRNT_url(link):
    clrf = os.getenv("CLOUDFRNT")
    return f"{clrf}/{link}"


# print(list_all_files("112359636982080060072/messages"))
# print(list_all_files())

# print(read_json_from_s3(filepath="112359636982080060072/messages/2025-07-24.json"))
