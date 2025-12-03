from flask import Blueprint, Flask, redirect, jsonify, request, session, url_for
import requests, urllib.parse
from db.rds_db import connect_to_rds
import os
import jwt
import uuid
from data import MESSAGES  # delete this later
import traceback
from email.utils import parseaddr
import html
from datetime import datetime, timezone, timedelta
from db.db_checkers import check_onboarding_user
import base64
from collections import defaultdict
from utils.s3_utils import upload_any_file, read_json_from_s3
from umail_helper.helper import get_users_client_id, extract_reply_content
from cust_helpers import pathconfig
from utils.normal import ensure_dir
import json


zoho_bp = Blueprint("zoho", __name__)
# app.secret_key = "your_secret_key"  # Required for sessions

CLIENT_ID = os.environ.get("ZOHO_CLIENT_ID")
CLIENT_SECRET = os.environ.get("ZOHO_CLIENT_SECRET")
REDIRECT_URI = "https://bytoid.ai/auth/zoho/callback"
ZOHO_AUTH_URL = "https://accounts.zoho.in/oauth/v2/auth"
ZOHO_TOKEN_URL = "https://accounts.zoho.in/oauth/v2/token"


SCOPES = (
    "openid",
    "email",
    # "profile",
    "ZohoMail.messages.READ",
    "ZohoMail.messages.CREATE",
    "ZohoMail.accounts.READ",
    "MailApps.messages.READ",
    "WorkDrive.files.ALL",
    # "WorkDrive.files.CREATE",
    "WorkDrive.teamfolders.ALL",
    "WorkDrive.team.READ",
    "AaaServer.profile.READ",
    "WorkDrive.workspace.READ",
)


@zoho_bp.route("/zoho/login", methods=["GET", "POST"])
def zoho_login():
    scopes_str = " ".join(SCOPES)
    auth_url = (
        f"{ZOHO_AUTH_URL}?"
        f"scope={urllib.parse.quote(scopes_str)}&"
        f"client_id={CLIENT_ID}&"
        f"response_type=code&"
        f"access_type=offline&"
        f"prompt=consent&"
        f"redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
    )
    return redirect(auth_url)


@zoho_bp.route("/zoho/callback", methods=["POST", "GET"])
def zoho_callback():
    data = request.json
    code = request.args.get("code") or data["code"]
    if not code:
        # print("no code provided")
        return "❌ No code provided"

    token_data = {
        "grant_type": "authorization_code",
        "client_id": "1000.ZVKMK2PPOQZTF4JBWGW8NG7PA4T2YB",
        "client_secret": "7097bf6e613d89a4ff75faf0f0cfe64ece80e9d908",
        "redirect_uri": REDIRECT_URI,
        "code": code,
    }

    response = requests.post(ZOHO_TOKEN_URL, data=token_data)
    tokens = response.json()
    id_token = tokens.get("id_token")
    claims = jwt.decode(id_token, options={"verify_signature": False})

    if "access_token" not in tokens:
        return f"\u274c Failed to obtain token: {tokens}"

    # After token exchange
    access_token = tokens["access_token"]
    refresh_token = tokens.get("refresh_token")
    session["zoho access token"] = access_token

    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    response = requests.get("https://accounts.zoho.in/oauth/user/info", headers=headers)

    if tokens.get("id_token"):

        id_token = tokens.get("id_token")
        data = jwt.decode(id_token, options={"verify_signature": False})
    else:
        # print("\u274c No user info available (API or id_token)")
        return redirect("https://bytoid.ai/login")

    # Extract user info
    id = data.get("ZUID") or data.get("sub")
    email = data.get("email")
    name = data.get("name")
    first_name = data.get("first_name")
    last_name = data.get("last_name")

    session["user"] = {
        "id": id,
        "name": name,
        "email": email,
    }

    conn = connect_to_rds()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM users WHERE email = %s", (email,))
    row = cursor.fetchone()

    access_token_ = access_token or ""
    expires_in = tokens.get("expires_in")

    if not row:
        cursor.execute(
            """INSERT INTO users (user_id, user_type, launch_id_fk, first_name, last_name, email, client_id,
            client_secret, token, refresh_token, expiry, password_hash, profile_pic, location, social,
            created_in, updated_in, logged_in_at, logged_out_at, special_access )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW(), NOW(),%s)""",
            (
                id,
                "admin",
                "",
                first_name,
                last_name,
                email,
                CLIENT_ID,
                CLIENT_SECRET,
                access_token_,
                refresh_token,
                expires_in,
                "",
                "",
                "",
                "zoho",
                None,
                True,
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
                CLIENT_ID,
                CLIENT_SECRET,
                access_token_,
                refresh_token,
                expires_in,
                email,
            ),
        )

    conn.commit()
    conn.close()

    newuser = check_onboarding_user(id)

    return jsonify({"user_id": id, "user_onboarded": newuser})

    # return redirect(f"https://bytoid.ai/auth/zoho/callback?user_id={id}&onboarded={str(newuser).lower()}")

    # return redirect("https://bytoid.ai/dashboard")


def get_zoho_account_id(access_token):
    mail_headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    mail_list_url = "https://mail.zoho.in/api/accounts"

    try:
        accounts_response = requests.get(mail_list_url, headers=mail_headers)

        try:
            accounts = accounts_response.json()
        except Exception as parse_err:
            print(f"[ERROR] Failed to parse Zoho account response JSON: {parse_err}")
            print(f"[DEBUG] Raw response text: {accounts_response.text}")
            return None

        if "data" not in accounts:
            print(f"[ERROR] No 'data' field in Zoho response")
            return None

        if not accounts["data"]:
            print(f"[ERROR] Empty 'data' list returned from Zoho")
            return None

        account_id = accounts["data"][0].get("accountId")
        if not account_id:
            print(
                f"[ERROR] 'accountId' missing in first data item: {accounts['data'][0]}"
            )
            return None

        # print(f"[INFO] Successfully retrieved Zoho account_id: {account_id}")
        return account_id

    except Exception as e:
        import traceback

        # print("❌ Exception in get_zoho_account_id:")
        print(traceback.format_exc())
        return None


def refresh_zoho_token(refresh_token, client_id, client_secret):
    url = "https://accounts.zoho.in/oauth/v2/token"
    payload = {
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
    }

    response = requests.post(url, data=payload)
    # print(response.status_code)
    # print(response.text)

    if response.ok:
        tokens = response.json()
        # tokens['access_token'] gives new token
        # tokens['expires_in'] gives token validity
        # print(f"new tokens after refreshing: {tokens}")
        return tokens
    else:
        raise Exception(f"Token refresh failed: {response.text}")


@zoho_bp.route("/zoho/get_email/user_id")
def fetch_zoho_emails(user_id):
    try:
        conn = connect_to_rds()
        cursor = conn.cursor()

        # Step 1: Try to fetch token info from users table
        cursor.execute(
            "SELECT token, refresh_token, email FROM users WHERE user_id = %s",
            (user_id,),
        )
        row = cursor.fetchone()

        user_email = None
        # If user is found directly in users table
        if row:
            access_token, refresh_token, email = row
            user_email = email
            old_access_token = access_token
        else:
            # Step 2: Fallback to business_info table
            cursor.execute(
                "SELECT BusinessEmail FROM business_info WHERE user_id_fk = %s",
                (user_id,),
            )
            biz_row = cursor.fetchone()
            if not biz_row:
                return {"error": "User not found in users or business_info tables"}, 404
            # print("error : User not found in users or business_info tables")

            business_email = biz_row[0]
            user_email = business_email

            # Step 3: Get token info using BusinessEmail
            cursor.execute(
                "SELECT token, refresh_token FROM users WHERE email = %s",
                (business_email,),
            )
            token_row = cursor.fetchone()

            if not token_row:
                print("error : No token found for business email")
                # return {"error": "No token found for business email"}, 404

            old_token, refresh_token = token_row

            # Step 4: Refresh token
            new_tokens = refresh_zoho_token(refresh_token, CLIENT_ID, CLIENT_SECRET)
            access_token = new_tokens["access_token"]
            expiry = new_tokens["expires_in"]

            # Step 5: Update users table with new access token
            cursor.execute(
                """
                        UPDATE users
                        SET token = %s, expiry = %s
                        WHERE token = %s
                        """,
                (access_token, expiry, old_token),
            )
            conn.commit()

        # Step 6: Get account_id and make mail request
        account_id = get_zoho_account_id(access_token)
        mails_url = f"https://mail.zoho.in/api/accounts/{account_id}/messages/view"
        mail_headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
        mails_response = requests.get(mails_url, headers=mail_headers)

        if mails_response.status_code != 200:
            return mails_response.status_code, mails_response.text

        mails_data = mails_response.json().get("data", [])

        grouped_messages = defaultdict(list)
        connection = connect_to_rds()
        if connection is None:
            return None

        cursor = connection.cursor()

        timestamp = datetime.now(timezone.utc)
        date_str = timestamp.strftime("%Y-%m-%d")
        filename = f"{date_str}.json"
        s3_key = f"{user_id}/messages/{filename}"

        # collecting messags from local file
        user_folder = os.path.join(pathconfig.basepath, "messages", user_id)
        ensure_dir(user_folder)
        filepath = os.path.join(user_folder, filename)

        email_to_client_id = {}

        for mail_data in mails_data:
            message_id = mail_data.get("messageId")

            cursor.execute(
                "SELECT 1 FROM messages WHERE message_id = %s", (message_id,)
            )
            m_id = cursor.fetchone()
            if m_id:
                continue

            subject = mail_data.get("subject")
            from_address = mail_data.get("fromAddress")
            decoded_from_address = html.unescape(from_address)
            from_name, from_email_only = parseaddr(decoded_from_address)

            raw_to_address = mail_data.get("toAddress")
            decoded_address = html.unescape(raw_to_address)
            to_name, email_only = parseaddr(decoded_address)

            folder_id = mail_data.get("folderId")

            zoho_timestamp_ms = mail_data.get("receivedTime")
            timestamp_dt = datetime.fromtimestamp(
                int(mail_data.get("receivedTime")) / 1000, tz=timezone.utc
            ).isoformat()
            snippet = mail_data.get("summary")
            extracted_body = extract_reply_content(snippet)
            has_attachment = mail_data.get("hasAttachment")

            direction = (
                "inbound" if from_address.lower() != user_email.lower() else "outbound"
            )
            conversation_id = from_address if direction == "inbound" else email_only

            if direction == "inbound":
                participant = from_address
                participant_name = from_name
            else:
                participant = email_only
                participant_name = to_name

            if participant in email_to_client_id:
                client_id = email_to_client_id[participant]
                print(f"Using cached client_id {client_id} for {participant}")
            else:
                # Check database for existing client
                client_id = get_users_client_id(participant, user_id, cursor)

                if not client_id:
                    # Create new client
                    client_id = add_lead_contact(
                        user_id, cursor, participant, participant_name
                    )

                # Cache the client_id for this email
                email_to_client_id[participant] = client_id

            message = {
                "id": message_id,
                "from": from_address,
                "to": email_only,
                "body": extracted_body,
                "subject": subject,
                "timestamp": timestamp_dt,
                "status": "received",
                "source": "zoho",
                "direction": direction,
                "user_id": user_id,
            }

            grouped_messages.setdefault(client_id, {}).setdefault("zoho", []).append(
                message
            )

            config_folder = os.path.join(
                pathconfig.basepath, "messages", user_id, client_id
            )
            ensure_dir(config_folder)

            config_filepath = os.path.join(config_folder, "config.json")
            if not os.path.exists(config_filepath):
                dummy_config = {
                    "userclients_id": client_id,
                    "conversations": [
                        {
                            "conv_id": "",
                            "ticket_id": "",
                            "ticket_name": "",
                            "subject": "",
                            "channel": "",
                            "updated_date": "",
                            "subject": "",
                            "parsed_timestamp": "",
                        }
                    ],
                }

                with open(config_filepath, "w", encoding="utf-8") as f:
                    json.dump(dummy_config, f, indent=2)

                s3_config_key = f"{user_id}/messages/{client_id}/config.json"
                s3_data = read_json_from_s3(s3_config_key)
                if s3_data is None:

                    upload_any_file(
                        config_filepath,
                        user_id,
                        type="messages",
                        s3_key_C=s3_config_key,
                    )

        existing_data = {}
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                existing_data = json.load(f)

        merged_messages = existing_data.get("input_data", {})

        # Add current Gmail messages to merged structure
        for client_id, channels in grouped_messages.items():
            for channel, messages in channels.items():
                merged_messages.setdefault(client_id, {}).setdefault("zoho", []).extend(
                    messages
                )

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(
                {"filename": filename, "input_data": merged_messages}, f, indent=2
            )

        return {
            "status": "success",
            # "new_messages": count_new
        }

    except Exception as e:
        print(f"[ERROR] → zoho fetch_mail failed: {e}")
        return {"error": str(e), "status": "failed"}


@zoho_bp.route("/zoho/send_email")
def send_zoho_email(user_id, to_email, subject, body_text, from_user_email):
    try:
        conn = connect_to_rds()
        cursor = conn.cursor()

        print(f"usr id is : {user_id}")
        cursor.execute(
            "SELECT BusinessEmail FROM business_info WHERE user_id_fk = %s", (user_id,)
        )
        biz_row = cursor.fetchone()
        if not biz_row:
            print("error : User not found in users or business_info tables")

        user_email = biz_row[0]
        # Step 3: Get token info using BusinessEmail
        cursor.execute("SELECT token FROM users WHERE email = %s", (user_email,))
        token_row = cursor.fetchone()

        if not token_row:
            print("error : No token found for business email")
            # return {"error": "No token found for business email"}, 404

        access_token = token_row[0]

        account_id = get_zoho_account_id(access_token)

        send_url = f"https://mail.zoho.in/api/accounts/{account_id}/messages"
        send_data = {
            "fromAddress": user_email,
            "toAddress": to_email,
            "subject": subject,
            "content": body_text,
        }

        headers = {
            "Authorization": f"Zoho-oauthtoken {access_token}",
            "Content-Type": "application/json",
        }

        send_response = requests.post(send_url, headers=headers, json=send_data)

        if send_response.status_code in [200, 201]:
            result = send_response.json()
            message_id = result.get("message", {}).get("messageId", str(uuid.uuid4()))
            return {
                "message_id": message_id,
                "status": "sent",
                "status_code": send_response.status_code,
            }, send_response.status_code

        # Handle non-200 errors gracefully
        try:
            error_details = send_response.json()
        except Exception:
            error_details = {"raw_response": send_response.text}

        return {
            "error": "Failed to send email via Zoho",
            "status_code": send_response.status_code,
            "zoho_error": error_details,
        }, send_response.status_code

    except Exception as e:
        import traceback

        print(traceback.format_exc())
        return {"error": str(e)}, 500


def add_lead_contact(user_id, cursor, participant, participant_name):

    # print("creating new user client and communication table")
    communication_id = str(uuid.uuid4())
    users_clients_id = str(uuid.uuid4())

    dt_utc = datetime.now(timezone.utc)
    created_date = dt_utc.strftime("%Y-%m-%d %H:%M:%S")  # For database (string)
    updated_date = dt_utc.isoformat()  # For parsing (ISO format with timezone)

    insert_communication_sql = """
                    INSERT INTO communication (
                        communication_id,
                        user_id_fk,
                        users_clients_id_fk
                    )
                    VALUES (%s, %s, NULL)
                """
    cursor.execute(insert_communication_sql, (communication_id, user_id))

    insert_sql = """
                    INSERT INTO users_clients (
                        users_clients_id,
                        communication_id_fk,
                        first_name,
                        last_name,
                        phone_number,
                        whatsapp_number,
                        email_id,
                        facebook_id,
                        instagram_id,
                        slack_id,
                        slack_workspace,
                        type,
                        created_in,
                        updated_in,
                        snooze


                    )
                    VALUES (%s, %s, %s, %s, NULL, NULL, %s, NULL, NULL, NULL, NULL,%s,%s,%s,%s)
                """
    cursor.execute(
        insert_sql,
        (
            users_clients_id,
            communication_id,
            participant_name,
            "",
            participant,
            "Lead",
            created_date,
            updated_date,
            False,
        ),
    )

    link_sql = """
                    UPDATE communication
                    SET users_clients_id_fk = %s
                    WHERE communication_id = %s
                """
    cursor.execute(link_sql, (users_clients_id, communication_id))

    cursor.connection.commit()

    return users_clients_id


@zoho_bp.route("/zoho/workdrive/root/<userId>", methods=["GET", "POST"])
def get_workdrive_root(userId):
    conn = connect_to_rds()
    cursor = conn.cursor()
    cursor.execute("SELECT token FROM users WHERE user_id = %s", (userId,))
    row = cursor.fetchone()

    if not row:
        return jsonify({"error": "Access token not found"}), 404

    access_token = row[0]
    headers = {
        "Authorization": f"Zoho-oauthtoken {access_token}",
        "Accept": "application/vnd.api+json",
    }

    """Helper function to get all team folders"""
    team_folders = []

    # Try multiple endpoints to find team folders
    endpoints_to_try = [
        "https://www.zohoapis.in/workdrive/api/v1/teamfolders",
        "https://www.zohoapis.in/workdrive/api/v1/folders",
    ]

    for endpoint in endpoints_to_try:
        try:
            response = requests.get(endpoint, headers=headers)
            if response.status_code == 200:
                data = response.json()
                for item in data.get("data", []):
                    folder_info = {
                        "id": item.get("id"),
                        "name": item.get("attributes", {}).get("name"),
                        "type": item.get("attributes", {}).get("type"),
                        "created_time": item.get("attributes", {}).get("created_time"),
                        "modified_time": item.get("attributes", {}).get(
                            "modified_time"
                        ),
                        "created_by": item.get("attributes", {})
                        .get("created_by", {})
                        .get("name", "Unknown"),
                        "source_endpoint": endpoint,
                    }

                    # Only add folders, avoid duplicates
                    if folder_info["type"] == "folder" and not any(
                        f["id"] == folder_info["id"] for f in team_folders
                    ):
                        team_folders.append(folder_info)

                # If we found folders, break
                if team_folders:
                    break

        except Exception as e:
            continue

    return jsonify(
        {"count": len(team_folders), "team_folders": team_folders, "endpoint": endpoint}
    )


# Specific route to test team folders API
@zoho_bp.route("/zoho/workdrive/teamfolders/<userId>", methods=["GET"])
def get_team_folders_direct(userId):
    conn = connect_to_rds()
    cursor = conn.cursor()
    cursor.execute("SELECT token FROM users WHERE user_id = %s", (userId,))
    row = cursor.fetchone()

    if not row:
        return jsonify({"error": "Access token not found"}), 404

    access_token = row[0]
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}

    # Try the specific team folders endpoint
    try:
        response = requests.get(
            "https://www.zohoapis.in/workdrive/api/v1/teamfolders", headers=headers
        )
        return jsonify(
            {
                "status_code": response.status_code,
                "endpoint": "teamfolders",
                "response": (
                    response.json() if response.status_code == 200 else response.text
                ),
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)})


# Alternative approach - if the above doesn't work, try this more comprehensive method
@zoho_bp.route("/zoho/workdrive/explore/<userId>", methods=["GET"])
def explore_workdrive_structure(userId):
    conn = connect_to_rds()
    cursor = conn.cursor()
    cursor.execute("SELECT token FROM users WHERE user_id = %s", (userId,))
    row = cursor.fetchone()

    if not row:
        return jsonify({"error": "Access token not found"}), 404

    access_token = row[0]
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}

    results = {}

    # Try different approaches to find your team files
    approaches = [
        # Approach 1: Try to get team workspace
        {
            "name": "team_workspaces",
            "url": "https://www.zohoapis.in/workdrive/api/v1/teamworkspaces",
            "method": "GET",
        },
        # Approach 2: Try to get private workspace
        {
            "name": "private_workspace",
            "url": "https://www.zohoapis.in/workdrive/api/v1/privateworkspace",
            "method": "GET",
        },
        # Approach 3: Try to get folders only
        {
            "name": "folders_only",
            "url": "https://www.zohoapis.in/workdrive/api/v1/files",
            "method": "GET",
            "params": {"filter[type]": "folder"},
        },
        # Approach 4: Try different files endpoint
        {
            "name": "files_recursive",
            "url": "https://www.zohoapis.in/workdrive/api/v1/files",
            "method": "GET",
            "params": {"page[limit]": "50"},
        },
    ]

    for approach in approaches:
        try:
            params = approach.get("params", {})
            response = requests.get(approach["url"], headers=headers, params=params)

            results[approach["name"]] = {
                "status_code": response.status_code,
                "url": approach["url"],
                "response": (
                    response.json()
                    if response.status_code == 200
                    else response.text[:500]
                ),
            }

            # If we found data, try to drill down
            if response.status_code == 200 and response.json().get("data"):
                data = response.json()["data"]
                for item in data[:3]:  # Check first 3 items
                    item_id = item.get("id")
                    if item_id:
                        # Try to get contents of this item
                        sub_url = f"https://www.zohoapis.in/workdrive/api/v1/files/{item_id}/files"
                        sub_response = requests.get(sub_url, headers=headers)
                        results[f"{approach['name']}_sub_{item_id}"] = {
                            "status_code": sub_response.status_code,
                            "url": sub_url,
                            "parent_name": item.get("attributes", {}).get(
                                "name", "Unknown"
                            ),
                            "response": (
                                sub_response.json()
                                if sub_response.status_code == 200
                                else sub_response.text[:300]
                            ),
                        }

        except Exception as e:
            results[approach["name"]] = {"error": str(e)}

    return jsonify(results)


def xor_encrypt(data, key):
    encrypted = bytes([b ^ ord(key[i % len(key)]) for i, b in enumerate(data.encode())])
    return base64.b64encode(encrypted).decode()


ZOHO_DOMAINS = [
    "zohoapis.in",  # India
    "zohoapis.com",  # US
    "zohoapis.eu",  # Europe
    "zohoapis.com.cn",  # China
]


def detect_zoho_domain(access_token):
    """
    Detect the correct Zoho API domain for the access token
    by calling /users/me on each domain until one succeeds.
    Returns (domain, user_info_json) or (None, None) if not found.
    """
    for domain in ZOHO_DOMAINS:
        url = f"https://www.{domain}/workdrive/api/v1/users/me"
        headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
        resp = requests.get(url, headers=headers)
        if resp.status_code == 200:
            return domain, resp.json()
    return None, None


@zoho_bp.route("/zoho/files", methods=["POST"])
def get_zoho_files():
    data = request.json
    access_token = data.get("access_token")
    if not access_token:
        return jsonify({"error": "Missing access_token"}), 400

    domain, user_info = detect_zoho_domain(access_token)
    if not domain:
        return jsonify({"error": "Invalid token or unsupported Zoho account"}), 401

    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    user_data = user_info.get("data", {})
    user_id = user_data.get("id")
    preferred_team_id = user_data.get("attributes", {}).get("preferred_team_id")
    has_personal = user_data.get("attributes", {}).get("is_personal_exist", False)

    results = []

    # Personal space
    if has_personal:
        priv_url = f"https://www.{domain}/workdrive/api/v1/users/{user_id}/privatespace"
        priv_resp = requests.get(priv_url, headers=headers)
        if priv_resp.status_code == 200:
            root_folder_id = priv_resp.json()["data"]["attributes"]["root_folder_id"]
            files_url = (
                f"https://www.{domain}/workdrive/api/v1/files/{root_folder_id}/records"
            )
            files_resp = requests.get(files_url, headers=headers)
            results.append({"source": "personal", "files": files_resp.json()})

    # Team folders
    if preferred_team_id:
        team_url = f"https://www.{domain}/workdrive/api/v1/teams/{preferred_team_id}/teamfolders"
        team_resp = requests.get(team_url, headers=headers)
        if team_resp.status_code == 200:
            for tf in team_resp.json().get("data", []):
                tf_root_id = tf["attributes"]["root_folder_id"]
                files_url = (
                    f"https://www.{domain}/workdrive/api/v1/files/{tf_root_id}/records"
                )
                tf_files_resp = requests.get(files_url, headers=headers)
                results.append(
                    {"source": "team", "team_folder": tf, "files": tf_files_resp.json()}
                )

    return jsonify(results), 200


@zoho_bp.route("/zoho")
def sendCredits():
    """
    Here we are sending the client id, accesstoken and secretkey to frontend as a encrypted one
    where frontend needs to decrypt it with secret key
    """

    client_id = CLIENT_ID
    # client_name = os.getenv("ACCESSTOKEN")
    secretkey = CLIENT_SECRET

    if not client_id:
        return jsonify({"error": "Missing environment variables"}), 500

    return jsonify(
        {
            "value": xor_encrypt(client_id, secretkey),
            # "name": xor_encrypt(client_name, secretkey),
            "mod": secretkey,
        }
    )


# def token_expired(expiry_str: str) -> bool:
#     try:
#         if not expiry_str or expiry_str.startswith("0000-00-00"):
#             return True

#         expiry = datetime.strptime(expiry_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
#         now = datetime.now(timezone.utc)

#         return now > (expiry - timedelta(minutes=2))
#     except Exception as e:
#         print(f"[token_expired] Error parsing expiry '{expiry_str}': {e}")
#         return True

from datetime import datetime, timedelta, timezone
import pytz

UTC = pytz.UTC  # or use timezone.utc if you're not using pytz


def token_expired(expiry_str: str) -> bool:
    try:
        if not expiry_str or expiry_str.startswith("0000-00-00"):
            return True

        # Try parsing with expected format
        try:
            expiry = datetime.strptime(expiry_str, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=UTC
            )
        except ValueError:
            # Fallback to ISO format or other common formats
            expiry = datetime.fromisoformat(expiry_str)

        now = datetime.now(timezone.utc)

        if not isinstance(expiry, datetime):
            print(f"[token_expired] expiry is not datetime: {type(expiry)}")
            return True

        return now > (expiry - timedelta(minutes=2))

    except Exception as e:
        print(f"[token_expired] Error parsing expiry '{expiry_str}': {e}")
        return True


@zoho_bp.route("/getZohoToken/<user_id>", methods=["GET"])
def get_zoho_token(user_id):
    conn = connect_to_rds()
    # cursor = conn.cursor(dictionary=True)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT token, refresh_token, expiry FROM users WHERE user_id = %s", (user_id,)
    )
    user = cursor.fetchone()

    if not user:
        return jsonify({"error": "User not found"}), 404

    else:
        access_token = user[0]

    conn.close()

    return jsonify({"access_token": access_token})
