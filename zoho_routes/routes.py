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
from umail_helper.helper import get_users_client_id
from cust_helpers import pathconfig
from utils.normal import ensure_dir
import json






zoho_bp = Blueprint("zoho", __name__)
# app.secret_key = "your_secret_key"  # Required for sessions

CLIENT_ID = os.environ.get("ZOHO_CLIENT_ID")
CLIENT_SECRET = os.environ.get("ZOHO_CLIENT_SECRET")
# REDIRECT_URI = "https://rtdtj5q9dh.execute-api.ca-central-1.amazonaws.com/zoho/callback"
REDIRECT_URI = "https://bytoid.ai/auth/zoho/callback"
ZOHO_AUTH_URL = "https://accounts.zoho.in/oauth/v2/auth"
ZOHO_TOKEN_URL = "https://accounts.zoho.in/oauth/v2/token"

# SCOPES = (
#     # "openid"
#     "email"
#     # "profile"
#     # "ZohoMail.messages.READ "
#     # "ZohoMail.messages.CREATE "
#     # "ZohoMail.accounts.READ "
#     # "WorkDrive.files.READ "
#     # "WorkDrive.files.CREATE "
#     # "WorkDrive.teamfolders.READ "
#     # "WorkDrive.team.READ "
#     # "AaaServer.profile.READ"
# )

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
        print("no code provided")
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
    print(f"tokens: {tokens}")
    id_token = tokens.get("id_token")
    claims = jwt.decode(id_token, options={"verify_signature": False})
    print("claims:", claims)

    if "access_token" not in tokens:
        print("access token not obtained")
        return f"\u274c Failed to obtain token: {tokens}"

    # After token exchange
    access_token = tokens["access_token"]
    refresh_token = tokens.get("refresh_token")
    session["zoho access token"] = access_token
    print(f"access_token : {access_token}")
    print(f"refresh_token : {refresh_token}")

    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    response = requests.get("https://accounts.zoho.in/oauth/user/info", headers=headers)
    print("userinfo response:", response.status_code, response.text)

    # if response.status_code == 200:
    #     data = response.json()
    #     print("data form if part ")
    if tokens.get("id_token"):
        print("data form if part ")

        id_token = tokens.get("id_token")
        data = jwt.decode(id_token, options={"verify_signature": False})
    else:
        print("\u274c No user info available (API or id_token)")
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
    print(f"user info :{id} : {name} : {email}")

    conn = connect_to_rds()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM users WHERE email = %s", (email,))
    row = cursor.fetchone()

    access_token_ = access_token or ""
    expires_in = tokens.get("expires_in")

    if not row:
        print("new user")
        cursor.execute(
            """INSERT INTO users (user_id, user_type, launch_id_fk, first_name, last_name, email, client_id,
            client_secret, token, refresh_token, expiry, password_hash, profile_pic, location, social,
            created_in, updated_in, logged_in_at, logged_out_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW(), NOW())""",
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
    print(newuser, "new user")

    return jsonify({"user_id": id, "user_onboarded": newuser})

    # return redirect(f"https://bytoid.ai/auth/zoho/callback?user_id={id}&onboarded={str(newuser).lower()}")

    # return redirect("https://bytoid.ai/dashboard")


def get_zoho_account_id(access_token):
    mail_headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    mail_list_url = "https://mail.zoho.in/api/accounts"
    accounts_response = requests.get(mail_list_url, headers=mail_headers)
    accounts = accounts_response.json()
    # print(f"accounts : {accounts}")

    return accounts["data"][0]["accountId"]


def refresh_zoho_token(refresh_token, client_id, client_secret):
    print("entered in refresh_zoho_token")
    url = "https://accounts.zoho.in/oauth/v2/token"
    payload = {
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token"
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
        # cursor.execute("SELECT token, refresh_token, email FROM users WHERE user_id = %s", (user_id,))
        # row = cursor.fetchone()

        # # If user is found directly in users table
        # if row:
        #     access_token, refresh_token, email = row
        #     print(f"***** access_token, refresh_token, email : {access_token}, {refresh_token}, {email}")
        #     old_access_token = access_token
        # else:
            # Step 2: Fallback to business_info table
                print("searching in buisness_info table")
                print(f"usr id is : {user_id}")
                cursor.execute("SELECT BusinessEmail FROM business_info WHERE user_id_fk = %s", (user_id,))
                biz_row = cursor.fetchone()
                if not biz_row:
                    # return {"error": "User not found in users or business_info tables"}, 404
                    print("error : User not found in users or business_info tables")


                business_email = biz_row[0]
                print(f"buisness email is: {business_email}")

                # Step 3: Get token info using BusinessEmail
                cursor.execute("SELECT token, refresh_token FROM users WHERE email = %s", (business_email,))
                token_row = cursor.fetchone()

                if not token_row:
                    print("error : No token found for business email")
                    # return {"error": "No token found for business email"}, 404

                old_token, refresh_token = token_row

                # Step 4: Refresh token
                print("***goiign to call refresh_zoho_token")
                new_tokens = refresh_zoho_token(refresh_token, CLIENT_ID, CLIENT_SECRET)
                access_token = new_tokens["access_token"]
                expiry = new_tokens["expires_in"]

                # Step 5: Update users table with new access token
                print("*****updating usrs table with new tokens")
                cursor.execute(
                    """
                    UPDATE users
                    SET token = %s, expiry = %s
                    WHERE token = %s
                    """,
                    (access_token, expiry, old_token)
                )
                conn.commit()

            # Step 6: Get account_id and make mail request
                print("**** getting zoho id")
                account_id = get_zoho_account_id(access_token)
                mails_url = f"https://mail.zoho.in/api/accounts/{account_id}/messages/view"
                mail_headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
                mails_response = requests.get(mails_url, headers=mail_headers)

                if mails_response.status_code != 200:
                    return mails_response.status_code, mails_response.text

                mails_data = mails_response.json().get("data", [])
                # print(f"****mails data: {mails_data}")

                grouped_messages = defaultdict(list)
                connection = connect_to_rds()
                if connection is None:
                    return None

                cursor = connection.cursor()

                timestamp = datetime.now(timezone.utc)
                date_str = timestamp.strftime("%Y-%m-%d")
                filename = f"{date_str}.json"
                s3_key = f"{user_id}/messages/{filename}"

                # Try to load existing messages
                try:
                    existing = read_json_from_s3(filepath=s3_key)
                    input_data = existing.get("input_data", {})
                    print(f" ***** got input data: {input_data}")
                except Exception as e:
                    print(f"⚠️ S3 file missing or unreadable: {e}")
                    input_data = {}

                count_new = 0

                existing_ids = set()
                for client_channels in input_data.values():
                    for channel_msgs in client_channels.values():
                        for msg in channel_msgs:
                            existing_ids.add(msg.get("id"))

                for mail_data in mails_data:
                    # print(f"mail_data:{mail_data}")
                    message_id = mail_data.get("messageId")

                    if message_id in existing_ids:
                        print(f"⏭️ Message {message_id} already exists. Skipping.")
                        continue
                
                    # h_headers = {"Authorization": f"Zoho-oauthtoken {access_token}, 'Accept': 'application/json'"}
                    # url = f'https://mail.zoho.in/api/accounts/{account_id}/messages/{message_id}/header'
                    # response = requests.get(url, headers=h_headers)
                    # response.raise_for_status()
                    # headers = response.json().get('data', {}).get('headers', [])
                    # print(f"******headers are:{headers}")
                    
                    # in_reply_to = next((h['value'] for h in headers if h['name'].lower() == 'in-reply-to'), None)
                    # references = next((h['value'] for h in headers if h['name'].lower() == 'references'), None)


                    subject = mail_data.get("subject")
                    from_address = mail_data.get("fromAddress")
                    raw_to_address = mail_data.get("toAddress")
                    decoded_address = html.unescape(raw_to_address)
                    name, email_only = parseaddr(decoded_address)

                    folder_id = mail_data.get("folderId")

                    # result = test_message_endpoints(account_id, folder_id, message_id, access_token)

                    zoho_timestamp_ms = mail_data.get("receivedTime")
                    timestamp_dt = datetime.fromtimestamp(
                        int(mail_data.get("receivedTime")) / 1000, tz=timezone.utc
                    ).isoformat()
                    snippet = mail_data.get("summary")
                    has_attachment = mail_data.get("hasAttachment")

                    direction = (
                        "inbound" if from_address.lower() != business_email.lower() else "outbound"
                    )
                    conversation_id = from_address if direction == "inbound" else email_only
                    participant = from_address if direction == "inbound" else email_only
                    client_id = get_users_client_id(participant, cursor)
                    
                    if client_id:

                        message = {

                            "id": message_id,
                            "from": from_address,
                            "to": email_only,
                            "body": snippet,
                            "subject": subject,
                            "timestamp": timestamp_dt,
                            "status": "received",
                            "source": "zoho",
                            "direction": direction,
                            "user_id": user_id,
                            "conversation_id": conversation_id,
                            # "reply-to":reply_to,
                            # "references":references
                            # "internet_message_id": message_id,
                        }

                        grouped_messages.setdefault(client_id, {}).setdefault(
                            "zoho", []
                        ).append(message)

                        count_new += 1

                        print(
                            f"******[DEBUG] for zoho user_id={user_id} ({type(user_id)}), client_id={client_id} ({type(client_id)}), basepath={pathconfig.basepath}"
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
                                        "subject":""
                                    }
                                ],
                            }

                            with open(config_filepath, "w", encoding="utf-8") as f:
                                json.dump(dummy_config, f, indent=2)

                            print(f"*************✅ Config file created at {config_filepath} in zoho")
                        else:
                            print(f"📁 Config file already exists: {config_filepath}")

                # Write file locally
                user_folder = os.path.join(pathconfig.basepath, "messages", user_id)
                ensure_dir(user_folder)
                filepath = os.path.join(user_folder, filename)

                existing_data = {}
                if os.path.exists(filepath):
                    with open(filepath, "r", encoding="utf-8") as f:
                        existing_data = json.load(f)

                merged_messages = existing_data.get("input_data", {})

                # Add current Gmail messages to merged structure
                for client_id, channels in grouped_messages.items():
                    for channel, messages in channels.items():
                        merged_messages.setdefault(client_id, {}).setdefault("zoho", []).extend(messages)

                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump({"filename": filename, "input_data": merged_messages}, f, indent=2)


                # with open(filepath, "w", encoding="utf-8") as f:
                #     json.dump(
                #         {"filename": filename, "input_data": grouped_messages}, f, indent=2
                #     )
                print("*********saved the zoho messags json file locally")

                # # Upload to S3
                upload_any_file(
                    file_path=filepath, user_id=user_id, type="messages", file_name=filename
                )

                # return jsonify({"status": "ok", "new_messages": count_new})
                return {
                "status": "success",
                "new_messages": count_new
            }

    except Exception as e:
        print(f"[ERROR] → zoho fetch_mail failed: {e}")
        return {"error": str(e), "status": "failed"}

# def test_message_endpoints(account_id, folder_id, message_id, auth_token):
#     base_url = "https://mail.zoho.in/api/accounts"
#     endpoints = {
#         "originalmessage": f"{base_url}/{account_id}/messages/{message_id}/originalmessage",
#         # "header": f"{base_url}/{account_id}/folders/{folder_id}/messages/{message_id}/header",
#         # "content": f"{base_url}/{account_id}/folders/{folder_id}/messages/{message_id}/content"
#     }

#     headers = {
#         "Accept": "application/json",
#         "Authorization": f"Zoho-oauthtoken {auth_token}"
#     }

#     for name, url in endpoints.items():
#         try:
#             response = requests.get(url, headers=headers)
#             print(f"\n[{name}] → Status: {response.status_code}")
#             if response.status_code == 200:
#                 print("✅ Success! Sample response:")
#                 try:
#                     json_data = response.json()
#                     print(str(json_data)[:500])  # Preview first 500 chars
                    
#                 except Exception:
#                     print("📦 Response wasn't JSON. Raw content:")
#                     print(response.text[:500])
#             else:
#                 print(f"❌ Failed with status {response.status_code}")
#         except Exception as e:
#             print(f"🔥 Error with {name}: {e}")

@zoho_bp.route("/zoho/send_email")
def send_zoho_email(
    user_id, to_email, subject, body_text, from_user_email, conversation_id
):
    try:

        conn = connect_to_rds()
        cursor = conn.cursor()

        cursor.execute("SELECT token FROM users WHERE user_id = %s", (user_id,))
        row = cursor.fetchone()

        if not row:
            return jsonify({"error": "Access token not found for user"}), 404

        access_token = row[0]
        print("access token inside sendzoho mail :", access_token)
        account_id = get_zoho_account_id(access_token)
        print(f"account_id inside zoho {account_id}")
        send_url = f"https://mail.zoho.in/api/accounts/{account_id}/messages"
        send_data = {
            "fromAddress": from_user_email,
            "toAddress": to_email,
            "subject": subject,
            "content": body_text,
        }
        print(f"send data for zoho:{send_data}")
        headers = {
            "Authorization": f"Zoho-oauthtoken {access_token}",
            "Content-Type": "application/json",
        }
        send_response = requests.post(send_url, headers=headers, json=send_data)
        print(f"status code: {send_response.status_code}")
        print(f"response text: {send_response.text}")
        print(f"request url: {send_response.request.url}")
        print(f"request body: {send_response.request.body}")

        print(f"status code:{send_response.status_code}")
        if send_response.status_code in [200, 201]:
            result = send_response.json()
            message_id = result.get("message", {}).get("messageId", str(uuid.uuid4()))
            timestamp_dt = datetime.now(timezone.utc).isoformat()

            direction = "outbound"
            MESSAGES[message_id] = {
                "id": message_id,
                "from": from_user_email,
                "to": to_email,
                "body": body_text,
                "subject": subject,
                "timestamp": timestamp_dt,
                "status": "sent",
                "source": "zoho",
                "direction": direction,
                "conversation_id": conversation_id,
            }
        print(f"messages send in zoho are:{MESSAGES}")
        return send_response.status_code, send_response.text

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# @zoho_bp.route("/zoho/workdrive/root/<userId>", methods=["GET", "POST"])
# def get_workdrive_root(userId):
#     print(" entered in root api")
#     # user_id = request.args.get("user_id") or session.get("user_id")
#     print(f"got user id:{userId}")

#     conn = connect_to_rds()
#     cursor = conn.cursor()

#     cursor.execute("SELECT token FROM users WHERE user_id = %s", (userId,))
#     row = cursor.fetchone()

#     if not row:
#         print("cant find acces token")
#         return jsonify({"error": "Access token not found for user"}), 404

#     access_token = row[0]
#     print("access token inside /zoho/workdrive/root  :", access_token)

#     headers = {
#         "Authorization": f"Zoho-oauthtoken {access_token}",
#         "Accept": "application/vnd.api+json",
#     }

#     response = requests.get(
#         "https://www.zohoapis.in/workdrive/api/v1/teamfolders", headers=headers
#     )

#     if response.status_code != 200:
#         return (
#             jsonify({"error": "Failed to get team folders", "detail": response.text}),
#             400,
#         )

#     team_folders = response.json().get("data", [])
#     all_files_and_folders = []

#     # Step 2: Get files and folders from each team folder
#     for team_folder in team_folders:
#         team_folder_id = team_folder.get("id")

#         # Get files and folders from this team folder
#         files_response = requests.get(
#             f"https://www.zohoapis.in/workdrive/api/v1/teamfolders/{team_folder_id}/files",
#             headers=headers,
#         )

#         if files_response.status_code == 200:
#             files_data = files_response.json().get("data", [])
#             all_files_and_folders.extend(files_data)

#     return jsonify(all_files_and_folders)

# @zoho_bp.route("/zoho/workdrive/root/<userId>", methods=["GET", "POST"])
# def get_workdrive_root(userId):
#     conn = connect_to_rds()
#     cursor = conn.cursor()
#     cursor.execute("SELECT token FROM users WHERE user_id = %s", (userId,))
#     row = cursor.fetchone()
    
#     if not row:
#         return jsonify({"error": "Access token not found"}), 404
    
#     access_token = row[0]
#     headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    
    
#     results = {}
    
#     # Test many different endpoints to understand the API structure
#     endpoints_to_test = [
#         # Basic endpoints
#         ("files", "https://www.zohoapis.in/workdrive/api/v1/files"),
#         ("teams", "https://www.zohoapis.in/workdrive/api/v1/teams"),
        
#         # Try workspace-based approach
#         ("workspaces", "https://www.zohoapis.in/workdrive/api/v1/workspaces"),
#         ("my", "https://www.zohoapis.in/workdrive/api/v1/files/my"),
#         ("shared", "https://www.zohoapis.in/workdrive/api/v1/files/shared"),
        
#         # Try different team-related endpoints
#         ("team-files", "https://www.zohoapis.in/workdrive/api/v1/files/team"),
#         ("files-all", "https://www.zohoapis.in/workdrive/api/v1/files?include=all"),
        
#         # Try with different parameters
#         ("files-with-params", "https://www.zohoapis.in/workdrive/api/v1/files?page[limit]=100&include=permissions"),
#     ]
    
#     for name, endpoint in endpoints_to_test:
#         try:
#             response = requests.get(endpoint, headers=headers)
#             results[name] = {
#                 "endpoint": endpoint,
#                 "status_code": response.status_code,
#                 "response": response.json() if response.status_code == 200 else response.text[:300]
#             }
#         except Exception as e:
#             results[name] = {
#                 "endpoint": endpoint,
#                 "error": str(e)
#             }
    
#     # Also test if we can get user info to verify token is working
#     try:
#         user_info_response = requests.get("https://accounts.zoho.in/oauth/user/info", headers=headers)
#         results["user_info"] = {
#             "status_code": user_info_response.status_code,
#             "response": user_info_response.text[:200]
#         }
#     except Exception as e:
#         results["user_info"] = {"error": str(e)}
    
#     return jsonify(results)


# file: routes/zoho_workdrive.py


@zoho_bp.route("/zoho/workdrive/root/<userId>", methods=["GET", "POST"])
def get_workdrive_root(userId):
    conn = connect_to_rds()
    cursor = conn.cursor()
    cursor.execute("SELECT token FROM users WHERE user_id = %s", (userId,))
    row = cursor.fetchone()
    
    if not row:
        return jsonify({"error": "Access token not found"}), 404
    
    access_token = row[0]
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}",
     "Accept": "application/vnd.api+json"}

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
                for item in data.get('data', []):
                    folder_info = {
                        "id": item.get('id'),
                        "name": item.get('attributes', {}).get('name'),
                        "type": item.get('attributes', {}).get('type'),
                        "created_time": item.get('attributes', {}).get('created_time'),
                        "modified_time": item.get('attributes', {}).get('modified_time'),
                        "created_by": item.get('attributes', {}).get('created_by', {}).get('name', 'Unknown'),
                        "source_endpoint": endpoint
                    }
                    
                    # Only add folders, avoid duplicates
                    if (folder_info['type'] == 'folder' and 
                        not any(f['id'] == folder_info['id'] for f in team_folders)):
                        team_folders.append(folder_info)
                        
                # If we found folders, break
                if team_folders:
                    break
                    
        except Exception as e:
            continue
    
    return jsonify(
        {   "count":len(team_folders),
            "team_folders":team_folders,
            "endpoint":endpoint
        }
    )
    
    # try:
    #     # Step 1: Get team folders (not teams) - this is the correct approach
    #     team_folders_response = requests.get("https://www.zohoapis.in/workdrive/api/v1/teamfolders", headers=headers)
        
    #     if team_folders_response.status_code != 200:
    #         # Fallback: Try getting all folders and filter for team folders
    #         all_folders_response = requests.get("https://www.zohoapis.in/workdrive/api/v1/files", headers=headers)
            
    #         if all_folders_response.status_code != 200:
    #             return jsonify({"error": "Failed to fetch folders", "details": all_folders_response.text}), 400
            
    #         # Look for folders that might be team folders
    #         folders_data = all_folders_response.json()
    #         team_folders_data = {"data": []}
            
    #         for folder in folders_data.get('data', []):
    #             if folder.get('attributes', {}).get('type') == 'folder':
    #                 # This might be a team folder or regular folder
    #                 team_folders_data["data"].append(folder)
    #     else:
    #         team_folders_data = team_folders_response.json()
        
    #     all_team_files = []
        
    #     # Step 2: For each team folder, get its contents
    #     for team_folder in team_folders_data.get('data', []):
    #         folder_id = team_folder.get('id')
    #         folder_name = team_folder.get('attributes', {}).get('name', 'Unknown')
            
    #         # Get files in this team folder
    #         folder_files_url = f"https://www.zohoapis.in/workdrive/api/v1/files/{folder_id}/files"
    #         folder_files_response = requests.get(folder_files_url, headers=headers)
            
    #         if folder_files_response.status_code == 200:
    #             folder_files_data = folder_files_response.json()
                
    #             folder_info = {
    #                 "folder_id": folder_id,
    #                 "folder_name": folder_name,
    #                 "folder_type": team_folder.get('attributes', {}).get('type'),
    #                 "files": []
    #             }
                
    #             for file_item in folder_files_data.get('data', []):
    #                 file_info = {
    #                     "id": file_item.get('id'),
    #                     "name": file_item.get('attributes', {}).get('name'),
    #                     "type": file_item.get('attributes', {}).get('type'),
    #                     "size": file_item.get('attributes', {}).get('size'),
    #                     "created_time": file_item.get('attributes', {}).get('created_time'),
    #                     "modified_time": file_item.get('attributes', {}).get('modified_time'),
    #                     "created_by": file_item.get('attributes', {}).get('created_by', {}).get('name'),
    #                     "is_folder": file_item.get('attributes', {}).get('type') == 'folder'
    #                 }
    #                 folder_info["files"].append(file_info)
                
    #             all_team_files.append(folder_info)
    #         else:
    #             # Even if we can't get files, include the folder info
    #             all_team_files.append({
    #                 "folder_id": folder_id,
    #                 "folder_name": folder_name,
    #                 "folder_type": team_folder.get('attributes', {}).get('type'),
    #                 "files": [],
    #                 "error": f"Could not fetch files: {folder_files_response.text[:100]}"
    #             })
        
        # return jsonify({
        #     "success": True,
        #     "folders_count": len(all_team_files),
        #     "folders": all_team_files
        # })
        
    # except Exception as e:
    #     return jsonify({"error": str(e)}), 500


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
        response = requests.get("https://www.zohoapis.in/workdrive/api/v1/teamfolders", headers=headers)
        return jsonify({
            "status_code": response.status_code,
            "endpoint": "teamfolders",
            "response": response.json() if response.status_code == 200 else response.text
        })
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
            "method": "GET"
        },
        # Approach 2: Try to get private workspace
        {
            "name": "private_workspace", 
            "url": "https://www.zohoapis.in/workdrive/api/v1/privateworkspace",
            "method": "GET"
        },
        # Approach 3: Try to get folders only
        {
            "name": "folders_only",
            "url": "https://www.zohoapis.in/workdrive/api/v1/files",
            "method": "GET",
            "params": {"filter[type]": "folder"}
        },
        # Approach 4: Try different files endpoint
        {
            "name": "files_recursive",
            "url": "https://www.zohoapis.in/workdrive/api/v1/files",
            "method": "GET", 
            "params": {"page[limit]": "50"}
        }
    ]
    
    for approach in approaches:
        try:
            params = approach.get("params", {})
            response = requests.get(approach["url"], headers=headers, params=params)
            
            results[approach["name"]] = {
                "status_code": response.status_code,
                "url": approach["url"],
                "response": response.json() if response.status_code == 200 else response.text[:500]
            }
            
            # If we found data, try to drill down
            if response.status_code == 200 and response.json().get('data'):
                data = response.json()['data']
                for item in data[:3]:  # Check first 3 items
                    item_id = item.get('id')
                    if item_id:
                        # Try to get contents of this item
                        sub_url = f"https://www.zohoapis.in/workdrive/api/v1/files/{item_id}/files"
                        sub_response = requests.get(sub_url, headers=headers)
                        results[f"{approach['name']}_sub_{item_id}"] = {
                            "status_code": sub_response.status_code,
                            "url": sub_url,
                            "parent_name": item.get('attributes', {}).get('name', 'Unknown'),
                            "response": sub_response.json() if sub_response.status_code == 200 else sub_response.text[:300]
                        }
                        
        except Exception as e:
            results[approach["name"]] = {"error": str(e)}
    
    return jsonify(results)




def xor_encrypt(data, key):
    encrypted = bytes([b ^ ord(key[i % len(key)]) for i, b in enumerate(data.encode())])
    return base64.b64encode(encrypted).decode()


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


def token_expired(expiry_str: str) -> bool:
    try:
        if not expiry_str or expiry_str.startswith("0000-00-00"):
            return True

        expiry = datetime.strptime(expiry_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        now = datetime.now(timezone.utc)

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

    # if token_expired(user["expiry"]):   uncomment later
    #     # Refresh the token
    #     response = requests.post("https://accounts.zoho.com/oauth/v2/token", data={
    #         "refresh_token": user["refresh_token"],
    #         "client_id": os.environ["ZOHO_CLIENT_ID"],
    #         "client_secret": os.environ["ZOHO_CLIENT_SECRET"],
    #         "grant_type": "refresh_token"
    #     })
    #     tokens = response.json()
    #     access_token = tokens["access_token"]
    #     expires_in = tokens["expires_in"]

    #     cursor.execute(
    #         "UPDATE users SET token=%s, expiry=%s WHERE user_id=%s",
    #         (access_token, expires_in, user_id)
    #     )
    #     conn.commit()
    else:
        access_token = user[0]

    conn.close()

    # access_token = "1000.202e903bdd36eff07b1e32a0191f2f05.f325e55fe60d0599b56f2d186f461d93"
    return jsonify({"access_token": access_token})
