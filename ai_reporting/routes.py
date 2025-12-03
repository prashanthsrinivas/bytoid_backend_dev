from flask import Flask, request, jsonify, Blueprint, Response
import logging
from create_db import connect_to_rds
import json
from utils.normal import load_yaml_file
from cust_helpers import pathconfig
from utils.fireworkzz import get_fireworks_response, get_fireworks_response2
import re
import yaml
import os, json
import pymysql
import spacy
from umail_lance.umail_lance_agent import UmailLanceClient
from datetime import datetime, date

# import sqlparse
# from sqlparse.sql import Identifier, IdentifierList
# from sqlparse.tokens import Name
from ai_reporting.schema_mapping import name_map
from sqlglot import parse_one, exp
import uuid

from datetime import datetime
from ai_reporting.sql_generation.aggregation_sql import (
    ast_to_sql,
    get_chart_axes_aggregation,
)
from ai_reporting.ast_generation.ast_generation_class import ASTGenerator
from ai_reporting.parse_llm import parse_llm_response
from .ast_component_extraction.intention_extraction import QueryIntentionExtractor
from .reporting_helpers.redis_functions import *
from collections import Counter


ai_reporting_bp = Blueprint("ai_reporting", __name__)

nlp = spacy.load("en_core_web_md")


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)
base_dir = os.path.dirname(__file__)

ast_gen = ASTGenerator()


def get_schema(cursor, database_name="bytoid_support_agent"):

    schema = {}
    try:

        # 1. Get basic table/column info
        cursor.execute(
            f"""
            SELECT table_name, column_name, data_type, column_key, extra, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = '{database_name}'
            ORDER BY table_name, ordinal_position
        """
        )
        columns = cursor.fetchall()

        # 2. Get foreign key relationships
        cursor.execute(
            f"""
            SELECT table_name, column_name, referenced_table_name, referenced_column_name
            FROM information_schema.key_column_usage
            WHERE table_schema = '{database_name}' AND referenced_table_name IS NOT NULL
        """
        )
        fks = cursor.fetchall()

        # 3. Get indexes
        cursor.execute(
            f"""
            SELECT table_name, index_name, column_name, non_unique
            FROM information_schema.statistics
            WHERE table_schema = '{database_name}'
        """
        )
        indexes = cursor.fetchall()

        # 4. Organize schema per table
        for col in columns:
            table = col[0]  # table_name
            if table not in schema:
                schema[table] = {"columns": [], "foreign_keys": [], "indexes": []}
            schema[table]["columns"].append(
                {
                    "column_name": col[1],
                    "data_type": col[2],
                    "is_nullable": col[5],
                    "column_key": col[3],
                }
            )

        # 5. Add foreign key info
        for fk in fks:
            table = fk[0]  # table_name
            schema[table]["foreign_keys"].append(
                {
                    "column_name": fk[1],
                    "referenced_table": fk[2],
                    "referenced_column": fk[3],
                }
            )

        # 6. Add indexes info
        for idx in indexes:
            table = idx[0]  # table_name
            schema[table]["indexes"].append(
                {
                    "index_name": idx[1],
                    "column_name": idx[2],
                    "non_unique": bool(idx[3]),
                }
            )

        schema_json = json.dumps(schema, indent=2)
        return schema_json

    except Exception as e:
        # print("⚠️ Error while getting the database schema:", e)
        return None


def replace_obfuscated_names(query: str, mapping: dict) -> str:
    """
    Replace obfuscated table and column names in a MySQL query safely,
    and fix INTERVAL syntax for MySQL.

    Args:
        query (str): SQL query with obfuscated names.
        mapping (dict): Dictionary mapping obfuscated -> real names.

    Returns:
        str: SQL query with names replaced and valid MySQL syntax.
    """

    # Step 1: Temporarily replace %s placeholders to protect them
    safe_query = query.replace("%s", "__PLACEHOLDER__")

    # Step 2: Parse SQL into AST with MySQL dialect
    tree = parse_one(safe_query, dialect="mysql")

    # Step 3: Replace column names
    for column in tree.find_all(exp.Column):
        if column.name in mapping:
            column.set("this", mapping[column.name])
        if column.table and column.table in mapping:
            column.set("table", mapping[column.table])

    # Step 4: Replace table names
    for table in tree.find_all(exp.Table):
        if table.name in mapping:
            table.set("this", mapping[table.name])

    # Step 5: Convert AST back to SQL
    new_query = tree.sql(dialect="mysql")

    # Step 6: Fix any INTERVAL duplication issues
    # Remove duplicate INTERVAL keywords
    new_query = re.sub(
        r"\bINTERVAL\s+INTERVAL\b", "INTERVAL", new_query, flags=re.IGNORECASE
    )

    # Remove duplicate time units (DAY DAY, MONTH MONTH, etc.)
    new_query = re.sub(
        r"\b(DAY|MONTH|YEAR|HOUR|MINUTE|SECOND)\s+\1\b",
        r"\1",
        new_query,
        flags=re.IGNORECASE,
    )

    # Step 7: Restore placeholders
    new_query = new_query.replace("__PLACEHOLDER__", "%s")

    return new_query


async def insert_user_report(new_report, user_id):
    """
    Inserts a new report into the 'reports' JSON column of the users table.

    """
    conn = connect_to_rds()
    cursor = conn.cursor()
    # Convert to JSON string
    new_report_json = json.dumps(new_report)

    try:
        sql = """
            UPDATE users
            SET reports = CASE
                WHEN reports IS NULL THEN JSON_ARRAY(%s)
                ELSE JSON_ARRAY_APPEND(reports, '$', %s)
            END
            WHERE user_id = %s
            """
        cursor.execute(sql, (new_report_json, new_report_json, user_id))
        conn.commit()
    # print("Report inserted successfully.")

    except Exception as e:
        if conn:
            conn.rollback()
        raise e


async def save_draft_report(user_id, new_report):
    """
    Save a draft report for a user in Redis.
    Each report is stored under its own key with a 1-hour TTL.
    """
    client = await GlideClusterClient.create(redis_config_glide)
    report_id = new_report["report_id"]
    key = f"user:{user_id}:draft_report:{report_id}"
    await client.set(key, json.dumps(new_report))
    await client.expire(key, 3600)
    await client.close()


async def get_draft_report(client, user_id, report_id):
    """
    Retrieve a single draft report by its report_id.
    """
    key = f"user:{user_id}:draft_report:{report_id}"
    report_json = await client.get(key)
    if report_json:
        return json.loads(report_json)
    return None


async def delete_draft_report(client, user_id, report_id):
    """
    Delete a specific draft report after finalizing.
    """
    key = f"user:{user_id}:draft_report:{report_id}"
    await client.delete(key)


def search_in_users_table(cursor, name_parts, start_idx, end_idx):
    """
    Search for user in `users` table by:
      - full name,
      - partial name (any part),
      - OR email containing the name.
    """
    results = []
    users = []

    if len(name_parts) == 2:
        first_name, last_name = name_parts

        # 1. Full name or email (with separators or concatenated)
        cursor.execute(
            """
            SELECT user_id, first_name, last_name, email
            FROM users
            WHERE (LOWER(first_name) LIKE LOWER(%s)
                   AND LOWER(last_name) LIKE LOWER(%s))
               OR LOWER(email) LIKE LOWER(%s)
               OR LOWER(email) LIKE LOWER(%s)
               OR LOWER(email) LIKE LOWER(%s)
               OR LOWER(email) LIKE LOWER(%s)
        """,
            (
                first_name + "%",
                last_name + "%",
                f"%{first_name}.{last_name}%",  # john.doe
                f"%{first_name}_{last_name}%",  # john_doe
                f"%{first_name}-{last_name}%",  # john-doe
                f"%{first_name}{last_name}%",  # johndoe
            ),
        )
        users = cursor.fetchall()

        # 2. If full name not found, match either first or last or partial email
        if not users:
            cursor.execute(
                """
                SELECT user_id, first_name, last_name, email
                FROM users
                WHERE LOWER(first_name) LIKE LOWER(%s)
                   OR LOWER(last_name) LIKE LOWER(%s)
                   OR LOWER(email) LIKE LOWER(%s)
                   OR LOWER(email) LIKE LOWER(%s)
            """,
                (
                    first_name + "%",
                    last_name + "%",
                    f"%{first_name}%",
                    f"%{last_name}%",
                ),
            )
            users = cursor.fetchall()

    else:
        # 3. Single or multi-part name: check each part in name or email
        for part in name_parts:
            cursor.execute(
                """
                SELECT user_id, first_name, last_name, email
                FROM users
                WHERE LOWER(first_name) LIKE LOWER(%s)
                   OR LOWER(last_name) LIKE LOWER(%s)
                   OR LOWER(email) LIKE LOWER(%s)
            """,
                (part + "%", part + "%", f"%{part}%"),
            )
            users.extend(cursor.fetchall())

    # ✅ Remove duplicates
    seen_ids = set()
    for usr in users:
        if usr[0] not in seen_ids:
            results.append(
                {
                    "name": f"{usr[1]} {usr[2]}".strip(),
                    "id": usr[0],
                    "email": usr[3],
                    "start_index": start_idx,
                    "end_index": end_idx,
                    "name_type": "user",
                }
            )
            seen_ids.add(usr[0])

    return results


def search_in_users_clients_table(cursor, name_parts, start_idx, end_idx, user_id_fk):
    """
    Search for a customer in `users_clients` table by:
      - full name,
      - partial name (any part),
      - OR email containing the name,
    but only include customers linked to the given user_id_fk in the communication table.
    """
    results = []
    clients = []

    if len(name_parts) == 2:
        first_name, last_name = name_parts

        # 1. Full name or email (joined with communication)
        cursor.execute(
            """
            SELECT DISTINCT uc.users_clients_id, uc.first_name, uc.last_name, uc.email_id
            FROM users_clients uc
            JOIN communication c 
                ON c.users_clients_id_fk = uc.users_clients_id
            WHERE c.user_id_fk = %s
              AND (
                    (LOWER(uc.first_name) LIKE LOWER(%s)
                     AND LOWER(uc.last_name) LIKE LOWER(%s))
                 OR LOWER(uc.email_id) LIKE LOWER(%s)
                 OR LOWER(uc.email_id) LIKE LOWER(%s)
                 OR LOWER(uc.email_id) LIKE LOWER(%s)
                 OR LOWER(uc.email_id) LIKE LOWER(%s)
              )
        """,
            (
                user_id_fk,
                first_name + "%",
                last_name + "%",
                f"%{first_name}.{last_name}%",
                f"%{first_name}_{last_name}%",
                f"%{first_name}-{last_name}%",
                f"%{first_name}{last_name}%",
            ),
        )
        clients = cursor.fetchall()

        # 2. Fallback — first/last name or partial email match
        if not clients:
            cursor.execute(
                """
                SELECT DISTINCT uc.users_clients_id, uc.first_name, uc.last_name, uc.email_id
                FROM users_clients uc
                JOIN communication c 
                    ON c.users_clients_id_fk = uc.users_clients_id
                WHERE c.user_id_fk = %s
                  AND (
                        LOWER(uc.first_name) LIKE LOWER(%s)
                     OR LOWER(uc.last_name) LIKE LOWER(%s)
                     OR LOWER(uc.email_id) LIKE LOWER(%s)
                     OR LOWER(uc.email_id) LIKE LOWER(%s)
                  )
            """,
                (
                    user_id_fk,
                    first_name + "%",
                    last_name + "%",
                    f"%{first_name}%",
                    f"%{last_name}%",
                ),
            )
            clients = cursor.fetchall()

    else:
        # 3. Single or multi-part names — search each part
        for part in name_parts:
            cursor.execute(
                """
                SELECT DISTINCT uc.users_clients_id, uc.first_name, uc.last_name, uc.email_id
                FROM users_clients uc
                JOIN communication c 
                    ON c.users_clients_id_fk = uc.users_clients_id
                WHERE c.user_id_fk = %s
                  AND (
                        LOWER(uc.first_name) LIKE LOWER(%s)
                     OR LOWER(uc.last_name) LIKE LOWER(%s)
                     OR LOWER(uc.email_id) LIKE LOWER(%s)
                  )
            """,
                (user_id_fk, part + "%", part + "%", f"%{part}%"),
            )
            clients.extend(cursor.fetchall())

    # ✅ Deduplicate results
    seen_ids = set()
    for c in clients:
        if c[0] not in seen_ids:
            results.append(
                {
                    "name": f"{c[1]} {c[2]}".strip(),
                    "id": c[0],
                    "email": c[3],
                    "start_index": start_idx,
                    "end_index": end_idx,
                    "name_type": "customer",
                }
            )
            seen_ids.add(c[0])

    return results


def extract_names_from_emails(query):
    """
    Extracts possible person names from email IDs found in the query,
    along with their start and end character positions.

    Example:
        Input:  "show mails from john.david@gmail.com and mary_smith@yahoo.com"
        Output: [
            ('John David', 15, 35),
            ('Mary Smith', 40, 62)
        ]
    """
    email_pattern = r"[\w\.-]+@[\w\.-]+(?:\.\w+)?"

    names = []
    for match in re.finditer(email_pattern, query):
        email = match.group()
        print(f"matching email: {email}")
        start_idx = match.start()
        end_idx = match.end()

        # Extract the part before '@'
        local_part = email.split("@")[0]

        # Replace separators like '.', '_', '-', and digits
        cleaned = re.sub(r"[\._\-\d]+", " ", local_part)

        # Remove extra spaces and capitalize properly
        name = " ".join(word.capitalize() for word in cleaned.split() if word)

        if name:
            # store tuple: (name, start_index, end_index)
            names.append((name, start_idx, end_idx))

    return names


def ner_using_prompt(query):

    ner_yaml = load_yaml_file(path=pathconfig.ner)
    ner_template = ner_yaml.get("name_recognition")
    filled_prompt = ner_template.replace("{{user_query}}", str(query))
    modified_yaml = get_fireworks_response(filled_prompt, role="system")

    try:
        ner_result = parse_llm_response(modified_yaml)
    except ValueError as e:
        print(f"🔥 report_generation parsing failed: {e}")
        return jsonify({"error": "Failed to parse report_generation response"}), 500

    entity_detected = ner_result.get("entity_detected")
    if entity_detected:
        names = ner_result.get("entity_name", [])
        results = []
        for word in names:
            start = 0
            while True:
                start = query.find(word, start)
                if start == -1:
                    break
                end = start + len(word)
                results.append([word, start, end])
                start = end  # continue search after this occurrence
        return results
    return None


def identify_names(query):

    # new_query = query.title()
    doc = nlp(query)
    person_names = []

    # extrct names from email id if present
    person_names.extend(extract_names_from_emails(query))
    print(f"person_names from email: {person_names}")

    STOPWORDS = {
        "number",
        "of",
        "times",
        "last",
        "months",
        "weeks",
        "days",
        "has",
        "have",
        "mailed",
        "emailed",
        "the",
        "in",
    }

    # Extract PERSON entities
    for ent in doc.ents:
        if ent.label_ == "PERSON":
            # Clean up the name
            words = [w for w in ent.text.split() if w.lower() not in STOPWORDS]
            if words:
                cleaned_name = " ".join(words)
                person_names.append((cleaned_name, ent.start_char, ent.end_char))

    # Fallback: proper nouns not caught by NER
    if not person_names:
        for token in doc:
            if (
                token.pos_ == "PROPN"
                and token.text.lower() not in STOPWORDS
                and token.i > 0
            ):  # Not first token
                person_names.append(
                    (token.text, token.idx, token.idx + len(token.text))
                )

    print(f"potential names: {person_names}")

    if person_names:
        return person_names


def check_for_names(
    conn,
    special_access_status,
    user_id,
    user_type,
    person_names,
    ambiguous,
    invited_users,
):

    cursor = conn.cursor()
    results = []
    not_found = []

    for full_name, start_idx, end_idx in person_names:
        name_parts = full_name.split()
        candidates = []

        if special_access_status:
            # print("User has special access — searching in both users and users_clients tables")

            # Search in users table
            user_matches = search_in_users_table(cursor, name_parts, start_idx, end_idx)
            for match in user_matches:
                if match["id"] in invited_users:
                    candidates.append(match)

            # Search in users_clients table
            client_matches = search_in_users_clients_table(
                cursor, name_parts, start_idx, end_idx, user_id
            )
            for match in client_matches:
                candidates.append(match)

        else:
            # print("User does NOT have special access — searching in users_clients table only")
            client_matches = search_in_users_clients_table(
                cursor, name_parts, start_idx, end_idx, user_id
            )
            for match in client_matches:
                candidates.append(match)

        # --- Step 3: Categorize based on number of matches ---
        if len(candidates) == 1:
            results.append(candidates[0])
        elif len(candidates) > 1:
            ambiguous[full_name] = candidates
        else:
            not_found.append(
                {
                    "name": full_name,
                    "message": "Could not find this person. If you meant a customer, please rename the customer.",
                }
            )

    # using prompt to detect names using context clarification

    # --- Step 4: Final fallback ---
    if not results and not ambiguous and not not_found:
        # print("No matches found for any names.")
        return (None, None, None)

    return results, ambiguous, not_found


def get_invited_users(user_id, conn, user_type):
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            # Fetch permissions JSON for the given user
            sql = "SELECT permissions FROM users WHERE user_id = %s"
            cursor.execute(sql, (user_id,))
            row = cursor.fetchone()

            emails = []

            if not row or not row.get("permissions"):
                return [], []

            if user_type == "admin":
                permissions = json.loads(row["permissions"])
                emails = [
                    entry["email"]
                    for entry in permissions.get("shared", [])
                    if "email" in entry
                ]

            else:
                # if user_type is "user"
                invited_by_email = json.loads(row["permissions"]).get("invited_by")
                if invited_by_email:
                    # Get the owner's permissions JSON
                    cursor.execute(
                        "SELECT permissions FROM users WHERE email = %s",
                        (invited_by_email,),
                    )
                    owner_row = cursor.fetchone()
                    if owner_row and owner_row.get("permissions"):
                        owner_permissions = json.loads(owner_row["permissions"])
                        emails = [
                            entry["email"]
                            for entry in owner_permissions.get("shared", [])
                            if "email" in entry
                        ]

            # Now fetch user_ids for each email
            invited_users_ids = []
            if emails:
                format_strings = ",".join(["%s"] * len(emails))
                sql = f"SELECT user_id FROM users WHERE email IN ({format_strings})"
                cursor.execute(sql, tuple(emails))
                invited_users_ids = [row["user_id"] for row in cursor.fetchall()]

            return invited_users_ids

    except Exception as e:
        # print("Error fetching invited users:", e)
        return None, None

    finally:
        if cursor:
            cursor.close()


def check_user_type(user_id, conn):
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            sql = "SELECT user_type, special_access FROM users WHERE user_id = %s"
            cursor.execute(sql, (user_id,))
            row = cursor.fetchone()

            if not row:
                return False, None

            user_type = row["user_type"]
            special_access = str(row.get("special_access", "")).lower()

            if user_type == "admin" or special_access == "true":
                return True, user_type

            return False, user_type

    except Exception as e:
        # print("Error checking user type:", e)
        return False, None


def replace_names_with_placeholders(
    query,
    results,
    ai_input_list,
    person_map,
    invited_users,
    authorized_users_list,
    actual_name_list,
):
    """
    Replaces names in the query using start/end indices from 'results'.
    Also assigns placeholders for authorized_users_list entries (not appended to ai_input_list).
    Collects new_person_param: dynamically creates sets for 'user'/'customer' as needed.

    Returns:
        new_query, person_map, ai_input_list, authorized_users_list, actual_name_list, new_person_param
    """
    sorted_results = []
    new_person_param = {}

    if results:
        print(f"results found : {results}")
        sorted_results = sorted(results, key=lambda x: x["start_index"], reverse=True)

        for i, res in enumerate(sorted_results, start=1):
            placeholder = f"person{i}id"
            start, end = res["start_index"], res["end_index"]

            # Replace text in the query
            query = query[:start] + placeholder + query[end:]

            # Store mappings
            person_map[res["id"]] = placeholder
            ai_input_list.append({placeholder: res["name_type"]})
            actual_name_list.append({placeholder: res["name"]})

            # Add ID to the correct set, create set if key does not exist
            key = res["name_type"]  # "user" or "customer"
            if key not in new_person_param:
                new_person_param[key] = set()
            new_person_param[key].add(res["id"])

    # Handle invited/authorized users
    if invited_users:
        print(f"invited_users found : {invited_users}")

        next_index = len(sorted_results) + 1
        for j, user_id in enumerate(invited_users, start=next_index):
            placeholder = f"person{j}id"
            person_map[user_id] = placeholder
            authorized_users_list.append(placeholder)
            actual_name_list.append({placeholder: "unknown"})

    return (
        query,
        person_map,
        ai_input_list,
        authorized_users_list,
        actual_name_list,
        new_person_param,
    )


def replace_placeholders_in_sql(sql_query, person_map):
    """
    Replaces placeholder keys (person1id, person2id, etc.) in the SQL query
    with the actual database IDs from person_map.
    """
    updated_query = sql_query

    # Iterate through mapping and replace placeholders safely
    for actual_id, placeholder in person_map.items():
        pattern = r"\b" + re.escape(placeholder) + r"\b"
        sql_query = re.sub(pattern, actual_id, sql_query)
    return sql_query


def prepare_for_lance(results):

    folder_names = []
    searched_user_id = ""

    for r in results:
        if r.get("name_type") == "user":
            searched_user_id = r.get("id")
        elif r.get("name_type") == "customer":
            folder_names.append(r.get("id"))
    return folder_names, searched_user_id


def determine_referenced_person(new_query, reporting_yaml):
    person_template = reporting_yaml.get("referenced_person_identifier")
    filled_prompt = person_template.replace("{{user_query}}", str(new_query))
    modified_yaml = get_fireworks_response(filled_prompt, role="system")

    try:
        referneced_person_result = parse_llm_response(modified_yaml)
    except ValueError as e:
        print(f"🔥 referneced_person_result parsing failed: {e}")
        return (
            jsonify({"error": "Failed to parse referneced_person_result response"}),
            500,
        )

    referenced_person = referneced_person_result.get("referenced_person")
    return referenced_person


def get_referenced_person(ai_input_list, new_query):
    # determine whether the user is referncing another user or customer
    reporting_yaml = load_yaml_file(path=pathconfig.reporting)
    if not ai_input_list:
        referenced_person = determine_referenced_person(new_query, reporting_yaml)
    else:
        values = {list(d.values())[0] for d in ai_input_list}
        if values == {"customer"}:
            referenced_person = "customer"
        elif values == {"user"}:
            referenced_person = "user"
        else:
            referenced_person = "mixed"

    return referenced_person


def dummy_execute(corrected_query, corrected_params, database_schema, reporting_yaml):
    template = reporting_yaml.get("sql_execution_engine")
    filled_prompt = (
        template.replace("{{sql_query}}", str(corrected_query))
        .replace(
            "{{sql_params}}", json.dumps(corrected_params, ensure_ascii=False, indent=2)
        )
        .replace(
            "{{database_schema}}",
            json.dumps(database_schema, ensure_ascii=False, indent=2),
        )
    )
    modified_yaml = get_fireworks_response2(filled_prompt, role="system", temp=0.1)

    try:
        dummy_executer_result = parse_llm_response(modified_yaml)
    except ValueError as e:
        print(f"🔥 dummy_executer_result parsing failed: {e}")
        return jsonify({"error": "Failed to parse dummy_executer_result response"}), 500

    success = dummy_executer_result.get("success", "")
    error = dummy_executer_result.get("error", "")

    return success, error


def perform_sql_verification(
    corrected_query,
    corrected_params,
    error,
    current_user_id,
    table_relationships,
    database_schema,
    reporting_yaml,
):
    template = reporting_yaml.get("report_generation_special_self_evalator")
    filled_prompt = (
        template.replace("{{sql_query}}", str(corrected_query))
        .replace(
            "{{sql_params}}", json.dumps(corrected_params, ensure_ascii=False, indent=2)
        )
        .replace("{{error}}", str(error))
        .replace("{{current_user_id}}", str(current_user_id))
        .replace(
            "{{table_relationships}}",
            json.dumps(table_relationships, ensure_ascii=False, indent=2),
        )
        .replace(
            "{{database_schema}}",
            json.dumps(database_schema, ensure_ascii=False, indent=2),
        )
    )
    modified_yaml = get_fireworks_response2(filled_prompt, role="system", temp=0.1)

    try:
        sql_verfication_result = parse_llm_response(modified_yaml)
    except ValueError as e:
        print(f"🔥 sql_verfication_result parsing failed: {e}")
        return (
            jsonify({"error": "Failed to parse sql_verfication_result response"}),
            500,
        )

    success = sql_verfication_result.get("success", "")
    fixed_sql = sql_verfication_result.get("fixed_sql", "")
    query_params = sql_verfication_result.get("query_params", "")

    return success, fixed_sql, query_params


def parameterization_n_validation(
    generated_query, query_params, reporting_yaml, current_date
):

    # parameterization_and_security_correction:
    template = reporting_yaml.get("parameterization_and_security_correction")
    filled_prompt = template.replace("{{sql_query}}", str(generated_query)).replace(
        "{{query_params}}", str(query_params)
    )
    modified_yaml = get_fireworks_response2(filled_prompt, role="system", temp=0.2)

    try:
        parameterization_result = parse_llm_response(modified_yaml)
    except ValueError as e:
        print(f"🔥 parameterization_result parsing failed: {e}")
        return (
            jsonify({"error": "Failed to parse parameterization_result response"}),
            500,
        )

    parameterized_corrected_sql = parameterization_result.get("corrected_query", "")
    parameterized_sql_params = parameterization_result.get("sql_params", "")

    print(f"parameterized_corrected_sql : {parameterized_corrected_sql}")
    print(f"parameterized_sql_params : {parameterized_sql_params}")

    # validation of the sql and params
    template = reporting_yaml.get("sql_verifier")
    filled_prompt = (
        template.replace("{{sql_query}}", str(parameterized_corrected_sql))
        .replace(
            "{{sql_params}}",
            json.dumps(parameterized_sql_params, ensure_ascii=False, indent=2),
        )
        .replace("{{current_date}}", str(current_date))
    )
    modified_yaml = get_fireworks_response2(filled_prompt, role="system", temp=0.2)

    try:
        validation_result = parse_llm_response(modified_yaml)
    except ValueError as e:
        print(f"🔥 validation parsing failed: {e}")
        return jsonify({"error": "Failed to parse validation response"}), 500

    corrected_query = validation_result.get("corrected_query")
    corrected_params = validation_result.get("sql_params", [])
    verifer_summary = validation_result.get("explanation")
    print(f"output of sql verifer : {corrected_query}")
    print(f"output of sql verifer corrected_params : {corrected_params}")
    print(f"output of verifer_summary : {verifer_summary}")

    return corrected_query, corrected_params


def escape_mysql_date_tokens(sql):
    """
    Escapes only MySQL date format tokens (%Y, %m, %d, %c, %e...)
    but does NOT touch %s used for query parameters.
    """

    # List all MySQL date tokens that need escaping
    # date_tokens = ["%Y","%y","%m","%c","%b","%M","%d","%e","%w","%a","%W","%H","%k","%h","%I","%l","%i","%r","%T","%p","%S","%u","%U","%j","%%"]

    # # Build replacements (but do NOT touch parameter %s)
    # replacements = {t: t.replace("%", "%%") for t in date_tokens if t != "%s"}

    # for token, escaped in replacements.items():
    #     sql = sql.replace(token, escaped)

    date_tokens = [
        "Y",
        "y",
        "m",
        "c",
        "b",
        "M",
        "d",
        "e",
        "w",
        "a",
        "W",
        "H",
        "k",
        "h",
        "I",
        "l",
        "i",
        "r",
        "T",
        "p",
        "S",
        "u",
        "U",
        "j",
    ]

    # Regex to find % followed by one of the date tokens, but not %s
    pattern = re.compile(r"%(?!%|s)(" + "|".join(date_tokens) + r")")

    # Replace single % with double %%
    return pattern.sub(r"%%\1", sql)

    # return sql


def format_rows(rows):
    clean_rows = []

    for row in rows:
        clean_row = []
        for val in row:
            if isinstance(val, datetime):
                clean_row.append(val.date().isoformat())  # datetime → date
            elif isinstance(val, date):
                clean_row.append(val.isoformat())  # date → just isoformat
            else:
                clean_row.append(val)
        clean_rows.append(clean_row)

    return clean_rows


async def continue_report_generation(
    data, ast, variable_columns, temporal_flag, aggregation_flag, pivot_values_map
):

    # Unpack everything from the dictionary
    report_id = data.get("report_id")
    user_id = data.get("user_id")
    actual_query = data.get("actual_query")
    new_query = data.get("new_query")
    ai_input_list = data.get("ai_input_list")
    person_map = data.get("person_map")
    authorized_users_list = data.get("authorized_users_list")

    contain_name_flag = data.get("contain_name_flag")
    sql_intent = data.get("sql_intent")

    base_dir = os.path.dirname(__file__)
    reporting_yaml = load_yaml_file(path=pathconfig.reporting)

    table_details_path = os.path.join(base_dir, "table_desc_self.json")
    with open(table_details_path, "r", encoding="utf-8") as f:
        table_details = json.load(f)

    # get the function according to the intent
    INTENT_TO_SQL_FUNCTION = {
        "Aggregation": ast_to_sql,
        "Trend": ast_to_sql,
        "Ranking": ast_to_sql,
        "Retrieval": ast_to_sql,
    }

    sql_function = INTENT_TO_SQL_FUNCTION[sql_intent]
    generated_query, query_params = sql_function(ast)
    # print("##------------------##")
    print(f"generated_query : {generated_query}")
    print(f"query_params : {query_params}")

    # corrected_query, corrected_params = parameterization_n_validation(generated_query, query_params, reporting_yaml, current_date )

    table_details_path_for_dummy = os.path.join(base_dir, "table_details.json")
    with open(table_details_path_for_dummy, "r", encoding="utf-8") as f:
        table_details_for_dummy = json.load(f)

    # Loop until dummy execution is successful
    success = False
    max_attempts = 3  # Optional safety to avoid infinite loops
    attempt = 0

    while not success and attempt < max_attempts:
        attempt += 1
        success, error = dummy_execute(
            generated_query, query_params, table_details_for_dummy, reporting_yaml
        )
        print(f"Attempt {attempt} | success: {success} | error: {error}")

        if not success:
            # Try fixing SQL using verification
            success, generated_query, query_params = perform_sql_verification(
                generated_query,
                query_params,
                error,
                user_id,
                table_details,
                table_details_for_dummy,
                reporting_yaml,
            )
            # generated_query, query_params = parameterization_n_validation(generated_query, query_params, reporting_yaml, current_date )

            print(
                f" //////// corrected using dummy execution :({attempt}) ////////////"
            )
            print(f"generated_query : {generated_query}")
            print(f"corrected_params : {query_params}")
        # print("*****************")

    if not success:
        print("Failed to correct the query after multiple attempts.")

    # report title generation
    template = reporting_yaml.get("report_title_generator")
    filled_prompt = template.replace("{{user_query}}", str(actual_query))
    modified_yaml = get_fireworks_response(filled_prompt, role="system")

    try:
        title_result = parse_llm_response(modified_yaml)
    except ValueError as e:
        print(f"🔥 title generation parsing failed: {e}")
        return jsonify({"error": "Failed to parse validation response"}), 500

    report_title = title_result.get("brief_summary")

    # replace the placeholder in corrected_params with the actual id
    # for key, value in person_map.items():
    #     corrected_params = [key if x == value else x for x in corrected_params]

    # replace the table, column names in query
    new_query_obfuscated_replaced = replace_obfuscated_names(generated_query, name_map)

    if contain_name_flag:
        sql_query = replace_placeholders_in_sql(
            new_query_obfuscated_replaced, person_map
        )
    else:
        sql_query = new_query_obfuscated_replaced

    sql_query = escape_mysql_date_tokens(sql_query)

    # execute the query
    success = False
    attempt = 0
    max_attempts = 5

    conn = None
    cursor = None

    while not success and attempt < max_attempts:
        attempt += 1
        try:

            conn = connect_to_rds()
            cursor = conn.cursor()
            print(f"sql_query before execution : {sql_query}")
            print(f"query_params before execution : {query_params}")

            cursor.execute(sql_query, query_params)
            rows = cursor.fetchall()
            rows = format_rows(rows)
            # print("Query executed successfully.")
            print(f"Rows returned: {rows}")

            success = True  # Exit loop if successful

        except Exception as e:
            logger.error("Database error: %s", e)

            # Always try to fix SQL via verification
            dummy_success, generated_query, query_params = perform_sql_verification(
                generated_query,
                query_params,
                str(e),
                user_id,
                table_details,
                table_details_for_dummy,
                reporting_yaml,
            )
            # generated_query, corrected_params = parameterization_n_validation(generated_query, query_params, reporting_yaml, current_date )

            print(f" retry generation during execution : attempt : {attempt}")
            print(f"Corrected query for next attempt: {generated_query}")

            # Reapply placeholder replacements
            for key, value in person_map.items():
                query_params = [key if x == value else x for x in query_params]
            new_query_obfuscated_replaced = replace_obfuscated_names(
                generated_query, name_map
            )
            if contain_name_flag:
                sql_query = replace_placeholders_in_sql(
                    new_query_obfuscated_replaced, person_map
                )
            else:
                sql_query = new_query_obfuscated_replaced

        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    if not success:
        # print("Failed to execute query after multiple attempts.")
        return jsonify({"data": None, "chart_type": None})

    # If we reach here, query executed successfully

    # visualization

    template = reporting_yaml.get("visualization_and_axis_labelling")
    filled_prompt = (
        template.replace("{{user_query}}", str(new_query)).replace(
            "{{sql_query}}", str(generated_query)
        )
        # .replace("{{variables}}", json.dumps(variables, ensure_ascii=False, indent=2))
    )
    modified_yaml = get_fireworks_response(filled_prompt, role="system")

    try:
        visualization_result = parse_llm_response(modified_yaml)
    except ValueError as e:
        print(f"🔥 visualization_result parsing failed: {e}")
        return jsonify({"error": "Failed to parse visualization_result response"}), 500

    suggested_chart = visualization_result.get("chart_type")

    # TODO

    chart_axis = get_chart_axes_aggregation(
        variable_columns, sql_intent.lower(), temporal_flag, pivot_values_map
    )
    x_axis = chart_axis.get("x_axis")
    y_axis = chart_axis.get("y_axis")
    variables = [x_axis, y_axis]

    # data = [dict(zip(variables, row)) for row in rows]
    report_details = {
        "report_id": report_id,
        "data": rows,
        "x_axis": x_axis,
        "y_axis": y_axis,
        "chart_type": suggested_chart,
        "variables": variables,
        "brief_summary": report_title,
    }
    await save_draft_report(
        user_id,
        {
            "report_id": report_id,
            "query": new_query,
            "x_axis": x_axis,
            "y_axis": y_axis,
            "chart_type": suggested_chart,
            "variables": variables,
            "brief_summary": report_title,
        },
    )

    return {"status": "resolved", "report_details": report_details}


def merge_decomposed_queries(final_decomposed_query, new_query_data):
    print(f"new: {new_query_data}")
    print(f"old: {final_decomposed_query}")

    for key, value in new_query_data.items():
        if key not in final_decomposed_query:
            final_decomposed_query[key] = value
        else:
            # Merge dicts recursively
            if isinstance(final_decomposed_query[key], dict) and isinstance(
                value, dict
            ):
                merge_decomposed_queries(final_decomposed_query[key], value)
            # Merge lists by extending
            elif isinstance(final_decomposed_query[key], list) and isinstance(
                value, list
            ):
                # Avoid duplicates
                for item in value:
                    if item not in final_decomposed_query[key]:
                        final_decomposed_query[key].append(item)
            # Overwrite if the new value is not None
            elif value is not None:
                final_decomposed_query[key] = value

    print(f"merged: {final_decomposed_query}")
    return final_decomposed_query


def find_join_path(schema_graph, start_table, end_table, visited=None):
    if visited is None:
        visited = set()
    if start_table == end_table:
        return [start_table]

    visited.add(start_table)
    for neighbor in schema_graph[start_table]["connected_to"]:
        if neighbor not in visited:
            path = find_join_path(schema_graph, neighbor, end_table, visited)
            if path:
                return [start_table] + path
    return None


def get_join_conditions(schema_graph, path):
    joins = []
    for i in range(len(path) - 1):
        left, right = path[i], path[i + 1]
        join = schema_graph[left]["joins"].get(right) or schema_graph[right][
            "joins"
        ].get(left)
        if join:
            joins.append(join)
    return joins


def get_tables(start, end):
    base_dir = os.path.dirname(__file__)
    graph_path = os.path.join(base_dir, "schema_graph.json")
    with open(graph_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    # ✅ FIX: Point directly to the tables dictionary
    schema_graph = raw["schema_graph"]["tables"]

    path = find_join_path(schema_graph, start, end)
    joins = get_join_conditions(schema_graph, path)

    ##print("Join Path:", path)
    ##print("Join Conditions:", joins)

    return path, joins


def get_enum_columns(table_names):
    enum_path = os.path.join(base_dir, "enum_columns.json")
    with open(enum_path, "r", encoding="utf-8") as f:
        enum_schema = json.load(f)

    result = {}
    for table in table_names:
        if table in enum_schema:
            result[table] = enum_schema[table]
    return result


def find_sql_intent(original_query, reporting_yaml):
    intent_template = reporting_yaml.get("sql_intent_detection")
    filled_prompt = intent_template.replace("{{user_query}}", str(original_query))
    modified_yaml = get_fireworks_response(filled_prompt, role="system")
    try:
        intent_result = parse_llm_response(modified_yaml)
    except ValueError as e:
        print(f"🔥 intent_result parsing failed: {e}")
        return jsonify({"error": "Failed to parse intent_result response"}), 500
    intent_type = intent_result.get("intent_type")
    return intent_type


async def perform_clarification(
    data,
    reporting_yaml=None,
    new_query=None,
    table_details=None,
    intent_query=None,
    user_id=None,
    query=None,
    decomposed_query=None,
    enum_results=None,
    sql_intent=None,
    referenced_person=None,
):

    print(f"final decomposed_query : {decomposed_query}")
    # Step 1: find top-level empty-string fields
    empty_string_fields = [
        key for key, value in decomposed_query.items() if value in ("", [], {}, None)
    ]

    # Step 2: if "filters" is in the list, check enum_results
    if "filters" in empty_string_fields:
        # If enum_results is empty/None, remove "filters"
        if not enum_results:
            empty_string_fields.remove("filters")
            print(empty_string_fields)

    # Step 3: if no aggregation is there for ranking intent, avoid asking questions on that
    aggregation_flag = True
    if sql_intent == "Ranking":
        if "aggregation" in empty_string_fields:
            aggregation_flag = False
            empty_string_fields.remove("aggregation")
        # print("removed aggregation from empty string fields")

    # Step 4: for retreival intent, aggreagation, grouping_dimension is not mandatory. so remove them to avoid asking questions on that
    if sql_intent == "Retrieval":
        if "aggregation" in empty_string_fields:
            empty_string_fields.remove("aggregation")
        if "grouping_dimension" in empty_string_fields:
            empty_string_fields.remove("grouping_dimension")

        # print("removed aggregation and grouping_dimension from empty string fields")

    # print("----------------")
    print(
        f"empty_string_fields before sending to clarification : {empty_string_fields}"
    )
    # print("----------------")

    if len(empty_string_fields) == 0:

        if not query:
            query = data.get("actual_query")
        ast, variable_columns, pivot_values_map = await ast_gen.generate_ast(
            decomposed_query,
            reporting_yaml,
            query,
            sql_intent,
            referenced_person,
            data,
            aggregation_flag,
        )

        temporal_flag = decomposed_query.get("temporal_flag")
        result = await continue_report_generation(
            data,
            ast,
            variable_columns,
            temporal_flag,
            aggregation_flag,
            pivot_values_map,
        )
        return result

    else:
        if not reporting_yaml:
            reporting_yaml = load_yaml_file(path=pathconfig.reporting)

        print(f"filters_possible: {enum_results}")
        clarification_template = reporting_yaml.get("clarification_prompt_1")

        if sql_intent in ["Trend", "Ranking", "Retrieval", "Aggregation"]:
            template_key_map = {
                "Ranking": "clarification_prompt_1_ranking",
                "Trend": "clarification_prompt_1_trend",
                "Retrieval": "clarification_prompt_1_retrieval",
                "Aggregation": "clarification_prompt_1_aggregation",
            }
        clarification_template = reporting_yaml.get(template_key_map.get(sql_intent))

        filled_prompt = (
            clarification_template.replace(
                "{{database_schema}}",
                json.dumps(table_details, ensure_ascii=False, indent=2),
            )
            .replace("{{primary_search_table}}", str(intent_query))
            .replace(
                "{{empty_string_fields}}",
                json.dumps(empty_string_fields, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{filters_possible}}",
                json.dumps(enum_results, ensure_ascii=False, indent=2),
            )
        )

        modified_yaml = get_fireworks_response2(filled_prompt, role="system", temp=0.2)

        try:
            clarification_result = parse_llm_response(modified_yaml)
            # print("***************")
            print(f"clarification_result : {clarification_result}")
        # print("***************")

        except ValueError as e:
            print(f"🔥 clarification_generation parsing failed: {e}")
            return (
                jsonify({"error": "Failed to parse clarification_generation response"}),
                500,
            )

        clarifications = clarification_result.get("clarifications")
        if clarifications:
            data["aggregation_flag"] = aggregation_flag
            data["final_decomposed_query"] = decomposed_query
            if sql_intent:
                data["sql_intent"] = sql_intent

            # converting from set to list because set is not serializable
            if "new_person_param" in data:
                for key, value in data["new_person_param"].items():
                    if isinstance(value, set):
                        data["new_person_param"][key] = list(value)

            await save_clarification_state_to_redis(user_id, data)
            return jsonify(
                {
                    "status": "clarifications",
                    "clarifications": clarifications,
                    "orginal_query": query,
                    "clarification_result": clarification_result,
                }
            )

        else:

            if not query:
                query = data.get("actual_query")
            ast, variable_columns, pivot_values_map = await ast_gen.generate_ast(
                decomposed_query,
                reporting_yaml,
                query,
                sql_intent,
                referenced_person,
                data,
                aggregation_flag,
            )

            temporal_flag = decomposed_query.get("temporal_flag")
            result = await continue_report_generation(
                data,
                ast,
                variable_columns,
                temporal_flag,
                aggregation_flag,
                pivot_values_map,
            )
            return result


@ai_reporting_bp.route("/generate_report", methods=["POST"])
async def generate_report():
    data = request.get_json()
    query = data.get("query", "").strip()
    user_id = data.get("user_id", "").strip()
    selected_entity_id = data.get("selected_entity_id")

    if not query:
        return jsonify({"message": "User query not found"}), 400
    if not user_id:
        return jsonify({"message": "user id not found"}), 400

    conn = connect_to_rds()
    cursor = conn.cursor()

    results = []
    ambiguous = {}
    not_found = []
    ai_input_list = []
    person_map = {}
    searched_user_id = ""
    folder_names = []
    invited_users = []
    authorized_users_list = []
    actual_name_list = []
    selected_id = []
    lance_flag = False

    if not selected_entity_id:

        # get the special_access and user type of the current user
        special_access_status, user_type = check_user_type(user_id, conn)
        logger.info(
            f"user type : {user_type}, special_access_status: {special_access_status} "
        )

        if special_access_status:
            invited_users = get_invited_users(user_id, conn, user_type)

        # check whether a name is present in the query and belongs to the current users' accessible list of users and clients
        person_names = identify_names(query)
        if person_names:
            results, ambiguous, not_found = check_for_names(
                conn,
                special_access_status,
                user_id,
                user_type,
                person_names,
                ambiguous,
                invited_users,
            )

        if not results:
            person_names = ner_using_prompt(query)
            if person_names:
                results, ambiguous, not_found = check_for_names(
                    conn,
                    special_access_status,
                    user_id,
                    user_type,
                    person_names,
                    ambiguous,
                    invited_users,
                )

        # logger.info(f"results from query : {results}")
        # logger.info(f"ambiguous from query : {ambiguous}")
        # logger.info(f"not_found from query : {not_found}")

        if ambiguous:
            await save_ambiguous_report_to_redis(
                user_id, results, ambiguous, query, special_access_status
            )
            return {"status": "ambiguous", "report_details": ambiguous}

        if not_found:
            pass

    else:
        data = await get_ambiguous_report_from_redis(user_id)
        results = data.get("results")
        ambiguous = data.get("ambiguous")
        query = data.get("query")
        special_access_status = data.get("special_access_status")

        # find the selected entity from ambiguous and append it to results
        for name, entries in ambiguous.items():
            match = next((e for e in entries if e["id"] == selected_entity_id), None)
            if match:
                results.append(match)
                break

    # anonymization of the actual query
    # ai_input_list is a list of anonymised persons mentioned in the querynthat will b sent to the prompt eg: [{'person1id': 'customer'}]
    (
        new_query,
        person_map,
        ai_input_list,
        authorized_users_list,
        actual_name_list,
        new_person_param,
    ) = replace_names_with_placeholders(
        query,
        results,
        ai_input_list,
        person_map,
        invited_users,
        authorized_users_list,
        actual_name_list,
    )

    if not results:
        new_query = query

    # logger.info(f"new_query : {new_query}")
    # logger.info(f"person_map : {person_map}")
    # logger.info(f"ai_input_list : {ai_input_list}")
    # print("##------------------##")
    logger.info(f"new_person_param : {new_person_param}")

    # classify as db or lance search
    reporting_yaml = load_yaml_file(path=pathconfig.reporting)
    classification_template = reporting_yaml.get("query_classification")
    filled_prompt = classification_template.replace("{{user_query}}", str(new_query))
    modified_yaml = get_fireworks_response(filled_prompt, role="system")

    try:
        classification_result = parse_llm_response(modified_yaml)
    except ValueError as e:
        print(f"🔥 report_generation parsing failed: {e}")
        return jsonify({"error": "Failed to parse report_generation response"}), 500

    query_type = classification_result.get("query_type")
    intent_query = classification_result.get("query", [])
    print(f"---------------intent_query : {intent_query}")

    if query_type == "semantic_query":
        if results:
            folder_names, searched_user_id = prepare_for_lance(results)
        if not searched_user_id:
            searched_user_id = user_id

        client = UmailLanceClient(user_id)
        lance_mails = client.search_email_from_lance(folder_names, user_id, query)
        if lance_mails:
            lance_flag = True
            dates = []
            for mail in lance_mails:
                id = mail.get("id")
                # plain_text = mail.get("plain_text", "")
                timestamp = mail.get("timestamp", "")
                # from_address = mail.get("from", "")

                selected_id.append(id)
                dates.append(timestamp)

            print(f"%%%%%%  selected_id : {selected_id}")

            print(f"no. of mails fetched: {len(selected_id)}")
            print(f"no. of unique mails fetched: {len(set(selected_id))}")

            # # Count occurrences of each ID
            # id_counts = Counter(mail["id"] for mail in lance_mails)

            # # Keep only repeated IDs
            # repeated_ids = {mid for mid, count in id_counts.items() if count > 1}

            # # Create list of dicts with id and plain_text for repeated IDs
            # repeated_mails = [
            #     {"id": mail["id"], "plain_text": mail["plain_text"]}
            #     for mail in lance_mails if mail["id"] in repeated_ids
            # ]

            # print(repeated_mails)

    # add the user id to person_map so that we can replace in the query params later
    person_map[user_id] = "current_user_id"

    # get referenced person
    referenced_person = get_referenced_person(ai_input_list, new_query)

    # data collection from raw user input
    decomposed_query, enum_results, sql_intent, primary_search_table = (
        await post_calrify_with_user(
            query,
            user_id,
            user_satisfied=None,
            clarifications=None,
            user_edit=None,
            system_query=None,
            referenced_person=referenced_person,
            primary_search_table=intent_query,
        )
    )

    # choose primary_search_table_context according to the primary_search_table
    base_dir = os.path.dirname(__file__)
    table_path = os.path.join(base_dir, "table_details.json")
    with open(table_path, "r", encoding="utf-8") as f:
        table_details = json.load(f)
    primary_search_table_context = table_details.get(primary_search_table, {})

    id = str(uuid.uuid4())
    report_id = f"{user_id}_report_{id}"

    data = {
        "report_id": report_id,
        "user_id": user_id,
        "actual_query": query,
        "new_query": new_query,
        "ai_input_list": ai_input_list,
        "person_map": person_map,
        "authorized_users_list": authorized_users_list,
        "primary_search_table": primary_search_table,
        "special_access_status": special_access_status,
        "primary_search_table_context": primary_search_table_context,
        "contain_name_flag": True if results else False,
        "referenced_person": referenced_person,
        "sql_intent": sql_intent,
        "new_person_param": new_person_param,
        "lance_flag": lance_flag,
        "selected_id": selected_id,
    }

    # clarification step
    return await perform_clarification(
        data,
        reporting_yaml,
        new_query,
        table_details,
        primary_search_table,
        user_id,
        query,
        decomposed_query,
        enum_results,
        sql_intent,
        referenced_person,
    )


def get_primary_search_table(entity):
    template_key_map = {
        "customers": "customer_accounts",
        "clients": "customer_accounts",
        "users": "users_account",
        "tickets": "service_requests",
        "messages": "msgs",
        "mails": "msgs",
        "integration": "integrated",
        "review": "reviews",
        "feedback": "reviews",
    }
    primary_search_table = template_key_map.get(entity)
    return primary_search_table


def get_enum_results(entity, primary_search_table, referenced_person, metric):
    if entity:
        # find the table and columns invloved
        if entity in ["user", "users", "all users"]:
            end = "users_account"
        elif entity in ["customer", "customers", "client", "clients"]:
            end = "customer_accounts"
        elif entity in ["tickets", "ticket"]:
            end = "service_requests"
        elif entity in ["message", "messages", "email", "emails"]:
            end = "msgs"
        else:
            end = "users_account"

        print(f"primary_search_table : {primary_search_table} | end = {end}")

        path_1, joins_1 = get_tables(primary_search_table, end)

    if referenced_person in ["self", "all users"]:
        end_should_be = "users_account"
    elif referenced_person == "customer":
        end_should_be = "customer_accounts"

    if end != end_should_be:
        path_2, joins_2 = get_tables(end, end_should_be)
        path = path_1 + path_2
        # joins = joins_1 + joins_2

    else:
        path = path_1
        # joins = joins_1

    metric_table = metric.split(".")[0]
    if metric_table not in path:
        path.append(metric_table)

    enum_results = get_enum_columns(path)
    # print("***************")
    print(f"enum_results : {enum_results}")
    # print("***************")

    return enum_results


async def post_calrify_with_user(
    original_query,
    user_id,
    user_satisfied=None,
    clarifications=None,
    user_edit=None,
    system_query=None,
    referenced_person=None,
    primary_search_table=None,
):

    reporting_yaml = load_yaml_file(path=pathconfig.reporting)

    if clarifications:

        if isinstance(clarifications, str):
            try:
                clarifications = json.loads(clarifications.replace("'", '"'))
            except json.JSONDecodeError:
                clarifications = []

        clarification_state = await get_report_data(user_id)
        sql_intent = clarification_state.get("sql_intent")
        final_decomposed_query = clarification_state.get("final_decomposed_query")
        temporal_flag = final_decomposed_query.get("temporal_flag")

        template_key_map = {
            "Retrieval": "data_extraction_from_qa_retrieval",
            "Ranking": "data_extraction_from_qa_ranking",
            "Aggregation": "data_extraction_from_qa_aggregation",
            "Trend": "data_extraction_from_qa_trend",
            "Comparison": "data_extraction_from_qa_comparison",
        }
        # If the query involves temporal grouping, override the template
        if temporal_flag:
            if sql_intent.lower() == "ranking":
                template_key = "data_extraction_from_qa_ranking_temporal_true"
            else:
                template_key = "data_extraction_from_qa_trend"
        else:
            template_key = template_key_map.get(sql_intent)

        data_from_qa_template = reporting_yaml.get(template_key)

        filled_prompt = data_from_qa_template.replace(
            "{{clarification_question_answers}}",
            json.dumps(clarifications, ensure_ascii=False, indent=2),
        )
        modified_yaml = get_fireworks_response2(filled_prompt, role="system", temp=0.5)
        try:
            data_from_qa_result = parse_llm_response(modified_yaml)
            print(f"data_from_qa_result : {data_from_qa_result}")
        except ValueError as e:
            print(f"🔥 post clarification parsing failed: {e}")
            return (
                jsonify({"error": "Failed to parse post clarification response"}),
                500,
            )

        new_query_data = data_from_qa_result.get("structured_query_data", "")
        structured_query_data = merge_decomposed_queries(
            final_decomposed_query, new_query_data
        )

        # check for any empty fields if still present
        # Step 1: find top-level empty-string fields
        empty_string_fields = [
            key for key, value in structured_query_data.items() if value == ""
        ]

        # Step 2: if "filters" is in the list, check enum_results
        if "filters" in empty_string_fields:
            # If enum_results is empty/None, remove "filters"
            # if not enum_results:
            empty_string_fields.remove("filters")

        # Step 3: if no aggregation is there for ranking intent, avoid asking questions on that
        aggregation_flag = clarification_state.get("aggregation_flag")
        if (
            sql_intent == "Ranking"
            and not aggregation_flag
            and "aggregation" in empty_string_fields
        ):
            empty_string_fields.remove("aggregation")
        # print("removed aggregation from empty string fields in post clarify")

        # Step 4: for retreival intent, aggreagation, grouping_dimension is not mandatory. so remove them to avoid asking questions on that
        if sql_intent == "Retrieval":
            if "aggregation" in empty_string_fields:
                empty_string_fields.remove("aggregation")
            if "grouping_dimension" in empty_string_fields:
                empty_string_fields.remove("grouping_dimension")

            # print("removed aggregation and grouping_dimension from empty string fields in post clarify")

        if len(empty_string_fields) == 0:
            query = clarification_state.get("actual_query")
            referenced_person = clarification_state.get("referenced_person")
            ast, variable_columns, pivot_values_map = await ast_gen.generate_ast(
                structured_query_data,
                reporting_yaml,
                query,
                sql_intent,
                referenced_person,
                clarification_state,
                aggregation_flag,
            )
            ##print("***************")
            # print(f"ast given to report generation : {ast}")
            ##print("***************")

            temporal_flag = structured_query_data.get("temporal_flag")
            result = await continue_report_generation(
                clarification_state,
                ast,
                variable_columns,
                temporal_flag,
                aggregation_flag,
                pivot_values_map,
            )
            return result

        return await perform_clarification(
            clarification_state,
            decomposed_query=structured_query_data,
            reporting_yaml=None,
            sql_intent=sql_intent,
            referenced_person=referenced_person,
        )

    # elif user_edit:
    #         post_clarification_template = reporting_yaml.get("post_clarification_prompt")
    #         filled_prompt = (
    #                         post_clarification_template.replace("{{user_query}}", str(original_query))
    #                         .replace("{{clarification_question_answers}}", str(user_edit))
    #                     )
    #         modified_yaml = get_fireworks_response2(filled_prompt, role="system", temp = 0.7)

    else:
        # if it gets here, it means this is the first iteration

        # find the sql intent
        sql_intent = find_sql_intent(original_query, reporting_yaml)
        print(f"sql_intent : {sql_intent}")

        # get entity, grouping_dimension, metric, filters, temporal flag to understand the tables and columns
        q_intent_extrt = QueryIntentionExtractor(reporting_yaml, base_dir)

        entity, grouping_dimension, metric, aggregation, filters, temporal_flag = (
            q_intent_extrt.extract_all(original_query, sql_intent)
        )
        print(
            f"entity : {entity} | grouping_dimension : {grouping_dimension} |  metric : {metric} | aggregation : {aggregation} | filters : {filters}"
        )
        print(f"temporal_flag : {temporal_flag}")

        # get filter options for the query according to the tables involved
        primary_search_table = get_primary_search_table(entity)
        enum_results = get_enum_results(
            entity, primary_search_table, referenced_person, metric
        )

        # If the query involves temporal grouping, override the template
        template_key_map = {
            "Retrieval": "data_extraction_from_query_retrieval",
            "Ranking": "data_extraction_from_query_ranking",
            "Aggregation": "data_extraction_from_query_aggregation",
            "Trend": "data_extraction_from_query_trend",
            "Comparison": "data_extraction_from_query_comparison",
        }
        if temporal_flag:
            template_key = "data_extraction_from_query_trend"
        else:
            template_key = template_key_map.get(sql_intent)

        query_data_extract = reporting_yaml.get(template_key)
        filled_prompt = query_data_extract.replace(
            "{{user_query}}", str(original_query)
        )
        # if sql_intent.lower() == "ranking"  :
        #     filled_prompt = filled_prompt.replace(
        #         "{{grouping_dimension}}", json.dumps(grouping_dimension, ensure_ascii=False, indent=2)
        #     )

        modified_yaml = get_fireworks_response2(filled_prompt, role="system", temp=0.5)
        try:
            data_extraction_result = parse_llm_response(modified_yaml)
        except ValueError as e:
            print(f"🔥 post clarification parsing failed: {e}")
            return (
                jsonify({"error": "Failed to parse post clarification response"}),
                500,
            )

        structured_query_data = data_extraction_result.get("structured_query_data", "")

        # attach data that we got manually
        structured_query_data["entity"] = entity
        structured_query_data["filters"] = filters
        structured_query_data["temporal_flag"] = temporal_flag
        structured_query_data["user_id"] = user_id

        if sql_intent.lower() in ["aggregation", "trend", "ranking", "retrieval"]:
            structured_query_data["metric"] = metric
            structured_query_data["aggregation"] = aggregation
            structured_query_data["grouping_dimension"] = grouping_dimension

        # if temporal flag is true and the intent is ranking, we need to find ranking_direction and limit seperately"
        if sql_intent.lower() == "ranking" and temporal_flag:
            direction_limit_template = reporting_yaml.get(
                "data_extraction_from_query_ranking_temporal_true"
            )
            filled_prompt = direction_limit_template.replace(
                "{{user_query}}", str(original_query)
            )
            modified_yaml = get_fireworks_response2(
                filled_prompt, role="system", temp=0.5
            )
            try:
                direction_limit_result = parse_llm_response(modified_yaml)
                print(f"direction_limit_result from query: {direction_limit_result}")
            except ValueError as e:
                print(f"🔥 post clarification parsing failed: {e}")
                return (
                    jsonify({"error": "Failed to parse post clarification response"}),
                    500,
                )

            direction_limit_data = direction_limit_result.get(
                "direction_limit_data", ""
            )
            ranking_direction = direction_limit_data.get("ranking_direction")
            limit = direction_limit_data.get("limit")

            structured_query_data["ranking_direction"] = ranking_direction or ""
            structured_query_data["limit"] = limit or ""

        return structured_query_data, enum_results, sql_intent, primary_search_table

    # status = post_clarification_result.get("status", "")
    # final = post_clarification_result.get("final", "")

    # try:
    #         post_clarification_result = parse_llm_response(modified_yaml)
    # except ValueError as e:
    #         print(f"🔥 post clarification parsing failed: {e}")
    #         return jsonify({"error": "Failed to parse post clarification response"}), 500

    # status = post_clarification_result.get("status", "")
    # final = post_clarification_result.get("final", "")

    # if status =="name clarification":   # TODO
    #         return jsonify({
    #             "sub_question": final,
    #             "status":"name clarification",
    #             "original_query":original_query

    #         })
    # else:
    #         print(f"inside else part")
    #         system_query = post_clarification_result.get("system_query", "")
    #         return jsonify({
    #             "final":final,
    #             "system_query":system_query,
    #             "original_query":original_query,
    #             "status":"post classification"
    #         })


@ai_reporting_bp.route("/post_clarifications", methods=["POST"])
async def post_clarifications():
    data = request.get_json()
    clarifications = data.get("clarifications", "")
    original_query = data.get("original_query", "")
    user_edit = data.get("user_edit")
    user_satisfied = data.get("user_satisfied")
    user_id = data.get("user_id")
    system_query = data.get("system_query", "")

    print(f"clarifications : {clarifications}")
    print(f"original_query : {original_query}")
    print(f"user_edit : {user_edit}")
    print(f"user_satisfied : {user_satisfied}")
    print(f"user_id : {user_id}")
    print(f"system_query: {system_query}")

    return await post_calrify_with_user(
        original_query, user_id, user_satisfied, clarifications, user_edit, system_query
    )


@ai_reporting_bp.route("/finalize_report", methods=["POST"])
async def finalize_report():
    data = request.get_json()
    report_id = data.get("report_id", "").strip()
    user_id = data.get("user_id", "").strip()

    client = await GlideClusterClient.create(redis_config_glide)
    report_data = await get_draft_report(client, user_id, report_id)
    if report_data:
        try:
            await insert_user_report(report_data, user_id)
            await delete_draft_report(client, user_id, report_id)
            return jsonify({"message": "Report finalized successfully"}), 200
        except Exception as e:
            return (
                jsonify({"message": "Report finalization failed", "error": str(e)}),
                400,
            )

    return jsonify({"message": "Report not found in drafts"}), 404


@ai_reporting_bp.route("/list_all_draft_reports", methods=["POST"])
async def list_all_draft_reports(client, user_id):
    """
    Retrieve all draft reports for a user from Redis.
    Prints each report_id and its content.
    """
    key = f"user:{user_id}:draft_reports"
    all_reports = await client.hgetall(key)

    if not all_reports:
        print(f"No draft reports found for user {user_id}")
        return

    print(f"Draft reports for user {user_id}:")
    for report_id, report_json in all_reports.items():
        report_data = json.loads(report_json)
        print(f"- Report ID: {report_id}")
        print(f"  Content: {json.dumps(report_data, indent=2)}\n")


@ai_reporting_bp.route("/change_name", methods=["POST"])
async def change_name():
    data = request.get_json()
    name = data.get("name", "").strip()
    report_id = data.get("report_id", "").strip()
    user_id = data.get("user_id", "").strip()

    client = await GlideClusterClient.create(redis_config_glide)
    report_data = await get_draft_report(client, user_id, report_id)

    if report_data:
        try:
            # Convert from JSON string if needed
            if isinstance(report_data, str):
                report_data = json.loads(report_data)

            # Update brief_summary and save back to Redis
            report_data["brief_summary"] = name
            await save_draft_report(user_id, report_data)

            await client.close()
            return (
                jsonify(
                    {
                        "success": True,
                        "message": "Report name updated successfully (Redis).",
                    }
                ),
                200,
            )

        except Exception as e:
            await client.close()
            return jsonify({"success": False, "error": str(e)}), 500

    # If report not in Redis, check MySQL
    else:
        await client.close()
        try:
            conn = await get_db_connection()  # your async DB connection function
            async with conn.cursor() as cursor:
                # Fetch reports JSON from users table
                await cursor.execute(
                    "SELECT reports FROM users WHERE user_id = %s", (user_id,)
                )
                result = await cursor.fetchone()

                if not result or not result[0]:
                    return (
                        jsonify(
                            {
                                "success": False,
                                "error": "No reports found for this user.",
                            }
                        ),
                        404,
                    )

                reports = json.loads(result[0])

                # Find report with matching report_id
                updated = False
                for report in reports:
                    if report.get("report_id") == report_id:
                        report["brief_summary"] = name
                        updated = True
                        break

                if not updated:
                    return (
                        jsonify({"success": False, "error": "Report ID not found."}),
                        404,
                    )

                # Save updated JSON back to DB
                await cursor.execute(
                    "UPDATE users SET reports = %s WHERE user_id = %s",
                    (json.dumps(reports), user_id),
                )
                await conn.commit()

                return (
                    jsonify(
                        {
                            "success": True,
                            "message": "Report name updated successfully (Database).",
                        }
                    ),
                    200,
                )

        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            await conn.ensure_closed()


@ai_reporting_bp.route("/set_special_access", methods=["POST"])
async def set_special_access():
    data = request.get_json()
    user_email = data.get("email", "")
    special_access = data.get("special_access", "")

    if not user_email:
        return jsonify({"message": "User email not found"}), 400
    if special_access == "":
        return jsonify({"message": "special_access not found"}), 400

    conn = connect_to_rds()
    try:
        with conn.cursor() as cursor:
            sql = "UPDATE users SET special_access = %s WHERE email = %s"
            cursor.execute(sql, (special_access, user_email))
            conn.commit()
            print(
                f"✅ Special access set to {special_access} for user_email {user_email}"
            )
            return jsonify({"message": "Special access updated successfully"}), 200

    except Exception as e:
        # print("❌ Error updating special access:", e)
        conn.rollback()
        return (
            jsonify({"message": "Error updating special access", "error": str(e)}),
            500,
        )

    finally:
        conn.close()


# --------- for testing ---------------


@ai_reporting_bp.route("/change_name_test", methods=["POST"])
def change_name_test():
    data = request.get_json()
    query = data.get("mysql_query", "")
    new_query_obfuscated_replaced = replace_obfuscated_names(query, name_map)
    return new_query_obfuscated_replaced


@ai_reporting_bp.route("/creation_of_umail_index", methods=["POST"])
def creation_of_umail_index():
    user_id = "112359636982080060072"
    try:
        client = UmailLanceClient(user_id)
        lance_mails = client.creating_index()
        return jsonify({"message": "index creation successful"})
    except Exception as e:
        print(f"Error in creation_of_umail_index: {e}")
        return []


@ai_reporting_bp.route("/extract_names_from_emails_test", methods=["POST"])
def extract_names_from_emails_test():
    """
    Extracts possible person names from email IDs found in the query,
    along with their start and end character positions.

    Example:
        Input:  "show mails from john.david@gmail.com and mary_smith@yahoo.com"
        Output: [
            ('John David', 15, 35),
            ('Mary Smith', 40, 62)
        ]
    """

    data = request.get_json()
    query = data.get("query", "")
    email_pattern = r"[\w\.-]+@[\w\.-]+(?:\.\w+)?"

    names = []
    for match in re.finditer(email_pattern, query):
        email = match.group()
        print(f"matching email: {email}")
        start_idx = match.start()
        end_idx = match.end()

        # Extract the part before '@'
        local_part = email.split("@")[0]

        # Replace separators like '.', '_', '-', and digits
        cleaned = re.sub(r"[\._\-\d]+", " ", local_part)

        # Remove extra spaces and capitalize properly
        name = " ".join(word.capitalize() for word in cleaned.split() if word)

        if name:
            # store tuple: (name, start_index, end_index)
            names.append((name, start_idx, end_idx))

    return names
