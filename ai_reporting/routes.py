from flask import Flask, request, jsonify, Blueprint, Response
import logging
from create_db import connect_to_rds
import json
from utils.normal import  load_yaml_file
from cust_helpers import pathconfig
from utils.fireworkzz import get_fireworks_response
import re
import yaml




ai_reporting_bp = Blueprint("ai_reporting", __name__)


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
     handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)



def get_schema(cursor):

    try:
        cursor.execute("SELECT table_name, column_name, data_type, column_key, extra FROM information_schema.columns WHERE table_schema='your_database_name'")
        rows = cursor.fetchall()

        schema = {}
        for row in rows:
            table = row['table_name']
            if table not in schema:
                schema[table] = []
            schema[table].append({
                'column_name': row['column_name'],
                'data_type': row['data_type'],
                'column_key': row['column_key'],
                'extra': row['extra']
            })

        schema_json = json.dumps(schema, indent=2)
        print("schema_json: {schema_json}")
        return schema_json
    
    except Exception as e:
        print("⚠️ Error while getting the database schema:", e)
        return None


    

def parse_llm_response(response_text):
    """
    Robustly parse LLM response that might be JSON or YAML,
    possibly wrapped in markdown code fences or preceded by preamble text.

    Args:
        response_text: Raw text from LLM

    Returns:
        dict: Parsed content

    Raises:
        ValueError: If parsing fails after all attempts
    """
    if not response_text or not response_text.strip():
        raise ValueError("Empty response from LLM")

    cleaned = response_text.strip()
    print(f"****cleaned : {cleaned}")

    # Strategy 1: Extract content between code fences
    code_fence_pattern = r"```(?:json|yaml|yml)?\s*\n(.*?)\n```"
    code_fence_match = re.search(code_fence_pattern, cleaned, re.DOTALL)
    if code_fence_match:
        cleaned = code_fence_match.group(1).strip()
    else:
        # Strategy 2: Remove leading markdown code fence markers
        cleaned = re.sub(
            r"^```(?:json|yaml|yml)?\s*\n", "", cleaned, flags=re.MULTILINE
        )
        cleaned = re.sub(r"\n```\s*$", "", cleaned, flags=re.MULTILINE)

    # Strategy 3: Try to find JSON object or YAML content after preamble
    # Look for content starting with { or a YAML key pattern
    json_match = re.search(r"(\{.*\})", cleaned, re.DOTALL)
    yaml_match = re.search(
        r"^([a-zA-Z_][\w]*\s*:.*)", cleaned, re.DOTALL | re.MULTILINE
    )

    # Prepare multiple candidates to try parsing
    candidates = [cleaned]

    if json_match:
        candidates.insert(0, json_match.group(1).strip())

    if yaml_match:
        candidates.insert(0, yaml_match.group(1).strip())

    # Try parsing each candidate
    errors = []

    for candidate in candidates:
        if not candidate:
            continue

        # Try JSON first (faster and more strict)
        try:
            result = json.loads(candidate)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError as e:
            errors.append(f"JSON parse error: {e}")

        # Try YAML (more forgiving)
        try:
            result = yaml.safe_load(candidate)
            if result is None:
                continue
            if isinstance(result, dict):
                return result
        except yaml.YAMLError as e:
            errors.append(f"YAML parse error: {e}")

    # If all attempts failed, raise detailed error
    error_msg = f"Failed to parse LLM response after trying all strategies.\n"
    error_msg += f"Errors encountered:\n" + "\n".join(f"  - {e}" for e in errors)
    error_msg += f"\n\nRaw output (first 500 chars):\n{response_text[:500]}"
    raise ValueError(error_msg)


@ai_reporting_bp.route("/generate_report", methods=["POST"])
def generate_report():
    data = request.get_json()
    query = data.get("query", "").strip()
    user_id = data.get("user_id", "").strip()

    if not query:
        return jsonify({"message": "User query not found"}), 400
    if not user_id:
            return jsonify({"message": "user id not found"}), 400

    conn = connect_to_rds()
    cursor = conn.cursor() 

    # generate the schema    
    schema_json = get_schema(cursor)

    # generate the sql query
    reporting_yaml = load_yaml_file(path=pathconfig.reporting)
    template = reporting_yaml.get("report_generation")
    filled_prompt = (
            template.replace("{{user_query}}", str(query))
            .replace("{{database_schema}}", str(schema_json))
            )
    modified_yaml = get_fireworks_response(filled_prompt, role="system")

    try:
            result = parse_llm_response(modified_yaml)
    except ValueError as e:
            print(f"🔥 report_generation parsing failed: {e}")
            return jsonify({"error": "Failed to parse report_generation response"}), 500
    mysql_query = result.get("mysql_query")
    suggested_chart = result.get("suggested_chart")

    # execute the query
    