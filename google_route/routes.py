from flask import Blueprint, request, jsonify, session, redirect, make_response
from google_route.google_helpers import update_user_alive
from services.gmail_service import GmailService
from pydrive.auth import GoogleAuth
from google_auth_oauthlib.flow import Flow
from pydrive.drive import GoogleDrive
from google.auth.transport import requests as g_requests
from google.oauth2 import id_token
from datetime import datetime, timedelta
import uuid
import os

os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
import requests
from db.rds_db import connect_to_rds
from dotenv import load_dotenv
import json
import pymysql
import base64
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as g_request
from db.db_checkers import (
    check_onboarding_user,
    ensure_starter_credits_for_user,
    fetch_apikey_from_launch,
)
from services.redis_service import get_redis
from utils.base_logger import get_logger
from session_manager_route.routes import session_login
from integrations.integrations_helpers import get_all_integrations
from umail_helper.helper import store_integrations_in_redis
from microsoft_route.microsoft_helpers import (
    OutlookSubscriptionManager,
    refresh_expired_microsoft_tokens,
    check_microsoft_token_expiry,
)
from umail_helper.mails_process import check_mailbox
from services.credit_system import CreditManager
from utils.g_scopes import g_basescopes
from utils.app_configs import ACCESSIBLE_IDS, ALLOWED_ORIGINS

load_dotenv()  # Load from .env into environment variables
google_bp = Blueprint("auth", __name__)
logger = get_logger(__name__)

dev_val = os.getenv("BASE_FRNT_URL", "")


@google_bp.route("/login")
def login():
    origin = request.headers.get("Origin")

    logger.info(f"[Google Login] Origin={origin}")

    # Clear old session
    session.pop("user_id", None)
    session.pop("state", None)

    mobile_redirect_uri = request.args.get("redirect_uri")
    platform = request.args.get("platform")

    # Store mobile redirect URI
    if mobile_redirect_uri and platform == "mobile":
        session["mobile_redirect_uri"] = mobile_redirect_uri

    # Resolve frontend origin
    if origin in ALLOWED_ORIGINS:
        val = origin
    else:
        val = os.getenv("BASE_FRNT_URL")

    WEB_REDIRECT_URI = f"{val}/auth/google/callback"

    logger.info(f"[Google Login] Redirect URI={WEB_REDIRECT_URI}")

    flow = Flow.from_client_secrets_file(
        "client_secrets.json",
        scopes=g_basescopes,
        redirect_uri=WEB_REDIRECT_URI,
    )

    # IMPORTANT:
    # access_type=offline -> refresh token
    # prompt=consent -> force refresh token every login
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )

    session["state"] = state

    logger.info("[Google Login] Auth URL generated successfully")

    if platform == "mobile":
        return redirect(auth_url)

    return jsonify(auth_url=auth_url)


from db.db_checkers import make_api_key


@google_bp.route("/oauth2callback")
def oauth2callback(url, state):

    if not state:
        return "Missing state in URL", 400

    # OPTIONAL SECURITY CHECK
    session_state = session.get("state")

    if session_state and session_state != state:
        logger.error(
            f"OAuth state mismatch. " f"session_state={session_state}, state={state}"
        )
        return "Invalid OAuth state", 400

    unique_id = str(uuid.uuid4())

    origin = request.headers.get("Origin")

    print("Origin:", origin)

    if origin in ALLOWED_ORIGINS:
        val = origin
    else:
        val = os.getenv("BASE_FRNT_URL")

    WEB_REDIRECT_URI = f"{val}/auth/google/callback"

    EXPO_REDIRECT_URI = "https://auth.expo.io/@anonymous/user-app-ee3ebe74"

    use_expo = os.getenv("USE_EXPO_REDIRECT", "false").lower() == "true"

    redirect_uri = WEB_REDIRECT_URI

    flow = Flow.from_client_secrets_file(
        "client_secrets.json",
        scopes=g_basescopes,
        redirect_uri=redirect_uri,
    )

    try:

        # IMPORTANT
        flow.fetch_token(authorization_response=url)

        credentials = flow.credentials

        logger.info(
            f"Refresh token received from Google: "
            f"{'YES' if credentials.refresh_token else 'NO'}"
        )

        logger.info(f"Access token expiry: {credentials.expiry}")

        # REMOVE THIS IN PRODUCTION
        with open("credentials.json", "w") as f:
            f.write(credentials.to_json())

        # Fetch Google user info
        userinfo_response = requests.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {credentials.token}"},
            timeout=20,
        )

        if userinfo_response.status_code == 200:

            userinfo = userinfo_response.json()

            logger.info("INFO : %s", userinfo)

            email = userinfo.get("email")
            given_name = userinfo.get("given_name")
            family_name = userinfo.get("family_name")
            user_id = userinfo.get("sub")
            phonenumber = userinfo.get("phoneNumber", "")

            import time

            MAX_RETRIES = 3

            for attempt in range(MAX_RETRIES):

                conn = None

                try:

                    conn = connect_to_rds()

                    cursor = conn.cursor(pymysql.cursors.DictCursor)

                    conn.begin()

                    # LOCK USER ROW
                    cursor.execute(
                        """
                        SELECT user_id, user_type, refresh_token
                        FROM users
                        WHERE email = %s
                        FOR UPDATE
                        """,
                        (email,),
                    )

                    user_exists = cursor.fetchone()

                    session["user_id"] = user_id
                    session.pop("active_workspace_id", None)

                    # =====================================================
                    # NEW USER
                    # =====================================================
                    if not user_exists:

                        logger.info("creating a new user")

                        cursor.execute(
                            """
                            INSERT INTO users (
                                user_id,
                                user_type,
                                launch_id_fk,
                                first_name,
                                last_name,
                                email,
                                phone,
                                client_id,
                                client_secret,
                                token,
                                refresh_token,
                                expiry,
                                password_hash,
                                profile_pic,
                                location,
                                social,
                                created_in,
                                updated_in,
                                logged_in_at,
                                logged_out_at,
                                sociallinks,
                                subscribe_id,
                                roles_creation,
                                permissions,
                                special_access
                            )
                            VALUES (
                                %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s,
                                %s, %s, %s, %s,
                                NOW(), NOW(), NOW(), %s,
                                %s, %s, %s, %s, %s
                            )
                            """,
                            (
                                user_id,
                                "admin",
                                "",
                                given_name,
                                family_name,
                                email,
                                phonenumber,
                                credentials.client_id,
                                credentials.client_secret,
                                credentials.token,
                                credentials.refresh_token,
                                credentials.expiry,
                                "",
                                "",
                                "",
                                "google",
                                None,
                                None,
                                None,
                                None,
                                None,
                                True,
                            ),
                        )

                        conn.commit()

                        conn.begin()

                        internal_subscription_id = f"starter_{user_id}"

                        cursor.execute("""
                            SELECT plan_code, monthly_token_limit
                            FROM plans
                            WHERE plan_code IN ('STARTER','FREE')
                            AND is_active = 1
                            ORDER BY CASE plan_code
                                WHEN 'STARTER' THEN 1
                                WHEN 'FREE' THEN 2
                            END
                            LIMIT 1
                            """)

                        starter_plan = cursor.fetchone()

                        if not starter_plan:
                            raise Exception("Neither STARTER nor FREE plan found")

                        cursor.execute(
                            """
                            INSERT INTO subscriptions (
                                user_id,
                                stripe_subscription_id,
                                stripe_customer_id,
                                stripe_price_id,
                                status,
                                current_period_start,
                                current_period_end,
                                created_at
                            )
                            VALUES (
                                %s,
                                %s,
                                NULL,
                                NULL,
                                'active',
                                NOW(),
                                NULL,
                                NOW()
                            )
                            """,
                            (
                                user_id,
                                internal_subscription_id,
                            ),
                        )

                        cursor.execute(
                            """
                            INSERT INTO credit_buckets (
                                bucket_id,
                                user_id,
                                source_type,
                                source_ref,
                                credits_total,
                                credits_used,
                                expires_at,
                                is_expired,
                                created_at
                            )
                            VALUES (
                                UUID(),
                                %s,
                                'SUBSCRIPTION',
                                %s,
                                %s,
                                0,
                                DATE_ADD(NOW(), INTERVAL 60 DAY),
                                0,
                                NOW()
                            )
                            """,
                            (
                                user_id,
                                internal_subscription_id,
                                starter_plan["monthly_token_limit"],
                            ),
                        )

                        conn.commit()

                        make_api_key(user_id)

                    # =====================================================
                    # EXISTING USER
                    # =====================================================
                    else:

                        logger.info("users update data")

                        prev_id = user_exists.get(
                            "user_id",
                            "NODATA",
                        )

                        existing_refresh_token = user_exists.get("refresh_token")

                        logger.info(
                            f"Existing refresh token present: "
                            f"{'YES' if existing_refresh_token else 'NO'}"
                        )

                        logger.info(
                            f"New refresh token received: "
                            f"{'YES' if credentials.refresh_token else 'NO'}"
                        )

                        # IMPORTANT:
                        # NEVER overwrite valid refresh token with NULL
                        final_refresh_token = (
                            credentials.refresh_token
                            if credentials.refresh_token
                            else existing_refresh_token
                        )

                        if not credentials.refresh_token:
                            logger.warning(
                                "Google did not return refresh token. "
                                "Keeping existing refresh token."
                            )

                        if user_id != prev_id:

                            cursor.execute(
                                """
                                UPDATE users
                                SET
                                    user_id = %s,
                                    first_name = %s,
                                    last_name = %s,
                                    client_id = %s,
                                    client_secret = %s,
                                    token = %s,
                                    refresh_token = %s,
                                    expiry = %s,
                                    social = %s,
                                    updated_in = NOW(),
                                    logged_in_at = NOW(),
                                    logged_out_at = NOW()
                                WHERE email = %s
                                """,
                                (
                                    user_id,
                                    given_name,
                                    family_name,
                                    credentials.client_id,
                                    credentials.client_secret,
                                    credentials.token,
                                    final_refresh_token,
                                    credentials.expiry,
                                    "google",
                                    email,
                                ),
                            )

                        else:

                            cursor.execute(
                                """
                                UPDATE users
                                SET
                                    first_name = %s,
                                    last_name = %s,
                                    client_id = %s,
                                    client_secret = %s,
                                    token = %s,
                                    refresh_token = %s,
                                    expiry = %s,
                                    social = %s,
                                    updated_in = NOW(),
                                    logged_in_at = NOW(),
                                    logged_out_at = NOW()
                                WHERE email = %s
                                """,
                                (
                                    given_name,
                                    family_name,
                                    credentials.client_id,
                                    credentials.client_secret,
                                    credentials.token,
                                    final_refresh_token,
                                    credentials.expiry,
                                    "google",
                                    email,
                                ),
                            )

                            conn.commit()

                            ensure_starter_credits_for_user(
                                user_id,
                                conn,
                            )

                    conn.commit()

                    cursor.close()
                    conn.close()

                    break

                except pymysql.err.OperationalError as e:

                    if conn:
                        conn.rollback()
                        conn.close()

                    if e.args[0] == 1205 and attempt < MAX_RETRIES - 1:
                        time.sleep(0.5 * (attempt + 1))
                        continue

                    raise

                except Exception as e:

                    if conn:
                        conn.rollback()
                        conn.close()

                    logger.error(
                        f"Database error: {str(e)}",
                        exc_info=True,
                    )

                    raise

            # MOBILE REDIRECT
            mobile_redirect_uri = session.pop(
                "mobile_redirect_uri",
                None,
            )

            if mobile_redirect_uri:

                return redirect(f"{mobile_redirect_uri}?code={unique_id}")

        # invited user special case
        if user_exists:

            prev_type = user_exists.get(
                "user_type",
                "NO TYPE",
            )

            logger.info(
                "prev UserType -> %s",
                prev_type,
            )

            if prev_type == "user":

                logger.info("Invited User Logged in")

                return user_id, True

        # onboarding check
        newuser = check_onboarding_user(user_id)

        logger.info("new user %s", newuser)

        if newuser:
            return user_id, True

        return user_id, False

    except Exception as e:

        logger.error(
            "OAuth error: %s",
            str(e),
            exc_info=True,
        )

        return None, 500


@google_bp.route("/user/alive", methods=["POST"])
async def user_alive():
    body = request.json or {}
    user_id = body.get("user_id")
    is_alive = body.get("is_alive", True)

    if not user_id:
        return jsonify({"error": "user_id required"}), 400

    redis = get_redis()

    await update_user_alive(redis, user_id, is_alive)

    return jsonify(
        {
            "user_id": user_id,
            "is_alive": is_alive,
        }
    )


@google_bp.route("/browser_url", methods=["POST"])
async def receive_browser_url():
    try:
        data = request.get_json()
        browser_url = data.get("url")
        state = session.get("state", data["state"])

        # This function should return the user ID (e.g., Google account ID)
        user_id, newuser = oauth2callback(browser_url, state)
        if not user_id:
            return jsonify({"error": "cant process google login"}), 500
        session_id, access_token, refresh_token = await session_login(user_id)
        # print("sdaas", user_id, newuser)
        apikey = fetch_apikey_from_launch(user_id)

        mailbox_setting = check_mailbox(user_id)
        if mailbox_setting:
            try:
                service = GmailService(user_id=user_id)
                service.create_watch_req()
            except Exception as e:
                logger.warning("Gmail watch setup failed for user %s: %s", user_id, e)

        connection = connect_to_rds()
        cursor = connection.cursor()

        # get all integrations for this user and store it in redis
        integrations_data, status_code = get_all_integrations(user_id)
        # integrations = integrations_data.get("integrations")

        # print(f"integrations_data : {integrations_data}")
        if integrations_data:
            redis_response = await store_integrations_in_redis(
                user_id, integrations_data
            )
            # if redis_response:
            #     #print(f"integrations stored in redis")
            # else:
            #     #print(f"integrations not stored in redis")

            exists = any(
                item["platform"] == "microsoft"
                for item in integrations_data.get("integrations", [])
            )
            if exists:
                # print(f"microsoft in integratiosn")

                cursor.execute(
                    """
                    SELECT email,access_token, refresh_token, user_id
                    FROM integrations
                    WHERE primary_user_id_fk = %s AND platform = 'microsoft'
                """,
                    (str(user_id),),
                )
                row = cursor.fetchone()

                # if not row:
                #     print(f"cannot find microsoft integration email")

                (
                    microsoft_email,
                    microsoft_access_token,
                    microsoft_refresh_token,
                    microsoft_user_id,
                ) = row

                expired = check_microsoft_token_expiry(cursor, user_id)
                if expired:
                    resp = refresh_expired_microsoft_tokens_for_integrations(
                        microsoft_refresh_token,
                        cursor,
                        connection,
                        None,
                        microsoft_user_id,
                    )
                    data = resp.get_json()
                    microsoft_access_token = data["token"]
                    # if microsoft_access_token:
                    # print(f"new token created")

                manager = OutlookSubscriptionManager()

                # print(f"creating subscription for {microsoft_email}")
                future = manager.create_subscription_async(
                    microsoft_access_token, microsoft_email
                )

        # check if credits are available
        credits = CreditManager(connection)
        avail_credits = credits.check_if_remaining(user_id=user_id)
        credit_status = avail_credits.get("status")
        message = avail_credits.get("message")

        cursor.close()
        connection.close()

        if user_id in ACCESSIBLE_IDS:
            # Prepare response
            response = make_response(
                jsonify(
                    {
                        "status": "success",
                        "url": browser_url,
                        "userid": user_id,
                        "user_onboarded": newuser,
                        "api_key": apikey or "",
                        "service": "google",
                        "credit_status": credit_status,
                        "message": message,
                        "bytoid_admin": True,
                    }
                )
            )
        else:
            # Prepare response
            response = make_response(
                jsonify(
                    {
                        "status": "success",
                        "url": browser_url,
                        "userid": user_id,
                        "user_onboarded": newuser,
                        "api_key": apikey or "",
                        "service": "google",
                        "credit_status": credit_status,
                        "message": message,
                    }
                )
            )
        # print("response from browser_url", response)
        redis = get_redis()
        await update_user_alive(redis, user_id, True)

        # Set secure session cookie (HttpOnly, Secure)
        response.set_cookie(
            "session_id",
            session_id,
            httponly=True,
            secure=True,
            samesite="None",
            max_age=60 * 60 * 24 * 7,
        )
        response.set_cookie(
            "access_token",
            access_token,
            httponly=True,
            secure=True,
            samesite="None",
            max_age=60 * 60 * 24 * 7,
        )
        response.set_cookie(
            "refresh_token",
            refresh_token,
            httponly=True,
            secure=True,
            samesite="None",
            max_age=60 * 60 * 24 * 7,
        )

        return response

    except Exception as e:
        logger.info("Error at login at browser_url", e)
        return jsonify({"error": str(e)}), 500


@google_bp.route("/sync-drive")
def sync_drive():
    try:
        # user_id = session.get('user_id')
        ga = GoogleAuth()
        ga.LoadClientConfigFile("client_secrets.json")
        ga.LocalWebserverAuth()  # Handles the OAuth flow
        ga.SaveCredentialsFile("credentials.json")  # Save for future sessions
        with open("credentials.json") as f:
            raw = json.load(f)

        # Reformat to PyDrive-compatible format
        reformatted = {
            "access_token": raw["token"],
            "client_id": raw["client_id"],
            "client_secret": raw["client_secret"],
            "refresh_token": raw["refresh_token"],
            "token_expiry": raw["expiry"].split(".")[0] + "Z",  # remove microseconds
            "token_uri": raw["token_uri"],
            "user_agent": None,
            "invalid": False,
            "_class": "OAuth2Credentials",
            "_module": "oauth2client.client",
        }

        # Save the reformatted JSON
        with open("credentials.json", "w") as f:
            json.dump(reformatted, f, indent=2)
        ga.LoadCredentialsFile("credentials.json")
        # ga.LoadCredentialsFile("client_secrets.json")
        if ga.credentials is None:
            return redirect(f"{os.getenv('BASE_FRNT_URL')}/login")
        elif ga.access_token_expired:
            ga.Refresh()
        else:
            ga.Authorize()

        drive = GoogleDrive(ga)
        # connection = pymysql.connect(
        #         host='database-1.czoeckiiosd2.ap-south-1.rds.amazonaws.com',
        #         user='skilbyt_db',
        #         password='JesusChristIsLord$1',
        #         db='ai_support'
        #         )
        # connection= connect_to_rds()
        folder_id = ""
        # try:
        #     with connection.cursor() as cursor:
        #         sql = """
        #             SELECT sa.documentation_link
        #             FROM subagents sa
        #             INNER JOIN launch l ON sa.launch_id = l.launch_id
        #             WHERE l.user_id = %s
        #             LIMIT 1
        #         """
        #         cursor.execute(sql, (user_id,))
        #         result = cursor.fetchone()
        #         if result:
        #              folder_id = result[0]  # documentation_link
        #         else:
        #             return None
        # finally:
        #     connection.close()

        file_list = drive.ListFile(
            {"q": f"'{folder_id}' in parents and trashed=false"}
        ).GetList()

        os.makedirs("data", exist_ok=True)
        synced_files = []
        for file in file_list:
            file_name = file["title"]
            mime_type = file["mimeType"]

            if mime_type == "application/vnd.google-apps.document":
                # Export Google Doc as .docx
                export_name = f"data/{file_name}.docx"
                file.GetContentFile(
                    export_name,
                    mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
                synced_files.append(f"{file_name}.docx")

            elif file["title"].endswith((".txt", ".pdf", ".docx")):
                file.GetContentFile(f"data/{file['title']}")
                synced_files.append(file["title"])
        # Reindex after sync
        # all_documents = []
        # txt_loader = DirectoryLoader("data", glob="**/*.txt", loader_cls=TextLoader)
        # all_documents.extend(txt_loader.load())
        # pdf_loader = DirectoryLoader("data", glob="**/*.pdf", loader_cls=PyMuPDFLoader)
        # all_documents.extend(pdf_loader.load())
        # doc_loader = DirectoryLoader("data", glob="**/*.docx", loader_cls=UnstructuredWordDocumentLoader)
        # all_documents.extend(doc_loader.load())

        # docs = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200).split_documents(all_documents)

        # PineconeVectorStore.from_documents(
        #     documents=docs,
        #     embedding=embedding,
        #     index_name=PINECONE_INDEX_NAME,
        #     text_key="text"
        # )
        # for fname in synced_files:
        #     print(f" - {fname}")

        return jsonify({"status": "success", "files": synced_files})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@google_bp.route("/get_google_client_id", methods=["POST"])
def get_google_client_id():
    try:
        ga = GoogleAuth()
        ga.LoadClientConfigFile("client_secrets.json")
        client_id = ga.client_config["client_id"]
        if not client_id:
            return jsonify({"error": "Client ID not found"}), 500
        return jsonify({"client_id": client_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@google_bp.route("/google_login", methods=["POST"])
def google_login():
    try:
        data = request.json
        if not data or "credential" not in data:
            return jsonify({"error": "Credential is required"}), 400

        credential = data["credential"]
        # Verify the token
        id_info = id_token.verify_oauth2_token(
            credential,
            g_requests.Request(),  # ,
            # YOUR_GOOGLE_CLIENT_ID  # optional: you can validate if token was issued for your app
        )

        # You now have user information
        email = id_info["email"]
        sub = id_info["sub"]  # Google's unique user ID

        # Example: allow only certain emails
        # if not email.endswith('@skilbyt.com'):
        #    return jsonify({"error": "Unauthorized user"}), 403

        return jsonify({"success": True, "email": email})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@google_bp.route("/auth/google/token", methods=["POST"])
def get_token(inuser=None, value=None, in_connection=None):
    """
    sending token for the user for the drive and picker.
    making sure if the token expired and making a new one
    if not redirects to login
    """
    if inuser:
        user_id = inuser
    else:
        data = request.json
        user_id = (
            session.get("user_id")
            or session.get("userState_id")
            or inuser
            or data["userid"]
        )
    if not in_connection:
        connection = connect_to_rds()
    else:
        connection = in_connection
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT client_id, client_secret, token, refresh_token, expiry
                FROM users
                WHERE user_id = %s
            """,
                (str(user_id),),
            )
            row = cursor.fetchone()
            source_table = "users"

            if not row:
                return jsonify({"error": "User not found"}), 404
            if not row[0]:
                cursor.execute(
                    """
                    SELECT client_id,client_secret,
                               access_token,refresh_token,expiry
                               FROM integrations WHERE user_id=%s and platform = 'google'
                               """,
                    (str(user_id),),
                )
                row = cursor.fetchone()
                source_table = "integrations"

            client_id, client_secret, token, refresh_token, expiry = row

            # Ensure expiry is a timezone-naive datetime object
            if isinstance(expiry, str):
                expiry = datetime.fromisoformat(expiry)
            if expiry is None:
                expiry = datetime.utcnow() - timedelta(seconds=1)
            elif expiry.tzinfo is not None:
                expiry = expiry.replace(tzinfo=None)

            time_to_expiry = expiry - datetime.utcnow()

            # Refresh only if token is close to expiring
            if expiry <= datetime.utcnow() or time_to_expiry <= timedelta(minutes=10):
                ##print("token time expired")
                try:
                    creds = Credentials(
                        token=token,
                        refresh_token=refresh_token,
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=client_id,
                        client_secret=client_secret,
                    )

                    creds.refresh(g_request())
                    # print("refresh started")

                    # Save refreshed token and new expiry time
                    if source_table == "users":
                        cursor.execute(
                            """
                            UPDATE users
                            SET token=%s, expiry=%s
                            WHERE user_id=%s
                        """,
                            (creds.token, creds.expiry.isoformat(), user_id),
                        )
                    else:
                        cursor.execute(
                            """
                            UPDATE integrations
                            SET access_token=%s, expiry=%s
                            WHERE user_id=%s
                            AND platform='google'
                        """,
                            (creds.token, creds.expiry.isoformat(), user_id),
                        )
                    connection.commit()
                    if value:
                        return creds.token

                    return jsonify({"token": creds.token})

                except Exception as e:
                    # print(f"Token refresh failed: {e}")
                    return redirect(f"{dev_val}/login")

            # Return existing (non-expired) token — use the value already fetched from DB
            if value:
                return token
            return jsonify({"token": token})

    except Exception as e:
        # print(f"Error occurred: {e}")
        return jsonify({"error": "Internal server error"}), 500

    finally:
        if not in_connection and connection:
            connection.close()


@google_bp.route("/check-user", methods=["POST"])
def token_update_and_check():
    """
    Universal token validator + refresher
    Supports Google & Microsoft
    Returns success=true if user stays logged in
    """
    data = request.json or {}

    user_id = (
        session.get("user_id")
        or session.get("userState_id")
        or data.get("user_id")
        or data.get("userid")
    )
    # print(f"user_id: {user_id}")

    if not user_id:
        return jsonify({"login_required": True}), 401

    connection = connect_to_rds()

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT social, client_id, client_secret, token, refresh_token, expiry
                FROM users
                WHERE user_id = %s
                """,
                (str(user_id),),
            )
            row = cursor.fetchone()

            if not row:
                return jsonify({"login_required": True}), 401

            social, client_id, client_secret, token, refresh_token, expiry = row

            if isinstance(expiry, str):
                expiry = datetime.fromisoformat(expiry)
            if expiry is None:
                expiry = datetime.utcnow() - timedelta(seconds=1)
            elif expiry.tzinfo is not None:
                expiry = expiry.replace(tzinfo=None)

            # print(f"{social} | {client_id} | {expiry}")
            logger.info("social got is %s - %s", social, client_id)

            # 🔹 Google
            if social == "google":
                return refresh_google_if_needed(
                    cursor,
                    connection,
                    user_id,
                    client_id,
                    client_secret,
                    token,
                    refresh_token,
                    expiry,
                )

            # 🔹 Microsoft
            if social == "microsoft":
                return refresh_microsoft_if_needed(
                    cursor,
                    connection,
                    user_id,
                    refresh_token,
                    expiry,
                )

            return jsonify({"login_required": True}), 401

    except Exception as e:
        # print("Token update error:", e)
        return jsonify({"login_required": True}), 401

    finally:
        connection.close()


def refresh_google_if_needed(
    cursor,
    connection,
    user_id,
    client_id,
    client_secret,
    token,
    refresh_token,
    expiry,
):
    now = datetime.utcnow()

    # Normalize expiry: handle None (treat as expired) and timezone-aware datetimes
    if expiry is None:
        expiry = now - timedelta(seconds=1)
    elif expiry.tzinfo is not None:
        expiry = expiry.replace(tzinfo=None)

    time_to_expiry = expiry - now

    if expiry <= now or time_to_expiry <= timedelta(minutes=10):
        try:
            creds = Credentials(
                token=token,
                refresh_token=refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=client_id,
                client_secret=client_secret,
            )

            creds.refresh(g_request())

            # 🔥 Google may rotate refresh token — ALWAYS save it
            cursor.execute(
                """
                UPDATE users
                SET token = %s,
                    refresh_token = %s,
                    expiry = %s
                WHERE user_id = %s
                """,
                (
                    creds.token,
                    creds.refresh_token,
                    creds.expiry.isoformat(),
                    user_id,
                ),
            )
            connection.commit()

            return jsonify({"message": "user found"})

        except Exception as e:
            # print("Google refresh failed:", e)
            return jsonify({"login_required": True}), 401

    return jsonify({"message": "user found"})


def refresh_microsoft_if_needed(
    cursor,
    connection,
    user_id,
    refresh_token,
    expiry,
):
    # logger.info("in the refresh_microsoft_if_needed")

    now = datetime.utcnow()

    # ✅ FIX: handle None expiry
    if expiry is None:
        logger.info("expiry is None → forcing refresh")
        expiry = now - timedelta(seconds=1)

    time_to_expiry = expiry - now

    if expiry <= now or time_to_expiry <= timedelta(minutes=10):
        logger.info("into expired state of microsoft")

        client_id = os.environ.get("MICROSOFT_CLIENT_ID")
        client_secret = os.environ.get("MICROSOFT_CLIENT_SECRET")

        try:
            response = requests.post(
                "https://login.microsoftonline.com/common/oauth2/v2.0/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "scope": "offline_access https://graph.microsoft.com/.default",
                },
            )

            data = response.json()
            logger.info("Microsoft refresh response: %s", data)

            if "access_token" not in data:
                raise Exception(data)

            new_access_token = data["access_token"]

            # refresh_token may not always come
            new_refresh_token = data.get("refresh_token", refresh_token)

            expires_in = int(data["expires_in"])
            new_expiry = datetime.utcnow() + timedelta(seconds=expires_in)

            cursor.execute(
                """
                UPDATE users
                SET token=%s, refresh_token=%s, expiry=%s
                WHERE user_id=%s
                """,
                (
                    new_access_token,
                    new_refresh_token,
                    new_expiry.isoformat(),
                    user_id,
                ),
            )

            connection.commit()

            logger.info("Microsoft token refreshed successfully")

            return jsonify({"message": "user found"})

        except Exception:
            logger.exception("Microsoft refresh failed")
            return jsonify({"login_required": True}), 401

    logger.info("no need of microsoft refresh")
    return jsonify({"message": "user found"})


def xor_encrypt(data, key):
    encrypted = bytes([b ^ ord(key[i % len(key)]) for i, b in enumerate(data.encode())])
    return base64.b64encode(encrypted).decode()


def xor_decrypt(encoded, key):
    data = base64.b64decode(encoded)
    decrypted = bytes([b ^ ord(key[i % len(key)]) for i, b in enumerate(data)])
    return decrypted.decode()


@google_bp.route("/creds", methods=["GET"])
def sendCredits():
    """
    Send the client ID, access token, and other credentials to the frontend in encrypted form.
    The frontend must decrypt them using the provided secret key.
    """
    pr = request.args.get("pr", "GM")  # Default to GM
    secretkey = os.getenv("SECRETKEY")

    if not secretkey:
        return jsonify({"error": "Missing SECRETKEY"}), 500

    # Common env values
    client_id = os.getenv("CLIENTID")
    access_token = os.getenv("ACCESSTOKEN")
    zoho_client_id = os.getenv("ZOHO_FRNT_CLIENT_ID")
    microsoft_client_id = os.getenv("MICROSOFT_CLIENT_ID")
    microsoft_tenantid = os.getenv("MICROSOFT_TENANT_ID")

    if pr == "GM":
        if not client_id or not access_token:
            return jsonify({"error": "Missing CLIENTID or ACCESSTOKEN"}), 500
        return jsonify(
            {
                "value": xor_encrypt(client_id, secretkey),
                "name": xor_encrypt(access_token, secretkey),
                "mod": secretkey,
            }
        )

    elif pr == "ZH":
        if not zoho_client_id:
            return jsonify({"error": "Missing ZOHO_CLIENT_ID"}), 500
        return jsonify(
            {"value": xor_encrypt(zoho_client_id, secretkey), "mod": secretkey}
        )

    elif pr == "MS":
        if not microsoft_client_id:
            return jsonify({"error": "Missing MICROSOFT_CLIENT_ID"}), 500
        return jsonify(
            {
                "value": xor_encrypt(microsoft_client_id, secretkey),
                "name": xor_encrypt(microsoft_tenantid, secretkey),
                "mod": secretkey,
            }
        )

    else:
        return jsonify({"error": f"Unknown pr value: {pr}"}), 400


def refresh_expired_microsoft_tokens_for_integrations(
    refresh_token, cursor, connection, value, user_id
):
    client_id = os.environ.get("MICROSOFT_CLIENT_ID")
    client_secret = os.environ.get("MICROSOFT_CLIENT_SECRET")
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
        # Microsoft Graph OAuth refresh URL
        token_url = "https://login.microsoftonline.com/common/oauth2/v2.0/token"

        payload = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
            "scope": " ".join(SCOPES + ["offline_access"]),
        }

        response = requests.post(token_url, data=payload)
        if response.status_code != 200:
            # print("Refresh failed:", response.text)
            return redirect(f"{os.getenv('BASE_FRNT_URL')}/login")

        new_data = response.json()

        new_token = new_data.get("access_token")
        new_refresh = new_data.get("refresh_token", refresh_token)
        expires_in = new_data.get("expires_in", 3600)

        new_expiry = datetime.now() + timedelta(seconds=expires_in)

        # Store updated token
        cursor.execute(
            """
                        UPDATE integrations
                        SET access_token = %s, refresh_token = %s, expiry = %s
                        WHERE user_id = %s
                        """,
            (new_token, new_refresh, new_expiry.isoformat(), user_id),
        )
        connection.commit()

        if value:
            return new_token

        return jsonify({"token": new_token})

    except Exception as e:
        # print(f"Microsoft token refresh failed: {e}")
        return redirect(f"{os.getenv('BASE_FRNT_URL')}/login")
