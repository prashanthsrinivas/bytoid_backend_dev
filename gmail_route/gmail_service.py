from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from email.message import EmailMessage
import base64
from email.mime.text import MIMEText
from db.rds_db import connect_to_rds
from data import MESSAGES  # delete this later, this is just for testing
from datetime import datetime, timezone
from googleapiclient.errors import HttpError
import traceback
import time
import asyncio

from typing import Optional


class GmailService:
    def __init__(self, user_id):
        conn = connect_to_rds()
        cursor = conn.cursor()
        cursor.execute(
            """
        SELECT client_id, client_secret, token, refresh_token, expiry
        FROM users
        WHERE user_id = %s
                       """,
            (str(user_id),),
        )
        row = cursor.fetchone()
        creds_data = Credentials(
            token=row[2],
            refresh_token=row[3],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=row[0],
            client_secret=row[1],
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
                # "https://www.googleapis.com/auth/docs",
                "openid",
                # "https://www.googleapis.com/auth/contacts.readonly",
                "https://www.googleapis.com/auth/contacts",
            ],
            expiry=row[4],  # Must be datetime, not string
        )

        self.creds = creds_data
        self.service = build("gmail", "v1", credentials=self.creds)

        profile = self.service.users().getProfile(userId="me").execute()
        self.user_email = profile["emailAddress"]

    def get_contacts(self):
        print("🔍 Starting get_contacts method...")
        try:
            print("📮 Fetching message list from Gmail API...")
            results = (
                self.service.users()
                .messages()
                .list(userId="me", maxResults=500)
                .execute()
            )
            messages = results.get("messages", [])
            print(f"📬 Found {len(messages)} messages to process")

            email_set = set()
            successful_messages = 0
            failed_messages = 0

            for i, msg in enumerate(messages):
                try:
                    print(f"🔄 Processing message {i+1}/{len(messages)}: {msg['id']}")
                    msg_detail = (
                        self.service.users()
                        .messages()
                        .get(
                            userId="me",
                            id=msg["id"],
                            format="metadata",
                            metadataHeaders=["From", "To"],
                        )
                        .execute()
                    )

                    headers = msg_detail.get("payload", {}).get("headers", [])
                    for header in headers:
                        if header["name"] in ["From", "To"]:
                            email_set.add(header["value"])
                            print(f"📧 Added email: {header['value']}")

                    successful_messages += 1

                except HttpError as e:
                    failed_messages += 1
                    if e.resp.status in [400, 404]:
                        print(
                            f"⏭️ Skipping inaccessible message {msg['id']}: HTTP {e.resp.status}"
                        )
                        continue
                    else:
                        print(f"❌ HTTP Error for message {msg['id']}: {e}")
                        raise e
                except Exception as e:
                    failed_messages += 1
                    print(f"❌ Error processing message {msg['id']}: {e}")
                    continue

            final_emails = list(email_set)
            print(
                f"✅ get_contacts completed - Success: {successful_messages}, Failed: {failed_messages}, Unique emails: {len(final_emails)}"
            )
            return final_emails

        except HttpError as e:
            print(f"❌ Gmail API error: {e}")
            return []
        except Exception as e:
            print(f"💥 Unexpected error in get_contacts: {e}")
            print(f"📋 Traceback: {traceback.format_exc()}")
            return []

    def parse_headers(self, headers):
        header_dict = {}
        for h in headers:
            header_dict[h["name"].lower()] = h["value"]
        return header_dict

    async def get_threads_async(
        self, email_type, max_results=None, batch_delay=0.5, start_page_token=None
    ):
        """
        Async version of get_threads with continuous batch support
        """
        try:
            all_threads = []
            next_page_token = start_page_token  # Start from specific page if provided
            page_size = 100  # Gmail API max is 100 per request
            total_fetched = 0

            # Get my email address once
            my_email = (
                self.service.users()
                .getProfile(userId="me")
                .execute()
                .get("emailAddress")
            )
            print(f"📧 My email: {my_email}")

            while True:
                try:
                    print(
                        f"🔄 Fetching page of threads (already got {total_fetched})..."
                    )

                    # Prepare the request parameters
                    request_params = {
                        "userId": "me",
                        "maxResults": page_size,
                        "labelIds": [email_type],
                    }

                    # Add page token if we have one
                    if next_page_token:
                        request_params["pageToken"] = next_page_token

                    # Make the API request
                    response = (
                        self.service.users().threads().list(**request_params).execute()
                    )

                    threads = response.get("threads", [])
                    print(f"📬 Retrieved {len(threads)} threads in this batch")

                    if not threads:
                        print("📭 No more threads found")
                        break

                    # Process threads concurrently
                    semaphore = asyncio.Semaphore(5)

                    async def process_with_semaphore(thread):
                        async with semaphore:
                            return await self._process_single_thread_async(
                                thread, my_email
                            )

                    tasks = [process_with_semaphore(thread) for thread in threads]
                    print(f"🚀 Processing {len(tasks)} threads concurrently...")
                    results = await asyncio.gather(*tasks, return_exceptions=True)

                    # Count successful and failed threads
                    successful_threads = 0
                    failed_threads = 0

                    for i, result in enumerate(results):
                        if isinstance(result, Exception):
                            failed_threads += 1
                            print(
                                f"💥 Failed to process thread {threads[i].get('id', 'unknown')}: {str(result)}"
                            )
                        elif result:
                            all_threads.extend(result)
                            successful_threads += 1
                        else:
                            failed_threads += 1

                    print(
                        f"📊 Batch complete - Success: {successful_threads}, Failed: {failed_threads}"
                    )

                    # Add delay between batches to avoid rate limiting
                    if batch_delay > 0 and next_page_token:
                        print(
                            f"😴 Sleeping for {batch_delay} seconds to avoid rate limits..."
                        )
                        await asyncio.sleep(batch_delay)

                    total_fetched += len(threads)
                    print(f"📊 Total threads processed so far: {total_fetched}")

                    # Check if we've reached the max_results limit
                    if max_results and total_fetched >= max_results:
                        print(f"🏁 Reached max_results limit of {max_results}")
                        # Return next page token for continuation
                        return all_threads, response.get("nextPageToken")

                    # Check if there are more pages
                    next_page_token = response.get("nextPageToken")
                    if not next_page_token:
                        print("🏁 No more pages available")
                        return all_threads, None  # No more pages

                    print(f"➡️ Moving to next page (token: {next_page_token[:20]}...)")

                except Exception as e:
                    print(f"❌ Error fetching thread batch: {str(e)}")
                    break

            print(f"✅ Completed! Total threads fetched: {len(all_threads)}")
            return all_threads, next_page_token

        except Exception as e:
            print(f"💥 A general error occurred in get_threads_async: {str(e)}")
            return [], None

    async def _process_single_thread_async(self, thread, my_email, max_retries=3):
        """
        Async version of _process_single_thread with proper error handling
        """

        thread_id = thread["id"]

        for attempt in range(max_retries + 1):
            try:
                if attempt > 0:
                    # Wait before retrying (exponential backoff)
                    wait_time = 2**attempt
                    print(
                        f"⏳ Retrying thread {thread_id} in {wait_time} seconds (attempt {attempt + 1}/{max_retries + 1})"
                    )
                    await asyncio.sleep(wait_time)

                # The Gmail API calls remain synchronous but wrapped in async function
                thread_detail = (
                    self.service.users()
                    .threads()
                    .get(userId="me", id=thread_id)
                    .execute()
                )
                messages = thread_detail.get("messages", [])

                if not messages:
                    return []

                thread_data = []

                for message in messages:
                    try:
                        headers = message.get("payload", {}).get("headers", [])
                        parsed = self.parse_headers(headers)

                        message_id = next(
                            (
                                h["value"]
                                for h in headers
                                if h["name"].lower() == "message-id"
                            ),
                            None,
                        )

                        from_header = parsed.get("from", "")
                        to_header = parsed.get("to", "")

                        # Extract email address from from_header
                        email = (
                            from_header.split()[-1].strip("<>")
                            if from_header
                            else "unknown@example.com"
                        )

                        is_sent_by_me = my_email.lower() in from_header.lower()

                        message_data = {
                            "thread_id": thread_id,
                            "messageId": message_id,
                            "from": parsed.get("from", "Unknown Sender"),
                            "to": parsed.get("to", ""),
                            "email": email,
                            "subject": parsed.get("subject", "No Subject"),
                            "snippet": thread_detail.get("snippet", ""),
                            "body": message.get("snippet", ""),
                            "date": parsed.get("date", ""),
                            "isRead": "UNREAD" not in message.get("labelIds", []),
                            "isStarred": "STARRED" in message.get("labelIds", []),
                            "labels": message.get("labelIds", []),
                            "attachments": [],  # You can enhance this to parse actual attachments
                            "isSentByMe": is_sent_by_me,
                        }

                        thread_data.append(message_data)

                    except Exception as e:
                        print(
                            f"⚠️ Error processing message in thread {thread_id}: {str(e)}"
                        )
                        continue

                # If we get here, the request was successful
                return thread_data

            except HttpError as e:
                if e.resp.status == 500:
                    print(
                        f"🔥 Gmail API backend error for thread {thread_id} (attempt {attempt + 1}): {str(e)}"
                    )
                    if attempt < max_retries:
                        continue  # Try again
                    else:
                        print(
                            f"❌ Giving up on thread {thread_id} after {max_retries + 1} attempts"
                        )
                        return []  # Skip this thread after all retries
                elif e.resp.status in [400, 404]:
                    print(
                        f"⏭️ Skipping inaccessible thread {thread_id}: HTTP {e.resp.status}"
                    )
                    return []  # Don't retry for client errors
                else:
                    print(
                        f"❌ HTTP Error {e.resp.status} for thread {thread_id}: {str(e)}"
                    )
                    return []
            except Exception as e:
                print(f"💥 Unexpected error processing thread {thread_id}: {str(e)}")
                if attempt < max_retries:
                    continue  # Try again for unexpected errors
                else:
                    print(
                        f"❌ Giving up on thread {thread_id} after {max_retries + 1} attempts"
                    )
                    return []

        return []  # This should never be reached, but just in case

    # noraml function

    # def get_threads(self, email_type, max_results=20):
    # try:
    #     response = (
    #         self.service.users()
    #         .threads()
    #         .list(userId="me", maxResults=max_results)
    #         .execute()
    #     )

    # except Exception as e:
    #     print("A general error occurred:", str(e))
    # response = (
    #     self.service.users()
    #     .threads()
    #     .list(userId="me", maxResults=max_results, labelIds=[email_type])
    #     .execute()
    # )
    # threads = response.get("threads", [])
    # thread_data = []

    # my_email = (
    #     self.service.users().getProfile(userId="me").execute().get("emailAddress")
    # )

    # for thread in threads:
    #     thread_id = thread["id"]
    #     thread_detail = (
    #         self.service.users().threads().get(userId="me", id=thread_id).execute()
    #     )
    #     messages = thread_detail.get("messages", [])

    #     if not messages:
    #         continue

    #     for message in messages:

    #         headers = message.get("payload", {}).get("headers", [])
    #         parsed = self.parse_headers(headers)
    #         message_id = next(
    #             (h["value"] for h in headers if h["name"].lower() == "message-id"),
    #             None,
    #         )

    #         from_header = parsed.get("from", "")
    #         to_header = parsed.get("to", "")

    #         email = (
    #             from_header.split()[-1].strip("<>")
    #             if from_header
    #             else "unknown@example.com"
    #         )

    #         is_sent_by_me = my_email.lower() in from_header.lower()

    #         thread_data.append(
    #             {
    #                 "thread_id": thread_id,
    #                 "messageId": message_id,
    #                 "from": parsed.get("from", "Unknown Sender"),
    #                 "to": parsed.get("to", ""),
    #                 "email": email,
    #                 "subject": parsed.get("subject", "No Subject"),
    #                 "snippet": thread_detail.get("snippet", ""),
    #                 "body": message.get("snippet", ""),
    #                 "date": parsed.get("date", ""),
    #                 "isRead": "UNREAD" not in message.get("labelIds", []),
    #                 "isStarred": "STARRED" in message.get("labelIds", []),
    #                 "labels": message.get("labelIds", []),
    #                 "attachments": [],  # You can enhance this to parse actual attachments
    #             }
    #         )

    # return thread_data

    def get_inbox(self):
        return self.get_threads("INBOX")

    def get_spam(self):
        return self.get_threads("SPAM")

    def get_trash(self):
        return self.get_threads("TRASH")

    def get_drafts(self, max_results=10):
        response = (
            self.service.users()
            .drafts()
            .list(userId="me", maxResults=max_results)
            .execute()
        )
        drafts = response.get("drafts", [])
        draft_data = []

        for draft in drafts:
            draft_id = draft["id"]
            draft_detail = (
                self.service.users()
                .drafts()
                .get(userId="me", id=draft_id, format="metadata")
                .execute()
            )
            message = draft_detail.get("message", {})
            headers = message.get("payload", {}).get("headers", [])
            snippet = message.get("snippet", "")
            parsed = self.parse_headers(headers)
            message_id = next(
                (h["value"] for h in headers if h["name"].lower() == "message-id"), None
            )

            from_header = parsed.get("from", "")
            email = (
                from_header.split()[-1].strip("<>")
                if from_header
                else "unknown@example.com"
            )

            draft_data.append(
                {
                    "id": draft_id,
                    "messageId": message_id,
                    "from": from_header or "Unknown Sender",
                    "email": email,
                    "subject": parsed.get("subject", "No Subject"),
                    "snippet": snippet,
                    "body": snippet,  # You can enhance this later with MIME parsing
                    "date": parsed.get("date", ""),
                    "labels": message.get("labelIds", []),
                    "attachments": [],  # Drafts may have attachments — extend this later
                }
            )

        return draft_data

    def update_draft(self, draft_id, to, subject, body):
        raw = self.build_raw_email(to, subject, body)
        draft_body = {"message": {"raw": raw}}
        updated = (
            self.service.users()
            .drafts()
            .update(userId="me", id=draft_id, body=draft_body)
            .execute()
        )
        return updated

    def build_raw_email(self, to, subject, body):
        message = MIMEText(body)
        message["to"] = to
        message["subject"] = subject
        return base64.urlsafe_b64encode(message.as_bytes()).decode()

    def create_draft(self, to, subject, body_text):
        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body_text)

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        draft_body = {"message": {"raw": raw}}

        draft = (
            self.service.users().drafts().create(userId="me", body=draft_body).execute()
        )
        return draft

    def send_email(self, to, subject, body_text):

        try:
            message = EmailMessage()
            message["To"] = to
            message["Subject"] = subject
            message.set_content(body_text)
            raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
            message_body = {"raw": raw}

            sent = (
                self.service.users()
                .messages()
                .send(userId="me", body=message_body)
                .execute()
            )

            timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            message_id = sent["id"]
            thread_id = sent["threadId"]

            MESSAGES[message_id] = {
                "id": message_id,
                "thread_id": thread_id,
                "from": self.user_email,
                "to": to,
                "body": body_text,
                "subject": subject,
                "timestamp": timestamp,
                "status": "sent",
                "source": "gmail",
                "direction": "outbound",
                "user_id": "user_id",
                "message_id": message_id,
            }

            return MESSAGES[message_id]

        except Exception as e:
            print(f"❌ Error sending email: {e}")
            raise

    def send_Meet_mail(
        self, to_email: str, bcc_list: list[str], subject: str, body_html: str
    ):
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        import base64
        import re

        # Create plain-text fallback by stripping HTML tags
        plain_text = re.sub(r"<[^>]+>", "", body_html)

        # multipart/alternative ensures the client picks the best format
        message = MIMEMultipart("alternative")
        message["to"] = to_email
        if bcc_list:
            message["bcc"] = ", ".join(bcc_list)
        message["subject"] = subject

        # Attach plain and HTML versions
        part1 = MIMEText(plain_text, "plain")
        part2 = MIMEText(body_html, "html")
        message.attach(part1)
        message.attach(part2)

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        msg = {"raw": raw}
        sent = self.service.users().messages().send(userId="me", body=msg).execute()
        return sent

    def send_invite_mail(
        self,
        inviter: str,  # inviter email
        invitee: str,  # invitee email
        role: dict,  # role details (dict from DB)
        invite_link: str,  # generated invite link
        business_info: Optional[dict] = None,  # optional business info
    ):
        """
        Send a styled invitation email using Gmail API.

        role: {
            "id": "uuid",
            "name": "Manager",
            "permissions": [...]
        }

        business_info can include:
        {
            "BusinessName": "Acme Corp",
            "LineOfBusiness": "AI Solutions",
            "businessLocation": "New York, USA",
            "BusinessImage": "https://cdn/logo.png",
            "Website": "https://acme.com"
        }
        """
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        import base64

        role_name = role.get("name", "User")

        # build optional extra info
        extra_html = ""
        extra_text = ""

        if business_info:
            if "BusinessName" in business_info:
                extra_html += f"<h3 style='font-size:16px; margin-top:16px;'>{business_info['BusinessName']}</h3>"
                extra_text += f"\nBusiness: {business_info['BusinessName']}"
            if "LineOfBusiness" in business_info:
                extra_html += (
                    f"<p style='color:#374151;'>{business_info['LineOfBusiness']}</p>"
                )
                extra_text += f"\nLine of Business: {business_info['LineOfBusiness']}"
            if "businessLocation" in business_info:
                extra_html += (
                    f"<p><b>Location:</b> {business_info['businessLocation']}</p>"
                )
                extra_text += f"\nLocation: {business_info['businessLocation']}"
            if "BusinessImage" in business_info:
                extra_html += f"<p><img src='{business_info['BusinessImage']}' alt='Business Logo' style='max-height:80px; margin-top:8px;'></p>"

        # fallback invite link
        if not invite_link:
            invite_link = f"https://abc.com/invite/{role.get('id')}"

        # HTML body
        body_html = f"""
        <html>
        <body style="font-family: Arial, sans-serif; background-color:#f9fafb; padding:20px;">
            <div style="max-width:600px; margin:auto; background:white; padding:24px; border-radius:12px; box-shadow:0 4px 10px rgba(0,0,0,0.05);">
            <h2 style="font-size:20px; color:#111827;">You're Invited!</h2>
            <p style="font-size:16px; color:#374151;">Hello,</p>
            <p style="font-size:16px; color:#374151;">
                You have been invited by <b>{inviter}</b> to join our platform with the role <b>{role_name}</b>.
            </p>
            <p style="margin:20px 0;">
                <a href="{invite_link}" 
                style="display:inline-block; background:#2563eb; color:white; padding:12px 20px; border-radius:8px; text-decoration:none; font-weight:600;">
                Accept Invitation
                </a>
            </p>
            <p style="font-size:14px; color:#6b7280;">(This link will expire in 1 hour)</p>
            {extra_html}
            <hr style="margin:24px 0; border:none; border-top:1px solid #e5e7eb;">
            <p style="font-size:12px; color:#9ca3af; text-align:center;">
                Made with ❤️ by <a href="https://bytoid.io" style="color:#2563eb; text-decoration:none;">Bytoid.io</a>
            </p>
            </div>
        </body>
        </html>
        """

        # Plain text fallback
        body_text = f"""
        Hello,

        You have been invited by {inviter} to join our platform with the role: {role_name}.

        Accept Invitation (valid for 1 hour):
        {invite_link}
        {extra_text}

        --
        Made with ❤️ by Bytoid.io
        """

        # multipart/alternative ensures the client picks the best format
        message = MIMEMultipart("alternative")
        message["to"] = invitee
        message["subject"] = f"Invitation to join as {role_name}"

        # Attach plain and HTML versions
        part1 = MIMEText(body_text, "plain")
        part2 = MIMEText(body_html, "html")
        message.attach(part1)
        message.attach(part2)

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        msg = {"raw": raw}

        sent = self.service.users().messages().send(userId="me", body=msg).execute()
        return sent

    def send_reply(
        self, conversation_id, to, subject, thread_id, in_reply_to, body_text, user_id
    ):
        if not to:
            raise ValueError("Recipient email 'to' is required")
        if not subject:
            raise ValueError("Subject is required")
        if not thread_id:
            raise ValueError("Thread ID is required")
        if not in_reply_to:
            raise ValueError("In-Reply-To message ID is required")

        message = EmailMessage()
        message["To"] = to
        message["Subject"] = (
            f"Re: {subject}" if not subject.lower().startswith("re:") else subject
        )
        message["In-Reply-To"] = in_reply_to
        message["References"] = in_reply_to
        message.set_content(body_text)

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        message_body = {"raw": raw, "threadId": thread_id}

        sent = (
            self.service.users()
            .messages()
            .send(userId="me", body=message_body)
            .execute()
        )
        return sent

    def send_forward(self, to, subject, body_text):
        message = EmailMessage()
        message["To"] = to
        message["Subject"] = (
            f"Fwd: {subject}" if not subject.lower().startswith("fwd:") else subject
        )
        message.set_content(body_text)

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        message_body = {"raw": raw}

        sent = (
            self.service.users()
            .messages()
            .send(userId="me", body=message_body)
            .execute()
        )
        return sent
