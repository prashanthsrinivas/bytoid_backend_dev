from cryptography.fernet import Fernet
import base64
import time
import os
from dotenv import load_dotenv

load_dotenv()
SECRET_KEY = os.getenv("SECRETKEY")
# fernet = Fernet(SECRET_KEY)
fernet = Fernet(base64.urlsafe_b64encode(SECRET_KEY.encode("utf-8").ljust(32)[:32]))


def generate_hashed_url(base_url: str, invited_by: str, invited_to: str) -> str:
    expiry_time = int(time.time()) + 3600  # 1 hour validity

    # Build payload with raw emails (not hashes)
    payload = f"{invited_by}|{invited_to}|{expiry_time}"

    # Encrypt the payload
    token = fernet.encrypt(payload.encode()).decode()

    return f"{base_url}={token}"


def dehashed_url(token: str) -> dict:
    """
    Decrypt token and return original emails + expiry
    """
    try:
        decoded = fernet.decrypt(token.encode()).decode()
        invited_by, invited_to, expiry = decoded.split("|")
        return {
            "invited_by": invited_by,
            "invited_to": invited_to,
            "expiry": int(expiry),
        }
    except Exception as e:
        return {"error": f"Invalid token: {str(e)}"}


def create_invited_user(email, invited_by,connection):
    pass
