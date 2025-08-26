from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from dotenv import load_dotenv
import os
import requests
import os
import json
import logging
from typing import Any, List
from pydantic import BaseModel
from datetime import datetime# from werkzeug.exceptions import HTTPException
import numpy as np
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.schema import Document







logger = logging.getLogger(__name__)

class UmailData(BaseModel):
    id: str
    text: str
    embedding: List[float]
    user_id: str
    folder_name: str


class UmailLanceClient:
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


    def get_records_from_lance(self,user_id: str, client_id: str):
        """
        Fetch all records stored under a specific user_id/client_id table.
        """
        try:
            print(f"inside get_all_records for user_id : {user_id} client_id :{client_id}")
            # Safely fetch or create table
            response = requests.post(
                f"{self.lancedb_url}/filter_umail_table",
                params={"user_id": user_id, "folder_name": client_id},
            )

            # Convert Arrow Table → Python list of dicts
            if response.status_code != 200:
                raise Exception(f"API call failed: {response.status_code} - {response.text}")
            
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



    def latest_messages_from_lance(self,user_id):   # used for getting recent messages

        response = requests.post(
                    f"{self.lancedb_url}/get_umail_table",
                    params={"user_id": user_id},
                )

        if response.status_code != 200:
                raise Exception(f"API call failed: {response.status_code} - {response.text}")
            
            # Get results from API response
        
        response_data = response.json() 
        print(f"response_data : {response_data}")
        # print(f"response_data : {response_data}")
        latest_per_folder = {}

        for row in response_data:
            print(f"row : {row}")
            folder = row.get("folder_name")
            text_data = row.get("text")
            conv_id = row.get("id")
            print(f"text_data : {text_data}")

            if not text_data:
                continue

            try:
                messages = json.loads(text_data)  # Convert JSON string back to list of dicts
                print(f"messages : {messages}")
            except Exception as e:
                print(f"[WARN] Failed to parse JSON for id {row.get('id')}: {e}")
                continue

            for msg in messages:
                try:
                    ts = datetime.fromisoformat(msg["timestamp"].replace("Z", "+00:00"))
                except Exception:
                    continue

                # keep the latest per folder
                if (folder not in latest_per_folder) or (ts > latest_per_folder[folder]["ts"]):
                    latest_per_folder[folder] = {
                        "ts" : ts,
                        "conv_id":conv_id,
                        "latest_message": msg
                    }

        # Drop helper ts field
        return latest_per_folder


    def get_selected_conv_from_lance(self,user_id,client_id):   # used for getting recent messages

        response = requests.post(
                    f"{self.lancedb_url}/filter_umail_table",
                    params={"user_id": user_id, "folder_name":client_id},
                )

        if response.status_code != 200:
                raise Exception(f"API call failed: {response.status_code} - {response.text}")
            
            # Get results from API response
        
        response_data = response.json() 
        selected_conv = {}

        for row in response_data:
            text_data = row.get("text")
            conv_id = row.get("id")

            if not text_data:
                continue

            try:
                messages = json.loads(text_data)  # Convert JSON string back to list of dicts
            except Exception as e:
                print(f"[WARN] Failed to parse JSON for id {row.get('id')}: {e}")
                continue
        
                # keep the latest per folder
               
            selected_conv[client_id] = {
                        "conv_id" : conv_id,
                        "message": messages
                    }

        # Drop helper ts field
        return selected_conv
    
    # Flatten JSON recursively
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


    def process_json_files(self,folder_path):
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
            
            result = {                    # result is a single conversation file
            "user_id": user_id,
            "client_id": client_id,
            "conv_id": conv_id,
            "flattened_texts": flattened_texts,
            "original_data" : data
            }
            results.append(result)       # results now contain all the conversion files appended to it
        print("process_json_files complete")
        return results

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
            separators=["\n\n", "\n", " ", ""]  # Try these separators in order
        )
        
        logger.info(f"[📄] Using dynamic chunk size: {dynamic_chunk_size} with overlap: {int(dynamic_chunk_size * 0.2)}")

        for file in data:
            user_id = file.get("user_id")
            client_id = file.get("client_id")
            conv_id = file.get("conv_id")
            page_content = file.get("flattened_texts", "").strip()
            original_data = file.get("original_data")  
            
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
                        "original_data": original_data
                    }
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
                    chunk_id = f"{conv_id}_chunk_{i}" if len(chunks) > 1 else conv_id
                    print(f"creating vector for {chunk_id}")
                    vector_data = UmailData(
                        id=chunk_id,
                        user_id=user_id,
                        text=json.dumps(original_data, ensure_ascii=False),
                        embedding=vector,
                        folder_name=client_id,
                    )
                    vector_batch.append(vector_data)
            # for file in data:
            #     user_id = file.get("user_id")
            #     client_id = file.get("client_id")
            #     conv_id = file.get("conv_id")
            #     page_content = file.get("flattened_texts")
            #     clean_text = page_content.strip()
            #     orginal_data = file.get("orginal_data")
                

            #     if not clean_text:
            #         continue

            #     try:
            #         vector = self.embeddings.embed_query(clean_text)
            #         vector_data = UmailData(
            #             id= conv_id,
            #             user_id=user_id,
            #             text=json.dumps(orginal_data, ensure_ascii=False), 
            #             embedding=vector,
            #             folder_name=client_id,
            #         )
            #         vector_batch.append(vector_data)
            except Exception as e:
                print(f"[!] Embedding failed: {e}")
                logger.error(f"[!] Embedding failed: {e}")

        if vector_batch:
            print("embedding complte. sending to lance db for insertion")
            self.send_json_batch_to_lancedb(vector_batch)
            return {"vectors_made": len(vector_batch), "docs_processed": len(data)}
        else:
            return {"vectors_made": 0, "docs_processed": len(data)}


    def send_json_batch_to_lancedb(self, vector_batch):
        payload = [vec.dict() for vec in vector_batch]


        try:
            print("sending to insert_umail_vectors ")
            response = requests.post(f"{self.lancedb_url}/insert_umail_vectors", json=payload)
            if response.status_code == 200:
                print(f"[✔] Inserted {len(payload)} vectors.")
                logger.info(f"[✔] Inserted {len(payload)} vectors.")
            else:
                print(f"[✘] Batch insert failed: {response.status_code} - {response.text}")
                logger.error(
                    f"[✘] Batch insert failed: {response.status_code} - {response.text}"
                )
        except Exception as e:
            print(f"[!] Exception during batch insert: {str(e)}")
            logger.error(f"[!] Exception during batch insert: {str(e)}")


    def print_content(self,user_id):

        try:

            response = requests.post(
                        f"{self.lancedb_url}/show_table",
                        params={"user_id": user_id},
                    )
            if response.status_code == 200:
                if response.text.strip():  # Check if response has content
                    try:
                        response_data = response.json()
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