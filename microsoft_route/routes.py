from utils.base_logger import get_logger
from db.db_checkers import fetch_userid_from_launch
from flask import Blueprint, request, jsonify, session, redirect
from msal import ConfidentialClientApplication
import os
import requests
from db.rds_db import connect_to_rds
from data import MESSAGES  # delete this later, this is just for testing
import uuid
from data import MESSAGES  # delete this later
from bs4 import BeautifulSoup
import uuid
from datetime import datetime, timezone
from agent_route.routes import process_and_update_yaml
from utils.chatopenzz import check_lancedb
import asyncio
from db.db_checkers import check_onboarding_user
import pymysql

microsoft_bp = Blueprint("microsoft", __name__)
logger = get_logger(__name__)
CLIENT_ID = os.environ.get("MICROSOFT_CLIENT_ID")
CLIENT_SECRET = os.environ.get("MICROSOFT_CLIENT_SECRET")
TENANT_ID = os.environ.get("MICROSOFT_TENANT_ID")
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
REDIRECT_URI = "https://bytoid.ai/microsoft/callback"
SCOPES = ["User.Read", "Mail.Send", "Mail.ReadWrite"]

msal_app = ConfidentialClientApplication(
    client_id=CLIENT_ID, client_credential=CLIENT_SECRET, authority=AUTHORITY
)


@microsoft_bp.route("/microsoft/login", methods=["GET"])
def microsoft_login():

    flow = msal_app.initiate_auth_code_flow(
        scopes=SCOPES,
        redirect_uri="https://rtdtj5q9dh.execute-api.ca-central-1.amazonaws.com/microsoft/callback",
    )

    session["auth_flow"] = flow
    return redirect(flow["auth_uri"])
    # return {"message":"api works"}


@microsoft_bp.route("/microsoft/callback")
def microsoft_callback():

    flow = session.pop("auth_flow", None)
    if not flow:
        #     return "Flow not found ", 400
        return redirect("https://bytoid.ai/login")

    result = msal_app.acquire_token_by_auth_code_flow(flow, request.args, scopes=SCOPES)

    if "access_token" not in result:
        return (
            jsonify({"error": "Failed to obtain access token", "details": result}),
            400,
        )

    # Retrieve user information
    id_token_claims = result.get("id_token_claims", {})
    if not id_token_claims:
        return (
            jsonify(
                {
                    "error": "Failed to obtain id token claims",
                    "details": id_token_claims,
                }
            ),
            400,
        )

    session["user"] = {
        "id": id_token_claims.get("oid"),
        "name": id_token_claims.get("name"),
        "email": id_token_claims.get("preferred_username"),
    }

    access_token = result["access_token"]
    refresh_token = result["refresh_token"]
    # print("access_token microsoft:", access_token)
    # print("access_token microsoft length:", len(access_token))
    # print("access_token microsoft length:", len(refresh_token))

    headers = {"Authorization": f"Bearer {access_token}"}

    userinfo_response = requests.get(
        "https://graph.microsoft.com/v1.0/me", headers=headers
    )

    if userinfo_response.status_code == 200:
        userinfo = userinfo_response.json()
        email = userinfo.get("mail")
        given_name = userinfo.get("givenName")
        family_name = userinfo.get("surname")
        user_id = userinfo.get("id")
        print("email from microsoft", email)

        conn = connect_to_rds()
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        # cursor.execute("SELECT refresh_token FROM users WHERE email = %s", (email,))
        cursor.execute("SELECT user_id,user_type FROM users WHERE email = %s", (email,))
        row = cursor.fetchone()
        print("db row ", row)

        access_token_ = result.get("access_token") or ""
        print("stored access_token_: ", access_token_)

        refresh_token_ = result.get("refresh_token")
        expires_in_ = result.get("expires_in")

        if not row:
            print("nmew user")

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
                    CLIENT_ID,
                    CLIENT_SECRET,
                    access_token_,
                    refresh_token_,
                    expires_in_,
                    "",
                    "",
                    "",
                    "microsoft",
                    None,
                    None,
                    None,
                    None,
                    None,
                ),
            )

        else:
            logger.info("users update data")
            prev_id = row.get("user_id", "NODATA")
            logger.info("Microsoft prev-> %s", prev_id)
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
                        CLIENT_ID,
                        CLIENT_SECRET,
                        access_token_,
                        refresh_token_,
                        expires_in_,
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
                        CLIENT_ID,
                        CLIENT_SECRET,
                        access_token_,
                        refresh_token_,
                        expires_in_,
                        email,
                    ),
                )

        conn.commit()
        conn.close()

        if row:
            prev_type = row.get("user_type", "NO TYPE")
            logger.info("Microsoft prev UserType -> %s", prev_type)
            if prev_type == "user":
                logger.info("Invited User Logged in")
                return redirect(
                    f"https://bytoid.ai/auth/microsoft/callback?userid={user_id}&user_onboarded={True}"
                )

        newuser = check_onboarding_user(user_id)
        # else:
        #     return redirect("https://bytoid.ai/login")
        print(f"going to return to dashboard: {newuser}")
        return redirect(
            f"https://bytoid.ai/auth/microsoft/callback?userid={user_id}&user_onboarded={newuser}"
        )

        # return jsonify({"user_id": user_id, "user_onboarded": newuser})

    else:
        return (
            jsonify(
                {
                    "error": "Failed to get user info from Microsoft",
                    "details": "Microsoft Graph API request failed",
                }
            ),
            400,
        )
    # return redirect('https://bytoid.ai/dashboard')
    # return jsonify(result)


@microsoft_bp.route("/microsoft/get_email")
def microsoft_get_email():

    try:
        conn = connect_to_rds()
        cursor = conn.cursor()

        email = session.get("user", {}).get("email")
        print(email)

        if not email:
            return redirect("https://bytoid.ai/login")

        cursor.execute("SELECT token FROM users WHERE email = %s", (email,))
        row = cursor.fetchone()

        if not row:
            return jsonify({"error": "Access token not found for user"}), 404

        access_token = row[0]
        # print("access token :", access_token)
        headers = {"Authorization": f"Bearer {access_token}"}
        url = "https://graph.microsoft.com/v1.0/me/messages"

        response = requests.get(url, headers=headers)
        print(response)

        cursor.close()
        conn.close()

        if response.status_code == 200:

            user_email = "riya@bytoid.io"

            ####################################
            # headers = {
            #     "Authorization": f"Bearer {access_token}",
            #     "Accept": "application/json"
            # }

            # url = f"https://graph.microsoft.com/v1.0/users/{user_email}/mailFolders"

            # try:
            #     resp = requests.get(url, headers=headers)
            #     resp.raise_for_status()

            #     folders = resp.json()
            #     for f in folders.get("value", []):
            #         print("Folder Name:", f["displayName"])
            #         print("Folder ID:  ", f["id"])
            #         print("-" * 40)

            # except requests.exceptions.HTTPError as e:
            #     print("HTTP error:", e)
            #     print("Response text:", resp.text)

            ###########################################

            emails = response.json().get("value", [])

            for email_data in emails:
                # print(f"email_data: {email_data}")
                email_id = email_data.get("id")
                from_name = (
                    email_data.get("sender", {}).get("emailAddress", {}).get("name")
                )
                to_recipients = email_data.get("toRecipients", [])
                to_addr = ", ".join(
                    [r["emailAddress"]["address"] for r in to_recipients]
                )

                body_content = email_data.get("body", {}).get("content")
                # conversation_id = email_data.get("conversationId")
                internet_message_id = email_data.get("internetMessageId")
                soup = BeautifulSoup(body_content, "html.parser")
                plain_text = soup.get_text().strip()
                subject = email_data.get("subject")
                sent_time = email_data.get("sentDateTime")

                from_address = (
                    email_data.get("from", {})
                    .get("emailAddress", {})
                    .get("address", "")
                )
                direction = (
                    "inbound" if from_address.lower() != email.lower() else "outbound"
                )
                from_email = email_data["from"]["emailAddress"]["address"]
                to_email = email_data["toRecipients"][0]["emailAddress"]["address"]

                conversation_id = from_email if direction == "inbound" else to_email

                message_id = str(uuid.uuid4())
                MESSAGES[email_id] = {
                    "id": message_id,
                    "from": email_data["from"]["emailAddress"]["name"],
                    "to": email_data["toRecipients"][0]["emailAddress"]["name"],
                    "to_email": to_email,  # remove after session is working
                    "from_email": from_email,
                    "body": plain_text,
                    "subject": subject,
                    "timestamp": sent_time,
                    "status": "received",
                    "source": "outlook",
                    "direction": direction,
                    "conversation_id": conversation_id,
                    "internet_message_id": internet_message_id,
                }
                # print(MESSAGES)
            return jsonify({"stored_messages": list(MESSAGES.keys())})
        else:
            return (
                jsonify({"error": "Failed to fetch emails", "details": response.text}),
                response.status_code,
            )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


def send_outlook_email(to_email, subject, body_text, from_user_email, conversation_id):

    print("Sending Outlook email...")
    print(f"To: {to_email}, Subject: {subject}, Body: {body_text}")

    # email = session.get("user", {}).get("email")  # remove after session is working
    email = from_user_email
    if not email:
        raise Exception("No user email found in session.")

    # Fetch token
    conn = connect_to_rds()
    cursor = conn.cursor()
    cursor.execute("SELECT token FROM users WHERE email = %s", (email,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if not row:
        raise Exception("Access token not found for user.")

    access_token = row[0]

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body_text},
            "toRecipients": [{"emailAddress": {"address": to_email}}],
        },
        "saveToSentItems": "true",
    }

    if from_user_email:
        url = f"https://graph.microsoft.com/v1.0/users/{from_user_email}/sendMail"
        print(f"Using from_user_email: {from_user_email}")
    else:
        url = "https://graph.microsoft.com/v1.0/me/sendMail"
        print("Using current user's email for sending.")

    # Send mail
    response = requests.post(url, headers=headers, json=payload)

    if not response.ok:
        print("Graph API returned an error!")
        print("Status:", response.status_code)
        print("Response text:", response.text)
        response.raise_for_status()
        response.raise_for_status()

    # Fetch last sent message to get conversationId
    if from_user_email:
        sent_items_url = f"https://graph.microsoft.com/v1.0/users/{from_user_email}/mailFolders/sentitems/messages?$orderby=sentDateTime desc&$top=1"
    else:
        sent_items_url = "https://graph.microsoft.com/v1.0/me/mailFolders/sentitems/messages?$orderby=sentDateTime desc&$top=1"
    sent_response = requests.get(sent_items_url, headers=headers)
    sent_response.raise_for_status()

    sent_data = sent_response.json()
    if sent_data.get("value"):
        last_msg = sent_data["value"][0]

        # conversation_id = last_msg.get("conversationId")
        real_message_id = last_msg.get("id")
        timestamp = last_msg.get("sentDateTime")
        subject_saved = last_msg.get("subject", subject)
        body_saved = last_msg.get("body", {}).get("content", body_text)
        message_id = str(uuid.uuid4())

        # Save the message to MESSAGES
        MESSAGES[real_message_id] = {
            "id": message_id,
            "from": email,
            "to": to_email,
            "body": body_saved,
            "subject": subject_saved,
            "timestamp": timestamp,
            "status": "sent",
            "source": "outlook",
            "direction": "outbound",
            "user_id": email,
            "conversation_id": conversation_id,
        }

        print(
            f"✅ Saved Outlook message. Conversation ID: {conversation_id} messages:{MESSAGES[real_message_id]}"
        )

        return {"id": message_id, "conversation_id": conversation_id}

    else:
        # fallback if no message found
        fallback_id = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        MESSAGES[fallback_id] = {
            "id": fallback_id,
            "from": email,
            "to": to_email,
            "body": body_text,
            "subject": subject,
            "timestamp": timestamp,
            "status": "sent",
            "source": "outlook",
            "direction": "outbound",
            "user_id": email,
            "conversation_id": None,
            "message_id": fallback_id,
        }

        print(
            f"⚠️ Could not find sent mail. Saved fallback message with no conversation ID."
        )
        return {"id": fallback_id}


@microsoft_bp.route("/microsoft/send_mail", methods=["POST"])
def send_mail_microsoft():

    user = session.get("user", {})
    email = user.get("email")

    if not email:
        return redirect("https://bytoid.ai/login")

    #################################
    # data = request.get_json()
    # to = data.get("to")
    # subject = data.get("subject")
    # body = data.get("body")
    #################################

    to = "test@example.com"
    subject = "subject"
    body = "body"

    if not to or not subject or not body:
        return jsonify({"error": "Missing 'to', 'subject', or 'body'"}), 400

    try:
        conn = connect_to_rds()
        cursor = conn.cursor()
        cursor.execute("SELECT token FROM users WHERE email = %s", (email,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if not row:
            return jsonify({"error": "Access token not found"}), 404

        access_token = row[0]

        send_payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": body},
                "toRecipients": [{"emailAddress": {"address": to}}],
            },
            "saveToSentItems": True,
        }

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        response = requests.post(
            "https://graph.microsoft.com/v1.0/me/sendMail",
            headers=headers,
            json=send_payload,
        )

        if response.status_code == 202:
            return jsonify({"message": "Email sent"}), 200
        else:
            return (
                jsonify({"error": "Failed to send email", "details": response.text}),
                response.status_code,
            )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@microsoft_bp.route("/microsoft/sent_items", methods=["GET"])
def microsoft_sent_items():
    try:
        email = session.get("user", {}).get("email")
        if not email:
            return redirect("https://bytoid.ai/login")

        conn = connect_to_rds()
        cursor = conn.cursor()
        cursor.execute("SELECT token FROM users WHERE email = %s", (email,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if not row:
            return jsonify({"error": "Access token not found"}), 404

        access_token = row[0]
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        drafts_url = (
            "https://graph.microsoft.com/v1.0/me/mailFolders/SentItems/messages"
        )
        response = requests.get(drafts_url, headers=headers)

        if response.status_code == 200:
            emails = response.json().get("value", [])
            for email_data in emails:
                email_id = email_data.get("id")
                from_name = (
                    email_data.get("sender", {}).get("emailAddress", {}).get("name")
                )
                to_recipients = email_data.get("toRecipients", [])
                to_name = (
                    to_recipients[0]["emailAddress"]["name"] if to_recipients else None
                )
                body_content = email_data.get("body", {}).get("content")
                subject = email_data.get("subject")
                sent_time = email_data.get("sentDateTime")

                message_id = str(uuid.uuid4())
                MESSAGES[email_id] = {
                    "id": message_id,
                    "from": from_name,
                    "to": to_name,
                    "body": body_content,
                    "subject": subject,
                    "timestamp": sent_time,
                    "status": "sent",
                    "source": "outlook",
                    "direction": "outbound",
                }
            else:
                return (
                    jsonify(
                        {
                            "error": "Failed to fetch sent items",
                            "details": response.text,
                        }
                    ),
                    response.status_code,
                )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@microsoft_bp.route("/microsoft/drafts", methods=["GET"])
def microsoft_list_drafts():
    try:
        email = session.get("user", {}).get("email")
        if not email:
            return redirect("https://bytoid.ai/login")

        conn = connect_to_rds()
        cursor = conn.cursor()
        cursor.execute("SELECT token FROM users WHERE email = %s", (email,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if not row:
            return jsonify({"error": "Access token not found"}), 404

        access_token = row[0]
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        drafts_url = "https://graph.microsoft.com/v1.0/me/mailFolders/drafts/messages"
        response = requests.get(drafts_url, headers=headers)

        if response.status_code == 200:
            return jsonify({"drafts": response.json().get("value", [])}), 200
        else:
            return (
                jsonify({"error": "Failed to fetch drafts", "details": response.text}),
                response.status_code,
            )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@microsoft_bp.route("/microsoft/spam", methods=["GET"])
def microsoft_list_spam():
    try:
        email = session.get("user", {}).get("email")
        if not email:
            return redirect("https://bytoid.ai/login")

        conn = connect_to_rds()
        cursor = conn.cursor()
        cursor.execute("SELECT token FROM users WHERE email = %s", (email,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if not row:
            return jsonify({"error": "Access token not found"}), 404

        access_token = row[0]
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        drafts_url = (
            "https://graph.microsoft.com/v1.0/me/mailFolders/JunkEmail/messages"
        )
        response = requests.get(drafts_url, headers=headers)

        if response.status_code == 200:
            return jsonify({"spam": response.json().get("value", [])}), 200
        else:
            return (
                jsonify({"error": "Failed to fetch spam", "details": response.text}),
                response.status_code,
            )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@microsoft_bp.route("/microsoft/trash", methods=["GET"])
def microsoft_list_trash():
    try:
        email = session.get("user", {}).get("email")
        if not email:
            return redirect("https://bytoid.ai/login")

        conn = connect_to_rds()
        cursor = conn.cursor()
        cursor.execute("SELECT token FROM users WHERE email = %s", (email,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if not row:
            return jsonify({"error": "Access token not found"}), 404

        access_token = row[0]
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        drafts_url = (
            "https://graph.microsoft.com/v1.0/me/mailFolders/DeletedItems/messages"
        )
        response = requests.get(drafts_url, headers=headers)

        if response.status_code == 200:
            return jsonify({"trash": response.json().get("value", [])}), 200
        else:
            return (
                jsonify({"error": "Failed to fetch trash", "details": response.text}),
                response.status_code,
            )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@microsoft_bp.route("/process-outlook", methods=["POST"])
def process_outlook():
    try:
        ok, val = check_lancedb()
        if not ok:
            logger.info(f"LanceDB service down: {val}")
        data = request.get_json(force=True, silent=True)
        if not data:
            return jsonify({"error": "Invalid or missing JSON body"}), 400

        apikey = data.get("api_key")
        if not apikey:
            return jsonify({"error": "Missing api_key"}), 400

        userid = fetch_userid_from_launch(apikey)
        if not userid:
            return jsonify({"error": "Invalid API key or user not found"}), 401

        files = data.get("files")
        if not files or not isinstance(files, list):
            return (
                jsonify({"error": "Invalid or missing 'files' (must be a list)"}),
                400,
            )

        pathdown = f"data/{userid}/outlook"
        os.makedirs(pathdown, exist_ok=True)
        all_downloaded_paths = []
        file_errors = []

        # Download files
        for file_info in files:
            file_url = file_info.get("downloadUrl")
            file_name = file_info.get("name")

            if not file_url or not file_name:
                file_errors.append(
                    {
                        "file": file_name or "unknown",
                        "error": "Missing file_url or file_name",
                    }
                )
                continue

            try:
                resp = requests.get(file_url, timeout=30)
                resp.raise_for_status()
            except requests.RequestException as e:
                file_errors.append({"file": file_name, "error": str(e)})
                continue

            save_path = os.path.join(pathdown, file_name)
            try:
                with open(save_path, "wb") as f:
                    f.write(resp.content)
                all_downloaded_paths.append(save_path)
            except OSError as e:
                file_errors.append(
                    {"file": file_name, "error": f"File save error: {str(e)}"}
                )

        if not all_downloaded_paths:
            return (
                jsonify(
                    {
                        "error": "No files downloaded successfully",
                        "details": file_errors,
                    }
                ),
                400,
            )

        # Process files
        try:
            all_file_data = asyncio.run(
                process_and_update_yaml(
                    all_downloaded_paths=all_downloaded_paths,
                    userid=userid,
                    provider="outlook",
                    folderpath=pathdown,
                )
            )
        except Exception as e:
            return jsonify({"error": f"Error processing files: {str(e)}"}), 500

        return (
            jsonify(
                {
                    "message": "Files processed",
                    "files": all_file_data,
                    "failed_files": file_errors,
                }
            ),
            200,
        )

    except Exception as e:
        return jsonify({"error": f"Unexpected server error: {str(e)}"}), 500


@microsoft_bp.route("/logout")
def microsoft_logout():
    user = session.get("user")

    if not user:
        return jsonify({"error": "No user is currently logged in"}), 400

    user_id = user.get("id")
    session.pop("user")

    return jsonify({"status": "User logged out", "user_id": user_id}), 200
