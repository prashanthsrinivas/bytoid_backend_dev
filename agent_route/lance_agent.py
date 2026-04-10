import asyncio
import inspect
import json
import os
import uuid
from cust_helpers import pathconfig
from dotenv import load_dotenv
from typing import List
from pydantic import BaseModel
from utils.base_logger import get_logger

from langchain_community.document_loaders import (
    DirectoryLoader,
    TextLoader,
    PyMuPDFLoader,
    UnstructuredWordDocumentLoader,
    UnstructuredPowerPointLoader,
    UnstructuredExcelLoader,
)
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_fireworks import ChatFireworks
from langchain_core.prompts import ChatPromptTemplate
from utils.fireworkzz import get_firework_embedding, get_fireworks_response
from db.lance_db_service import LanceDBServer
from utils.normal import load_yaml_file
from credits_route.route import Credits
from request_context import current_user_id
import numpy as np

import re
from html import unescape


def normalize_for_embedding(value):
    """
    Turns ANY input (dict, list, html, text, numbers) into clean searchable text
    """
    if value is None:
        return ""

    if isinstance(value, (int, float, bool)):
        return str(value)

    if isinstance(value, str):
        # Remove HTML
        text = re.sub(r"<[^>]+>", " ", value)
        text = unescape(text)
        return text

    if isinstance(value, list):
        return " ".join(normalize_for_embedding(v) for v in value)

    if isinstance(value, dict):
        parts = []
        for k, v in value.items():
            parts.append(str(k))
            parts.append(normalize_for_embedding(v))
        return " ".join(parts)

    return str(value)


# ────────────────────────
# Setup Logging
# ────────────────────────
logger = get_logger(__name__)


# ────────────────────────
# Data Model
# ────────────────────────
class VectorData(BaseModel):
    user_id: str
    id: str
    text: str
    embedding: List[float]
    foldername: str


class QueryInput(BaseModel):
    user_id: str
    query_text: str
    top_k: int = 5


class QueryData(BaseModel):
    user_id: str
    embedding: List[float]
    top_k: int = 5


class AppQueryInput(BaseModel):
    user_id: str
    query_text: str
    app_id: int
    endpoint_id: int
    top_k: int = 5


class AppQueryData(BaseModel):
    user_id: str
    embedding: List[float]
    top_k: int = 5
    app_id: int
    endpoint_id: int


# ────────────────────────
# LanceClient Class
# ────────────────────────
class LanceClient:
    def __init__(self, user_id: str, credits=None):
        load_dotenv()
        self.user_id = user_id
        self.dimension = 2880
        self.service = LanceDBServer()
        self.credits = credits
        #     embeddings = OpenAIEmbeddings(
        # #     model="text-embedding-3-large",
        # #     openai_api_key=os.getenv("OPENAI_API_KEY"),
        # #     dimensions=3072,
        # # )

        # 🔹 Embeddings (you can keep this or replace with nomic-embed-text-v1)
        self.embeddings = None
        # asyncio.create_task(self._load_embeddings())
        # 🔹 Use Fireworks Llama 3.1 405B Instruct instead of GPT-4
        self.llm = ChatFireworks(
            model=os.getenv("FIREWORKS_MODEL_EVAL"),
            fireworks_api_key=os.getenv("FIREWORKS_KEY"),
            temperature=0.2,
        )

        # --- Friendly prompt ---
        _template_string = """
            You are a friendly and helpful chatbot, acting like a supportive friend or mentor. Your goal is to answer user queries based on the given context in a **natural, human-like way**, keeping responses short, clear, and easy to understand.

            Keep in mind:
            - The user may be young (around 10th grade), may use short forms, typos, or casual language.
            - Always respond **kindly, patiently, and in a friendly tone**.
            - Never judge the user for mistakes or short questions.
            - Avoid anything sexual, offensive, or inappropriate. Even if the user asks about it, respond politely without engaging.

            Instructions:
            - If the query is about a specific **page**, mention the **URL** naturally in your reply.
            - If the query is asking for **related questions**, share the relevant **FAQs** naturally in your response.
            - If both types of information are found, include both in one smooth, readable message.
            - If the context does not contain exact information:
                - Provide 1–2 references, examples, or related ideas that could help answer the query.
                - You can also ask a polite clarifying question like "Can you tell me a bit more about what you mean?"
            - Always respond **like a friend**: short, casual, helpful, and human-like.
        """

        self.prompt_template = ChatPromptTemplate.from_template(_template_string)

        # 🔹 Combine the template with the LLM
        self.relevance_chain = self.prompt_template | self.llm

    async def _load_embeddings(self):
        self.embeddings = await get_firework_embedding()

    async def _ensure_embeddings(self):
        if self.embeddings is None:
            await self._load_embeddings()

    def langchainprocessDocs(self, file_path: str):
        all_documents = []

        # Mapping file extensions to loader classes
        extension_loader_map = {
            ".txt": (TextLoader, {"autodetect_encoding": True}),
            ".pdf": (PyMuPDFLoader, {}),
            ".docx": (UnstructuredWordDocumentLoader, {}),
            ".pptx": (UnstructuredPowerPointLoader, {}),
            ".xlsx": (UnstructuredExcelLoader, {}),
        }

        if os.path.isfile(file_path):
            ext = os.path.splitext(file_path)[1].lower()
            loader_cls_kwargs = extension_loader_map.get(ext)

            if loader_cls_kwargs:
                loader_cls, kwargs = loader_cls_kwargs
                try:
                    loader = loader_cls(file_path, **kwargs)
                    loaded_docs = loader.load()
                    logger.info(f"[📄] Loaded 1 file: {file_path}")
                    all_documents.extend(loaded_docs)
                except Exception as e:
                    logger.error(
                        f"[!] Failed to load file {file_path}: {type(e).__name__}: {e}"
                    )
            else:
                logger.warning(f"[⚠️] Unsupported file extension: {ext}")

        elif os.path.isdir(file_path):
            loaders = [
                (TextLoader, "**/*.txt"),
                (PyMuPDFLoader, "**/*.pdf"),
                (UnstructuredWordDocumentLoader, "**/*.docx"),
                (UnstructuredPowerPointLoader, "**/*.pptx"),
                (UnstructuredExcelLoader, "**/*.xlsx"),
            ]

            for loader_cls, pattern in loaders:
                try:
                    kwargs = (
                        {"loader_kwargs": {"autodetect_encoding": True}}
                        if loader_cls is TextLoader
                        else {}
                    )
                    loader = DirectoryLoader(
                        file_path,
                        glob=pattern,
                        loader_cls=loader_cls,
                        show_progress=True,
                        **kwargs,
                    )
                    loaded_docs = loader.load()
                    logger.info(
                        f"[+] Loaded {len(loaded_docs)} documents from pattern: {pattern}"
                    )

                    seen_sources = set()
                    for i, doc in enumerate(loaded_docs):
                        source = doc.metadata.get("source", f"Unknown source #{i}")
                        if source not in seen_sources:
                            logger.info(f"[📄] Processed file: {source}")
                            seen_sources.add(source)

                    all_documents.extend(loaded_docs)

                except Exception as e:
                    logger.error(
                        f"[!] Failed to load {pattern} files: {type(e).__name__}: {e}"
                    )
        else:
            logger.error(
                f"[!] Invalid path: {file_path} is neither file nor directory."
            )
            return []

        if not all_documents:
            logger.warning(
                "[⚠️] No documents loaded. Check file paths or encoding issues."
            )

        # splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
        # Estimate dynamic chunk size
        doc_lengths = [len(doc.page_content) for doc in all_documents]
        ##print("the docs lengths", doc_lengths)
        avg_length = sum(doc_lengths) // len(doc_lengths) if doc_lengths else 1000
        ##print("the average length is ", avg_length)

        # Heuristic: Clamp between 500 and 1500 characters
        dynamic_chunk_size = max(500, min(800, avg_length))

        splitter = RecursiveCharacterTextSplitter(
            chunk_size=dynamic_chunk_size,
            chunk_overlap=int(dynamic_chunk_size * 0.2),  # 20% overlap
        )
        docs = splitter.split_documents(all_documents)
        logger.info(
            f"[📄] Split into {len(docs)} chunks. and dynamic chunksize {dynamic_chunk_size} and overlap {int(dynamic_chunk_size * 0.2)}"
        )
        return docs

    async def process_document(self, file_path: str, filename: str, credits=None):
        # print("inside process_document ")
        await self._ensure_embeddings()
        documents = self.langchainprocessDocs(file_path)
        if not documents:
            return {"error": "unable to vectorize documents"}
        vector_batch = []

        total_input_chars = 0
        total_output_chars = 0
        logger.info("documents processed %s", len(documents))

        for doc in documents:
            text = doc.page_content.strip()
            if not text:
                continue

            total_input_chars += len(text)

            try:
                vector = self.embeddings.embed_query(text)
                total_output_chars += 4096

                vector_data = VectorData(
                    user_id=self.user_id,
                    id=str(uuid.uuid4()),
                    text=text,
                    embedding=vector,
                    foldername=filename,
                )
                vector_batch.append(vector_data)
            except Exception as e:
                logger.error(f"[!] Embedding failed: {e}")

        if vector_batch:
            await self.send_batch_to_lancedb(vector_batch)

        # --------- calculate credits -------------------

        total_chars = total_input_chars + total_output_chars

        credit_response = await credits.update_ai_credits_redis(
            credit_type="embedding",
            total_chars=total_chars,
            user_id=self.user_id,
            reference_id=inspect.stack()[0].function,
        )
        if (
            not credit_response
            or credit_response.get("error") == "INSUFFICIENT_CREDITS"
        ):
            return {
                "error": "INSUFFICIENT_CREDITS",
                "message": "Your credits are too low to continue processing.",
            }

        # ----------------------------------------------
        # print(f"************ vectors_made:{len(vector_batch)} ")
        return {
            "vectors_made": len(vector_batch),
            "docs_processed": len(documents),
        }

    # ------------------------------------------
    # PURE ASYNC INSERT
    # ------------------------------------------
    async def send_batch_to_lancedb(self, vector_batch: List[VectorData]):
        try:
            await self.service.insert_batch(vector_batch)
        except Exception as e:
            logger.error(f"[!] Exception during batch insert: {str(e)}")

    # ------------------------------------------
    # MAKE QUERY VECTOR ASYNC
    # ------------------------------------------
    async def query_vector(self, query_input: QueryInput, vector=None):
        try:
            await self._ensure_embeddings()
            total_output_chars = 0

            if not vector:

                vector = self.embeddings.embed_query(query_input.query_text)
                # --------- calculate credits -------------------

                total_input_chars = len(query_input.query_text)
                # total_output_chars = 0
                # total_output_chars += sum(len(vec) for vec in vector)
                total_output_chars = len(vector)

                total_chars = total_input_chars + total_output_chars

                # credits = Credits()
                await self.credits.update_ai_credits_redis(
                    credit_type="embedding",
                    total_chars=total_chars,
                    user_id=self.user_id,
                    reference_id=inspect.stack()[0].function,
                )

            payload = QueryData(
                user_id=query_input.user_id,
                embedding=vector,
                top_k=query_input.top_k,
            )

            results = await self.service.query_vector(payload.dict())
            # logger.info(f"[🔍] Retrieved doc {len(results)} results.")

            # ------------------------------------------------

            return results

        except Exception as e:
            logger.error(f"[!] Query failed: {str(e)}")
            raise

    async def query_app_endpoint(
        self, query_input: AppQueryInput, foldernames: List = None, vector=None
    ):
        try:
            await self._ensure_embeddings()
            total_output_chars = 0

            if not vector:

                vector = self.embeddings.embed_query(query_input.query_text)
                # --------- calculate credits -------------------

                total_input_chars = len(query_input.query_text)
                total_output_chars = len(vector)

                total_chars = total_input_chars + total_output_chars

                # credits = Credits()
                await self.credits.update_ai_credits_redis(
                    credit_type="embedding",
                    total_chars=total_chars,
                    user_id=self.user_id,
                    reference_id=inspect.stack()[0].function,
                )

            payload = AppQueryData(
                user_id=query_input.user_id,
                embedding=vector,
                top_k=query_input.top_k,
                app_id=query_input.app_id,
                endpoint_id=query_input.endpoint_id,
                foldernames=foldernames,
            )

            results = await self.service.query_app_endpoint(payload.dict())
            # logger.info(f"[🔍] Retrieved doc {len(results)} results.")

            # ------------------------------------------------

            return results

        except Exception as e:
            logger.error(f"[!] Query failed: {str(e)}")
            raise

    async def save_app_run(
        self,
        user_id: str,
        app_id: str,
        endpoint_id: str,
        request_cfg: dict,
        result: dict,
        trigger: str,
        minute_bucket,
    ):
        await self._ensure_embeddings()

        table = await self.service._open_or_create_apiconnectors_table(
            user_id, app_id, endpoint_id
        )
        print("✅ save_app_run called", user_id, app_id, endpoint_id)
        # Searchable text
        text_blob = " ".join(
            [
                normalize_for_embedding(trigger),
                normalize_for_embedding(request_cfg),
                normalize_for_embedding(result),
            ]
        )

        # 🔥 Raw structured payload
        original_blob = json.dumps(
            {
                "trigger": trigger,
                "request": request_cfg,
                "response": result,
            },
            ensure_ascii=False,
        )

        embedding = np.array(
            self.embeddings.embed_query(text_blob[:30000]),
            dtype=np.float32,
        )

        record = {
            "id": minute_bucket,
            "foldername": minute_bucket,
            "text": text_blob,
            "original": original_blob,  # ✅ real JSON preserved
            "embedding": embedding,
        }

        await asyncio.to_thread(lambda: table.add([record]))
        await asyncio.sleep(0.5)
        print("📦 Stored record ID:", record["id"])
        return f"apiconnectors/{user_id}/{app_id}/{endpoint_id}/{minute_bucket}"

    async def audio_query_vector(
        self,
        query_input: QueryInput,
        vector=None,
        sender_email=None,
    ):
        try:
            await self._ensure_embeddings()
            if not vector:
                vector = self.embeddings.embed_query(query_input.query_text)
                # --------- calculate credits -------------------

                total_input_chars = len(query_input.query_text)
                # total_output_chars += sum(len(vec) for vec in vector)
                total_output_chars = len(vector)

                total_chars = total_input_chars + total_output_chars

                user_id = query_input.user_id
                # credits = Credits()
                await self.credits.update_ai_credits_redis(
                    credit_type="embedding",
                    total_chars=total_chars,
                    user_id=self.user_id,
                    reference_id=inspect.stack()[0].function,
                )

            payload = QueryData(
                user_id=query_input.user_id,
                embedding=vector,
                top_k=query_input.top_k,
            )

            results = await self.service.rec_query_vector(payload.dict())
            # logger.info(f"[🔍] Retrieved audio {len(results)} results.")

            # ------------------------------------------------

            need_to_return = []

            for br in results:
                # print("the keys", br.keys())

                # br["text"] is a JSON string, decode it
                if "text" in br:
                    try:
                        r = json.loads(br["text"])
                    except Exception:
                        logger.error("Invalid JSON in audio record text")
                        continue

                    contacts = r.get("contacts", [])
                    if "text" in r:
                        br["text"] = r["text"]

                    if "All" in contacts:
                        need_to_return.append(br)
                    elif isinstance(sender_email, list):
                        if any(email in contacts for email in sender_email):
                            need_to_return.append(br)
                    elif sender_email and sender_email in contacts:
                        need_to_return.append(br)

                    # print("contacts appended", contacts)
                    # if (sender_email and sender_email in contacts) or "All" in contacts:
                    #     # print("appended here")
                    #     if "text" in r:
                    #         br["text"] = r["text"]

                    #     need_to_return.append(br)

            return need_to_return

        except Exception as e:
            logger.error(f"[!] Query failed: {str(e)}")
            raise

    async def scrape_query_vector(
        self,
        query_input: QueryInput,
        vector=None,
        sender_email=None,
    ):
        try:
            await self._ensure_embeddings()
            if not vector:
                vector = self.embeddings.embed_query(query_input.query_text)
                # --------- calculate credits -------------------

                total_input_chars = len(query_input.query_text)
                # total_output_chars += sum(len(vec) for vec in vector)
                total_output_chars = len(vector)

                total_chars = total_input_chars + total_output_chars

                # credits = Credits()
                await self.credits.update_ai_credits_redis(
                    credit_type="embedding",
                    total_chars=total_chars,
                    user_id=self.user_id,
                    reference_id=inspect.stack()[0].function,
                )
                # -------------------------------------------------

            payload = QueryData(
                user_id=query_input.user_id,
                embedding=vector,
                top_k=query_input.top_k,
            )

            result = self.service.search_scraped_data(
                payload.dict(), sender_email=sender_email
            )
            # logger.info(f"[🔍] Retrieved scrape {len(result)} results.")

            return result

        except Exception as e:
            logger.error(f"[!] Query failed: {str(e)}")
            raise

    def extract_text(self, results):
        if not results:
            return ""
        if isinstance(results, list):
            return "\n".join(r.get("text", "") for r in results if isinstance(r, dict))
        if isinstance(results, dict):
            return results.get("text", "")
        return str(results)

    async def mixed_query_vector(
        self, query_input, sender_email=None, vector=None, wfchecker=None
    ):
        try:
            user_id = query_input.user_id or self.user_id

            # print("started mixed query")
            if not vector:
                # print("new vector")
                await self._ensure_embeddings()
                vector = self.embeddings.embed_query(query_input.query_text)

                # --------- calculate credits -------------------
                total_input_chars = len(query_input.query_text)
                # total_output_chars = 0
                # total_output_chars += sum(len(vec) for vec in vector)
                total_output_chars = len(vector)

                total_chars = total_input_chars + total_output_chars

                # credits = Credits()
                # print("cheees", inspect.stack()[0].function)
                await self.credits.update_ai_credits_redis(
                    credit_type="embedding",
                    total_chars=total_chars,
                    user_id=user_id,
                    reference_id=inspect.stack()[0].function,
                )
            # ----------------------------------------------

            docs_results = await self.query_vector(query_input, vector=vector)
            aud_results = await self.audio_query_vector(
                sender_email=sender_email, query_input=query_input, vector=vector
            )
            scrape_results = await self.scrape_query_vector(
                sender_email=sender_email, query_input=query_input, vector=vector
            )

            # ---- normalize to text ----
            docs_data = self.extract_text(docs_results)
            audio_data = self.extract_text(aud_results)
            website_data = self.extract_text(scrape_results)

            prompts = load_yaml_file(path=pathconfig.agent_template)
            if wfchecker:
                base_prompt = prompts.get("multi_source_workflow_context_analyzer")
            else:
                base_prompt = prompts.get("multi_source_information_analyzer")

            full_prompt = base_prompt.format(
                users_query=query_input.query_text,
                docs_data=docs_data,
                audio_data=audio_data,
                website_data=website_data,
            )

            ai_response = await get_fireworks_response(
                user_message=full_prompt,
                role="system",
                user_id=user_id,
                credits=self.credits,
            )

            if ai_response:
                # print("the ai extracted information", len(ai_response))
                return ai_response.strip()

            # fallback (raw data)
            mixed_ans = []
            if docs_results:
                mixed_ans.extend(docs_results)
            if aud_results:
                mixed_ans.extend(aud_results)
            if scrape_results:
                mixed_ans.append(scrape_results)

            return mixed_ans

        except Exception as e:
            logger.error(f"[!] Query failed: {str(e)}")
            raise

    # ------------------------------------------
    # FRIENDLY LLM EXTRACTOR (sync, OK)
    # ------------------------------------------
    def extract_relevant_text(self, query: str, context: str) -> str:
        response = self.relevance_chain.invoke({"query": query, "context": context})
        return response.content.strip()

    # ------------------------------------------
    # MAKE DELETE ASYNC
    # ------------------------------------------
    async def delete_file_Data(self, foldername: str):
        try:
            val = await self.service.delete_folder_async(
                user_id=self.user_id, foldername=foldername
            )
            if val:
                return {"status": "success", "message": f"File {foldername} deleted."}
            return {"status": "error", "message": f"Failed to delete {foldername}."}

        except Exception as e:
            logger.error(f"[!] Exception during file deletion: {str(e)}")
            return {"status": "error", "message": str(e)}

    async def delete_all_file_Data(self):
        try:
            val = await self.service.delete_all_user_Data(self.user_id)

            # val is the deleted count (0, 1, or more)
            return {
                "status": "success",
                "message": f"Deleted vector data for user {self.user_id}.",
            }

        except Exception as e:
            logger.error(f"[!] Exception during file deletion: {str(e)}")
            return {"status": "error", "message": str(e)}
