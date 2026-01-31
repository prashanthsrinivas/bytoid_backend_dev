from asyncio.log import logger
from db.rds_db import connect_to_rds
import requests
import aiohttp
import asyncio
from bs4 import BeautifulSoup
import re


class OutlookService:
    def __init__(self, user_id):
        self.user_id = user_id

    async def get_total_message_count(self, months=3):
        """Get approximate count of messages"""
        try:
            conn = connect_to_rds()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT token FROM users WHERE user_id = %s", (self.user_id,)
            )
            row = cursor.fetchone()

            if not row:
                return 0

            access_token = row[0]
            headers = {"Authorization": f"Bearer {access_token}"}

            # Simple count request
            response = requests.get(
                "https://graph.microsoft.com/v1.0/me/messages/$count", headers=headers
            )

            if response.status_code == 200:
                return int(response.text)
            return 0

        except Exception as e:
            logger.error(f"Error getting message count: {str(e)}")
            return 0

    async def make_graph_request(
        self,
        method,
        url,
        params=None,
        data=None,
        json_data=None,
        headers=None,
        integration=None,
    ):
        """
        Universal Microsoft Graph API request handler.
        Handles:
        - Auth headers
        - Query params
        - JSON body
        - 429 retry
        - JSON parsing
        """
        #print("inside make_graph_request")
        if headers is None:
            headers = {}

        connection = connect_to_rds()
        if connection is None:
            return

        cursor = connection.cursor()
        #print(f"integration inside : {integration}")

        if integration:
            cursor.execute(
                "SELECT access_token FROM integrations WHERE user_id=%s",
                (self.user_id,),
            )
        else:
            cursor.execute("SELECT token FROM users WHERE user_id=%s", (self.user_id,))

        row = cursor.fetchone()
        if not row:
            return None

        access_token = row[0]

        # expired = check_microsoft_token_expiry_normal(cursor, self.user_id)
        # print(f"expired: {expired}")

        headers["Authorization"] = f"Bearer {access_token}"
        headers["Content-Type"] = "application/json"

        max_retries = 5
        retry_delay = 1

        async with aiohttp.ClientSession() as session:
            for attempt in range(max_retries):

                try:
                    async with session.request(
                        method,
                        url,
                        params=params,
                        json=json_data,
                        data=data,
                        headers=headers,
                        timeout=60,
                    ) as resp:

                        # Retry for rate-limit
                        if resp.status == 429:
                            retry_after = resp.headers.get("Retry-After")
                            delay = int(retry_after) if retry_after else retry_delay
                            await asyncio.sleep(delay + 1)
                            retry_delay *= 2
                            continue

                        # Handle bad tokens
                        if resp.status in (401, 403):
                            raise Exception(f"Auth failed: {resp.status}")

                        # Other client or server error
                        if resp.status >= 400:
                            text = await resp.text()
                            raise Exception(f"Graph error {resp.status}: {text}")

                        # Parse JSON
                        try:
                            return await resp.json()
                        except:
                            text = await resp.text()
                            return {"raw": text}

                except aiohttp.ClientError as e:
                    if attempt == max_retries - 1:
                        raise Exception(f"Network error: {str(e)}")
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2

        raise Exception("Graph request failed after retries")

    async def list_messages(self, params, integration=None):
        """
        Minimal wrapper around Microsoft Graph /me/messages
        Only used for getting message metadata (conversationId, receivedDateTime)
        """
        #print("inside list_messages")
        url = "https://graph.microsoft.com/v1.0/me/messages"

        response = await self.make_graph_request(
            method="GET", url=url, params=params, integration=integration
        )

        return response

    async def list_messages_with_body(self, params=None, top=50):
        #print("inside list_messages_with_body")

        url = "https://graph.microsoft.com/v1.0/me/messages"

        if params is None:
            params = {}
        params.setdefault("$top", top)

        #print(f"**********")
        #print(f"params : {params}")

        response = await self.make_graph_request(method="GET", url=url, params=params)

        if not response or "value" not in response:
            return []

        result = []

        for msg in response["value"]:
            message_id = msg["id"]

            # Fetch full message
            msg_detail = await self.make_graph_request(
                method="GET",
                url=f"https://graph.microsoft.com/v1.0/me/messages/{message_id}",
                params={
                    "$select": "id,subject,body,receivedDateTime,from,toRecipients,ccRecipients,bccRecipients,conversationId"
                },
            )

            if not msg_detail:
                continue

            result.append(
                {
                    "id": msg_detail.get("id"),
                    "subject": msg_detail.get("subject"),
                    "body": msg_detail.get("body", {}).get("content", ""),
                    "receivedDateTime": msg_detail.get("receivedDateTime"),
                    "conversationId": msg_detail.get("conversationId"),
                    "from": msg_detail.get("from"),
                    "toRecipients": msg_detail.get("toRecipients", []),
                    "ccRecipients": msg_detail.get("ccRecipients", []),
                    "bccRecipients": msg_detail.get("bccRecipients", []),
                }
            )

        return result

    async def list_messages_minimal(self, params=None, url=None, integration=None):
        if url is None:
            url = "https://graph.microsoft.com/v1.0/me/messages"

        response = await self.make_graph_request_for_getting_messages(
            method="GET", url=url, params=params, integration=integration
        )

        return response

    def clean_invisible_chars(self, text: str) -> str:
        # Remove invisible unicode characters
        invisible = [
            "\u034f",  # Combining grapheme joiner
            "\u200b",  # Zero width space
            "\u200c",  # Zero width non-joiner
            "\u200d",  # Zero width joiner
            "\u2060",  # Word joiner
            "\ufeff",  # Zero width no-break space
            "\u2800",  # Braille blank
        ]

        for ch in invisible:
            text = text.replace(ch, "")

        # Also collapse repeated spaces/newlines
        text = re.sub(r"\s+", " ", text).strip()
        return text

    async def batch_fetch_message_bodies_og(self, message_ids, integration=None):
        """
        Fetch up to 20 message bodies using Graph Batch API.
        message_ids: list of message IDs (max 20)
        """
        url = "https://graph.microsoft.com/v1.0/$batch"

        batch_requests = []
        for idx, msg_id in enumerate(message_ids):
            batch_requests.append(
                {
                    "id": str(idx + 1),
                    "method": "GET",
                    "url": f"/me/messages/{msg_id}?$select=id,conversationId,from,subject,body,receivedDateTime,toRecipients,ccRecipients,bccRecipients",
                }
            )

        payload = {"requests": batch_requests}

        response = await self.make_graph_request_for_getting_messages(
            method="POST", url=url, json_data=payload, integration=integration
        )

        results = []

        if "responses" not in response:
            return results
        # print(f"response : {response}")
        for item in response["responses"]:
            if 200 <= item.get("status", 0) < 300:
                body = item.get("body", {})
                html_content = body.get("body", {}).get("content", "")
                # plain = html2text.html2text(html_content)
                # plain_text = clean_plain_text(plain)

                soup = BeautifulSoup(html_content, "html.parser")

                # Remove scripts/styles if present
                for tag in soup(["script", "style"]):
                    tag.decompose()

                cleaned = soup.get_text(separator="\n", strip=True)
                plain_text = self.clean_invisible_chars(cleaned)

                # Extract "from" email
                body = response["responses"][0]["body"]

                from_email = body["from"]["emailAddress"]["address"]

                from_name = body["from"]["emailAddress"]["name"]

                to_emails = [
                    t["emailAddress"]["address"] for t in body.get("toRecipients", [])
                ]

                to_names = [
                    t["emailAddress"]["name"] for t in body.get("toRecipients", [])
                ]

                cc_emails = [
                    c["emailAddress"]["address"] for c in body.get("ccRecipients", [])
                ]

                bcc_emails = [
                    b["emailAddress"]["address"] for b in body.get("bccRecipients", [])
                ]

                c = body.get("conversationId")
                #print("----------------------------")
                #print(f"in batch_fetch_messsage_bodies : {c}")
                #print("----------------------------")

                #print(f"id : {body.get('id')}")
                results.append(
                    {
                        "id": body.get("id"),
                        "subject": body.get("subject"),
                        "conversationId": body.get("conversationId"),
                        "receivedDateTime": body.get("receivedDateTime"),
                        "body": html_content,
                        "from": from_email,
                        "from_name": from_name,
                        "plain_text": plain_text,
                        "toRecipients": to_emails,
                        "to_names": to_names,
                        "cc": cc_emails,
                        "bcc": bcc_emails,
                    }
                )

        return results

    async def batch_fetch_message_bodies(self, message_ids, integration=None):
        """
        Fetch up to 20 message bodies using Graph Batch API.
        message_ids: list of message IDs (max 20)
        """
        url = "https://graph.microsoft.com/v1.0/$batch"

        batch_requests = []
        for idx, msg_id in enumerate(message_ids):
            batch_requests.append(
                {
                    "id": str(idx + 1),
                    "method": "GET",
                    "url": f"/me/messages/{msg_id}?$select=id,conversationId,from,subject,body,receivedDateTime,toRecipients,ccRecipients,bccRecipients",
                }
            )

        payload = {"requests": batch_requests}

        response = await self.make_graph_request_for_getting_messages(
            method="POST", url=url, json_data=payload, integration=integration
        )

        results = []

        if "responses" not in response:
            return results
        # print(f"response : {response}")
        response["responses"].sort(
            key=lambda r: r.get("body", {}).get("receivedDateTime", ""),
            reverse=True,
        )

        for item in response["responses"]:
            if 200 <= item.get("status", 0) < 300:
                body = item.get("body", {})
                html_content = body.get("body", {}).get("content", "")
                # plain = html2text.html2text(html_content)
                # plain_text = clean_plain_text(plain)

                soup = BeautifulSoup(html_content, "html.parser")

                # Gmail quotes
                for quote in soup.select(".gmail_quote, .gmail_quote_container"):
                    quote.decompose()

                # Outlook reply headers ONLY
                for hdr in soup.select("div[id^='divRplyFwdMsg'] p"):
                    hdr.decompose()

                # Remove scripts/styles if present
                for tag in soup(["script", "style"]):
                    tag.decompose()

                cleaned_html = str(soup)
                cleaned = soup.get_text(separator="\n", strip=True)
                plain_text = self.clean_invisible_chars(cleaned)

                # Extract "from" email
                body = item.get("body", {})

                from_email = body["from"]["emailAddress"]["address"]

                from_name = body["from"]["emailAddress"]["name"]

                to_emails = [
                    t["emailAddress"]["address"] for t in body.get("toRecipients", [])
                ]

                to_names = [
                    t["emailAddress"]["name"] for t in body.get("toRecipients", [])
                ]

                cc_emails = [
                    c["emailAddress"]["address"] for c in body.get("ccRecipients", [])
                ]

                bcc_emails = [
                    b["emailAddress"]["address"] for b in body.get("bccRecipients", [])
                ]

                c = body.get("conversationId")
                #print("----------------------------")
                #print(f"in batch_fetch_messsage_bodies : {c}")
                #print("----------------------------")

                #print(f"id : {body.get('id')}")
                results.append(
                    {
                        "id": body.get("id"),
                        "subject": body.get("subject"),
                        "conversationId": body.get("conversationId"),
                        "receivedDateTime": body.get("receivedDateTime"),
                        "body": cleaned_html,
                        "from": from_email,
                        "from_name": from_name,
                        "plain_text": plain_text,
                        "toRecipients": to_emails,
                        "to_names": to_names,
                        "cc": cc_emails,
                        "bcc": bcc_emails,
                    }
                )

        return results

    async def process_conversations_batch(
        self, conv_ids, my_email, batch_count, integration=None
    ):
        results = {}

        for conv_id in conv_ids:
            try:
                all_messages = []

                url = None
                params = {
                    "$top": 100,
                    "$select": "id,conversationId,receivedDateTime",
                    "$orderby": "receivedDateTime desc",
                }

                message_ids_to_fetch = []

                # ---- STEP 1: Collect all message IDs for this conversation ----
                while True:
                    page = await self.list_messages_minimal(
                        params=params, url=url, integration=integration
                    )

                    if not page or "value" not in page:
                        break

                    for msg in page["value"]:
                        if msg.get("conversationId") == conv_id:
                            message_ids_to_fetch.append(msg["id"])

                    url = page.get("@odata.nextLink")
                    params = None  # nextLink includes params

                    if not url:
                        break

                # ---- STEP 2: Fetch message bodies in batches of 20 ----
                for i in range(0, len(message_ids_to_fetch), 20):
                    batch_ids = message_ids_to_fetch[i : i + 20]
                    batch_results = await self.batch_fetch_message_bodies(
                        batch_ids, integration=integration
                    )
                    all_messages.extend(batch_results)

                results[conv_id] = (all_messages, None)

                #print(f"✔️ Conversation {conv_id}: fetched {len(all_messages)} messages")

            except Exception as e:
                #print(f"⚠️ Error fetching conversation {conv_id}: {e}")
                results[conv_id] = (None, e)

        return results

    async def make_graph_request_for_getting_messages(
        self,
        method: str,
        url: str,
        params=None,
        data=None,
        json_data=None,
        headers=None,
        integration=None,
    ):
        """
        Unified Microsoft Graph API request function.
        Features:
        - Correct handling of GET/POST/PATCH
        - Removes request body for GET (important for Graph)
        - Handles OAuth token loading
        - Handles 429 retry with backoff
        - Handles nextLink absolute URLs
        - Clean JSON parsing
        """

        if headers is None:
            headers = {}

        # ---------------------------
        # 1. Load access token
        # ---------------------------
        connection = connect_to_rds()
        if not connection:
            raise Exception("DB connection failed")

        cursor = connection.cursor()

        if integration:
            cursor.execute(
                "SELECT access_token FROM integrations WHERE user_id=%s",
                (self.user_id,),
            )
        else:
            cursor.execute("SELECT token FROM users WHERE user_id=%s", (self.user_id,))

        row = cursor.fetchone()

        if not row:
            raise Exception("No token found")

        access_token = row[0]

        # ---------------------------
        # 3. Prepare headers
        # ---------------------------
        headers["Authorization"] = f"Bearer {access_token}"
        headers["Content-Type"] = "application/json"

        # ---------------------------
        # 4. GET requests must never send body
        # ---------------------------
        if method.upper() == "GET":
            json_data = None
            data = None

        max_retries = 5
        retry_delay = 1

        # ---------------------------
        # 5. Make request with retries
        # ---------------------------
        async with aiohttp.ClientSession() as session:
            for attempt in range(max_retries):

                try:
                    async with session.request(
                        method=method,
                        url=url,
                        params=params,  # OK for GET
                        json=json_data,  # OK for POST/PATCH
                        data=data,
                        headers=headers,
                        timeout=60,
                    ) as resp:

                        # Rate limit
                        if resp.status == 429:
                            retry_after = int(
                                resp.headers.get("Retry-After", retry_delay)
                            )
                            await asyncio.sleep(retry_after)
                            retry_delay *= 2
                            continue

                        # Authentication failed
                        if resp.status in (401, 403):
                            text = await resp.text()
                            raise Exception(f"Auth failed ({resp.status}): {text}")

                        # Other errors
                        if resp.status >= 400:
                            text = await resp.text()
                            raise Exception(f"Graph error {resp.status}: {text}")

                        # Parse JSON safely
                        try:
                            return await resp.json()
                        except:
                            # Sometimes responses aren't JSON (rare)
                            raw = await resp.text()
                            return {"raw": raw}

                except aiohttp.ClientError as e:
                    if attempt == max_retries - 1:
                        raise Exception(f"Network failure: {str(e)}")

                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2

        raise Exception("Graph request failed after all retries.")
