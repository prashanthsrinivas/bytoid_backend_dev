import os
import uuid
from credits_route.route import Credits
import lancedb
from dotenv import load_dotenv
from pydantic import BaseModel
from typing import Any, Callable, Dict, List, Optional, Sequence, Union
import numpy as np
import pyarrow as pa
import pandas as pd
import json, random, asyncio, time
from datetime import datetime, timedelta, timezone
from db.rds_db import connect_to_rds
from flask import jsonify
from utils.key_rotation_manager import SecureKMSService
import re
from utils.base_logger import get_logger

logger = get_logger(__name__)

load_dotenv()
db_key = os.getenv("LANCE_SERVERLESS")
db_uri = os.getenv("LANCE_SERVERLESS_URI")
# if not db_key and db_uri:
#    print("NEED LANCE DB DETAILS")

EMBEDDING_DIM = 4096
MetricsClientType = Any  # e.g., datadog client with increment/timing/gauge methods
ErrorHookType = Optional[Callable[[Exception, Dict[str, Any]], None]]


# ---- MODELS ----
class ScrapedData(BaseModel):
    user_id: str
    url: str
    title: str
    content: str
    contacts: Union[str, List[str]]  # ← FIXED
    timestamp: str
    metadata: dict
    embedding: List[float]  # primary vector
    pages_by_level: Dict[str, List[Any]]


class VectorData(BaseModel):
    user_id: str
    id: str
    text: str
    embedding: List[float]
    foldername: str


class QueryData(BaseModel):
    user_id: str
    embedding: List[float]
    top_k: int = 5


class DeleteData(BaseModel):
    user_id: str
    id: str


class BatchQueryData(BaseModel):
    user_id: str
    embeddings: List[List[float]]
    top_k: int = 5
    filenames: List[str] = []


class UmailData(BaseModel):
    id: str
    text: str
    embedding: List[float]
    user_id: str
    folder_name: str
    timestamp: str
    plain_text_embedding: List[float]


class SearchEmailQueryData(BaseModel):
    user_id: str
    embeddings: List[float]
    folder_names: Optional[List[str]] = None
    semantic_condition: Optional[str] = None


class Bytoid_pro(BaseModel):
    id: str
    chat_id: str
    role: str
    content: str
    timestamp: str
    images: Optional[List[str]] = []
    files: Optional[List[str]] = []
    embedding: List[float]


MetricsClientType = Any  # e.g., datadog client with increment/timing/gauge methods
ErrorHookType = Optional[Callable[[Exception, Dict[str, Any]], None]]


def retry_async(
    *,
    attempts: int = 3,
    initial_delay: float = 0.5,
    max_delay: float = 10.0,
    factor: float = 2.0,
    jitter: float = 0.1,
):
    """
    Async retry decorator with exponential backoff + optional jitter.

    Usage:
        @retry_async(attempts=4)
        async def fn(...): ...
    """

    def decorator(func):
        async def wrapper(*args, **kwargs):
            delay = initial_delay
            last_exc = None
            for attempt in range(1, attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:
                    last_exc = exc
                    # compute jitter
                    j = random.uniform(-jitter, jitter) * delay
                    sleep_for = min(max_delay, delay + j)
                    logger.warning(
                        "Attempt %d/%d failed for %s: %s — retrying in %.3fs",
                        attempt,
                        attempts,
                        getattr(func, "__name__", str(func)),
                        exc,
                        sleep_for,
                    )
                    await asyncio.sleep(sleep_for)
                    delay *= factor
            # All attempts exhausted
            logger.error(
                "All %d attempts failed for %s. Raising last exception.",
                attempts,
                getattr(func, "__name__", str(func)),
            )
            raise last_exc

        return wrapper

    return decorator


def _safe_json_parse(value):
    JSON_REGEX = re.compile(r"^\s*(\{.*\}|\[.*\])\s*$", re.DOTALL)
    if value is None:
        return {}

    # ✅ Already parsed
    if isinstance(value, (dict, list)):
        return value

    if not isinstance(value, str):
        return {}

    s = value.strip()

    # 🔁 Remove wrapping quotes if JSON is inside a string
    # Example: "{\"a\": 1}" → {"a": 1}
    if (s.startswith('"') and s.endswith('"')) or (
        s.startswith("'") and s.endswith("'")
    ):
        s = s[1:-1]
        s = s.replace('\\"', '"')

    # 🎯 Check if it LOOKS like JSON
    if not JSON_REGEX.match(s):
        return {}

    # 🧠 Parse once
    try:
        parsed = json.loads(s)

        # 🪆 Double-encoded JSON
        if isinstance(parsed, str) and JSON_REGEX.match(parsed.strip()):
            return json.loads(parsed)

        return parsed

    except Exception:
        return {}


def parse_ts(ts):
    """
    Safely parse a timestamp (ISO string or Unix epoch ms/s) into a timezone-aware datetime.
    Returns None if invalid.
    """
    if not ts or str(ts).lower() in ("init", "none", "null"):
        return None

    try:
        # Already a datetime?
        if isinstance(ts, datetime):
            return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)

        # Epoch ms or s
        if isinstance(ts, (int, float)) or str(ts).isdigit():
            val = int(ts)
            if val > 1e12:  # milliseconds
                return datetime.fromtimestamp(val / 1000, tz=timezone.utc)
            else:  # seconds
                return datetime.fromtimestamp(val, tz=timezone.utc)

        # ISO string variants
        s = str(ts).strip()
        s = s.replace("Z", "+00:00")
        # fix “space before offset” → replace last space with +
        if " " in s and s.rsplit(" ", 1)[-1].count(":") >= 1 and "+" not in s:
            # e.g. "2025-09-16T13:18:06.823 00:00" → "2025-09-16T13:18:06.823+00:00"
            parts = s.rsplit(" ", 1)
            s = parts[0] + "+" + parts[1]

        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


class MetricsClient:
    def increment(self, name: str, value: float = 1, tags: dict = None):
        logger.debug(f"[metric] increment {name} +{value} tags={tags}")

    def timing(self, name: str, ms: float, tags: dict = None):
        logger.debug(f"[metric] timing {name}: {ms}ms tags={tags}")

    def gauge(self, name: str, value: float, tags: dict = None):
        logger.debug(f"[metric] gauge {name}: {value} tags={tags}")


def report_exception(exc: Exception, context: dict = None):
    logger.exception(f"[error_hook] Exception={exc} context={context}")


# ---- LANCE DB HANDLER ----
class LanceDBServer:
    def __init__(self):
        self.db_uri = db_uri
        self.db_key = db_key
        self.region = "us-east-1"
        self.secure_kms = SecureKMSService()
        self.EMBEDDING_DIM = EMBEDDING_DIM
        self.db = None

        self.metrics = MetricsClient()
        self.error_hook = report_exception  # <-- FIXED (no parentheses)

    # -------------------------
    # Internal helpers
    # -------------------------
    def _connect_if_needed(self):
        # print("CONNECT_IF_NEEDED start")
        # print("self.db =", self.db)
        # print("URI:", self.db_uri)
        # print("KEY:", "SET" if self.db_key else "MISSING")
        # print("REGION:", self.region)

        if self.db is None:
            try:
                # print("going to create new instance of lance")
                self.db = lancedb.connect(
                    uri=self.db_uri, api_key=self.db_key, region=self.region
                )
            # print("CONNECTED:", self.db)
            except Exception as e:
                # print("LanceDB CONNECT ERROR:", e)
                return e
        return self.db

    async def _create_schema_dummy(self) -> pa.Schema:
        """Return the Arrow schema used for the table."""
        return pa.schema(
            [
                pa.field("id", pa.string()),
                pa.field("text", pa.string()),
                pa.field("embedding", pa.list_(pa.float32(), self.EMBEDDING_DIM)),
                pa.field("foldername", pa.string()),
            ]
        )

    def check_lance_db_Connection(self):
        self.db = self._connect_if_needed()
        if self.db:
            return True
        else:
            return False

    async def _open_or_create_table(self, user_id: str):
        """
        Open existing table or create it with an 'init' dummy row.
        Removes dummy row once and attempts to create an index (quietly).
        """
        table_name = f"index_{user_id}"
        self.db = self._connect_if_needed()

        def _open():
            return self.db.open_table(table_name)

        try:
            table = await asyncio.to_thread(_open)
            return table

        except Exception:
            # Table does not exist — create it
            schema = await self._create_schema_dummy()
            dummy = [
                {
                    "id": "init",
                    "text": "init row",
                    "embedding": np.zeros(self.EMBEDDING_DIM, dtype=np.float32),
                    "foldername": "init",
                }
            ]

            def _create():
                return self.db.create_table(
                    table_name, data=dummy, schema=schema, mode="create"
                )

            try:
                table = await asyncio.to_thread(_create)
            except Exception as e:
                logger.exception("Failed to create table %s: %s", table_name, e)
                if self.error_hook:
                    try:
                        self.error_hook(
                            e, {"action": "create_table", "table_name": table_name}
                        )
                    except Exception:
                        logger.debug("error_hook raised an exception")
                raise

            # Remove dummy row once (use double quotes)
            try:
                await asyncio.to_thread(lambda: table.delete('id == "init"'))
            except Exception as e:
                logger.warning("Failed to delete dummy row for %s: %s", table_name, e)

            # Try to create index quietly (may be no-op or fail)
            try:
                await asyncio.to_thread(lambda: table.create_index("embedding"))
            except Exception as e:
                logger.debug("Index creation warning for %s: %s", table_name, e)

            logger.info("Created table %s for user %s", table_name, user_id)
            return table

    async def delete_user_table(self, user_id: str):
        """
        Delete the vector table for a given user_id.
        Fails quietly if the table does not exist.
        """
        table_name = f"u_{user_id}"
        self.db = self._connect_if_needed()
        # print("deleting db tables started", table_name)

        def _drop():
            return self.db.drop_table(table_name)

        try:
            await asyncio.to_thread(_drop)
            logger.info("Deleted table %s for user %s", table_name, user_id)
            return True

        except Exception as e:
            # Most common case: table does not exist
            logger.warning("Failed to delete table %s: %s", table_name, e)

            if self.error_hook:
                try:
                    self.error_hook(
                        e, {"action": "delete_table", "table_name": table_name}
                    )
                except Exception:
                    logger.debug("error_hook raised an exception")

            return False

    # -------------------------
    # Public API
    # -------------------------
    # @retry_async("/delete_from_lance/<user_id>", methods=["GET"])
    # async def delete_from_lance(self, user_id):
    #     await self.delete_user_table(user_id)
    #     return {"status": "ok"}

    @retry_async(attempts=4, initial_delay=0.5, factor=2.0, max_delay=8.0, jitter=0.15)
    async def insert_vector(self, data: "VectorData"):
        """
        Insert a single vector record (async).
        Expects `VectorData`-like object/dict with fields: user_id, id, text, embedding, foldername.
        """
        start = time.time()
        try:
            if len(data.embedding) != self.EMBEDDING_DIM:
                raise ValueError(f"Embedding must be {self.EMBEDDING_DIM} floats long")

            table = await self._open_or_create_table(data.user_id)

            embedding = np.array(data.embedding, dtype=np.float32)

            # Delete existing record with same id+folder (double quotes)
            def _delete_existing():
                return table.delete(
                    f'id == "{data.id}" AND foldername == "{data.foldername}"'
                )

            await asyncio.to_thread(_delete_existing)

            payload = {
                "id": data.id,
                "text": data.text,
                "embedding": embedding,
                "foldername": data.foldername,
            }

            # add (append=True recommended)
            await asyncio.to_thread(table.add, [payload])

            latency = time.time() - start
            logger.debug(
                "Inserted vector id=%s user=%s latency=%.3fs",
                data.id,
                data.user_id,
                latency,
            )
            if self.metrics:
                try:
                    self.metrics.increment("lancedb.insert.success")
                    self.metrics.timing("lancedb.insert.latency", latency)
                except Exception:
                    logger.debug("metrics client call failed on insert_vector")
        except Exception as e:
            logger.exception(
                "insert_vector failed for user=%s id=%s: %s",
                getattr(data, "user_id", None),
                getattr(data, "id", None),
                e,
            )
            if self.metrics:
                try:
                    self.metrics.increment("lancedb.insert.failure")
                except Exception:
                    logger.debug("metrics client increment failed on insert failure")
            if self.error_hook:
                try:
                    self.error_hook(
                        e,
                        {
                            "action": "insert_vector",
                            "user_id": getattr(data, "user_id", None),
                            "id": getattr(data, "id", None),
                        },
                    )
                except Exception:
                    logger.debug("error_hook raised an exception")
            raise

    @retry_async(attempts=4, initial_delay=0.5, factor=2.0, max_delay=8.0, jitter=0.15)
    async def insert_batch(self, vectors: Sequence["VectorData"]):
        """
        Insert a batch of vectors. All entries MUST have the same user_id and foldername.
        """
        start = time.time()
        user_id = None
        foldername = None

        try:
            if not vectors:
                raise ValueError("Empty batch")

            # Validate vectors
            user_id = vectors[0].user_id
            foldername = vectors[0].foldername

            for v in vectors:
                if v.user_id != user_id or v.foldername != foldername:
                    raise ValueError(
                        "All vectors must have the same user_id and foldername"
                    )
                if len(v.embedding) != self.EMBEDDING_DIM:
                    raise ValueError(f"Invalid embedding length for id {v.id}")

            # Open/create table
            table = await self._open_or_create_table(user_id)

            # --- DELETE old rows (OK) ---
            await asyncio.to_thread(table.delete, f'foldername == "{foldername}"')

            # Build new records
            records = [
                {
                    "id": v.id,
                    "text": v.text,
                    "embedding": np.array(v.embedding, dtype=np.float32),
                    "foldername": v.foldername,
                }
                for v in vectors
            ]

            # --- ADD new rows (NO append=True) ---
            await asyncio.to_thread(table.add, records)

            latency = time.time() - start
            logger.debug(
                "Inserted batch user=%s folder=%s count=%d latency=%.3fs",
                user_id,
                foldername,
                len(records),
                latency,
            )

            if self.metrics:
                try:
                    self.metrics.increment("lancedb.insert_batch.success")
                    self.metrics.timing("lancedb.insert_batch.latency", latency)
                except Exception:
                    logger.debug("metrics client call failed on insert_batch")

        except Exception as e:
            logger.exception(
                "insert_batch failed for user=%s folder=%s: %s",
                user_id,
                foldername,
                e,
            )

            if self.metrics:
                try:
                    self.metrics.increment("lancedb.insert_batch.failure")
                except Exception:
                    logger.debug(
                        "metrics client increment failed on insert_batch failure"
                    )

            if self.error_hook:
                try:
                    self.error_hook(
                        e,
                        {
                            "action": "insert_batch",
                            "user_id": user_id,
                            "foldername": foldername,
                        },
                    )
                except Exception:
                    logger.debug("error_hook raised an exception")

            raise

    @retry_async(attempts=3, initial_delay=0.2, factor=2.0, max_delay=4.0, jitter=0.1)
    async def query_vector(self, query: "QueryData") -> List[Dict[str, Any]]:
        """
        Single-vector query. Returns list of result dicts as produced by LanceDB `.to_list()`.
        """
        if isinstance(query, dict):
            query = QueryData(**query)
        start = time.time()
        # print("len values", len(query.embedding))
        try:
            if len(query.embedding) > self.EMBEDDING_DIM:
                raise ValueError(
                    f"Query embedding must be {self.EMBEDDING_DIM} floats long"
                )
            table = await self._open_or_create_table(query.user_id)
            query_embedding = np.array(query.embedding, dtype=np.float32)

            def _search():
                return (
                    table.search(query_embedding, vector_column_name="embedding")
                    .limit(query.top_k)
                    .to_list()
                )

            results = await asyncio.to_thread(_search)
            latency = time.time() - start
            logger.debug(
                "query_vector user=%s top_k=%d results=%d latency=%.3fs",
                query.user_id,
                query.top_k,
                len(results),
                latency,
            )
            if self.metrics:
                try:
                    self.metrics.increment("lancedb.query.success")
                    self.metrics.timing("lancedb.query.latency", latency)
                except Exception:
                    logger.debug("metrics client call failed on query_vector")
            return results
        except Exception as e:
            logger.exception(
                "query_vector failed for user=%s: %s",
                getattr(query, "user_id", None),
                e,
            )
            if self.metrics:
                try:
                    self.metrics.increment("lancedb.query.failure")
                except Exception:
                    logger.debug("metrics client increment failed on query failure")
            if self.error_hook:
                try:
                    self.error_hook(
                        e,
                        {
                            "action": "query_vector",
                            "user_id": getattr(query, "user_id", None),
                        },
                    )
                except Exception:
                    logger.debug("error_hook raised an exception")
            raise

    @retry_async(attempts=3, initial_delay=0.2, factor=2.0, max_delay=4.0, jitter=0.1)
    async def query_vector_filename(
        self, query: "QueryData", filename: str
    ) -> List[Dict[str, Any]]:

        if isinstance(query, dict):
            query = QueryData(**query)

        start = time.time()

        try:
            if len(query.embedding) > self.EMBEDDING_DIM:
                raise ValueError(
                    f"Query embedding must be {self.EMBEDDING_DIM} floats long"
                )

            table = await self._open_or_create_table(query.user_id)
            query_embedding = np.array(query.embedding, dtype=np.float32)

            def _search():
                return (
                    table.search(query_embedding, vector_column_name="embedding")
                    .where(f'foldername == "{filename}"')  # 🔥 filter by file
                    .limit(query.top_k)
                    .to_list()
                )

            results = await asyncio.to_thread(_search)

            latency = time.time() - start
            logger.debug(
                "query_vector_filename user=%s filename=%s top_k=%d results=%d latency=%.3fs",
                query.user_id,
                filename,
                query.top_k,
                len(results),
                latency,
            )

            if self.metrics:
                try:
                    self.metrics.increment("lancedb.query.success")
                    self.metrics.timing("lancedb.query.latency", latency)
                except Exception:
                    logger.debug("metrics client call failed on query_vector")

            return results

        except Exception as e:
            logger.exception(
                "query_vector_filename failed user=%s filename=%s: %s",
                getattr(query, "user_id", None),
                filename,
                e,
            )

            if self.metrics:
                try:
                    self.metrics.increment("lancedb.query.failure")
                except Exception:
                    logger.debug("metrics client increment failed on query failure")

            if self.error_hook:
                try:
                    self.error_hook(
                        e,
                        {
                            "action": "query_vector_filename",
                            "user_id": getattr(query, "user_id", None),
                            "filename": filename,
                        },
                    )
                except Exception:
                    logger.debug("error_hook raised an exception")

            raise

    @retry_async(attempts=3, initial_delay=0.2, factor=2.0, max_delay=4.0, jitter=0.1)
    async def fetch_by_filename(
        self, user_id: str, filename: str
    ) -> List[Dict[str, Any]]:

        start = time.time()

        try:
            table = await self._open_or_create_table(user_id)

            def _fetch():
                return (
                    table.search()
                    .where(f'foldername == "{filename}"')  # 🔥 filter by file
                    .to_list()
                )

            results = await asyncio.to_thread(_fetch)

            latency = time.time() - start
            logger.debug(
                "fetch_by_filename user=%s filename=%s results=%d latency=%.3fs",
                user_id,
                filename,
                len(results),
                latency,
            )

            if self.metrics:
                try:
                    self.metrics.increment("lancedb.fetch.success")
                    self.metrics.timing("lancedb.fetch.latency", latency)
                except Exception:
                    logger.debug("metrics client call failed on fetch_by_filename")

            return results

        except Exception as e:
            logger.exception(
                "fetch_by_filename failed user=%s filename=%s: %s",
                user_id,
                filename,
                e,
            )

            if self.metrics:
                try:
                    self.metrics.increment("lancedb.fetch.failure")
                except Exception:
                    logger.debug("metrics client increment failed on fetch failure")

            if self.error_hook:
                try:
                    self.error_hook(
                        e,
                        {
                            "action": "fetch_by_filename",
                            "user_id": user_id,
                            "filename": filename,
                        },
                    )
                except Exception:
                    logger.debug("error_hook raised an exception")

            raise

    async def query_vector_batch(
        self, query: "BatchQueryData"
    ) -> List[List[Dict[str, Any]]]:
        """
        Batch query: serverless LanceDB does not support multi-vector in a single call reliably,
        so we execute one search per embedding in parallel and return a list-of-lists (per query embedding).
        """
        start = time.time()
        try:
            table = await self._open_or_create_table(query.user_id)

            query_embeddings = np.array(query.embeddings, dtype=np.float32)
            if (
                query_embeddings.ndim != 2
                or query_embeddings.shape[1] != self.EMBEDDING_DIM
            ):
                raise ValueError(
                    f"Each embedding must be of length {self.EMBEDDING_DIM}"
                )

            folder_filter = None
            if getattr(query, "filenames", None):
                folder_filter = " OR ".join(
                    [f'foldername == "{fn}"' for fn in query.filenames]
                )

            async def _single_search(vec):
                def _inner():
                    search_obj = table.search(vec, vector_column_name="embedding")
                    if folder_filter:
                        search_obj = search_obj.where(folder_filter)
                    return search_obj.limit(query.top_k).to_list()

                return await asyncio.to_thread(_inner)

            tasks = [_single_search(vec) for vec in query_embeddings]
            results = await asyncio.gather(*tasks)

            latency = time.time() - start
            logger.debug(
                "query_vector_batch user=%s queries=%d total_results=%d latency=%.3fs",
                query.user_id,
                len(query_embeddings),
                sum(len(r) for r in results),
                latency,
            )
            if self.metrics:
                try:
                    self.metrics.increment("lancedb.batch_query.success")
                    self.metrics.timing("lancedb.batch_query.latency", latency)
                except Exception:
                    logger.debug("metrics client call failed on query_vector_batch")
            return results
        except Exception as e:
            logger.exception(
                "query_vector_batch failed for user=%s: %s",
                getattr(query, "user_id", None),
                e,
            )
            if self.metrics:
                try:
                    self.metrics.increment("lancedb.batch_query.failure")
                except Exception:
                    logger.debug(
                        "metrics client increment failed on batch query failure"
                    )
            if self.error_hook:
                try:
                    self.error_hook(
                        e,
                        {
                            "action": "query_vector_batch",
                            "user_id": getattr(query, "user_id", None),
                        },
                    )
                except Exception:
                    logger.debug("error_hook raised an exception")
            raise

    @retry_async(attempts=3, initial_delay=0.2, factor=2.0, max_delay=4.0, jitter=0.1)
    async def delete_vector(self, data: "DeleteData"):
        """
        Delete a single vector by id for a user.
        """
        try:
            table = await self._open_or_create_table(data.user_id)
            await asyncio.to_thread(lambda: table.delete(f'id == "{data.id}"'))
            logger.debug("Deleted vector id=%s user=%s", data.id, data.user_id)
            if self.metrics:
                try:
                    self.metrics.increment("lancedb.delete.success")
                except Exception:
                    logger.debug("metrics client call failed on delete_vector")
        except Exception as e:
            logger.exception(
                "delete_vector failed for user=%s id=%s: %s",
                getattr(data, "user_id", None),
                getattr(data, "id", None),
                e,
            )
            if self.metrics:
                try:
                    self.metrics.increment("lancedb.delete.failure")
                except Exception:
                    logger.debug("metrics client increment failed on delete failure")
            if self.error_hook:
                try:
                    self.error_hook(
                        e,
                        {
                            "action": "delete_vector",
                            "user_id": getattr(data, "user_id", None),
                            "id": getattr(data, "id", None),
                        },
                    )
                except Exception:
                    logger.debug("error_hook raised an exception")
            raise

    @retry_async(attempts=3, initial_delay=0.2, factor=2.0, max_delay=4.0, jitter=0.1)
    async def delete_batch(self, user_id: str, ids: Sequence[str]):
        """
        Delete multiple IDs in one call using IN (...) -- minimizes network round trips.
        """
        try:
            table = await self._open_or_create_table(user_id)
            if not ids:
                return
            ids_expr = ",".join([f'"{i}"' for i in ids])
            await asyncio.to_thread(lambda: table.delete(f"id IN ({ids_expr})"))
            logger.debug("Deleted %d vectors for user=%s", len(ids), user_id)
            if self.metrics:
                try:
                    self.metrics.increment("lancedb.delete_batch.success")
                except Exception:
                    logger.debug("metrics client call failed on delete_batch")
        except Exception as e:
            logger.exception(
                "delete_batch failed for user=%s ids_count=%d: %s",
                user_id,
                len(ids) if ids else 0,
                e,
            )
            if self.metrics:
                try:
                    self.metrics.increment("lancedb.delete_batch.failure")
                except Exception:
                    logger.debug(
                        "metrics client increment failed on delete_batch failure"
                    )
            if self.error_hook:
                try:
                    self.error_hook(
                        e,
                        {
                            "action": "delete_batch",
                            "user_id": user_id,
                            "ids_count": len(ids) if ids else 0,
                        },
                    )
                except Exception:
                    logger.debug("error_hook raised an exception")
            raise

    @retry_async(attempts=3, initial_delay=0.5, factor=2.0, max_delay=5.0, jitter=0.1)
    async def delete_folder_async(self, user_id: str, foldername: str) -> int:
        """
        Async delete all vectors for a folder for a user.
        Returns number of rows deleted.
        """
        try:
            table = await self._open_or_create_table(user_id)
            logger.info(f"Deleting folder '{foldername}' for user '{user_id}'")

            result = table.delete(f"foldername == '{foldername}'")
            deleted_count = result

            logger.info(f"Deleted {deleted_count} rows from folder '{foldername}'")
            return deleted_count

        except Exception as e:
            logger.exception(
                f"Failed to delete folder '{foldername}' for user '{user_id}': {e}"
            )
            if self.error_hook:
                try:
                    self.error_hook(
                        e,
                        {
                            "action": "delete_folder",
                            "user_id": user_id,
                            "foldername": foldername,
                        },
                    )
                except Exception:
                    logger.debug("error_hook raised an exception")
            raise

    async def list_indexes(self) -> List[str]:
        """Return all table names (indexes) in the connected LanceDB instance."""
        try:

            def _names():
                self.db = self._connect_if_needed()
                return self.db.table_names()

            names = await asyncio.to_thread(_names)
            logger.debug("list_indexes returned %d tables", len(names))
            return names
        except Exception as e:
            logger.exception("list_indexes failed: %s", e)
            if self.metrics:
                try:
                    self.metrics.increment("lancedb.list_indexes.failure")
                except Exception:
                    logger.debug(
                        "metrics client increment failed on list_indexes failure"
                    )
            if self.error_hook:
                try:
                    self.error_hook(e, {"action": "list_indexes"})
                except Exception:
                    logger.debug("error_hook raised an exception")
            raise

    def _get_umail_table(self, user_id, folder_name=None):
        table_name = f"u_{user_id}"
        # print("in get umail table")

        try:
            self.db = self._connect_if_needed()
            # print("self.db", self.db)
            return self.db.open_table(table_name)
        except Exception:
            # print(f"[DEBUG] Creating new table {table_name}")

            schema = pa.schema(
                [
                    pa.field("user_id", pa.string()),
                    pa.field("id", pa.string()),
                    pa.field("text", pa.string()),
                    pa.field("embedding", pa.list_(pa.float32(), EMBEDDING_DIM)),
                    pa.field("folder_name", pa.string()),
                    pa.field("timestamp", pa.string()),
                    pa.field(
                        "plain_text_embedding", pa.list_(pa.float32(), EMBEDDING_DIM)
                    ),
                ]
            )

            dummy = [
                {
                    "user_id": user_id,
                    "id": "init",
                    "text": "init row",
                    "embedding": np.zeros(EMBEDDING_DIM, dtype=np.float32),
                    "folder_name": "init_row",
                    "timestamp": "init",
                    "plain_text_embedding": np.zeros(EMBEDDING_DIM, dtype=np.float32),
                }
            ]

            return self.db.create_table(
                table_name, data=dummy, schema=schema, mode="create"
            )

    def filter_umail_table(self, user_id, folder_name):
        table = self._get_umail_table(user_id)

        filters = []
        if user_id:
            filters.append(f"user_id == '{user_id}'")
        if folder_name:
            filters.append(f"folder_name == '{folder_name}'")

        try:
            if filters:
                filter_str = " AND ".join(filters)
                rows = table.search().where(filter_str).to_list()
            else:
                rows = table.to_list()
        except Exception:
            rows = table.to_pandas().to_dict("records")

        return [{"id": r.get("id"), "text": r.get("text")} for r in rows]

    def insert_umail_vectors(self, vectors):
        # print("in the insert umail")
        if not vectors:
            return {"inserted_count": 0}

        user_id = vectors[0].user_id
        # print(f"********user id inside lance: {user_id}")
        # folder_name = vectors[0].folder_name
        id = vectors[0].id
        # print(f"********id inside lance: {id}")

        table = self._get_umail_table(user_id)
        # print("table", table)

        # === DELETE once per folder (FAST, not per ID) ===
        # table.delete(f"folder_name == '{folder_name}'")

        rows = (
            table.search()  # start query
            .where(f"id == '{id}'")  # filter
            .limit(10)  # safety
            .to_list()  # EXECUTE
        )
        # if rows:
        #     for row in rows:
        #        #print(f"row already inside:")
        #         # print(row["text"])
        # else:
        #    #print(f"no rows already")

        table.delete(f"id == '{id}'")

        # Build all rows
        records = [
            {
                "id": v.id,
                "text": v.text,
                "user_id": v.user_id,
                "embedding": np.array(v.embedding, dtype=np.float32),
                "folder_name": v.folder_name,
                "timestamp": v.timestamp,
                "plain_text_embedding": v.plain_text_embedding,
            }
            for v in vectors
        ]
        for v in vectors:
            text = v.text
            # print(f"text inserted:")
            # print(f"{text}")

        # Clean dummy only once
        table.delete("id == 'init'")

        # Add records in one shot
        table.add(records)

        return {"user_id": user_id, "inserted_count": len(records)}

    def insert_umail_vectors_for_reply(self, vectors):
        if not vectors:
            return {"inserted_count": 0}

        if isinstance(vectors[0], dict):
            vectors = [UmailData(**v) for v in vectors]

        user_id = vectors[0].user_id
        table = self._get_umail_table(user_id)

        records = []

        for v in vectors:
            table.delete(f"id == '{v.id}'")

            if len(v.embedding) != EMBEDDING_DIM:
                raise ValueError(f"Invalid embedding length for id {v.id}")

            records.append(
                {
                    "id": v.id,
                    "text": v.text,
                    "user_id": v.user_id,
                    "embedding": np.array(v.embedding, dtype=np.float32),
                    "folder_name": v.folder_name,
                    "timestamp": v.timestamp,
                }
            )

        table.add(records)
        # table.optimize()

        return {"user_id": user_id, "inserted_count": len(records)}

    def serverless_get_umail_page(self, user_id: str, next_cursor=None, page_size=1000):
        table = self._get_umail_table(user_id)
        # print(f"[SERVERLESS] user_id:{user_id} next_cursor:{next_cursor}")

        # print("----------------------------")

        # Normalize timestamp cursor
        current_dt = (
            parse_ts(next_cursor) if next_cursor else datetime.now(timezone.utc)
        )
        if current_dt is None:
            current_dt = datetime.now(timezone.utc)

        collected = []
        checked_timestamps = False
        all_timestamps = []

        # --------------------------------------------
        #  MAIN PAGINATION LOOP (same as API version)
        # --------------------------------------------
        while len(collected) < page_size:
            day_start = current_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start.replace(
                hour=23, minute=59, second=59, microsecond=999999
            )

            # Query exactly like API
            day_messages = (
                table.search()
                .where(
                    f"timestamp >= '{day_start.isoformat()}' and timestamp <= '{day_end.isoformat()}'"
                )
                .to_list()
            )

            day_messages = sorted(
                day_messages, key=lambda x: parse_ts(x["timestamp"]), reverse=True
            )

            if day_messages:
                collected.extend(day_messages)

            else:
                if not checked_timestamps:
                    # Gather all timestamps
                    raw = table.search().select(["timestamp"]).to_list()
                    all_timestamps = sorted(
                        [
                            parse_ts(r["timestamp"])
                            for r in raw
                            if parse_ts(r["timestamp"])
                        ],
                        reverse=True,
                    )
                    checked_timestamps = True

                if all_timestamps:
                    # Jump to nearest available timestamp <= current_dt
                    recent = next(
                        (ts for ts in all_timestamps if ts <= current_dt), None
                    )
                    if recent:
                        current_dt = recent
                        continue
                    else:
                        break
                else:
                    break

            if len(collected) >= page_size:
                break

            current_dt = day_start - timedelta(seconds=1)
            if current_dt.year < 2020:
                break

        collected = collected[:page_size]

        # -----------------------------------------------------
        #        PICK **LATEST MESSAGE PER FOLDER**
        # -----------------------------------------------------
        latest_per_folder = {}
        # print(f"collected: {collected}")
        for msg in collected:
            folder = msg.get("folder_name") or "_no_folder_"
            ts = parse_ts(msg["timestamp"])
            if not ts:
                continue

            if folder not in latest_per_folder or ts > parse_ts(
                latest_per_folder[folder]["timestamp"]
            ):
                latest_per_folder[folder] = msg

        records = sorted(
            latest_per_folder.values(),
            key=lambda x: parse_ts(x["timestamp"]),
            reverse=True,
        )

        next_cursor = (
            parse_ts(records[-1]["timestamp"]).isoformat() if records else None
        )
        return records, next_cursor

    def serverless_get_umail_page_test(
        self, user_id: str, next_cursor=None, page_size=1000
    ):

        connection = connect_to_rds()
        table = self._get_umail_table(user_id)

        # Cursor logic
        if next_cursor:
            end_date = parse_ts(next_cursor)
        else:
            end_date = datetime.now(timezone.utc)

        query = """
            SELECT m.conversation_id_fk, m.sender_id, m.created_at
            FROM messages m
            JOIN (
                SELECT m2.sender_id,
                    MAX(m2.created_at) AS latest_created_at
                FROM messages m2
                JOIN communication c
                ON m2.sender_id = c.users_clients_id_fk
                WHERE m2.created_at < %s
                AND c.user_id_fk = %s
                GROUP BY m2.sender_id
            ) latest
            ON m.sender_id = latest.sender_id
            AND m.created_at = latest.latest_created_at
            ORDER BY m.created_at DESC
            LIMIT %s;
        """

        params = (
            end_date,  # cursor upper bound
            user_id,
            page_size,
        )

        with connection.cursor() as cursor:
            cursor.execute(query, params)
            rows = cursor.fetchall()

        connection.close()

        collected = []

        for conversation_id, client_id, timestamp in rows:
            # print("Searching Lance for:", conversation_id, client_id)

            text_rows = (
                table.search()
                .where(f"id == '{conversation_id}' and folder_name == '{client_id}'")
                .to_list()
            )

            if text_rows:
                text_rows.sort(key=lambda x: x["timestamp"], reverse=True)
                collected.append(text_rows[0])

        # Set next cursor = oldest item in this page
        next_cursor = None
        if collected:
            oldest_item = min(collected, key=lambda x: x["timestamp"])
            next_cursor = oldest_item.get("timestamp")

        return collected, next_cursor

    def serverless_get_umail_page_test_test(
        self, user_id: str, next_cursor=None, page_size=1000
    ):

        connection = connect_to_rds()
        table = self._get_umail_table(user_id)

        if next_cursor:
            end_date = parse_ts(next_cursor)
        else:
            end_date = datetime.now(timezone.utc)

        start_date = end_date - timedelta(days=5)

        query = """
        SELECT m.conversation_id_fk, m.sender_id, m.created_at
        FROM messages m
        JOIN (
            SELECT m2.sender_id,
                MAX(m2.created_at) AS latest_created_at
            FROM messages m2
            JOIN communication c
            ON m2.sender_id = c.users_clients_id_fk
            WHERE m2.created_at >= %s
            AND m2.created_at < %s            
            AND c.user_id_fk = %s
            GROUP BY m2.sender_id
        ) latest
        ON m.sender_id = latest.sender_id
        AND m.created_at = latest.latest_created_at
        JOIN communication c2
        ON m.sender_id = c2.users_clients_id_fk
        WHERE m.created_at < %s
        AND c2.user_id_fk = %s
        ORDER BY m.created_at DESC
        LIMIT %s;
        """

        params = (
            start_date,  # window start
            end_date,  # window end
            user_id,
            end_date,  # ✅ cursor
            user_id,
            page_size,
        )

        with connection.cursor() as cursor:
            cursor.execute(query, params)
            rows = cursor.fetchall()

        connection.close()

        result = []
        # print(f"rows:")
        # print({rows})
        # print(f"lenght : {len(rows)}")

        collected = []
        for row in rows:
            conversation_id, client_id, timestamp = row
            # text_row = table.search().where(f"id == '{conversation_id}' and folder_name == '{client_id}'").select(["text"]).to_list()
            text_rows = (
                table.search()
                .where(f"id == '{conversation_id}' and folder_name == '{client_id}'")
                .to_list()
            )

            if text_rows:
                # Sort by timestamp in Python
                text_rows.sort(key=lambda x: x["timestamp"], reverse=True)
                collected.append(text_rows[0])
            # print(f"****** {text_rows[0]['folder_name']}  | {text_rows[0]['id']}")

            # latest_text = text_rows[0]["text"]
            # latest_timestamp = text_rows[0]["timestamp"]
            # else:
            #    #print(f"not found:")
            #    #print(f"client_id : {client_id} | conversation_id : {conversation_id}")

        next_cursor = None

        for item in reversed(collected):
            ts = item.get("timestamp")
            if ts:
                next_cursor = ts
                break
        # next_cursor = collected[-1]["timestamp"]
        # result.append({
        #     "conversation_id": conversation_id,
        #     "client_id": client_id,
        #     "timestamp": latest_timestamp,
        #     "text": latest_text
        # })
        # print(f"lenght of collected: {len(collected)}")
        # print(f"next_cursor: {next_cursor}")
        return collected, next_cursor

    # -------------------SCRAPE FUNCTIONALITY-------------------------------#

    def _get_scrape_table(self, user_id: str):
        table_name = f"scrape_{user_id}"
        self.db = self._connect_if_needed()

        try:
            # 🔥 Always try to open first
            return self.db.open_table(table_name)

        except Exception as e:
            # print(
            #     f"[DEBUG] Scrape table not found for user {user_id}, creating new one"
            # )

            schema = pa.schema(
                [
                    pa.field("user_id", pa.string()),
                    pa.field("url", pa.string()),
                    pa.field("title", pa.string()),
                    pa.field("content", pa.string()),
                    pa.field("contacts", pa.string()),
                    pa.field("timestamp", pa.string()),
                    pa.field("metadata", pa.string()),
                    pa.field("embedding", pa.list_(pa.float32(), EMBEDDING_DIM)),
                    pa.field("pages_by_level", pa.string()),
                ]
            )

            dummy = [
                {
                    "user_id": user_id,
                    "url": "init",
                    "title": "init",
                    "content": "init",
                    "contacts": "{}",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "metadata": "{}",
                    "embedding": np.zeros(EMBEDDING_DIM, dtype=np.float32),
                    "pages_by_level": "{}",
                }
            ]

            return self.db.create_table(
                table_name,
                data=dummy,
                schema=schema,
                mode="create",  # 🔥 DO NOT overwrite
            )

    def _update_summary_scrape(self, user_id, url, content):
        table = self._get_scrape_table(user_id=user_id)
        # Find row first
        matches = table.search().where(f"url == '{url}'").limit(1).to_list()
        if not matches:
            return False

        # Correct update call (NO keyword args!)
        # table.update({"contacts": contacts_value}, f"url == {url}")
        table.update(
            values={"content": content},
            where=f"url == '{url}'",
        )

        return True

    def _update_status_scrape(self, user_id, url, status):
        table = self._get_scrape_table(user_id=user_id)

        # ✅ NO search(), ONLY where()
        rows = table.search().where(f"url == '{url}'").limit(1).to_list()
        if not rows:
            return False

        row = rows[0]
        metadata = row.get("metadata") or {}

        # ✅ Handle string metadata
        if isinstance(metadata, str):
            metadata = json.loads(metadata)

        metadata["status"] = status

        table.update(
            values={"metadata": json.dumps(metadata)},  # keep schema consistent
            where=f"url == '{url}'",
        )

        return True

    def _update_innerscrape_scrape(self, user_id, url, innerurl, content):
        table = self._get_scrape_table(user_id=user_id)

        # Fetch parent row
        matches = (
            table.search("pages_by_level").where(f"url == '{url}'").limit(1).to_list()
        )

        if not matches:
            return False

        row = matches[0]
        pages_by_level = row.get("pages_by_level", {})

        updated = False

        for level, pages in pages_by_level.items():
            if not isinstance(pages, list):
                continue

            for page in pages:
                if page.get("url") == innerurl:
                    page["content"] = content
                    updated = True
                    break

            if updated:
                break

        if not updated:
            return False  # inner URL not found

        # Persist update
        table.update(
            values={"pages_by_level": pages_by_level},
            where=f"url == '{url}'",
        )

        return True

    def _delete_innerscrape_scrape(self, user_id, url, innerurl):
        table = self._get_scrape_table(user_id=user_id)

        matches = (
            table.search("pages_by_level").where(f"url == '{url}'").limit(1).to_list()
        )

        if not matches:
            return False

        row = matches[0]
        pages_by_level = row.get("pages_by_level", {})
        deleted = False

        for level in list(pages_by_level.keys()):
            pages = pages_by_level.get(level, [])
            if not isinstance(pages, list):
                continue

            original_len = len(pages)
            pages_by_level[level] = [p for p in pages if p.get("url") != innerurl]

            if len(pages_by_level[level]) != original_len:
                deleted = True

            if not pages_by_level[level]:
                del pages_by_level[level]

        if not deleted:
            return False

        table.update(
            values={"pages_by_level": pages_by_level},
            where=f"url == '{url}'",
        )

        return True

    def insert_scraped_data(self, data: ScrapedData):
        """Insert a single scraped data entry"""
        try:
            if len(data.embedding) != EMBEDDING_DIM:
                raise ValueError(f"Embedding must be {EMBEDDING_DIM} dimensions")

            table = self._get_scrape_table(data.user_id)

            # Remove dummy and existing URL data
            table.delete("url == 'init'")
            table.delete(f"url == '{data.url}'")

            record = [
                {
                    "user_id": data.user_id,
                    "url": data.url,
                    "title": data.title,
                    "content": data.content,
                    "contacts": data.contacts,
                    "timestamp": data.timestamp,
                    "metadata": json.dumps(data.metadata),
                    "embedding": np.array(data.embedding, dtype=np.float32),
                    "pages_by_level": json.dumps(data.pages_by_level),
                }
            ]

            table.add(record)
            # table.optimize()

            return {"status": "success", "url": data.url}

        except Exception as e:
            # print(f"Error inserting scraped data: {e}")
            raise e

    def delete_scraped_data(self, user_id, url):
        """Insert a single scraped data entry"""
        try:
            table = self._get_scrape_table(user_id)

            # Remove dummy and existing URL data
            table.delete("url == 'init'")
            table.delete(f"url == '{url}'")

            return {"status": "success", "url": url}

        except Exception as e:
            # print(f"Error inserting scraped data: {e}")
            raise e

    def update_scraped_contacts(self, user_id, url, contacts):
        table = self._get_scrape_table(user_id)

        # Find row first
        matches = table.search().where(f"url == '{url}'").limit(1).to_list()
        if not matches:
            return False

        # print("matches found scrape", matches)
        # Normalize contacts
        if isinstance(contacts, list):
            contacts_value = json.dumps(contacts)
        else:
            contacts_value = str(contacts)

        # Correct update call (NO keyword args!)
        # table.update({"contacts": contacts_value}, f"url == {url}")
        table.update(
            values={"contacts": contacts_value},
            where=f"url == '{url}'",
        )

        return True

    def search_scraped_data(self, query: "QueryData", sender_email="All"):
        from training.scrape.helper import generate_level_context

        if isinstance(query, dict):
            query = QueryData(**query)

        try:
            if len(query.embedding) > EMBEDDING_DIM:
                raise ValueError(f"Embedding must be {EMBEDDING_DIM} dimensions")

            table = self._get_scrape_table(query.user_id)
            query_vector = np.array(query.embedding, dtype=np.float32)

            # Perform vector search
            base_results = (
                table.search(query_vector)
                .metric("cosine")
                .limit(query.top_k)  # fetch few candidates first
                .to_list()
            )
            results = []
            for result in base_results:
                contacts = result.get("contacts", [])

                metadata = result.get("metadata") or {}

                # ✅ Normalize metadata
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except json.JSONDecodeError:
                        continue  # bad metadata → skip safely

                metadata_status = metadata.get("status")

                # ✅ Only active records
                if metadata_status != "active":
                    continue

                # ✅ Contact filtering
                if "All" in contacts:
                    results.append(result)
                elif isinstance(sender_email, list):
                    if any(email in contacts for email in sender_email):
                        results.append(result)
                elif sender_email and sender_email in contacts:
                    results.append(result)
            if not results:
                # print("no results found in base search")
                return None
            # for i in results:
            #    #print("title", i["title"])
            #    #print("distance", i["_distance"])
            # Pick the BEST (lowest cosine distance)
            best = min(results, key=lambda x: x.get("_distance", 999999))
            best_distance = best.get("_distance", 1)
            if len(results) >= 1 and best_distance > 0.8:
                # print("result found and score > 0.8 → rejecting")
                return None
            pages_by_level = best.get("pages_by_level")
            main_content = best.get("content", "")
            # print("len of main content", len(main_content))

            # Try to generate from pages_by_level
            full_context = None
            if pages_by_level and isinstance(pages_by_level, (list, dict)):
                # print("checking the internal pages of the site")
                full_context = generate_level_context(pages_by_level)

            # If invalid or empty, fallback to 'content'
            if not full_context:
                logger.warning(
                    "[FAST] pages_by_level invalid or empty; falling back to best.content"
                )
                # print("--", type(main_content))
                if isinstance(main_content, str):
                    # print("1111")
                    full_context = main_content.strip()
                else:
                    # print("2222")
                    full_context = str(main_content)
            # print("len of retun text length", len(full_context))
            # Format output
            result = {
                "url": best.get("url"),
                "title": best.get("title"),
                "text": full_context,
                "contacts": best.get("contacts"),
                "score": best.get("_distance"),
            }

            return result

        except Exception as e:
            # print(f"Error searching scraped data: {e}")
            raise e

    def debug_list_scrape_urls(self, user_id: str, limit=50):
        try:
            table = self._get_scrape_table(user_id)

            # Use a zero vector to fetch arbitrary rows
            dummy_vector = np.zeros(EMBEDDING_DIM, dtype=np.float32)

            rows = (
                table.search(dummy_vector, vector_column_name="embedding")
                .limit(limit)
                .to_list()
            )

        # print(f"\n[SCRAPE TABLE] Showing up to {limit} rows\n")

        # for i, row in enumerate(rows):
        #    #print(
        #         f"{i+1}. url={row.get('url')} | title={row.get('title')} | metadata={row.get('metadata')}"
        #     )

        # if not rows:
        #    #print("Table is empty")

        except Exception as e:
            # print("Failed to read scrape table:", e)
            return e

    def search_scraped_data_by_url(
        self, query: "QueryData", url: str, sender_email="All"
    ):
        from training.scrape.helper import generate_level_context
        import numpy as np
        import json

        if isinstance(query, dict):
            query = QueryData(**query)

        try:
            if len(query.embedding) != EMBEDDING_DIM:
                raise ValueError(f"Embedding must be {EMBEDDING_DIM} dimensions")

            table = self._get_scrape_table(query.user_id)

            # Stage 1 — ANN search (page-level embedding)
            candidates = (
                table.search(query.embedding, vector_column_name="embedding")
                .where(f'url == "{url}"')
                .limit(query.top_k)
                .to_arrow()
                .to_pylist()
            )

            if not candidates:
                # print(f"No scraped data found for URL: {url}")
                return None

            # Cosine distance
            def cosine_distance(a, b):
                return 1 - (np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))

            reranked = []

            for row in candidates:
                contacts = row.get("contacts", [])

                metadata = row.get("metadata") or {}
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except Exception:
                        continue

                # Only active pages
                if metadata.get("status") != "active":
                    continue

                # Contact filtering
                if sender_email != "All":
                    if isinstance(sender_email, list):
                        if not any(email in contacts for email in sender_email):
                            continue
                    elif sender_email not in contacts:
                        continue

                # Stage 2 — Chunk-level reranking
                chunk_embeddings = metadata.get("chunk_embeddings")

                if chunk_embeddings:
                    try:
                        best_chunk_distance = min(
                            cosine_distance(
                                query.embedding, np.array(chunk, dtype=np.float32)
                            )
                            for chunk in chunk_embeddings
                            if len(chunk) == EMBEDDING_DIM
                        )
                    except Exception:
                        best_chunk_distance = row.get("_distance", 1)
                else:
                    # fallback to page embedding distance
                    best_chunk_distance = row.get("_distance", 1)

                row["_effective_distance"] = best_chunk_distance
                reranked.append(row)

            if not reranked:
                # print("No valid results after filtering")
                return None

            # Pick best match using chunk-aware score
            best = min(reranked, key=lambda x: x["_effective_distance"])
            best_distance = best["_effective_distance"]

            # print("Best semantic distance:", best_distance)

            if best_distance > 0.8:
                # print("Weak match – rejecting")
                return None

            pages_by_level = best.get("pages_by_level")
            main_content = best.get("content", "")

            full_context = None
            if pages_by_level and isinstance(pages_by_level, (list, dict)):
                full_context = generate_level_context(pages_by_level)

            if not full_context:
                full_context = (
                    main_content if isinstance(main_content, str) else str(main_content)
                )

            return {
                "url": best.get("url"),
                "title": best.get("title"),
                "text": full_context,
                "contacts": best.get("contacts"),
                "score": best_distance,
            }

        except Exception as e:
            # print(f"Error searching scraped data by URL: {e}")
            raise

    # -----------------SEARCH EMAIL-------------------------#
    def search_email(self, data: SearchEmailQueryData):

        # print("inside search_emails")

        table = self._get_umail_table(data.user_id)
        # print(f"schema : {table.schema}")
        # print(f"indices : {table.list_indices()}")
        # for idx in table.list_indices():
        #     print(idx)

        query_embeddings = np.array(data.embeddings, dtype=np.float32)
        # if query_embeddings.ndim != 2 or query_embeddings.shape[1] != EMBEDDING_DIM:
        #     raise ValueError(f"Each embedding must be of length {EMBEDDING_DIM}")

        # Optional filter
        filter_condition = None

        if data.folder_names:
            folder_list_str = (
                "(" + ",".join([f"'{f}'" for f in data.folder_names]) + ")"
            )
            filter_condition = f"folder_name IN {folder_list_str}"

        if data.semantic_condition:
            if filter_condition:
                filter_condition += f" AND {data.semantic_condition}"
            else:
                filter_condition = data.semantic_condition

        # Run ANN search
        # search_obj = table.search(query_embeddings, vector_column_name="embedding")
        search_obj = table.search(
            query_embeddings, vector_column_name="plain_text_embedding"
        ).metric("cosine")
        if filter_condition:
            # print(f"filter_condition: {filter_condition}")
            search_obj = search_obj.where(filter_condition)

        results = search_obj.to_pandas()
        # print(f"results: {results}")
        results = results[results["_distance"] < 1.5]
        # print(results[["_distance", "text"]])
        text_results = results["text"].tolist()

        return text_results

    # -------------------FETCHING CONV FILE FOR AI ASSISTANT---------------#
    def get_conv_file(self, id, user_id, folder_name):

        # print(f"DEBUG: id={id}, user_id={user_id}, folder_name={folder_name}")
        table = self._get_umail_table(user_id)
        results = (
            table.search()
            .where(
                f"id = '{id}' AND user_id = '{user_id}' AND folder_name = '{folder_name}'"
            )
            .to_pandas()
        )
        text_results = results["text"]

        return text_results

    async def _open_recording_create(self, user_id: str):
        """
        Open existing table or create it with an 'init' dummy row.
        Removes dummy row once and attempts to create an index (quietly).
        """
        table_name = f"aud_{user_id}"
        self.db = self._connect_if_needed()

        def _open():
            return self.db.open_table(table_name)

        try:
            table = await asyncio.to_thread(_open)
            return table

        except Exception:
            # Table does not exist — create it
            schema = await self._create_schema_dummy()

            enc = self.secure_kms.encrypt(user_id, "init row")

            dummy = [
                {
                    "id": "init",
                    "text": json.dumps(enc),
                    "embedding": np.zeros(self.EMBEDDING_DIM, dtype=np.float32),
                    "foldername": "init",
                }
            ]

            def _create():
                return self.db.create_table(
                    table_name, data=dummy, schema=schema, mode="create"
                )

            try:
                table = await asyncio.to_thread(_create)
            except Exception as e:
                logger.exception("Failed to create table %s: %s", table_name, e)
                if self.error_hook:
                    try:
                        self.error_hook(
                            e, {"action": "create_table", "table_name": table_name}
                        )

                    except Exception:
                        logger.debug("error_hook raised an exception")
                raise

            # Remove dummy row once (use double quotes)
            try:
                await asyncio.to_thread(lambda: table.delete('id == "init"'))
            except Exception as e:
                logger.warning("Failed to delete dummy row for %s: %s", table_name, e)

            # Try to create index quietly (may be no-op or fail)
            try:
                await asyncio.to_thread(lambda: table.create_index("embedding"))
            except Exception as e:
                logger.debug("Index creation warning for %s: %s", table_name, e)

            logger.info("Created table %s for user %s", table_name, user_id)
            return table

    @retry_async(attempts=4, initial_delay=0.5, factor=2.0, max_delay=8.0, jitter=0.15)
    async def rec_insert_vector(self, data: "VectorData"):
        """
        Insert a single vector record (async).
        Expects `VectorData`-like object/dict with fields: user_id, id, text, embedding, foldername.
        """
        start = time.time()
        try:
            if len(data.embedding) != self.EMBEDDING_DIM:
                raise ValueError(f"Embedding must be {self.EMBEDDING_DIM} floats long")

            table = await self._open_recording_create(data.user_id)

            embedding = np.array(data.embedding, dtype=np.float32)

            # Delete existing record with same id+folder (double quotes)
            def _delete_existing():
                return table.delete(
                    f'id == "{data.id}" AND foldername == "{data.foldername}"'
                )

            await asyncio.to_thread(_delete_existing)

            enc = self.secure_kms.encrypt(data.user_id, data.text)

            encrypted_payload = json.dumps(
                {
                    "ciphertext": enc["ciphertext"],
                    "iv": enc.get("iv"),
                    "encrypted_key": enc.get("encrypted_key"),
                }
            )

            payload = {
                "id": data.id,
                "text": encrypted_payload,
                "embedding": embedding,
                "foldername": data.foldername,
            }

            # add (append=True recommended)
            await asyncio.to_thread(table.add, [payload])

            latency = time.time() - start
            logger.debug(
                "Inserted vector id=%s user=%s latency=%.3fs",
                data.id,
                data.user_id,
                latency,
            )
            if self.metrics:
                try:
                    self.metrics.increment("lancedb.insert.success")
                    self.metrics.timing("lancedb.insert.latency", latency)
                except Exception:
                    logger.debug("metrics client call failed on insert_vector")
        except Exception as e:
            logger.exception(
                "insert_vector failed for user=%s id=%s: %s",
                getattr(data, "user_id", None),
                getattr(data, "id", None),
                e,
            )
            if self.metrics:
                try:
                    self.metrics.increment("lancedb.insert.failure")
                except Exception:
                    logger.debug("metrics client increment failed on insert failure")
            if self.error_hook:
                try:
                    self.error_hook(
                        e,
                        {
                            "action": "insert_vector",
                            "user_id": getattr(data, "user_id", None),
                            "id": getattr(data, "id", None),
                        },
                    )
                except Exception:
                    logger.debug("error_hook raised an exception")
            raise

    @retry_async(attempts=3, initial_delay=0.2, factor=2.0, max_delay=4.0, jitter=0.1)
    async def rec_query_vector(self, query: "QueryData") -> List[Dict[str, Any]]:
        """
        Single-vector query. Returns list of result dicts as produced by LanceDB `.to_list()`.
        """
        if isinstance(query, dict):
            query = QueryData(**query)
        start = time.time()
        # print("len values", len(query.embedding))
        try:
            if len(query.embedding) > self.EMBEDDING_DIM:
                raise ValueError(
                    f"Query embedding must be {self.EMBEDDING_DIM} floats long"
                )
            table = await self._open_recording_create(query.user_id)
            query_embedding = np.array(query.embedding, dtype=np.float32)

            def _search():
                return (
                    table.search(query_embedding, vector_column_name="embedding")
                    .limit(query.top_k)
                    .to_list()
                )

            results = await asyncio.to_thread(_search)

            for r in results:
                enc = json.loads(r["text"])
                if "encrypted_key" in enc:
                    r["text"] = self.secure_kms.decrypt(
                        query.user_id,
                        enc["encrypted_key"],
                        enc["iv"],
                        enc["ciphertext"],
                    )
            latency = time.time() - start
            logger.debug(
                "query_vector user=%s top_k=%d results=%d latency=%.3fs",
                query.user_id,
                query.top_k,
                len(results),
                latency,
            )
            if self.metrics:
                try:
                    self.metrics.increment("lancedb.query.success")
                    self.metrics.timing("lancedb.query.latency", latency)
                except Exception:
                    logger.debug("metrics client call failed on query_vector")

            return results
        except Exception as e:
            logger.exception(
                "query_vector failed for user=%s: %s",
                getattr(query, "user_id", None),
                e,
            )
            if self.metrics:
                try:
                    self.metrics.increment("lancedb.query.failure")
                except Exception:
                    logger.debug("metrics client increment failed on query failure")
            if self.error_hook:
                try:
                    self.error_hook(
                        e,
                        {
                            "action": "query_vector",
                            "user_id": getattr(query, "user_id", None),
                        },
                    )
                except Exception:
                    logger.debug("error_hook raised an exception")
            raise

    @retry_async(attempts=3, initial_delay=0.2, factor=2.0, max_delay=4.0, jitter=0.1)
    async def rec_query_vector_foldername(
        self, query: "QueryData", foldername: str
    ) -> List[Dict[str, Any]]:

        if isinstance(query, dict):
            query = QueryData(**query)

        start = time.time()

        try:
            if len(query.embedding) > self.EMBEDDING_DIM:
                raise ValueError(
                    f"Query embedding must be {self.EMBEDDING_DIM} floats long"
                )

            table = await self._open_recording_create(query.user_id)
            query_embedding = np.array(query.embedding, dtype=np.float32)

            def _search():
                return (
                    table.search(query_embedding, vector_column_name="embedding")
                    .where(f'foldername == "{foldername}"')  # 🔥 filter by recording
                    .limit(query.top_k)
                    .to_list()
                )

            results = await asyncio.to_thread(_search)

            for r in results:
                r["text"] = self.secure_kms.decrypt(query.user_id, r["text"])
            latency = time.time() - start
            logger.debug(
                "rec_query_vector_foldername user=%s folder=%s top_k=%d results=%d latency=%.3fs",
                query.user_id,
                foldername,
                query.top_k,
                len(results),
                latency,
            )

            if self.metrics:
                try:
                    self.metrics.increment("lancedb.query.success")
                    self.metrics.timing("lancedb.query.latency", latency)
                except Exception:
                    logger.debug("metrics client call failed on query_vector")

            return results

        except Exception as e:
            logger.exception(
                "rec_query_vector_foldername failed user=%s folder=%s: %s",
                getattr(query, "user_id", None),
                foldername,
                e,
            )

            if self.metrics:
                try:
                    self.metrics.increment("lancedb.query.failure")
                except Exception:
                    logger.debug("metrics client increment failed on query failure")

            if self.error_hook:
                try:
                    self.error_hook(
                        e,
                        {
                            "action": "rec_query_vector_foldername",
                            "user_id": getattr(query, "user_id", None),
                            "foldername": foldername,
                        },
                    )
                except Exception:
                    logger.debug("error_hook raised an exception")

            raise

    @retry_async(attempts=3, initial_delay=0.2, factor=2.0, max_delay=4.0, jitter=0.1)
    async def rec_delete_vector(self, data: "DeleteData"):
        """
        Delete a single vector by id for a user.
        """
        try:
            table = await self._open_recording_create(data.user_id)
            await asyncio.to_thread(lambda: table.delete(f'id == "{data.id}"'))
            logger.debug("Deleted vector id=%s user=%s", data.id, data.user_id)
            if self.metrics:
                try:
                    self.metrics.increment("lancedb.delete.success")
                except Exception:
                    logger.debug("metrics client call failed on delete_vector")
            return True
        except Exception as e:
            logger.exception(
                "delete_vector failed for user=%s id=%s: %s",
                getattr(data, "user_id", None),
                getattr(data, "id", None),
                e,
            )
            if self.metrics:
                try:
                    self.metrics.increment("lancedb.delete.failure")
                except Exception:
                    logger.debug("metrics client increment failed on delete failure")
            if self.error_hook:
                try:
                    self.error_hook(
                        e,
                        {
                            "action": "delete_vector",
                            "user_id": getattr(data, "user_id", None),
                            "id": getattr(data, "id", None),
                        },
                    )
                except Exception:
                    logger.debug("error_hook raised an exception")
            return False

    async def update_contacts_for_audio(
        self, user_id: str, filename: str, contacts: str
    ):
        table = await self._open_recording_create(user_id=user_id)

        # Search for row using foldername mapped to filename
        matches = table.search().where(f"foldername == '{filename}'").limit(1).to_list()

        if not matches:
            return False

        row = matches[0]
        row_id = row["id"]

        # Load stored JSON
        decrypted_text = self.secure_kms.decrypt(user_id, row["text"])

        data = json.loads(decrypted_text)

        data["contacts"] = contacts

        enc = self.secure_kms.encrypt(user_id, json.dumps(data, ensure_ascii=False))

        encrypted_text = enc["ciphertext"]

        # Remote-table compatible update
        table.update(
            values={"text": encrypted_text},
            where=f"id == '{row_id}'",
        )

        return True

    async def delete_all_user_Data(self, user_id: str) -> int:
        """
        Deletes ALL vector rows for a user from LanceDB.
        Returns number of rows deleted.
        """
        try:
            table = await asyncio.to_thread(lambda: self._get_table(user_id))
            logger.info(f"Deleting ALL vector rows for user '{user_id}'")

            deleted_count = await asyncio.to_thread(lambda: table.delete("True"))

            logger.info(f"Deleted {deleted_count} total rows for user '{user_id}'")
            return deleted_count

        except Exception as e:
            logger.exception(f"Failed to delete ALL data for user '{user_id}': {e}")

            if self.error_hook:
                try:
                    self.error_hook(
                        e,
                        {
                            "action": "delete_all_user_data",
                            "user_id": user_id,
                        },
                    )
                except Exception:
                    logger.debug("error_hook raised an exception")

            raise

    async def _open_or_create_apiconnectors_table(
        self, user_id: str, app_id: str, endpoint_id: str
    ):
        table_name = f"apiconnectors_{user_id}_{app_id}_{endpoint_id}"
        self.db = self._connect_if_needed()

        schema = pa.schema(
            [
                pa.field("id", pa.string()),
                pa.field("foldername", pa.string()),
                pa.field("text", pa.string()),
                pa.field("original", pa.string()),  # ✅ raw JSON as string
                pa.field("embedding", pa.list_(pa.float32(), self.EMBEDDING_DIM)),
            ]
        )

        required_cols = ["id", "foldername", "text", "original", "embedding"]

        def _open():
            return self.db.open_table(table_name)

        try:
            table = await asyncio.to_thread(_open)

            # 🚨 Detect schema drift
            if table.schema.names != required_cols:
                # print(f"⚠️ Schema mismatch for {table_name}, recreating")
                await asyncio.to_thread(lambda: self.db.drop_table(table_name))
                raise Exception("Schema mismatch")

            return table

        except Exception:
            dummy = [
                {
                    "id": "init",
                    "foldername": "init",
                    "text": "init",
                    "original": "{}",
                    "embedding": np.zeros(self.EMBEDDING_DIM, dtype=np.float32),
                }
            ]

            def _create():
                return self.db.create_table(
                    table_name,
                    data=dummy,
                    schema=schema,
                    mode="create",
                )

            table = await asyncio.to_thread(_create)

            await asyncio.to_thread(lambda: table.delete('id == "init"'))

            # ✅ Correct vector index
            try:
                await asyncio.to_thread(
                    lambda: table.create_index(column="embedding", metric="cosine")
                )
            except Exception as e:
                # print("Index creation warning:", e)
                return e

            # print(f"Created LanceDB table {table_name}")
            return table

    async def get_app_runs(
        self,
        user_id: str,
        app_id: str,
        endpoint_id: str,
        limit: int = 10,
        newest_first: bool = True,
    ):
        """
        Fetch the latest app run records for a given endpoint.
        Sorted by foldername (minute_bucket).
        """
        table = await self._open_or_create_apiconnectors_table(
            user_id, app_id, endpoint_id
        )

        def _query():
            records = table.search().limit(limit).to_list()
            # Sort by foldername (minute_bucket)
            records.sort(key=lambda r: r["foldername"], reverse=newest_first)
            return records[:limit]

        records = await asyncio.to_thread(_query)

        # Parse JSON text
        # for r in records:
        #     r["text"] = json.loads(r["text"])
        for r in records:
            for field in ["text", "original"]:
                val = r.get(field)

                if isinstance(val, str):
                    val = val.strip()

                    if not val or val == "init":
                        r[field] = None
                        continue

                    try:
                        r[field] = json.loads(val)
                    except json.JSONDecodeError:
                        # leave as-is (string)
                        pass
        return records

    async def delete_app_runs(self, user_id: str, app_id: str, endpoint_id: str):
        """
        Delete all runs for a given endpoint.
        """
        table = await self._open_or_create_apiconnectors_table(
            user_id, app_id, endpoint_id
        )

        def _delete():
            table.delete(
                'id != "init"'
            )  # delete all real records, keep dummy if exists

        await asyncio.to_thread(_delete)
        return True

    async def query_app_endpoint(self, payload: dict):
        user_id = payload["user_id"]
        app_id = payload["app_id"]
        endpoint_id = payload["endpoint_id"]
        embedding = payload["embedding"]
        foldernames = payload.get("foldernames")
        top_k = payload.get("top_k", 5)

        table_name = f"apiconnectors_{user_id}_{app_id}_{endpoint_id}"

        db = self._connect_if_needed()
        table = db.open_table(table_name)

        # 1️⃣ Build LanceDB query
        query = table.search(embedding)

        # 2️⃣ Apply foldername filter (time slicing)
        if foldernames:
            quoted = ",".join([f'"{f}"' for f in foldernames])
            query = query.where(f"foldername IN ({quoted})")

        # 3️⃣ Run vector search
        results = query.limit(top_k).to_list()

        # 4️⃣ Parse JSON payloads
        final = []
        for r in results:
            final.append(
                {
                    "score": r.get("_distance") or r.get("score"),
                    "foldername": r["foldername"],
                    "data": json.loads(r["text"]),
                }
            )

        return final

    # --------------  bytoid pro --------------------

    def _get_bytoid_pro_table(self, user_id):
        table_name = f"bytoid_pro_chat{user_id}"
        # print("in bytoid pro chat table")

        try:
            self.db = self._connect_if_needed()
            # print("self.db", self.db)
            return self.db.open_table(table_name)
        except Exception:
            # print(f"[DEBUG] Creating new table {table_name}")

            schema = pa.schema(
                [
                    pa.field("id", pa.string()),
                    pa.field("chat_id", pa.string()),
                    pa.field("role", pa.string()),
                    pa.field("content", pa.string()),
                    pa.field("timestamp", pa.string()),
                    pa.field("images", pa.list_(pa.string())),
                    pa.field("files", pa.list_(pa.string())),
                    pa.field("embedding", pa.list_(pa.float32(), self.EMBEDDING_DIM)),
                ]
            )

            dummy = [
                {
                    "id": "init",
                    "chat_id": "init row",
                    "role": "init_row",
                    "content": "init_row",
                    "timestamp": "init_row",
                    "images": [],
                    "files": [],
                    "embedding": [0.0] * self.EMBEDDING_DIM,
                }
            ]

            return self.db.create_table(
                table_name, data=dummy, schema=schema, mode="create"
            )

    async def insert_chat(self, chat, user_id):
        # print("in the insert chat")
        if not chat:
            return {"inserted_count": 0}

        chat_id = chat[0].chat_id
        # print(f"********user id inside lance: {user_id}")
        # folder_name = chat[0].folder_name
        id = chat[0].id
        # print(f"********id inside lance: {id}")

        table = self._get_bytoid_pro_table(user_id)
        # print("table", table)

        records = []
        for v in chat:

            records.append(
                {
                    "id": v.id,
                    "chat_id": v.chat_id,
                    "role": v.role,
                    "content": v.content,
                    "timestamp": v.timestamp,
                    "images": v.images or [],
                    "files": v.files or [],
                    "embedding": v.embedding,
                }
            )

        # Add records in one shot
        table.add(records)

        return {"user_id": user_id, "chat_id": chat_id}

    def get_user_chats_by_timestamp(
        self, user_id: str, last_timestamp: str = None, limit: int = 30
    ):
        """
        Fetch chat rows for a user, ordered by timestamp.
        If last_timestamp is provided, fetch rows newer than that (cursor pagination).
        """
        table = self._get_bytoid_pro_table(user_id)

        query = table.search()

        if last_timestamp:
            # fetch only rows after the last timestamp
            query = query.where(f"timestamp > '{last_timestamp}'")

        rows = query.limit(limit).to_list()

        # sort oldest → newest
        rows.sort(key=lambda r: r.get("timestamp", ""))
        return rows

    def get_chat_by_id(self, user_id: str, chat_id: str) -> List[Dict]:
        """
        Fetch all chat messages for a given user_id and chat_id from LanceDB.
        Returns a list of dictionaries with: id, chat_id, role, content, timestamp
        """

        table = self._get_bytoid_pro_table(user_id)

        # Search all rows for the given chat_id
        rows = table.search().where(f"chat_id == '{chat_id}'").limit(30).to_list()

        return rows

    def find_semantic_matches(self, vector, user_id, chat_id, top_k: int = 5):
        try:
            # print("inside find_semantic_matches")
            table = self._get_bytoid_pro_table(user_id)

            # 1️⃣ Create search object and filter by chat_id
            search_obj = (
                table.search(vector, vector_column_name="embedding")
                .metric("cosine")
                .limit(top_k)
            )

            if chat_id:
                search_obj = search_obj.where(f"chat_id == '{chat_id}'")

            # 2️⃣ Execute search and convert to DataFrame
            results_df = search_obj.to_pandas()

            # 3️⃣ Optionally filter by distance if needed
            results_df = results_df[results_df["_distance"] < 1.5]

            # 4️⃣ Extract relevant content
            matched_content = []
            for _, row in results_df.iterrows():
                matched_content.append(
                    {
                        "chat_id": row.get("chat_id"),
                        "role": row.get("role"),
                        "content": row.get("content"),
                        "timestamp": row.get("timestamp"),
                        "images": row.get("images"),
                        "files": row.get("files"),
                        "score": row.get("_distance"),  # cosine distance
                    }
                )

            # print(f"matched_content : {matched_content}")
            return matched_content

        except Exception as e:
            # print(f"Semantic search failed: {str(e)}")
            return []

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------
    async def _create_radar_schema(self):
        return pa.schema(
            [
                pa.field("id", pa.string()),  # logical RADAR session id
                pa.field("name", pa.string()),
                pa.field("review_id", pa.string()),  # unique per run
                pa.field("user_input", pa.string()),
                pa.field("status", pa.string()),
                pa.field("result", pa.string()),  # JSON
                pa.field("error", pa.string()),
                pa.field("started_at", pa.int64()),
                pa.field("main_source", pa.string()),
                pa.field("data_sources", pa.string()),
                pa.field("reference_sources", pa.string()),
                pa.field("refernce_main_source", pa.string()),
            ]
        )

    # ------------------------------------------------------------------
    # Table bootstrap
    # ------------------------------------------------------------------
    async def _open_or_create_radar_table(self, user_id: str):
        table_name = f"radar_{user_id}"
        self.db = self._connect_if_needed()

        def _open():
            return self.db.open_table(table_name)

        try:
            return await asyncio.to_thread(_open)

        except Exception:
            schema = await self._create_radar_schema()

            dummy = [
                {
                    "id": "init",
                    "name": "",
                    "review_id": "init",
                    "user_input": "",
                    "status": "init",
                    "result": "{}",
                    "error": "",
                    "started_at": 0,
                    "main_source": "",
                    "data_sources": "",
                    "reference_sources": "",
                    "refernce_main_source": "",
                }
            ]

            def _create():
                return self.db.create_table(
                    table_name, data=dummy, schema=schema, mode="create"
                )

            table = await asyncio.to_thread(_create)

            try:
                await asyncio.to_thread(lambda: table.delete('review_id == "init"'))
            except Exception:
                pass

            logger.info("Created RADAR table %s", table_name)
            return table

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------
    async def radar_create_review(self, user_id: str, review_data: dict):
        table = await self._open_or_create_radar_table(user_id)

        record = {
            "id": review_data.get("id"),
            "name": review_data.get("name", ""),
            "review_id": review_data["review_id"],
            "user_input": review_data.get("user_input", ""),
            "status": review_data.get("status", ""),
            "result": json.dumps(review_data.get("result") or {}),
            "error": review_data.get("error") or "",
            "started_at": review_data.get("started_at", int(time.time())),
            "main_source": review_data.get("main_source", ""),
            "data_sources": json.dumps(review_data.get("data_sources") or {}),
            "reference_sources": json.dumps(review_data.get("reference_sources") or {}),
            "refernce_main_source": review_data.get("refernce_main_source", ""),
        }

        await asyncio.to_thread(lambda: table.add([record]))

    # ------------------------------------------------------------------
    # Upsert (append new execution)
    # ------------------------------------------------------------------
    async def radar_upsert_review(
        self,
        user_id: str,
        *,
        name: str,
        radar_id: str,
        review_id: str,
        user_input: str,
        new_result: dict,
        status: str,
        error: str = "",
        main_source: str = "",
        data_sources=None,
        reference_sources=None,
        refernce_main_source: str = "",
    ):
        table = await self._open_or_create_radar_table(user_id)
        now = int(time.time())

        record = {
            "id": radar_id,
            "name": name,
            "review_id": review_id,
            "user_input": user_input,
            "status": status,
            "result": json.dumps(new_result or {}),
            "error": error or "",
            "started_at": now,
            "main_source": main_source,
            "data_sources": json.dumps(data_sources or {}),
            "reference_sources": json.dumps(reference_sources or {}),
            "refernce_main_source": refernce_main_source,
        }

        await asyncio.to_thread(lambda: table.add([record]))

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    async def radar_get_by_id(self, user_id: str, radar_id: str):
        table = await self._open_or_create_radar_table(user_id)

        def _query():
            return table.search().where(f'id == "{radar_id}"').to_list()

        rows = await asyncio.to_thread(_query)
        if not rows:
            return None

        row = rows[0]
        row["result"] = _safe_json_parse(row.get("result"))
        row["data_sources"] = _safe_json_parse(row.get("data_sources"))
        row["reference_sources"] = _safe_json_parse(row.get("reference_sources"))
        return row

    async def radar_get_review(self, user_id: str, review_id: str):
        table = await self._open_or_create_radar_table(user_id)

        def _query():
            return table.search().where(f'review_id == "{review_id}"').to_list()

        rows = await asyncio.to_thread(_query)
        if not rows:
            return None

        row = rows[0]
        row["result"] = _safe_json_parse(row.get("result"))
        row["data_sources"] = _safe_json_parse(row.get("data_sources"))
        row["reference_sources"] = _safe_json_parse(row.get("reference_sources"))
        return row

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _extract_result(self, row: dict):
        result = _safe_json_parse(row.get("result"))
        if isinstance(result, dict) and "professional_review" in result:
            return result["professional_review"]
        return result

    async def radar_get_review_last_response(
        self,
        user_id: str,
        radar_id: str,
        review_id=None,
    ):
        table = await self._open_or_create_radar_table(user_id)

        def _query():
            return table.search().where(f'id == "{radar_id}"').to_list()

        rows = await asyncio.to_thread(_query)
        if not rows:
            return None

        if review_id:
            for row in rows:
                if row.get("review_id") == review_id:
                    return self._extract_result(row)
            return None

        rows.sort(key=lambda r: r.get("started_at", 0), reverse=True)
        return self._extract_result(rows[0])

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------
    async def radar_delete_review(self, user_id: str, review_id: str):
        table = await self._open_or_create_radar_table(user_id)

        def _delete_and_verify():
            # 1️⃣ Check before delete
            before_count = table.count_rows(f'review_id == "{review_id}"')

            if before_count == 0:
                return {
                    "deleted": False,
                    "reason": "review_id not found",
                    "before": 0,
                    "after": 0,
                }

            # 2️⃣ Delete
            table.delete(f'review_id == "{review_id}"')

            # 3️⃣ Physically remove
            table.compact_files()

            # 4️⃣ Verify after delete
            after_count = table.count_rows(f'review_id == "{review_id}"')

            return {
                "deleted": after_count == 0,
                "before": before_count,
                "after": after_count,
            }

        result = await asyncio.to_thread(_delete_and_verify)
        return result

    # ------------------------------------------------------------------
    # Lists
    # ------------------------------------------------------------------
    async def radar_get_all_reviews(self, user_id: str):
        table = await self._open_or_create_radar_table(user_id)

        def _query():
            return table.search().to_list()

        rows = await asyncio.to_thread(_query) or []
        return rows

    async def radar_get_review_index(self, user_id: str):
        table = await self._open_or_create_radar_table(user_id)

        def _query():
            return table.search().to_list()

        rows = await asyncio.to_thread(_query) or []

        reviews = [
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "review_id": r.get("review_id"),
                "user_input": r.get("user_input"),
                "status": r.get("status"),
                "started_at": r.get("started_at", 0),
            }
            for r in rows
        ]

        reviews.sort(key=lambda r: r["started_at"], reverse=True)
        return reviews

    async def radar_update_result(
        self,
        user_id: str,
        review_id: str,
        new_result: dict,
    ):
        table = await self._open_or_create_radar_table(user_id)

        payload = {
            "result": json.dumps(new_result),
            "status": "completed",
            "error": "",
        }

        def _update():
            table.update(where=f'review_id == "{review_id}"', values=payload)

        await asyncio.to_thread(_update)

    # runbook schema
    async def _create_runbook_schema(self):

        return pa.schema(
            [
                pa.field("runbook_id", pa.string()),
                pa.field("user_id", pa.string()),
                pa.field("name", pa.string()),
                pa.field("description", pa.string()),
                pa.field("runbook_type", pa.string()),
                pa.field("schedule", pa.string()),
                pa.field("input_type", pa.string()),
                pa.field("playbook_id", pa.string()),
                pa.field("api_endpoint", pa.string()),
                pa.field("log_source", pa.string()),
                pa.field("files", pa.string()),
                pa.field("links", pa.string()),
                pa.field("data_sources", pa.string()),
                pa.field("reference_sources", pa.string()),
                pa.field("app_id", pa.string()),
                pa.field("is_template", pa.string()),
                pa.field("structure_theme", pa.string()),
                pa.field("playbook_source", pa.string()),
                pa.field("api_source", pa.string()),
                pa.field("log_file", pa.string()),
                pa.field("main_source", pa.string()),
                pa.field("reference_main_source", pa.string()),
                pa.field("created_at", pa.timestamp("us")),
                pa.field("runbook_evidence_config", pa.string()),
                pa.field("tracker_configuration", pa.string()),
            ]
        )

    async def _open_or_create_runbook_table(self, user_id: str):

        table_name = f"runbook_{user_id}"
        self.db = self._connect_if_needed()

        try:
            # ✅ Try opening existing table
            # print("opening the runbook table")
            table = await asyncio.to_thread(lambda: self.db.open_table(table_name))
            return table

        except Exception:
            # ✅ Create only if not exists
            schema = await self._create_runbook_schema()

            table = await asyncio.to_thread(
                lambda: self.db.create_table(table_name, schema=schema, mode="create")
            )
            return table

    # insert runbook details
    async def insert_runbook(self, data: dict):
        table = await self._open_or_create_runbook_table(data["user_id"])
        # print("TABLE SCHEMA:", table.schema)
        row = {
            "runbook_id": data["runbook_id"],
            "user_id": data["user_id"],
            "name": data["name"],
            "description": data.get("description", ""),
            "runbook_type": data.get("runbook_type"),
            "schedule": data.get("schedule", {}),
            "input_type": data.get("input_type"),
            "playbook_id": data.get("playbook_id", ""),
            "api_endpoint": data.get("api_endpoint", ""),
            "app_id": data.get("app_id", ""),
            "log_source": data.get("log_source", ""),
            "files": data.get("files", {}),
            "links": data.get("links", {}),
            "data_sources": data.get("data_sources", {}),
            "reference_sources": data.get("reference_sources", {}),
            "is_template": data.get("is_template", ""),
            "structure_theme": data.get("structure_theme", {}),
            "playbook_source": data.get("playbook_source", ""),
            "api_source": data.get("api_source", ""),
            "log_file": data.get("log_file", ""),
            "main_source": data.get("main_source"),
            "reference_main_source": data.get("reference_main_source"),
            "created_at": data.get("created_at") or datetime.utcnow().isoformat(),
            "runbook_evidence_config": data.get("runbook_evidence_config", ""),
            "tracker_configuration": json.dumps(data.get("tracker_configuration") or {})

        }

        # print("ROW KEYS:", row.keys())
        def _insert():
            table_fields = {f.name for f in table.schema}
            filtered_row = {k: v for k, v in row.items() if k in table_fields}
            print([filtered_row])
            return table.add([filtered_row])

        await asyncio.to_thread(_insert)

        return row

    # get runbook by id
    async def get_runbook_by_id(self, user_id: str, runbook_id: str):

        table = await self._open_or_create_runbook_table(user_id)

        def _query():
            return table.search().where(f"runbook_id == '{runbook_id}'").to_list()

        result = await asyncio.to_thread(_query)

        return result

    # get all runbooks of user
    async def get_user_runbook(self, user_id: str):

        # FIX 1: await the table creation
        table = await self._open_or_create_runbook_table(user_id)

        # FIX 2: use to_list instead of to_pandas
        records = await asyncio.to_thread(lambda: table.search().limit(1000).to_list())

        # Remove dummy/init row
        records = [r for r in records if r.get("runbook_id") != "init"]

        # Optional (only if shared table)
        # records = [r for r in records if r.get("user_id") == user_id]

        return records

    async def delete_runbook(self, user_id: str, runbook_id: str):

        table = await self._open_or_create_runbook_table(user_id)

        await asyncio.to_thread(lambda: table.delete(f'runbook_id == "{runbook_id}"'))

        return True

    async def delete_all_runbook(self, user_id: str, runbook_id: list[str]):

        table = await self._open_or_create_runbook_table(user_id)
        # ✅ Build filter: runbook_id IN (...)
        ids_str = ", ".join([f'"{rid}"' for rid in runbook_id])
        filter_expr = f"runbook_id IN ({ids_str})"

        await asyncio.to_thread(lambda: table.delete(filter_expr))

        # Delete all associated results for each runbook
        results_table = await self._open_or_create_runbook_results_table(user_id)
        for rid in runbook_id:
            await asyncio.to_thread(
                lambda r=rid: results_table.delete(f'runbook_id == "{r}"')
            )

        return True

    async def update_runbook(self, user_id: str, runbook_id: str, updates: dict):
        table = await self._open_or_create_runbook_table(user_id)

        # Step 1: Fetch existing runbook
        def _query():
            return table.search().where(f'runbook_id == "{runbook_id}"').to_list()

        records = await asyncio.to_thread(_query)
        existing = records[0] if records else None

        if not existing:
            raise ValueError("Runbook not found")

        # -------------------------------
        # Helper: merge tracker config
        # -------------------------------
        def merge_tracker_config(existing_str, new_dict):
            try:
                existing_map = json.loads(existing_str or "{}")
            except:
                existing_map = {}

            existing_map.update(new_dict)
            return json.dumps(existing_map)

        # -------------------------------
        # Handle tracker_configuration
        # -------------------------------
        if "tracker_configuration" in updates:
            new_val = updates["tracker_configuration"]

            # Ensure incoming value is dict
            if isinstance(new_val, str):
                try:
                    new_val = json.loads(new_val)
                except:
                    new_val = {}

            updates["tracker_configuration"] = merge_tracker_config(
                existing.get("tracker_configuration"),
                new_val
            )

        # -------------------------------
        # Normalize function
        # -------------------------------
        def normalize(value, key=None):
            # tracker_configuration already JSON string → don't re-dump
            if key == "tracker_configuration":
                return value or json.dumps({})

            if isinstance(value, (dict, list)):
                return json.dumps(value)

            # Keep None as None so pa.Table.from_pydict with schema can handle
            # typed nulls (e.g. pa.timestamp) without triggering ArrowInvalid
            # when the inferred type (string) mismatches the table column type.
            if value is None:
                return None

            return value

        # Step 2: Merge + normalize
        merged = {**existing, **updates}
        updated_row = {k: normalize(v, k) for k, v in merged.items()}

        # -------------------------------
        # Prepare insert
        # -------------------------------
        def _insert():
            table_fields = {f.name for f in table.schema}
            filtered = {k: v for k, v in updated_row.items() if k in table_fields}
            column_data = {k: [v] for k, v in filtered.items()}
            # Use the existing table schema so typed columns (e.g. timestamp)
            # get the correct Arrow type even when the value is None.
            insert_schema = pa.schema(
                [f for f in table.schema if f.name in column_data]
            )
            table.add(pa.Table.from_pydict(column_data, schema=insert_schema))

        # -------------------------------
        # Delete old record
        # -------------------------------
        def _delete():
            table.delete(f'runbook_id == "{runbook_id}"')

        # ✅ IMPORTANT: insert first, then delete
        await asyncio.to_thread(_delete)
        await asyncio.to_thread(_insert)
        

        return updated_row

    async def get_all_runbooks(self, user_id: str):
        # Get runbooks table
        table = await self._open_or_create_runbook_table(user_id)

        # Fetch all runbooks (Cloud-friendly)
        runbooks = await asyncio.to_thread(lambda: table.search().limit(1000).to_list())

        # Remove dummy row
        runbooks = [r for r in runbooks if r["runbook_id"] != "init"]

        # ✅ Fetch ALL results in ONE call
        all_results = await self.get_runbook_results_by_user_id(user_id)

        result_map = {}

        for r in all_results:
            if (
                r.get("status", "").lower() == "completed"
                and r.get("risk_score") != 0.0
            ):  # STRICT filter
                rid = r.get("runbook_id")

                if rid:
                    # Keep latest execution
                    if (
                        rid not in result_map
                        or r["started_at"] > result_map[rid]["started_at"]
                    ):
                        result_map[rid] = r

        # ✅ Return only runbooks with completed executions
        final_results = []

        for rb in runbooks:
            rid = rb["runbook_id"]

            if rid in result_map:
                latest = result_map[rid]

                rb["execution_result"] = {
                    "execution_id": latest.get("execution_id"),
                    "result_id": latest.get("result_id"),  # make sure this exists in DB
                }

            final_results.append(rb)

        return final_results

    # runbook results schema
    async def _create_runbook_results_schema(self):

        return pa.schema(
            [
                pa.field("result_id", pa.string()),
                pa.field("runbook_id", pa.string()),
                pa.field("execution_id", pa.string()),
                pa.field("user_id", pa.string()),
                pa.field("input_mode", pa.string()),
                pa.field("status", pa.string()),  # running / completed / failed
                pa.field("risk_score", pa.float32()),
                pa.field("started_at", pa.int64()),
                pa.field("ended_at", pa.int64()),
                pa.field("execution_time_ms", pa.int64()),
                pa.field("result", pa.large_string()),
            ]
        )

    # open or create new runbook results
    async def _open_or_create_runbook_results_table(self, user_id: str):

        table_name = f"runbook_results_{user_id}"

        self.db = self._connect_if_needed()

        def _open():
            # print("opening the result table")
            return self.db.open_table(table_name)

        try:
            return await asyncio.to_thread(_open)

        except Exception:

            schema = await self._create_runbook_results_schema()

            dummy = [
                {
                    "result_id": "init",
                    "runbook_id": "init",
                    "execution_id": "init",
                    "user_id": user_id,
                    "status": "init",
                    "started_at": 0,
                    "ended_at": 0,
                    "execution_time_ms": 0,
                    "input_mode": "init",
                    "risk_score": 0.0,
                    "result": "init",
                }
            ]

            def _create():
                return self.db.create_table(
                    table_name, data=dummy, schema=schema, mode="create"
                )

            table = await asyncio.to_thread(_create)

            try:
                await asyncio.to_thread(lambda: table.delete('result_id == "init"'))
            except Exception:
                pass

            logger.info("Created RUNBOOK RESULTS table %s", table_name)

            return table

    # insert runbook results
    async def insert_runbook_result(self, data: dict):

        table = await self._open_or_create_runbook_results_table(data["user_id"])
        now = int(time.time())
        # print("inside insert runbook result for user : ",data["user_id"])
        started_at = data.get("started_at", now)
        ended_at = data.get("ended_at", now)
        row = {
            "result_id": data["result_id"],
            "runbook_id": data["runbook_id"],
            "execution_id": data["execution_id"],
            "user_id": data["user_id"],
            "status": data.get("status", "completed"),
            "started_at": started_at,
            "ended_at": ended_at,
            "execution_time_ms": (ended_at - started_at) * 1000,
            "input_mode": data.get("input_mode"),
            "risk_score": float(data.get("risk_score") or 0.0),
            "result": json.dumps(data.get("result", {})),
        }

        try:
            print("📦 ROW TO INSERT:", row["result_id"], row["runbook_id"])

            await asyncio.to_thread(lambda: table.add([row]))

            print("✅ inserted runbook result")
            return row

        except Exception as e:
            print("❌ INSERT FAILED:", str(e))

            import traceback

            print(traceback.format_exc())

    # get runbook results by id
    async def get_runbook_results(self, user_id: str, runbook_id: str):

        table = await self._open_or_create_runbook_results_table(user_id)

        def _query():
            return table.search().where(f'runbook_id == "{runbook_id}"').to_list()

        results = await asyncio.to_thread(_query)

        return results

    async def get_runbook_results_by_user_id(self, user_id: str):

        table = await self._open_or_create_runbook_results_table(user_id)

        def _query():
            return table.search().where(f'user_id == "{user_id}"').to_list()

        results = await asyncio.to_thread(_query)

        return results

    async def delete_runbook_result(self, user_id: str, runbook_id: str):

        table = await self._open_or_create_runbook_results_table(user_id)
        # return True
        await asyncio.to_thread(lambda: table.delete(f'runbook_id == "{runbook_id}"'))

        return True

    async def delete_runbook_result_by_id(
        self, user_id: str, runbook_id: str, result_id: str
    ):
        table = await self._open_or_create_runbook_results_table(user_id)

        def _delete():
            table.delete(f"runbook_id == '{runbook_id}' AND result_id == '{result_id}'")

        await asyncio.to_thread(_delete)
        return True

    # latest runbook result
    async def get_latest_runbook_result(
        self, user_id: str, runbook_id: str, result_id=None
    ):

        table = await self._open_or_create_runbook_results_table(user_id)

        def _query():
            data = table.search().to_list()

            filtered = [
                x
                for x in data
                if x.get("runbook_id") == runbook_id
                and x.get("status") == "completed"
                and (not result_id or x.get("result_id") == result_id)
            ]

            return filtered

        results = await asyncio.to_thread(_query)
        if not results:
            return None
        latest = max(
            results, key=lambda x: x.get("ended_at") or x.get("run_timestamp") or 0
        )

        return latest

    async def get_runbooks_by_endpoint(self, user_id, app_id, endpoint_id):
        table = await self._open_or_create_runbook_table(user_id)

        filter_expr = f"api_endpoint == '{endpoint_id}' AND " f"input_type == 'api'"

        def _query():
            return table.search().where(filter_expr).to_list()

        results = await asyncio.to_thread(_query)

        if not results:
            print("⚠️ No runbooks found for endpoint:", endpoint_id)
            return []

        # ✅ sort newest first
        # results = sorted(results, key=lambda x: x.get("created_at", 0), reverse=True)
        latest_runbook = max(results, key=lambda x: x.get("created_at", 0))
        if isinstance(latest_runbook, str):
            latest_runbook = json.loads(latest_runbook)

        # print("latest result: ",latest_runbook)

        return latest_runbook
        # return results  # ✅ ALWAYS RETURN LIST

    async def get_runbook_by_playbookid(self, user_id, playbook_id):
        table = await self._open_or_create_runbook_table(user_id)

        def _query():
            return (
                table.search()
                .where(f"playbook_id == '{playbook_id}' and input_type == 'playbook'")
                .to_list()
            )

        result = await asyncio.to_thread(_query)

        return result

    async def   runbook_get_result(self, user_id, result_id):
        table = await self._open_or_create_runbook_results_table(user_id)

        def _query():
            return (
                table.search()
                .where(f'result_id == "{result_id}" and status == "completed"')
                .to_list()
            )

        rows = await asyncio.to_thread(_query)
        if not rows:
            return {
                "status": "not_found",
                "message": f"No completed result found for result_id={result_id}",
                "data": None,
            }
        row = rows[0]
        # Parse result JSON safely
        row["result"] = _safe_json_parse(row.get("result"))
        if row.get("status") != "completed":
            return {
                "status": "running",
                "message": "Execution still in progress",
                "data": row,
            }

        return row

    async def update_runbook_result(
        self,
        user_id: str,
        result_id: str,
        new_result: dict,
    ):
        table = await self._open_or_create_runbook_results_table(user_id)

        # LanceDB's table.update() is append-only: it writes a new fragment and
        # marks old rows for deletion but does not immediately remove the old
        # fragment. Queries via to_list() can return both versions before
        # compaction runs, causing duplicate rows to appear.
        # Fix: delete the existing row, then re-insert with updated fields.
        def _fetch():
            rows = table.search().where(f'result_id == "{result_id}" and status == "completed"').to_list()
            return rows[0] if rows else None

        existing = await asyncio.to_thread(_fetch)
        if not existing:
            return

        await asyncio.to_thread(lambda: table.delete(f'result_id == "{result_id}"'))

        # Only replace the result field; all other columns stay exactly as stored.
        updated_row = dict(existing)
        updated_row["result"] = json.dumps(new_result)

        await asyncio.to_thread(lambda: table.add([updated_row]))

    async def update_runbook_schedule(self, user_id, runbook_id, schedule):
        table = await self._open_or_create_runbook_table(user_id)

        schedule_str = json.dumps(schedule)

        def _update():
            rows = table.search().where(f'runbook_id = "{runbook_id}"').to_list()

            if not rows:
                raise Exception("Runbook not found")

            table.update(
                where=f'runbook_id = "{runbook_id}"',
                values={
                    "schedule": schedule_str,
                    "runbook_type": "scheduled",
                },
            )

            return True

        await asyncio.to_thread(_update)

        return {"status": "updated", "runbook_id": runbook_id}

