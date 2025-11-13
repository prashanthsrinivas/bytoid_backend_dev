import os
import re
import yaml
from dotenv import load_dotenv
from fireworks.client import Fireworks
from langchain_fireworks import FireworksEmbeddings
import json

load_dotenv()


FIREWORKS_KEY = os.getenv("FIREWORKS_KEY")
FIREWORKS_MODEL = os.getenv("FIREWORKS_MODEL")
EMBEDMODEL = os.getenv("EMBED_MODEL")
EVAL_FIREWORKS = os.getenv("FIREWORKS_MODEL_EVAL")
fw = Fireworks(api_key=FIREWORKS_KEY)


def get_fireworks_response(user_message: str, role: str) -> str:
    chat = fw.chat.completions.create(
        model=FIREWORKS_MODEL,
        messages=[{"role": role, "content": user_message}],
        temperature=0.7,
    )
    return chat.choices[0].message.content.strip()


def get_fireworks_response2(user_message: str, role: str, temp: float = 0.7) -> str:
    chat = fw.chat.completions.create(
        model=FIREWORKS_MODEL,
        messages=[{"role": role, "content": user_message}],
        temperature=temp,
    )
    return chat.choices[0].message.content.strip()


def get_fireworks_response3(user_message: str, role: str, temp: float = 0.7) -> str:
    chat = fw.chat.completions.create(
        model="accounts/fireworks/models/deepseek-v3p1-terminus",
        messages=[{"role": role, "content": user_message}],
        temperature=temp,
    )
    return chat.choices[0].message.content.strip()


def get_firework_embedding():
    embeddings = FireworksEmbeddings(
        model=EMBEDMODEL, api_key=FIREWORKS_KEY, dimensions=3072
    )
    return embeddings


def get_evaluator_fireworks(user_message: str, role: str) -> str:
    chat = fw.chat.completions.create(
        model=EVAL_FIREWORKS,
        messages=[{"role": role, "content": user_message}],
        temperature=0.7,
    )
    return chat.choices[0].message.content.strip()


def evaluator_llama(prompt_template_str, query, context, industry):
    # 🔧 Format prompt
    full_prompt = prompt_template_str.format(
        user=query, response=context, industry=industry
    )

    try:
        llama_response = get_fireworks_response(full_prompt, role="user")
        print(f"🔥 Raw LLaMA Evaluator Response:\n{llama_response}\n")

        # 🔍 Parse the returned JSON from the model's output
        match = re.search(r"\{.*\}", llama_response, re.DOTALL)
        if not match:
            raise ValueError("Could not extract JSON object from model response")

        result_obj = yaml.safe_load(match.group(0))
        return result_obj

    except Exception as e:
        print(f"🔥 LLaMA Evaluator Error: {e}")
        return {
            "is_valid": False,
            "reason": "Model output could not be parsed",
            "refined_response": "",
        }


def evaluator_batch_llama(prompt_template_str, qa_list, industry):
    qa_input_block = "\n".join(
        [
            f"{i+1}.\nUser Question: {item['query']}\nAI Response: {item['response_text']}"
            for i, item in enumerate(qa_list)
        ]
    )

    full_prompt = prompt_template_str.format(qa_list=qa_input_block, industry=industry)

    try:
        llama_response = get_fireworks_response(full_prompt, role="user")

        # return yaml.safe_load(llama_response)
        return llama_response
    except Exception as e:
        print(f"🔥 LLaMA Evaluator batch Error: {e}")
        return []


def evaluator_context_llama(prompt_template_str, qa_list):
    if not prompt_template_str:
        print("❌ Error: Prompt template is missing.")
        return []
    qa_input_block = "\n".join(
        [
            f"{i+1}.\nUser Question: {item['query']}\nAI Response: {item['response_text']}"
            for i, item in enumerate(qa_list)
        ]
    )

    full_prompt = prompt_template_str.format(qa_list=qa_input_block)

    try:
        llama_response = get_evaluator_fireworks(full_prompt, role="system")

        # return yaml.safe_load(llama_response)
        return llama_response
    except Exception as e:
        print(f"🔥 LLaMA Evaluator context Error: {e}")
        return []


def enforce_json_keys(data: dict) -> dict:
    """Ensure output always has summary, clean_text, clarifications."""
    return {
        "summary": data.get("summary", ""),
        "clean_text": data.get("clean_text", ""),
        "clarifications": data.get(
            "clarifications", [] if isinstance(data.get("clarifications"), list) else []
        ),
    }


def evaluate_transcript(prompt_template_str, text):
    full_prompt = prompt_template_str.format(input_text=text)
    llama_response = get_evaluator_fireworks(full_prompt, role="system")

    cleaned = llama_response.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = cleaned.rstrip("`").strip()

    # try direct parse
    try:
        parsed = json.loads(cleaned)
        return enforce_json_keys(parsed)
    except:
        # try regex extract
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            try:
                parsed = json.loads(match.group(0))
                return enforce_json_keys(parsed)
            except:
                pass
        # fallback dummy output
        return {
            "summary": "Error",
            "clean_text": text,
            "clarifications": ["Model did not return valid JSON."],
        }


# def get_firework_embedding():
#     embeddings = FireworksEmbeddings(
#         model=EMBEDMODEL, api_key=FIREWORKS_KEY, dimensions=3072
#     )

#     # # Single string for embed_query
#     # test_text = "Hello world"
#     # vec = embeddings.embed_query(test_text)  # <- pass a string, not [string]
#     # print("Embedding length:", len(vec))  # Should match dimensions
#     return embeddings


# print("embedding of firework", get_firework_embedding())
