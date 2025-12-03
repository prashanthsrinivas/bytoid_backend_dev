import asyncio
from dotenv import load_dotenv
import os
import requests
import os
import json
import logging
from typing import List
from pydantic import BaseModel
from datetime import (
    datetime,
    timedelta,
    timezone,
)  # from werkzeug.exceptions import HTTPException
import numpy as np
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.schema import Document
import time

# from sentence_transformers import SentenceTransformer

from utils.fireworkzz import get_firework_embedding
from db.lance_db_service import LanceDBServer

load_dotenv()
logger = logging.getLogger(__name__)
# model = SentenceTransformer('all-MiniLM-L6-v2')

lancedb_url = os.getenv("LANCE_DB_IP")
openai_api_key = os.getenv("OPENAI_API_KEY")


class UmailData(BaseModel):
    id: str
    text: str
    embedding: List[float]
    user_id: str
    folder_name: str
    timestamp: str
    plain_text_embedding: List[float]


class UmailLanceClient:
    def __init__(self, user_id: str):
        self.lancedb_url = lancedb_url
        self.user_id = user_id
        self.dimension = 4096
        self.embeddings = get_firework_embedding()
        self.lance_service = LanceDBServer()

    def get_records_from_lance(self, user_id: str, client_id: str):
        """
        Fetch all records stored under a specific user_id/client_id table.
        """
        try:
            print(
                f"inside get_all_records for user_id : {user_id} client_id :{client_id}"
            )
            # Safely fetch or create table
            # response = requests.post(
            #     f"{self.lancedb_url}/filter_umail_table",
            #     params={"user_id": user_id, "folder_name": client_id},
            # )

            # Convert Arrow Table → Python list of dicts
            # if response.status_code != 200:
            #     raise Exception(
            #         f"API call failed: {response.status_code} - {response.text}"
            #     )
            response = self.lance_service.filter_umail_table(
                user_id=user_id, folder_name=client_id
            )

            # Get results from API response
            results = response.json()["results"]

            # Remove the dummy "init" row
            results = [row for row in results if row["id"] != "init"]

            if not results:
                print("No records found")
                # raise HTTPException(status_code=404, detail="No records found")

            return {"data": results}

        except Exception as e:
            print(f"error : {e}")
            print(f"Response content: {response.text[:500]}")  # First 500 chars

            # raise HTTPException(status_code=400, detail=str(e))

    def latest_messages_from_lance(
        self, user_id, next_cursor
    ):  # used for getting recent messages
        print(f"latest_messages_from_lance")
        # response = requests.post(
        #     f"{self.lancedb_url}/get_umail_table",
        #     params={"user_id": user_id, "next_cursor": next_cursor},
        # )

        # if response.status_code != 200:
        #     raise Exception(
        #         f"API call failed: {response.status_code} - {response.text}"
        #     )
        response = self.lance_service.serverless_get_umail_page(
            user_id=user_id, next_cursor=next_cursor
        )

        # Get results from API response
        if type(response) is dict:
            response_data, vnext_cursor = response.json()
        else:
            response_data, vnext_cursor = response
        if vnext_cursor == next_cursor and vnext_cursor is not None:
            try:
                dt = datetime.fromisoformat(vnext_cursor.replace("Z", "+00:00"))
                dt = dt - timedelta(days=1)  # 🔥 decrease by 1 day
                vnext_cursor = dt.isoformat()
            except Exception as e:
                print("Failed to modify next_cursor:", e)
        # print("data length", len(response_data))
        latest_per_folder = {}

        for row in response_data:
            folder = row.get("folder_name")
            text_data = row.get("text")
            conv_id = row.get("id")
            if not text_data:
                continue

            try:
                messages_parsed = json.loads(
                    text_data
                )  # Convert JSON string back to list of dicts
            except Exception as e:
                print(f"[WARN] Failed to parse JSON for id {row.get('id')}: {e}")
                continue

            # If it's a list, pick the last message (or you can pick first or max timestamp)
            if isinstance(messages_parsed, list):
                latest_message = messages_parsed[-1]  # last message
            elif isinstance(messages_parsed, dict):
                latest_message = messages_parsed  # single message
            else:
                continue  # unknown format, skip

            try:
                ts = datetime.fromisoformat(
                    latest_message["timestamp"].replace("Z", "+00:00")
                )
            except Exception:
                continue

            # try:
            #     ts = datetime.fromisoformat(
            #         messages["timestamp"].replace("Z", "+00:00")
            #     )
            # except Exception:
            #     continue

            # keep the latest per folder - fix timezone comparison issue
            if folder not in latest_per_folder:
                should_update = True
            else:
                existing_ts = latest_per_folder[folder]["ts"]
                # Ensure both timestamps have timezone info for comparison
                if existing_ts.tzinfo is None:
                    # Make existing timestamp timezone-aware (assume UTC)
                    existing_ts = existing_ts.replace(tzinfo=timezone.utc)
                elif ts.tzinfo is None:
                    # Make new timestamp timezone-aware (assume UTC)
                    ts = ts.replace(tzinfo=timezone.utc)
                should_update = ts > existing_ts

            if should_update:
                latest_per_folder[folder] = {
                    "ts": ts,
                    "conv_id": conv_id,
                    "latest_message": latest_message,
                }

        # Drop helper ts field
        # print("return data lenght from lance", len(latest_per_folder))
        return latest_per_folder, vnext_cursor

    def get_selected_conv_from_lance(
        self, user_id, client_id
    ):  # used for getting recent messages
        print("checking client id", client_id)
        response_data = self.lance_service.filter_umail_table(
            user_id=user_id, folder_name=client_id
        )
        print("response from selected conv", type(response_data))

        # Get results from API response

        if not response_data:
            print("response_data is empty")
            return None

        selected_conv = {}

        for row in response_data:
            text_data = row.get("text")
            conv_id = row.get("id")

            if not text_data:
                print("NO TEXT DATA")
                continue

            try:
                if isinstance(text_data, (dict, list)):
                    parsed_data = text_data
                elif isinstance(text_data, str):
                    parsed_data = json.loads(text_data)
                else:
                    print(
                        f"[WARN] Unexpected text type for id {conv_id}: {type(text_data)}"
                    )
                    continue
            except Exception as e:
                print(f"[WARN] Failed to parse JSON for id {conv_id}: {e}")
                continue

            # Handle both cases: single message (dict) or multiple messages (list)
            if isinstance(parsed_data, list):
                # If it's a list, process each message
                for message in parsed_data:
                    conversation_id = message.get("conversation_id")

                    if not conversation_id:
                        print(f"[WARN] No conversation_id found for id {conv_id}")
                        continue

                    if conversation_id not in selected_conv:
                        selected_conv[conversation_id] = []

                    # Directly append the message to the conversation_id list
                    selected_conv[conversation_id].append(message)

            elif isinstance(parsed_data, dict):
                # If it's a single dict, process it directly
                conversation_id = parsed_data.get("conversation_id")

                if not conversation_id:
                    print(f"[WARN] No conversation_id found for id {conv_id}")
                    continue

                if conversation_id not in selected_conv:
                    selected_conv[conversation_id] = []

                # Directly append the message to the conversation_id list
                selected_conv[conversation_id].append(parsed_data)

            else:
                print(
                    f"[WARN] Unexpected data type for id {conv_id}: {type(parsed_data)}"
                )
                continue

        return selected_conv

    def flatten_json(self, obj, parent_key: str = ""):
        items = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                new_key = f"{parent_key}.{k}" if parent_key else k
                items.extend(self.flatten_json(v, new_key))
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                new_key = f"{parent_key}[{i}]"
                items.extend(self.flatten_json(v, new_key))
        else:
            text = f"{parent_key}: {obj}"
            items.append(text)

        return " ".join(items)

    def process_json_files(self, folder_path):
        """
        Reads all JSON files in a folder, flattens each JSON into a list of "key: value" strings,
        and returns a combined list of all flattened strings.
        """
        results = []
        print("processing folderpath", folder_path)

        for filename in os.listdir(folder_path):
            if not filename.lower().endswith(".json"):
                continue

            file_path = os.path.join(folder_path, filename)
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                print(f"[!] Failed to read JSON file {filename}: {e}")
                logger.error(f"[!] Failed to read JSON file {filename}: {e}")
                continue

            parts = filename.split(":")
            user_id = parts[0]
            client_id = parts[1]
            conv_id = os.path.splitext(parts[2])[0]

            def make_row(d):
                flattened_texts = self.flatten_json(d)
                return {
                    "user_id": user_id,
                    "client_id": client_id,
                    "conv_id": conv_id,
                    "flattened_texts": flattened_texts,
                    "original_data": d,
                    "timestamp": d.get("timestamp"),
                    "plain_text": d.get("plain_text", ""),
                }

            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        results.append(make_row(item))
            elif isinstance(data, dict):
                results.append(make_row(data))

        return results

    def process_json_files_for_reply(
        self, lance_data, user_id, client_id, conversation_id
    ):
        """
        Reads all JSON files in a folder, flattens each JSON into a list of "key: value" strings,
        and returns a combined list of all flattened strings.
        """
        flattened_texts = self.flatten_json(lance_data)
        # print(f"lance_data : {lance_data}")
        timestamp = lance_data[-1].get("timestamp")
        pl_text = lance_data[-1].get("plain_text")
        result = {
            "user_id": user_id,
            "client_id": client_id,
            "conv_id": conversation_id,
            "flattened_texts": flattened_texts,
            "original_data": lance_data,
            "timestamp": timestamp,
            "plain_text": pl_text,
        }
        print(f"process_json_files complete with timestamp : {timestamp}")
        return result

    # first fucntion

    def embed_json_files(self, folder_path):
        data = self.process_json_files(folder_path)

        vector_batch = []
        all_text_lengths = []

        for file in data:
            page_content = file.get("flattened_texts", "").strip()
            if page_content:
                all_text_lengths.append(len(page_content))

        # Calculate dynamic chunk size based on content
        if all_text_lengths:
            avg_length = sum(all_text_lengths) // len(all_text_lengths)
            # Heuristic: Clamp between reasonable limits for embeddings
            # Considering token limits, aim for smaller chunks
            dynamic_chunk_size = max(2000, min(self.dimension, avg_length))
        else:
            dynamic_chunk_size = self.dimension

        # Initialize the splitter
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=dynamic_chunk_size,
            chunk_overlap=int(dynamic_chunk_size * 0.2),  # 20% overlap
            length_function=len,
            separators=["\n\n", "\n", " ", ""],  # Try these separators in order
        )

        logger.info(
            f"[📄] Using dynamic chunk size: {dynamic_chunk_size} with overlap: {int(dynamic_chunk_size * 0.2)}"
        )

        for file in data:
            user_id = file.get("user_id")
            client_id = file.get("client_id")
            conv_id = file.get("conv_id")
            page_content = file.get("flattened_texts", "").strip()
            original_data = file.get("original_data")
            timestamp = file.get("timestamp")

            if not page_content:
                continue

            try:
                # Create Document object for the splitter
                document = Document(
                    page_content=page_content,
                    metadata={
                        "user_id": user_id,
                        "client_id": client_id,
                        "conv_id": conv_id,
                        "original_data": original_data,
                        "timestamp": timestamp,
                    },
                )

                # Split the document into chunks
                chunks = splitter.split_documents([document])

                logger.info(f"[📝] Split document {conv_id} into {len(chunks)} chunks")

                # Process each chunk
                for i, chunk in enumerate(chunks):
                    chunk_text = chunk.page_content.strip()
                    if not chunk_text:
                        continue

                    # Generate embedding for the chunk
                    vector = self.embeddings.embed_query(chunk_text)

                    # Create unique ID for each chunk
                    # chunk_id = f"{conv_id}_chunk_{i}" if len(chunks) > 1 else conv_id
                    # print(f"creating vector for {chunk_id}")
                    vector_data = UmailData(
                        id=conv_id,  # sometimes multple files can have same id because of splitting into chunks
                        user_id=user_id,
                        text=json.dumps(original_data, ensure_ascii=False),
                        embedding=vector,
                        folder_name=client_id,
                        timestamp=timestamp,
                    )
                    print(f"{conv_id} : {user_id} : {client_id} : {timestamp}")
                    vector_batch.append(vector_data)
            except Exception as e:
                print(f"[!] Embedding failed: {e}")
                logger.error(f"[!] Embedding failed: {e}")

        if vector_batch:
            print("embedding complte. sending to lance db for insertion")
            self.send_json_batch_to_lancedb(vector_batch)
            return {"vectors_made": len(vector_batch), "docs_processed": len(data)}
        else:
            return {"vectors_made": 0, "docs_processed": len(data)}

    def embed_both_json_and_plain(self, folder_path, batch_size=100):
        print(f"inside embed_both_json_and_plain")
        data = self.process_json_files(folder_path)

        vector_batch = []
        batch_index = 1

        dynamic_chunk_size = 4000
        json_splitter = RecursiveCharacterTextSplitter(
            chunk_size=dynamic_chunk_size,
            chunk_overlap=int(dynamic_chunk_size * 0.2),  # 20% overlap
            length_function=len,
            separators=["\n\n", "\n", " ", ""],  # Try these separators in order
        )

        plain_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1500,
            chunk_overlap=150,
            length_function=len,
            separators=["\n\n", "\n", " ", ""],
        )

        for f in data:
            user_id = f["user_id"]
            client_id = f["client_id"]
            conv_id = f["conv_id"]
            flattened = f["flattened_texts"].strip()
            original_data = f["original_data"]
            timestamp = f["timestamp"]
            plain_text = f.get("plain_text", "")

            # ---- JSON embedding ----
            json_chunks = json_splitter.split_text(flattened) if flattened else []
            json_vectors = (
                [self.embeddings.embed_query(c) for c in json_chunks]
                if json_chunks
                else []
            )
            merged_json_embedding = (
                np.mean(json_vectors, axis=0).tolist() if json_vectors else []
            )

            # ---- Plain embedding ----
            plain_chunks = plain_splitter.split_text(plain_text) if plain_text else []
            plain_vectors = (
                [self.embeddings.embed_query(c) for c in plain_chunks]
                if plain_chunks
                else []
            )
            merged_plain_embedding = (
                np.mean(plain_vectors, axis=0).tolist() if plain_vectors else []
            )

            # ---- Build row ----
            row = UmailData(
                id=conv_id,
                user_id=user_id,
                text=json.dumps(original_data, ensure_ascii=False),
                embedding=merged_json_embedding,
                folder_name=client_id,
                timestamp=timestamp,
                plain_text_embedding=merged_plain_embedding,
            )

            vector_batch.append(row)

            # ---- When batch is full → insert ----
            if len(vector_batch) >= batch_size:
                print(
                    f"🟦 Inserting batch #{batch_index} ({len(vector_batch)} vectors)"
                )
                self.send_json_batch_to_lancedb(vector_batch)  # your batched insert
                vector_batch = []  # clear batch
                batch_index += 1

        # Insert remaining items (if < batch_size)
        if vector_batch:
            print(
                f"🟩 Inserting final batch #{batch_index} ({len(vector_batch)} vectors)"
            )
            self.send_json_batch_to_lancedb(vector_batch)

        return {
            "total_rows": len(data),
            "batches_inserted": batch_index,
        }

    def embed_json_file_for_reply(
        self, lance_data, user_id, client_id, conversation_id
    ):
        file = self.process_json_files_for_reply(
            lance_data, user_id, client_id, conversation_id
        )

        print("🔍 embed_json_file_for_reply called:")
        print(f"   user_id: {user_id}")
        print(f"   client_id: {client_id}")
        print(f"   conversation_id: {conversation_id}")
        print(f"   input_data type: {type(lance_data)}")

        # Extract data
        flattened = file.get("flattened_texts", "").strip()
        plain_text = file.get("plain_text", "")
        original_data = file.get("original_data")
        timestamp = file.get("timestamp")
        conv_id = file.get("conv_id")
        user_id = file.get("user_id")
        client_id = file.get("client_id")

        # -------------------------------
        # Dynamic chunk sizing
        # -------------------------------
        if flattened:
            avg_length = len(flattened)
            dynamic_chunk_size = max(2000, min(self.dimension, avg_length))
        else:
            dynamic_chunk_size = 4000

        logger.info(
            f"[📄 reply-embed] Using dynamic chunk size: {dynamic_chunk_size} "
            f"with overlap: {int(dynamic_chunk_size * 0.2)}"
        )

        # JSON splitter
        json_splitter = RecursiveCharacterTextSplitter(
            chunk_size=dynamic_chunk_size,
            chunk_overlap=int(dynamic_chunk_size * 0.2),
            length_function=len,
            separators=["\n\n", "\n", " ", ""],
        )

        # Plain text splitter
        plain_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1500,
            chunk_overlap=150,
            length_function=len,
            separators=["\n\n", "\n", " ", ""],
        )

        # -------------------------------
        # JSON embedding (flattened)
        # -------------------------------
        json_chunks = json_splitter.split_text(flattened) if flattened else []
        json_vectors = (
            [self.embeddings.embed_query(c) for c in json_chunks] if json_chunks else []
        )

        merged_json_embedding = (
            np.mean(json_vectors, axis=0).tolist() if json_vectors else []
        )

        # -------------------------------
        # Plain text embedding
        # -------------------------------
        plain_chunks = plain_splitter.split_text(plain_text) if plain_text else []
        plain_vectors = (
            [self.embeddings.embed_query(c) for c in plain_chunks]
            if plain_chunks
            else []
        )

        merged_plain_embedding = (
            np.mean(plain_vectors, axis=0).tolist() if plain_vectors else []
        )

        # -------------------------------
        # Build row (ONE row per conversation)
        # -------------------------------
        row = UmailData(
            id=conv_id,
            user_id=user_id,
            text=json.dumps(original_data, ensure_ascii=False),
            embedding=merged_json_embedding,
            folder_name=client_id,
            timestamp=timestamp,
            plain_text_embedding=merged_plain_embedding,
        )

        # -------------------------------
        # Insert to LanceDB
        # -------------------------------
        try:
            self.send_json_batch_to_lancedb_for_reply([row])
            logger.info(
                f"✅ reply embedding inserted into LanceDB for conv_id={conv_id}"
            )
            return {"status": "success", "conv_id": conv_id}
        except Exception as e:
            logger.error(f"❌ Failed inserting reply embedding for {conv_id}: {e}")
            return {"status": "failed", "error": str(e)}

    def send_json_batch_to_lancedb_for_reply(self, vector_batch):
        """
        Send vector data to LanceDB for insertion
        """
        try:
            response = self.lance_service.insert_umail_vectors_for_reply(vector_batch)
            return response

        except Exception as e:
            print(f"[!] Exception during batch insert: {str(e)}")
            logger.error(f"[!] Exception during batch insert: {str(e)}")

            # Retry logic
            # print("⚠️ Initial insert failed, retrying in 2 seconds...")
            time.sleep(2)
            try:
                response = self.lance_service.insert_umail_vectors_for_reply(
                    vector_batch
                )
                return response
            except Exception as retry_e:
                print(f"[!] Retry exception: {str(retry_e)}")

            return None

    def send_json_batch_to_lancedb(self, vector_batch, batch_size=50):
        total = len(vector_batch)
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
                f"→ Sending batch {start // batch_size + 1} " f"({len(batch)} items)"
            )

            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    response = self.lance_service.insert_umail_vectors(batch)
                    results.append(safe_json(response))
                    break

                except Exception as e:
                    logger.error(f"[Attempt {attempt}] Failed batch {start}: {e}")
                    if attempt == MAX_ATTEMPTS:
                        raise

                    sleep_time = BACKOFF**attempt
                    time.sleep(sleep_time)

        return results

    def print_content(self, user_id):

        try:

            response = requests.post(
                f"{self.lancedb_url}/show_table",
                params={"user_id": user_id},
            )
            if response.status_code == 200:
                if response.text.strip():  # Check if response has content
                    try:
                        response_data = response.json()
                        print(f"response_data : {response_data}")
                        # Process your JSON data here
                    except ValueError as e:
                        print(f"Response is not valid JSON: {response.text}")
                        print(f"JSON decode error: {e}")
                else:
                    print("Response is empty")
            else:
                print(f"HTTP Error: {response.status_code}")
                print(f"Response text: {response.text}")

        except requests.exceptions.RequestException as e:
            print(f"Request failed: {e}")

    def search_email_from_lance(
        self, folder_names, user_id, text_input, semantic_condition=None
    ):
        try:
            embeddings = self.embeddings.embed_query(text_input)
            payload = {
                "user_id": user_id,
                "folder_names": folder_names,
                "embeddings": embeddings,
                "semantic_condition": semantic_condition,
            }
            response = requests.post(
                f"{self.lancedb_url}/find_email_from_query",
                json=payload,
            )

            if response.status_code == 200:
                result_from_lance = response.json().get("results", [])
                # print("successfully fetched the results")
                # print(f"result_from_lance : {result_from_lance}")
                return result_from_lance
            else:
                print(f"HTTP Error: {response.status_code}")
                print(f"Response text: {response.text}")
                return []

        except Exception as e:
            print(f"Error in search_email_from_lance: {e}")
            return []

    # ------------FETCHING CONV FILE FOR AI ASSISSTANT--------#

    def get_conv_from_lance(self, id, user_id, folder_name):
        try:
            # print("inside  get_conv_from_lance")

            payload = {
                "id": id,
                "user_id": user_id,
                "folder_name": folder_name,
            }
            # print("payload", payload)
            response = requests.post(
                f"{self.lancedb_url}/fetch_conv_file",
                params=payload,
            )

            if response.status_code == 200:
                # print("successfully fetched the results")
                return response.json().get("results", [])
            else:
                print(f"HTTP Error: {response.status_code}")
                print(f"Response text: {response.text}")
                return []

        except Exception as e:
            print(f"Error in search_email_from_lance: {e}")
            return []

    # ---------------------Creating index for umail----------------#

    def creating_index(self):
        try:
            # print("inside  creating_index")

            response = requests.post(f"{self.lancedb_url}/create_index")

            if response.status_code == 200:
                # print("successfully created index for umail table")
                return response.json()
            else:
                print(f"HTTP Error: {response.status_code}")
                print(f"Response text: {response.text}")
                return []

        except Exception as e:
            print(f"Error in creating_index: {e}")
            return []
