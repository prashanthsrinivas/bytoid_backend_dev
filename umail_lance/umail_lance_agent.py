from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from dotenv import load_dotenv
import os
import requests
import os
import json
import logging
from typing import Any, List
from pydantic import BaseModel
from datetime import datetime  # from werkzeug.exceptions import HTTPException
import numpy as np
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.schema import Document
import time

# from sentence_transformers import SentenceTransformer
import base64

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


class UmailLanceClient:
    def __init__(self, user_id: str):
        self.lancedb_url = lancedb_url
        self.user_id = user_id
        self.dimension = 3072
        self.embeddings = OpenAIEmbeddings(
            model="text-embedding-3-large",
            openai_api_key=openai_api_key,
            dimensions=self.dimension,
        )

    def get_records_from_lance(self, user_id: str, client_id: str):
        """
        Fetch all records stored under a specific user_id/client_id table.
        """
        try:
            print(
                f"inside get_all_records for user_id : {user_id} client_id :{client_id}"
            )
            # Safely fetch or create table
            response = requests.post(
                f"{self.lancedb_url}/filter_umail_table",
                params={"user_id": user_id, "folder_name": client_id},
            )

            # Convert Arrow Table → Python list of dicts
            if response.status_code != 200:
                raise Exception(
                    f"API call failed: {response.status_code} - {response.text}"
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

    # def latest_messages_from_lance(self,user_id):   # used for getting recent messages

    #     response = requests.post(
    #                 f"{self.lancedb_url}/get_umail_table",
    #                 params={"user_id": user_id},
    #             )

    #     if response.status_code != 200:
    #             raise Exception(f"API call failed: {response.status_code} - {response.text}")

    #         # Get results from API response

    #     response_data = response.json()
    #     latest_per_folder = {}

    #     for row in response_data:
    #         folder = row.get("folder_name")
    #         text_data = row.get("text")
    #         conv_id = row.get("id")

    #         # print(f"folder : {folder}")
    #         # print(f"text_data : {text_data}")
    #         # print(f"conv_id : {conv_id}")

    #         if not text_data:
    #             continue

    #         try:
    #             messages = json.loads(text_data)  # Convert JSON string back to list of dicts
    #         except Exception as e:
    #             print(f"[WARN] Failed to parse JSON for id {row.get('id')}: {e}")
    #             continue

    #         try:
    #                 ts = datetime.fromisoformat(messages["timestamp"].replace("Z", "+00:00"))
    #         except Exception:
    #                 continue

    #             # keep the latest per folder
    #         if (folder not in latest_per_folder) or (ts > latest_per_folder[folder]["ts"]):
    #                 latest_per_folder[folder] = {
    #                     "ts" : ts,
    #                     "conv_id":conv_id,
    #                     "latest_message": messages
    #                 }
    #     # Drop helper ts field
    #     return latest_per_folder

    def latest_messages_from_lance(
        self, user_id, next_cursor
    ):  # used for getting recent messages
        print(f"latest_messages_from_lance")
        response = requests.post(
            f"{self.lancedb_url}/get_umail_table",
            params={"user_id": user_id, "next_cursor": next_cursor},
        )

        if response.status_code != 200:
            raise Exception(
                f"API call failed: {response.status_code} - {response.text}"
            )

        # Get results from API response

        response_data, next_cursor = response.json()
        print("data length", len(response_data))
        latest_per_folder = {}

        for row in response_data:
            folder = row.get("folder_name")
            text_data = row.get("text")
            conv_id = row.get("id")
            if not text_data:
                continue

            try:
                messages = json.loads(
                    text_data
                )  # Convert JSON string back to list of dicts
            except Exception as e:
                print(f"[WARN] Failed to parse JSON for id {row.get('id')}: {e}")
                continue

            try:
                ts = datetime.fromisoformat(
                    messages["timestamp"].replace("Z", "+00:00")
                )
            except Exception:
                continue

            # keep the latest per folder
            if (folder not in latest_per_folder) or (
                ts > latest_per_folder[folder]["ts"]
            ):
                latest_per_folder[folder] = {
                    "ts": ts,
                    "conv_id": conv_id,
                    "latest_message": messages,
                }

        # Drop helper ts field
        print("return data lenght from lance", len(latest_per_folder))
        return latest_per_folder, next_cursor

    def get_selected_conv_from_lance(
        self, user_id, client_id
    ):  # used for getting recent messages
        print("calling /filter_umail_table", user_id, client_id)
        response = requests.post(
            f"{self.lancedb_url}/filter_umail_table",
            params={"user_id": user_id, "folder_name": client_id},
        )
        print("got response from /filter_umail_table")
        if response.status_code != 200:
            raise Exception(
                f"API call failed: {response.status_code} - {response.text}"
            )

        # Get results from API response
        response_data = response.json()

        if not response_data:
            print("response_data is empty")
            return None

        selected_conv = {}

        for row in response_data:
            text_data = row.get("text")
            conv_id = row.get("id")

            if not text_data:
                continue

            try:
                parsed_data = json.loads(
                    text_data
                )  # Convert JSON string back to dict/list
            except Exception as e:
                print(f"[WARN] Failed to parse JSON for id {row.get('id')}: {e}")
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

        # Loop over all files in the folder
        for filename in os.listdir(folder_path):
            if not filename.lower().endswith(".json"):
                continue  # skip non-JSON files

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
            conv_id = os.path.splitext(parts[2])[0]  # remove ".json"

            flattened_texts = self.flatten_json(data)
            timestamp_msg = data.get("timestamp")
            result = {  # result is a single conversation file
                "user_id": user_id,
                "client_id": client_id,
                "conv_id": conv_id,
                "flattened_texts": flattened_texts,
                "original_data": data,
                "timestamp": timestamp_msg,
            }
            results.append(
                result
            )  # results now contain all the conversion files appended to it
        return results

        #  for reply sending

    def process_json_files_for_reply(
        self, lance_data, user_id, client_id, conversation_id
    ):
        """
        Reads all JSON files in a folder, flattens each JSON into a list of "key: value" strings,
        and returns a combined list of all flattened strings.
        """
        flattened_texts = self.flatten_json(lance_data)
        print(f"lance_data : {lance_data}")
        timestamp = lance_data[-1].get("timestamp")
        result = {
            "user_id": user_id,
            "client_id": client_id,
            "conv_id": conversation_id,
            "flattened_texts": flattened_texts,
            "original_data": lance_data,
            "timestamp": timestamp,
        }
        print("process_json_files complete with timestamp : {timestamp}")
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
            dynamic_chunk_size = max(2000, min(8000, avg_length))
        else:
            dynamic_chunk_size = 4000

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

    def embed_json_file_for_reply(
        self, lance_data, user_id, client_id, conversation_id
    ):
        file = self.process_json_files_for_reply(
            lance_data, user_id, client_id, conversation_id
        )

        all_text_lengths = []

        page_content = file.get("flattened_texts", "").strip()
        if page_content:
            all_text_lengths.append(len(page_content))

        # Calculate dynamic chunk size based on content
        if all_text_lengths:
            avg_length = sum(all_text_lengths) // len(all_text_lengths)
            # Heuristic: Clamp between reasonable limits for embeddings
            # Considering token limits, aim for smaller chunks
            dynamic_chunk_size = max(2000, min(8000, avg_length))
        else:
            dynamic_chunk_size = 4000

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

        user_id = file.get("user_id")
        client_id = file.get("client_id")
        conv_id = file.get("conv_id")
        page_content = file.get("flattened_texts", "").strip()
        original_data = file.get("original_data")
        timestamp = file.get("timestamp")

        if not page_content:
            return

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

        except Exception as e:
            print(f"[!] Embedding failed: {e}")
            logger.error(f"[!] Embedding failed: {e}")

        if vector_data:
            print("embedding complte. sending to lance db for insertion")
            self.send_json_batch_to_lancedb_for_reply(vector_data)
            return {"embedding successful for {orginal_data}"}
        else:
            return {"vectors_made": 0}

    def send_json_batch_to_lancedb_for_reply(self, vector_data):
        """
        Send vector data to LanceDB for insertion
        """
        # Convert UmailData object to dict if needed
        if hasattr(vector_data, "dict") and callable(getattr(vector_data, "dict")):
            payload = vector_data.dict()
        else:
            payload = vector_data

        try:
            print("sending to insert_umail_vectors_for_reply")

            # Make sure the URL is correct
            url = f"{self.lancedb_url}/insert_umail_vectors_for_reply"
            print(f"[DEBUG] Full URL: {url}")

            response = requests.post(url, json=payload)

            print(f"[DEBUG] Response status: {response.status_code}")
            print(f"[DEBUG] Response text: {response.text}")

            if response.status_code == 200:
                print(f"[✔] Inserted reply vectors.")
                logger.info(f"[✔] Inserted reply vectors.")
                return response.json()
            else:
                print(
                    f"[✘] Batch insert failed: {response.status_code} - {response.text}"
                )
                logger.error(
                    f"[✘] Batch insert failed: {response.status_code} - {response.text}"
                )
                return None

        except requests.exceptions.ConnectionError as e:
            print(f"[!] Connection error: {str(e)}")
            logger.error(f"[!] Connection error: {str(e)}")
            return None
        except requests.exceptions.RequestException as e:
            print(f"[!] Request exception: {str(e)}")
            logger.error(f"[!] Request exception: {str(e)}")
            return None
        except Exception as e:
            print(f"[!] Exception during batch insert: {str(e)}")
            logger.error(f"[!] Exception during batch insert: {str(e)}")
            return None

    def send_json_batch_to_lancedb(self, vector_batch):
        payload = [vec.dict() for vec in vector_batch]

        try:
            print("sending to insert_umail_vectors ")
            response = requests.post(
                f"{self.lancedb_url}/insert_umail_vectors", json=payload
            )
            if response.status_code == 200:
                print(f"[✔] Inserted {len(payload)} vectors.")
                logger.info(f"[✔] Inserted {len(payload)} vectors.")
            else:
                print(
                    f"[✘] Batch insert failed: {response.status_code} - {response.text}"
                )
                logger.error(
                    f"[✘] Batch insert failed: {response.status_code} - {response.text}"
                )
        except Exception as e:
            print(f"[!] Exception during batch insert: {str(e)}")
            logger.error(f"[!] Exception during batch insert: {str(e)}")

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

    def call_ticket_number(self, user_id):
        print(f"calling /get_ticket_number")
        response = requests.post(
            f"{self.lancedb_url}/get_ticket_number",
            params={"user_id": user_id},
        )
        if response.status_code == 200:
            response_data = response.json()
            print(f"response_data: {response_data}")
            ticket_number = response_data[0]["number"]
            return ticket_number
        else:
            print(f"HTTP Error: {response.status_code}")
            print(f"Response text: {response.text}")

    def update_ticket_number(self, user_id, lance_ticket_id):
        print("inside update_ticket_number")
        response = requests.post(
            f"{self.lancedb_url}/update_new_ticket_number",
            params={"user_id": user_id, "ticket_number": lance_ticket_id},
        )
        if response.status_code == 200:
            print("tikcet number successfully updated in table : {lance_ticket_id}")
        else:
            print(f"HTTP Error: {response.status_code}")
            print(f"Response text: {response.text}")
        return

    def search_email_from_lance(
        self, folder_names, user_id, text_input, semantic_condition=None
    ):
        try:
            print("inside  search_email_from_lance")
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
                print("successfully fetched the results")
                return response.json().get("results", [])
            else:
                print(f"HTTP Error: {response.status_code}")
                print(f"Response text: {response.text}")
                return []

        except Exception as e:
            print(f"Error in search_email_from_lance: {e}")
            return []
