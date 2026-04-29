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
from umail_helper.helper import (
    get_users_client_id,
    extract_reply_content,
    get_last_sync_time_zoho,
    set_user_sync_time_zoho,
    update_user_message_cache,
)
from cust_helpers import pathconfig
from utils.normal import ensure_dir
import json
from email.utils import getaddresses
from gmail_route.routes import add_lead_contact, add_customer_contact, safe_json_load
from umail_helper.ticketalloc import TicketAllocator
from services.redis_service import get_redis
from db.db_checkers import update_umail_json
import shutil
from services.credit_system import CreditManager


zoho_bp = Blueprint("zoho", __name__)
# app.secret_key = "your_secret_key"  # Required for sessions

CLIENT_ID = os.environ.get("ZOHO_CLIENT_ID")
CLIENT_SECRET = os.environ.get("ZOHO_CLIENT_SECRET")
frontend_url = os.getenv("BASE_FRNT_URL")
REDIRECT_URI = f"{frontend_url}/auth/zoho/callback"
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
    print("inside zoho_login")
    return redirect(auth_url)


@zoho_bp.route("/zoho/callback", methods=["POST", "GET"])
def zoho_callback():
    # print("inside zoho_callback ")
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
    # print(f"email for zoho : {email}")
    cursor.execute("SELECT 1 FROM users WHERE email = %s", (email,))
    row = cursor.fetchone()

    access_token_ = access_token or ""
    expires_in = tokens.get("expires_in")

    if not row:
        # print("user not present. inserting....")
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
        # print("user already present. updating...")
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

    newuser = check_onboarding_user(id)

    # check if credits are available
    credits = CreditManager(conn)
    avail_credits = credits.check_if_remaining(user_id=id)
    credit_status = avail_credits.get("status")
    message = avail_credits.get("message")

    conn.commit()
    conn.close()

    return jsonify(
        {
            "user_id": id,
            "user_onboarded": newuser,
            "credit_status": credit_status,
            "message": message,
        }
    )

    # return redirect(f"https://bytoid.ai/auth/zoho/callback?user_id={id}&onboarded={str(newuser).lower()}")

    # return redirect("https://bytoid.ai/radar")


def get_zoho_account_id(access_token):
    mail_headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    mail_list_url = "https://mail.zoho.in/api/accounts"

    try:
        accounts_response = requests.get(mail_list_url, headers=mail_headers)

        try:
            accounts = accounts_response.json()
        except Exception as parse_err:
            # print(f"[ERROR] Failed to parse Zoho account response JSON: {parse_err}")
            # print(f"[DEBUG] Raw response text: {accounts_response.text}")
            return None

        if "data" not in accounts:
            # print(f"[ERROR] No 'data' field in Zoho response")
            return None

        if not accounts["data"]:
            # print(f"[ERROR] Empty 'data' list returned from Zoho")
            return None

        account_id = accounts["data"][0].get("accountId")
        if not account_id:
            # print(
            #     f"[ERROR] 'accountId' missing in first data item: {accounts['data'][0]}"
            # )
            return None

        # print(f"[INFO] Successfully retrieved Zoho account_id: {account_id}")
        return account_id

    except Exception as e:
        import traceback

        # print("❌ Exception in get_zoho_account_id:")
        # print(traceback.format_exc())
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


def get_zoho_thread_count_dynamic(user_id):
    try:
        conn = connect_to_rds()
        cursor = conn.cursor()

        # 1️⃣ Fetch user + tokens + watermark
        cursor.execute(
            """
            SELECT token, refresh_token, email
            FROM users
            WHERE user_id = %s
            """,
            (user_id,),
        )
        row = cursor.fetchone()

        if not row:
            return {"error": "User not found"}, 404

        access_token, refresh_token, user_email = row

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        last_synced_at = get_last_sync_time_zoho(user_id)
        # 🔹 If first sync → only last 7 days
        if last_synced_at is None:
            last_synced_at = now_ms - (7 * 24 * 60 * 60 * 1000)

        # print(f"last_synced_at : {last_synced_at}")

        # 2️⃣ Resolve Zoho account ID
        account_id = get_zoho_account_id(access_token)

        headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}

        start = 0
        limit = 200
        newest_received_time = last_synced_at
        has_more = True
        results = 0

        while has_more:
            params = {
                "start": start,
                "limit": limit,
            }

            url = f"https://mail.zoho.in/api/accounts/{account_id}/messages/view"
            resp = requests.get(url, headers=headers, params=params)

            # 🔁 Handle token expiry
            if resp.status_code == 401:
                tokens = refresh_zoho_token(refresh_token, CLIENT_ID, CLIENT_SECRET)
                access_token = tokens["access_token"]

                cursor.execute(
                    "UPDATE users SET token = %s WHERE user_id = %s",
                    (access_token, user_id),
                )
                conn.commit()

                headers["Authorization"] = f"Zoho-oauthtoken {access_token}"
                resp = requests.get(url, headers=headers, params=params)

            if resp.status_code != 200:
                raise Exception(f"Zoho API failed: {resp.text}")

            mails = resp.json().get("data", [])
            if not mails:
                len = 0
            else:
                len = len(mails)
            results += len
        # print(f"number of messages: {results}")

        return results

    except Exception as e:
        # print("Zoho mail sync failed")
        return {"status": "failed", "error": str(e)}


# @zoho_bp.route("/zoho/get_email/<user_id>")
def fetch_zoho_emails(user_id):
    try:
        conn = connect_to_rds()
        cursor = conn.cursor()

        # 1️⃣ Fetch user + tokens + watermark
        cursor.execute(
            """
            SELECT token, refresh_token, email
            FROM users
            WHERE user_id = %s
            """,
            (user_id,),
        )
        row = cursor.fetchone()

        if not row:
            return {"error": "User not found"}, 404

        access_token, refresh_token, user_email = row

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        last_synced_at = get_last_sync_time_zoho(user_id)
        # 🔹 If first sync → only last 7 days
        if last_synced_at is None:
            last_synced_at = now_ms - (7 * 24 * 60 * 60 * 1000)

        # print(f"last_synced_at : {last_synced_at}")

        # 2️⃣ Resolve Zoho account ID
        account_id = get_zoho_account_id(access_token)

        headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}

        start = 0
        limit = 200
        newest_received_time = last_synced_at
        has_more = True
        results = []

        while has_more:
            params = {
                "start": start,
                "limit": limit,
            }

            url = f"https://mail.zoho.in/api/accounts/{account_id}/messages/view"
            resp = requests.get(url, headers=headers, params=params)

            # 🔁 Handle token expiry
            if resp.status_code == 401:
                tokens = refresh_zoho_token(refresh_token, CLIENT_ID, CLIENT_SECRET)
                access_token = tokens["access_token"]

                cursor.execute(
                    "UPDATE users SET token = %s WHERE user_id = %s",
                    (access_token, user_id),
                )
                conn.commit()

                headers["Authorization"] = f"Zoho-oauthtoken {access_token}"
                resp = requests.get(url, headers=headers, params=params)

            if resp.status_code != 200:
                raise Exception(f"Zoho API failed: {resp.text}")

            mails = resp.json().get("data", [])
            if not mails:
                break
            # print(f"mails : {mails}")
            for mail in mails:
                received_time = int(mail.get("receivedTime", 0))
                # print(f"received_time : {received_time}")

                # ⛔ Stop once we cross watermark
                if received_time <= last_synced_at:
                    has_more = False
                    break

                message_id = mail.get("messageId")
                thread_id = mail.get("threadId") or mail.get("messageId")
                conversationId = thread_id

                # Dedup safeguard
                cursor.execute(
                    "SELECT 1 FROM messages WHERE message_id = %s",
                    (message_id,),
                )
                if cursor.fetchone():
                    continue

                try:
                    folderid = mail.get("folderId")
                    content_url = f"https://mail.zoho.in/api/accounts/{account_id}/folders/{folderid}/messages/{message_id}/content"
                    resp = requests.get(
                        content_url,
                        headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
                    )
                    full_body = resp.json()["data"]["content"]
                except Exception as e:
                    print(f"error in getting body: {e}")
                subject = mail.get("subject")
                from_addr = html.unescape(mail.get("fromAddress", ""))

                # ---- to_emails ------------
                raw = mail.get("toAddress", "")
                decoded = html.unescape(raw)
                addresses = getaddresses([decoded])
                emails = [email for _, email in addresses]
                to_emails = emails
                # ----------------------------

                from_email = mail.get("fromAddress")
                from_name = mail.get("Sender")

                direction = (
                    "inbound"
                    if from_email.lower() != user_email.lower()
                    else "outbound"
                )

                participant = from_email if direction == "inbound" else to_email
                participant_name = from_name if direction == "inbound" else to_name

                message = {
                    "id": message_id,
                    "subject": subject,
                    "conversationId": conversationId,
                    "threadId": thread_id,
                    "receivedDateTime": received_time,
                    "body": full_body,
                    "from": from_email,
                    "from_name": from_name,
                    "plain_text": full_body,
                    "to_emails": to_emails,
                    "direction": direction,
                }
                results.append(message)

                newest_received_time = max(newest_received_time, received_time)

            start += limit

        return results

    except Exception as e:
        # print("Zoho mail sync failed")
        return {"status": "failed", "error": str(e)}


async def v2fetch_zoho_messages_batch(user_id, connection):
    """
    Fetch a single batch of Zoho messages dynamically using conversation IDs.
    - user_id: external user id
    - my_email: user's primary email (for direction/conversation assignment)
    - batch_count: logging/debug counter
    - connection: optional DB connection; if None, function will create & close one
    Returns:
      - status
      - new_messages
      - next_page_token (always None)
      - grouped_messages
    """
    from collections import defaultdict
    import os
    import json
    import uuid
    from datetime import datetime, timezone
    from email.utils import parsedate_to_datetime

    new_connection = None
    try:
        # Ensure DB connection
        if connection is None:
            new_connection = connect_to_rds()
            connection = new_connection

        cursor = connection.cursor()
        # print(f"🚀 Starting zoho mail fetch for user {user_id}")

        results = fetch_zoho_emails(user_id)

        if not results:
            # print("⚠️ No conversations found for user")
            return {
                "status": "success",
                "new_messages": 0,
                "next_page_token": None,
                "grouped_messages": {},
            }

        count_new = 0
        grouped_messages = defaultdict(list)

        # File setup (same pattern as Gmail)
        timestamp = datetime.now(timezone.utc)
        date_str = timestamp.strftime("%Y-%m-%d")
        filename = f"{date_str}.json"

        user_folder = os.path.join(pathconfig.basepath, "messages", user_id)
        ensure_dir(user_folder)
        filepath = os.path.join(user_folder, filename)

        # Load existing data for dedup / reprocess checks
        existing_ids_local = set()
        input_data_local = {}
        if os.path.exists(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    existing_data_local = json.load(f)
                    input_data_local = existing_data_local.get("input_data", {})
                # Extract existing message IDs
                for client_data in input_data_local.values():
                    if isinstance(client_data, dict):
                        for client_channels in client_data.values():
                            if isinstance(client_channels, dict):
                                for channel_msgs in client_channels.values():
                                    if isinstance(channel_msgs, list):
                                        for msg in channel_msgs:
                                            if isinstance(msg, dict) and "id" in msg:
                                                existing_ids_local.add(msg["id"])
            except Exception as e:
                print(f"⚠️ Error loading existing data: {e}")

        # print(f"************* before db pre checks")

        # DB pre-checks
        email_to_client_id = {}
        configs_created = set()
        first_time_user = True

        # if integration:
        #     cursor.execute(
        #         "SELECT m.message_id, th.conversation_id FROM messages m JOIN threads th ON m.conversation_id_fk = th.conversation_id WHERE th.external_user_id = %s",
        #         (primary_user_id,),
        #     )
        # else:
        cursor.execute(
            "SELECT m.message_id, th.conversation_id FROM messages m JOIN threads th ON m.conversation_id_fk = th.conversation_id WHERE th.external_user_id = %s",
            (user_id,),
        )

        rows = cursor.fetchall()
        if rows:
            first_time_user = False

        # --- STEP 2: process each conversation ---
        for msg in results:
            message_id = msg.get("messageId") or msg.get("id") or str(uuid.uuid4())
            # if integration:
            #     row_id = f"{primary_user_id}_{message_id}"
            # else:
            row_id = f"{user_id}_{message_id}"

            # Skip existing in DB check (still allow reprocessing if old)

            cursor.execute(
                """
                        SELECT m.conversation_id_fk, m.sender_id, t.external_user_id
                        FROM messages m
                        JOIN threads t ON m.conversation_id_fk = t.conversation_id
                        WHERE m.message_id = %s
                        """,
                (row_id,),
            )

            row = cursor.fetchone()
            # if row and row[2] == (user_id):
            #     #print(f"message {message_id} already exists in DB for user")

            # Check local cache for recent messages to skip
            if row_id in existing_ids_local:
                try:
                    msg_time = parsedate_to_datetime(msg.get("date"))
                    if (datetime.now(timezone.utc) - msg_time).total_seconds() < 3600:
                        # print(f"skipping recent message {message_id}")
                        continue
                    existing_ids_local.discard(row_id)
                except Exception:
                    pass

            # Normalize fields
            try:
                dt = parsedate_to_datetime(msg.get("date"))
            except Exception:
                dt = datetime.now(timezone.utc)
            timestamp_iso = dt.isoformat()
            direction = msg.get("direction", "inbound")
            body_content = msg.get("body", "")
            plain_text = msg.get("plain_text", "")
            subject = msg.get("subject", "")

            from_email = msg.get("from", "")
            from_name = msg.get("from_name", "")
            to_emails = msg.get("toRecipients", [])
            to_names = msg.get("to_names", [])

            thread_id = msg.get("conversationId")

            conversation_id = f"{user_id}_{thread_id}"

            participant = (
                from_email
                if direction == "inbound"
                else (to_emails[0] if to_emails else None)
            )
            participant_name = (
                from_name
                if direction == "inbound"
                else (to_names[0] if to_names else "")
            )

            # Get or create client
            if participant in email_to_client_id:
                client_id, client_type = email_to_client_id[participant]
            else:
                result = get_users_client_id(
                    participant,
                    user_id,
                    cursor,
                )
                if isinstance(result, tuple) and len(result) == 2:
                    client_id, client_type = result
                else:
                    client_id = result if result else None
                    client_type = None

                if not client_id:
                    if not first_time_user:
                        client_id = add_lead_contact(
                            user_id,
                            cursor,
                            participant,
                            participant_name,
                        )

                        client_type = "Lead"
                    else:
                        client_id = add_customer_contact(
                            user_id,
                            cursor,
                            participant,
                            participant_name,
                        )

                        client_type = "Customer"
                email_to_client_id[participant] = (client_id, client_type)

            # Build message dict
            zoho_attachments = msg.get("attachments", [])
            message = {
                "id": row_id,
                "from": from_email,
                "to": to_emails[0] if to_emails else None,
                "cc": msg.get("cc", ""),
                "bcc": msg.get("bcc", ""),
                "body": body_content,
                "plain_text": plain_text,
                "subject": subject,
                "timestamp": timestamp_iso,
                "source": "zoho",
                "direction": direction,
                "user_id": user_id,
                "thread_id": thread_id,
                "conversation_id": conversation_id,
                "type": client_type,
                "attachments": zoho_attachments,
            }

            # print("----------------------------")
            # print(f"in vefetch_zoho_messages_batch : {conversation_id}")
            # print("----------------------------")

            grouped_messages.setdefault(client_id, {}).setdefault("zoho", []).append(
                message
            )
            count_new += 1

            # Create per-client config files if needed
            if client_id not in configs_created:
                config_folder = os.path.join(
                    pathconfig.basepath,
                    "messages",
                    user_id,
                    client_id,
                )
                ensure_dir(config_folder)
                config_filepath = os.path.join(config_folder, "config.json")
                if not os.path.exists(config_filepath):
                    dummy_config = {
                        "userclients_id": client_id,
                        "conversations": [],
                    }
                    with open(config_filepath, "w", encoding="utf-8") as f:
                        json.dump(dummy_config, f, indent=2)
                        f.flush()
                        os.fsync(f.fileno())

                    s3_config_key = f"{user_id}/messages/{client_id}/config.json"
                    # print(f"s3_config_key : {s3_config_key}")

                    s3_data = read_json_from_s3(s3_config_key)
                    if s3_data is None:
                        upload_any_file(
                            config_filepath,
                            user_id,
                            type="messages",
                            s3_key_C=s3_config_key,
                        )

                configs_created.add(client_id)

        # Merge with existing data and save
        existing_data = safe_json_load(filepath)
        # print(f"filepath : {filepath}")
        merged_messages = existing_data.get("input_data", {})
        for client_id, channels in grouped_messages.items():
            for channel, messages in channels.items():
                merged_messages.setdefault(client_id, {}).setdefault(
                    channel, []
                ).extend(messages)

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(
                {"filename": filename, "input_data": merged_messages}, f, indent=2
            )

        # print(f"✅ Zoho batch complete: {count_new} new messages processed")
        return {
            "status": "success",
            "new_messages": count_new,
            "next_page_token": None,
            "grouped_messages": dict(grouped_messages),
        }

    except Exception as e:
        # print(f"[ERROR] → v2fetch_zoho_messages_batch failed: {e}")
        return {
            "error": str(e),
            "status": "failed",
            "next_page_token": None,
            "grouped_messages": {},
        }
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        if new_connection:
            new_connection.close()


async def v2all_continuous_zoho(user_id, integration=None, min_days=2):
    """
    Run Zoho fetch + processing in parallel batches.
    Each batch also runs heavy embedding processes in parallel.
    """
    # print(f"inside v2all_continuous_zoho with user id : {user_id} ")
    import time
    from umail_helper.asyn_functions import v2process_batch_with_embedding
    import asyncio

    any_new_messages = False
    all_results = []
    complete_results = 0
    embedding_futures = []
    start_time = time.perf_counter()

    connection = connect_to_rds()
    cursor = connection.cursor()

    try:

        try:
            zoho_result = await v2fetch_zoho_messages_batch(user_id, connection)
        except Exception as e:
            # print(f"❌ Error fetching zoho batch: {e}")
            import traceback

            traceback.print_exc()
            return None

        if zoho_result.get("status") != "success":
            # print(
            #     f"❌ zoho batch failed: {zoho_result.get('error')}"
            # )
            return None

        # print("====================================")
        # print(f"outlook_result : {outlook_result}")
        # print("====================================")

        new_messages = zoho_result.get("new_messages", 0)
        if new_messages > 0:
            any_new_messages = True

        complete_results += new_messages

        current_batch_messages = zoho_result.get("grouped_messages", {})
        ticket_allocator = await TicketAllocator.create(user_id)

        if new_messages > 0 and current_batch_messages:
            batch_count = 1
            lance_folder = os.path.join(
                pathconfig.basepath,
                "messages",
                user_id,
                f"lance_folder:{user_id}",
            )
            os.makedirs(lance_folder, exist_ok=True)

            # print(f"current_batch_messages : {current_batch_messages}")
            task = asyncio.to_thread(
                v2process_batch_with_embedding,
                user_id,
                current_batch_messages,
                batch_count,
                lance_folder,
                ticket_allocator,
                None,
                integration=integration,
            )
            embedding_futures.append(task)

        redis_service = get_redis()
        await update_user_message_cache(
            redis_service, user_id, all_results, newly_creation=True
        )

        if embedding_futures:
            await asyncio.gather(*embedding_futures)

        total_runtime = time.perf_counter() - start_time

        # ✅ Only update umail_json + finalize if any batch had new messages
        if any_new_messages:

            update_umail_json(
                user_id=user_id, new_count=new_messages, connection=connection
            )
            await ticket_allocator.finalize()
            folder_path = os.path.join(pathconfig.basepath, "messages", user_id)
            if os.path.exists(folder_path):
                shutil.rmtree(folder_path)
                # print(f"🗑️ Deleted folder and contents: {folder_path}")
            # else:
            # print(f"⚠️ Folder not found: {folder_path}")

        # else:
        #     print(
        #         "ℹ️ No new messages in any batch → skipping umail_json update/finalize"
        #     )

        # update the outlook_sync file
        # Current UTC time
        current_utc = datetime.utcnow().replace(tzinfo=timezone.utc)

        # Convert to epoch milliseconds
        current_epoch_ms = int(current_utc.timestamp() * 1000)

        # Call the function to set/update the user's sync time
        set_user_sync_time_zoho(user_id, current_epoch_ms)

        return {
            "user": user_id,
            "total_conversations": new_messages,
            "batches": new_messages,
            "runtime_seconds": total_runtime,
            "results": all_results,
        }

    except Exception as e:
        # print(f"[ERROR] v2all_continuous_zoho failed: {e}")
        import traceback

        traceback.print_exc()
        return {"error": str(e), "status": "failed"}

    finally:
        try:
            if cursor:
                cursor.close()
            if connection:
                connection.close()
        except Exception:
            pass


@zoho_bp.route("/zoho/send_email")
def send_zoho_email(user_id, to_email, subject, body_text, from_user_email):
    try:
        conn = connect_to_rds()
        cursor = conn.cursor()

        # print(f"usr id is : {user_id}")
        cursor.execute(
            "SELECT BusinessEmail FROM business_info WHERE user_id_fk = %s", (user_id,)
        )
        biz_row = cursor.fetchone()
        # if not biz_row:
        # print("error : User not found in users or business_info tables")

        user_email = biz_row[0]
        # Step 3: Get token info using BusinessEmail
        cursor.execute("SELECT token FROM users WHERE email = %s", (user_email,))
        token_row = cursor.fetchone()

        # if not token_row:
        #     print("error : No token found for business email")
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
