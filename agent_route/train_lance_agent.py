import asyncio
import inspect
from dotenv import load_dotenv
import os
import json
import logging
from typing import List
from pydantic import BaseModel
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.documents import Document
import time
from utils.fireworkzz import get_firework_embedding
from db.lance_db_service import LanceDBServer
from credits_route.route import Credits


load_dotenv()
logger = logging.getLogger(__name__)


class VectorData(BaseModel):
    user_id: str
    id: str
    text: str
    embedding: List[float]
    foldername: str


class DeleteData(BaseModel):
    user_id: str
    id: str


class TrainLanceAgent:
    def __init__(self, user_id: str):
        self.user_id = user_id
        self.dimension = 4096
        self.embeddings = None
        # asyncio.create_task(self._load_embeddings())
        self.lance_service = LanceDBServer()
        self.json_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1200,
            chunk_overlap=200,
            length_function=len,
            separators=["\n\n", "\n", " ", ""],
        )

        self.plain_splitter = RecursiveCharacterTextSplitter(
            chunk_size=800,
            chunk_overlap=100,
            length_function=len,
            separators=["\n\n", "\n", " ", ""],
        )

    async def _load_embeddings(self):
        self.embeddings = await get_firework_embedding()

    async def _ensure_embeddings(self):
        if self.embeddings is None:
            await self._load_embeddings()

    def process_single_audio_json(self, file_path, filename):
        """
        Reads a single transcript JSON file and normalizes the structure.
        """
        print("inside the proce")
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.error(f"[AUDIO] Failed to read transcript JSON: {e}")
            return None

        # transcript JSON saved by /process_audio
        transcript_id = data.get("id")
        text = data.get("transcript") or data.get("text")
        summary = data.get("summary", "")

        return {
            "user_id": self.user_id,
            "id": transcript_id,
            "plain_text": text,
            "summary": summary,
            "original_data": data,
            "foldername": str(filename),
        }

    async def embed_single_audio_json(self, file_path, filename, credits):
        """
        Embeds a single transcript JSON created by /process_audio.
        """
        # print("in the audio docs")
        await self._ensure_embeddings()
        record = self.process_single_audio_json(file_path, filename)
        if not record:
            # print("no record")
            return {"vectors_made": 0, "docs_processed": 0}

        user_id = record["user_id"] or self.user_id
        transcript_id = record["id"]
        foldername = record["foldername"]
        text = record["plain_text"]
        original_data = record["original_data"]

        if isinstance(text, dict):
            text = text.get("ciphertext", "")
        if not text:
            # print("no text")
            return {"vectors_made": 0, "docs_processed": 1}

        document = Document(
            page_content=text,
            metadata={
                "user_id": user_id,
                "id": transcript_id,
                "foldername": foldername,
            },
        )

        chunks = self.json_splitter.split_documents([document])
        logger.info(f"[AUDIO] Transcript {transcript_id} → {len(chunks)} chunks")

        vector_batch = []

        total_input_chars = 0
        total_output_chars = 0

        for c in chunks:
            ctext = c.page_content.strip()
            if not ctext:
                continue

            embedding = self.embeddings.embed_query(ctext)

            vector_obj = VectorData(
                id=transcript_id,
                user_id=user_id,
                text=json.dumps(original_data, ensure_ascii=False),
                embedding=embedding,
                foldername=foldername,
            )

            vector_batch.append(vector_obj)

            total_input_chars += len(ctext)
            total_output_chars += 4096

        if vector_batch:
            logger.info(f"[AUDIO] Sending {len(vector_batch)} vectors to LanceDB")
            await self.send_json_batch_to_lancedb(vector_batch)

        # ---------- calculate credits -------------------------
        total_chars = total_input_chars + total_output_chars

        await credits.update_ai_credits_redis(
            credit_type="embedding",
            total_chars=total_chars,
            user_id=user_id,
            reference_id=inspect.stack()[0].function,
        )
        # ------------------------------------------------------

        return {"vectors_made": len(vector_batch), "docs_processed": 1}

    async def send_json_batch_to_lancedb(self, vector_batch, batch_size=50):
        total = len(vector_batch)
        # print("vector batch in train", vector_batch)
        logger.info(
            f"Sending {total} vectors to insert_umail_vectors in batches of {batch_size}"
        )

        MAX_ATTEMPTS = 3
        BACKOFF = 1.5

        def safe_json(response):
            if response is None:
                return None
            if isinstance(response, dict):
                return response
            if hasattr(response, "json") and callable(response.json):
                return response.json()
            if hasattr(response, "text"):
                try:
                    return json.loads(response.text)
                except:
                    return {"error": "invalid_json", "raw": response.text}
            if asyncio.iscoroutine(response):
                raise RuntimeError(
                    "insert_umail_vectors returned coroutine – missing await"
                )
            return response

        results = []

        for start in range(0, total, batch_size):
            batch = vector_batch[start : start + batch_size]

            logger.info(
                f"→ Sending batch {start // batch_size + 1} ({len(batch)} items)"
            )

            for vector in batch:
                for attempt in range(1, MAX_ATTEMPTS + 1):
                    try:
                        response = await self.lance_service.rec_insert_vector(vector)
                        results.append(safe_json(response))
                        break

                    except Exception as e:
                        logger.error(
                            f"[Attempt {attempt}] Failed vector {vector.id}: {e}"
                        )
                        if attempt == MAX_ATTEMPTS:
                            raise

                        sleep_time = BACKOFF**attempt
                        time.sleep(sleep_time)

        return results

    async def delete_rec_lance(self, base_id):
        data = DeleteData(user_id=self.user_id, id=base_id)
        response = await self.lance_service.rec_delete_vector(data)
        return response
