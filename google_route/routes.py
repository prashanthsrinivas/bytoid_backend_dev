from flask import Blueprint, request, jsonify, session, redirect, make_response
from pydrive.auth import GoogleAuth
from google_auth_oauthlib.flow import Flow
from pydrive.drive import GoogleDrive
from google.auth.transport import requests as g_requests
from google.oauth2 import id_token
from datetime import datetime, timedelta
import uuid
import os
import requests
from db.rds_db import connect_to_rds
from dotenv import load_dotenv
import json
import pymysql
import base64
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as g_request
from db.db_checkers import check_onboarding_user, fetch_apikey_from_launch
from session_manager_route.routes import generate_session
from utils.base_logger import get_logger

load_dotenv()  # Load from .env into environment variables
google_bp = Blueprint("auth", __name__)
logger = get_logger(__name__)


@google_bp.route("/login")
def login():
    session.pop("user_id", None)
    session.pop("state", None)  # Optional, but clean
    ga = GoogleAuth()

    flow = Flow.from_client_secrets_file(
        "client_secrets.json",
        scopes=(
            "https://www.googleapis.com/auth/userinfo.profile",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.compose",
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/drive.metadata.readonly",
            "https://www.googleapis.com/auth/calendar",
            "https://www.googleapis.com/auth/contacts",
            # "https://www.googleapis.com/auth/docs",
            "openid",
        ),
        redirect_uri="https://www.bytoid.ai/auth/facebook/callback",
    )
    # google_bp.logger.info(f"{flow}")

    auth_url, state = flow.authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="false"
    )
    session["state"] = state
    # print(auth_url)
    # print("login",session['state'])
    return jsonify(auth_url=auth_url)


@google_bp.route("/oauth2callback")
def oauth2callback(url, state):
    if not state:
        return "Missing state in URL", 400

    unique_id = str(uuid.uuid4())

    flow = Flow.from_client_secrets_file(
        "client_secrets.json",
        scopes=(
            "https://www.googleapis.com/auth/userinfo.profile",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.compose",
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/drive.metadata.readonly",
            "https://www.googleapis.com/auth/calendar",
            "https://www.googleapis.com/auth/contacts",
            # "https://www.googleapis.com/auth/docs",
            "openid",
        ),
        redirect_uri="https://www.bytoid.ai/auth/facebook/callback",
    )

    try:
        # flow.fetch_token(code=code)
        flow.fetch_token(authorization_response=url)
        credentials = flow.credentials
        # google_bp.logger.info(f"{credentials}")

        # delete this line later
        with open("credentials.json", "w") as f:
            f.write(credentials.to_json())

        # actual_creds = credentials.to_json()
        # json_creds = json.loads(actual_creds)

        # Correct way to access the token
        access_token = credentials.token
        print("access : ", access_token)
        userinfo_response = requests.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {credentials.token}"},
        )

        if userinfo_response.status_code == 200:
            userinfo = userinfo_response.json()
            logger.info("INFO :", userinfo)
            email = userinfo.get("email")
            given_name = userinfo.get("given_name")
            family_name = userinfo.get("family_name")
            user_id = userinfo.get("sub")
            phonenumber = userinfo.get("phoneNumber", "")

            conn = connect_to_rds()
            cursor = conn.cursor(pymysql.cursors.DictCursor)

            # Check if the user_id is present
            cursor.execute(
                "SELECT user_id,user_type FROM users WHERE email = %s", (email,)
            )
            user_exists = cursor.fetchone()
            session["user_id"] = user_id

            if not user_exists:
                logger.info("creating a new user")

                cursor.execute(
                    """INSERT INTO users (user_id, user_type, launch_id_fk, first_name, last_name, email,phone, client_id,
                client_secret, token, refresh_token, expiry, password_hash, profile_pic, location, social,
                created_in, updated_in, logged_in_at, logged_out_at,sociallinks,subscribe_id,roles_creation,permissions)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW(), NOW(), %s,%s,%s,%s,%s)
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
                    ),
                )

            else:
                logger.info("users update data")
                prev_id = user_exists.get("user_id", "NODATA")
                logger.info("prev-> %s", prev_id)
                if user_id != prev_id:
                    cursor.execute(
                        """
                    UPDATE users 
                    SET 
                        user_id = %s,
                        client_id = %s,
                        client_secret = %s,
                        token = %s,
                        refresh_token = %s,
                        expiry = %s,
                        updated_in = NOW(),
                        logged_in_at = NOW(),
                        logged_out_at = NOW()
                    WHERE email = %s
                    """,
                        (
                            user_id,
                            credentials.client_id,
                            credentials.client_secret,
                            credentials.token,
                            credentials.refresh_token,
                            credentials.expiry,
                            email,
                        ),
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE users 
                        SET 
                            client_id = %s,
                            client_secret = %s,
                            token = %s,
                            refresh_token = %s,
                            expiry = %s,
                            updated_in = NOW(),
                            logged_in_at = NOW(),
                            logged_out_at = NOW()
                        WHERE email = %s
                    """,
                        (
                            credentials.client_id,
                            credentials.client_secret,
                            credentials.token,
                            credentials.refresh_token,
                            credentials.expiry,
                            email,
                        ),
                    )
            conn.commit()
            conn.close()
            generate_session()
        # invited user special case
        if user_exists:
            prev_type = user_exists.get("user_type", "NO TYPE")
            logger.info("prev UserType -> %s", prev_type)
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
        logger.error("OAuth error: %s", str(e))
        return "Failure", 500


@google_bp.route("/browser_url", methods=["POST"])
def receive_browser_url():
    try:
        data = request.get_json()
        browser_url = data.get("url")
        state = session.get("state", data["state"])

        # This function should return the user ID (e.g., Google account ID)
        user_id, newuser = oauth2callback(browser_url, state)
        print("sdaas", user_id, newuser)
        apikey = fetch_apikey_from_launch(user_id)

        # Prepare response
        response = make_response(
            jsonify(
                {
                    "status": "success",
                    "url": browser_url,
                    "userid": user_id,
                    "user_onboarded": newuser,
                    "api_key": apikey or "",
                }
            )
        )
        print("response from browser_url", response)

        # Set secure session cookie (HttpOnly, Secure)
        response.set_cookie(
            key="session_user_id",
            value=str(user_id),
            httponly=True,
            secure=True,
            samesite="None",
            path="/",
            max_age=7 * 24 * 60 * 60,
        )

        return response

    except Exception as e:
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
            return redirect("https://bytoid.ai/login")
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

            if not row:
                return jsonify({"error": "User not found"}), 404

            client_id, client_secret, token, refresh_token, expiry = row

            # Ensure expiry is a datetime object
            if isinstance(expiry, str):
                expiry = datetime.fromisoformat(expiry)

            time_to_expiry = expiry - datetime.now()
            # print("expiry from db",expiry)
            # print("current time", datetime.now())

            # Refresh only if token is close to expiring
            if expiry <= datetime.now() or time_to_expiry <= timedelta(minutes=10):
                # print("token time expired")
                try:
                    creds = Credentials(
                        token=token,
                        refresh_token=refresh_token,
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=client_id,
                        client_secret=client_secret,
                        scopes=[
                            "https://www.googleapis.com/auth/userinfo.profile",
                            "https://www.googleapis.com/auth/userinfo.email",
                            "https://www.googleapis.com/auth/gmail.readonly",
                            "https://www.googleapis.com/auth/gmail.send",
                            "https://www.googleapis.com/auth/gmail.modify",
                            "https://www.googleapis.com/auth/gmail.compose",
                            "https://www.googleapis.com/auth/drive.metadata.readonly",
                            "https://www.googleapis.com/auth/drive",
                            "https://www.googleapis.com/auth/calendar",
                            "https://www.googleapis.com/auth/contacts",
                            "openid",
                        ],
                    )

                    creds.refresh(g_request())
                    print("refresh started")

                    # Save refreshed token and new expiry time
                    cursor.execute(
                        """
                        UPDATE users SET token = %s, expiry = %s WHERE user_id = %s
                    """,
                        (creds.token, creds.expiry.isoformat(), user_id),
                    )
                    connection.commit()
                    if value:
                        return creds.token

                    return jsonify({"token": creds.token})

                except Exception as e:
                    print(f"Token refresh failed: {e}")
                    return redirect("https://bytoid.ai/login")

            # Return existing token if not refreshed
            cursor.execute("SELECT token FROM users WHERE user_id = %s", (user_id,))
            user_row = cursor.fetchone()
            # print("token not expired")

            if user_row is None:
                return jsonify({"error": "Token missing after fallback"}), 400
            if value:
                return user_row[0]
            return jsonify({"token": user_row[0]})

    except Exception as e:
        print(f"Error occurred: {e}")
        return jsonify({"error": "Internal server error"}), 500

    finally:
        if not in_connection and connection:
            connection.close()


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


# --- Flask Route ---
# @google_bp


@google_bp.route("/check-user", methods=["POST"])
def check_user():
    """
    here we check weather the user present or not
    """
    data = request.json
    userid = data["userid"]
    connection = connect_to_rds()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT social
                FROM users
                WHERE user_id = %s
            """,
                (str(userid),),
            )
            row = cursor.fetchone()

            if not row:
                return jsonify({"error": "User not found"}), 404

            social = row[0]
            if social == "google":
                print("google based account")
                get_token(userid)
                return {"message": "user found"}, 200
            return {"message": "user found"}, 200

    except Exception as e:
        return jsonify({"error": f"Failed to check user: {e}"}), 500
    finally:
        connection.close()
