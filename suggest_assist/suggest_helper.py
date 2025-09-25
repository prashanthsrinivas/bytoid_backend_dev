from agent_route.lance_agent import LanceClient, QueryInput
from cust_helpers import pathconfig
from flask import Blueprint, request, jsonify, session
from db.rds_db import connect_to_rds
from umail_lance.umail_lance_agent import UmailLanceClient
from utils.fireworkzz import get_fireworks_response
from utils.normal import load_yaml_file
import pymysql, json


def getselectedconv(conv_id, userid):
    try:
        connection = connect_to_rds()
        if connection is None:
            return jsonify({"error": "Database connection failed"}), 500
        cursor = connection.cursor()

        cursor.execute(
            "SELECT sender_id FROM messages WHERE conversation_id_fk = %s",
            (conv_id,),
        )
        client_id_row = cursor.fetchone()
        if client_id_row:
            client_id = client_id_row[0]
        else:
            return (
                jsonify(
                    {"message": f"⚠️ No sender_id found for conversation_id {conv_id}"}
                ),
                404,
            )
    except Exception as e:
        return (
            jsonify({"message": f"❌ Error executing sender_id query: {e}"}),
            500,
        )
    finally:
        if connection:
            connection.close()
    client = UmailLanceClient(userid)
    recent_msg = client.get_selected_conv_from_lance(userid, client_id)
    return recent_msg[conv_id] or []


def suggest_helper_base(userid, email_msg, conv_id):

    # Fetch email conversation data
    umail_conversations = getselectedconv(conv_id=conv_id, userid=userid)
    umail_bodies = [item.get("body", "") for item in umail_conversations]

    # Extract sender email
    sender_email = None
    for item in umail_conversations:
        if item.get("direction") == "inbound" and item.get("from"):
            sender_email = item["from"]
            break
    if not sender_email and umail_conversations:
        sender_email = umail_conversations[0].get("from")

    # Extract sender name from email
    sender_name = ""
    if sender_email:
        local_part = sender_email.split("@")[0]
        if "noreply" in local_part.lower():
            domain = sender_email.split("@")[1].split(".")[0]
            sender_name = domain.capitalize()
        else:
            sender_name = local_part.split(".")[0].capitalize()

    # Fetch retrieval question template
    pr_file = load_yaml_file(path=pathconfig.conv_template)
    template = pr_file.get("generate_retrieval_question")
    filled_prompt = template.replace("{{message_text}}", email_msg)

    # Fetch business info
    connection = connect_to_rds()
    with connection.cursor(pymysql.cursors.DictCursor) as cursor:
        cursor.execute(
            "SELECT user_type,permissions from users where user_id = %s LIMIT 1",
            (userid,),
        )
        user_row = cursor.fetchone()
        businessdata = {}

        if user_row:
            if user_row["user_type"] == "user":
                user_permissions = (
                    json.loads(user_row["permissions"])
                    if user_row.get("permissions")
                    else {}
                )
                invited_by_email = user_permissions.get("invited_by")

                base_user_id = None
                if invited_by_email:
                    cursor.execute(
                        "SELECT user_id from users where email = %s",
                        (invited_by_email,),
                    )
                    base = cursor.fetchone()
                    base_user_id = base.get("user_id") if base else None

                if base_user_id:
                    cursor.execute(
                        "SELECT BusinessName, BillingAddress, WebsiteUrl FROM business_info WHERE user_id_fk = %s LIMIT 1",
                        (base_user_id,),
                    )
                    businessdata = cursor.fetchone() or {}
            else:
                cursor.execute(
                    "SELECT BusinessName, BillingAddress, WebsiteUrl FROM business_info WHERE user_id_fk = %s LIMIT 1",
                    (userid,),
                )
                businessdata = cursor.fetchone() or {}

    business_name = (
        businessdata.get("BusinessName") if businessdata else "Our Organization"
    )
    business_address = businessdata.get("BillingAddress") if businessdata else ""
    business_website = businessdata.get("WebsiteUrl") if businessdata else ""

    business_name = (
        businessdata.get("BusinessName") if businessdata else "Our Organization"
    )
    business_address = businessdata.get("BillingAddress") if businessdata else ""
    business_website = businessdata.get("WebsiteUrl") if businessdata else ""

    # Call model to generate retrieval question
    base_query = get_fireworks_response(filled_prompt, "system")

    # Parse retrieval question safely
    try:
        question_data = json.loads(base_query)
    except json.JSONDecodeError:
        import re

        json_text = re.search(r"\{.*\}", base_query, re.DOTALL)
        question_data = json.loads(json_text.group(0)) if json_text else {}

    question_text = question_data.get("question", "").strip() if question_data else ""
    base_doc_ans = []
    if question_text:
        top_k = 3
        query_input = QueryInput(user_id=userid, query_text=question_text, top_k=top_k)
        lance_client = LanceClient(user_id=userid)
        results = lance_client.query_vector(query_input)
        for r in results:
            clean_text = r.get("text", "").encode().decode("unicode_escape")
            base_doc_ans.append(clean_text)

    # Build final prompt for AI reply
    prompt_template = pr_file.get("base_eval_response")
    filled_prompt = (
        prompt_template.replace("{{email_msg}}", email_msg)
        .replace(
            "{{umail_conversations}}",
            json.dumps(umail_bodies, ensure_ascii=False, indent=2),
        )
        .replace(
            "{{base_doc_ans}}", json.dumps(base_doc_ans, ensure_ascii=False, indent=2)
        )
        .replace("{{business_name}}", business_name)
        .replace("{{business_address}}", business_address)
        .replace("{{business_website}}", business_website)
        .replace("{{sender_name}}", sender_name)
    )
    print("base docs ans", base_doc_ans)

    ai_reply = get_fireworks_response(filled_prompt, "system")
    return ai_reply
