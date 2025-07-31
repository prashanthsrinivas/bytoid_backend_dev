import os
import re
import yaml
from dotenv import load_dotenv
from fireworks.client import Fireworks

load_dotenv()


FIREWORKS_KEY = os.getenv("FIREWORKS_KEY")
FIREWORKS_MODEL = os.getenv("FIREWORKS_MODEL")

fw = Fireworks(api_key=FIREWORKS_KEY)


def get_fireworks_response(user_message: str, role: str) -> str:
    chat = fw.chat.completions.create(
        model=FIREWORKS_MODEL,
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
        llama_response = get_fireworks_response(full_prompt, role="user")

        # return yaml.safe_load(llama_response)
        return llama_response
    except Exception as e:
        print(f"🔥 LLaMA Evaluator context Error: {e}")
        return []
