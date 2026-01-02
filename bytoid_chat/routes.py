from flask import Blueprint, request, jsonify, session

# Initialize blueprint and logger
bytoid_chat_bps = Blueprint("bytoid_chat", __name__)
logger = get_logger(__name__)
load_dotenv()

@bytoid_chat_bp.route("/chat")
def chat():
    import requests
import json

url = "https://api.fireworks.ai/inference/v1/chat/completions"
payload = {
  "model": "accounts/fireworks/models/qwen3-vl-235b-a22b-thinking",
  "max_tokens": 32768,
  "top_p": 1,
  "top_k": 40,
  "presence_penalty": 0,
  "frequency_penalty": 0,
  "temperature": 0.6,
  "messages": [
    {
      "role": "user",
      "content": [
        {
          "type": "text",
          "text": "Can you describe this image?"
        },
        {
          "type": "image_url",
          "image_url": {
            "url": "https://images.unsplash.com/photo-1582538885592-e70a5d7ab3d3?ixlib=rb-4.0.3&ixid=M3wxMjA3fDB8MHxwaG90by1wYWdlfHx8fGVufDB8fHx8fA%3D%3D&auto=format&fit=crop&w=1770&q=80"
          }
        }
      ]
    }
  ]
}
headers = {
  "Accept": "application/json",
  "Content-Type": "application/json",
  "Authorization": "Bearer <API_KEY>"
}
requests.request("POST", url, headers=headers, data=json.dumps(payload))
