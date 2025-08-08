from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from email.message import EmailMessage
import base64
from email.mime.text import MIMEText
from db.rds_db import connect_to_rds
from data import MESSAGES  # delete this later, this is just for testing
from datetime import datetime, timezone


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
            ],
            expiry=row[4],  # Must be datetime, not string
        )

        self.creds = creds_data
        self.service = build("gmail", "v1", credentials=self.creds)

        profile = self.service.users().getProfile(userId="me").execute()
        self.user_email = profile["emailAddress"]
        print(
            f"GmailService initialized for user: {self.user_email}, user_id: {user_id}"
        )

    def parse_headers(self, headers):
        header_dict = {}
        for h in headers:
            header_dict[h["name"].lower()] = h["value"]
        return header_dict

    def get_threads(self, email_type="INBOX", max_results=10):
        try:
            response = (
                self.service.users()
                .threads()
                .list(userId="me", maxResults=max_results, labelIds=[email_type])
                .execute()
            )

        except Exception as e:
            print("A general error occurred:", str(e))
        response = (
            self.service.users()
            .threads()
            .list(userId="me", maxResults=max_results, labelIds=[email_type])
            .execute()
        )
        threads = response.get("threads", [])
        thread_data = []
        for thread in threads:
            thread_id = thread["id"]
            thread_detail = (
                self.service.users().threads().get(userId="me", id=thread_id).execute()
            )
            messages = thread_detail.get("messages", [])

            if not messages:
                continue
            latest_message = messages[-1]
            headers = latest_message.get("payload", {}).get("headers", [])
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

            thread_data.append(
                {
                    "thread_id": thread_id,
                    "messageId": message_id,
                    "from": parsed.get("from", "Unknown Sender"),
                    "email": email,
                    "subject": parsed.get("subject", "No Subject"),
                    "snippet": thread_detail.get("snippet", ""),
                    "body": latest_message.get("snippet", ""),
                    "date": parsed.get("date", ""),
                    "isRead": "UNREAD" not in latest_message.get("labelIds", []),
                    "isStarred": "STARRED" in latest_message.get("labelIds", []),
                    "labels": latest_message.get("labelIds", []),
                    "attachments": [],  # You can enhance this to parse actual attachments
                }
            )

        return thread_data

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
            print(f"to inside gmail service before sending mail is:{to}")

            sent = (
                self.service.users()
                .messages()
                .send(userId="me", body=message_body)
                .execute()
            )
            print(f"to inside gmail service after sending mail is:{to}")

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

    # def send_reply(
    #     self, conversation_id, to, subject, thread_id, in_reply_to, body_text, user_id
    # ):

    #     # Defensive checks
    #     if not to:
    #         raise ValueError("Recipient email 'to' is required")
    #     if not subject:
    #         raise ValueError("Subject is required")
    #     if not thread_id:
    #         raise ValueError("Thread ID is required")
    #     if not in_reply_to:
    #         raise ValueError("In-Reply-To message ID is required")

    #     message = EmailMessage()
    #     message["To"] = to
    #     message["Subject"] = (
    #         f"Re: {subject}" if not subject.lower().startswith("re:") else subject
    #     )
    #     message["In-Reply-To"] = in_reply_to
    #     message["References"] = in_reply_to
    #     message.set_content(body_text)

    #     raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    #     message_body = {"raw": raw, "threadId": thread_id}

    #     sent = (
    #         self.service.users()
    #         .messages()
    #         .send(userId="me", body=message_body)
    #         .execute()
    #     )

    #     timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    #     message_id = sent["id"]

    #     MESSAGES[message_id] = {
    #         "id": message_id,
    #         "from": self.user_email,
    #         "to": to,
    #         "body": body_text,
    #         "subject": subject,
    #         "timestamp": timestamp,
    #         "status": "sent",
    #         "source": "gmail",
    #         "direction": "outbound",
    #         "user_id": user_id,
    #         "thread_id": thread_id,
    #         "message_id": message_id,
    #         "conversation_id": conversation_id,
    #     }
    #     print("✅ Saved sent message to MESSAGES:")

    #     return sent

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

        print(f"[DEBUG] Message sent to Gmail API. ID: {sent['id']}")
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
