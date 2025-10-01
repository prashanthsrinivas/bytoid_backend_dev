from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from email.message import EmailMessage
import base64
from email.mime.text import MIMEText
from db.rds_db import connect_to_rds, get_cursor
from data import MESSAGES  # delete this later, this is just for testing
from datetime import datetime, timezone
from googleapiclient.errors import HttpError
import traceback
import time
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional
from googleapiclient.http import BatchHttpRequest
from utils.base_logger import get_logger
from utils.s3_utils import attach_CLDFRNT_url
import random
from typing import Optional, Tuple, List
import re
from bs4 import BeautifulSoup

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
    def __init__(self, user_id, connection=None):
        # Use provided connection or get a new one
        self.conn = connection or connect_to_rds()

        if not self.conn:
            raise ConnectionError("❌ Failed to connect to RDS (too many connections?)")

        with get_cursor(self.conn) as cursor:
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

        # Build credentials object
        self.creds = Credentials(
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
                "openid",
                "https://www.googleapis.com/auth/contacts",
            ],
            expiry=row[4],
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
                #     print("id",i.get("id"))
                #     print("labelIds",i.get("labelIds"))
                #     print("snippet",i.get("snippet"))
                #     print("--------------------------------------")

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
        print("batch build started", len(thread_ids))

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
        print("returning from batch", len(thread_ids))
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
                            if "error" in current_results.get(t["id"], {})
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
                    t for t in chunk if "error" in results.get(t["id"], {})
                ]
                if failed_after_retries:
                    print(
                        f"❌ Chunk {chunk_idx} had {len(failed_after_retries)} threads that failed permanently"
                    )

            # Cooldown after all chunks complete
            cooldown = random.randint(5, 10)
            print(f"🕒 All {len(chunks)} chunks processed. Cooling down {cooldown}s...")
            await asyncio.sleep(cooldown)

            print("retuening results from fetch_threads_batch", len(results))
            return results

        finally:
            self.service_running = False
            print(f"current batch fetched {batch_count}")

    @staticmethod
    def get_message_body(msg):
        """
        Extracts message body, links, attachments, and calendar events from a Gmail message.
        Returns: (body_text, attachments_list)
        """
        payload = msg.get("payload", {})
        body = ""
        attachments = []

        def parse_part(part):
            nonlocal body, attachments
            mime_type = part.get("mimeType", "")
            part_body = part.get("body", {})
            data = part_body.get("data")

            # TEXT PARTS
            if mime_type == "text/plain" and data:
                decoded = base64.urlsafe_b64decode(data.encode("ASCII")).decode(
                    "utf-8", errors="ignore"
                )
                body += decoded

            elif mime_type == "text/html" and data:
                decoded = base64.urlsafe_b64decode(data.encode("ASCII")).decode(
                    "utf-8", errors="ignore"
                )
                soup = BeautifulSoup(decoded, "html.parser")
                # Extract visible text
                body += soup.get_text(separator="\n")
                # Extract links
                for a in soup.find_all("a", href=True):
                    attachments.append(
                        {"type": "link", "url": a["href"], "text": a.get_text()}
                    )

            # CALENDAR INVITES
            elif mime_type in ["text/calendar", "application/ics"] and data:
                decoded = base64.urlsafe_b64decode(data.encode("ASCII")).decode(
                    "utf-8", errors="ignore"
                )
                attachments.append({"type": "calendar", "content": decoded})

            # ATTACHMENTS
            if part.get("filename"):
                attachment_id = part_body.get("attachmentId")
                attachments.append(
                    {
                        "filename": part["filename"],
                        "mimeType": mime_type,
                        "attachmentId": attachment_id,
                    }
                )

            # RECURSE nested parts
            for sub_part in part.get("parts", []):
                parse_part(sub_part)

        # Start parsing
        if "parts" in payload:
            for part in payload["parts"]:
                parse_part(part)
        else:
            parse_part(payload)

        # Remove quoted previous messages
        split_patterns = [
            r"\nOn .* wrote:",
            r"\n>.*",
            r"\nFrom: .*",
        ]
        for pattern in split_patterns:
            body = re.split(pattern, body, maxsplit=1)[0]

        return body.strip(), attachments

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
                        from_header = headers.get("from", "Unknown Sender")
                        to_header = headers.get("to", "")

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
                        body, attachments = self.get_message_body(msg)

                        thread_data.append(
                            {
                                "thread_id": thread_id,
                                "messageId": message_id,
                                "from": from_header,
                                "to": to_header,
                                "email": from_email,  # sender’s email
                                "subject": headers.get("subject", "No Subject"),
                                "snippet": resp.get("snippet", ""),
                                "body": body,
                                "direction": direction,
                                "date": headers.get("date", ""),
                                "isRead": "UNREAD" not in labelids,
                                "isStarred": "STARRED" in labelids,
                                "labels": list(labelids),
                                "attachments": attachments,
                                "isSentByMe": my_email.lower() in from_header.lower(),
                            }
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

        print("✅ Returning results from batch", len(final_results))
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
            # print("-->", response)
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
            # print("-->", response)
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
                    f"in:inbox category:primary after:{after_ts} before:{before_ts}",
                    f"in:inbox after:{after_ts} before:{before_ts}",
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
        print("fetching date wise data", start_date, end_date)
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
            print("Error fetching history:", e)
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

    def send_reply(self, to, subject, thread_id, in_reply_to, body_text):
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
