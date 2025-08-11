import os
import uuid
import logging
import requests
from dotenv import load_dotenv
from typing import List
from pydantic import BaseModel
import shutil
import nltk

nltk.download("averaged_perceptron_tagger_eng", quiet=True)
import numpy as np
from langchain_community.document_loaders import (
    DirectoryLoader,
    TextLoader,
    PyMuPDFLoader,
    UnstructuredWordDocumentLoader,
    UnstructuredPowerPointLoader,
    UnstructuredExcelLoader,
)
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from langchain.prompts import ChatPromptTemplate
from langchain.chains.llm import LLMChain
from langchain_core.documents import Document

# ────────────────────────
# Setup Logging
# ────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


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
        self.lancedb_url = os.getenv("LANCE_DB_IP")
        self.user_id = user_id
        self.dimension = 3072
        self.embeddings = OpenAIEmbeddings(
            model="text-embedding-3-large",
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            dimensions=self.dimension,
        )

        # Pre-load the LLM and chain for reuse
        self.llm = ChatOpenAI(model="gpt-4", temperature=0.2)

        # --- FIX: Define prompt_template as a ChatPromptTemplate object ---
        # Define the prompt template string
        _template_string = """
        You are an intelligent assistant that extracts relevant information from a block of text based on a user query.

        Query: "{query}"

        Context:
        \"\"\"
        {context}
        \"\"\"

        Instructions:
        - If the query is about a specific **page**, return the **URL** of that page.
        - If the query is asking for **related questions**, return the relevant **Frequently Asked Questions (FAQs)** from the context.
        - If both types of information are found, include both.
        - If no relevant information exists, respond with: **"No relevant content found."**

        Extract only the most relevant part of the context that answers the query. Be concise and direct.
        """

        # Create the ChatPromptTemplate object
        self.prompt_template = ChatPromptTemplate.from_template(_template_string)
        # --- END FIX ---

        # Use the created prompt_template object in the RunnableSequence
        self.relevance_chain = self.prompt_template | self.llm

    def check_user(self):
        """
        Check if the user exists in the LanceDB.
        """
        try:
            user_id = self.user_id
            response = requests.get(f"{self.lancedb_url}/check_user/{user_id}")
            if response.status_code == 200:
                return response.json().get("exists", False)
            else:
                logger.error(
                    f"[✘] Failed to check user {user_id}: {response.status_code} - {response.text}"
                )
                return False
        except Exception as e:
            logger.error(f"[!] Exception during user check: {str(e)}")
            return False

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
        # print("the docs lengths", doc_lengths)
        avg_length = sum(doc_lengths) // len(doc_lengths) if doc_lengths else 1000
        # print("the average length is ", avg_length)

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

    def process_document(self, file_path: str, filename: str):
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
            self.send_batch_to_lancedb(vector_batch)
            return {"vectors_made": len(vector_batch), "docs_processed": len(documents)}
        else:
            return {"vectors_made": 0, "docs_processed": len(documents)}

    def send_batch_to_lancedb(self, vector_batch: List[VectorData]):
        payload = [vec.dict() for vec in vector_batch]

        logger = logging.getLogger("google_route.lance_agent")

        try:
            response = requests.post(f"{self.lancedb_url}/insert_batch", json=payload)
            if response.status_code == 200:
                logger.info(f"[✔] Inserted {len(payload)} vectors.")
            else:
                logger.error(
                    f"[✘] Batch insert failed: {response.status_code} - {response.text}"
                )
        except Exception as e:
            logger.error(f"[!] Exception during batch insert: {str(e)}")

    def query_vector(self, query_input: QueryInput):
        try:
            query_text = query_input.query_text
            top_k = query_input.top_k
            user_id = query_input.user_id

            # Create embedding
            # embedding = np.array(self.embeddings.embed_query(query_text), dtype=np.float32).tolist()
            vector = self.embeddings.embed_query(query_text)

            # Now construct valid QueryData
            query_payload = QueryData(user_id=user_id, embedding=vector, top_k=top_k)
            response = requests.post(
                f"{self.lancedb_url}/query", json=query_payload.dict()
            )
            response.raise_for_status()
            results = response.json().get("results")
            logger.info(f"[🔍] Retrieved {len(results)} results.")
            return results

        except Exception as e:
            logger.error(f"[!] Query failed: {str(e)}")
            raise e

    def extract_relevant_text(self, query: str, context: str) -> str:
        # For RunnableSequence, 'invoke' returns a message object, so we extract content.
        # print("context from the lancedb", context)
        response = self.relevance_chain.invoke({"query": query, "context": context})
        print(
            "response from extraction", response
        )  # This will print the AIMessage object
        # You need to access .content from the AIMessage object
        return response.content.strip()

    def delete_file_Data(self, foldername: str):
        try:
            response = requests.post(
                f"{self.lancedb_url}/delete_folder",
                json={"user_id": self.user_id, "foldername": foldername},
            )
            if response.status_code == 200:
                logger.info(f"[✔] Successfully deleted file with ID: {foldername}")
                return {"status": "success", "message": f"File {foldername} deleted."}
            else:
                logger.error(
                    f"[✘] Failed to delete file {foldername}: {response.status_code} - {response.text}"
                )
                return {
                    "status": "error",
                    "message": f"Failed to delete file {foldername}.",
                }
        except Exception as e:
            logger.error(f"[!] Exception during file deletion: {str(e)}")
            return {"status": "error", "message": str(e)}
