from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse
from typing import List, Optional
from datetime import datetime, timezone

MESSAGES = {}  # Store sent message metadata


class TwilioService:
    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_whatsapp_number: str,
        from_sms_number: str,
        from_call_number: str,
    ):
        """
        Initialize Twilio client for WhatsApp, SMS, and voice calls.
        """
        self.client = Client(account_sid, auth_token)
        self.from_whatsapp = f"whatsapp:{from_whatsapp_number}"
        self.from_sms = from_sms_number
        self.from_call = from_call_number

    # ---------------------- WhatsApp ----------------------
    def send_whatsapp_message(self, to: str, body: str) -> dict:
        to_whatsapp = f"whatsapp:{to}"
        sent = self.client.messages.create(
            from_=self.from_whatsapp, to=to_whatsapp, body=body
        )
        return self._store_message(sent.sid, to, body, "whatsapp")

    def send_whatsapp_media(
        self, to: str, media_url: str, body: Optional[str] = None
    ) -> dict:
        to_whatsapp = f"whatsapp:{to}"
        sent = self.client.messages.create(
            from_=self.from_whatsapp, to=to_whatsapp, body=body, media_url=[media_url]
        )
        return self._store_message(sent.sid, to, body, "whatsapp", media_url)

    # ---------------------- SMS ----------------------
    def send_sms(self, to: str, body: str) -> dict:
        sent = self.client.messages.create(from_=self.from_sms, to=to, body=body)
        return self._store_message(sent.sid, to, body, "sms")

    # ---------------------- Voice Call ----------------------
    def make_call(
        self, to: str, twiml: Optional[str] = None, message: Optional[str] = None
    ) -> dict:
        """
        Make a voice call. You can pass raw TwiML or a simple message to speak.
        """
        if message and not twiml:
            response = VoiceResponse()
            response.say(message)
            twiml = str(response)

        sent = self.client.calls.create(from_=self.from_call, to=to, twiml=twiml)
        return {
            "id": sent.sid,
            "from": self.from_call,
            "to": to,
            "type": "voice_call",
            "status": sent.status,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

    # ---------------------- Helper ----------------------
    def _store_message(
        self,
        sid: str,
        to: str,
        body: str,
        channel: str,
        media_url: Optional[str] = None,
    ) -> dict:
        """
        Internal method to store message metadata.
        """
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        MESSAGES[sid] = {
            "id": sid,
            "from": getattr(self, f"from_{channel}", None),
            "to": to,
            "body": body,
            "media_url": media_url,
            "timestamp": timestamp,
            "status": "sent",
            "source": channel,
            "direction": "outbound",
        }
        return MESSAGES[sid]

    def get_message_status(self, message_sid: str) -> dict:
        """
        Retrieve the delivery status of a sent message.
        """
        message = self.client.messages(message_sid).fetch()
        return {
            "id": message.sid,
            "status": message.status,
            "from": message.from_,
            "to": message.to,
            "body": message.body,
            "date_sent": message.date_sent,
        }

    def get_call_status(self, call_sid: str) -> dict:
        """
        Retrieve the status of a voice call.
        """
        call = self.client.calls(call_sid).fetch()
        return {
            "id": call.sid,
            "status": call.status,
            "from": call.from_,
            "to": call.to,
            "start_time": call.start_time,
            "end_time": call.end_time,
            "duration": call.duration,
        }
