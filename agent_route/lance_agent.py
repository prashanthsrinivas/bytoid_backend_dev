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
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_fireworks import ChatFireworks
from langchain.prompts import ChatPromptTemplate
from utils.fireworkzz import get_firework_embedding, get_fireworks_response
from db.lance_db_service import LanceDBServer
from utils.normal import load_yaml_file

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


# ────────────────────────
# LanceClient Class
# ────────────────────────
class LanceClient:
    def __init__(self, user_id: str):
        load_dotenv()
        self.user_id = user_id
        self.dimension = 2880
        self.service = LanceDBServer()
        #     embeddings = OpenAIEmbeddings(
        # #     model="text-embedding-3-large",
        # #     openai_api_key=os.getenv("OPENAI_API_KEY"),
        # #     dimensions=3072,
        # # )

        # 🔹 Embeddings (you can keep this or replace with nomic-embed-text-v1)
        self.embeddings = get_firework_embedding()

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

    async def process_document(self, file_path: str, filename: str):
        documents = self.langchainprocessDocs(file_path)
        vector_batch = []

        for doc in documents:
            text = doc.page_content.strip()
            if not text:
                continue

            try:
                vector = self.embeddings.embed_query(text)
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
            if not vector:
                vector = self.embeddings.embed_query(query_input.query_text)
            payload = QueryData(
                user_id=query_input.user_id,
                embedding=vector,
                top_k=query_input.top_k,
            )

            results = await self.service.query_vector(payload.dict())
            logger.info(f"[🔍] Retrieved doc {len(results)} results.")

            return results

        except Exception as e:
            logger.error(f"[!] Query failed: {str(e)}")
            raise

    async def audio_query_vector(
        self,
        query_input: QueryInput,
        vector=None,
        sender_email=None,
    ):
        try:
            if not vector:
                vector = self.embeddings.embed_query(query_input.query_text)

            payload = QueryData(
                user_id=query_input.user_id,
                embedding=vector,
                top_k=query_input.top_k,
            )

            results = await self.service.rec_query_vector(payload.dict())
            logger.info(f"[🔍] Retrieved audio {len(results)} results.")

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

                    # print("contacts appended", contacts)
                    if (sender_email and sender_email in contacts) or "All" in contacts:
                        # print("appended here")
                        if "text" in r:
                            br["text"] = r["text"]

                        need_to_return.append(br)

            return need_to_return

        except Exception as e:
            logger.error(f"[!] Query failed: {str(e)}")
            raise

    def scrape_query_vector(
        self,
        query_input: QueryInput,
        vector=None,
        sender_email=None,
    ):
        try:
            if not vector:
                vector = self.embeddings.embed_query(query_input.query_text)

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

    async def mixed_query_vector(self, query_input, sender_email=None):
        try:
            print("started mixed query")

            vector = self.embeddings.embed_query(query_input.query_text)

            docs_results = await self.query_vector(query_input, vector=vector)
            aud_results = await self.audio_query_vector(
                sender_email=sender_email, query_input=query_input, vector=vector
            )
            scrape_results = self.scrape_query_vector(
                sender_email=sender_email, query_input=query_input, vector=vector
            )

            # ---- normalize to text ----
            docs_data = self.extract_text(docs_results)
            audio_data = self.extract_text(aud_results)
            website_data = self.extract_text(scrape_results)

            prompts = load_yaml_file(path=pathconfig.agent_template)
            base_prompt = prompts.get("multi_source_information_analyzer")

            full_prompt = base_prompt.format(
                users_query=query_input.query_text,
                docs_data=docs_data,
                audio_data=audio_data,
                website_data=website_data,
            )

            ai_response = get_fireworks_response(full_prompt,role="system")

            if ai_response:
                print("the ai extracted information", len(ai_response))
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
