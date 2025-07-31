import os
import json
import time
import requests

class TokenManager:
    def __init__(self, storage_path="tokens"):
        self.path = storage_path
        os.makedirs(self.path, exist_ok=True)

    def _file_path(self, provider):
        return os.path.join(self.path, f"{provider}.json")

    def get(self, provider):
        try:
            with open(self._file_path(provider), "r") as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def save(self, provider, token_data):
        with open(self._file_path(provider), "w") as f:
            json.dump(token_data, f)

    def delete(self, provider):
        try:
            os.remove(self._file_path(provider))
        except FileNotFoundError:
            pass

    def is_expired(self, provider):
        token = self.get(provider)
        if not token:
            return True
        return time.time() >= token.get("expires_at", 0)

    def refresh(self, provider):
        if provider == "gmail":
            return self._refresh_gmail()
        elif provider == "facebook":
            return self._refresh_facebook()
        elif provider == "outlook":
            return self._refresh_outlook()
        else:
            raise NotImplementedError(f"No refresh method for provider: {provider}")

    def _refresh_gmail(self):
        token = self.get("gmail")
        if not token:
            raise ValueError("No stored token for Gmail.")

        refresh_token = token.get("refresh_token")
        client_id = token.get("client_id")
        client_secret = token.get("client_secret")
        token_uri = token.get("token_uri", "https://oauth2.googleapis.com/token")

        if not all([refresh_token, client_id, client_secret]):
            raise ValueError("Missing required fields in Gmail token.")

        response = requests.post(token_uri, data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        })

        if response.status_code == 200:
            new_token = response.json()
            new_token.update({
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
                "expires_at": time.time() + new_token.get("expires_in", 3600),
                "token_uri": token_uri
            })
            self.save("gmail", new_token)
            return new_token
        else:
            raise Exception(f"Failed to refresh Gmail token: {response.text}")

    def _refresh_facebook(self):
        raise NotImplementedError("Facebook token refresh logic not implemented.")

    def _refresh_outlook(self):
        raise NotImplementedError("Outlook token refresh logic not implemented.")
