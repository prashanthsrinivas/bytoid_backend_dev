import os
import yaml
import re


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def load_yaml_file(path):
    if not os.path.exists(path):
        return None  # Important: use None to differentiate from empty list
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or []
    except yaml.YAMLError as e:
        print(f"❌ Error reading YAML file at {path}: {e}")
        return []


def can_reply_to_email(email: str) -> bool:
    """
    Returns True if it is safe to reply to this email.
    Skips system/no-reply/admin/bounce/automation emails.
    """
    if not email:
        return False

    email = email.lower().strip()

    # List of patterns to block
    blocked_keywords = [
        "no-reply",
        "noreply",
        "donotreply",
        "do-not-reply",
        "system",
        "postmaster",
        "bounce",
        "mailer-daemon",
        "undeliverable",
        "return",
        "notifications",
        "alerts",
        "updates",
        "robot",
        "automation",
    ]

    if any(keyword in email for keyword in blocked_keywords):
        return False

    # Basic email format validation
    pattern = r"^[\w\.-]+@[\w\.-]+\.\w+$"
    if not re.match(pattern, email):
        return False

    return True
