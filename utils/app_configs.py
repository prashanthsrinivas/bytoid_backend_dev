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
    "http://localhost:8080",
    "http://127.0.0.1:8080",
    "http://localhost:8081",
    "http://localhost:19000",
    "http://localhost:19006",
    "http://172.31.5.214",
    "https://dev.bytoid.ai",
}

ALLOWED_ORIGINS = PROD_ORIGINS | (DEV_ORIGINS if IS_DEV else set())
