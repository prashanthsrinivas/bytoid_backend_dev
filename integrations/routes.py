from flask import Blueprint, request, redirect, g, jsonify
from db.rds_db import connect_to_rds
from services.audit_log_service import log_audit_event, INTEGRATION_DELETED
from db.db_checkers import get_email_by_id
import pymysql
from utils.base_logger import get_logger
from .google_integration import google_integration_login
from .microsoft_integration import microsoft_integration_login
from urllib.parse import unquote
from microsoft_route.microsoft_helpers import retrieve_auth_state_from_redis
import asyncio
from utils.s3_utils import S3_BUCKET, load_yaml_from_s3, s3bucket, save_yaml_to_s3
from agent_route.lance_agent import LanceClient
from utils.g_scopes import g_basescopes
from microsoft_route.microsoft_helpers import OutlookSubscriptionManager
from umail_helper.helper import delete_user_sync_time
from .integrations_helpers import get_all_integrations
from dotenv import load_dotenv

# import request


integrations_bp = Blueprint("integrations", __name__)
logger = get_logger(__name__)


from flask import request, jsonify
from google_auth_oauthlib.flow import Flow
import requests
from datetime import datetime
import os
import uuid

load_dotenv()
dev_val = os.getenv("BASE_FRNT_URL")


@integrations_bp.route("/check_integrations", methods=["POST"])
def check_integrations():
    connection = connect_to_rds()
    cursor = connection.cursor(pymysql.cursors.DictCursor)
    try:
        data = request.json
        user_id = data.get("user_id")
        platform = data.get("platform")  # example: "google", "microsoft"
        type_ = data.get("type")  # example: "drive", "mails"

        # print("----- REQUEST DATA START -----")
        # print(f"user_id: {user_id}")
        # print(f"platform: {platform}")
        # print(f"type: {type_}")
        # print("----- REQUEST DATA END -----")

        if not user_id or not platform or not type_:
            return {"error": "user_id, platform, and type are required"}, 400

        query = """
            SELECT *
            FROM integrations
            WHERE primary_user_id_fk = %s
              AND platform = %s
              AND status = 'active'
            LIMIT 1;
        """

        cursor.execute(query, (user_id, platform))
        row = cursor.fetchone()

        if row:
            return {
                "exists": True,
            }, 200
        else:
            if platform == "google":
                result = google_integration_login()
            elif platform == "microsoft":
                result = microsoft_integration_login()
            return result

    except Exception as e:
        logger.error("Integration check error: %s", str(e))
        return {"error": "Internal server error"}, 500

    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@integrations_bp.route("/get_all_integrations_for_user", methods=["POST"])
def get_all_integrations_for_user():
    data = request.json
    user_id = data.get("user_id")
    return get_all_integrations(user_id)


def deletefilebasedData(filenames, userid):
    """
    Delete all Q&A entries for each filename/URL in `filenames`
    from the user's passed_ques.yaml and failed_ques.yaml files.
    """
    try:
        s3 = s3bucket()

        # Normalize filenames list
        target_names = [fn.strip() for fn in filenames]

        for ques_file in ["passed_ques.yaml", "failed_ques.yaml"]:
            s3_key = f"{userid}/yaml/{ques_file}"
            ques_data = load_yaml_from_s3(s3_key) or []

            # Flatten if nested
            flat_data = []
            for item in ques_data:
                if isinstance(item, list):
                    flat_data.extend(item)
                else:
                    flat_data.append(item)

            # Start filtering
            filtered_data = []

            for q in flat_data:
                if not isinstance(q, dict):
                    filtered_data.append(q)
                    continue

                file_value = (q.get("filename") or "").strip()
                file_base = os.path.splitext(file_value)[0].lower()

                remove_entry = False

                for target_name in target_names:
                    target_base = os.path.splitext(target_name)[0].lower()

                    # Scraped → exact url match
                    if q.get("is_scraping"):
                        if file_value == target_name:
                            remove_entry = True
                            break

                    # Regular file → match by basename (without extension)
                    else:
                        if file_base == target_base:
                            remove_entry = True
                            break

                if not remove_entry:
                    filtered_data.append(q)

            # Write back or delete file entirely
            if filtered_data:
                save_yaml_to_s3(filtered_data, userid, ques_file)
            else:
                try:
                    s3.delete_object(Bucket=S3_BUCKET, Key=s3_key)
                    logger.info(f"Deleted empty file {s3_key} from S3")
                except Exception as e:
                    logger.warning(f"Could not delete {s3_key} from S3: {e}")

        return True

    except Exception as e:
        logger.error(
            f"Error deleting question entries for user {userid}, filenames {filenames}: {e}",
            exc_info=True,
        )
        return False


async def delete_integration_file_(userid, source):
    """
    Deletes vector data from LanceDB via LanceClient and updates the YAML metadata:
    - Sets 'FileStatus' to 'Deleted'
    - Sets 'updated_date' to current datetime
    - Removes entries from passed_ques.yaml and failed_ques.yaml with matching filename
    - Deletes passed/failed YAML files if they become empty
    """

    yaml_path = f"{userid}/yaml/users_fileData.yaml"
    # if not os.path.exists(yaml_path):
    #     return jsonify({"error": "No documents found for this user"}), 404

    # Load main file metadata YAML
    all_file_data = load_yaml_from_s3(yaml_path) or {}

    # if source not in all_file_data or not isinstance(all_file_data[source], list):
    # print("error: no entries found for source")
    #     return False

    # Step 1: Update YAML entry
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    file_found = False

    if source in all_file_data:
        # 1️⃣ Get all filenames under this source
        filenames = []
        for entry in all_file_data[source]:
            if isinstance(entry, dict) and "filename" in entry:
                filenames.append(entry["filename"])

        # 2️⃣ Delete the source section entirely
        del all_file_data[source]
        file_found = True

    # if not file_found:
    # print("error: source not found")
    #     return False

    # Step 2: Save updated YAML
    # with open(yaml_path, "w") as f:
    #     yaml.safe_dump(all_file_data, f, sort_keys=False)
    save_yaml_to_s3(all_file_data, userid, "users_fileData.yaml")

    # Step 2: Delete related passed/failed Q&A entries
    if file_found:
        success = deletefilebasedData(filenames, userid)
        if not success:
            logger.warning(
                f"Failed to delete question entries for user {userid}, file {filename}"
            )

            # Step 3: Delete vectors from LanceDB
            lance_agent = LanceClient(user_id=userid)
            for filename in filenames:
                delete_result = await lance_agent.delete_file_Data(foldername=filename)
                if delete_result.get("status") != "success":
                    # print(f"error: {delete_result.get('message', 'Unknown error')}")
                    return False

    # # Reload for returning updated data
    # all_file_data

    return True


async def delete_all_user_integration_files(userid):
    """
    Deletes ALL vector data and YAML metadata for a given user:
    - Removes every source block from users_fileData.yaml
    - Collects all filenames across all sources
    - Deletes question mappings (passed/failed YAML)
    - Deletes corresponding vectors from LanceDB
    """

    yaml_path = f"{userid}/yaml/users_fileData.yaml"

    # Load full metadata
    all_file_data = load_yaml_from_s3(yaml_path) or {}

    # Nothing to delete
    if not all_file_data:
        # print("no files found for this user")
        return True

    # Clear the YAML entirely
    all_file_data = {}  # wipe everything

    # Save updated YAML (now empty)
    save_yaml_to_s3(all_file_data, userid, "users_fileData.yaml")

    # Delete passed/failed question entries
    success = delete_all_QA_files(userid)
    # if not success:
    # print(f"warning: failed to delete Q&A entries for {userid}")

    # Delete vector files from LanceDB
    lance_agent = LanceClient(user_id=userid)
    delete_result = await lance_agent.delete_all_file_Data()
    if delete_result.get("status") != "success":
        # print(
        #     f"error while deleting vector for {userid}: {delete_result.get('message')}"
        # )
        return False

    return True


def delete_all_QA_files(userid):
    """
    Deletes ALL Q&A files for the user:
    - Removes passed_ques.yaml
    - Removes failed_ques.yaml
    No filename filtering is done; all files are wiped.
    """
    try:
        s3 = s3bucket()

        for ques_file in ["passed_ques.yaml", "failed_ques.yaml"]:
            s3_key = f"{userid}/yaml/{ques_file}"

            try:
                # Try deleting from S3
                s3.delete_object(Bucket=S3_BUCKET, Key=s3_key)
                logger.info(f"Deleted {s3_key} from S3")
            except Exception as e:
                logger.warning(f"Failed to delete {s3_key}: {e}")

        return True

    except Exception as e:
        logger.error(
            f"Error deleting ALL Q&A files for user {userid}: {e}",
            exc_info=True,
        )
        return False


@integrations_bp.route("/delete/integration", methods=["POST"])
async def delete_integration():
    connection = connect_to_rds()
    cursor = connection.cursor(pymysql.cursors.DictCursor)
    try:

        data = request.json
        primary_user_id = data.get("user_id")
        platform = data.get("platform")  # example: "google", "microsoft"

        # print("----- REQUEST DATA START -----")
        # print(f"primary_user_id: {primary_user_id}")
        # print(f"platform: {platform}")
        # print("----- REQUEST DATA END -----")

        if not primary_user_id or not platform:
            return {"error": "primary_user_id are required"}, 400

        query = """
            SELECT user_id
            FROM integrations
            WHERE primary_user_id_fk = %s
            AND status = 'active'
            AND platform = %s
        """

        cursor.execute(query, (primary_user_id, platform))
        row = cursor.fetchone()  # fetch all rows

        if not row:
            return {"error": "integration does not exist"}, 400

        user_id = row["user_id"]

        # Get a list of tuples with (user_id, platform)
        query = """
            DELETE 
            FROM integrations
            WHERE primary_user_id_fk = %s
            AND status = 'active'
            AND platform = %s
        """

        cursor.execute(query, (primary_user_id, platform))
        connection.commit()

        if cursor.rowcount > 0:
            result = await delete_integration_file_(primary_user_id, platform)
            if not result:
                return jsonify({"error": "not deleted"}), 400
        else:
            return jsonify({"error": "not deleted"}), 400

        result = delete_user_sync_time(user_id)
        if result:
            # Audit logging
            actor_email = get_email_by_id(primary_user_id)
            log_audit_event(
                action=INTEGRATION_DELETED,
                endpoint="/delete/integration",
                ip=request.remote_addr,
                status="success",
                actor_user_id=primary_user_id,
                actor_email=actor_email,
                metadata={"integration_type": platform},
            )
            g.audit_logged = True

            return jsonify({"message": "successfully deleted"}), 200
        else:
            return jsonify({"error": "not deleted"}), 400

    except Exception as e:
        logger.error("delete_integrations check error: %s", str(e))
        return {"error": "Internal server error in delete_integrations"}, 500

    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


async def delete_integrations_of_contact():
    connection = connect_to_rds()
    cursor = connection.cursor(pymysql.cursors.DictCursor)
    try:

        data = request.json
        primary_user_id = data.get("user_id")

        # print("----- REQUEST DATA START -----")
        # print(f"primary_user_id: {primary_user_id}")
        # print("----- REQUEST DATA END -----")

        if not primary_user_id:
            return {"error": "primary_user_id are required"}, 400

        query = """
            SELECT user_id
            FROM integrations
            WHERE primary_user_id_fk = %s
            AND status = 'active'
        """

        cursor.execute(query, (primary_user_id,))
        row = cursor.fetchall()  # fetch all rows

        if not row:
            # print(f"no integrations for this user")
            return True

        user_ids = [r["user_id"] for r in row]

        # Get a list of tuples with (user_id, platform)
        query = """
                DELETE 
                FROM integrations
                WHERE primary_user_id_fk = %s
                AND status = 'active'
            """

        cursor.execute(query, (primary_user_id,))
        connection.commit()

        if cursor.rowcount > 0:
            pass

            # for user_id in user_ids:

            #         result = await delete_all_user_integration_files(primary_user_id)
            #         if not result:
            #             return jsonify({"error": "not deleted"}), 400
            #     else:
            #         return jsonify({"error": "not deleted"}), 400

            #     result = delete_user_sync_time(user_id)
            #     if result:
            #         return jsonify({"message": "successfully deleted"}), 200
            #     else:
            #         return jsonify({"error": "not deleted"}), 400

    except Exception as e:
        logger.error("delete_integrations check error: %s", str(e))
        return {"error": "Internal server error in delete_integrations"}, 500

    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


from urllib.parse import unquote

# ----------- GOOGLE CALLBAK FOR INTEGRATION ---------------


@integrations_bp.route("/integration/google/callback", methods=["GET"])
def google_integration_callback():
    # print("inside callback")

    try:
        full_url = request.args.get("url")
        code = request.args.get("code")
        state = request.args.get("state")
        app_user_id = request.args.get("user_id")

        # print("----- Integration Callback Params -----")
        # print(f"full_url     : {full_url}")
        # print(f"code         : {code}")
        # print(f"state        : {state}")
        # print(f"app_user_id  : {app_user_id}")
        # print("----------------------------------------")

        if not full_url or not code:
            return (
                jsonify({"connected": False, "error": "Missing required params"}),
                400,
            )

        # Google URL is double encoded → decode twice
        decoded_url = unquote(unquote(full_url))
        # print("DECODED URL:", decoded_url)

        # OAuth Flow
        flow = Flow.from_client_secrets_file(
            "client_secrets.json",
            scopes=g_basescopes,
            redirect_uri=f"{dev_val}/integration/google/callback",
        )

        flow.fetch_token(authorization_response=decoded_url)
        credentials = flow.credentials

        access_token = credentials.token
        refresh_token = credentials.refresh_token
        expiry = credentials.expiry
        expiry_str = expiry.strftime("%Y-%m-%d %H:%M:%S")
        client_id = credentials.client_id
        client_secret = credentials.client_secret

        # print("----- Google OAuth Tokens -----")
        # print(f"access_token : {access_token}")
        # print(f"refresh_token: {refresh_token}")
        # print(f"expiry       : {expiry}")
        # print(f"expiry_str   : {expiry_str}")
        # print(f"client_id   : {client_id}")
        # print(f"client_secret   : {client_secret}")
        # print("--------------------------------")

        # User info
        userinfo = requests.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        ).json()

        google_user_id = userinfo.get("sub")
        email = userinfo.get("email")

        # print("----- Google User Info -----")
        # print(f"google_user_id: {google_user_id}")
        # print(f"email         : {email}")
        # print("--------------------------------")

        # Save to DB
        conn = connect_to_rds()
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        cursor.execute(
            """
        INSERT INTO integrations
            (
                integration_id,
                user_id,
                platform,
                client_id,
                client_secret,
                access_token,
                refresh_token,
                expiry,
                status,
                created_at,
                updated_at,
                primary_user_id_fk,
                email
            )
        VALUES
            (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW(), %s, %s)
        ON DUPLICATE KEY UPDATE
            access_token = %s,
            refresh_token = %s,
            expiry = %s,
            status = 'active',
            updated_at = NOW()
        """,
            (
                # INSERT values (13)
                str(uuid.uuid4()),  # 1 integration_id
                google_user_id,  # 2 user_id
                "google",  # 3 platform
                client_id,  # 4 client_id
                client_secret,  # 5 client_secret
                access_token,  # 6 access_token
                refresh_token,  # 7 refresh_token
                expiry_str,  # 8 expiry
                "active",  # 9 status
                app_user_id,  # 10 primary_user_id_fk
                email,  # 11 email
                # UPDATE values (3)
                access_token,  # 12
                refresh_token,  # 13
                expiry_str,  # 14
            ),
        )

        conn.commit()
        cursor.close()
        conn.close()

        return jsonify({"connected": True})

    except Exception as e:
        # print("ERROR:", str(e))
        return jsonify({"connected": False, "error": str(e)}), 500


# ----------- MICROSOFT CALLBAK FOR INTEGRATION ---------------


@integrations_bp.route("/integration/microsoft/callback", methods=["GET"])
def microsoft_integration_callback():
    CLIENT_ID = os.environ.get("MICROSOFT_CLIENT_ID")
    CLIENT_SECRET = os.environ.get("MICROSOFT_CLIENT_SECRET")
    TENANT_ID = os.environ.get("MICROSOFT_TENANT_ID")
    SCOPES = [
        "User.Read",
        "Mail.Send",
        "Mail.ReadWrite",
        "Calendars.ReadWrite",
        "OnlineMeetings.ReadWrite",
        "Chat.ReadWrite",
        "Files.Read.All",
    ]
    try:
        # Get parameters
        auth_code = request.args.get("code")
        error = request.args.get("error")
        state = request.args.get("state")
        app_user_id = request.args.get("user_id")

        # print("----------------------------")
        # print(f"auth_code: {auth_code}")
        # print(f"error: {error}")
        # print(f"state: {state}")
        # print(f"app_user_id: {app_user_id}")
        # print("----------------------------")

        if error or not auth_code or not state:
            return (
                jsonify({"connected": False, "error": "Missing required params"}),
                400,
            )

        # ✅ Retrieve the stored PKCE verifier from Redis using state
        stored_state = asyncio.run(retrieve_auth_state_from_redis(state))

        if not stored_state:
            return jsonify({"connected": False, "error": "no stored variable"}), 400

        code_verifier = stored_state.get("code_verifier")

        if not code_verifier:
            return jsonify({"connected": False, "error": "no code verifier"}), 400

        # print("----------------------------")
        # print(f"code_verifier: {code_verifier}")
        # print(f"auth_code: {auth_code}")
        # print("----------------------------")

        # Use direct HTTP call to Microsoft token endpoint with PKCE code_verifier
        # redirect_uri = get_microsoft_redirect_uri(request)
        redirect_uri = f"{dev_val}/integration/microsoft/callback"

        token_url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"

        token_data = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": auth_code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,  # ✅ Send PKCE verifier directly
            "scope": " ".join(SCOPES),
        }

        token_response = requests.post(token_url, data=token_data, timeout=10)

        result = token_response.json()

        # Get user info (like Google does)
        access_token = result["access_token"]
        refresh_token = result.get("refresh_token", "")
        expiry = result["expires_in"]  # int

        from datetime import datetime, timedelta

        expiry_dt = datetime.utcnow() + timedelta(seconds=expiry)
        expiry_str = expiry_dt.strftime("%Y-%m-%d %H:%M:%S")

        # print("----- TOKEN DETAILS START -----")
        # print(f"access_token: {access_token}")
        # print(f"refresh_token: {refresh_token}")
        # print(f"expiry (seconds): {expiry}")
        # print(f"expiry_str: {expiry_str}")
        # print("----- TOKEN DETAILS END -----")

        headers = {"Authorization": f"Bearer {access_token}"}

        userinfo_response = requests.get(
            "https://graph.microsoft.com/v1.0/me", headers=headers
        )

        userinfo = userinfo_response.json()
        email = userinfo.get("mail") or userinfo.get("userPrincipalName")
        microsoft_user_id = userinfo.get("id")

        # print("-----------------------")
        # print(f"microsoft_user_id : {microsoft_user_id}")
        # print(f"email : {email}")
        # print(f"expiry_str: {expiry_str}")
        # print("-----------------------")

        try:
            conn = connect_to_rds()
            cursor = conn.cursor(pymysql.cursors.DictCursor)

            # check if this email is already in user table
            cursor.execute(
                """
                SELECT 1 
                FROM users
                WHERE email = %s
                """,
                (str(email),),
            )
            row = cursor.fetchone()
            if row:
                return (
                    jsonify(
                        {
                            "connected": False,
                            "error": "This account is already registered as a user. Use a different microsoft account to integrate.",
                        }
                    ),
                    409,
                )

            cursor.execute(
                """
            INSERT INTO integrations
                (integration_id, user_id, platform, access_token, refresh_token, expiry, status, created_at, updated_at, primary_user_id_fk, email)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, NOW(), NOW(), %s, %s)
            ON DUPLICATE KEY UPDATE
                access_token=%s,
                refresh_token=%s,
                expiry=%s,
                status='active',
                updated_at=NOW()
            """,
                (
                    # INSERT (11)
                    str(uuid.uuid4()),  # 1 integration_id
                    microsoft_user_id,  # 2 user_id
                    "microsoft",  # 3 platform
                    access_token,  # 4
                    refresh_token,  # 5
                    expiry_str,  # 6
                    "active",  # 7 status
                    app_user_id,  # 8 primary_user_id_fk
                    email,  # 9 email
                    # UPDATE (3)
                    access_token,  # 10
                    refresh_token,  # 11
                    expiry_str,  # 12
                ),
            )

            conn.commit()
            cursor.close()
            conn.close()

            manager = OutlookSubscriptionManager()
            future = manager.create_subscription_async(access_token, email)

            return jsonify({"connected": True}), 200

        except Exception as db_error:
            # print("ERROR:", str(e))
            return jsonify({"connected": False, "error": str(e)}), 500

    except Exception as e:
        # print("ERROR:", str(e))
        return jsonify({"connected": False, "error": str(e)}), 500
