from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from email.message import EmailMessage
import base64
from email.mime.text import MIMEText
from db.rds_db import connect_to_rds, get_cursor
from data import MESSAGES  # delete this later, this is just for testing
from datetime import datetime, timezone
from googleapiclient.errors import HttpError
from google.auth.transport.requests import Request
import traceback
import time
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional
from googleapiclient.http import BatchHttpRequest
from utils.base_logger import get_logger
from utils.s3_utils import attach_CLDFRNT_url, upload_any_file
import random
from typing import Optional, Tuple, List
import re
from bs4 import BeautifulSoup
import email
from lxml import html as lxml_html


logger = get_logger(__name__)


def to_epoch_days(date_str: str) -> int:
    """
    Convert 'YYYY-MM-DD' to Unix timestamp (epoch seconds).
    """
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def get_cutoff_ts(days_back: int) -> int:
    """
    Returns a Unix day timestamp (epoch seconds at 00:00 UTC)
    for N days ago.
    """
    days_back = int(days_back)
    cutoff_date = datetime.utcnow().replace(tzinfo=timezone.utc) - timedelta(
        days=days_back
    )
    cutoff_day = cutoff_date.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(cutoff_day.timestamp())


class GmailService:
    def __init__(
        self, user_id, connection=None, testing=False, workflow=None, wf_id=None, integration = None
    ):
        # Use provided connection or get a new one
        self.conn = connection or connect_to_rds()
        self.user_id = user_id
        self.testing = testing
        self.workflow = workflow
        self.current_wf_id = wf_id

        if not self.conn:
            raise ConnectionError("❌ Failed to connect to RDS (too many connections?)")
        print(f"user_id : {str(user_id)}")
        with get_cursor(self.conn) as cursor:
            if integration:
                cursor.execute(
                """
                SELECT client_id, client_secret, access_token, refresh_token, expiry
                FROM integrations
                WHERE user_id = %s
                """,
                (str(user_id)),
            )
            else:
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
                raise ValueError(f"No Gmail credentials found for user {user_id}")
        client_id, client_secret, access_token, refresh_token, expiry = row
        expiryed = datetime.fromisoformat(expiry) if isinstance(expiry, str) else expiry
        # Build credentials object
        self.creds = Credentials(
            token=access_token,
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
                "openid",
                "https://www.googleapis.com/auth/contacts",
            ],
            expiry=expiryed,
        )
        if self.creds.expired and self.creds.refresh_token:
            try:
                # This call uses the refresh_token to get a new access token
                self.creds.refresh(
                    Request()
                )  # You need to import google.auth.transport.requests.Request
                print(f"✅ Token refreshed successfully for user {user_id}")

                # 4. CRITICAL STEP: Save the NEW tokens and expiry back to the database
                with get_cursor(self.conn) as cursor:
                    cursor.execute(
                        """
                        UPDATE users
                        SET token = %s, expiry = %s 
                        WHERE user_id = %s
                        """,
                        (self.creds.token, self.creds.expiry, str(user_id)),
                    )
                self.conn.commit()

            except Exception as e:
                # Token refresh failed (e.g., refresh token revoked)
                print(f"❌ Token refresh failed for user {user_id}: {e}")
                raise ValueError(
                    f"Token refresh failed. User must re-authenticate: {e}"
                )
        if connection is None:
            self.conn.close()

        # Build Gmail API service
        self.service = build("gmail", "v1", credentials=self.creds)
        self.service_running = False

        # Fetch user profile (email)
        profile = self.service.users().getProfile(userId="me").execute()
        self.user_email = profile["emailAddress"]

    def get_contacts(self):
        # print("🔍 Starting get_contacts method...")
        try:
            # print("📮 Fetching message list from Gmail API...")
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
                for attempt in range(5):  # Retry up to 5 times
                    try:
                        print(
                            f"🔄 Processing message {i+1}/{len(messages)}: {msg['id']}"
                        )
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
                        break  # Success, exit retry loop

                    except HttpError as e:
                        if e.resp.status in [403, 429]:  # Quota exceeded or Rate limit
                            wait_time = (2**attempt) + random.random()
                            print(
                                f"⏳ Quota/Rate limit hit. Retrying in {wait_time:.2f}s..."
                            )
                            time.sleep(wait_time)
                        elif e.resp.status in [400, 404]:
                            failed_messages += 1
                            print(
                                f"⏭️ Skipping inaccessible message {msg['id']}: HTTP {e.resp.status}"
                            )
                            break  # Don't retry client errors
                        else:
                            failed_messages += 1
                            print(f"❌ HTTP Error for message {msg['id']}: {e}")
                            break  # Don't retry other errors
                    except Exception as e:
                        failed_messages += 1
                        print(f"❌ Error processing message {msg['id']}: {e}")
                        break  # Don't retry unexpected errors

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

    def create_watch_req(self):
        logger.info("making watch log for user %s", self.user_email)
        watch_request = {
            "labelIds": ["INBOX"],  # optional
            "topicName": "projects/bytoid-engineering/topics/gmailSync",  # your Pub/Sub topic
        }

        response = self.service.users().watch(userId="me", body=watch_request).execute()
        if response:
            logger.info(
                "watch log created for the user successfully %s", self.user_email
            )
        else:
            logger.info("watch log creation failed %s", self.user_email)
        return response
    

    def check_hisdata(self, stored_history_id):
        response = (
            self.service.users()
            .history()
            .list(
                userId="me",
                startHistoryId=stored_history_id,  # no historyTypes to get everything
            )
            .execute()
        )

        added_messages = []
        deleted_messages = []
        other_messages = []

        if "history" in response:
            for record in response["history"]:
                for added in record.get("messagesAdded", []):
                    added_messages.append(added["message"])
                for deleted in record.get("messagesDeleted", []):
                    deleted_messages.append(deleted["message"])
                for msg in record.get("messages", []):
                    other_messages.append(msg)  # catch-all

        return {
            "response": response,
            "messages_added": added_messages,
            "messages_deleted": deleted_messages,
            "other_messages": other_messages,
        }

    async def get_threads_async(
        self, email_type, max_results=100, batch_delay=0.5, start_page_token=None
    ):
        """
        Async version of get_threads with continuous batch support
        """
        try:
            all_threads = []
            next_page_token = start_page_token  # Start from specific page if provided
            page_size = max_results  # Gmail API max is 100 per request
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
                        "q": "in:inbox category:primary",
                        "maxResults": page_size,
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
                        # print("📭 No more threads found")
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
                        # print("🏁 No more pages available")
                        return all_threads, None  # No more pages

                    print(f"➡️ Moving to next page (token: {next_page_token[:20]}...)")

                except Exception as e:
                    # print(f"❌ Error fetching thread batch: {str(e)}")
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
                    .get(userId="me", id=thread_id, format="full")
                    .execute()
                )
                messages = thread_detail.get("messages", [])
                # for i in messages:
                #    #print("id",i.get("id"))
                #    #print("labelIds",i.get("labelIds"))
                #    #print("snippet",i.get("snippet"))
                #    #print("--------------------------------------")

                # break
                if not messages:
                    print(f"⚠️ Thread {thread_id} has no messages")
                    return []

                # DEBUG: Print message IDs and basic info
                for i, msg in enumerate(messages):
                    msg_id = msg.get("id", "unknown")
                    headers = msg.get("payload", {}).get("headers", [])
                    subject = next(
                        (h["value"] for h in headers if h["name"].lower() == "subject"),
                        "No Subject",
                    )
                    from_addr = next(
                        (h["value"] for h in headers if h["name"].lower() == "from"),
                        "Unknown",
                    )

                thread_data = []

                for message_index, message in enumerate(messages):
                    try:
                        labelids = message.get("labelIds", [])

                        # 🚫 Skip promotional emails
                        if "CATEGORY_PROMOTIONS" in labelids:
                            continue

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

                        snippet_text = message.get(
                            "snippet", thread_detail.get("snippet", "")
                        )

                        message_data = {
                            "thread_id": thread_id,
                            "messageId": message_id,
                            "from": parsed.get("from", "Unknown Sender"),
                            "to": parsed.get("to", ""),
                            "email": email,
                            "subject": parsed.get("subject", "No Subject"),
                            "snippet": thread_detail.get("snippet", ""),
                            "body": snippet_text,
                            "date": parsed.get("date", ""),
                            "isRead": "UNREAD" not in labelids,
                            "isStarred": "STARRED" in labelids,
                            "labels": labelids,
                            "attachments": [],  # Enhance later to parse actual attachments
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

    def build_batch_request(self, thread_ids, results):
        # print("batch build started", len(thread_ids))

        def callback(request_id, response, exception):
            if exception is not None:
                results[request_id] = {"error": str(exception)}
            else:
                results[request_id] = response

        batch = BatchHttpRequest(
            callback=callback, batch_uri="https://gmail.googleapis.com/batch/gmail/v1"
        )

        for t in thread_ids:
            # normalize whether t is dict or str
            thread_id = t["id"] if isinstance(t, dict) else t
            batch.add(
                self.service.users()
                .threads()
                .get(userId="me", id=thread_id, format="full"),
                request_id=thread_id,
            )
        # print("returning from batch", len(thread_ids))
        return batch

    async def fetch_threads_batch(self, thread_ids, batch_count, max_retries=5):
        BATCH_LIMIT = 100
        loop = asyncio.get_running_loop()
        results = {}

        while getattr(self, "service_running", False):
            await asyncio.sleep(1)

        self.service_running = True
        try:
            # Split into chunks of 100
            chunks = [
                thread_ids[i : i + BATCH_LIMIT]
                for i in range(0, len(thread_ids), BATCH_LIMIT)
            ]
            # print(
            #     f"🔹 Split {len(thread_ids)} threads into {len(chunks)} batches of {BATCH_LIMIT}"
            # )

            for chunk_idx, chunk in enumerate(chunks, start=1):
                print(
                    f"➡️ Processing chunk {chunk_idx}/{len(chunks)} (size={len(chunk)})"
                )

                threads_to_fetch = list(chunk)

                for attempt in range(max_retries):
                    if not threads_to_fetch:
                        break  # All fetched

                    current_results = {}
                    batch = self.build_batch_request(threads_to_fetch, current_results)

                    try:
                        await loop.run_in_executor(None, batch.execute)

                        # Merge results
                        results.update(current_results)

                        # Collect failed ones only
                        failed_threads = [
                            t
                            for t in threads_to_fetch
                            if "error"
                            in current_results.get(
                                t["id"] if isinstance(t, dict) else t, {}
                            )
                        ]

                        if not failed_threads:
                            print(f"✅ Chunk {chunk_idx} successful")
                            break
                        else:
                            threads_to_fetch = failed_threads
                            # print(
                            #     f"⚠️ {len(failed_threads)} failed in chunk {chunk_idx}, retrying..."
                            # )

                            wait = (2**attempt) + random.random()
                            # print(f"🔄 Retry in {wait:.2f}s...")
                            await asyncio.sleep(wait)

                    except HttpError as e:
                        if e.resp.status == 429:
                            wait = (2**attempt) + random.random()
                            # print(
                            #     f"⚠️ Rate limited on chunk {chunk_idx}, retry in {wait:.2f}s..."
                            # )
                            await asyncio.sleep(wait)
                        else:
                            raise

                # After retries, report any failures left for this chunk
                failed_after_retries = [
                    t
                    for t in chunk
                    if "error" in results.get(t["id"] if isinstance(t, dict) else t, {})
                ]
                if failed_after_retries:
                    print(
                        f"❌ Chunk {chunk_idx} had {len(failed_after_retries)} threads that failed permanently"
                    )

            # Cooldown after all chunks complete
            cooldown = random.randint(5, 10)
            print(f"🕒 All {len(chunks)} chunks processed. Cooling down {cooldown}s...")
            await asyncio.sleep(cooldown)

            # print("retuening results from fetch_threads_batch", len(results))
            return results

        finally:
            self.service_running = False
            print(f"current batch fetched {batch_count}")

    # ============ ATTACHMENT S3 UPLOAD LOGIC ============

    async def get_thread_last_message_direction(self, thread_id: str):
        """
        Return ONLY:
        - thread_id
        - message_id   (from Message-ID header, not Gmail API id)
        - message_text
        - direction ("inbound" / "outbound")
        """

        result = await self.fetch_threads_batch([thread_id], batch_count=1)
        user_email = self.user_email

        # Error or missing thread
        if thread_id not in result or "error" in result.get(thread_id, {}):
            return {
                "thread_id": thread_id,
                "message_id": None,
                "direction": None,
            }

        thread_data = result[thread_id]
        messages = thread_data.get("messages", [])

        # No messages in thread
        if not messages:
            return {
                "thread_id": thread_id,
                "message_id": None,
                "direction": None,
            }

        # Sort chronologically
        messages = sorted(messages, key=lambda m: int(m["internalDate"]))
        last_msg = messages[-1]
        # -----------------------------------------------------------
        # Extract Message-ID (real email Message-ID header)
        # -----------------------------------------------------------
        headers = last_msg.get("payload", {}).get("headers", [])
        message_id = next(
            (h["value"] for h in headers if h["name"].lower() == "message-id"),
            None,
        )

        # -----------------------------------------------------------
        # Determine inbound/outbound
        # -----------------------------------------------------------
        from_email = next(
            (h["value"] for h in headers if h["name"].lower() == "from"),
            None,
        )

        is_inbound = user_email.lower() not in (from_email or "").lower()
        direction = "inbound" if is_inbound else "outbound"

        # -----------------------------------------------------------
        # FINAL RETURN
        # -----------------------------------------------------------
        return {
            "thread_id": thread_id,
            "message_id": message_id,
            "direction": direction,
        }

    @staticmethod
    def process_and_upload_attachments(
        attachments_list, user_id, thread_id, message_id, service=None
    ):
        """
        Process a list of attachments and store metadata (WITHOUT uploading to S3).

        ⚠️ NEW BEHAVIOR:
        - Non-ICS attachments: Store METADATA ONLY (filename, mimeType, size, attachment_id)
          → Downloaded on-demand via download endpoint
        - ICS calendar files: STILL uploaded to S3 immediately (calendar events need to be processed)

        This approach saves S3 storage space by only uploading files when users actually download them.

        ALLOWED_MIMETYPES: Set of MIME types permitted for metadata storage
        - Documents: application/pdf, application/msword, application/vnd.openxmlformats-officedocument.*
        - Spreadsheets: application/vnd.ms-excel, application/vnd.openxmlformats-officedocument.spreadsheetml.*
        - Presentations: application/vnd.ms-powerpoint, application/vnd.openxmlformats-officedocument.presentationml.*
        - Images: image/jpeg, image/png, image/gif, image/webp
        - Archives: application/zip, application/x-rar-compressed
        - Text: text/plain, text/csv
        - Calendar: text/calendar, application/ics (ALWAYS uploaded to S3)
        - Other: application/json

        Args:
            attachments_list: List of dicts with {filename, mimeType, size, data, attachment_id}
            user_id: User ID for S3 path
            thread_id: Gmail thread ID
            message_id: Gmail message ID
            service: Gmail API service (optional, for future use)

        Returns:
            List of attachment dicts with metadata and optional S3 URLs
        """
        # Define allowed MIME types for metadata storage
        ALLOWED_MIMETYPES = {
            # Documents
            "application/pdf",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx
            "application/vnd.openxmlformats-officedocument.wordprocessingml.template",  # .dotx
            "application/vnd.ms-word.document.macroenabled.12",  # .docm
            # Spreadsheets
            "application/vnd.ms-excel",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # .xlsx
            "application/vnd.openxmlformats-officedocument.spreadsheetml.template",  # .xltx
            "application/vnd.ms-excel.sheet.macroenabled.12",  # .xlsm
            # Presentations
            "application/vnd.ms-powerpoint",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",  # .pptx
            "application/vnd.openxmlformats-officedocument.presentationml.template",  # .potx
            "application/vnd.ms-powerpoint.presentation.macroenabled.12",  # .pptm
            # Images
            "image/jpeg",
            "image/png",
            "image/gif",
            "image/webp",
            "image/bmp",
            "image/tiff",
            "image/svg+xml",
            # Archives
            "application/zip",
            "application/x-rar-compressed",
            "application/x-7z-compressed",
            "application/gzip",
            # Text
            "text/plain",
            "text/csv",
            "text/html",
            # Calendar invites (SPECIAL: always upload to S3)
            "text/calendar",
            "application/ics",
            # Other
            "application/json",
            "application/xml",
        }

        # Calendar MIME types - these ALWAYS get uploaded to S3
        CALENDAR_MIMETYPES = {"text/calendar", "application/ics"}

        processed_attachments = []

        if not attachments_list:
            # print("⚠️ No attachments to process")
            return processed_attachments

        import os
        import tempfile

        for attachment in attachments_list:
            try:
                filename = attachment.get("filename", "unknown")
                mime_type = attachment.get("mimeType", "application/octet-stream")
                file_data = attachment.get("data")
                attachment_id = attachment.get("attachment_id", attachment.get("id"))

                # Step 1: Check MIME type against whitelist
                if mime_type not in ALLOWED_MIMETYPES:
                    print(f"⏭️ Skipping {filename} - MIME type {mime_type} not allowed")
                    continue

                if not file_data:
                    print(f"⏭️ Skipping {filename} - No file data")
                    continue

                print(
                    f"� Processing attachment: {filename} ({mime_type}, {len(file_data)} bytes)"
                )

                # ===== CALENDAR FILES (ICS/iCal) - UPLOAD TO S3 =====
                if mime_type in CALENDAR_MIMETYPES or filename.lower().endswith(".ics"):
                    print(
                        f"📅 [CALENDAR] {filename} is a calendar file - uploading to S3"
                    )

                    # Create temporary file
                    with tempfile.NamedTemporaryFile(
                        delete=False, suffix=os.path.splitext(filename)[1]
                    ) as tmp_file:
                        tmp_file.write(file_data)
                        tmp_path = tmp_file.name

                    # Build S3 key path
                    filename_safe = filename.replace("/", "_").replace("\\", "_")
                    s3_key = f"{user_id}/messages/attachments/{thread_id}/{message_id}/{filename_safe}"

                    # Upload to S3
                    print(f"☁️ Uploading calendar to S3: {s3_key}")
                    result = upload_any_file(
                        tmp_path, user_id, type="messages", s3_key_C=s3_key
                    )

                    # Get CloudFront URL
                    if result.get("status") == "success":
                        s3_url = attach_CLDFRNT_url(s3_key)

                        processed_attachments.append(
                            {
                                "filename": filename,
                                "mimeType": mime_type,
                                "size": len(file_data),
                                "s3_key": s3_key,
                                "url": s3_url,
                                "status": "uploaded",
                                "type": "calendar",
                            }
                        )
                        print(f"✅ Calendar uploaded {filename}: {s3_url}")
                    else:
                        print(
                            f"❌ S3 upload failed for {filename}: {result.get('message')}"
                        )
                        processed_attachments.append(
                            {
                                "filename": filename,
                                "mimeType": mime_type,
                                "size": len(file_data),
                                "attachment_id": attachment_id,
                                "status": "failed",
                                "error": result.get("message", "Unknown error"),
                                "type": "calendar",
                            }
                        )

                    # Clean up temp file
                    try:
                        os.remove(tmp_path)
                    except Exception as e:
                        print(f"⚠️ Could not delete temp file {tmp_path}: {e}")

                # ===== NON-CALENDAR FILES - STORE METADATA ONLY =====
                else:
                    print(
                        f"💾 [ON-DEMAND] {filename} - storing metadata only (will download on-demand)"
                    )

                    processed_attachments.append(
                        {
                            "filename": filename,
                            "mimeType": mime_type,
                            "size": len(file_data),
                            "attachment_id": attachment_id,
                            "thread_id": thread_id,
                            "message_id": message_id,
                            "status": "pending",
                            "type": "file",
                            "download_required": True,
                        }
                    )
                    print(f"📝 Metadata stored for {filename} (requires user download)")

            except Exception as e:
                print(f"❌ Error processing attachment {filename}: {e}")
                print(f"📋 Traceback: {traceback.format_exc()}")
                processed_attachments.append(
                    {
                        "filename": attachment.get("filename", "unknown"),
                        "status": "error",
                        "error": str(e),
                    }
                )

        print(
            f"📊 Attachment summary: {len(processed_attachments)} processed from {len(attachments_list)} total"
        )
        return processed_attachments

    @staticmethod
    def get_message_body_via_mime_og(
        msg, service=None, user_id=None, s3_config_key_prefix=None
    ):
        """
        Extracts message body and attachments using MIME format fetching.
        SIMPLIFIED: Extract direct HTTPS image links from HTML instead of complex Base64 embedding.

        Features:
        - Fetches the complete raw email message (base64-encoded RFC 5322 format)
        - Decodes and parses all MIME parts properly
        - Extracts text/html with direct HTTPS image links preserved
        - Handles complex multipart structures and nested messages
        - Better attachment handling through MIME parsing

        Returns: (body_text, attachments_list)
        """
        body = None
        attachments = []

        try:
            if not service:
                # print("⚠️ [MIME] Service not provided for MIME fetch, falling back")
                return "", []

            message_id = msg.get("id")
            if not message_id:
                # print("⚠️ [MIME] No message ID found")
                return "", []

            # Step 1: Fetch raw message using Gmail API
            raw_msg = (
                service.users()
                .messages()
                .get(userId="me", id=message_id, format="raw")
                .execute()
            )

            # Step 2: Extract and decode the base64 raw data
            raw_data = raw_msg.get("raw")
            if not raw_data:
                return "", []

            # Decode base64 to get email bytes
            msg_bytes = base64.urlsafe_b64decode(raw_data)

            # Step 3: Parse as email message using Python's email library
            mime_msg = email.message_from_bytes(msg_bytes)

            # Step 4: Extract body and attachments from MIME structure
            # Dictionary to store cid references -> data for later processing
            inline_images = {}

            def extract_mime_parts(msg_part):
                nonlocal body, attachments, inline_images

                content_type = msg_part.get_content_type()
                content_disposition = msg_part.get("Content-Disposition", "")
                content_id = msg_part.get("Content-ID", "").strip("<>")

                # Priority: Check for inline images (embedded in email body via Content-ID)
                # Images with Content-ID are always inline, regardless of disposition
                is_inline_image = content_type.startswith("image/") and content_id

                # Check other inline/attachment types
                is_inline_other = (
                    "inline" in content_disposition
                    and not content_type.startswith("image/")
                )
                # FIX: Attachments are marked with disposition=attachment, even if they have a cid
                # Don't exclude them just because they have a content_id
                is_attachment = content_disposition.startswith("attachment")

                if is_inline_image:
                    # Handle inline/embedded images
                    filename = msg_part.get_filename()
                    if not filename:
                        filename = f"image_{content_id}.{content_type.split('/')[-1]}"

                    payload = msg_part.get_payload(decode=True)
                    if payload and content_id:
                        # Store inline image for later S3 upload
                        inline_images[content_id] = {
                            "filename": filename,
                            "mimeType": content_type,
                            "data": payload,
                            "cid": content_id,
                        }

                elif is_attachment:
                    # Handle attachments
                    filename = msg_part.get_filename()
                    if filename:
                        payload = msg_part.get_payload(decode=True)
                        if payload:
                            # Extract attachment_id from Gmail API if available
                            attachment_id = None
                            if hasattr(msg_part, "_msg") and msg_part._msg:
                                attachment_id = msg_part._msg.get("id")

                            attachment_entry = {
                                "filename": filename,
                                "mimeType": content_type,
                                "size": len(payload),
                                "data": payload,
                                "attachment_id": attachment_id,
                            }

                            # Mark calendar invites for special handling
                            if filename.lower().endswith(".ics") or content_type in [
                                "text/calendar",
                                "application/ics",
                            ]:
                                attachment_entry["type"] = "calendar"

                            attachments.append(attachment_entry)

                # Handle text content - HTML is PREFERRED over plain text
                elif content_type == "text/html":
                    # Always prefer HTML (overwrite plain text if we had it)
                    payload = msg_part.get_payload(decode=True)
                    if payload:
                        html_content = payload.decode("utf-8", errors="ignore")

                        try:
                            # Clean up HTML but preserve images and formatting
                            soup = BeautifulSoup(html_content, "html.parser")

                            # Remove tracking pixels and problematic tags
                            for tag in soup.find_all(["script", "style", "meta"]):
                                try:
                                    if tag:
                                        tag.decompose()
                                except:
                                    pass

                            # Only remove images with display:none (hidden tracking pixels)
                            for img in soup.find_all("img"):
                                try:
                                    style = img.get("style", "")
                                    if (
                                        "display: none" in style
                                        or "display:none" in style
                                    ):
                                        img.decompose()
                                except:
                                    pass

                            # Get cleaned HTML - preserves images, links, formatting
                            body = str(soup)

                        except Exception as e:
                            # If cleanup fails, use the raw HTML anyway
                            body = html_content

                        try:
                            plain_text = (
                                lxml_html.fromstring(body).text_content().strip()
                            )
                        except:
                            plain_text = ""

                elif content_type == "text/plain" and body is None:
                    # Plain text only if we don't have HTML yet
                    payload = msg_part.get_payload(decode=True)
                    if payload:
                        plain_content = payload.decode("utf-8", errors="ignore")
                        body = plain_content.strip()
                        plain_text = body

                elif msg_part.is_multipart():
                    # Recursively handle multipart messages
                    for sub_part in msg_part.get_payload():
                        extract_mime_parts(sub_part)

            # Step 5: Start parsing
            if mime_msg.is_multipart():
                for part in mime_msg.get_payload():
                    extract_mime_parts(part)
            else:
                extract_mime_parts(mime_msg)

            # Step 7: Clean up body content (but preserve HTML tags and images)
            body = body or ""

            if not body.startswith("<"):
                # Plain text format - apply basic cleanup only
                # Remove quoted replies/forwards
                reply_patterns = [
                    r"\nOn .* wrote:",
                    r"\n>.*",
                    r"\nFrom: .*",
                    r"\nSent: .*",
                    r"\nTo: .*",
                    r"\nSubject: .*",
                ]
                for pattern in reply_patterns:
                    body = re.split(pattern, body, maxsplit=1)[0]

                body = body.strip()

            return body, attachments

        except HttpError as e:
            print(f"❌ [MIME] Gmail API error: {e}")
            return "", []
        except Exception as e:
            print(f"❌ [MIME] Extraction error: {e}")
            return "", []

    @staticmethod
    def get_message_body_via_mime(
        msg, service=None, user_id=None, s3_config_key_prefix=None
    ):
        """
        Extracts message body and attachments using MIME format fetching.
        Now ALSO returns plain_text.
        """

        body = None
        plain_text = ""
        attachments = []

        try:
            if not service:
                # print("⚠️ [MIME] Service not provided for MIME fetch, falling back")
                return "", "", []

            message_id = msg.get("id")
            if not message_id:
                # print("⚠️ [MIME] No message ID found")
                return "", "", []

            raw_msg = (
                service.users()
                .messages()
                .get(userId="me", id=message_id, format="raw")
                .execute()
            )

            raw_data = raw_msg.get("raw")
            if not raw_data:
                return "", "", []

            msg_bytes = base64.urlsafe_b64decode(raw_data)
            mime_msg = email.message_from_bytes(msg_bytes)

            inline_images = {}

            def extract_mime_parts(msg_part):
                nonlocal body, attachments, inline_images, plain_text

                content_type = msg_part.get_content_type()
                content_disposition = msg_part.get("Content-Disposition", "")
                content_id = msg_part.get("Content-ID", "").strip("<>")

                is_inline_image = content_type.startswith("image/") and content_id
                is_inline_other = (
                    "inline" in content_disposition
                    and not content_type.startswith("image/")
                )
                is_attachment = content_disposition.startswith("attachment")

                if is_inline_image:
                    filename = msg_part.get_filename()
                    if not filename:
                        filename = f"image_{content_id}.{content_type.split('/')[-1]}"

                    payload = msg_part.get_payload(decode=True)
                    if payload and content_id:
                        inline_images[content_id] = {
                            "filename": filename,
                            "mimeType": content_type,
                            "data": payload,
                            "cid": content_id,
                        }

                elif is_attachment:
                    filename = msg_part.get_filename()
                    if filename:
                        payload = msg_part.get_payload(decode=True)
                        if payload:
                            attachment_id = None
                            if hasattr(msg_part, "_msg") and msg_part._msg:
                                attachment_id = msg_part._msg.get("id")

                            attachment_entry = {
                                "filename": filename,
                                "mimeType": content_type,
                                "size": len(payload),
                                "data": payload,
                                "attachment_id": attachment_id,
                            }

                            if filename.lower().endswith(".ics") or content_type in [
                                "text/calendar",
                                "application/ics",
                            ]:
                                attachment_entry["type"] = "calendar"

                            attachments.append(attachment_entry)

                elif content_type == "text/html":
                    payload = msg_part.get_payload(decode=True)
                    if payload:
                        html_content = payload.decode("utf-8", errors="ignore")

                        try:
                            soup = BeautifulSoup(html_content, "html.parser")

                            for quote in soup.select(
                                ".gmail_quote, .gmail_quote_container, blockquote, div[id^='divRplyFwdMsg']"
                            ):
                                try:
                                    quote.decompose()
                                except:
                                    pass

                            for tag in soup.find_all(["script", "style", "meta"]):
                                try:
                                    tag.decompose()
                                except:
                                    pass

                            for img in soup.find_all("img"):
                                try:
                                    style = img.get("style", "")
                                    if (
                                        "display: none" in style
                                        or "display:none" in style
                                    ):
                                        img.decompose()
                                except:
                                    pass

                            body = str(soup)

                        except:
                            body = html_content

                        try:
                            plain_text = (
                                lxml_html.fromstring(body).text_content().strip()
                            )
                        except:
                            plain_text = ""

                        # Normalize whitespace: removes newlines, tabs, multiple spaces
                        plain_text = " ".join(plain_text.split())

                elif content_type == "text/plain" and body is None:
                    payload = msg_part.get_payload(decode=True)
                    if not payload:
                        return

                    plain = payload.decode("utf-8", errors="ignore")

                    # Strip quoted replies (plain text only)
                    reply_patterns = [
                        r"\nOn .* wrote:",
                        r"\n>.*",
                        r"\nFrom: .*",
                        r"\nSent: .*",
                        r"\nTo: .*",
                        r"\nSubject: .*",
                        r"\n-----Original Message-----",
                    ]

                    for pattern in reply_patterns:
                        plain = re.split(pattern, plain, maxsplit=1, flags=re.IGNORECASE)[0]

                    body = plain.strip()
                    plain_text = " ".join(body.split())

                elif msg_part.is_multipart():
                    for sub_part in msg_part.get_payload():
                        extract_mime_parts(sub_part)

            if mime_msg.is_multipart():
                for part in mime_msg.get_payload():
                    extract_mime_parts(part)
            else:
                extract_mime_parts(mime_msg)

            body = body or ""

            # if not body.startswith("<"):
            #     reply_patterns = [
            #         r"\nOn .* wrote:",
            #         r"\n>.*",
            #         r"\nFrom: .*",
            #         r"\nSent: .*",
            #         r"\nTo: .*",
            #         r"\nSubject: .*",
            #     ]
            #     for pattern in reply_patterns:
            #         body = re.split(pattern, body, maxsplit=1)[0]

            #     body = body.strip()
            #     plain_text = body  # ensure plain text is filled for plain emails

            #     # Normalize whitespace: removes newlines, tabs, multiple spaces
            #     plain_text = " ".join(plain_text.split())

            return body, attachments, plain_text

        except HttpError as e:
            print(f"❌ [MIME] Gmail API error: {e}")
            return "", "", []
        except Exception as e:
            print(f"❌ [MIME] Extraction error: {e}")
            return "", "", []

    @staticmethod
    def get_message_body_og(msg, service=None, user_id=None, s3_config_key_prefix=None):
        """
        Extracts a clean Gmail/Outlook-like message body and attachments with clickable S3 links.
        FALLBACK METHOD: Uses the standard API format parsing.

        Features:
        - Parses the payload structure from standard API format
        - Prefers HTML over plain text
        - Recursively parses all parts
        - Removes quoted replies, signatures, duplicate lines

        For complete MIME parsing with raw email, use get_message_body_via_mime() instead.

        Returns: (body_text, attachments_list)
        """
        payload = msg.get("payload", {})
        body = None  # Only assign when actual content found
        attachments = []

        def parse_part(part):
            nonlocal body, attachments
            mime_type = part.get("mimeType", "")
            part_body = part.get("body", {})
            data = part_body.get("data")

            # HTML PARTS (preferred)
            if mime_type == "text/html" and data and body is None:
                decoded = base64.urlsafe_b64decode(data.encode("ASCII")).decode(
                    "utf-8", errors="ignore"
                )
                soup = BeautifulSoup(decoded, "html.parser")

                # Preserve lists
                for ul in soup.find_all("ul"):
                    for li in ul.find_all("li"):
                        li.insert_before("- ")
                    ul.unwrap()
                for ol in soup.find_all("ol"):
                    for idx, li in enumerate(ol.find_all("li"), start=1):
                        li.insert_before(f"{idx}. ")
                    ol.unwrap()

                # Preformatted text
                for pre in soup.find_all("pre"):
                    pre.insert_before("\n")
                    pre.insert_after("\n")

                # Block-level newlines
                for tag in soup.find_all(["br", "p", "div"]):
                    tag.insert_before("\n")

                # Convert to string (preserving HTML tags) instead of plain text
                # This keeps images and formatting intact
                body = str(soup).strip()

            # Plain text fallback
            elif mime_type == "text/plain" and data and body is None:
                decoded = base64.urlsafe_b64decode(data.encode("ASCII")).decode(
                    "utf-8", errors="ignore"
                )
                body = decoded.strip()

            # Calendar invites
            elif mime_type in ["text/calendar", "application/ics"] and data:
                decoded = base64.urlsafe_b64decode(data.encode("ASCII")).decode(
                    "utf-8", errors="ignore"
                )
                attachments.append({"type": "calendar", "content": decoded})

            # File attachments → S3
            # Track already uploaded filenames to avoid duplicates
            uploaded_filenames = set()

            # if part.get("filename") and user_id and s3_config_key_prefix:
            #     filename = part["filename"]
            #     mime_type = part.get("mimeType", "")

            #     # Skip calendar files if not needed
            #     if filename.lower().endswith(".ics") or mime_type in [
            #         "text/calendar",
            #         "application/ics",
            #     ]:
            #         return

            #     # Skip duplicates
            #     if filename in uploaded_filenames:
            #         return
            #     uploaded_filenames.add(filename)

            #     attachment_id = part_body.get("attachmentId")
            #     try:
            #         attachment_data = (
            #             service.users()
            #             .messages()
            #             .attachments()
            #             .get(userId="me", messageId=msg["id"], id=attachment_id)
            #             .execute()
            #         )
            #         file_bytes = base64.urlsafe_b64decode(
            #             attachment_data["data"].encode("UTF-8")
            #         )

            #         # Construct S3 key
            #         filename_safe = filename.replace("/", "_")
            #         s3_key_C = (
            #             f"{s3_config_key_prefix}/{msg['threadId']}/{filename_safe}"
            #         )

            #         # Save locally temporarily
            #         tmp_path = f"/tmp/{filename_safe}"
            #         with open(tmp_path, "wb") as f:
            #             f.write(file_bytes)

            #         # Upload to S3
            #         upload_any_file(
            #             tmp_path, user_id, type="messages", s3_key_C=s3_key_C
            #         )

            #         # Get clickable URL
            #         url = attach_CLDFRNT_url(s3_key_C)

            #         attachments.append(
            #             {"filename": filename, "mimeType": mime_type, "url": url}
            #         )
            #     except Exception as e:
            #         print(f"⚠️ Failed to process attachment {filename}: {e}")

            # Recurse nested parts
            for sub_part in part.get("parts", []):
                parse_part(sub_part)

            # Nested messages
            if mime_type == "message/rfc822":
                for npart in part.get("parts", []):
                    parse_part(npart)

        # Start parsing
        if "parts" in payload:
            for part in payload["parts"]:
                parse_part(part)
        else:
            parse_part(payload)

        body = body or ""

        # Remove quoted replies/forwards
        reply_patterns = [
            r"\nOn .* wrote:",
            r"\n>.*",
            r"\nFrom: .*",
            r"\nSent: .*",
            r"\nTo: .*",
            r"\nSubject: .*",
        ]
        for pattern in reply_patterns:
            body = re.split(pattern, body, maxsplit=1)[0]

        # Remove common signatures
        signature_patterns = [
            r"\n--\s*\n.*",
            r"\n__\s*\n.*",
            r"\nThanks[,\n].*",
            r"\nBest[,\n].*",
        ]
        for pattern in signature_patterns:
            body = re.sub(pattern, "", body, flags=re.IGNORECASE | re.DOTALL)

        # Normalize whitespace
        body = re.sub(r"\n\s*\n+", "\n\n", body)
        body = re.sub(r"[ \t]+", " ", body)
        body = body.strip()

        # Remove consecutive duplicate lines
        lines = body.splitlines()
        clean_lines = []
        prev_line = None
        for line in lines:
            if line.strip() != prev_line:
                clean_lines.append(line.strip())
                prev_line = line.strip()
        body = "\n".join(clean_lines)

        # Retry using raw format if body is empty
        if not body and service is not None:
            try:
                raw_msg = (
                    service.users()
                    .messages()
                    .get(userId="me", id=msg["id"], format="raw")
                    .execute()
                )
                msg_bytes = base64.urlsafe_b64decode(raw_msg["raw"])
                mime_msg = email.message_from_bytes(msg_bytes)
                fallback_body = ""
                if mime_msg.is_multipart():
                    for part in mime_msg.walk():
                        if part.get_content_type() in ["text/plain", "text/html"]:
                            part_payload = part.get_payload(decode=True)
                            if part_payload:
                                fallback_body += (
                                    part_payload.decode("utf-8", errors="ignore") + "\n"
                                )
                else:
                    fallback_body = mime_msg.get_payload(decode=True).decode(
                        "utf-8", errors="ignore"
                    )
                body = fallback_body.strip()
            except Exception as e:
                print(f"⚠️ Retry failed for message {msg.get('id')}: {e}")

        return body, attachments

    @staticmethod
    def get_message_body(msg, service=None, user_id=None, s3_config_key_prefix=None):
        """
        Extracts a clean Gmail/Outlook-like message body and attachments.
        Now returns BOTH html body and plain text body.

        Returns: (body_html, body_text, attachments)
        """
        payload = msg.get("payload", {})
        body = None  # HTML (preferred)
        plain_text = ""  # NEW: always capture plaintext
        attachments = []

        def parse_part(part):
            nonlocal body, attachments, plain_text
            mime_type = part.get("mimeType", "")
            part_body = part.get("body", {})
            data = part_body.get("data")

            # -------------------------------
            # HTML PARTS (PREFERRED)
            # -------------------------------
            if mime_type == "text/html" and data and body is None:
                decoded = base64.urlsafe_b64decode(data.encode("ASCII")).decode(
                    "utf-8", errors="ignore"
                )
                soup = BeautifulSoup(decoded, "html.parser")

                # List/formatting preservation (your existing logic)
                for ul in soup.find_all("ul"):
                    for li in ul.find_all("li"):
                        li.insert_before("- ")
                    ul.unwrap()
                for ol in soup.find_all("ol"):
                    for idx, li in enumerate(ol.find_all("li"), start=1):
                        li.insert_before(f"{idx}. ")
                    ol.unwrap()

                for pre in soup.find_all("pre"):
                    pre.insert_before("\n")
                    pre.insert_after("\n")

                for tag in soup.find_all(["br", "p", "div"]):
                    tag.insert_before("\n")

                # HTML body (unchanged)
                body = str(soup).strip()

                # NEW: also extract plain text from HTML
                plain_text = soup.get_text("\n", strip=True)
                plain_text = " ".join(plain_text.split())

            # -------------------------------
            # PLAIN TEXT PARTS (fallback)
            # -------------------------------
            elif mime_type == "text/plain" and data:
                decoded = base64.urlsafe_b64decode(data.encode("ASCII")).decode(
                    "utf-8", errors="ignore"
                )

                # If HTML not set yet, use as body
                if body is None:
                    body = decoded.strip()

                # Always preserve plain-text separately
                if not plain_text:
                    plain_text = decoded.strip()
                    plain_text = " ".join(plain_text.split())

            # -------------------------------
            # Calendar invites
            # -------------------------------
            elif mime_type in ["text/calendar", "application/ics"] and data:
                decoded = base64.urlsafe_b64decode(data.encode("ASCII")).decode(
                    "utf-8", errors="ignore"
                )
                attachments.append({"type": "calendar", "content": decoded})

            # -------------------------------
            # Recurse nested parts
            # -------------------------------
            for sub_part in part.get("parts", []):
                parse_part(sub_part)

            # Nested message/rfc822
            if mime_type == "message/rfc822":
                for npart in part.get("parts", []):
                    parse_part(npart)

        # Start parsing
        if "parts" in payload:
            for part in payload["parts"]:
                parse_part(part)
        else:
            parse_part(payload)

        body = body or ""

        # -------------------------------
        # Quoted reply cleanup (unchanged)
        # -------------------------------
        reply_patterns = [
            r"\nOn .* wrote:",
            r"\n>.*",
            r"\nFrom: .*",
            r"\nSent: .*",
            r"\nTo: .*",
            r"\nSubject: .*",
        ]
        for pattern in reply_patterns:
            body = re.split(pattern, body, maxsplit=1)[0]

        # Signature cleanup
        signature_patterns = [
            r"\n--\s*\n.*",
            r"\n__\s*\n.*",
            r"\nThanks[,\n].*",
            r"\nBest[,\n].*",
        ]
        for pattern in signature_patterns:
            body = re.sub(pattern, "", body, flags=re.IGNORECASE | re.DOTALL)

        # Whitespace normalization
        body = re.sub(r"\n\s*\n+", "\n\n", body)
        body = re.sub(r"[ \t]+", " ", body)
        body = body.strip()

        # Remove duplicate lines
        lines = body.splitlines()
        clean_lines = []
        prev_line = None
        for line in lines:
            if line.strip() != prev_line:
                clean_lines.append(line.strip())
                prev_line = line.strip()
        body = "\n".join(clean_lines)

        # -------------------------------
        # RAW fallback if empty (unchanged)
        # -------------------------------
        if not body and service is not None:
            try:
                raw_msg = (
                    service.users()
                    .messages()
                    .get(userId="me", id=msg["id"], format="raw")
                    .execute()
                )
                msg_bytes = base64.urlsafe_b64decode(raw_msg["raw"])
                mime_msg = email.message_from_bytes(msg_bytes)

                fallback_body = ""
                if mime_msg.is_multipart():
                    for part in mime_msg.walk():
                        if part.get_content_type() in ["text/plain", "text/html"]:
                            part_payload = part.get_payload(decode=True)
                            if part_payload:
                                fallback_body += (
                                    part_payload.decode("utf-8", errors="ignore") + "\n"
                                )
                else:
                    fallback_body = mime_msg.get_payload(decode=True).decode(
                        "utf-8", errors="ignore"
                    )
                body = fallback_body.strip()
            except Exception as e:
                print(f"⚠️ Retry failed for message {msg.get('id')}: {e}")

        # -------------------------------
        # Final plain-text fallback if still empty
        # -------------------------------
        if not plain_text:
            try:
                soup_for_text = BeautifulSoup(body, "html.parser")
                plain_text = soup_for_text.get_text("\n", strip=True)

            except:
                plain_text = body.strip()

            plain_text = " ".join(plain_text.split())

        return body, attachments, plain_text

    async def process_threads_batch(
        self, thread_ids, my_email, batch_count, global_retries=3
    ):
        """
        Process Gmail threads with retries.
        Retries only failed thread_ids and merges results.
        """
        final_results = {}
        remaining = list(thread_ids)

        for attempt in range(global_retries):
            if not remaining:
                break  # ✅ all done

            print(
                f"🔄 Global attempt {attempt+1}/{global_retries} with {len(remaining)} threads",
                # f"the data pushing to{remaining[0]} {type(remaining[0])}",
            )

            responses = await self.fetch_threads_batch(remaining, batch_count)
            # print(f"len of the responses batch {batch_count} --->", len(responses))

            next_remaining = []

            for thread_id, resp in responses.items():
                if "error" in resp:
                    # print(f"⚠️ Error fetching thread {thread_id}: {resp['error']}")
                    final_results[thread_id] = ([], resp["error"])
                    failed_data = next(
                        (
                            t
                            for t in remaining
                            if (t["id"] if isinstance(t, dict) else t) == thread_id
                        ),
                        thread_id,  # fallback: just the string
                    )
                    next_remaining.append(failed_data)  # retry this one
                    continue

                messages = resp.get("messages", [])
                if not messages:
                    print(f"⚠️ Thread {thread_id} has no messages")
                    final_results[thread_id] = ([], None)
                    continue

                thread_data = []
                for msg in messages:
                    try:
                        labelids = msg.get("labelIds", [])
                        # if "CATEGORY_PROMOTIONS" in labelids:
                        #     continue

                        headers = {
                            h["name"].lower(): h["value"]
                            for h in msg.get("payload", {}).get("headers", [])
                        }

                        message_id = headers.get("message-id")
                        print(f" *********** messages_id : {message_id}")
                        from_header = headers.get("from", "Unknown Sender")
                        to_header = headers.get("to", "")
                        cc_header = headers.get("cc", "")
                        bcc_header = headers.get("bcc", "")

                        from_email = (
                            from_header.split()[-1].strip("<>")
                            if from_header
                            else "unknown@example.com"
                        )
                        to_email = (
                            to_header.split()[-1].strip("<>") if to_header else ""
                        )

                        direction = (
                            "inbound"
                            if self.user_email.lower() == to_email.lower()
                            else "outbound"
                        )

                        # Try MIME format first (new method), fallback to old method if it fails
                        # print(
                        #     f"\n🔍 [MIME DEBUG] Starting MIME extraction for message {message_id}"
                        # )
                        body, attachments, plain_text = self.get_message_body_via_mime(
                            msg,
                            service=self.service,
                            user_id=self.user_id,
                            s3_config_key_prefix=f"{self.user_id}/messages/files",
                        )
                        # print(
                        #     f"🔍 [MIME DEBUG] Result: body_len={len(body) if body else 0}, plain_text_len={len(plain_text if plain_text else 0)}, attachments={len(attachments) if attachments else 0}"
                        # )
                        ##print("body data", body)

                        # If MIME extraction yielded empty body, fallback to old method
                        if not body:
                            # print(
                            #     f"⚠️ MIME extraction empty for {message_id}, using fallback method"
                            # )
                            body, attachments, plain_text = self.get_message_body(
                                msg,
                                service=self.service,
                                user_id=self.user_id,
                                s3_config_key_prefix=f"{self.user_id}/messages/files",
                            )
                            # print(
                            #     f"🔍 [FALLBACK DEBUG] Fallback result: body_len={len(body) if body else 0}, plain_text_len={len(plain_text if plain_text else 0)}, attachments={len(attachments) if attachments else 0}"
                            # )

                        # Step 2: Process and upload valid attachments to S3
                        # This happens immediately after extraction
                        processed_attachments = []
                        # print(
                        #     f"🔍 [ATTACHMENT DEBUG] Before processing: attachments={len(attachments) if attachments else 0}, type={type(attachments)}"
                        # )
                        if attachments:
                            # print(
                            #     f"📎 Processing {len(attachments)} attachments for message {message_id}"
                            # )
                            for att in attachments:
                                print(
                                    f"   - {att.get('filename', '?')} ({att.get('mimeType', '?')})"
                                )
                            processed_attachments = self.process_and_upload_attachments(
                                attachments,
                                user_id=self.user_id,
                                thread_id=thread_id,
                                message_id=msg.get("id", "unknown"),
                            )
                            # print(
                            #     f"✅ Attachment processing complete: {len(processed_attachments)} uploaded/processed"
                            # )
                            if processed_attachments:
                                for att in processed_attachments:
                                    print(
                                        f"   ✅ {att.get('filename', '?')}: {att.get('status', '?')} - URL: {att.get('url', 'NO URL')}"
                                    )
                        #     else:
                        #         print(
                        #             f"⚠️ WARNING: No attachments were processed/uploaded!"
                        #         )
                        # else:
                        #     print(
                        #         f"🔍 [MIME DEBUG] No attachments found for message {message_id}"
                        #     )

                        thread_data.append(
                            {
                                "thread_id": thread_id,
                                "messageId": message_id,
                                "from": from_header,
                                "to": to_header,
                                "cc": cc_header,
                                "email": from_email,  # sender’s email
                                "subject": headers.get("subject", "No Subject"),
                                "snippet": resp.get("snippet", ""),
                                "body": body,
                                "plain_text": plain_text,
                                "direction": direction,
                                "date": headers.get("date", ""),
                                "isRead": "UNREAD" not in labelids,
                                "isStarred": "STARRED" in labelids,
                                "labels": list(labelids),
                                "attachments": processed_attachments,  # Use S3-processed attachments with URLs
                                "isSentByMe": my_email.lower() in from_header.lower(),
                            }
                        )
                        # print(
                        #     f"📊 [MESSAGE DEBUG] Message added to thread_data: {message_id}"
                        # )
                        # print(
                        #     f"   - Attachments in message object: {len(processed_attachments) if processed_attachments else 0}"
                        # )
                        if processed_attachments:
                            for att in processed_attachments:
                                print(
                                    f"     ✅ {att.get('filename', '?')}: {att.get('status', '?')}"
                                )

                    except Exception as e:
                        print(f"⚠️ Error processing message in thread {thread_id}: {e}")
                        continue

                final_results[thread_id] = (thread_data, None)

            remaining = next_remaining

            if remaining:
                wait = 10  # fixed wait, can also do exponential backoff if you want
                # print(
                #     f"⚠️ {len(remaining)} threads still failed. Retrying in {wait}s..."
                # )
                await asyncio.sleep(wait)

        # After all retries, log permanent failures
        if remaining:
            print("❌ Permanent failures after all retries:")
            # for tid in remaining:
            #     print(f"   - Thread {tid}: {final_results[tid][1]}")

        # print("✅ Returning results from batch", len(final_results))
        return final_results

    def _extract_message_body(self, payload):
        """
        Enhanced body extraction to handle various message formats
        """
        body = ""

        try:
            # Handle different payload structures
            if "parts" in payload:
                # Multi-part message
                for part in payload["parts"]:
                    body += self._extract_message_body(part) + "\n"
            elif "body" in payload and "data" in payload["body"]:
                # Single-part message with body data
                import base64

                body_data = payload["body"]["data"]
                # Gmail uses URL-safe base64
                body_data = body_data.replace("-", "+").replace("_", "/")
                # Add padding if needed
                missing_padding = len(body_data) % 4
                if missing_padding:
                    body_data += "=" * (4 - missing_padding)

                try:
                    decoded_body = base64.b64decode(body_data).decode(
                        "utf-8", errors="ignore"
                    )
                    body += decoded_body
                except Exception as decode_error:
                    print(f"⚠️ Error decoding message body: {decode_error}")
                    body += "[Error decoding message body]"

        except Exception as e:
            print(f"⚠️ Error extracting message body: {e}")
            body = "[Error extracting message body]"

        return body.strip()

    def get_real_message_count(self, days_back=180):
        count = 0
        page_token = None
        cutoff_ts = get_cutoff_ts(days_back)
        q = f"in:inbox category:primary after:{cutoff_ts}"
        mess = []
        while True:
            response = (
                self.service.users()
                .messages()
                .list(
                    userId="me",
                    q=q,
                    pageToken=page_token,
                    maxResults=500,
                )
                .execute()
            )
            ##print("-->", response)
            count += len(response.get("messages", []))
            mess.extend(response.get("messages", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return mess

    def get_real_thread_count(self, days_back=180):
        count = 0
        page_token = None
        cutoff_ts = get_cutoff_ts(days_back)
        q = f"in:inbox category:primary after:{cutoff_ts}"

        while True:
            response = (
                self.service.users()
                .threads()
                .list(
                    userId="me",
                    q=q,
                    pageToken=page_token,
                    maxResults=500,
                )
                .execute()
            )
            ##print("-->", response)
            count += len(response.get("threads", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return count

    def get_real_date_basedmessage_count(self, start_date: str, end_date: str):
        def fetch_count(query: str = None):
            count = 0
            page_token = None
            while True:
                params = {
                    "userId": "me",
                    "maxResults": 500,
                    "pageToken": page_token,
                }
                if query:
                    params["q"] = query

                response = self.service.users().messages().list(**params).execute()
                msgs = response.get("messages", [])
                count += len(msgs)

                page_token = response.get("nextPageToken")
                if not page_token:
                    break
            return count

        # ✅ build initial query
        after_ts = to_epoch_days(start_date)
        before_ts = to_epoch_days(end_date) + 86400  # include end_date
        # query = f"in:inbox category:primary after:{after_ts} before:{before_ts}"
        query = f"in:inbox after:{after_ts} before:{before_ts}"

        # ✅ first attempt with query
        count = fetch_count(query)

        # ✅ if nothing found, retry without query (broad fetch)
        # if count == 0:
        #     count = fetch_count(query2)

        return count

    async def get_real_date_thread_count_dynamic(
        self, start_date: str, end_date: str, min_days=7
    ) -> dict:
        """
        Fetch threads dynamically: if large ranges fail, split into smaller chunks.
        Returns dict: {"count": total_count, "threads": all_threads_list}
        """

        async def fetch_chunk(s_date: str, e_date: str) -> dict:
            """Fetch a single chunk of Gmail threads, with fallback query if strict search returns nothing."""
            while getattr(self, "service_running", False):
                await asyncio.sleep(0.5)

            self.service_running = True
            try:
                s_dt = datetime.fromisoformat(s_date)
                e_dt = datetime.fromisoformat(e_date)

                after_ts = int(s_dt.replace(tzinfo=timezone.utc).timestamp())
                before_ts = int(
                    (e_dt + timedelta(days=1)).replace(tzinfo=timezone.utc).timestamp()
                )

                queries = [
                    f"(in:inbox OR in:sent) category:primary after:{after_ts} before:{before_ts}",
                    f"(in:inbox OR in:sent) after:{after_ts} before:{before_ts}",
                ]

                async def run_query(q: Optional[str]) -> Tuple[int, List]:
                    count = 0
                    threads = []
                    page_token = None

                    while True:
                        try:
                            params = {
                                "userId": "me",
                                "maxResults": 500,
                                "pageToken": page_token,
                            }
                            if q:
                                params["q"] = q

                            response = (
                                self.service.users().threads().list(**params).execute()
                            )
                            thd = response.get("threads", [])
                            count += len(thd)
                            threads.extend(thd)

                            page_token = response.get("nextPageToken")
                            if not page_token:
                                break
                        except Exception as e:
                            if hasattr(e, "resp") and e.resp.status == 429:
                                await asyncio.sleep(1 + random.random())
                                continue
                            else:
                                raise
                    return count, threads

                for q in queries:
                    count, threads = await run_query(q)
                    if count > 0:
                        return {"count": count, "threads": threads}

                return {"count": 0, "threads": []}

            finally:
                self.service_running = False

        all_threads = []
        total_count = 0
        stack = [(start_date, end_date)]

        while stack:
            s_date, e_date = stack.pop(0)
            try:
                result = await fetch_chunk(s_date, e_date)
                total_count += result["count"]
                all_threads.extend(result["threads"])
            except Exception as e:
                print(f"⚠️ Chunk {s_date} → {e_date} failed: {e}")
                # Split if more than min_days
                s_dt = datetime.fromisoformat(s_date)
                e_dt = datetime.fromisoformat(e_date)
                delta_days = (e_dt - s_dt).days
                if delta_days > min_days:
                    mid_dt = s_dt + timedelta(days=delta_days // 2)
                    stack.insert(0, (mid_dt.strftime("%Y-%m-%d"), e_date))
                    stack.insert(0, (s_date, mid_dt.strftime("%Y-%m-%d")))
                else:
                    print(f"❌ Skipping unresponsive chunk {s_date} → {e_date}")

        return {"count": total_count, "threads": all_threads}

    async def get_inbox_date_wise_stats_dynamic(
        self, start_date: str, end_date: str, min_days=7
    ):
        """
        Fetch Gmail threads dynamically for a large date range with rate-limit handling.
        """
        # print("fetching date wise data", start_date, end_date)
        try:
            allthreads = await self.get_real_date_thread_count_dynamic(
                start_date, end_date, min_days
            )
            # msg_threads = self.get_real_date_basedmessage_count(start_date, end_date)
            return {
                "email": self.user_email,
                "threadsTotal": allthreads,
                "start_date": start_date,
                "end_date": end_date,
            }
        except Exception as e:
            print(f"❌ Error fetching inbox stats: {e}")
            return None

    def get_inbox_stats(self, days_back=180):
        """
        Fetch actual Gmail messages grouped by thread/conversation.
        Each element returned = one thread with all its messages.
        """
        try:
            cutoff_ts = get_cutoff_ts(days_back)
            q = f"in:inbox category:primary after:{cutoff_ts}"

            all_threads = []
            page_token = None

            # 1. List all thread IDs that match the query
            while True:
                response = (
                    self.service.users()
                    .threads()
                    .list(
                        userId="me",
                        q=q,
                        pageToken=page_token,
                        maxResults=500,
                    )
                    .execute()
                )

                all_threads.extend(response.get("threads", []))
                page_token = response.get("nextPageToken")
                if not page_token:
                    break

            # 2. Fetch full thread content (with all messages) for each thread
            full_threads = []
            for thread in all_threads:
                thread_id = thread["id"]
                full_thread = (
                    self.service.users()
                    .threads()
                    .get(userId="me", id=thread_id, format="full")
                    .execute()
                )
                full_threads.append(full_thread)

            return full_threads  # Each full_thread contains all its messages
        except Exception as e:
            print(f"❌ Error fetching inbox stats: {e}")
            return None

    def get_non_promotional_messages(self, max_results=20):
        """Return non-promotional inbox messages with subject, from, snippet"""
        results = (
            self.service.users()
            .messages()
            .list(
                userId="me",
                q="in:inbox category:primary",  # ✅ only personal/primary
                maxResults=max_results,
            )
            .execute()
        )

        messages = results.get("messages", [])
        output = []

        for msg in messages:
            msg_detail = (
                self.service.users()
                .messages()
                .get(
                    userId="me",
                    id=msg["id"],
                    format="metadata",
                    metadataHeaders=["Subject", "From", "Date"],
                )
                .execute()
            )

            headers = msg_detail.get("payload", {}).get("headers", [])
            subject = next(
                (h["value"] for h in headers if h["name"] == "Subject"), None
            )
            sender = next((h["value"] for h in headers if h["name"] == "From"), None)
            date = next((h["value"] for h in headers if h["name"] == "Date"), None)
            labels = msg_detail.get("labelIds", [])

            output.append(
                {
                    "id": msg["id"],
                    "threadId": msg["threadId"],
                    "labels": labels,
                    "subject": subject,
                    "from": sender,
                    "date": date,
                    "snippet": msg_detail.get("snippet"),
                }
            )

        return output

    def get_non_promotional_threads(self, max_results=20):
        """Return only primary inbox threads with subject, from, snippet"""
        results = (
            self.service.users()
            .threads()
            .list(
                userId="me",
                q="in:inbox category:primary",  # ✅ only primary
                maxResults=max_results,
            )
            .execute()
        )

        threads = results.get("threads", [])
        output = []

        for thread in threads:
            thread_detail = (
                self.service.users()
                .threads()
                .get(
                    userId="me",
                    id=thread["id"],
                    format="metadata",
                    metadataHeaders=["Subject", "From", "Date"],
                )
                .execute()
            )

            # Get the first message in the thread (usually contains subject/from)
            messages = thread_detail.get("messages", [])
            if not messages:
                continue

            first_msg = messages[0]
            headers = first_msg.get("payload", {}).get("headers", [])
            subject = next(
                (h["value"] for h in headers if h["name"] == "Subject"), None
            )
            sender = next((h["value"] for h in headers if h["name"] == "From"), None)
            date = next((h["value"] for h in headers if h["name"] == "Date"), None)
            labels = first_msg.get("labelIds", [])

            output.append(
                {
                    "threadId": thread["id"],
                    "historyId": thread_detail.get("historyId"),
                    "labels": labels,
                    "subject": subject,
                    "from": sender,
                    "date": date,
                    "snippet": thread_detail.get("snippet"),
                    "messageCount": len(messages),
                }
            )

        return output

    def get_gmail_changes(self, start_history_id):
        """
        Fetch Gmail changes since a given historyId and return as dict.

        Args:
            service: Gmail API service object
            start_history_id: last processed historyId

        Returns:
            dict with messages added, deleted, and label changes
        """
        changes = {
            "messages_added": [],
            "messages_deleted": [],
            "labels_added": [],
            "labels_removed": [],
        }

        try:
            history_response = (
                self.service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=start_history_id,
                    historyTypes=[
                        "messageAdded",
                        "messageDeleted",
                        "labelAdded",
                        "labelRemoved",
                    ],
                )
                .execute()
            )

            for record in history_response.get("history", []):
                if "messagesAdded" in record:
                    changes["messages_added"].extend(
                        [m["message"]["id"] for m in record["messagesAdded"]]
                    )
                if "messagesDeleted" in record:
                    changes["messages_deleted"].extend(
                        [m["message"]["id"] for m in record["messagesDeleted"]]
                    )
                if "labelsAdded" in record:
                    changes["labels_added"].extend(record["labelsAdded"])
                if "labelsRemoved" in record:
                    changes["labels_removed"].extend(record["labelsRemoved"])

        except Exception as e:
            # HistoryId expired or other error
            # print("Error fetching history:", e)
            return None

        return changes

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

    def send_email(
        self, receipent_emails, subject, body_text, bcc_list=None, attachments=None
    ):
        """
        Sends an email via Gmail API.
        Automatically detects HTML content and includes both text + HTML versions.
        Supports file attachments.

        Args:
            receipent_emails (str | list): One or more recipient addresses.
            subject (str): Email subject.
            body_text (str): Email body (plain or HTML).
            bcc_list (list[str], optional): Optional BCC recipients.
            attachments (list[dict], optional): List of attachments. Each dict should have:
                - 's3_key': str - S3 path to the file
                - 'filename': str - Original filename
                - 'mime_type': str - MIME type (e.g., 'application/pdf')

        Returns:
            dict: {
                "success": bool,
                "response": dict | None,
                "error": str | None,
                "return_str": str
            }
        """
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        from email.mime.base import MIMEBase
        from email import encoders
        import base64
        import re

        try:
            # print("emails for send_email", receipent_emails, type(receipent_emails))
            # Normalize recipients
            if isinstance(receipent_emails, (list, tuple)):
                receipent_emails = ", ".join(receipent_emails)

            if bcc_list is None:
                bcc_list = []

            if attachments is None:
                attachments = []

            # Detect if the body is HTML and prepare a plain-text fallback
            is_html = bool("<" in body_text and ">" in body_text)
            plain_text = re.sub(r"<[^>]+>", "", body_text) if is_html else body_text

            # Create a multipart email supporting both plain and HTML and attachments
            message = MIMEMultipart("mixed")
            message["To"] = receipent_emails
            if bcc_list:
                message["Bcc"] = ", ".join(bcc_list)
            message["Subject"] = subject

            # Create message body as multipart/alternative for text + HTML
            msg_alternative = MIMEMultipart("alternative")
            msg_alternative.attach(MIMEText(plain_text, "plain"))
            if is_html:
                msg_alternative.attach(MIMEText(body_text, "html"))

            # Attach the message body to the main message
            message.attach(msg_alternative)

            # Process and attach files if provided
            if attachments:
                from utils.s3_utils import read_binary_from_s3
                from email.mime.base import MIMEBase
                from email import encoders

                print(f"📎 Attaching {len(attachments)} file(s) to email...")
                for att in attachments:
                    try:
                        s3_key = att.get("s3_key")
                        # Use original_filename if available (from upload handler), otherwise use filename
                        filename = att.get("original_filename") or att.get("filename")
                        mime_type = att.get("mime_type", "application/octet-stream")

                        if not s3_key or not filename:
                            print(
                                f"⚠️ Skipping attachment - missing s3_key or filename: {att}"
                            )
                            continue

                        # Read file from S3
                        file_data = read_binary_from_s3(s3_key)
                        if not file_data:
                            print(f"⚠️ Failed to read attachment from S3: {s3_key}")
                            continue

                        # Parse MIME type
                        maintype, subtype = (
                            mime_type.split("/", 1)
                            if "/" in mime_type
                            else (mime_type, "octet-stream")
                        )

                        # Create attachment part
                        attachment = MIMEBase(maintype, subtype)
                        attachment.set_payload(file_data)
                        encoders.encode_base64(attachment)
                        # Use proper Content-Disposition header with filename parameter for best compatibility
                        attachment.add_header(
                            "Content-Disposition", "attachment", filename=filename
                        )

                        # Add to message
                        message.attach(attachment)
                        print(f"✅ Attached: {filename} ({len(file_data)} bytes)")

                    except Exception as e:
                        print(
                            f"❌ Error attaching file {att.get('filename')}: {str(e)}"
                        )
                        continue

            # Encode message for Gmail API
            raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
            message_body = {"raw": raw}

            # Send via Gmail API
            sent = (
                self.service.users()
                .messages()
                .send(userId="me", body=message_body)
                .execute()
            )

            # Build response data
            message_id = sent.get("id")
            thread_id = sent.get("threadId")
            return_str = f"✅ Email titled '{subject}' sent successfully to {receipent_emails}. Gmail message ID: {message_id}"

            # Workflow mode short return
            if getattr(self, "workflow", None) or getattr(self, "current_wf_id", None):
                return {
                    "success": True,
                    "response": sent,
                    "error": None,
                    "return_str": return_str,
                }

            # Log into MESSAGES store if not workflow
            timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            MESSAGES[message_id] = {
                "id": message_id,
                "thread_id": thread_id,
                "from": self.user_email,
                "to": receipent_emails,
                "body": body_text,
                "subject": subject,
                "timestamp": timestamp,
                "status": "sent",
                "source": "gmail",
                "direction": "outbound",
                "user_id": "user_id",
                "message_id": message_id,
            }
            if self.workflow or self.current_wf_id:
                return {"return_str": return_str}

            return MESSAGES[message_id]
        except Exception as e:
            print(f"❌ Error sending email: {e}")
            return {
                "success": False,
                "response": None,
                "error": str(e),
                "return_str": f"❌ Failed to send email: {str(e)}",
            }

    def send_Meeting_invite_mail(
        self, receipent_emails, bcc_list: list[str], subject: str, body_html: str
    ):
        """
        Send an email via Gmail API.

        Args:
            to_email (str or list[str]): Recipient email(s)
            bcc_list (list[str]): BCC recipients
            subject (str): Email subject
            body_html (str): Email body in HTML
        Returns:
            dict: {
                "success": bool,
                "response": dict | None,
                "error": str | None,
                "return_str": str
            }
        """
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        import base64
        import re

        try:
            # Convert to_email to comma-separated string if it's a list
            if isinstance(receipent_emails, list):
                to_email_str = ", ".join(receipent_emails)
            else:
                to_email_str = receipent_emails

            # Create plain-text fallback by stripping HTML tags
            plain_text = re.sub(r"<[^>]+>", "", body_html)

            # multipart/alternative ensures the client picks the best format
            message = MIMEMultipart("alternative")
            message["to"] = to_email_str
            if bcc_list:
                message["bcc"] = ", ".join(bcc_list)
            message["subject"] = subject

            # Attach plain and HTML versions
            message.attach(MIMEText(plain_text, "plain"))
            message.attach(MIMEText(body_html, "html"))

            raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
            msg = {"raw": raw}

            sent = self.service.users().messages().send(userId="me", body=msg).execute()

            # ✅ Friendly summary for UI / logs
            return_str = (
                f"✅ Email titled '{subject}' sent successfully to {to_email_str}."
                f" Gmail message ID: {sent.get('id')}"
            )

            return {
                "success": True,
                "response": sent,
                "error": None,
                "return_str": return_str,
            }

        except Exception as e:
            return {
                "success": False,
                "response": None,
                "error": str(e),
                "return_str": f"❌ Failed to send email: {str(e)}",
            }

    def send_invite_mail(
        self,
        receipent_emails: str,  # invitee email
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
        inviter = self.user_email

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
                link_base = attach_CLDFRNT_url(business_info["BusinessImage"])
                extra_html += f"<p><img src='{link_base}' alt='Business Logo' style='max-height:80px; margin-top:8px;'></p>"

        # fallback invite link
        if not invite_link:
            invite_link = f"https://bytoid.ai/invite/{role.get('id')}"

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
        if isinstance(receipent_emails, list):
            to_email_str = ", ".join(receipent_emails)
        else:
            to_email_str = receipent_emails

        message["to"] = to_email_str
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
        self,
        receipent_emails,
        subject,
        thread_id,
        in_reply_to,
        body_text,
        attachments=None,
        cc=None,
        bcc=None,
        reply_type="reply",
    ):
        """
        Send a reply to an existing Gmail thread (safe version).
        """

        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        from email.mime.base import MIMEBase
        from email import encoders
        import base64
        import json
        import re

        if attachments is None:
            attachments = []

        # ================
        # 1) Normalize body_text (CRITICAL FIX)
        # ================
        if isinstance(body_text, dict):
            # Try common fields used in your system
            body_text = (
                body_text.get("message")
                or body_text.get("text")
                or body_text.get("body")
                or json.dumps(body_text, ensure_ascii=False)
            )

        if body_text is None:
            body_text = ""

        if not isinstance(body_text, str):
            body_text = str(body_text)

        body_text = body_text.strip()

        # ================
        # 2) Build email message
        # ================
        message = MIMEMultipart("mixed")

        # Recipients
        if isinstance(receipent_emails, list):
            to_email_str = ", ".join(receipent_emails)
        else:
            to_email_str = receipent_emails

        message["To"] = to_email_str

        # Subject (ensure Re:)
        message["Subject"] = (
            subject if subject.lower().startswith("re:") else f"Re: {subject}"
        )

        # Required reply headers
        message["In-Reply-To"] = in_reply_to
        message["References"] = in_reply_to

        # CC
        if cc:
            if isinstance(cc, list):
                message["Cc"] = ", ".join(cc)
            else:
                message["Cc"] = cc

        # BCC
        if bcc:
            if isinstance(bcc, list):
                message["Bcc"] = ", ".join(bcc)
            else:
                message["Bcc"] = bcc

        # ================
        # 3) Detect HTML
        # ================
        is_html = body_text.lower().startswith(
            ("<html", "<div", "<p", "<!doctype", "<body")
        )

        # multipart/alternative for body
        msg_alternative = MIMEMultipart("alternative")

        if is_html:
            # Plain text version (strip tags)
            plain_text = re.sub("<[^<]+?>", "", body_text)
            plain_text = re.sub(r"\s+", " ", plain_text).strip()

            msg_alternative.attach(MIMEText(plain_text, "plain"))
            msg_alternative.attach(MIMEText(body_text, "html"))
        else:
            msg_alternative.attach(MIMEText(body_text, "plain"))

        message.attach(msg_alternative)

        # ================
        # 4) Attachments
        # ================
        if attachments:
            from utils.s3_utils import read_binary_from_s3

            for att in attachments:
                try:
                    s3_key = att.get("s3_key")
                    filename = att.get("original_filename") or att.get("filename")
                    mime_type = att.get("mime_type", "application/octet-stream")

                    if not s3_key or not filename:
                        print(f"⚠️ Skipping invalid attachment: {att}")
                        continue

                    file_data = read_binary_from_s3(s3_key)
                    if not file_data:
                        print(f"⚠️ Failed to load attachment from S3: {s3_key}")
                        continue

                    # Parse MIME
                    maintype, subtype = (
                        mime_type.split("/", 1)
                        if "/" in mime_type
                        else (mime_type, "octet-stream")
                    )

                    part = MIMEBase(maintype, subtype)
                    part.set_payload(file_data)
                    encoders.encode_base64(part)

                    part.add_header(
                        "Content-Disposition",
                        "attachment",
                        filename=filename,
                    )

                    message.attach(part)

                except Exception as e:
                    print(f"❌ Error attaching file: {e}")
                    continue

        # ================
        # 5) Encode & send
        # ================
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

        message_body = {"raw": raw, "threadId": thread_id}

        try:
            sent = (
                self.service.users()
                .messages()
                .send(userId="me", body=message_body)
                .execute()
            )
            return sent

        except Exception as e:
            raise ValueError(f"Gmail reply failed: {e}") from e

    def send_forward(
        self, receipent_emails, subject, body_text, cc=None, bcc=None, attachments=None
    ):
        """
        Forward an email to recipients.

        Args:
            receipent_emails (str|list): Recipient email(s)
            subject (str): Email subject
            body_text (str): Email body
            cc (str|list, optional): CC recipients
            bcc (str|list, optional): BCC recipients
            attachments (list[dict], optional): List of attachments with keys:
                - 's3_key': S3 path to file
                - 'filename': Original filename
                - 'mime_type': MIME type

        Returns:
            dict: Gmail API response with message and thread IDs
        """
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        from email.mime.base import MIMEBase
        from email import encoders
        import base64

        if attachments is None:
            attachments = []

        # Use multipart/mixed for attachments, multipart/alternative otherwise
        if attachments:
            message = MIMEMultipart("mixed")
        else:
            message = MIMEMultipart("alternative")

        if isinstance(receipent_emails, list):
            to_email_str = ", ".join(receipent_emails)
        else:
            to_email_str = receipent_emails
        message["To"] = to_email_str
        if cc:
            message["Cc"] = cc
        if bcc:
            message["Bcc"] = bcc
        message["Subject"] = (
            f"Fwd: {subject}" if not subject.lower().startswith("fwd:") else subject
        )

        # Detect if body is HTML or plain text
        is_html = (
            body_text.strip().lower().startswith(("<html", "<div", "<p", "<!doctype"))
        )

        if attachments:
            # Create multipart/alternative for the body content
            msg_alternative = MIMEMultipart("alternative")
            if is_html:
                # For HTML, add plain text first, then HTML (RFC 2046 specifies most featured last)
                # Extract plain text from HTML by removing tags
                import re

                plain_text = re.sub("<[^<]+?>", "", body_text)
                plain_text = re.sub(r"\s+", " ", plain_text).strip()

                plain_part = MIMEText(plain_text, "plain")
                msg_alternative.attach(plain_part)

                # Then add HTML version (most featured)
                html_part = MIMEText(body_text, "html")
                msg_alternative.attach(html_part)
            else:
                # If plain text, just attach as text
                part = MIMEText(body_text, "plain")
                msg_alternative.attach(part)

            # Add body to main message
            message.attach(msg_alternative)

            # Process and attach files
            from utils.s3_utils import read_binary_from_s3

            print(f"📎 Attaching {len(attachments)} file(s) to forward...")
            for att in attachments:
                try:
                    s3_key = att.get("s3_key")
                    # Use original_filename if available (from upload handler), otherwise use filename
                    filename = att.get("original_filename") or att.get("filename")
                    mime_type = att.get("mime_type", "application/octet-stream")

                    if not s3_key or not filename:
                        print(
                            f"⚠️ Skipping attachment - missing s3_key or filename: {att}"
                        )
                        continue

                    # Read file from S3
                    file_data = read_binary_from_s3(s3_key)
                    if not file_data:
                        print(f"⚠️ Failed to read attachment from S3: {s3_key}")
                        continue

                    # Parse MIME type
                    maintype, subtype = (
                        mime_type.split("/", 1)
                        if "/" in mime_type
                        else (mime_type, "octet-stream")
                    )

                    # Create attachment part
                    attachment = MIMEBase(maintype, subtype)
                    attachment.set_payload(file_data)
                    encoders.encode_base64(attachment)
                    # Use proper Content-Disposition header with filename parameter for best compatibility
                    attachment.add_header(
                        "Content-Disposition", "attachment", filename=filename
                    )

                    # Add to message
                    message.attach(attachment)
                    print(f"✅ Attached: {filename} ({len(file_data)} bytes)")

                except Exception as e:
                    print(f"❌ Error attaching file {att.get('filename')}: {str(e)}")
                    continue
        else:
            # No attachments - use simpler structure
            if is_html:
                # If HTML, attach as HTML (Gmail will render it)
                part = MIMEText(body_text, "html")
                message.attach(part)
            else:
                # If plain text, just attach as text
                part = MIMEText(body_text, "plain")
                message.attach(part)

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        message_body = {"raw": raw}

        sent = (
            self.service.users()
            .messages()
            .send(userId="me", body=message_body)
            .execute()
        )
        return sent
