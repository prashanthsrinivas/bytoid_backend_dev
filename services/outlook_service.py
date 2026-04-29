import asyncio
import aiohttp
import requests
from datetime import datetime, timedelta, timezone
import uuid
from bs4 import BeautifulSoup
from db.rds_db import connect_to_rds, get_cursor
from utils.base_logger import get_logger
from typing import List, Dict, Any, Optional, Tuple
import time
import traceback
import json
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import base64
from utils.s3_utils import attach_CLDFRNT_url
from dotenv import load_dotenv
import os

logger = get_logger(__name__)
load_dotenv()
basefrntpath = f"{os.getenv('BASE_FRNT_URL')}"


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


class OutlookService:
    """Service for fetching Outlook emails with batch processing and semaphore control"""

    def __init__(
        self,
        user_id,
        connection=None,
        batch_size: int = 25,
        concurrent_limit: int = 5,
        testing=False,
        workflow=None,
        wf_id=None,
    ):
        # Use provided connection or get a new one
        self.conn = connection or connect_to_rds()
        self.user_id = user_id
        self.batch_size = batch_size
        self.concurrent_limit = concurrent_limit
        self.access_token = None
        self.refresh_token = None
        self.user_email = None
        self.base_url = "https://graph.microsoft.com/v1.0"
        self.service_running = False
        self.testing = testing
        self.workflow = workflow
        self.current_wf_id = wf_id

        if not self.conn:
            raise ConnectionError("❌ Failed to connect to RDS (too many connections?)")

        # Get Microsoft credentials from database
        with get_cursor(self.conn) as cursor:
            cursor.execute(
                """
                SELECT client_id, client_secret, token, refresh_token, expiry, email
                FROM users
                WHERE user_id = %s 
                """,
                (str(user_id),),
            )
            row = cursor.fetchone()

            if not row:
                raise ValueError(f"User not found: {user_id}")
            (
               db_client_id,
               db_client_secret,
               self.access_token,
               self.refresh_token,
               expiry,
               self.user_email,
            ) = row
            self.client_id = db_client_id or os.getenv("MICROSOFT_CLIENT_ID2")
            self.client_secret = db_client_secret or os.getenv("MICROSOFT_CLIENT_SECRET2")


            # ✅ ADD THIS (VERY IMPORTANT)
            if not self.access_token:
               raise ValueError(f"Outlook not connected for user {user_id}")

        # Check token expiry and refresh if needed
        if expiry and isinstance(expiry, (str, datetime)):
            expiry_dt = (
                datetime.fromisoformat(expiry) if isinstance(expiry, str) else expiry
            )
            # Ensure both datetimes are timezone-aware for comparison
            if expiry_dt.tzinfo is None:
                expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)

            current_time = datetime.now(timezone.utc)
            if expiry_dt < current_time:
                self._refresh_access_token()

        if connection is None:
            self.conn.close()

        logger.info(f"✅ Outlook service initialized for user: {self.user_email}")

    def _refresh_access_token(self):
        """Refresh the Microsoft access token using refresh token"""
        try:
            refresh_url = "https://login.microsoftonline.com/common/oauth2/v2.0/token"


            data = {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
                "scope": "User.Read Mail.ReadWrite Mail.Send",
            }

            response = requests.post(refresh_url, data=data)

            if response.status_code == 200:
                token_data = response.json()
                self.access_token = token_data["access_token"]

                # Update database with new token
                with get_cursor(self.conn) as cursor:
                    cursor.execute(
                        """
                        UPDATE users
                        SET token = %s, expiry = %s 
                        WHERE user_id = %s
                        """,
                        (
                            self.access_token,
                            datetime.now(timezone.utc)
                            + timedelta(seconds=token_data.get("expires_in", 3600)),
                            str(self.user_id),
                        ),
                    )
                self.conn.commit()
                logger.info(f"✅ Token refreshed successfully for user {self.user_id}")
            else:
                logger.error(f"❌ Token refresh failed: {response.text}")
                raise ValueError("Token refresh failed. User must re-authenticate")

        except Exception as e:
            logger.error(f"❌ Exception during token refresh: {str(e)}")
            raise ValueError(f"Token refresh failed: {e}")

    def _get_date_filter(self, months: int = 3) -> str:
        """Generate date filter for last N months"""
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=months * 30)  # Approximate months

        start_date_str = start_date.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        # Filter for emails received in the last N months
        return f"receivedDateTime ge {start_date_str}"

    def get_contacts(self):
        """Get contacts from Outlook messages (similar to Gmail service)"""
        logger.info("🔍 Starting get_contacts method...")
        try:
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Accept": "application/json",
            }

            url = f"{self.base_url}/me/messages"
            params = {"$top": 500, "$select": "from,toRecipients"}

            response = requests.get(url, headers=headers, params=params)

            if response.status_code != 200:
                logger.error(f"❌ Failed to fetch messages: {response.text}")
                return []

            messages = response.json().get("value", [])
            logger.info(f"📬 Found {len(messages)} messages to process")

            email_set = set()
            successful_messages = 0
            failed_messages = 0

            for i, message in enumerate(messages):
                try:
                    logger.info(f"🔄 Processing message {i+1}/{len(messages)}")

                    # Extract from email
                    from_email = (
                        message.get("from", {})
                        .get("emailAddress", {})
                        .get("address", "")
                    )
                    if from_email:
                        email_set.add(from_email)

                    # Extract to emails
                    to_recipients = message.get("toRecipients", [])
                    for recipient in to_recipients:
                        to_email = recipient.get("emailAddress", {}).get("address", "")
                        if to_email:
                            email_set.add(to_email)

                    successful_messages += 1

                except Exception as e:
                    failed_messages += 1
                    logger.error(f"❌ Error processing message: {e}")
                    continue

            final_emails = list(email_set)
            logger.info(
                f"✅ get_contacts completed - Success: {successful_messages}, Failed: {failed_messages}, Unique emails: {len(final_emails)}"
            )
            return final_emails

        except Exception as e:
            logger.error(f"❌ Error in get_contacts: {str(e)}")
            return []

    async def get_total_message_count(self, months: int = 3) -> int:
        """Get total count of messages for the last N months"""
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "text/plain",
        }

        date_filter = self._get_date_filter(months)
        url = f"{self.base_url}/me/messages/$count"
        params = {"$filter": date_filter}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, params=params) as response:
                    if response.status == 200:
                        count = await response.text()
                        return int(count)
                    else:
                        logger.error(
                            f"❌ Failed to get message count: {response.status}"
                        )
                        return 0
        except Exception as e:
            logger.error(f"❌ Error getting message count: {str(e)}")
            return 0

    async def _fetch_messages_batch(
        self, skip: int = 0, top: int = None, months: int = 3
    ) -> Dict[str, Any]:
        """Fetch a single batch of messages"""
        if top is None:
            top = self.batch_size

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
        }

        date_filter = self._get_date_filter(months)
        url = f"{self.base_url}/me/messages"

        params = {
            "$filter": date_filter,
            "$orderby": "receivedDateTime desc",
            "$top": top,
            "$skip": skip,
            "$select": "id,subject,body,from,toRecipients,receivedDateTime,sentDateTime,sender,internetMessageId,conversationId,hasAttachments,importance,flag,categories,isDraft",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        return {
                            "status": "success",
                            "messages": data.get("value", []),
                            "count": len(data.get("value", [])),
                            "next_link": data.get("@odata.nextLink"),
                        }
                    else:
                        error_text = await response.text()
                        logger.error(
                            f"❌ Batch fetch failed: {response.status} - {error_text}"
                        )
                        return {
                            "status": "error",
                            "error": f"HTTP {response.status}: {error_text}",
                            "messages": [],
                            "count": 0,
                        }
        except Exception as e:
            logger.error(f"❌ Exception in batch fetch: {str(e)}")
            return {"status": "error", "error": str(e), "messages": [], "count": 0}

    def _process_message(self, email_data: Dict) -> Optional[Dict[str, Any]]:
        """Process a single email message (similar to Gmail message processing)"""
        try:
            email_id = email_data.get("id")
            from_name = (
                email_data.get("sender", {}).get("emailAddress", {}).get("name", "")
            )
            to_recipients = email_data.get("toRecipients", [])

            body_content = email_data.get("body", {}).get("content", "")
            internet_message_id = email_data.get("internetMessageId")
            conversation_id = email_data.get("conversationId")

            # Parse HTML content to plain text
            soup = BeautifulSoup(body_content, "html.parser")
            plain_text = soup.get_text().strip()

            subject = email_data.get("subject", "")
            sent_time = email_data.get("sentDateTime")
            received_time = email_data.get("receivedDateTime")

            from_address = (
                email_data.get("from", {}).get("emailAddress", {}).get("address", "")
            )
            to_email = ""
            to_name = ""

            if to_recipients:
                to_email = to_recipients[0]["emailAddress"]["address"]
                to_name = to_recipients[0]["emailAddress"]["name"]

            # Determine direction
            direction = (
                "inbound"
                if from_address.lower() != self.user_email.lower()
                else "outbound"
            )

            # Use conversation_id from Outlook or fall back to email-based grouping
            conv_id = conversation_id or (
                from_address if direction == "inbound" else to_email
            )

            message_id = str(uuid.uuid4())

            return {
                "id": message_id,
                "email_id": email_id,
                "from": from_name,
                "from_email": from_address,
                "to": to_name,
                "to_email": to_email,
                "body": plain_text,
                "subject": subject,
                "timestamp": sent_time or received_time,
                "received_time": received_time,
                "sent_time": sent_time,
                "status": "received" if direction == "inbound" else "sent",
                "source": "outlook",
                "direction": direction,
                "conversation_id": conv_id,
                "internet_message_id": internet_message_id,
                "has_attachments": email_data.get("hasAttachments", False),
            }
        except Exception as e:
            logger.error(f"❌ Error processing message: {str(e)}")
            return None

    async def get_threads_async(
        self,
        email_type=None,
        max_results=100,
        batch_delay=0.5,
        start_page_token=None,
        months=3,
    ):
        """
        Async version of get_threads with continuous batch support (similar to Gmail)
        """
        try:
            all_messages = []
            total_fetched = 0
            skip_count = 0
            batch_count = 0

            logger.info(f"📧 My email: {self.user_email}")
            logger.info(f"🎯 Target: {max_results} messages, Last {months} months")

            while True:
                try:
                    batch_count += 1
                    logger.info(
                        f"🔄 Batch {batch_count}: Fetching messages (already got {total_fetched})..."
                    )

                    # Fetch batch of messages with larger batch size for efficiency
                    batch_size = min(
                        100, max_results - total_fetched if max_results else 100
                    )
                    batch_result = await self._fetch_messages_batch(
                        skip=skip_count, top=batch_size, months=months
                    )

                    if batch_result["status"] != "success":
                        logger.error(
                            f"❌ Batch {batch_count} fetch failed: {batch_result.get('error')}"
                        )
                        break

                    messages = batch_result["messages"]
                    logger.info(
                        f"📬 Batch {batch_count}: Retrieved {len(messages)} messages"
                    )

                    if not messages:
                        logger.info("📭 No more messages found")
                        break

                    # Process messages concurrently with semaphore
                    semaphore = asyncio.Semaphore(self.concurrent_limit)

                    async def process_with_semaphore(message):
                        async with semaphore:
                            return await self._process_single_message_async(message)

                    tasks = [process_with_semaphore(message) for message in messages]
                    logger.info(f"🚀 Processing {len(tasks)} messages concurrently...")
                    results = await asyncio.gather(*tasks, return_exceptions=True)

                    # Count successful and failed messages
                    successful_messages = 0
                    failed_messages = 0

                    for i, result in enumerate(results):
                        if isinstance(result, Exception):
                            failed_messages += 1
                            logger.error(
                                f"💥 Failed to process message {messages[i].get('id', 'unknown')}: {str(result)}"
                            )
                        elif result:
                            all_messages.append(result)
                            successful_messages += 1
                        else:
                            failed_messages += 1

                    logger.info(
                        f"📊 Batch {batch_count} complete - Success: {successful_messages}, Failed: {failed_messages}"
                    )

                    # Add delay between batches to avoid rate limiting
                    if batch_delay > 0:
                        logger.info(
                            f"😴 Sleeping for {batch_delay} seconds to avoid rate limits..."
                        )
                        await asyncio.sleep(batch_delay)

                    total_fetched += len(messages)
                    skip_count += len(messages)
                    logger.info(f"📊 Total messages processed so far: {total_fetched}")

                    # Check if we've reached the max_results limit
                    if max_results and total_fetched >= max_results:
                        logger.info(f"🏁 Reached max_results limit of {max_results}")
                        return (
                            all_messages,
                            skip_count,
                        )  # Return skip count for continuation

                    # Check if we got fewer messages than requested (end of data)
                    if len(messages) < batch_size:
                        logger.info("🏁 Reached end of available messages")
                        return all_messages, None

                except Exception as e:
                    logger.error(
                        f"❌ Error fetching message batch {batch_count}: {str(e)}"
                    )
                    break

            logger.info(
                f"✅ Completed! Total messages fetched: {len(all_messages)} across {batch_count} batches"
            )
            return all_messages, None

        except Exception as e:
            logger.error(f"💥 A general error occurred in get_threads_async: {str(e)}")
            return [], None

    async def _process_single_message_async(self, message, max_retries=3):
        """
        Async version of message processing with proper error handling
        """
        message_id = message.get("id", "unknown")

        for attempt in range(max_retries + 1):
            try:
                if attempt > 0:
                    # Wait before retrying (exponential backoff)
                    wait_time = 2**attempt
                    logger.info(
                        f"⏳ Retrying message {message_id} in {wait_time} seconds (attempt {attempt + 1}/{max_retries + 1})"
                    )
                    await asyncio.sleep(wait_time)

                # Process the message
                processed_message = self._process_message(message)

                if processed_message:
                    return processed_message
                else:
                    logger.warning(f"⚠️ Message {message_id} could not be processed")
                    return None

            except Exception as e:
                if attempt == max_retries:
                    logger.error(
                        f"💥 Failed to process message {message_id} after {max_retries + 1} attempts: {str(e)}"
                    )
                    return None
                else:
                    logger.warning(
                        f"⚠️ Attempt {attempt + 1} failed for message {message_id}: {str(e)}"
                    )

        return None

    def send_email(
        self, to_email: str, subject: str, body_text: str, conversation_id: str = None
    ):
        """Send an email using Microsoft Graph API"""
        try:
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
            }

            payload = {
                "message": {
                    "subject": subject,
                    "body": {"contentType": "Text", "content": body_text},
                    "toRecipients": [{"emailAddress": {"address": to_email}}],
                },
                "saveToSentItems": True,
            }

            url = f"{self.base_url}/me/sendMail"
            response = requests.post(url, headers=headers, json=payload)

            if response.status_code == 202:
                logger.info(f"✅ Email sent successfully to {to_email}")
                return {"status": "success", "message": "Email sent"}
            else:
                logger.error(f"❌ Failed to send email: {response.text}")
                return {"status": "error", "error": response.text}

        except Exception as e:
            logger.error(f"❌ Exception sending email: {str(e)}")
            return {"status": "error", "error": str(e)}

    def send_invitation_email(
        self,
        invitee: str,
        inviter: str,
        role: dict,
        invite_link: str = "",
        business_info: dict = None,
    ):
        """
        Send a styled invitation email using Microsoft Graph API
        (similar to Gmail service invitation method)
        """
        role_name = role.get("name", "User")

        # Build optional extra info
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

        # Fallback invite link
        if not invite_link:
            invite_link = f"{basefrntpath}/invite/{role.get('id')}"

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
                Made with ❤️ by <a href="{basefrntpath}" style="color:#2563eb; text-decoration:none;">Bytoid.io</a>
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

        try:
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
            }

            payload = {
                "message": {
                    "subject": f"Invitation to join as {role_name}",
                    "body": {"contentType": "Html", "content": body_html},
                    "toRecipients": [{"emailAddress": {"address": invitee}}],
                },
                "saveToSentItems": True,
            }

            url = f"{self.base_url}/me/sendMail"
            response = requests.post(url, headers=headers, json=payload)

            if response.status_code == 202:
                logger.info(f"✅ Invitation email sent successfully to {invitee}")
                return {"status": "success", "message": "Invitation sent"}
            else:
                logger.error(f"❌ Failed to send invitation: {response.text}")
                return {"status": "error", "error": response.text}

        except Exception as e:
            logger.error(f"❌ Exception sending invitation: {str(e)}")
            return {"status": "error", "error": str(e)}

    def parse_headers(self, headers):
        """Parse email headers (utility method similar to Gmail service)"""
        parsed = {}
        for header in headers:
            name = header.get("name", "").lower()
            value = header.get("value", "")

            if name in ["from", "to", "subject", "date", "message-id"]:
                parsed[name] = value

        return parsed

    async def fetch_emails_batch_with_semaphore(
        self, months: int = 3, max_messages: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Main method to fetch emails in batches with semaphore control
        This is the primary interface method similar to Gmail service
        """
        logger.info(f"🚀 Starting batch email fetch for last {months} months")

        try:
            # Get total message count first
            total_count = await self.get_total_message_count(months)
            logger.info(f"📊 Total messages to fetch: {total_count}")

            if total_count == 0:
                return {
                    "status": "success",
                    "total_messages": 0,
                    "processed_messages": 0,
                    "batches_processed": 0,
                    "messages": [],
                }

            # Limit messages if max_messages is specified
            fetch_limit = min(max_messages or total_count, total_count)
            logger.info(f"📊 Will fetch up to {fetch_limit} messages")

            # Use the get_threads_async method for actual fetching
            messages, _ = await self.get_threads_async(
                max_results=fetch_limit, months=months
            )

            return {
                "status": "success",
                "total_messages": total_count,
                "processed_messages": len(messages),
                "batches_processed": (len(messages) + self.batch_size - 1)
                // self.batch_size,
                "messages": messages,
            }

        except Exception as e:
            logger.error(f"❌ Error in batch email fetch: {str(e)}")
            return {"status": "error", "error": str(e), "messages": []}


# Utility functions for backward compatibility and easy access
async def fetch_outlook_emails_batch(
    user_id: str, months: int = 3, max_messages: Optional[int] = None
) -> Dict[str, Any]:
    """Fetch Outlook emails in batches - main function for batch processing"""
    try:
        # Use larger batch size for better performance
        service = OutlookService(user_id, batch_size=100, concurrent_limit=5)
        return await service.fetch_emails_batch_with_semaphore(
            months=months, max_messages=max_messages
        )
    except Exception as e:
        logger.error(f"❌ Error in fetch_outlook_emails_batch: {str(e)}")
        return {"status": "error", "error": str(e), "messages": []}


async def fetch_outlook_emails_simple(user_id: str, months: int = 3) -> Dict[str, Any]:
    """Simple Outlook email fetch - for backward compatibility"""
    try:
        service = OutlookService(user_id)
        messages, _ = await service.get_threads_async(max_results=1000, months=months)

        return {
            "status": "success",
            "total_messages": len(messages),
            "messages": messages,
        }
    except Exception as e:
        logger.error(f"❌ Error in fetch_outlook_emails_simple: {str(e)}")
        return {"status": "error", "error": str(e), "messages": []}


def send_outlook_email(
    user_id: str,
    to_email: str,
    subject: str,
    body_text: str,
    conversation_id: str = None,
):
    """Send an Outlook email - utility function"""
    try:
        service = OutlookService(user_id)
        return service.send_email(to_email, subject, body_text, conversation_id)
    except Exception as e:
        logger.error(f"❌ Error sending Outlook email: {str(e)}")
        return {"status": "error", "error": str(e)}
