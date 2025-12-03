from fireworks.client import Fireworks
from fireworks.client.audio import AudioInference
import os
import re
import yaml
import time
import requests
from dotenv import load_dotenv
import asyncio

from utils.base_logger import get_logger

logger = get_logger(__name__)
load_dotenv()
AUDIO_MODEL = os.getenv(
    "AUDIO_MODEL", "whisper-v3-turbo"
)  # Default to whisper-v3-turbo if not set
# AUDIO_MODEL = "accounts/fireworks/models/whisper-v3-turbo"
FK_VAL = os.getenv("FIREWORKS_KEY")


class Speech2TextService:
    def __init__(self, userid):
        self.user_id = userid
        self.model = Fireworks(api_key=FK_VAL)
        self.model_name = AUDIO_MODEL

    async def transcribe_audio(self, audio_path: str):
        # Prepare client
        client = AudioInference(
            model=self.model_name,
            base_url="https://audio-prod.us-virginia-1.direct.fireworks.ai",
            api_key=FK_VAL,
        )

        # Load file as bytes
        with open(audio_path, "rb") as f:
            audio_bytes = f.read()

        # Make request
        start = time.time()
        r = await client.transcribe_async(audio=audio_bytes)
        logger.info(f"Took: {(time.time() - start):.3f}s. Text: '{r.text}'")
        return r.text


# Run the async function properly
# if __name__ == "__main__":
#     service = Speech2TextService()
#     result = asyncio.run(service.transcribe_audio("agent_route/podcast_20min.mp3"))
#     # result = asyncio.run(service.transcribe_audio("agent_route/Recording.m4a"))
#    #print("Final transcription:", result)
