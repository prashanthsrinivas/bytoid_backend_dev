from cust_helpers import pathconfig
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain.prompts import ChatPromptTemplate
import json
import re
import logging
from dotenv import load_dotenv
from utils.normal import load_yaml_file
from utils.s3_utils import read_json_from_s3

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def evaluator(prompt, model, query, context, industry):
    llm = ChatOpenAI(model=model, temperature=0.2)
    prompt_template = ChatPromptTemplate.from_template(prompt)
    relavance_checker = prompt_template | llm
    response = relavance_checker.invoke(
        {"user": query, "response": context, "industry": industry}
    )
    return response


def generate_usecases_questions(
    prompt_block, model, usecase, industry, documents_contents
):
    prompt_text = prompt_block["instructions"]

    # Define prompt template with variables
    prompt_template = ChatPromptTemplate.from_template(prompt_text)

    # Initialize language model
    llm = ChatOpenAI(model=model, temperature=0.2)
    chain = prompt_template | llm

    # Inject variables into the prompt
    response = chain.invoke(
        {
            "usecase": usecase,
            "industry": industry,
            "documents_contents": documents_contents,
        }
    )

    # Normalize LLM output into clean question list
    questions = "\n".join(
        [
            line.strip().lstrip("1234567890.- ").strip()
            for line in response.content.strip().split("\n")
            if line.strip()
        ]
    )

    return questions


def generate_usecases_questions_batch(
    prompt_block, model, industry, usecases_with_docs
):
    prompt_text = prompt_block["instructions"]

    # Prepare prompt template
    prompt_template = ChatPromptTemplate.from_template(prompt_text)

    # Initialize LLM
    llm = ChatOpenAI(model=model, temperature=0)
    chain = prompt_template | llm

    # Send batch
    response = chain.invoke(
        {"industry": industry, "usecases_with_docs": usecases_with_docs}
    )
    # logger.info(f"[🔍] Model response: {response}")
    try:
        raw = response.content.strip()

        # Remove triple backticks if present
        if raw.startswith("```json") or raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
        # Clean invalid escape sequences like \e, \i, etc.
        raw = re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", raw)
        questions_json = json.loads(raw)
        return questions_json
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse model response: {e}\nRaw content:\n{raw}")
        raise ValueError("Failed to parse model response as JSON") from e


def reEvaluateinstructionJson():
    file = "4ba5e730.json"
    user_id = "107642411636394027005"

    # Load instruction prompt
    prompt_path = load_yaml_file(path=pathconfig.play_template)

    prompt_text = prompt_path.get("re_evaluate_instruction")

    # Load workflow data from S3
    generated_workflow_json = read_json_from_s3(f"{user_id}/workflow/{file}")

    # Input form metadata (normally from form or metadata file)
    form_metadata = {
        "name": "Promote Signature Dishes on Social Medias",
        "description": "Post daily updates on Instagram and Facebook showcasing one of our signature dishes, including its name, description, and image",
        "trigger_mode": "scheduled",
        "trigger_input": "",
        "communication_mode": "auto",
        "ai_mode": "normal",
        "context_section": "",  # optionally enrich with business logic or user info
    }

    # Format prompt
    prompt_template = ChatPromptTemplate.from_template(prompt_text)
    input_vars = {
        "name": form_metadata["name"],
        "description": form_metadata["description"],
        "trigger_mode": form_metadata["trigger_mode"],
        "trigger_input": form_metadata["trigger_input"],
        "communication_mode": form_metadata["communication_mode"],
        "ai_mode": form_metadata["ai_mode"],
        "context_section": form_metadata.get("context_section", ""),
        "generated_workflow_json": json.dumps(generated_workflow_json, indent=2),
    }

    # Initialize model
    llm = ChatOpenAI(model="gpt-3.5-turbo", temperature=0)
    chain = prompt_template | llm

    # Run chain
    response = chain.invoke(input_vars)

    try:
        raw = response.content.strip()

        # Optional: clean markdown
        if raw.startswith("```json") or raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)

        return {"evaluation_text": raw}

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse model response: {e}\nRaw content:\n{raw}")
        raise ValueError("Failed to parse model response as JSON") from e


# print(reEvaluateinstructionJson())
