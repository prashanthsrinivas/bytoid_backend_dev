from datetime import datetime, timezone
import os
import uuid
from agent_route.lance_agent import LanceClient, QueryInput
from cust_helpers import pathconfig
from flask import Blueprint, request, jsonify, session
from db.rds_db import connect_to_rds
from gmail_route.routes import gmail_reply
from umail_helper.mails_process import update_config_file
from umail_lance.umail_lance_agent import UmailLanceClient
from utils.fireworkzz import get_fireworks_response
from utils.normal import ensure_dir, load_yaml_file
import pymysql, json

from utils.s3_utils import read_json_from_s3, upload_any_file
from zoho_routes.routes import send_zoho_email


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


def suggest_helper_base(userid, email_msg, umail_conversations, umail_bodies):
    # Extract sender email
    try:
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

        question_text = (
            question_data.get("question", "").strip() if question_data else ""
        )
        base_doc_ans = []
        if question_text:
            top_k = 3
            query_input = QueryInput(
                user_id=userid, query_text=question_text, top_k=top_k
            )
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
                "{{base_doc_ans}}",
                json.dumps(base_doc_ans, ensure_ascii=False, indent=2),
            )
            .replace("{{business_name}}", business_name)
            .replace("{{business_address}}", business_address)
            .replace("{{business_website}}", business_website)
            .replace("{{sender_name}}", sender_name)
        )
        # print("base docs ans", base_doc_ans)

        ai_reply = get_fireworks_response(filled_prompt, "system")
        return ai_reply
    except Exception as e:
        return {"error": f"suggest ai {e}"}
    finally:
        if connection:
            connection.close()


def send_pilot_messages(
    user_id=None,
    channel=None,
    text=None,
    conversation_id=None,
    b_connection=None,
    client_id=None,
    user_email=None,
    client_email=None,
    subject=None,
    thread_id=None,
    ticket_id=None,
    ticket_name=None,
    is_reply=True,
):
    print("🚀 [DEBUG] Starting send_messages() function")

    try:
        if b_connection is None:
            connection = connect_to_rds()
        connection = b_connection
        cursor = connection.cursor()
        conv_folder = os.path.join(pathconfig.basepath, "messages", user_id, client_id)
        print(f"📁 [DEBUG] Conversation folder: {conv_folder}")
        ensure_dir(conv_folder)
        file_name = f"{conversation_id}.json"
        conv_filepath = os.path.join(conv_folder, file_name)
        s3_conv_key = f"{user_id}/messages/{client_id}/{conversation_id}.json"
        print(f"📁 [DEBUG] Conversation file path: {conv_filepath}")
        print(f"📁 [DEBUG] S3 conversation key: {s3_conv_key}")

        # Load existing conversation data
        try:
            print(f"📖 [DEBUG] Attempting to read existing conversation from S3...")
            raw_data = read_json_from_s3(s3_conv_key)
            input_data = raw_data.get("input_data", [])
            print(f"📖 [DEBUG] Loaded {len(input_data)} existing messages")
        except Exception as e:
            print(f"📖 [DEBUG] No existing conversation found, starting fresh: {e}")
            input_data = []

        print(f"📖 [DEBUG] input_data length: {len(input_data)}")
        # Create final message object
        now_utc = datetime.now(timezone.utc)
        formatted_time = now_utc.isoformat(timespec="seconds")
        msg_id = str(uuid.uuid4())

        message = {
            "id": msg_id,
            "from": user_email,
            "to": client_email,
            "body": text,
            "timestamp": formatted_time,
            "channel": channel,
            "direction": "outbound",
            "subject": subject,
            "status": "pending",
            "source": channel,
            "user_id": user_id,
            "thread_id": thread_id,
            "conversation_id": conversation_id,
            "ticket_id": ticket_id,
            "ticket_name": ticket_name,
        }
        print(f"📧 [DEBUG] Final message object created: {message}")

        # Send the message via appropriate channel
        sent_message_id, sent_thread_id = None, None

        print(f"🚀 [DEBUG] Sending message via channel: {channel}")

        if channel == "gmail":
            print("📧 [DEBUG] Processing Gmail send...")
            if isinstance(input_data, dict):
                # input_data is a single message dict
                latest_msg = input_data
            elif isinstance(input_data, list):
                # input_data is a list of message dicts
                valid_messages = [
                    msg
                    for msg in input_data
                    if isinstance(msg, dict) and "timestamp" in msg
                ]
                if not valid_messages:
                    print("❌ [DEBUG] No valid messages found in input_data")
                    return jsonify({"error": "No valid messages found"}), 400
                latest_msg = max(
                    valid_messages,
                    key=lambda msg: datetime.fromisoformat(msg["timestamp"]),
                )
            else:
                print("❌ [DEBUG] input_data is neither dict nor list")
                return jsonify({"error": "Invalid input_data format"}), 400
            latest_id = latest_msg["id"]
            print(f"📧 [DEBUG] Latest message ID: {latest_id}")

            # Getting the reply subject
            latest_subject = latest_msg["subject"].strip()
            if not latest_subject.lower().startswith("re:"):
                reply_subject = f"Re: {latest_subject}"
            else:
                reply_subject = latest_subject
            print(f"📧 [DEBUG] Reply subject: {reply_subject}")

            try:
                print(f"📧 [DEBUG] Calling gmail_reply()...")
                sent_message_id = gmail_reply(
                    user_id,
                    to=client_email,
                    subject=reply_subject,
                    thread_id=thread_id,
                    body_text=text,
                    in_reply_to=latest_id,
                    connection=connection,
                )
                message["id"] = sent_message_id
                print(
                    f"✅ [DEBUG] Gmail reply sent successfully, message_id: {sent_message_id}"
                )

            except Exception as e:
                print(f"❌ [DEBUG] Gmail reply failed: {e}")
                return jsonify({"error": "Gmail send failed"}), 500

        elif channel == "zoho":
            print("📧 [DEBUG] Processing Zoho send...")
            try:
                print(f"📧 [DEBUG] Calling send_zoho_email()...")
                response_payload, status_code = send_zoho_email(
                    user_id=user_id,
                    to_email=client_email,
                    subject=subject,
                    body_text=text,
                    from_user_email=user_email,
                )
                print(
                    f"📧 [DEBUG] Zoho response - status: {status_code}, payload: {response_payload}"
                )

                if status_code in [200, 201]:
                    message_id = response_payload.get("message_id")
                    print(
                        f"✅ [DEBUG] Zoho message sent successfully, message_id: {message_id}"
                    )
                else:
                    print(
                        f"❌ [DEBUG] Zoho send failed: {response_payload.get('error')}"
                    )
                    return (
                        jsonify({"error": response_payload.get("error")}),
                        status_code,
                    )

            except Exception as e:
                print(f"❌ [DEBUG] Zoho send failed: {e}")
                return jsonify({"error": "Zoho send failed"}), 500

        else:
            print(f"❌ [DEBUG] Unsupported channel: {channel}")
            return jsonify({"error": "Unsupported channel"}), 400

        # Database updates
        print("💾 [DEBUG] Starting database updates...")
        updated_date = datetime.now(timezone.utc).isoformat()
        created_date = updated_date
        print(
            f"💾 [DEBUG] Timestamps - created: {created_date}, updated: {updated_date}"
        )

        try:
            print("💾 [DEBUG] Updating existing ticket and thread...")
            cursor.execute(
                "UPDATE tickets SET updated_in = %s, status = %s WHERE conversation_id_fk = %s",
                (updated_date, "In-Progress", conversation_id),
            )
            print("✅ [DEBUG] Ticket updated")

            cursor.execute(
                "UPDATE threads SET last_message_at = %s WHERE conversation_id = %s",
                (updated_date, conversation_id),
            )
            print("✅ [DEBUG] Thread updated")

            cont_ref = f"{user_id}/messages/{client_id}/{conversation_id}.json"
            print(f"💾 [DEBUG] Content reference: {cont_ref}")

            print("💾 [DEBUG] Inserting message record...")
            cursor.execute(
                """
                INSERT INTO messages (
                    message_id,
                    conversation_id_fk,
                    sender_id,
                    content_ref,
                    message_type,
                    is_summary,
                    created_at,
                    update_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    msg_id,
                    conversation_id,
                    client_id,
                    cont_ref,
                    "outbound",
                    subject,
                    created_date,
                    updated_date,
                ),
            )
            print("✅ [DEBUG] Message record inserted")

            connection.commit()
            print("✅ [DEBUG] Database transaction committed")

        except Exception as e:
            connection.rollback()
            print(f"❌ [DEBUG] Database operation failed — rolled back: {e}")
            return jsonify({"error": "Database operation failed"}), 500

        # Update Conversation File
        print("📄 [DEBUG] Updating conversation file...")
        try:
            if isinstance(input_data, dict):
                input_data = [input_data]
            input_data.append(message)
            conversation_data = {"input_data": input_data}
            print(f"📄 [DEBUG] Total messages in conversation: {len(input_data)}")
            print(f"conversation_data : {conversation_data}")
            print(f"💾 [DEBUG] Writing to local file: {conv_filepath}")
            with open(conv_filepath, "w", encoding="utf-8") as f:
                json.dump(conversation_data, f, indent=2)

            print(f"☁️ [DEBUG] Uploading to S3: {s3_conv_key}")
            upload_any_file(
                conv_filepath,
                user_id,
                type="messages",
                s3_key_C=s3_conv_key,
            )
            print("✅ [DEBUG] Conversation file updated successfully")

        except Exception as e:
            print(f"❌ [DEBUG] Failed to update conversation file: {e}")
            return jsonify({"error": "Failed to save conversation"}), 500

        # updating lancedb
        lance_data = conversation_data.get("input_data", [])
        client = UmailLanceClient(user_id)
        client.embed_json_file_for_reply(
            lance_data, user_id, client_id, conversation_id
        )

        # Update Config File
        print("⚙️ [DEBUG] Updating config file...")
        config_folder = os.path.join(
            pathconfig.basepath, "messages", user_id, client_id
        )
        ensure_dir(config_folder)
        config_filepath = os.path.join(config_folder, "config.json")

        s3_config_key = f"{user_id}/messages/{client_id}/config.json"
        try:
            config_data = read_json_from_s3(s3_config_key)
            print(
                f"⚙️ [DEBUG] Existing config loaded with {len(config_data.get('conversations', []))} conversations"
            )
        except Exception as e:
            print(f"⚙️ [DEBUG] No config file found — creating new: {e}")
            config_data = {"userclients_id": client_id, "conversations": []}

        # Parse timestamp
        try:
            if updated_date.endswith("Z"):
                parsed_ts = datetime.fromisoformat(updated_date.replace("Z", "+00:00"))
            else:
                parsed_ts = datetime.fromisoformat(updated_date)
            print(f"⚙️ [DEBUG] Parsed timestamp: {parsed_ts.isoformat()}")
        except Exception as e:
            print(f"⚠️ [DEBUG] Could not parse updated_date '{updated_date}': {e}")
            parsed_ts = datetime.now(timezone.utc)

        # Create updated entry
        updated_entry = {
            "conv_id": conversation_id,
            "ticket_id": ticket_id,
            "ticket_name": ticket_name,
            "subject": subject,
            "channel": channel,
            "updated_date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "parsed_timestamp": parsed_ts.isoformat(),
        }
        print(f"*****ticket_id 2 : {ticket_id}")
        if channel == "gmail":
            if sent_thread_id:
                updated_entry["thread_id"] = sent_thread_id
                print(f"⚙️ [DEBUG] Using sent_thread_id: {sent_thread_id}")
            else:
                updated_entry["thread_id"] = thread_id
                print(f"⚙️ [DEBUG] Using existing thread_id: {thread_id}")
        else:
            updated_entry["thread_id"] = ""
            print("⚙️ [DEBUG] Non-Gmail channel, no thread_id")

        print(f"⚙️ [DEBUG] Updated entry: {updated_entry}")

        # Update or add conversation in config
        conversation_exists = False
        for i, conv in enumerate(config_data.get("conversations", [])):
            if conv.get("conv_id") == conversation_id:
                config_data["conversations"][i] = updated_entry
                conversation_exists = True
                print(f"⚙️ [DEBUG] Updated existing conversation at index {i}")
                break

        if not conversation_exists:
            config_data.setdefault("conversations", []).append(updated_entry)
            print("⚙️ [DEBUG] Added new conversation to config")

        config_data["userclients_id"] = client_id
        print(f"⚙️ [DEBUG] Calling update_config_file()...")
        update_config_file(user_id, client_id, config_data)
        print("✅ [DEBUG] Config file updated successfully")

        # Final Response
        response_data = {
            "status": "sent",
            "id": sent_message_id or msg_id,
            "channel": channel,
            "conversationId": conversation_id,
            "is_reply": is_reply,
        }
        print(f"🎉 [DEBUG] Function completed successfully - Response: {response_data}")
        return response_data

    except Exception as e:
        print(f"❌ [DEBUG] Unexpected error in send_messages(): {e}")
        import traceback

        print(f"❌ [DEBUG] Full traceback: {traceback.format_exc()}")
        return {"error": "Internal server error"}
    finally:
        if b_connection is None and connection:
            connection.close()
