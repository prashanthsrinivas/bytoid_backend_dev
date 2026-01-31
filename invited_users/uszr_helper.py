from cryptography.fernet import Fernet
import base64
import time
import os
from dotenv import load_dotenv

load_dotenv()
SECRET_KEY = os.getenv("SECRETKEY")
# fernet = Fernet(SECRET_KEY)
fernet = Fernet(base64.urlsafe_b64encode(SECRET_KEY.encode("utf-8").ljust(32)[:32]))


def generate_hashed_url(base_url: str, invited_by: str, invited_to: str) -> str:
    expiry_time = int(time.time()) + 3600  # 1 hour validity

    # Build payload with raw emails (not hashes)
    payload = f"{invited_by}|{invited_to}|{expiry_time}"

    # Encrypt the payload
    token = fernet.encrypt(payload.encode()).decode()

    return f"{base_url}/{token}"


def dehashed_url(token: str) -> dict:
    """
    Decrypt token and return original emails + expiry
    """
    try:
        decoded = fernet.decrypt(token.encode()).decode()
        invited_by, invited_to, expiry = decoded.split("|")
        return invited_by, invited_to, int(expiry)
    except Exception as e:
        return {"error": f"Invalid token: {str(e)}"}


def get_user_info(email):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    SERVICE_ACCOUNT_FILE = "new_service_secrets.json"
    SCOPES = ["https://www.googleapis.com/auth/admin.directory.user.readonly"]

    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )

    # impersonate an admin in your Workspace
    delegated_creds = creds.with_subject("admin@yourdomain.com")

    service = build("admin", "directory_v1", credentials=delegated_creds)

    # lookup the user by email
    user = service.users().get(userKey=email).execute()
    # print("details invite user", user)
    return {
        "id": user["id"],
        "primaryEmail": user["primaryEmail"],
        "fullName": user["name"]["fullName"],
    }


import uuid


def create_invited_user(email, connection, permission, launch_id_fk):
    try:
        with connection.cursor() as cursor:

            # generate unique user_id
            user_id = str(uuid.uuid4())
            # print("Launch id ", launch_id_fk)  # Not inserting now

            cursor.execute(
                """
                INSERT INTO users (
                    user_id, user_type, launch_id_fk, first_name, last_name, email, phone, 
                    client_id, client_secret, token, refresh_token, expiry, password_hash, 
                    profile_pic, location, social, created_in, updated_in, logged_in_at, 
                    logged_out_at, sociallinks, subscribe_id, roles_creation, permissions, special_access 
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    NOW(), NOW(), NOW(), %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    user_id,
                    "user",  # user_type
                    None,  # launch_id_fk
                    None,  # first_name
                    None,  # last_name
                    email,
                    None,  # phone
                    None,
                    None,  # client_id, client_secret
                    None,
                    None,
                    None,  # token, refresh_token, expiry
                    "",  # password_hash
                    "",  # profile_pic
                    "",  # location
                    None,  # social
                    None,  # logged_out_at
                    None,  # sociallinks
                    None,  # subscribe_id
                    None,  # roles_creation
                    permission,  # permissions
                    False,
                ),
            )

            connection.commit()

            return True

    except Exception as e:
        #print(f"❌ Error creating invited user: {e}")
        return False
