import os
from dotenv import load_dotenv

load_dotenv()
dev = os.getenv("DEV", "").lower()
IS_DEV = dev == "true"
# Only production-safe origins here
PROD_ORIGINS = {
    "https://www.bytoid.ai",
    "https://bytoid.ai",
    "https://app.bytoid.ai",
    "https://api.bytoid.ai",
}

# Dev-only origins
DEV_ORIGINS = {
    "https://preview--bytoid-45.lovable.app",
    "https://preview--bytoiddev.lovable.app",
    "https://dev.bytoid.ai",
}

ALLOWED_ORIGINS = PROD_ORIGINS | (DEV_ORIGINS if IS_DEV else set())
if IS_DEV:
    ACCESSIBLE_IDS = ["109161866299858012556", "113605503284012967393"]
else:
    ACCESSIBLE_IDS = ["113605503284012967393"]
BACKURL = (
    "https://rtdtj5q9dh.execute-api.ca-central-1.amazonaws.com"
    if IS_DEV
    else "https://api.bytoid.ai"
)
