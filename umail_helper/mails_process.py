from cust_helpers import pathconfig
from utils.normal import ensure_dir, load_yaml_file
from datetime import datetime, timezone
import os
import json
from create_db import connect_to_rds
from utils.fireworkzz import get_fireworks_response
import yaml
from utils.s3_utils import upload_any_file, read_json_from_s3, list_all_files
import re
import uuid





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



# @umail_bp.route("/analyze_and_collect_messages/<user_id>", methods=["GET"])
def analyze_and_collect_messages_for_batch(user_id, grouped_messages,batch_count):
    user_folder = os.path.join(pathconfig.basepath, "messages", user_id)
    ensure_dir(user_folder)

    timestamp = datetime.now(timezone.utc)
    date_str = timestamp.strftime("%Y-%m-%d")
    filename = f"{date_str}.json"
    user_filepath = os.path.join(user_folder, filename)

    # try:
    #     with open(user_filepath, "r", encoding="utf-8") as f:
    #         data = json.load(f)
    # except FileNotFoundError:
    #     data = {"input_data": {}}
    #     with open(user_filepath, "w", encoding="utf-8") as f:
    #         json.dump(data, f, indent=2)

    # grouped_messages = data.get("input_data", {})

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

                type = m.get("type")
                if type == "Lead":
                    continue
                
                new_msg = {
                    "msg_id": msg_id,
                    "body": m.get("body"),
                    "from": "client" if m.get("from") == client_email else "user",
                    "to": "client" if m.get("to") == client_email else "user",
                    "date": m.get("timestamp"),
                    "channel": channel,
                }
                merged_messages.append(new_msg)

            with open(output_path, "w", encoding="utf-8") as f:
                json.dump({"new_messages": merged_messages}, f, indent=2)
            
            subjects = generate_subject(user_id, output_path, channel)

            grouped_messages = append_subject_to_messages(grouped_messages,channel,subjects,user_id,existing_new_msg,batch_count)

    return new_messages

            

# @umail_bp.route("/subject_summarisations/<user_id>", methods=["POST"])
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


# def append_subject_to_messages(grouped_messages,channel, subjects, user_id,existing_new_msg):
#     # Build a lookup of message_id → subject
#     subject_map = {}
#     for group in subjects:
#         subject = group["summary"]
#         for mid in group["message_ids"]:
#             subject_map[str(mid)] = subject

 

#     connection = connect_to_rds()
#     if connection is None:
#         print("⚠️ DB connection failed inside append_subject_to_messages")
#         return None

#     cursor = connection.cursor()

#     dt_utc = datetime.now(timezone.utc)
#     created_date = dt_utc.strftime("%Y-%m-%d %H:%M:%S")  # For database (string)
#     updated_date = dt_utc.isoformat()  # For parsing (ISO format with timezone)

#     processed_message_ids = set()

#     # Iterate through grouped_messages structure
#     for client_id, channels in grouped_messages.items():
#         s3_config_key = f"{user_id}/messages/{client_id}/config.json"

#         try:
           
#             config_data = read_json_from_s3(s3_config_key)
#             if config_data is None:
#                 config_data = {}

#         except FileNotFoundError:
#             config_data = {}
#             print("no config file")

#         existing_thread_ids = [
#                 conv.get("thread_id")
#                 for conv in config_data.get("conversations", [])
#                 if "thread_id" in conv and conv.get("thread_id")
#             ]
#         # for channel, messages in channels.items():
#         messages = grouped_messages.get(client_id, {}).get(channel, [])

#         if channel == "zoho":
#                 # Build subject lookup per client
#                     config_subject_lookup = {}
#                     if config_data:
#                         for conv in config_data.get("conversations", []):
#                             if conv.get("channel", "").lower() != "zoho":
#                                 continue
#                             conf_subj = conv.get("subject", "")
#                             conf_ticket_name = conv.get("ticket_name", "")
#                             conf_conv_id = conv.get("conv_id", "")
#                             conf_ticket_id = conv.get("ticket_id", "")
#                             if conf_subj:
#                                 config_subject_lookup[conf_subj] = {
#                                     "conv_id": conf_conv_id,
#                                     "ticket_id": conf_ticket_id,
#                                     "ticket_name": conf_ticket_name,
#                                 }


#         for msg in messages:
                        
#                         msg_id = msg.get("id")

#                         if msg_id in processed_message_ids:
#                             print(f"⏭️ Skipping already processed message: {msg_id}")
#                             continue

#                         # Check database
#                         cursor.execute("SELECT 1 FROM messages WHERE message_id = %s", (msg_id,))
#                         if cursor.fetchone():
#                             continue
                            
#                         processed_message_ids.add(msg_id)

#                         print(f"🔄 Processing message: {msg_id}")

#                         subject = msg.get("subject", "") 
#                         is_reply = subject.lower().startswith("re:") or "wrote:" in msg.get("summary", "").lower()

#                         # 1. for zoho reply from client
#                         if channel == "zoho" and is_reply:
                    
#                             normalized_subject = re.sub(r"^re:\s*", "", subject, flags=re.IGNORECASE)
#                             config_thread = config_subject_lookup.get(normalized_subject)
#                             if config_thread:
#                                 c_id = config_thread["conv_id"]
#                                 t_id = config_thread["ticket_id"]
#                                 t_name = config_thread["ticket_name"]
#                                 msg["conversation_id"] = c_id
#                                 msg["ticket_id"] = t_id
#                                 msg["ticket_name"] = t_name


#                                 cursor.execute(
#                                         "UPDATE tickets SET updated_in = %s, status = %s WHERE conversation_id_fk = %s",
#                                         (updated_date, "In-Progress", c_id)
#                                     )

#                                 cursor.execute(
#                                         "UPDATE threads SET last_message_at = %s WHERE conversation_id = %s",
#                                         (updated_date, c_id)
#                                     )

#                                 grouped_messages[client_id][channel] = messages
#                                 update_or_create_conversation_file(grouped_messages, user_id, client_id, channel,cursor)

#                                 cursor.execute(
#                                         "UPDATE messages SET update_at = %s WHERE conversation_id = %s",
#                                         (updated_date, c_id)
#                                     ) # this is incorrect. refer code block for gmail

                                
            
#                                 parsed_ts = dt_utc
                                                    
#                                 for i, conv in enumerate(config_data.get("conversations", [])):
#                                     if conv.get("conv_id") == c_id:
#                                         config_data["conversations"][i]["updated_date"] = updated_date
#                                         config_data["conversations"][i]["parsed_timestamp"] = parsed_ts.isoformat()
#                                         break
                                                
#                                 update_config_file(user_id, client_id, config_data)
#                                 connection.commit()

#                                 continue
                        
                        
#                         thread_id = msg.get("thread_id")
#                         direction = msg.get("direction")

#                         # 2. for gmail reply from client
#                         if thread_id:
#                             mesag_id = msg.get("id")
#                             msg_body = msg.get("body")

#                             found_existing_conversation = False
#                             if config_data:

#                                 matching_conv = next(
#                                     (conv for conv in config_data.get("conversations", [])
#                                     if conv.get("thread_id") == thread_id ),
#                                     None
#                                 )
                                
#                                 if matching_conv:
                                           
#                                         conversation_id = matching_conv.get("conv_id")   
#                                         print(f"found matching thread-id:{thread_id}; conv_id :{conversation_id}")
#                                         if conversation_id:
#                                             found_existing_conversation = True 
#                                             msg["conversation_id"] = conversation_id
#                                             cursor.execute(
#                                                 "SELECT tickets_id, ticket_name FROM tickets WHERE conversation_id_fk = %s",
#                                                 (conversation_id,)
#                                             )
#                                             ticket_row = cursor.fetchone()

#                                             if not ticket_row:

#                                                 ticket_id = str(uuid.uuid4())
#                                                 # ticket_name = subject_map.get(str(msg_id))
#                                                 ticket_name = matching_conv.get("subject")   

#                                                 cursor.execute(
#                                                     "INSERT INTO tickets (tickets_id, ticket_name, conversation_id_fk,status,priority,created_in,updated_in) VALUES (%s, %s, %s,%s,%s,%s,%s)",
#                                                     (ticket_id, ticket_name, conversation_id,"Open","Medium",created_date,updated_date)
#                                                 )

#                                                 # insert into assigned table
#                                                 assigned_id = str(uuid.uuid4())  
#                                                 cursor.execute(
#                                                     """
#                                                     INSERT INTO assigned (assigned_id, user_id_fk, users_clients_id_fk, ticket_id_fk)
#                                                     VALUES (%s, %s, %s, %s)
#                                                     """,
#                                                     (assigned_id, user_id, client_id, ticket_id)
#                                                 )
                                                
#                                             else:

#                                                 ticket_id = ticket_row[0]
#                                                 ticket_name = ticket_row[1]
#                                             if ticket_id and ticket_name:
#                                                 msg["ticket_id"] = ticket_id
#                                                 msg["ticket_name"] = ticket_name


#                                                 cursor.execute(
#                                                     "UPDATE tickets SET updated_in = %s, status = %s WHERE conversation_id_fk = %s",
#                                                     (updated_date, "In-Progress", conversation_id)
#                                                 )

#                                                 cursor.execute(
#                                                     "UPDATE threads SET last_message_at = %s WHERE conversation_id = %s",
#                                                     (updated_date, conversation_id)
#                                                 )
                                        
                                               
                                                

#                                                 cursor.execute("SELECT 1 FROM messages WHERE message_id = %s", (msg_id,))
#                                                 if not cursor.fetchone():
#                                                                 cont_ref = f"{user_id}/messages/{client_id}/{conversation_id}.json"
#                                                                 cursor.execute(
#                                                                     """
#                                                                     INSERT INTO messages (
#                                                                         message_id,
#                                                                         conversation_id_fk,
#                                                                         sender_id,
#                                                                         content_ref,
#                                                                         message_type,
#                                                                         is_summary,
#                                                                         created_at,
#                                                                         update_at
#                                                                     )
#                                                                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
#                                                                     """,
#                                                                     (
#                                                                         msg_id,
#                                                                         conversation_id,
#                                                                         client_id,
#                                                                         cont_ref,
#                                                                         "inbound",
#                                                                         subject,
#                                                                         created_date,
#                                                                         updated_date
#                                                                     )
#                                                                 )
                                                                                                                                                                                                                                                                   
#                                                 grouped_messages[client_id][channel] = messages
#                                                 update_or_create_conversation_file(grouped_messages, user_id, client_id, channel,cursor)

#                                                 parsed_ts = dt_utc
#                                                 for i, conv in enumerate(config_data.get("conversations", [])):
#                                                     if conv.get("conv_id") == conversation_id:
#                                                         config_data["conversations"][i]["updated_date"] = updated_date
#                                                         config_data["conversations"][i]["parsed_timestamp"] = parsed_ts.isoformat()
#                                                         config_data["conversations"][i]["ticket_id"] = ticket_id
#                                                         config_data["conversations"][i]["ticket_name"] = ticket_name

#                                                         break

#                                                 update_config_file(user_id, client_id, config_data)

#                                                 connection.commit()
#                                                 print(f"✅ Processed existing conversation: {msg_id}")
#                                                 continue
#                                             else:
#                                                 print(f"⚠️ No ticket found for conversation: {conversation_id}")
                                    
#                             if found_existing_conversation:
#                                 continue 

#                         # Thread does not exist — create new thread and optionally ticket
#                         new_conversation_id = str(uuid.uuid4())
#                         msg["conversation_id"] = new_conversation_id

#                         cursor.execute(
#                                 """
#                                 INSERT INTO threads (conversation_id, started_at, status, last_message_at)
#                                 VALUES (%s, %s, %s, %s)
#                                 """,
#                                 (new_conversation_id, created_date, "Open", updated_date)
#                             )


#                         # 3. new for inbound msg
#                         if direction == "inbound":
#                             print("inside # 3 for msg id: {msg_id}")
#                             new_ticket_id = str(uuid.uuid4())
#                             subject = msg.get("subject")
#                             msg["ticket_id"] = new_ticket_id
#                             msg["conversation_id"] = new_conversation_id
                            
#                             type = msg.get("type")
#                             if type == "Customer":
#                                 ticket_name = subject_map.get(str(msg_id))
#                                 msg["ticket_name"] = ticket_name
#                             else:
#                                 ticket_name = subject or ""
#                                 msg["ticket_name"] = ticket_name 


                            
#                             timestamp = msg.get("timestamp")
#                             # dt = datetime.fromisoformat(timestamp)
#                             # dt_utc = dt.astimezone(pytz.UTC)
#                             # created_in = dt_utc.strftime("%Y-%m-%d %H:%M:%S")
#                             # updated_in = created_in
                            
#                             try:
#                                 # Parse the message timestamp (which is offset-aware)
#                                 dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
#                                 if dt.tzinfo is None:
#                                     dt = dt.replace(tzinfo=timezone.utc)
#                                 dt_utc_msg = dt.astimezone(timezone.utc)
#                                 created_in = dt_utc_msg.strftime("%Y-%m-%d %H:%M:%S")
#                             except Exception as e:
#                                 print(f"Error parsing timestamp {timestamp}: {e}")
#                                 created_in = created_date
                            

#                             cursor.execute("SELECT 1 FROM tickets WHERE tickets_id = %s", (new_ticket_id,))
#                             if not cursor.fetchone():
                                
#                                 cursor.execute(
#                                     "INSERT INTO tickets (tickets_id, ticket_name, conversation_id_fk,status,priority,created_in,updated_in) VALUES (%s, %s, %s,%s,%s,%s,%s)",
#                                     (new_ticket_id, ticket_name, new_conversation_id,"Open","Medium",created_in,updated_date)
#                                 )

#                                 assigned_id = str(uuid.uuid4())  # Generate UUID for assigned_id
#                                 cursor.execute(
#                                     """
#                                     INSERT INTO assigned (assigned_id, user_id_fk, users_clients_id_fk, ticket_id_fk)
#                                     VALUES (%s, %s, %s, %s)
#                                     """,
#                                     (assigned_id, user_id, client_id, new_ticket_id)
#                                 )

                                
#                             cursor.execute(
#                                 """
#                                 UPDATE threads
#                                 SET ticket_id_fk = %s
#                                 WHERE conversation_id = %s
#                                 """,
#                                 (new_ticket_id, new_conversation_id)
#                             )

#                             grouped_messages[client_id][channel] = messages
#                             update_or_create_conversation_file(grouped_messages,user_id,client_id,channel,cursor)

#                             cursor.execute("SELECT 1 FROM messages WHERE message_id = %s", (msg_id,))
#                             existing = cursor.fetchone()
                           
#                             if not existing:  
                                
#                                 cont_ref = f"{user_id}/messages/{client_id}/{new_conversation_id}.json"
#                                 cursor.execute(
#                                     """
#                                     INSERT INTO messages (
#                                         message_id,
#                                         conversation_id_fk,
#                                         sender_id,
#                                         content_ref,
#                                         message_type,
#                                         is_summary,
#                                         created_at,
#                                         update_at
#                                     )
#                                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
#                                     """,
#                                     (
#                                         msg_id,
#                                         new_conversation_id,
#                                         client_id,
#                                         cont_ref,
#                                         "inbound",
#                                         subject,
#                                         created_in,
#                                         updated_date
#                                     )
#                                 )

                                             
#                             parsed_ts = dt_utc

#                             updated_entry = {
#                                 "conv_id": new_conversation_id,
#                                 "ticket_id": new_ticket_id,
#                                 "ticket_name": ticket_name,
#                                 "subject": subject,
#                                 "channel": channel,
#                                 "updated_date": updated_date,
#                                 "parsed_timestamp": parsed_ts.isoformat()
#                             }
#                             if channel == "gmail" and thread_id:
#                                 updated_entry["thread_id"] = thread_id

#                             config_data.setdefault("userclients_id", client_id)
#                             config_data.setdefault("conversations", []).append(updated_entry)

#                             update_config_file(user_id, client_id, config_data)

#                             connection.commit()
                                
                     

#                         # 4. for new outbound msg
#                         else:
#                             msg["ticket_id"] = None
#                             msg["ticket_name"] = None
#                             msg["conversation_id"] = new_conversation_id

#                             cursor.execute(
#                                 """
#                                 UPDATE threads
#                                 SET ticket_id_fk = %s
#                                 WHERE conversation_id = %s
#                                 """,
#                                 (None, new_conversation_id)
#                             )

#                             grouped_messages[client_id][channel] = messages
#                             update_or_create_conversation_file(grouped_messages,user_id,client_id,channel,cursor)

#                             cursor.execute("SELECT 1 FROM messages WHERE message_id = %s", (msg_id,))
#                             if not cursor.fetchone():

#                                 cont_ref = f"{user_id}/messages/{client_id}/{new_conversation_id}.json"

#                                 cursor.execute(
#                                     """
#                                     INSERT INTO messages (
#                                         message_id,
#                                         conversation_id_fk,
#                                         sender_id,
#                                         content_ref,
#                                         message_type,
#                                         is_summary,
#                                         created_at,
#                                         update_at
#                                     )
#                                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
#                                     """,
#                                     (
#                                         msg_id,
#                                         new_conversation_id,
#                                         client_id,
#                                         cont_ref,
#                                         "outbound",
#                                         subject,
#                                         created_date,
#                                         updated_date
#                                     )
#                                 )

#                             updated_entry = {
#                                 "conv_id": new_conversation_id,
#                                 "ticket_id": None,
#                                 "ticket_name": None,
#                                 "subject": subject_map.get(str(msg_id)),
#                                 "channel": channel,
#                                 "updated_date": dt_utc.isoformat()
#                             }
#                             if channel == "gmail" and thread_id:
#                                 updated_entry["thread_id"] = thread_id
                            
#                             config_data.setdefault("userclients_id", client_id)
#                             config_data.setdefault("conversations", []).append(updated_entry)
                        
                            

#                             update_config_file(user_id, client_id, config_data)
                                

#                             connection.commit()
                    
#     connection.close()
#     return grouped_messages

def append_subject_to_messages(grouped_messages,channel, subjects, user_id,existing_new_msg,batch_count):
    # print(f"🚀 ENTERING append_subject_to_messages for user_id={user_id}, channel={channel}")
    # print(f"🔢 Total clients in grouped_messages: {len(grouped_messages)}")
    for client_id, channels in grouped_messages.items():
        print(f"   Client {client_id}: {len(channels.get(channel, []))} messages in channel {channel}")
    
    # Build a lookup of message_id → subject
    subject_map = {}
    for group in subjects:
        subject = group["summary"]
        for mid in group["message_ids"]:
            subject_map[str(mid)] = subject
    
    print(f"📝 Built subject_map for {len(subject_map)} messages")

    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed inside append_subject_to_messages")
        return None

    cursor = connection.cursor()
    # print("✅ Database connection established")

    dt_utc = datetime.now(timezone.utc)
    created_date = dt_utc.strftime("%Y-%m-%d %H:%M:%S")  # For database (string)
    updated_date = dt_utc.isoformat()  # For parsing (ISO format with timezone)

    processed_message_ids = set()

    # Iterate through grouped_messages structure
    for client_id, channels in grouped_messages.items():
        # print(f"\n🏢 Processing client_id: {client_id}")
        s3_config_key = f"{user_id}/messages/{client_id}/config.json"

        try:
            config_data = read_json_from_s3(s3_config_key)
            if config_data is None:
                config_data = {}
                print(f"⚠️ No S3 config data found for {s3_config_key}")
            else:
                print(f"✅ Loaded S3 config data for {client_id}")

        except FileNotFoundError:
            config_data = {}
            # print(f"📁 No config file found for {client_id}")

        existing_thread_ids = [
                conv.get("thread_id")
                for conv in config_data.get("conversations", [])
                if "thread_id" in conv and conv.get("thread_id")
            ]
        
        # print(f"🧵 Found {len(existing_thread_ids)} existing thread_ids for client {client_id}")
        
        messages = grouped_messages.get(client_id, {}).get(channel, [])
        # print(f"📨 Processing {len(messages)} messages for client {client_id} in channel {channel}")

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
                    # print(f"📧 Built zoho subject lookup with {len(config_subject_lookup)} entries")

        for msg in messages:
                        msg_id = msg.get("id")
                        # print(f"\n📧 Examining message: {msg_id}")
                        # print(f"   Direction: {msg.get('direction')}")
                        # print(f"   Subject: {msg.get('subject', 'No subject')}")
                        # print(f"   Thread ID: {msg.get('thread_id', 'No thread_id')}")

                        if msg_id in processed_message_ids:
                            # print(f"⏭️ Skipping already processed message in this batch: {msg_id}")
                            continue

                        # Check database
                        # print(f"🔍 Checking database for message_id: {msg_id}")
                        cursor.execute("SELECT 1 FROM messages WHERE message_id = %s", (msg_id,))
                        db_result = cursor.fetchone()
                        if db_result:
                            # print(f"⚠️ Message {msg_id} already exists in database, skipping")
                            continue
                        else:
                            print(f"✅ Message {msg_id} NOT found in database, proceeding with processing")
                            
                        processed_message_ids.add(msg_id)
                        # print(f"📝 Added {msg_id} to processed_message_ids set (now contains {len(processed_message_ids)} messages)")

                        subject = msg.get("subject", "") 
                        is_reply = subject.lower().startswith("re:") or "wrote:" in msg.get("summary", "").lower()
                        # print(f"🔄 Processing message: {msg_id}, is_reply: {is_reply}")

                        # 1. for zoho reply from client
                        if channel == "zoho" and is_reply:
                            # print(f"🎯 CASE 1: Zoho reply from client for {msg_id}")
                    
                            normalized_subject = re.sub(r"^re:\s*", "", subject, flags=re.IGNORECASE)
                            config_thread = config_subject_lookup.get(normalized_subject)
                            if config_thread:
                                # print(f"✅ Found matching config thread for normalized subject: {normalized_subject}")
                                c_id = config_thread["conv_id"]
                                t_id = config_thread["ticket_id"]
                                t_name = config_thread["ticket_name"]
                                msg["conversation_id"] = c_id
                                msg["ticket_id"] = t_id
                                msg["ticket_name"] = t_name

                                # print(f"🎫 Updating tickets and threads for conv_id: {c_id}")
                                cursor.execute(
                                        "UPDATE tickets SET updated_in = %s, status = %s WHERE conversation_id_fk = %s",
                                        (updated_date, "In-Progress", c_id)
                                    )

                                cursor.execute(
                                        "UPDATE threads SET last_message_at = %s WHERE conversation_id = %s",
                                        (updated_date, c_id)
                                    )

                                grouped_messages[client_id][channel] = messages
                                update_or_create_conversation_file(msg,client_id,cursor,batch_count)

                                cursor.execute(
                                        "UPDATE messages SET update_at = %s WHERE conversation_id = %s",
                                        (updated_date, c_id)
                                    ) # this is incorrect. refer code block for gmail

                                parsed_ts = dt_utc
                                                    
                                for i, conv in enumerate(config_data.get("conversations", [])):
                                    if conv.get("conv_id") == c_id:
                                        config_data["conversations"][i]["updated_date"] = updated_date
                                        config_data["conversations"][i]["parsed_timestamp"] = parsed_ts.isoformat()
                                        break
                                                
                                update_config_file(user_id, client_id, config_data)
                                connection.commit()
                                # print(f"✅ CASE 1 COMPLETE: Committed zoho reply processing for {msg_id}")

                                continue
                            else:
                                print(f"⚠️ No matching config thread found for normalized subject: {normalized_subject}")
                        
                        
                        thread_id = msg.get("thread_id")
                        direction = msg.get("direction")

                        # 2. for gmail reply from client
                        if thread_id:
                            # print(f"🎯 CASE 2: Gmail message with thread_id {thread_id} for {msg_id}")
                            mesag_id = msg.get("id")
                            msg_body = msg.get("body")

                            cursor.execute("SELECT 1 FROM messages WHERE message_id = %s", (mesag_id,))
                            existing_msg = cursor.fetchone()
                            if existing_msg:
                                continue
                            
                            found_existing_conversation = False
                            if config_data:
                                print(f"config_data : {config_data}")
                                matching_conv = next(
                                    (conv for conv in config_data.get("conversations", [])
                                    if conv.get("thread_id") == thread_id ),
                                    None
                                )


                                
                                if matching_conv:
                                    # print(f"✅ Found matching conversation for thread_id: {thread_id}")
                                    conversation_id = matching_conv.get("conv_id")   
                                    print(f"found matching thread-id:{thread_id}; conv_id :{conversation_id}")
                                    if conversation_id:
                                        found_existing_conversation = True 
                                        msg["conversation_id"] = conversation_id

                                        ticket_id = ticket_name = None
                                        
                                        # print(f"🎫 Looking for existing ticket for conversation: {conversation_id}")
                                        cursor.execute(
                                            "SELECT tickets_id, ticket_name FROM tickets WHERE conversation_id_fk = %s",
                                            (conversation_id,)
                                        )
                                        ticket_row = cursor.fetchone()

                                        if not ticket_row:
                                            # print(f"🆕 No existing ticket found, creating new ticket for conversation: {conversation_id}")
                                            ticket_id = str(uuid.uuid4())
                                            # ticket_name = subject_map.get(str(msg_id))
                                            ticket_name = matching_conv.get("subject")   

                                            cursor.execute(
                                                "INSERT INTO tickets (tickets_id, ticket_name, conversation_id_fk,status,priority,created_in,updated_in) VALUES (%s, %s, %s,%s,%s,%s,%s)",
                                                (ticket_id, ticket_name, conversation_id,"Open","Medium",created_date,updated_date)
                                            )

                                            # insert into assigned table
                                            assigned_id = str(uuid.uuid4())  
                                            cursor.execute(
                                                """
                                                INSERT INTO assigned (assigned_id, user_id_fk, users_clients_id_fk, ticket_id_fk)
                                                VALUES (%s, %s, %s, %s)
                                                """,
                                                (assigned_id, user_id, client_id, ticket_id)
                                            )
                                            # print(f"✅ Created new ticket {ticket_id} and assigned it")
                                            
                                        else:
                                            # print(f"✅ Found existing ticket for conversation: {conversation_id}")
                                            ticket_id = ticket_row[0]
                                            ticket_name = ticket_row[1]
                                            # print(f"ticket_id: {ticket_id}")
                                            # print(f"ticket_name : {ticket_name}")
                                            
                                        if ticket_id :
                                            msg["ticket_id"] = ticket_id
                                            msg["ticket_name"] = ticket_name

                                            # print(f"🔄 Updating ticket and thread status for {ticket_id}")
                                            cursor.execute(
                                                "UPDATE tickets SET updated_in = %s, status = %s WHERE conversation_id_fk = %s",
                                                (updated_date, "In-Progress", conversation_id)
                                            )

                                            cursor.execute(
                                                "UPDATE threads SET last_message_at = %s WHERE conversation_id = %s",
                                                (updated_date, conversation_id)
                                            )

                                            # print(f"🔍 Checking if message {msg_id} already exists in messages table")
                                            cursor.execute("SELECT 1 FROM messages WHERE message_id = %s", (msg_id,))
                                            existing_msg = cursor.fetchone()
                                            if not existing_msg:
                                                # print(f"✅ Message {msg_id} not found in messages table, inserting now")
                                                cont_ref = f"{user_id}/messages/{client_id}/{conversation_id}.json"
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
                                                        "inbound",
                                                        subject,
                                                        created_date,
                                                        updated_date
                                                    )
                                                )
                                                # print(f"✅ Successfully inserted message {msg_id} into messages table")
                                            else:
                                                print(f"⚠️ Message {msg_id} already exists in messages table")
                                                                                                                                                                                                                                                                   
                                            grouped_messages[client_id][channel] = messages
                                            update_or_create_conversation_file(msg,client_id,cursor,batch_count)

                                            parsed_ts = dt_utc
                                            # print(f"🔄 Updating config data for conversation {conversation_id}")
                                            for i, conv in enumerate(config_data.get("conversations", [])):
                                                if conv.get("conv_id") == conversation_id:
                                                    config_data["conversations"][i]["updated_date"] = updated_date
                                                    config_data["conversations"][i]["parsed_timestamp"] = parsed_ts.isoformat()
                                                    config_data["conversations"][i]["ticket_id"] = ticket_id
                                                    config_data["conversations"][i]["ticket_name"] = ticket_name
                                                    break

                                            update_config_file(user_id, client_id, config_data)

                                            connection.commit()
                                            # print(f"✅ CASE 2 COMPLETE: Committed existing conversation processing for {msg_id}")
                                            continue
                                        else:
                                            print(f"⚠️ No ticket found for conversation: {conversation_id}")
                                else:
                                    print(f"⚠️ No matching conversation found for thread_id: {thread_id}")
                                    
                            if found_existing_conversation:
                                # print(f"⏭️ Found existing conversation, skipping to next message")
                                continue 

                        # Thread does not exist — create new thread and optionally ticket
                        # print(f"🆕 CASE 3/4: Creating new conversation for message {msg_id}")
                        new_conversation_id = str(uuid.uuid4())
                        msg["conversation_id"] = new_conversation_id

                        # print(f"🧵 Inserting new thread with conversation_id: {new_conversation_id}")
                        cursor.execute(
                                """
                                INSERT INTO threads (conversation_id, started_at, status, last_message_at)
                                VALUES (%s, %s, %s, %s)
                                """,
                                (new_conversation_id, created_date, "Open", updated_date)
                            )

                        # 3. new for inbound msg
                        if direction == "inbound":
                            # print(f"🎯 CASE 3: New inbound message {msg_id}")
                            # print(f"inside # 3 for msg id: {msg_id}")
                            new_ticket_id = str(uuid.uuid4())
                            subject = msg.get("subject")
                            msg["ticket_id"] = new_ticket_id
                            msg["conversation_id"] = new_conversation_id
                            
                            type = msg.get("type")
                            if type == "Customer":
                                ticket_name = subject_map.get(str(msg_id))
                                msg["ticket_name"] = ticket_name
                                # print(f"📝 Customer type: Using AI-generated ticket_name: {ticket_name}")
                            else:
                                ticket_name = subject or ""
                                msg["ticket_name"] = ticket_name 
                                # print(f"📝 Non-customer type: Using subject as ticket_name: {ticket_name}")

                            timestamp = msg.get("timestamp")
                            
                            try:
                                # Parse the message timestamp (which is offset-aware)
                                dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                                if dt.tzinfo is None:
                                    dt = dt.replace(tzinfo=timezone.utc)
                                dt_utc_msg = dt.astimezone(timezone.utc)
                                created_in = dt_utc_msg.strftime("%Y-%m-%d %H:%M:%S")
                                # print(f"📅 Parsed timestamp: {created_in}")
                            except Exception as e:
                                # print(f"⚠️ Error parsing timestamp {timestamp}: {e}")
                                created_in = created_date

                            # print(f"🔍 Checking if ticket {new_ticket_id} already exists")
                            cursor.execute("SELECT 1 FROM tickets WHERE tickets_id = %s", (new_ticket_id,))
                            if not cursor.fetchone():
                                # print(f"✅ Ticket {new_ticket_id} doesn't exist, creating new ticket")
                                cursor.execute(
                                    "INSERT INTO tickets (tickets_id, ticket_name, conversation_id_fk,status,priority,created_in,updated_in) VALUES (%s, %s, %s,%s,%s,%s,%s)",
                                    (new_ticket_id, ticket_name, new_conversation_id,"Open","Medium",created_in,updated_date)
                                )

                                assigned_id = str(uuid.uuid4())  # Generate UUID for assigned_id
                                cursor.execute(
                                    """
                                    INSERT INTO assigned (assigned_id, user_id_fk, users_clients_id_fk, ticket_id_fk)
                                    VALUES (%s, %s, %s, %s)
                                    """,
                                    (assigned_id, user_id, client_id, new_ticket_id)
                                )
                                # print(f"✅ Created and assigned new ticket {new_ticket_id}")
                            else:
                                print(f"⚠️ Ticket {new_ticket_id} already exists")

                            # print(f"🔄 Updating thread {new_conversation_id} with ticket_id {new_ticket_id}")
                            cursor.execute(
                                """
                                UPDATE threads
                                SET ticket_id_fk = %s
                                WHERE conversation_id = %s
                                """,
                                (new_ticket_id, new_conversation_id)
                            )

                            grouped_messages[client_id][channel] = messages
                            # update_or_create_conversation_file(grouped_messages,user_id,client_id,channel,cursor,batch_count)
                            update_or_create_conversation_file(msg,client_id,cursor,batch_count)

                            # print(f"🔍 Final check: Does message {msg_id} exist in messages table?")
                            cursor.execute("SELECT 1 FROM messages WHERE message_id = %s", (msg_id,))
                            existing = cursor.fetchone()
                           
                            if not existing:  
                                # print(f"✅ Message {msg_id} not in messages table, inserting now")
                                cont_ref = f"{user_id}/messages/{client_id}/{new_conversation_id}.json"
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
                                        cont_ref,
                                        "inbound",
                                        subject,
                                        created_in,
                                        updated_date
                                    )
                                )
                                # print(f"✅ Successfully inserted message {msg_id} into messages table")
                            else:
                                print(f"⚠️ Message {msg_id} already exists in messages table - THIS SHOULD NOT HAPPEN!")

                            parsed_ts = dt_utc

                            updated_entry = {
                                "conv_id": new_conversation_id,
                                "ticket_id": new_ticket_id,
                                "ticket_name": ticket_name,
                                "subject": subject,
                                "channel": channel,
                                "updated_date": updated_date,
                                "parsed_timestamp": parsed_ts.isoformat()
                            }
                            if channel == "gmail" and thread_id:
                                updated_entry["thread_id"] = thread_id

                            config_data.setdefault("userclients_id", client_id)
                            config_data.setdefault("conversations", []).append(updated_entry)

                            update_config_file(user_id, client_id, config_data)

                            connection.commit()
                            # print(f"✅ CASE 3 COMPLETE: Committed new inbound message processing for {msg_id}")

                        # 4. for new outbound msg
                        else:
                            # print(f"🎯 CASE 4: New outbound message {msg_id}")
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

                            grouped_messages[client_id][channel] = messages
                            update_or_create_conversation_file(msg,client_id,cursor,batch_count)

                            # print(f"🔍 Checking if outbound message {msg_id} exists in messages table")
                            cursor.execute("SELECT 1 FROM messages WHERE message_id = %s", (msg_id,))
                            if not cursor.fetchone():
                                # print(f"✅ Outbound message {msg_id} not found, inserting now")

                                cont_ref = f"{user_id}/messages/{client_id}/{new_conversation_id}.json"

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
                                        cont_ref,
                                        "outbound",
                                        subject,
                                        created_date,
                                        updated_date
                                    )
                                )
                                # print(f"✅ Successfully inserted outbound message {msg_id}")
                            else:
                                print(f"⚠️ Outbound message {msg_id} already exists in messages table")

                            updated_entry = {
                                "conv_id": new_conversation_id,
                                "ticket_id": None,
                                "ticket_name": None,
                                "subject": subject_map.get(str(msg_id)),
                                "channel": channel,
                                "updated_date": dt_utc.isoformat()
                            }
                            if channel == "gmail" and thread_id:
                                updated_entry["thread_id"] = thread_id
                            
                            config_data.setdefault("userclients_id", client_id)
                            config_data.setdefault("conversations", []).append(updated_entry)

                            update_config_file(user_id, client_id, config_data)

                            connection.commit()
                            print(f"✅ CASE 4 COMPLETE: Committed new outbound message processing for {msg_id}")
                    
    connection.close()
    print(f"🏁 EXITING append_subject_to_messages - processed {len(processed_message_ids)} unique messages")
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
    print("config data updated !!")

def update_or_create_conversation_file(msg,client_id,cursor,batch_count):
    # print("entered in update_or_create_conversation_file")
    user_id = msg.get("user_id")
    channel = msg.get("source")
    prefix = f"{user_id}/messages/{client_id}"
    config_key = f"{prefix}/config.json"
    config_data = read_json_from_s3(config_key)

    # Pull existing conversation IDs for the specified 
    # print(f"getting config dta for : {client_id}")
    existing_conversations = {
        conv["conv_id"] for conv in config_data.get("conversations", [])
        if conv.get("channel") == channel and conv.get("conv_id")
    }

    message_id = msg.get("id")
    cursor.execute(
                "SELECT 1 FROM messages WHERE message_id = %s", (message_id,)
            )
    m_id = cursor.fetchone()
    if not m_id:
             
        conv_id = msg.get("conversation_id")
        print(f"conv id : {conv_id}")
    
        file_key = f"{prefix}/{conv_id}.json"
        conv_folder = os.path.join(
                                pathconfig.basepath, "messages", user_id, client_id
                            )
        ensure_dir(conv_folder)
        conv_file_name = f"{conv_id}.json"
        conv_filepath = os.path.join(conv_folder, conv_file_name)
        s3_config_key = f"{user_id}/messages/{client_id}/{conv_id}.json"


        if conv_id in existing_conversations:
                raw_data = read_json_from_s3(file_key)
                input_data = raw_data.get("input_data", [])
                msg = normalize_datetimes(msg)   # check this later
                input_data.extend(msg)

                with open(conv_filepath, "w", encoding="utf-8") as f:
                    json.dump({"input_data": input_data}, f, indent=2)


                upload_any_file(
                                        conv_filepath,
                                        user_id,
                                        type="messages",
                                        s3_key_C=s3_config_key,
                                    ) 
        else:

                with open(conv_filepath, "w", encoding="utf-8") as f:
                    json.dump({"input_data": msg}, f, indent=2)

                upload_any_file(
                                        conv_filepath,
                                        user_id,
                                        type="messages",
                                        s3_key_C=s3_config_key,
                                    )   
                
        lance_folder =   os.path.join(
                                pathconfig.basepath, "messages", user_id,f"lance_folder:{batch_count}" 
                            )
        ensure_dir(lance_folder) 
        lance_conv_file_name = f"{user_id}:{client_id}:{conv_id}.json"
        full_file_path = os.path.join(lance_folder, lance_conv_file_name)
        if os.path.exists(full_file_path):
                with open(full_file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    data.extend(msg)

                with open(full_file_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                # print("appended the messages to lance_file")

        else:
                with open(full_file_path, "w", encoding="utf-8") as f:
                    json.dump(msg, f, indent=2)
                    # print("created and added the messages to lance_file")



# def update_or_create_conversation_file(grouped_messages, user_id, client_id, channel,cursor,batch_count):
#     # print("entered in update_or_create_conversation_file")
#     prefix = f"{user_id}/messages/{client_id}"
#     config_key = f"{prefix}/config.json"
#     config_data = read_json_from_s3(config_key)

#     # Pull existing conversation IDs for the specified 
#     # print(f"getting config dta for : {client_id}")
#     existing_conversations = {
#         conv["conv_id"] for conv in config_data.get("conversations", [])
#         if conv.get("channel") == channel and conv.get("conv_id")
#     }
#     channel_messages = grouped_messages.get(client_id, {}).get(channel, [])
#     if not channel_messages:
#         # print(f"[INFO] No messages found for client={client_id}, channel={channel}")
#         return

#     # Group messages by conversation_id
#     conversation_groups = {}
#     for msg in channel_messages:
        
#         message_id = msg.get("id")
#         cursor.execute(
#                 "SELECT 1 FROM messages WHERE message_id = %s", (message_id,)
#             )
#         m_id = cursor.fetchone()
#         if m_id:
#                 continue

#         conv_id = msg.get("conversation_id")
#         if conv_id:
#             conversation_groups.setdefault(conv_id, []).append(msg)

#     for conv_id, messages in conversation_groups.items():
#         file_key = f"{prefix}/{conv_id}.json"
#         conv_folder = os.path.join(
#                             pathconfig.basepath, "messages", user_id, client_id
#                         )
#         ensure_dir(conv_folder)
#         conv_file_name = f"{conv_id}.json"
#         conv_filepath = os.path.join(conv_folder, conv_file_name)
#         s3_config_key = f"{user_id}/messages/{client_id}/{conv_id}.json"


#         if conv_id in existing_conversations:
#             raw_data = read_json_from_s3(file_key)
#             input_data = raw_data.get("input_data", [])
#             messages = normalize_datetimes(messages)   # check this later
#             input_data.extend(messages)

#             with open(conv_filepath, "w", encoding="utf-8") as f:
#                 json.dump({"input_data": input_data}, f, indent=2)


#             upload_any_file(
#                                     conv_filepath,
#                                     user_id,
#                                     type="messages",
#                                     s3_key_C=s3_config_key,
#                                 ) 
#         else:
#             # print(f"coud not found conv id :{conv_id} in existing")

#             with open(conv_filepath, "w", encoding="utf-8") as f:
#                 json.dump({"input_data": messages}, f, indent=2)

#             upload_any_file(
#                                     conv_filepath,
#                                     user_id,
#                                     type="messages",
#                                     s3_key_C=s3_config_key,
#                                 )   
            
#         lance_folder =   os.path.join(
#                             pathconfig.basepath, "messages", user_id,f"lance_folder:{batch_count}" 
#                         )
#         ensure_dir(lance_folder) 
#         lance_conv_file_name = f"{user_id}:{client_id}:{conv_id}.json"
#         full_file_path = os.path.join(lance_folder, lance_conv_file_name)
#         if os.path.exists(full_file_path):
#             with open(full_file_path, "r", encoding="utf-8") as f:
#                 data = json.load(f)
#                 data.extend(messages)

#             with open(full_file_path, "w", encoding="utf-8") as f:
#                 json.dump(data, f, indent=2)
#             # print("appended the messages to lance_file")

#         else:
#             with open(full_file_path, "w", encoding="utf-8") as f:
#                 json.dump(messages, f, indent=2)
#                 # print("created and added the messages to lance_file")


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
