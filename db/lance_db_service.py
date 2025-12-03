import os
import lancedb
from dotenv import load_dotenv
from pydantic import BaseModel
from typing import Any, Callable, Dict, List, Optional, Sequence, Union
import numpy as np
import pyarrow as pa
import json, random, asyncio, time
from datetime import datetime, timedelta, timezone
from utils.base_logger import get_logger

logger = get_logger(__name__)

load_dotenv()
db_key = os.getenv("LANCE_SERVERLESS")
db_uri = os.getenv("LANCE_SERVERLESS_URI")
if not db_key and db_uri:
    print("NEED LANCE DB DETAILS")

EMBEDDING_DIM = 4096
MetricsClientType = Any  # e.g., datadog client with increment/timing/gauge methods
ErrorHookType = Optional[Callable[[Exception, Dict[str, Any]], None]]


# ---- MODELS ----
class ScrapedData(BaseModel):
    user_id: str
    url: str
    title: str
    content: str
    timestamp: str
    metadata: dict
    embedding: List[float]


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
        self.EMBEDDING_DIM = EMBEDDING_DIM
        self.db = None

        self.metrics = MetricsClient()
        self.error_hook = report_exception  # <-- FIXED (no parentheses)

        # try:
        #     # Synchronous connect (recommended)
        #     self.db = lancedb.connect(
        #         uri=self.db_uri,
        #         api_key=self.db_key,
        #         region=self.region,
        #     )

        #     logger.info("Connected to LanceDB (%s)", self.db_uri)

        #     # metrics
        #     try:
        #         self.metrics.increment("lancedb.connect.success")
        #     except Exception:
        #         logger.debug("metrics client increment failed on connect")

        # except Exception as e:
        #     logger.exception("Failed to connect to LanceDB: %s", e)

        #     try:
        #         self.metrics.increment("lancedb.connect.failure")
        #     except Exception:
        #         logger.debug("metrics client increment failed on connect failure")

        #     if self.error_hook:
        #         try:
        #             self.error_hook(e, {"action": "connect"})
        #         except Exception:
        #             logger.debug("error_hook raised an exception")

        #     raise

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
                print("going to create new instance of lance")
                self.db = lancedb.connect(
                    uri=self.db_uri, api_key=self.db_key, region=self.region
                )
                print("CONNECTED:", self.db)
            except Exception as e:
                print("LanceDB CONNECT ERROR:", e)
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

    # -------------------------
    # Public API
    # -------------------------
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
        print("len values", len(query.embedding))
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
            table = await asyncio.to_thread(lambda: self._get_table(user_id))
            logger.info(f"Deleting folder '{foldername}' for user '{user_id}'")

            deleted_count = await asyncio.to_thread(
                lambda: table.delete(f"foldername == '{foldername}'")
            )
            await asyncio.to_thread(lambda: table.optimize())

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
        print("in get umail table")

        try:
            self.db = self._connect_if_needed()
            print("self.db", self.db)
            return self.db.open_table(table_name)
        except Exception:
            print(f"[DEBUG] Creating new table {table_name}")

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
        print("in the insert umail")
        if not vectors:
            return {"inserted_count": 0}

        user_id = vectors[0].user_id
        folder_name = vectors[0].folder_name

        table = self._get_umail_table(user_id)
        print("table", table)

        # === DELETE once per folder (FAST, not per ID) ===
        table.delete(f"folder_name == '{folder_name}'")

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
        table.optimize()

        return {"user_id": user_id, "inserted_count": len(records)}

    def serverless_get_umail_page(self, user_id: str, next_cursor=None, page_size=1000):
        table = self._get_umail_table(user_id)
        print(f"[SERVERLESS] user_id:{user_id} next_cursor:{next_cursor}")

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

    # -------------------SCRAPE FUNCTIONALITY-------------------------------#

    def _get_scrape_table(self, user_id: str):
        """Create or get the scraping data table for a user"""
        table_name = f"scrape_{user_id}"
        self.db = self._connect_if_needed()

        if table_name not in self.db.table_names():
            print(f"[DEBUG] Creating new scrape table for user {user_id}")

            schema = pa.schema(
                [
                    pa.field("user_id", pa.string()),
                    pa.field("url", pa.string()),
                    pa.field("title", pa.string()),
                    pa.field("content", pa.string()),
                    pa.field("timestamp", pa.string()),
                    pa.field("metadata", pa.string()),  # JSON serialized
                    pa.field("embedding", pa.list_(pa.float32(), EMBEDDING_DIM)),
                ]
            )

            # Insert dummy row
            dummy = [
                {
                    "user_id": user_id,
                    "url": "init",
                    "title": "init",
                    "content": "init",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "metadata": "{}",
                    "embedding": np.zeros(EMBEDDING_DIM, dtype=np.float32),
                }
            ]

            return self.db.create_table(
                table_name, data=dummy, schema=schema, mode="overwrite"
            )

        return self.db.open_table(table_name)

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
                    "timestamp": data.timestamp,
                    "metadata": json.dumps(data.metadata),
                    "embedding": np.array(data.embedding, dtype=np.float32),
                }
            ]

            table.add(record)
            table.optimize()

            return {"status": "success", "url": data.url}

        except Exception as e:
            print(f"Error inserting scraped data: {e}")
            raise e

    # -----------------SEARCH EMAIL-------------------------#
    def search_email(self, data: SearchEmailQueryData):

        print("inside search_emails")

        table = self._get_umail_table(data.user_id)
        print(f"schema : {table.schema}")
        print(f"indices : {table.list_indices()}")
        for idx in table.list_indices():
            print(idx)

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
            print(f"filter_condition: {filter_condition}")
            search_obj = search_obj.where(filter_condition)

        results = search_obj.to_pandas()
        print(f"results: {results}")
        results = results[results["_distance"] < 1.5]
        print(results[["_distance", "text"]])
        text_results = results["text"].tolist()

        return text_results

    # -------------------FETCHING CONV FILE FOR AI ASSISTANT---------------#
    def get_conv_file(self, id, user_id, folder_name):

        print(f"DEBUG: id={id}, user_id={user_id}, folder_name={folder_name}")
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
