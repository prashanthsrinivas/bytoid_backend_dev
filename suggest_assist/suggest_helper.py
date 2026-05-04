from datetime import datetime, timezone
import inspect
import os
import uuid
from agent_route.lance_agent import LanceClient, QueryInput
from cust_helpers import pathconfig
from db.db_checkers import get_business_info, get_users_clients_id
from db.rds_db import connect_to_rds
from gmail_route.routes import gmail_reply
from services.gmail_service import GmailService
from umail_helper.mails_process import update_config_file
from umail_lance.umail_lance_agent import UmailLanceClient
from utils.async_check import run_async
from utils.base_logger import get_logger
from utils.fireworkzz import get_fireworks_response, get_fireworks_response2
from utils.normal import can_reply_to_email, ensure_dir, load_yaml_file
import json
from credits_route.route import Credits


from utils.s3_utils import read_json_from_s3, upload_any_file
from zoho_routes.routes import send_zoho_email
from request_context import current_user_id

logger = get_logger(__name__)


def umail_get_sorted_lance_emails(connection, user_id, client_id):
    client = UmailLanceClient(user_id)
    recent_msg = client.get_selected_conv_from_lance(user_id, client_id)
    if not recent_msg:
        # print("NO RECENT MSG")
        return None

    all_messages = []
    sorted_conversations = []
    if isinstance(recent_msg, dict):
        values = recent_msg.items()
    else:
        values = recent_msg

    # open a cursor once and reuse
    with connection.cursor() as cursor:
        # print("connection", connection)
        for conv_id, messages_list in values:
            try:
                # 🔥 Deduplicate messages
                unique_messages = {}
                for msg in messages_list:
                    msg_id = (
                        msg.get("id") or f"{msg.get('timestamp')}-{msg.get('sender')}"
                    )
                    if msg_id not in unique_messages:
                        unique_messages[msg_id] = msg

                messages = list(unique_messages.values())
                channel = messages[0].get("source") if messages else "unknown"
                ticket_id = messages[0].get("ticket_id")

                assigned_id = ""
                assignee_full_name = ""

                if ticket_id:
                    cursor.execute(
                        "SELECT assignee FROM tickets WHERE tickets_id = %s",
                        (ticket_id,),
                    )
                    t_row = cursor.fetchone()
                    if t_row:
                        assigned_id = t_row[0]

                if assigned_id:
                    cursor.execute(
                        "SELECT first_name, last_name, email FROM users WHERE user_id = %s",
                        (assigned_id,),
                    )
                    names = cursor.fetchone()
                    if names:
                        first_name, last_name, assignee_email = names
                        if not first_name or first_name == "None":
                            first_name = assignee_email.split("@")[0]
                        if not last_name or last_name == "None":
                            last_name = ""
                        assignee_full_name = (first_name + " " + last_name).strip()

                all_messages.append(
                    {
                        "id": conv_id,
                        "channel": channel,
                        "messages": messages,
                        "assigned_name": assignee_full_name,
                    }
                )
            except Exception as e:
                # print(f"❌ Failed to read or parse {e}")
                continue

    if all_messages:
        sorted_conversations = sorted(
            all_messages,
            key=lambda conv: (
                max(msg.get("timestamp") for msg in conv.get("messages", []))
                if conv.get("messages")
                else ""
            ),
            reverse=False,
        )
    # print("len ofumail_get_sorted_lance_emails ", len(sorted_conversations))
    return sorted_conversations


def getselectedconv(conv_id, userid):
    try:
        connection = connect_to_rds()
        if connection is None:
            return {"error": "Database connection failed"}, 500
        cursor = connection.cursor()

        cursor.execute(
            "SELECT sender_id FROM messages WHERE conversation_id_fk = %s",
            (conv_id,),
        )
        client_id_row = cursor.fetchone()
        if client_id_row:
            client_id = client_id_row[0]
        else:
            return {
                "message": f"⚠️ No sender_id found for conversation_id {conv_id}"
            }, 404
    except Exception as e:
        return (
            {"message": f"❌ Error executing sender_id query: {e}"},
            500,
        )
    finally:
        if connection:
            connection.close()
    client = UmailLanceClient(userid)
    recent_msg = client.get_selected_conv_from_lance(userid, client_id)
    if recent_msg:
        return recent_msg[conv_id] or []
    else:
        return []


def normalize_ai_response(resp):
    if resp is None:
        return ""
    if isinstance(resp, dict):
        return json.dumps(resp, ensure_ascii=False)
    return str(resp).strip()


async def suggest_helper_base(userid, email_msg, umail_conversations, umail_bodies):
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
        businessdata = get_business_info(connection=connection, userid=userid)

        business_name = businessdata.get("BusinessName") if businessdata else ""
        business_address = businessdata.get("BillingAddress") if businessdata else ""
        business_website = businessdata.get("WebsiteUrl") if businessdata else ""
        # print("before ai check on suggest")
        try:

            # Call model to generate retrieval question
            task_credits = Credits(connection)

            base_query = await get_fireworks_response(
                user_message=filled_prompt,
                role="system",
                credits=task_credits,
                user_id=userid,
            )
            # print("basequery", type(base_query), base_query)

            # Parse retrieval question safely
            try:
                question_data = json.loads(base_query)
                ##print("json loads")
            except json.JSONDecodeError:
                import re

                json_text = re.search(r"\{.*\}", base_query, re.DOTALL)
                question_data = json.loads(json_text.group(0)) if json_text else {}
                ##print("re loads")
            ##print("type of questiondata", type(question_data), question_data)
            question_text = (
                question_data.get("question", "").strip() if question_data else ""
            )
            # print("queston text", question_text)
            # print("len of question", len(question_text))
            base_doc_ans = []
            if question_text:
                top_k = 3
                query_input = QueryInput(
                    user_id=userid, query_text=question_text, top_k=top_k
                )
                lance_client = LanceClient(user_id=userid, credits=task_credits)
                # print("lanceclient", lance_client)
                # results = run_async(lance_client.query_vector(query_input))
                results = await lance_client.mixed_query_vector(
                    query_input=query_input, sender_email=sender_email
                )

                if isinstance(results, list):
                    for r in results:
                        clean_text = r.get("text", "").encode().decode("unicode_escape")
                        base_doc_ans.append(clean_text)
                elif isinstance(results, str):
                    base_doc_ans = results

            # Build final prompt for AI reply
            prompt_template = pr_file.get("ai_reply_suggest")
            filled_prompt = (
                (prompt_template or "")
                .replace("{{email_msg}}", str(email_msg or ""))
                .replace(
                    "{{umail_conversations}}",
                    json.dumps(umail_bodies or [], ensure_ascii=False, indent=2),
                )
                .replace(
                    "{{base_doc_ans}}",
                    json.dumps(base_doc_ans or [], ensure_ascii=False, indent=2),
                )
                .replace("{{business_name}}", str(business_name or ""))
                .replace("{{business_address}}", str(business_address or ""))
                .replace("{{business_website}}", str(business_website or ""))
                .replace("{{sender_name}}", str(sender_name or ""))
            )

            print("base docs ans", len(base_doc_ans))

            # ai_reply = get_fireworks_response(filled_prompt, "system")
            ##print("print filled prompt",filled_prompt)
            ai_reply = normalize_ai_response(
                await get_fireworks_response2(
                    user_id=userid,
                    user_message=filled_prompt,
                    role="system",
                    credits=task_credits,
                )
            )

        except Exception as e:
            print(f"error in suggest_helper_base:{e} ")
            return None

        # print("AOI RTEPLy", ai_reply)
        if not ai_reply or ai_reply.lower() in ["none", "null", ""]:
            return None
        return ai_reply
        # return base_doc_ans
    except Exception as e:
        return {"error": f"suggest ai {e}"}
    finally:
        if connection:
            connection.close()


async def ai_suggest_helper(userid, currentmsg, conversation_id):
    umail_conversations = getselectedconv(conv_id=conversation_id, userid=userid)
    # print("umial", len(umail_conversations))
    umail_bodies = [item.get("body", "") for item in umail_conversations]
    ai_reply = await suggest_helper_base(
        userid=userid,
        email_msg=currentmsg,
        umail_conversations=umail_conversations,
        umail_bodies=umail_bodies,
    )
    if ai_reply:
        return ai_reply.strip()
    else:
        return None


async def send_pilot_messages(
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
    # print("🚀 [DEBUG] Starting send_pilot_messages function")

    try:
        if b_connection is None:
            connection = connect_to_rds()
        connection = b_connection
        cursor = connection.cursor()
        conv_folder = os.path.join(pathconfig.basepath, "messages", user_id, client_id)
        # print(f"📁 [DEBUG] Conversation folder: {conv_folder}")
        ensure_dir(conv_folder)
        file_name = f"{conversation_id}.json"
        conv_filepath = os.path.join(conv_folder, file_name)
        s3_conv_key = f"{user_id}/messages/{client_id}/{conversation_id}.json"
        # print(f"📁 [DEBUG] Conversation file path: {conv_filepath}")
        # print(f"📁 [DEBUG] S3 conversation key: {s3_conv_key}")

        # Load existing conversation data
        try:
            # print(f"📖 [DEBUG] Attempting to read existing conversation from S3...")
            raw_data = read_json_from_s3(s3_conv_key)
            input_data = raw_data.get("input_data", [])
            # print(f"📖 [DEBUG] Loaded {len(input_data)} existing messages")
        except Exception as e:
            # print(f"📖 [DEBUG] No existing conversation found, starting fresh: {e}")
            input_data = []

        # print(f"📖 [DEBUG] input_data length: {len(input_data)}")
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
        # print(f"📧 [DEBUG] Final message object created: {message}")

        # Send the message via appropriate channel
        sent_message_id, sent_thread_id = None, None

        # print(f"🚀 [DEBUG] Sending message via channel: {channel}")

        if channel == "gmail":
            # print("📧 [DEBUG] Processing Gmail send...")
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
                    # print("❌ [DEBUG] No valid messages found in input_data")
                    return {"error": "No valid messages found"}, 400
                latest_msg = max(
                    valid_messages,
                    key=lambda msg: datetime.fromisoformat(msg["timestamp"]),
                )
            else:
                # print("❌ [DEBUG] input_data is neither dict nor list")
                return {"error": "Invalid input_data format"}, 400
            latest_id = latest_msg["id"]
            # print(f"📧 [DEBUG] Latest message ID: {latest_id}")

            # Getting the reply subject
            latest_subject = latest_msg["subject"].strip()
            if not latest_subject.lower().startswith("re:"):
                reply_subject = f"Re: {latest_subject}"
            else:
                reply_subject = latest_subject
            # print(f"📧 [DEBUG] Reply subject: {reply_subject}")

            try:
                # print(f"📧 [DEBUG] Calling gmail_reply()...")
                sent_message_id = gmail_reply(
                    user_id,
                    to=client_email,
                    subject=reply_subject,
                    thread_id=thread_id,
                    body_text=text,
                    in_reply_to=latest_id,
                    connection=connection,
                )
                if not sent_message_id:
                    return {"error": "Gmail send failed"}, 500
                msg_id = sent_message_id
                message["id"] = sent_message_id
                # print(
                #     f"✅ [DEBUG] Gmail reply sent successfully, message_id: {sent_message_id}"
                # )

            except Exception as e:
                # print(f"❌ [DEBUG] Gmail reply failed: {e}")
                return {"error": f"Gmail send failed {e}"}, 500

        elif channel == "zoho":
            # print("📧 [DEBUG] Processing Zoho send...")
            try:
                # print(f"📧 [DEBUG] Calling send_zoho_email()...")
                response_payload, status_code = send_zoho_email(
                    user_id=user_id,
                    to_email=client_email,
                    subject=subject,
                    body_text=text,
                    from_user_email=user_email,
                )
                # print(
                #     f"📧 [DEBUG] Zoho response - status: {status_code}, payload: {response_payload}"
                # )

                if status_code in [200, 201]:
                    message_id = response_payload.get("message_id")
                    # print(
                    #     f"✅ [DEBUG] Zoho message sent successfully, message_id: {message_id}"
                    # )
                else:
                    # print(
                    #     f"❌ [DEBUG] Zoho send failed: {response_payload.get('error')}"
                    # )
                    return (
                        {"error": response_payload.get("error")},
                        status_code,
                    )

            except Exception as e:
                # print(f"❌ [DEBUG] Zoho send failed: {e}")
                return {"error": "Zoho send failed"}, 500

        else:
            # print(f"❌ [DEBUG] Unsupported channel: {channel}")
            return {"error": "Unsupported channel"}, 400

        # Database updates
        ##print("💾 [DEBUG] Starting database updates...")
        updated_date = datetime.now(timezone.utc).isoformat()
        created_date = updated_date
        # print(
        #     f"💾 [DEBUG] Timestamps - created: {created_date}, updated: {updated_date}"
        # )

        try:
            ##print("💾 [DEBUG] Updating existing ticket and thread...")
            cursor.execute(
                "UPDATE tickets SET updated_in = %s, status = %s WHERE conversation_id_fk = %s",
                (updated_date, "In-Progress", conversation_id),
            )
            ##print("✅ [DEBUG] Ticket updated")

            cursor.execute(
                "UPDATE threads SET last_message_at = %s WHERE conversation_id = %s",
                (updated_date, conversation_id),
            )
            ##print("✅ [DEBUG] Thread updated")

            cont_ref = f"{user_id}/messages/{client_id}/{conversation_id}.json"
            # print(f"💾 [DEBUG] Content reference: {cont_ref}")

            # print("💾 [DEBUG] Inserting message record...")
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
                    update_at,
                    sender_type
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                    channel,
                ),
            )
            # print("✅ [DEBUG] Message record inserted")

            connection.commit()
        # print("✅ [DEBUG] Database transaction committed")

        except Exception as e:
            connection.rollback()
            logger.error("Database operation failed — rolled back: %s", e)
            return {"error": "Database operation failed"}, 500

        # Update Conversation File
        # print("📄 [DEBUG] Updating conversation file...")
        try:
            if isinstance(input_data, dict):
                input_data = [input_data]
            input_data.append(message)
            conversation_data = {"input_data": input_data}
            # print(f"📄 [DEBUG] Total messages in conversation: {len(input_data)}")
            # print(f"conversation_data : {conversation_data}")
            # print(f"💾 [DEBUG] Writing to local file: {conv_filepath}")
            with open(conv_filepath, "w", encoding="utf-8") as f:
                json.dump(conversation_data, f, indent=2)

            # print(f"☁️ [DEBUG] Uploading to S3: {s3_conv_key}")
            upload_any_file(
                conv_filepath,
                user_id,
                type="messages",
                s3_key_C=s3_conv_key,
            )
        # print("✅ [DEBUG] Conversation file updated successfully")

        except Exception as e:
            # print(f"❌ [DEBUG] Failed to update conversation file: {e}")
            return {"error": "Failed to save conversation"}, 500

        # updating lancedb
        lance_data = conversation_data.get("input_data", [])
        client = UmailLanceClient(user_id)

        await client.embed_json_file_for_reply(
            lance_data, user_id, client_id, conversation_id
        )

        # Update Config File
        # print("⚙️ [DEBUG] Updating config file...")
        config_folder = os.path.join(
            pathconfig.basepath, "messages", user_id, client_id
        )
        ensure_dir(config_folder)
        config_filepath = os.path.join(config_folder, "config.json")

        s3_config_key = f"{user_id}/messages/{client_id}/config.json"
        try:
            config_data = read_json_from_s3(s3_config_key)
            # print(
            #     f"⚙️ [DEBUG] Existing config loaded with {len(config_data.get('conversations', []))} conversations"
            # )
        except Exception as e:
            # print(f"⚙️ [DEBUG] No config file found — creating new: {e}")
            config_data = {"userclients_id": client_id, "conversations": []}

        # Parse timestamp
        try:
            if updated_date.endswith("Z"):
                parsed_ts = datetime.fromisoformat(updated_date.replace("Z", "+00:00"))
            else:
                parsed_ts = datetime.fromisoformat(updated_date)
            # print(f"⚙️ [DEBUG] Parsed timestamp: {parsed_ts.isoformat()}")
        except Exception as e:
            # print(f"⚠️ [DEBUG] Could not parse updated_date '{updated_date}': {e}")
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
        # print(f"*****ticket_id 2 : {ticket_id}")
        if channel == "gmail":
            if sent_thread_id:
                updated_entry["thread_id"] = sent_thread_id
                # print(f"⚙️ [DEBUG] Using sent_thread_id: {sent_thread_id}")
            else:
                updated_entry["thread_id"] = thread_id
                # print(f"⚙️ [DEBUG] Using existing thread_id: {thread_id}")
        else:
            updated_entry["thread_id"] = ""
        # print("⚙️ [DEBUG] Non-Gmail channel, no thread_id")

        # print(f"⚙️ [DEBUG] Updated entry: {updated_entry}")

        # Update or add conversation in config
        conversation_exists = False
        for i, conv in enumerate(config_data.get("conversations", [])):
            if conv.get("conv_id") == conversation_id:
                config_data["conversations"][i] = updated_entry
                conversation_exists = True
                # print(f"⚙️ [DEBUG] Updated existing conversation at index {i}")
                break

        if not conversation_exists:
            config_data.setdefault("conversations", []).append(updated_entry)
        # print("⚙️ [DEBUG] Added new conversation to config")

        config_data["userclients_id"] = client_id
        # print(f"⚙️ [DEBUG] Calling update_config_file()...")
        update_config_file(user_id, client_id, config_data)
        # print("✅ [DEBUG] Config file updated successfully")

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
        # print(f"❌ [DEBUG] Unexpected error in send_messages(): {e}")
        import traceback

        # print(f"❌ [DEBUG] Full traceback: {traceback.format_exc()}")
        return {"error": "Internal server error"}
    finally:
        if b_connection is None and connection:
            connection.close()


async def helper_make_reply_email(userid=None, from_email=None, n_connection=None):
    connection = None  # always defined
    try:
        userid = userid

        if n_connection is None and connection is None:
            connection = connect_to_rds()
            # print("make connection creation", connection)
        else:
            # print("conn", connection)
            connection = n_connection

        clientid = get_users_clients_id(email=from_email, user_id=userid)
        if not clientid:
            # print("No client id")
            return None

        # print("max connection", connection)

        # sorted conversations
        sorted_conversations = umail_get_sorted_lance_emails(
            connection=connection, user_id=userid, client_id=clientid
        )
        if not sorted_conversations:
            # print("No sorted conversations")
            return None

        # collect all messages
        all_messages = []
        for conv in sorted_conversations:
            all_messages.extend(conv.get("messages", []))
        if not all_messages:
            # print("no all messages")
            return None
        # print("all msg", all_messages)

        latest_msg = all_messages[-1]
        # print("latest message", latest_msg)
        client_email = latest_msg.get("from")

        if not can_reply_to_email(client_email):
            return False, "its not a valid email to make an auto reply"

        if latest_msg.get("direction") == "inbound":
            serv = GmailService(user_id=userid)
            thread_id = latest_msg.get("thread_id")
            res = await serv.get_thread_last_message_direction(thread_id=thread_id)
            if res.get("direction") != "inbound":
                return False, "cant send because last message of user is outbound"
            msgs_same_thread = (
                [m for m in all_messages if m.get("thread_id") == thread_id]
                if thread_id
                else all_messages  # fallback if no thread ID
            )

            # Re-sort after filtering
            msgs_same_thread = sorted(msgs_same_thread, key=lambda x: x["timestamp"])
            ai_reply = await suggest_helper_base(
                userid=userid,
                email_msg=latest_msg["body"],
                umail_conversations=msgs_same_thread,
                umail_bodies=[msg.get("body") for msg in msgs_same_thread],
            )
            # print("aPA reply from gmail ", ai_reply)
            # logger.info("apa reply %s", ai_reply)
            # print("values", latest_msg["to"], latest_msg["from"])
            if ai_reply:
                send_val = await send_pilot_messages(
                    user_id=userid,
                    channel="gmail",
                    text=ai_reply,
                    conversation_id=latest_msg["conversation_id"],
                    b_connection=connection,
                    client_id=clientid,
                    user_email=latest_msg["to"],
                    client_email=latest_msg["from"],
                    subject=latest_msg["subject"],
                    thread_id=latest_msg["thread_id"],
                    ticket_id=latest_msg["ticket_id"],
                    ticket_name=latest_msg["ticket_name"],
                    is_reply=True,
                )
                if send_val:
                    # print("sent val ok")
                    return send_val, "sent email success"
                else:
                    # print("sent val fail")
                    return False, "sending email to user failed"
            else:
                # print("cant generate suggest")
                return False, "failed to generate emailbody"
        else:
            # print("sent val outbound")
            return False, "cant send because last message of user is outbound"

    except Exception as e:
        # print("ERROR at helper make reply email", e)
        logger.info("ERROR %s", e)
        return None
    finally:
        if n_connection is None and connection is not None:
            connection.close()
