from flask import Flask, request, jsonify, Blueprint, Response
import re

from create_db import connect_to_rds
from umail_lance.umail_lance_agent import UmailLanceClient
from cust_helpers import pathconfig
from utils.normal import load_yaml_file
import json
from utils.fireworkzz import get_fireworks_response
import yaml
from utils.s3_utils import read_json_from_s3
from presidio_anonymizer import AnonymizerEngine
from datetime import datetime
from utils.base_logger import get_logger
import re
from difflib import SequenceMatcher
from ai_reporting.parse_llm import parse_llm_response
from request_context import current_user_id


search_bp = Blueprint("search", __name__)


def get_nlp_emails():
    import spacy

    nlp = spacy.load("en_core_web_md")
    return nlp


logger = get_logger(__name__)


def get_entity(text_input, user_id, cursor):

    # 1. regex search to find email id
    email_pattern = r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"
    match = re.search(email_pattern, text_input)
    if match:
        email_id = match.group(0)
        start_index = match.start()
        end_index = match.end()

        email_prefix = email_id.split("@")[0] if "@" in email_id else email_id
        # Split by common separators like dots, underscores, numbers, etc.
        parts = re.split(r"[._\-0-9]+", email_prefix)
        parts = [part for part in parts if part]  # Remove empty strings

        # Build the query dynamically
        if parts:
            like_conditions = " OR ".join(
                ["LOWER(SUBSTRING_INDEX(email_id, '@', 1)) LIKE LOWER(%s)"] * len(parts)
            )
            params = [user_id] + [f"%{part}%" for part in parts]

            cursor.execute(
                f"""
                SELECT users_clients_id
                FROM users_clients
                WHERE users_clients_id IN (
                    SELECT users_clients_id_fk
                    FROM communication
                    WHERE user_id_fk = %s
                )
                AND ({like_conditions})
                """,
                params,
            )
        client_exists = cursor.fetchone()

        client_id = None
        if client_exists:
            client_id = client_exists[0]

        return [
            {
                "name": email_id,
                "id": client_id,
                "start_index": start_index,
                "end_index": end_index,
            }
        ]

    # 2. name search using spaCy
    text_title = text_input.title()
    nlp = get_nlp_emails()
    doc = nlp(text_title)
    results = []
    person_names = []

    # for token in doc:
    # print(token.text, token.pos_, token.tag_)

    for ent in doc.ents:
        if ent.label_ == "PERSON":
            person_names.append((ent.text, ent.start_char, ent.end_char))

    if not person_names:
        temp_name = []
        start_idx = None
        for token in doc:
            if token.pos_ == "PROPN":
                if not temp_name:
                    start_idx = token.idx
                temp_name.append(token.text)
            else:
                if temp_name:
                    person_names.append((" ".join(temp_name), start_idx, token.idx))
                    temp_name = []
        if temp_name:
            person_names.append(
                (" ".join(temp_name), start_idx, token.idx + len(temp_name[-1]))
            )

    # print(f"person_names : {person_names}")
    for full_name, start_idx, end_idx in person_names:
        name_parts = full_name.strip().split()

        if len(name_parts) == 2:
            first_name, last_name = name_parts
            # print(f"name_parts :{name_parts}")

            # 1. Try exact full name match
            cursor.execute(
                """
                    SELECT uc.users_clients_id
                    FROM users_clients uc
                    JOIN communication c ON uc.users_clients_id = c.users_clients_id_fk
                    WHERE c.user_id_fk = %s 
                    AND LOWER(uc.first_name) LIKE LOWER(%s)
                    AND LOWER(uc.last_name) LIKE LOWER(%s)
                    """,
                (user_id, first_name + "%", last_name + "%"),
            )
            clients = cursor.fetchall()
            if clients:
                for client in clients:
                    results.append(
                        {
                            "name": full_name,
                            "id": client[0],
                            "start_index": start_idx,
                            "end_index": end_idx,
                        }
                    )

            # 2. If full name not matched, match either first or last name
            if not clients:
                cursor.execute(
                    """
                        SELECT uc.users_clients_id
                        FROM users_clients uc
                        JOIN communication c ON uc.users_clients_id = c.users_clients_id_fk
                        WHERE c.user_id_fk = %s 
                        AND (LOWER(uc.first_name) LIKE LOWER(%s) 
                            OR LOWER(uc.last_name) LIKE LOWER(%s))
                        """,
                    (user_id, first_name + "%", last_name + "%"),
                )
                clients = cursor.fetchall()
                if clients:
                    for client in clients:
                        results.append(
                            {
                                "name": full_name,
                                "id": client[0],
                                "start_index": start_idx,
                                "end_index": end_idx,
                            }
                        )

        else:
            # Single name: match first or last name

            single_name = name_parts[0]
            # print(f"single_name : {single_name}")
            cursor.execute(
                """
                    SELECT uc.users_clients_id
                    FROM users_clients uc
                    JOIN communication c ON uc.users_clients_id = c.users_clients_id_fk
                    WHERE c.user_id_fk = %s 
                    AND (LOWER(uc.first_name) LIKE LOWER(%s) OR LOWER(uc.last_name) LIKE LOWER(%s))
                    """,
                (user_id, single_name + "%", single_name + "%"),
            )
            clients = cursor.fetchall()
            if clients:
                for client in clients:
                    results.append(
                        {
                            "name": full_name,
                            "id": client[0],
                            "start_index": start_idx,
                            "end_index": end_idx,
                        }
                    )

    return results if results else None
    # return person_names


def remove_multiple_entities(text, spans):
    """
    Remove multiple entities from text given their (start_index, end_index) spans.

    Args:
        text (str): Original text
        spans (list of tuples): List of (start, end) positions for entities

    Returns:
        removed_entities (list): List of removed entity substrings
        cleaned_text (str): Text with entities removed and spaces cleaned
    """
    # Sort spans in reverse order so indexes don't shift while removing
    spans = sorted(spans, key=lambda x: x[0], reverse=True)
    # print(f"spans: {spans}")
    for start, end in spans:
        text = text[:start] + text[end:]  # remove the entity

    # Clean up spacing (collapse multiple spaces)
    cleaned_text = re.sub(r"\s+", " ", text).strip()

    return cleaned_text  # reverse back to original order


def merge_spans(spans):
    """
    Merge overlapping or duplicate spans.
    Input: list of (start, end)
    Output: sorted, non-overlapping spans
    """
    if not spans:
        return []

    # Sort by start index
    spans = sorted(spans, key=lambda x: x[0])
    merged = [spans[0]]

    for current in spans[1:]:
        last = merged[-1]
        if current[0] <= last[1]:  # overlap or duplicate
            # merge by taking max end
            merged[-1] = (last[0], max(last[1], current[1]))
        else:
            merged.append(current)

    return merged


def anonymizer(text_input, entities):

    name_mapping = {}
    counter = 1
    new_text = text_input

    for enitity in entities:
        original_name = enitity.get("name")
        if original_name not in name_mapping:
            secret_name = f"secret_name_{counter}"
            name_mapping[original_name] = secret_name
            counter += 1

        pattern = re.compile(re.escape(original_name), re.IGNORECASE)
        new_text = pattern.sub(name_mapping[original_name], new_text)

    return new_text, name_mapping


async def normalize_query(text_input, userid):

    try:
        # print(f"inside normalize query")
        now = datetime.now()
        todays_date = now.strftime("%Y-%m-%d %H:%M:%S")

        yaml_data = load_yaml_file(path=pathconfig.search_email_template)
        update_prompt_template = yaml_data.get("email_search_rewriter")
        if not update_prompt_template:
            # print("❌ Prompt 'email_search_rewriter' not found in template")
            return None

        # Inject message data into prompt
        message_payload = json.dumps(text_input, indent=2)
        full_prompt = update_prompt_template.replace("{input_query}", message_payload)
        full_prompt = full_prompt.replace("{date}", todays_date)

        # Generate YAML output from model
        modified_yaml = await get_fireworks_response(
            full_prompt, role="system", user_id=userid
        )

        try:
            parsed_yaml = yaml.safe_load(modified_yaml.strip())
            if "output_query" in parsed_yaml:
                output_query = parsed_yaml["output_query"]
                return output_query
            else:
                # print("⚠️ Key 'output_query' missing after parsing")
                return None
        except Exception as e:
            # print(f"🔥 YAML parse failed: {e}")
            return None

    except Exception as e:
        # print(f"🔥 Exception during normalisation of query: {str(e)}")
        return None


async def get_search_summary(text_input, anonymised_input, entity_groups, userid):

    try:

        yaml_data = load_yaml_file(path=pathconfig.search_summary)
        update_prompt_template = yaml_data.get("email_summary_generator")
        if not update_prompt_template:
            # print("❌ Prompt 'email_summary_generator' not found in template")
            return None

        # Inject message data into prompt
        message_payload = json.dumps(text_input, indent=2)
        full_prompt = update_prompt_template.replace("{emails_list}", message_payload)
        full_prompt = full_prompt.replace("{user_query}", anonymised_input)
        entity_groups_payload = json.dumps(entity_groups, indent=2)
        full_prompt = full_prompt.replace("{entity_groups}", entity_groups_payload)

        # Generate YAML output from model
        modified_yaml = await get_fireworks_response(
            full_prompt, role="system", user_id=userid
        )

        try:
            parsed_yaml = parse_llm_response(modified_yaml)
            if "output_query" in parsed_yaml:
                return parsed_yaml["output_query"]
            else:
                # print("⚠️ Key 'output_query' missing after parsing")
                return None
        except Exception as e:
            # print(f"🔥 YAML parse failed: {e}")
            return None

    except Exception as e:
        # print(f"🔥 Exception during normalisation of query : {str(e)}")
        return None


def search_db(db_queries, user_id, cursor):

    s3_results = []
    all_query_results = []  # List to store results from each query
    query_messages = {}

    for db_query in db_queries:
        current_query_ids = set()

        # query for date related searches
        if "message_id" in db_query:
            cursor.execute(db_query, (user_id,))
            db_results = cursor.fetchall()
            if db_results:

                for row in db_results:
                    message_id = row[0]

                    s3_conv_key = row[1]
                    raw_data = read_json_from_s3(s3_conv_key)
                    input_data = raw_data.get("input_data", [])
                    if input_data:
                        if isinstance(input_data, list):
                            message = [
                                msg for msg in input_data if msg["id"] == message_id
                            ]
                            if message:
                                current_query_ids.add(message_id)
                                if message_id not in query_messages:
                                    query_messages[message_id] = {
                                        "body": message.get("body"),
                                        "message_id": message_id,
                                        "conversation_id": message.get(
                                            "conversation_id"
                                        ),
                                        "from": message.get("from"),
                                        "ticket_name": message.get("ticket_name"),
                                        "ticket_id": message.get("ticket_id"),
                                        "timestamp": message.get("timestamp"),
                                    }

                        elif isinstance(input_data, dict):
                            message = input_data
                            current_query_ids.add(message_id)
                            if message_id not in query_messages:
                                query_messages[message_id] = {
                                    "body": message.get("body"),
                                    "message_id": message_id,
                                    "conversation_id": message.get("conversation_id"),
                                    "from": message.get("from"),
                                    "ticket_name": message.get("ticket_name"),
                                    "ticket_id": message.get("ticket_id"),
                                    "timestamp": message.get("timestamp"),
                                }

        elif "communication_id_fk" in db_query:
            cursor.execute(db_query, (user_id,))
            db_results = cursor.fetchall()
            if db_results:

                for row in db_results:
                    conversation_id = row[0]
                    sql_query = (
                        "select content_ref from messages where conversation_id_fk = %s"
                    )
                    cursor.execute(sql_query, (conversation_id,))
                    rows = cursor.fetchone()

                    if not rows:
                        continue

                    s3_conv_key = rows[0]
                    raw_data = read_json_from_s3(s3_conv_key)
                    input_data = raw_data.get("input_data", [])

                    # Case 1: input_data is a dict
                    if isinstance(input_data, dict):
                        message_id = input_data.get("id")
                        if message_id:
                            current_query_ids.add(message_id)
                            if message_id not in query_messages:
                                query_messages[message_id] = {
                                    "body": input_data.get("body"),
                                    "message_id": input_data.get("id"),
                                    "conversation_id": input_data.get(
                                        "conversation_id"
                                    ),
                                    "from": input_data.get("from"),
                                    "ticket_name": input_data.get("ticket_name"),
                                    "ticket_id": input_data.get("ticket_id"),
                                    "timestamp": input_data.get("timestamp"),
                                }

                    # Case 2: input_data is a list
                    elif isinstance(input_data, list) and input_data:
                        last_item = input_data[-1]
                        message_id = last_item.get("id")
                        if message_id:
                            current_query_ids.add(message_id)
                            if message_id not in query_messages:
                                query_messages[message_id] = {
                                    "body": last_item.get("body"),
                                    "message_id": input_data.get("id"),
                                    "conversation_id": last_item.get("conversation_id"),
                                    "from": last_item.get("from"),
                                    "ticket_name": last_item.get("ticket_name"),
                                    "ticket_id": last_item.get("ticket_id"),
                                    "timestamp": last_item.get("timestamp"),
                                }

        if current_query_ids:
            all_query_results.append(current_query_ids)
            # print(f"Query IDs: {current_query_ids}")

        # Find common IDs across all queries
    if len(all_query_results) == 0:
        common_query_ids = set()
    elif len(all_query_results) == 1:
        # Only one query, use all its results
        common_query_ids = all_query_results[0]
    else:
        # Multiple queries, find intersection
        common_query_ids = all_query_results[0]
        for query_result in all_query_results[1:]:
            common_query_ids = common_query_ids.intersection(query_result)

    # print(f"common_query_ids: {common_query_ids}")

    for message_id in common_query_ids:
        if message_id in query_messages:
            s3_results.append(query_messages[message_id])

    return s3_results


def anonymize_before_summarisation(message_body, user_query):
    from .setup_presidio import analyzer

    anonymizer = AnonymizerEngine()
    pii_mapping = {}  # Store mapping for de-anonymization

    # ensures that only the longest, non-overlapping entities are anonymized
    def filter_overlaps(results):
        filtered = []
        for res in sorted(
            results, key=lambda r: (r.start, -(r.end - r.start))
        ):  # Ensures that for overlapping spans, the longest one comes first.
            if not any(
                res.start >= f.start and res.end <= f.end for f in filtered
            ):  # This keeps an entity only if it is not fully inside another already chosen one.
                filtered.append(res)
        return filtered

    # anonymise the message body
    for msg in message_body:
        text = msg["body"]

        results = analyzer.analyze(text=text, language="en")
        results = filter_overlaps(results)

        for res in sorted(results, key=lambda r: r.start, reverse=True):
            token = f"<{res.entity_type}_{len(pii_mapping)+1}>"
            pii_mapping[token] = text[res.start : res.end]
            text = text[: res.start] + token + text[res.end :]

        msg["body"] = text

    # anonymise the user_query
    q_results = analyzer.analyze(text=user_query, language="en")
    q_results = filter_overlaps(q_results)

    for res in sorted(q_results, key=lambda r: r.start, reverse=True):
        token = f"<{res.entity_type}_{len(pii_mapping)+1}>"
        pii_mapping[token] = user_query[res.start : res.end]
        user_query = user_query[: res.start] + token + user_query[res.end :]

    return message_body, user_query, pii_mapping


def generate_entity_groups(pii_mapping):
    """
    Consolidate related PII entities into groups.
    Returns: a list of dictionaries, each dictionary represents one group of related tokens
    Example: [{"<PERSON_1>": ["<PERSON_1>", "<PERSON_3>", "<EMAIL_ADDRESS_2>"]}, ...]
    """
    entity_groups_list = []

    # Separate tokens by type
    persons = {t: v.lower() for t, v in pii_mapping.items() if t.startswith("<PERSON_")}
    emails = {
        t: v.lower() for t, v in pii_mapping.items() if t.startswith("<EMAIL_ADDRESS_")
    }

    # Helper: extract name part from email
    def extract_name_from_email(email):
        local_part = email.split("@")[0]
        return re.sub(r"[._0-9]+", "", local_part).lower()

    # Helper: check if two names are similar
    def names_similar(name1, name2, threshold=0.6):
        if name1 in name2 or name2 in name1:
            return True
        return SequenceMatcher(None, name1, name2).ratio() > threshold

    processed_tokens = set()

    for token, value in pii_mapping.items():
        if token in processed_tokens:
            continue

        current_group = [token]
        processed_tokens.add(token)
        value_lower = value.lower()

        # Compare PERSON tokens
        if token.startswith("<PERSON_"):
            person_name = value_lower

            # Related person tokens
            for other_token, other_value in persons.items():
                if other_token not in processed_tokens and names_similar(
                    person_name, other_value
                ):
                    current_group.append(other_token)
                    processed_tokens.add(other_token)

            # Related email tokens
            for email_token, email_value in emails.items():
                if email_token not in processed_tokens:
                    email_name = extract_name_from_email(email_value)
                    if names_similar(person_name, email_name):
                        current_group.append(email_token)
                        processed_tokens.add(email_token)

        # Compare EMAIL tokens
        elif token.startswith("<EMAIL_ADDRESS_"):
            email_name = extract_name_from_email(value_lower)

            # Related person tokens
            for person_token, person_value in persons.items():
                if person_token not in processed_tokens and names_similar(
                    email_name, person_value
                ):
                    current_group.append(person_token)
                    processed_tokens.add(person_token)

            # Other identical emails
            for other_email_token, other_email_value in emails.items():
                if (
                    other_email_token not in processed_tokens
                    and other_email_value == value_lower
                ):
                    current_group.append(other_email_token)
                    processed_tokens.add(other_email_token)

        if len(current_group) > 1:
            # Use first token as canonical token
            main_token = current_group[0]
            entity_groups_list.append({current_group[0]: current_group})

    return entity_groups_list


@search_bp.route("/search-emails", methods=["POST"])
async def search_emails(text_input=None, user_id=None):
    try:

        conn = connect_to_rds()
        cursor = conn.cursor()

        data = request.get_json()
        text_input = data.get("text_input", "")
        user_query = text_input
        user_id = data.get("user_id")

        name_resolved = False
        folder_names = None
        name_mapping = {}

        token = current_user_id.set(user_id)
        try:

            entities = get_entity(text_input, user_id, cursor)
            logger.info(f"entities :{entities}")
            if entities:
                spans = [(item["start_index"], item["end_index"]) for item in entities]
                spans = merge_spans(
                    spans
                )  # spans contain unique start and end index of enitities
                # text_input = remove_multiple_entities(text_input,spans) # text_input is now free from enitites
                name_resolved = True

            logger.info(f"name_resolved :{name_resolved}")
            if name_resolved:
                folder_names = [
                    item["id"] for item in entities
                ]  # folder_names contain client_id
                text_input, name_mapping = anonymizer(
                    text_input, entities
                )  # the names of entities are replaced by secret_name_1 ....

            # Initialize variables
            semantic_query = None
            db_queries = None

            normalized_input = await normalize_query(text_input, userid=user_id)
            if normalized_input is not None:
                for item in normalized_input:
                    if "semantic_search_query" in item:
                        semantic_query = item["semantic_search_query"]
                    if "db_search_queries" in item:
                        db_queries = item["db_search_queries"]
            else:
                logger.warning(
                    "normalize_query returned None - proceeding with empty search parameters"
                )

            # To reverse the replacement
            if name_mapping and semantic_query:
                for original, secret in name_mapping.items():
                    semantic_query = semantic_query.replace(secret, original)

            logger.info(f"semantic_query : {semantic_query}")
            logger.info(f"db_query : {db_queries}")

            # query the db
            db_mails = []
            if db_queries is not None and db_queries != "null":
                db_mails = search_db(db_queries, user_id, cursor)

            # semantic search in the lance table
            client = UmailLanceClient(user_id)
            if semantic_query and semantic_query != "null":
                lance_mails = await client.search_email_from_lance(
                    folder_names, user_id, semantic_query
                )

            # Determine which messages to process
            if db_queries and db_queries != "null":
                if semantic_query and semantic_query != "null":
                    if db_mails and lance_mails:
                        # Find intersection when both sources have data
                        db_conversation_ids = {
                            mail["conversation_id"] for mail in db_mails
                        }
                        lance_conversation_ids = {
                            mail["conversation_id"] for mail in lance_mails
                        }
                        common_conversation_ids = db_conversation_ids.intersection(
                            lance_conversation_ids
                        )
                        messages_to_process = [
                            mail
                            for mail in db_mails + lance_mails
                            if mail["conversation_id"] in common_conversation_ids
                        ]

                    else:
                        messages_to_process = db_mails or lance_mails or []

                else:
                    messages_to_process = db_mails or []

            elif semantic_query:
                messages_to_process = lance_mails or []

            else:
                messages_to_process = []

            # Process messages
            disp_results = []
            message_body = []

            for mail in messages_to_process:
                plain_text = mail.get("plain_text", "")
                timestamp = mail.get("timestamp", "")
                from_address = mail.get("from", "")

                disp = {
                    "id": mail.get("id"),
                    "body": plain_text,
                    "conversation_id": mail.get("conversation_id"),
                    "from": from_address,
                    "ticket_name": mail.get("ticket_name"),
                    "ticket_id": mail.get("ticket_id"),
                    "timestamp": timestamp,
                }
                disp_results.append(disp)

                msg = {
                    "body": plain_text,
                    "timestamp": timestamp,
                    "from_address": from_address,
                }
                message_body.append(msg)
                # print(
                #     f"id: {mail.get('id')} | conversation_id: {mail.get('conversation_id')}"
                # )

                # print(f"disp_results : {disp_results}")
            # print(f"message_body before anonymization : {message_body}")
            anonymised_text, anonymised_input, pii_mapping = (
                anonymize_before_summarisation(message_body, user_query)
            )

            # logger.info(f"pii_mapping is : {pii_mapping}")
            # logger.info(f"anonymised_text is : {anonymised_text}")
            # logger.info(f"anonymised_input is : {anonymised_input}")

            entity_groups = generate_entity_groups(pii_mapping)
            # logger.info(f"entity_groups is : {entity_groups}")

            summary = await get_search_summary(
                anonymised_text, anonymised_input, entity_groups, userid=user_id
            )
            # logger.info(f"summary before re anonymisation: {summary}")

        finally:
            current_user_id.reset(token)

        # reverse the anonymisation of the summary
        if summary and pii_mapping:
            for token, original in pii_mapping.items():
                summary = summary.replace(token, original)
        # logger.info(f"summary after re anonymisation: {summary}")

        cursor.close()
        conn.close()

        return jsonify([{"emails": disp_results, "summary": summary}])

    except Exception as e:
        logger.error(f"Error in search_emails: {e}")
        return jsonify({"error": "An error occurred during search"}), 500
