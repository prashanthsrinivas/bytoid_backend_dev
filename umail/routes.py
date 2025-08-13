from flask import Flask, request, jsonify, Blueprint, Response, session
from datetime import datetime, timezone
from gmail_route.routes import fetch_gmail_messages
from zoho_routes.routes import fetch_zoho_emails
from cust_helpers import pathconfig
from utils.normal import ensure_dir, load_yaml_file
import json
from create_db import connect_to_rds
import os
from utils.fireworkzz import get_fireworks_response
from utils.s3_utils import upload_any_file, read_json_from_s3, list_all_files
import uuid
from gmail_route.routes import gmail_reply, send_mail
import re
import yaml
from zoho_routes.routes import send_zoho_email
import pytz





umail_bp = Blueprint("umail", __name__)



@umail_bp.route("/get_all_messages/<user_id>", methods=["GET"])
def getall(user_id):

    timestamp = datetime.now(timezone.utc)
    date_str = timestamp.strftime("%Y-%m-%d")
    file_loc = f"cust_helpers/messages/{user_id}/{date_str}"
    gmail = fetch_gmail_messages(user_id)
    zoho = fetch_zoho_emails(user_id)

    analyze_and_collect_messages(user_id)

    return "OK"


def get_existing_messages(user_id):
    user_folder = os.path.join(pathconfig.basepath, "messages", user_id)
    ensure_dir(user_folder)

    timestamp = datetime.now(timezone.utc)
    date_str = timestamp.strftime("%Y-%m-%d")
    filename = f"{date_str}.json"
    user_filepath = os.path.join(user_folder, filename)

    with open(user_filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    grouped_messages = data.get("input_data", {})



@umail_bp.route("/analyze_and_collect_messages/<user_id>", methods=["GET"])
def analyze_and_collect_messages(user_id):
    
    user_folder = os.path.join(pathconfig.basepath, "messages", user_id)
    ensure_dir(user_folder)

    timestamp = datetime.now(timezone.utc)
    date_str = timestamp.strftime("%Y-%m-%d")
    filename = f"{date_str}.json"
    user_filepath = os.path.join(user_folder, filename)

    try:
        with open(user_filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {"input_data": {}}
        with open(user_filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    grouped_messages = data.get("input_data", {})

    new_messages = []

    for client_id, channel_data in grouped_messages.items():
        connection = connect_to_rds()
        if connection is None:
            print("❌ Failed to connect to RDS")
            return None

        cursor = connection.cursor()
        cursor.execute(
            "SELECT email_id FROM users_clients WHERE users_clients_id = %s", (client_id,)
        )
        client_row = cursor.fetchone()
        if not client_row:
            print("❌ Error: User not found in users_clients table")
            continue

        client_email = client_row[0]

        config_folder = os.path.join(pathconfig.basepath, "messages", user_id, client_id)
        ensure_dir(config_folder)

        config_filepath = os.path.join(config_folder, "config.json")
        try:
            with open(config_filepath, "r", encoding="utf-8") as f:
                config_data = json.load(f)
        except FileNotFoundError:
            config_data = {}
            print("⚠️ Config file not found")

        existing_channels = {
            convo.get("channel")
            for convo in config_data.get("conversations", [])
            if convo.get("channel")
        }

        for channel, channel_msgs in channel_data.items():
            channel_msgs.sort(key=lambda x: x.get("timestamp", ""))  # optional
            latest_msg = channel_msgs[-1] if channel_msgs else None

            output_filename = f"{channel}_new_messages.json"
            output_path = os.path.join(config_folder, output_filename)

            new_msg_data = {}
            try:
                with open(output_path, "r", encoding="utf-8") as f:
                    new_msg_data = json.load(f)
            except Exception as e:
                print(f"⚠️ Couldn't read existing messages: {e}")

            existing_new_msg = {
                msg.get("msg_id"): msg for msg in new_msg_data.get("new_messages", [])
            }
            merged_messages = new_msg_data.get("new_messages", [])

            for m in channel_msgs:
                msg_id = m.get("id")
                
                cursor.execute("SELECT 1 FROM messages WHERE message_id = %s", (msg_id,))
                m_id = cursor.fetchone()
                if m_id:
                    continue
                
                new_msg = {
                    "msg_id": msg_id,
                    "body": m.get("body"),
                    "from": "client" if m.get("from") == client_email else "user",
                    "to": "client" if m.get("to") == client_email else "user",
                    "date": m.get("timestamp"),
                    # "status": classification,
                    "channel": channel,
                }
                merged_messages.append(new_msg)

            with open(output_path, "w", encoding="utf-8") as f:
                json.dump({"new_messages": merged_messages}, f, indent=2)
            
            subjects = generate_subject(user_id, output_path, channel)
            print("successfuly generated subjects")

            grouped_messages = append_subject_to_messages(grouped_messages,channel,subjects,user_id,existing_new_msg)
            print("successfuly appended subjects")

    return new_messages

            

@umail_bp.route("/subject_summarisations/<user_id>", methods=["POST"])
def generate_subject(user_id, output_path,channel):
    try:
        if not user_id or not output_path:
            print("❌ Missing user_id or filename")
            return None

        with open(output_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        all_messages = data.get("new_messages", [])
        if channel:
            filtered_messages = [msg for msg in all_messages if msg.get("channel") == channel]
        else:
            filtered_messages = all_messages

        if not filtered_messages:
            return []
        
        # Load prompt + workflow YAML
        yaml_data = load_yaml_file(path=pathconfig.conv_template)
        update_prompt_template = yaml_data.get("summarize_message_body")
        if not update_prompt_template:
            print("❌ Prompt 'summarize_message_body' not found in template")
            return None

        # Inject message data into prompt
        message_payload = json.dumps(filtered_messages, indent=2)
        full_prompt = update_prompt_template.replace("{full_text_message_body}", message_payload)

        # Generate YAML output from model
        modified_yaml = get_fireworks_response(full_prompt, role="system")


        try:
            parsed_yaml = yaml.safe_load(modified_yaml.strip())
            if "subject_groups" in parsed_yaml:
                return parsed_yaml["subject_groups"]
            else:
                print("⚠️ Key 'subject_groups' missing after parsing")
                return None
        except Exception as e:
            print(f"🔥 YAML parse failed: {e}")
            return None

        try:
            if yaml_match:
                parsed_yaml = yaml.safe_load(yaml_match.group(0))
            else:
                raise ValueError("Could not extract valid subject_groups YAML block.")

            if "subject_groups" not in parsed_yaml:
                print("⚠️ Key 'subject_groups' missing after parsing")
                return None

            return parsed_yaml["subject_groups"]

        except Exception as e:
            print(f"🔥 Exception during summarisation: {str(e)}")
            return None
    except Exception as e:
            print(f"🔥 Exception during summarisation: {str(e)}")
            return None


def append_subject_to_messages(grouped_messages,channel, subjects, user_id,existing_new_msg):
    # Build a lookup of message_id → subject
    subject_map = {}
    for group in subjects:
        subject = group["summary"]
        for mid in group["message_ids"]:
            subject_map[str(mid)] = subject

    updated_date = datetime.now(timezone.utc).isoformat()
    created_date = updated_date

    connection = connect_to_rds()
    if connection is None:
        print("⚠️ DB connection failed inside append_subject_to_messages")
        return None

    cursor = connection.cursor()

    # Iterate through grouped_messages structure
    for client_id, channels in grouped_messages.items():
        s3_config_key = f"{user_id}/messages/{client_id}/config.json"

        try:
           
            config_data = read_json_from_s3(s3_config_key)
            if config_data is None:
                config_data = {}

        except FileNotFoundError:
            config_data = {}
            print("no config file")

        existing_thread_ids = [
                conv.get("thread_id")
                for conv in config_data.get("conversations", [])
                if "thread_id" in conv and conv.get("thread_id")
            ]
        # for channel, messages in channels.items():
        messages = grouped_messages.get(client_id, {}).get(channel, [])

        if channel == "zoho":
                # Build subject lookup per client
                    config_subject_lookup = {}
                    if config_data:
                        for conv in config_data.get("conversations", []):
                            if conv.get("channel", "").lower() != "zoho":
                                continue
                            conf_subj = conv.get("subject", "")
                            conf_ticket_name = conv.get("ticket_name", "")
                            conf_conv_id = conv.get("conv_id", "")
                            conf_ticket_id = conv.get("ticket_id", "")
                            if conf_subj:
                                config_subject_lookup[conf_subj] = {
                                    "conv_id": conf_conv_id,
                                    "ticket_id": conf_ticket_id,
                                    "ticket_name": conf_ticket_name,
                                }
        for msg in messages:
                        
                        msg_id = msg.get("id")

                        cursor.execute("SELECT 1 FROM messages WHERE message_id = %s", (msg_id,))
                        m_id = cursor.fetchone()
                        if m_id:
                            continue


                        subject = msg.get("subject", "") 
                        is_reply = subject.lower().startswith("re:") or "wrote:" in msg.get("summary", "").lower()

                        # 1. for zoho reply
                        if channel == "zoho" and is_reply:
                    
                            normalized_subject = re.sub(r"^re:\s*", "", subject, flags=re.IGNORECASE)
                            config_thread = config_subject_lookup.get(normalized_subject)
                            if config_thread:
                                c_id = config_thread["conv_id"]
                                t_id = config_thread["ticket_id"]
                                t_name = config_thread["ticket_name"]
                                msg["conversation_id"] = c_id
                                msg["ticket_id"] = t_id
                                msg["ticket_name"] = t_name


                                cursor.execute(
                                        "UPDATE tickets SET updated_in = %s, status = %s WHERE conversation_id_fk = %s",
                                        (updated_date, "In-Progress", c_id)
                                    )

                                cursor.execute(
                                        "UPDATE threads SET last_message_at = %s WHERE conversation_id = %s",
                                        (updated_date, c_id)
                                    )

                                cursor.execute(
                                        "UPDATE messages SET update_at = %s WHERE conversation_id = %s",
                                        (updated_date, c_id)
                                    )

                                grouped_messages[client_id][channel] = messages
                                update_or_create_conversation_file(grouped_messages, user_id, client_id, channel)


                                try:
                                                    if updated_date.endswith('Z'):
                                                        parsed_ts = datetime.fromisoformat(updated_date.replace("Z", "+00:00"))
                                                    else:
                                                        parsed_ts = datetime.fromisoformat(updated_date)
                                except Exception as e:
                                                    print(f"[WARN] Could not parse updated_date '{updated_date}': {e}")
                                                    parsed_ts = datetime.now(timezone.utc)

                                                    
                                for i, conv in enumerate(config_data.get("conversations", [])):
                                    if conv.get("conv_id") == c_id:
                                        config_data["conversations"][i]["updated_date"] = updated_date
                                        config_data["conversations"][i]["parsed_timestamp"] = parsed_ts.isoformat()
                                        break
                                                
                                update_config_file(user_id, client_id, config_data)
                                connection.commit()

                                continue
                        
                        
                        thread_id = msg.get("thread_id")
                        direction = msg.get("direction")

                        # 2. for gmail reply
                        if thread_id:
                            print(f" ********* thread_id : {thread_id}")
                            mesag_id = msg.get("id")
                            print(f" ********* mesag_id : {mesag_id}")
                            msg_body = msg.get("body")
                            print(f" ********* mesag_id : {msg_body}")

                            found_existing_conversation = False
                            if config_data:

                                matching_conv = next(
                                    (conv for conv in config_data.get("conversations", [])
                                    if conv.get("thread_id") == thread_id ),
                                    None
                                )
                                if matching_conv:
                                        a=matching_conv.get("thread_id")
                                        b=thread_id
                                        print(f"a is :{a}; b is :{b}")
                    
                                        conversation_id = matching_conv.get("conv_id")   
                                        print(f"✅ Found existing thread! Using conversation_id: {conversation_id}")
                                        print(f"✅ Message will be added to existing conversation, not creating new one")                      
                                        if conversation_id:
                                            found_existing_conversation = True 
                                            msg["conversation_id"] = conversation_id
                                            cursor.execute(
                                                "SELECT tickets_id, ticket_name FROM tickets WHERE conversation_id_fk = %s",
                                                (conversation_id)
                                            )
                                            ticket_row = cursor.fetchone()
                                            ticket_id = ticket_row[0]
                                            ticket_name = ticket_row[1]
                                            if ticket_row:
                                                msg["ticket_id"] = ticket_id
                                                msg["ticket_name"] = ticket_name


                                                cursor.execute(
                                                    "UPDATE tickets SET updated_in = %s, status = %s WHERE conversation_id_fk = %s",
                                                    (updated_date, "In-Progress", conversation_id)
                                                )

                                                cursor.execute(
                                                    "UPDATE threads SET last_message_at = %s WHERE conversation_id = %s",
                                                    (updated_date, conversation_id)
                                                )
                                        
                                                # cursor.execute(
                                                #     "UPDATE messages SET update_at = %s WHERE conversation_id = %s",
                                                #     (updated_date, conversation_id)
                                                # )

                                                cursor.execute("SELECT 1 FROM messages WHERE message_id = %s", (msg_id,))
                                                if not cursor.fetchone():
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
                                                                        "ref",
                                                                        "inbound",
                                                                        subject,
                                                                        created_date,
                                                                        updated_date
                                                                    )
                                                                )
                                                                                
                                                print(f"message is {msg}")
                                                grouped_messages[client_id][channel] = messages
                                                update_or_create_conversation_file(grouped_messages, user_id, client_id, channel)                                                                      

                                               
                                                try:
                                                    if updated_date.endswith('Z'):
                                                        parsed_ts = datetime.fromisoformat(updated_date.replace("Z", "+00:00"))
                                                    else:
                                                        parsed_ts = datetime.fromisoformat(updated_date)
                                                except Exception as e:
                                                    print(f"[WARN] Could not parse updated_date '{updated_date}': {e}")
                                                    parsed_ts = datetime.now(timezone.utc)

                                                for i, conv in enumerate(config_data.get("conversations", [])):
                                                    if conv.get("conv_id") == conversation_id:
                                                        config_data["conversations"][i]["updated_date"] = updated_date
                                                        config_data["conversations"][i]["parsed_timestamp"] = parsed_ts.isoformat()
                                                        break

                                                update_config_file(user_id, client_id, config_data)

                                                connection.commit()
                                                # continue
                                                break
                                            else:
                                                print(f"⚠️ No ticket found for conversation: {conversation_id}")
                                    
                            if found_existing_conversation:
                                print(f"✅ {found_existing_conversation} Gmail reply processed successfully for existing conversation")
                                continue 

                        # Thread does not exist — create new thread and optionally ticket
                        new_conversation_id = str(uuid.uuid4())
                        msg["conversation_id"] = new_conversation_id

                        cursor.execute(
                                """
                                INSERT INTO threads (conversation_id, started_at, status, last_message_at)
                                VALUES (%s, %s, %s, %s)
                                """,
                                (new_conversation_id, created_date, "Open", updated_date)
                            )


                        # 3. new for inbound msg
                        if direction == "inbound":
                            new_ticket_id = str(uuid.uuid4())
                        

                            ticket_name = subject_map.get(str(msg_id))
                            subject = msg.get("subject")
                            msg["ticket_id"] = new_ticket_id
                            msg["ticket_name"] = ticket_name
                            msg["conversation_id"] = new_conversation_id

                            timestamp = msg.get("timestamp")
                            dt = datetime.fromisoformat(timestamp)
                            dt_utc = dt.astimezone(pytz.UTC)
                            created_in = dt_utc.strftime("%Y-%m-%d %H:%M:%S")
                            updated_in = created_in

                            

                            cursor.execute("SELECT 1 FROM tickets WHERE tickets_id = %s", (new_ticket_id,))
                            if not cursor.fetchone():
                                
                                cursor.execute(
                                    "INSERT INTO tickets (tickets_id, ticket_name, conversation_id_fk,status,priority,created_in,updated_in) VALUES (%s, %s, %s,%s,%s,%s,%s)",
                                    (new_ticket_id, ticket_name, new_conversation_id,"Open","Medium",created_in,updated_in)
                                )

                                assigned_id = str(uuid.uuid4())  # Generate UUID for assigned_id
                                cursor.execute(
                                    """
                                    INSERT INTO assigned (assigned_id, user_id_fk, users_clients_id_fk, ticket_id_fk)
                                    VALUES (%s, %s, %s, %s)
                                    """,
                                    (assigned_id, user_id, client_id, new_ticket_id)
                                )

                                
                            cursor.execute(
                                """
                                UPDATE threads
                                SET ticket_id_fk = %s
                                WHERE conversation_id = %s
                                """,
                                (new_ticket_id, new_conversation_id)
                            )

                            cursor.execute("SELECT 1 FROM messages WHERE message_id = %s", (msg_id,))
                            existing = cursor.fetchone()
                            print("Existing message check:", existing)
                            print("Inserting message_id:", msg_id)
                            if not existing:  
                               
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
                                        new_conversation_id,
                                        client_id,
                                        "ref",
                                        "inbound",
                                        subject,
                                        created_date,
                                        updated_date
                                    )
                                )


                            # Update config immediately
                            try:
                                if updated_date.endswith('Z'):
                                    parsed_ts = datetime.fromisoformat(updated_date.replace("Z", "+00:00"))
                                else:
                                    parsed_ts = datetime.fromisoformat(updated_date)
                            except Exception as e:
                                    print(f"[WARN] Could not parse updated_date '{updated_date}': {e}")
                            
                            updated_entry = {
                                "conv_id": new_conversation_id,
                                "ticket_id": new_ticket_id,
                                "ticket_name": ticket_name,
                                "subject": subject_map.get(str(msg_id)),
                                "channel": channel,
                                "updated_date": updated_date,
                                "parsed_timestamp": parsed_ts.isoformat()
                            }
                            if channel == "gmail" and thread_id:
                                updated_entry["thread_id"] = thread_id

                            config_data.setdefault("userclients_id", client_id)
                            config_data.setdefault("conversations", []).append(updated_entry)

                     

                        # 4. for new outbound msg
                        else:
                            msg["ticket_id"] = None
                            msg["ticket_name"] = None
                            msg["conversation_id"] = new_conversation_id

                            cursor.execute(
                                """
                                UPDATE threads
                                SET ticket_id_fk = %s
                                WHERE conversation_id = %s
                                """,
                                (None, new_conversation_id)
                            )

                            cursor.execute("SELECT 1 FROM messages WHERE message_id = %s", (msg_id,))
                            if not cursor.fetchone():
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
                                        new_conversation_id,
                                        client_id,
                                        "ref",
                                        "outbound",
                                        subject,
                                        created_date,
                                        updated_date
                                    )
                                )

                            updated_entry = {
                                "conv_id": new_conversation_id,
                                "ticket_id": new_ticket_id,
                                "ticket_name": ticket_name,
                                "subject": subject_map.get(str(msg_id)),
                                "channel": channel,
                                "updated_date": datetime.now(timezone.utc).isoformat()
                            }
                            if channel == "gmail" and thread_id:
                                updated_entry["thread_id"] = thread_id
                            
                            config_data.setdefault("userclients_id", client_id)
                            config_data.setdefault("conversations", []).append(updated_entry)
                        
                        grouped_messages[client_id][channel] = messages
                        update_or_create_conversation_file(grouped_messages,user_id,client_id,channel)

                        update_config_file(user_id, client_id, config_data)
                            

                        connection.commit()
                    
    connection.close()
    return grouped_messages


def update_config_file(user_id, client_id, config_data):
    config_folder = os.path.join(pathconfig.basepath, "messages", user_id, client_id)
    ensure_dir(config_folder)
    config_filepath = os.path.join(config_folder, "config.json")
    s3_config_key = f"{user_id}/messages/{client_id}/config.json"

    s3_data = read_json_from_s3(s3_config_key)
    input_data = s3_data.get("conversations", [])

    # Save locally
    with open(config_filepath, "w", encoding="utf-8") as f:
        json.dump(config_data, f, indent=2)

    # Upload to S3
    upload_any_file(
        config_filepath,
        user_id,
        type="messages",
        s3_key_C=s3_config_key,
    )


def update_or_create_conversation_file(grouped_messages, user_id, client_id, channel):
    prefix = f"{user_id}/messages/{client_id}"
    config_key = f"{prefix}/config.json"
    config_data = read_json_from_s3(config_key)
    print("config_data is : {config_data}")

    # Pull existing conversation IDs for the specified channel
    existing_conversations = {
        conv["conv_id"] for conv in config_data.get("conversations", [])
        if conv.get("channel") == channel and conv.get("conv_id")
    }
    print(f"existing_conversations : {existing_conversations}")
    channel_messages = grouped_messages.get(client_id, {}).get(channel, [])
    if not channel_messages:
        print(f"[INFO] No messages found for client={client_id}, channel={channel}")
        return

    # Group messages by conversation_id
    conversation_groups = {}
    for msg in channel_messages:
        conv_id = msg.get("conversation_id")
        print(f"mesage conv_id to be checked: {conv_id}")
        if conv_id:
            conversation_groups.setdefault(conv_id, []).append(msg)

    for conv_id, messages in conversation_groups.items():
        file_key = f"{prefix}/{conv_id}.json"
        conv_folder = os.path.join(
                            pathconfig.basepath, "messages", user_id, client_id
                        )
        ensure_dir(conv_folder)
        conv_file_name = f"{conv_id}.json"
        conv_filepath = os.path.join(conv_folder, conv_file_name)
        s3_config_key = f"{user_id}/messages/{client_id}/{conv_id}.json"


        if conv_id in existing_conversations:
            print(f"found conv id :{conv_id} in existing")
            raw_data = read_json_from_s3(file_key)
            input_data = raw_data.get("input_data", [])
            messages = normalize_datetimes(messages)   # check this later
            input_data.extend(messages)

            with open(conv_filepath, "w", encoding="utf-8") as f:
                json.dump({"input_data": input_data}, f, indent=2)


            upload_any_file(
                                    conv_filepath,
                                    user_id,
                                    type="messages",
                                    s3_key_C=s3_config_key,
                                ) 
        else:
            print(f"coud not found conv id :{conv_id} in existing")

            with open(conv_filepath, "w", encoding="utf-8") as f:
                json.dump({"input_data": messages}, f, indent=2)

            upload_any_file(
                                    conv_filepath,
                                    user_id,
                                    type="messages",
                                    s3_key_C=s3_config_key,
                                )            

def normalize_datetimes(obj):
    if isinstance(obj, dict):
        return {
            k: normalize_datetimes(v)
            for k, v in obj.items()
        }
    elif isinstance(obj, list):
        return [normalize_datetimes(item) for item in obj]
    elif isinstance(obj, datetime):
        return obj.isoformat()
    else:
        return obj

def get_latest_convo_info(config):
    """
    Get the latest conversation from a config file based on parsed_timestamp
    """

    if not config or "conversations" not in config:
        return None

    config_data = config.get("conversations", [])
    client_id=config.get("userclients_id", [])

    if not config_data:
        print("[DEBUG] input_data is empty")
        return None

    conversations = {}

    for msg in config_data:
        conv_id = msg.get("conv_id")
        ts_str = msg.get("parsed_timestamp")

        if not conv_id or not ts_str:
            # print(f"[DEBUG] Skipping message due to missing conv_id or parsed_timestamp")
            continue

        try:
            msg_ts = datetime.fromisoformat(ts_str)

            if conv_id not in conversations or msg_ts > conversations[conv_id]["parsed_timestamp"]:
                conversations[conv_id] = {
                    "message": msg,
                    "parsed_timestamp": msg_ts
                }
        except Exception as e:
            print(f"[WARN] Failed to parse parsed_timestamp '{ts_str}': {e}")
            continue

    if not conversations:
        print("[DEBUG] No valid conversations found after grouping")
        return None

    latest_conv = max(conversations.values(), key=lambda x: x["parsed_timestamp"])
    latest_msg = latest_conv["message"]


    return {
        "updated_date": latest_conv["parsed_timestamp"].isoformat(),
        "conv_id": latest_msg.get("conv_id"),
        "client_id": client_id,  
    }


def extract_unique_client_folders(file_list, base_prefix):
    if not file_list:
        return []

    client_ids = set()
    for obj in file_list:
        key = obj.get("Key", "")
        if key.startswith(base_prefix):
            parts = key[len(base_prefix):].split("/")
            if parts and parts[0]:
                client_ids.add(parts[0])
    return list(client_ids)

@umail_bp.route("/conversations/<user_id>", methods=["GET"])
def get_latest_conversations(user_id):
    """
    Get the latest conversation from each client's config file and load full conversation data
    """
    client_prefix = f"{user_id}/messages/"

    raw_file_list = list_all_files(client_prefix)
    client_ids = extract_unique_client_folders(raw_file_list, client_prefix)

    conversations = []
    disp_messages = []
    
    for client_id in client_ids:
        config_path = f"{client_prefix}{client_id}/config.json"
        
        try:
            config = read_json_from_s3(config_path)
            recent_msg = get_latest_convo_info(config)
            
            if recent_msg:
                conversations.append(recent_msg)
                
                conv_id = recent_msg['conv_id']
                convo_path = f"{client_prefix}{client_id}/{conv_id}.json"
                
                try:
                    convo_data = read_json_from_s3(convo_path)
                    convo_messages = convo_data.get("input_data", [])

                    if convo_messages:
                        latest_msg_in_conv = None
                        latest_timestamp = None
                        
                        for msg in convo_messages:
                            timestamp_str = msg.get("timestamp")
                            if not timestamp_str:
                                continue
                            try:
                                if timestamp_str.endswith('Z'):
                                    msg_ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                                else:
                                    msg_ts = datetime.fromisoformat(timestamp_str)
                                
                                if latest_timestamp is None or msg_ts > latest_timestamp:
                                    latest_timestamp = msg_ts
                                    latest_msg_in_conv = msg
                            except Exception as e:
                                print(f"[WARN] Failed to parse message timestamp: {e}")
                                continue
                        
                        if latest_msg_in_conv:
                            now = datetime.now(timezone.utc)
                            time_diff = now - latest_timestamp
                            
                            if time_diff.days > 0:
                                relative_time = f"{time_diff.days} days ago"
                            elif time_diff.seconds > 3600:
                                relative_time = f"{time_diff.seconds // 3600} hours ago"
                            elif time_diff.seconds > 60:
                                relative_time = f"{time_diff.seconds // 60} minutes ago"
                            else:
                                relative_time = "Just now"
                            
                            user_email = latest_msg_in_conv.get("user_id", "Unknown")
                            from_email = latest_msg_in_conv.get("from", "")
                            to_email = latest_msg_in_conv.get("to", "")
                            
                            contact_email = from_email if from_email != user_email else to_email
                            contact_name = contact_email.split('@')[0] if contact_email else "Unknown"
                            has_unread = any(m.get("status") == "received" for m in convo_messages)
                            
                            disp_message = {
                                "contact_id": contact_email,
                                "name": contact_name,
                                "lastMessage": latest_msg_in_conv.get("body", "")[:100],
                                "timestamp": relative_time,
                                "isoTimestamp": latest_msg_in_conv.get("timestamp"),
                                "unread": has_unread,
                                "channel": latest_msg_in_conv.get("source"),
                                "subject": latest_msg_in_conv.get("subject"),
                                "conv_id": conv_id,
                                "ticket_id": latest_msg_in_conv.get("ticket_id"),
                                "ticket_name": latest_msg_in_conv.get("ticket_name"),
                                "full_conversation": convo_messages
                            }
                            
                            disp_messages.append(disp_message)
                    
                except Exception as e:
                    print(f"  [WARN] Failed to read conversation {conv_id}: {e}")
            else:
                print(f"  [INFO] No conversations found in config for client: {client_id}")
                
        except Exception as e:
            print(f"  [WARN] Skipping config for client {client_id}: {e}")
            continue
    
    disp_messages.sort(key=lambda x: x['isoTimestamp'], reverse=True)    
    return disp_messages


def get_conv_order(config):
    """
    Return sorted list of conv_ids based on parsed_timestamp
    """

    if not config or "conversations" not in config:
        return None

    config_data = config.get("conversations", [])

    if not config_data:
        return None

    conversations = []

    for msg in config_data:
        conv_id = msg.get("conv_id")
        ts_str = msg.get("parsed_timestamp")

        if not conv_id or not ts_str:
            continue

        try:
            msg_ts = datetime.fromisoformat(ts_str)
            conversations.append((conv_id, msg_ts))
        except Exception as e:
            print(f"[WARN] Failed to parse parsed_timestamp '{ts_str}': {e}")
            continue

    if not conversations:
        print("[DEBUG] No valid conversations found after timestamp parsing")
        return None

    sorted_convos = sorted(conversations, key=lambda x: x[1], reverse=True)
    sorted_ids = [conv_id for conv_id, _ in sorted_convos]

    return sorted_ids



@umail_bp.route("/conversations/<conversation_id>/<user_id>", methods=["GET"])
def get_selected_conv(conversation_id, user_id):

    try:
        connection = connect_to_rds()
        if connection is None:
            return jsonify({"error": "Database connection failed"}), 500
        cursor = connection.cursor()

        try:
            cursor.execute(
                "SELECT sender_id FROM messages WHERE conversation_id_fk = %s",
                (conversation_id,)
            )
            client_id_row = cursor.fetchone()
            if client_id_row is None:
                print(f"⚠️ No sender_id found for conversation_id {conversation_id}")
                return jsonify({"error": "Conversation not found"}), 404
            client_id = client_id_row[0]
        except Exception as e:
            print(f"❌ Error executing sender_id query: {e}")

        config_path = f"{user_id}/messages/{client_id}/config.json"
        try:
            config = read_json_from_s3(config_path)
        except Exception as e:
            return jsonify({"error": "Failed to load config"}), 500

        try:
            recent_msg = get_conv_order(config)
        except Exception as e:
            return jsonify({"error": "Invalid config format"}), 500

        messages = []
        for conv in recent_msg:
            try:
                convo_path = f"{user_id}/messages/{client_id}/{conv}.json"
                convo_data = read_json_from_s3(convo_path)
                convo_messages = convo_data.get("input_data", [])

                channel = convo_messages[0].get("source") if convo_messages else "unknown"
                messages.append({
                    "id": conv,
                    "channel": channel,
                    "messages": convo_messages
                })

            except Exception as e:
                print(f"❌ Failed to read or parse {convo_path}: {e}")
                continue
        return jsonify(messages)

    except Exception as e:
        print(f"❌ Unexpected error in get_selected_conv(): {e}")
        return jsonify({"error": "Internal server error"}), 500


    
@umail_bp.route("/start-conversation", methods=["POST"])
def start_conversation():
        
        data = request.get_json() or {}
        user_id = data.get("user_id")
        client_id = data.get("contact_id")


        if not user_id or not client_id:
            return jsonify({"error": "Missing user_id or contact_id"}), 400

        try:
            connection = connect_to_rds()
            if connection is None:
                return jsonify({"error": "Database connection failed"}), 500
            cursor = connection.cursor()

            config_path = f"{user_id}/messages/{client_id}/config.json"
            config = None
            try:
                config = read_json_from_s3(config_path)
            except Exception as e:
                print(f"[WARN] → Config not found for {client_id}, returning minimal response")
            
            if config is None:
                return jsonify({
                    "identities": [],
                    "status": "new",
                    "conversationId": client_id,
                    "messages": []
                }), 200

            try:
                recent_msg = get_conv_order(config)
            except Exception as e:
                return jsonify({"error": "Invalid config format"}), 500

            messages = []
            for conv in recent_msg:
                try:
                    convo_path = f"{user_id}/messages/{client_id}/{conv}.json"
                    convo_data = read_json_from_s3(convo_path)
                    convo_messages = convo_data.get("input_data", [])
                    channel = convo_messages[0].get("source") if convo_messages else "unknown"

                    messages.append({
                        "id": conv,
                        "channel": channel,
                        "messages": convo_messages
                    })

                except Exception as e:
                    print(f"❌ Failed to read or parse {convo_path}: {e}")
                    continue

            # return jsonify(messages)
            return jsonify({
                "identities": config.get("identities", []),
                "status": "existing",
                "conversationId": client_id,
                "messages": messages  # this is ConversationThread[]
            }), 200



        except Exception as e:
            print(f"❌ Unexpected error in get_selected_conv(): {e}")
            return jsonify({"error": "Internal server error"}), 500



def match_email_to_channel(email, channel):
    """Returns True if the email matches the given channel."""
    if not email or "@" not in email:
        return False
    domain = email.lower().split("@")[1]
    return channel.lower() in domain


@umail_bp.route("/send-reply", methods=["POST"])
def send_messages():

    try:
        data = request.json
        user_id = data.get("user_id")
        channel = data.get("channel")
        text = data.get("text")
        conversation_id = data.get("conversation_id")
        contact_id = data.get("contact_id")


        

        if not all([user_id, channel, text]):
            return jsonify({"error": "Missing required payload fields"}), 400

        connection = connect_to_rds()
        if connection is None:
            return jsonify({"error": "Database connection failed"}), 500

        cursor = connection.cursor()

        is_reply = False
        client_id = None
        thread_id = None

         # getting client id
        try:
            cursor.execute(
                "SELECT sender_id FROM messages WHERE conversation_id_fk = %s",
                (conversation_id,)
            )
            client_id_row = cursor.fetchone()
            if client_id_row is None:
                return jsonify({"error": "Conversation not found"}), 404
            client_id = client_id_row[0]
        except Exception as e:
            return jsonify({"error": "Failed to retrieve client_id"}), 500


        conv_folder = os.path.join(pathconfig.basepath, "messages", user_id, client_id)
        ensure_dir(conv_folder)
        file_name = f"{conversation_id}.json"
        conv_filepath = os.path.join(conv_folder, file_name)
        s3_conv_key = f"{user_id}/messages/{client_id}/{conversation_id}.json"

        try:
                raw_data = read_json_from_s3(s3_conv_key)
                input_data = raw_data.get("input_data", [])
        except Exception as e:
                input_data = []

        if conversation_id:
            # Check if conversation_id exists in threads table and matches the channel
            try:
            # Load config file
                # with open(conv_filepath, "r", encoding="utf-8") as f:
                #     config_data = json.load(f)

                s3_config_key = f"{user_id}/messages/{client_id}/config.json"
                config_data = read_json_from_s3(s3_config_key)

                # Check if conversation_id exists and channel matches
                conv_list = config_data.get("conversations", [])
                for conv in conv_list:
                    if conv.get("channel") == channel:
                        is_reply = True
                        conversation_id = conv.get("conv_id")
                        break
                    

            except FileNotFoundError:
                print(f"⚠️ Config file not found at {conv_filepath} — treating as user-initiated")
            except Exception as e:
                print(f"❌ Error checking config file for reply status: {e}")
                
        if not is_reply:
            conversation_id = str(uuid.uuid4())

     # getting email from tables to check which one to use
        cursor.execute("SELECT email FROM users WHERE user_id = %s", (user_id,))
        u_email = cursor.fetchone()
        if  u_email:
            user_email = u_email[0]       

        cursor.execute("SELECT BusinessEmail FROM business_info WHERE user_id_fk = %s", (user_id,))
        b_email = cursor.fetchone()
        if not b_email:
            print("error : No email found from business_info table")
                    # return {"error": "No token found for business email"}, 404
        business_email = b_email[0]       

        try:
            # Choose email based on channel
            selected_email = None
            if match_email_to_channel(user_email, channel):
                selected_email = user_email
            elif match_email_to_channel(business_email, channel):
                selected_email = business_email

        except Exception as e:
            print("🔥 Exception occurred while selecting email by channel:", str(e))

        # getting client email
        cursor.execute("SELECT email_id FROM users_clients WHERE users_clients_id = %s", (client_id,))
        c_email = cursor.fetchone()
        if  c_email:
            client_email = c_email[0]       

        conv_folder = os.path.join(pathconfig.basepath, "messages", user_id, client_id)
        ensure_dir(conv_folder)
        file_name = f"{conversation_id}.json"
        conv_filepath = os.path.join(conv_folder, file_name)

        # Handle subject, ticket info, and thread_id based on message type
        if is_reply:
            
            # Read config file to get ticket info and subject
            config_folder = os.path.join(pathconfig.basepath, "messages", user_id, client_id)
            config_filepath = os.path.join(config_folder, "config.json")
            
            try:
                s3_config_key = f"{user_id}/messages/{client_id}/config.json"
                config_data = read_json_from_s3(s3_config_key)
                
                # Find the conversation in config
                ticket_id = ticket_name = subject = thread_id = None
                for conv in config_data.get("conversations", []):
                    if conv.get("conv_id") == conversation_id:

                        ticket_id = conv.get("ticket_id")
                        ticket_name = conv.get("ticket_name")
                        subject = conv.get("subject")
                        thread_id = conv.get("thread_id") 
                        break                
                    
            except Exception as e:
                return jsonify({"error": "Failed to read conversation config"}), 500
                
        else:
       # Create temporary conversation file for AI processing
            conv_folder = os.path.join(pathconfig.basepath, "messages", user_id, client_id)
            ensure_dir(conv_folder)
            file_name = f"{conversation_id}.json"
            conv_filepath = os.path.join(conv_folder, file_name)
            
            # Create initial message structure
            now_utc = datetime.now(timezone.utc)
            formatted_time = now_utc.isoformat(timespec="seconds")
            msg_id = str(uuid.uuid4())
            
            initial_message = {
                "id": msg_id,
                "from": "user",
                "to": "client",
                "body": text,
                "timestamp": formatted_time,
                "channel": channel,
                "direction": "outbound",
            }
            
            # Save temporary file for subject generation
            with open(conv_filepath, "w", encoding="utf-8") as f:
                json.dump({"new_messages": [initial_message]}, f, indent=2)
            
            # Generate subject using AI
            subjects = generate_subject(user_id, conv_filepath, channel)
            
            # Get subject from AI response
            subject = ticket_name = ticket_id = None
            
            
            for group in subjects:
                if msg_id in group.get("message_ids", []):
                    subject = ticket_name = group.get("summary")
                    break
                    
            if not subject:
                subject = ticket_name = f"Message from {client_email}"  # Fallback subject
                
            # thread_id = None  # New conversation, no thread_id yet
        # assigning ticket_id and ticket_name
        now_utc = datetime.now(timezone.utc)
        formatted_time = now_utc.isoformat(timespec="seconds")
        msg_id = str(uuid.uuid4())

        message = {
            "id": msg_id,
            "from": selected_email,
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
            "ticket_name": ticket_name
        }
        # Send the message via appropriate channel
        sent_message_id, sent_thread_id = None, None


        # sending messages
        if channel == "gmail" :
            if thread_id:
                print(f"[INFO] → Dispatching reply Gmail message to {client_email}")
                latest_msg = max(
                    input_data,
                    key=lambda msg: datetime.fromisoformat(msg["timestamp"])
                )

                latest_id = latest_msg["id"]
                # getting the reply subject
                latest_subject = latest_msg["subject"].strip()
                if not latest_subject.lower().startswith("re:"):
                    reply_subject = f"Re: {latest_subject}"
                else:
                    reply_subject = latest_subject

                try:
                    sent_message_id = gmail_reply(
                        user_id,
                        to=client_email,
                        subject=reply_subject,
                        thread_id=thread_id,
                        body_text=text,
                        in_reply_to=latest_id
                    )
                    message["id"] = sent_message_id
                    print(f"sent_message_id : {sent_message_id}")
                    print(f"thread_id : {thread_id}")
                except Exception as e:
                    print(f"❌ Gmail send failed: {e}")
                    return jsonify({"error": "Gmail send failed"}), 500

            else:
                print(f"[INFO] → Dispatching new Gmail message to {client_email}")
                try:
                    sent_message_id, sent_thread_id = send_mail(
                        user_id,
                        to=client_email,
                        subject=subject,
                        body_text=text,
                    )
                    message["id"] = sent_message_id
                    message["thread_id"] = sent_thread_id
                    print(f" sent_message_id :{sent_message_id}")
                      
                except Exception as e:
                        print(f"❌ Gmail send failed: {e}")
                        return jsonify({"error": "Gmail send failed"}), 500


        elif channel == "zoho":
            try:
                response_payload, status_code = send_zoho_email(
                                                    user_id=user_id,
                                                    to_email=client_email,
                                                    subject=subject,
                                                    body_text=text,
                                                    from_user_email=selected_email,
                                                )

                if status_code in [200, 201]:
                    message_id = response_payload.get("message_id")
                
                else:
                    print(f"Zoho send failed: {response_payload.get('error')}")
                    return jsonify({"error": response_payload.get('error')}), status_code

            except Exception as e:
                print(f"❌ Zoho send failed: {e}")
                return jsonify({"error": "Zoho send failed"}), 500

        else:
            print(f"[WARN] → Unsupported channel: {channel}")
            return jsonify({"error": "Unsupported channel"}), 400
        

        updated_date = datetime.now(timezone.utc).isoformat()
        created_date = updated_date
        try:

            if not is_reply:
                cursor.execute(
                    """
                    INSERT INTO threads (conversation_id, started_at, status, last_message_at)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (conversation_id, created_date, "Open", updated_date)
                )
            else:
                cursor.execute(
                    "UPDATE tickets SET updated_in = %s, status = %s WHERE conversation_id_fk = %s",
                    (updated_date, "In-Progress", conversation_id)
                )
                cursor.execute(
                    "UPDATE threads SET last_message_at = %s WHERE conversation_id = %s",
                    (updated_date, conversation_id)
                )

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
                    "ref",
                    "outbound",
                    subject,
                    created_date,
                    updated_date
                )
            )

            connection.commit()

        except Exception as e:
            connection.rollback()
            print(f"❌ Database operation failed — rolled back: {e}")
            return jsonify({"error": "Database operation failed"}), 500

        # ------------------ Update Conversation File ------------------

        
        try:
           

            input_data.append(message)
            conversation_data = {"input_data": input_data}

            with open(conv_filepath, "w", encoding="utf-8") as f:
                json.dump(conversation_data, f, indent=2)

            upload_any_file(
                conv_filepath,
                user_id,
                type="messages",
                s3_key_C=s3_conv_key,
            )

        except Exception as e:
            print(f"❌ Failed to update conversation file: {e}")
            return jsonify({"error": "Failed to save conversation"}), 500

        # ------------------ Update Config File ------------------

        config_folder = os.path.join(pathconfig.basepath, "messages", user_id, client_id)
        ensure_dir(config_folder)
        config_filepath = os.path.join(config_folder, "config.json")

        try:
            s3_config_key = f"{user_id}/messages/{client_id}/config.json"
            config_data = read_json_from_s3(s3_config_key)
        except Exception as e:
            print(f"[DEBUG] No config file found — creating new: {e}")
            config_data = {"userclients_id": client_id, "conversations": []}


        try:
            if updated_date.endswith('Z'):
                parsed_ts = datetime.fromisoformat(updated_date.replace("Z", "+00:00"))
            else:
                parsed_ts = datetime.fromisoformat(updated_date)
        except Exception as e:
            print(f"[WARN] Could not parse updated_date '{updated_date}': {e}")
            
        updated_entry = {
            "conv_id": conversation_id,
            "ticket_id": ticket_id,
            "ticket_name": ticket_name,
            "subject": subject,
            "channel": channel,
            "updated_date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "parsed_timestamp": parsed_ts.isoformat()
        }
        if channel == "gmail" :
            if sent_thread_id:
                updated_entry["thread_id"] = sent_thread_id
            else:
                updated_entry["thread_id"] = thread_id
        else:
            updated_entry["thread_id"] = ""
        
        conversation_exists = False
        for i, conv in enumerate(config_data.get("conversations", [])):
            if conv.get("conv_id") == conversation_id:
                config_data["conversations"][i] = updated_entry
                conversation_exists = True
                break

        if not conversation_exists:
            config_data.setdefault("conversations", []).append(updated_entry)

        config_data["userclients_id"] = client_id
        update_config_file(user_id, client_id, config_data)

        # ------------------ Final Response ------------------

        return jsonify({
            "status": "sent", 
            "id": sent_message_id or msg_id, 
            "channel": channel,
            "conversation_id": conversation_id,
            "is_reply": is_reply
        }), 200

    except Exception as e:
            print(f"❌ Unexpected error: {e}")
            return jsonify({"error": "Internal server error"}), 500

    finally:
            if 'connection' in locals():
                connection.close()
            