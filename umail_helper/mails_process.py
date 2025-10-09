import asyncio
from cust_helpers import pathconfig
from db.rds_db import safe_execute
from utils.normal import ensure_dir, load_yaml_file
from datetime import datetime, timezone
import os
import json
from create_db import connect_to_rds
from utils.fireworkzz import get_fireworks_response
import yaml
from utils.s3_utils import upload_any_file, read_json_from_s3
import uuid


async def vtooanalyze_and_collect_messages_for_batch(
    user_id, grouped_messages, batch_count, cursor, ticket_allocator
):
    print("vtoo analyze")
    user_folder = os.path.join(pathconfig.basepath, "messages", user_id)
    ensure_dir(user_folder)

    new_messages = []

    async def process_channel(client_id, channel, channel_msgs, ticket_allocator):
        # fetch client email
        cursor.execute(
            "SELECT email_id FROM users_clients WHERE users_clients_id = %s",
            (client_id,),
        )
        row = cursor.fetchone()
        if not row:
            print("❌ Error: User not found")
            return []
        client_email = row[0]

        config_folder = os.path.join(user_folder, client_id)
        ensure_dir(config_folder)

        msg_ids = [m["id"] for m in channel_msgs if "id" in m]
        if not msg_ids:
            return []

        # ✅ Check existing IDs in DB
        placeholders = ",".join(["%s"] * len(msg_ids))
        sql = f"SELECT message_id FROM messages WHERE message_id IN ({placeholders})"
        cursor.execute(sql, tuple(msg_ids))
        existing_ids = {row[0] for row in cursor.fetchall()}

        # Load previous new_messages.json
        output_path = os.path.join(config_folder, f"{channel}_new_messages.json")
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                new_msg_data = json.load(f)
        except Exception:
            new_msg_data = {}

        merged_messages = new_msg_data.get("new_messages", [])

        new_msgs_local = []
        for m in channel_msgs:
            msg_id = m["id"]
            if msg_id in existing_ids or m.get("type") == "Lead":
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
            new_msgs_local.append(new_msg)

        # Write updated file
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump({"new_messages": merged_messages}, f, indent=2)

        # Heavier async tasks
        subjects = await asyncio.to_thread(
            generate_subject, user_id, output_path, channel
        )
        channel_grouped = {client_id: {channel: channel_msgs}}

        await asyncio.to_thread(
            append_subject_to_messages,
            channel_grouped,
            channel,
            subjects,
            user_id,
            batch_count,
            ticket_allocator,
        )

        # UmailLanceClient(user_id).update_ticket_number(user_id, lance_ticket_id)

        return new_msgs_local

    # build async tasks for all client_id + channels
    tasks = []
    # Pre-fetch next ticket number once
    # client_ticket = UmailLanceClient(user_id)
    # lance_ticket_id_base = client_ticket.call_ticket_number(user_id) or 0
    for client_id, channel_data in grouped_messages.items():
        for channel, channel_msgs in channel_data.items():
            tasks.append(
                process_channel(client_id, channel, channel_msgs, ticket_allocator)
            )

    # client_ticket = UmailLanceClient(user_id)
    # latest = client_ticket.call_ticket_number(user_id) or 0

    # tasks = []
    # offset = 0
    # for client_id, channel_data in grouped_messages.items():
    #     for channel, channel_msgs in channel_data.items():
    #         lance_base_for_task = latest + offset
    #         offset += len(channel_msgs)  # or 1, depending on your increment rule
    #         tasks.append(
    #             process_channel(client_id, channel, channel_msgs, lance_base_for_task)
    #         )
    # after all tasks complete, update to latest+offset
    # run all tasks concurrently
    results = await asyncio.gather(*tasks)
    # final_ticket = latest + offset
    # client_ticket.update_ticket_number(user_id, final_ticket)

    # flatten results
    for res in results:
        new_messages.extend(res)

    # UmailLanceClient(user_id).update_ticket_number(user_id, lance_ticket_id)
    return new_messages


# @umail_bp.route("/subject_summarisations/<user_id>", methods=["POST"])
def generate_subject(user_id, output_path, channel):
    try:
        if not user_id or not output_path:
            print("❌ Missing user_id or filename")
            return None

        with open(output_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        all_messages = data.get("new_messages", [])
        if channel:
            filtered_messages = [
                msg.get("body", "")
                for msg in all_messages
                if channel is None or msg.get("channel") == channel
            ]
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
        full_prompt = update_prompt_template.replace(
            "{full_text_message_body}", message_payload
        )

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
    except Exception as e:
        print(f"🔥 Exception during summarisation: {str(e)}")
        return None


def append_subject_to_messages(
    grouped_messages,
    channel,
    subjects,
    user_id,
    batch_count,
    ticket_allocator,
):
    print(f"****inside append_subject_to_messages for batch number : {batch_count}")

    # Build a lookup of message_id → subject (unchanged)
    subject_map = {}
    for group in subjects or []:
        subject = group.get("summary")
        for mid in group.get("message_ids", []):
            subject_map[str(mid)] = subject
    # print(f"📝 Built subject_map for {len(subject_map)} messages")

    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed inside append_subject_to_messages")
        return None

    cursor = connection.cursor()

    dt_utc = datetime.now(timezone.utc)
    updated_date = dt_utc.isoformat()

    processed_message_ids = set()

    # Pre-fetch next ticket number once
    # client_ticket = UmailLanceClient(user_id)
    # lance_ticket_id_base = client_ticket.call_ticket_number(user_id) or 0
    async def _get_ticket():
        return await ticket_allocator.next_ticket()

    # Small helper caches (per client)
    communication_id_cache = {}

    # Iterate through grouped_messages structure
    for client_id, channels in grouped_messages.items():
        # print("IN the loop of append_subject")
        s3_config_key = f"{user_id}/messages/{client_id}/config.json"
        try:
            config_data = read_json_from_s3(s3_config_key) or {}
        except FileNotFoundError:
            config_data = {}

        # # Make list/set once
        # existing_thread_ids = [
        #     conv.get("thread_id")
        #     for conv in config_data.get("conversations", [])
        #     if conv.get("thread_id")
        # ]

        # Build Zoho subject lookup once per client (only if needed)
        config_subject_lookup = {}
        if channel == "zoho" and config_data:
            for conv in config_data.get("conversations", []):
                if conv.get("channel", "").lower() != "zoho":
                    continue
                conf_subj = conv.get("subject", "")
                if conf_subj:
                    config_subject_lookup[conf_subj] = {
                        "conv_id": conv.get("conv_id", ""),
                        "ticket_id": conv.get("ticket_id", ""),
                        "ticket_name": conv.get("ticket_name", ""),
                    }

        # Cache communication_id per (user_id, client_id)
        if client_id not in communication_id_cache:
            cursor.execute(
                "SELECT communication_id FROM communication WHERE user_id_fk = %s and users_clients_id_fk = %s",
                (user_id, client_id),
            )
            row = cursor.fetchone()
            communication_id_cache[client_id] = row[0] if row else None

        messages = grouped_messages.get(client_id, {}).get(channel, [])
        if not messages:
            print("messages not found for client", client_id)
            continue

        # ----------- BIG WIN: Batch existence check for messages -----------
        all_ids = [m.get("id") for m in messages if m.get("id")]
        existing_ids = set()
        if all_ids:
            # Postgres style: ANY(%s); for MySQL use IN (%s) with executemany builder
            try:
                cursor.execute(
                    "SELECT message_id FROM messages WHERE message_id = ANY(%s)",
                    (all_ids,),
                )
                existing_ids = {r[0] for r in cursor.fetchall()}
            except Exception:
                # Fallback for MySQL (build an IN clause safely)
                # Note: we keep behavior, but this branch only runs if ANY is unsupported
                chunk = 1000
                for i in range(0, len(all_ids), chunk):
                    subset = all_ids[i : i + chunk]
                    placeholders = ", ".join(["%s"] * len(subset))
                    cursor.execute(
                        f"SELECT message_id FROM messages WHERE message_id IN ({placeholders})",
                        subset,
                    )
                    existing_ids.update(r[0] for r in cursor.fetchall())
        # --------------------------------------------------------------------

        found_existing_conversation = False  # variable used later

        for msg in messages:

            msg_id = msg.get("id")
            if not msg_id:
                continue

            if msg_id in processed_message_ids:
                print("skipping bcz of processed_message", msg_id)
                continue

            # Use the batched set instead of per-message SELECT
            if msg_id in existing_ids:
                print("skipping because it is present in existing ids", msg_id)
                continue

            processed_message_ids.add(msg_id)

            subject = msg.get("subject", "")
            is_reply = (
                subject.lower().startswith("re:")
                or "wrote:" in (msg.get("summary") or "").lower()
            )

            created_date = datetime.fromisoformat(
                msg.get("timestamp").replace("Z", "+00:00")
            ).strftime("%Y-%m-%d %H:%M:%S")
            lance_ticket_id = asyncio.run(_get_ticket())
            print(f"internally lance_ticket_id_ : {lance_ticket_id}")

            # # 1) ZOHO reply path
            # if channel == "zoho" and is_reply:
            #     normalized_subject = re.sub(
            #         r"^re:\s*", "", subject, flags=re.IGNORECASE
            #     )
            #     config_thread = config_subject_lookup.get(normalized_subject)
            #     if config_thread:
            #         c_id = config_thread["conv_id"]
            #         t_id = config_thread["ticket_id"]
            #         t_name = config_thread["ticket_name"]
            #         msg["conversation_id"] = c_id
            #         msg["ticket_id"] = t_id
            #         msg["ticket_name"] = t_name

            #         cursor.execute(
            #             "UPDATE tickets SET updated_in = %s, status = %s WHERE conversation_id_fk = %s",
            #             (updated_date, "In-Progress", c_id),
            #         )
            #         cursor.execute(
            #             "UPDATE threads SET last_message_at = %s WHERE conversation_id = %s",
            #             (updated_date, c_id),
            #         )

            #         grouped_messages[client_id][channel] = messages
            #         update_or_create_conversation_file(
            #             msg, client_id, cursor, batch_count
            #         )

            #         # NOTE: original code updated messages.update_at by conversation_id (comment said incorrect)
            #         cursor.execute(
            #             "UPDATE messages SET update_at = %s WHERE conversation_id = %s",
            #             (updated_date, c_id),
            #         )

            #         # update config_data in memory
            #         for i, conv in enumerate(config_data.get("conversations", [])):
            #             if conv.get("conv_id") == c_id:
            #                 config_data["conversations"][i][
            #                     "updated_date"
            #                 ] = updated_date
            #                 config_data["conversations"][i][
            #                     "parsed_timestamp"
            #                 ] = dt_utc.isoformat()
            #                 break

            #         update_config_file(user_id, client_id, config_data)
            #         connection.commit()
            #         continue
            #     else:
            #         print(
            #             f"⚠️ No matching config thread for normalized subject: {normalized_subject}"
            #         )

            # 2) Gmail reply (has thread_id)
            thread_id = msg.get("thread_id")
            direction = msg.get("direction")
            msg_body = msg.get("body")
            new_conversation_id = f"{user_id}_{thread_id}"
            print("conversation id", new_conversation_id)
            cursor.execute(
                "SELECT 1 from threads where conversation_id = %s",
                (new_conversation_id,),
            )
            row = cursor.fetchone()
            if row:
                print("found an existing conversation", new_conversation_id)
                if thread_id:
                    print("THREAD BASE CASE", msg_id)
                    # # mesag_id = msg_id  # keep original name usage
                    # print("MSG BODY", msg_body)

                    # We already batched existing message ids; this keeps behavior
                    if msg_id in existing_ids:
                        print("SKIPPING line 509")
                        continue

                    found_existing_conversation = False
                    matching_conv = None
                    if config_data:
                        matching_conv = next(
                            (
                                conv
                                for conv in config_data.get("conversations", [])
                                if conv.get("thread_id") == thread_id
                            ),
                            None,
                        )
                    if matching_conv:
                        conversation_id = matching_conv.get("conv_id")
                        # print(
                        #     f"found matching thread-id:{thread_id}; conv_id :{conversation_id}"
                        # )
                        if conversation_id:
                            found_existing_conversation = True
                            msg["conversation_id"] = conversation_id

                            # find or create ticket
                            cursor.execute(
                                "SELECT tickets_id, ticket_name FROM tickets WHERE conversation_id_fk = %s",
                                (conversation_id,),
                            )
                            ticket_row = cursor.fetchone()
                            if not ticket_row:
                                ticket_uuid = str(uuid.uuid4())
                                ticket_id = f"TKT-{lance_ticket_id}#{ticket_uuid}"
                                ticket_name = matching_conv.get("subject")
                                # print(
                                #     f"NO TKT - matched config TKT {lance_ticket_id} thread {thread_id} clientid {client_id} conv {conversation_id}"
                                # )

                                communication_id = communication_id_cache.get(client_id)
                                # print("thread case insert to tickets")
                                safe_execute(
                                    cursor,
                                    """
                                    INSERT INTO tickets 
                                    (tickets_id,ticket_name, conversation_id_fk, status, priority, created_in, updated_in, communication_id_fk)
                                    VALUES (%s,%s, %s, %s, %s, %s, %s, %s)
                                    """,
                                    (
                                        ticket_id,
                                        ticket_name,
                                        conversation_id,
                                        "Open",  # or rely on DB default
                                        "Medium",  # or rely on DB default
                                        created_date,
                                        created_date,
                                        communication_id,
                                    ),
                                )
                                # lance_ticket_id += 1

                                assigned_id = str(uuid.uuid4())
                                # print("thread case insert to assigned")
                                safe_execute(
                                    cursor,
                                    """
                                    INSERT INTO assigned (assigned_id, user_id_fk, users_clients_id_fk, ticket_id_fk)
                                    VALUES (%s, %s, %s, %s)
                                    """,
                                    (assigned_id, user_id, client_id, ticket_id),
                                )
                                # print("CREATED TICKET 569")
                            else:
                                # print(
                                #     f"matched config TKT {lance_ticket_id} thread {thread_id} clientid {client_id} conv {conversation_id}"
                                # )
                                ticket_id, ticket_name = ticket_row[0], ticket_row[1]

                            if ticket_id:
                                msg["ticket_id"] = ticket_id
                                msg["ticket_name"] = ticket_name
                                # print(
                                #     f"matched config TKT {lance_ticket_id} thread {thread_id} clientid {client_id} conv {conversation_id}"
                                # )

                                safe_execute(
                                    cursor,
                                    "UPDATE tickets SET updated_in = %s, status = %s WHERE conversation_id_fk = %s",
                                    (updated_date, "In-Progress", conversation_id),
                                )

                                safe_execute(
                                    cursor,
                                    "UPDATE threads SET last_message_at = %s WHERE conversation_id = %s",
                                    (updated_date, conversation_id),
                                )

                                # final insert if message doesn't exist (we already checked via set; keep the guard)
                                if msg_id not in existing_ids:
                                    cont_ref = f"{user_id}/messages/{client_id}/{conversation_id}.json"
                                    safe_execute(
                                        cursor,
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
                                            direction,
                                            subject,
                                            created_date,
                                            updated_date,
                                            channel,
                                        ),
                                    )
                                    existing_ids.add(msg_id)  # keep local set in sync
                                    # print("MSG ADDED 623", msg_id)

                                update_or_create_conversation_file(
                                    msg, client_id, cursor, batch_count
                                )
                                # print("UPDATE CONVERSATION FILE 628", msg_id)

                                # update config memory
                                for i, conv in enumerate(
                                    config_data.get("conversations", [])
                                ):
                                    if conv.get("conv_id") == conversation_id:
                                        conv["updated_date"] = updated_date
                                        conv["parsed_timestamp"] = dt_utc.isoformat()
                                        conv["ticket_id"] = ticket_id
                                        conv["ticket_name"] = ticket_name
                                        # print("found the config", i)
                                        break

                                update_config_file(user_id, client_id, config_data)
                                connection.commit()
                                # print("UPDATED CONFIG FILE 643", msg_id)
                                continue
                else:
                    print("NOT A GMAIL")

                # if found_existing_conversation:
                #     print("found_existing_conversation Line 637")
                #     continue
            else:
                # 3/4) No existing thread → create new thread (inbound/outbound paths)
                print("new conversation creating", direction)
                msg["conversation_id"] = new_conversation_id

                safe_execute(
                    cursor,
                    """
                    INSERT INTO threads (conversation_id, started_at, status, last_message_at,external_user_id)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (new_conversation_id, created_date, "Open", updated_date, user_id),
                )

                if direction == "inbound":
                    print("INBOUND msg", msg_id)
                    # print("MSG BODY", msg_body)
                    ticket_uuid = str(uuid.uuid4())
                    new_ticket_id = f"TKT-{lance_ticket_id}#{ticket_uuid}"
                    # print(f"new inbound TKT {lance_ticket_id} thread {thread_id} clientid {client_id} conv {new_conversation_id}")
                    subj = msg.get("subject")
                    msg["ticket_id"] = new_ticket_id
                    msg["conversation_id"] = new_conversation_id

                    if msg.get("type") == "Customer":
                        ticket_name = subject_map.get(str(msg_id))
                    else:
                        ticket_name = subj or ""
                    msg["ticket_name"] = ticket_name

                    timestamp = msg.get("timestamp")
                    try:
                        dt = datetime.fromisoformat(
                            (timestamp or "").replace("Z", "+00:00")
                        )
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        dt_utc_msg = dt.astimezone(timezone.utc)
                        created_in = dt_utc_msg.strftime("%Y-%m-%d %H:%M:%S")
                    except Exception:
                        created_in = created_date

                    cursor.execute(
                        "SELECT 1 FROM tickets WHERE tickets_id = %s",
                        (new_ticket_id,),
                    )
                    if not cursor.fetchone():
                        communication_id = communication_id_cache.get(client_id)
                        safe_execute(
                            cursor,
                            """
                            INSERT INTO tickets (
                                tickets_id, ticket_name, conversation_id_fk, status, priority,
                                created_in, updated_in, communication_id_fk
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                new_ticket_id,
                                ticket_name,
                                new_conversation_id,
                                "Open",
                                "Medium",
                                created_in,
                                created_in,
                                communication_id,
                            ),
                        )

                        # lance_ticket_id += 1

                        assigned_id = str(uuid.uuid4())
                        safe_execute(
                            cursor,
                            """
                            INSERT INTO assigned (assigned_id, user_id_fk, users_clients_id_fk, ticket_id_fk)
                            VALUES (%s, %s, %s, %s)
                            """,
                            (assigned_id, user_id, client_id, new_ticket_id),
                        )
                        # print("NEW TKT CREATED 718")

                    safe_execute(
                        cursor,
                        """
                        UPDATE threads
                        SET ticket_id_fk = %s
                        WHERE conversation_id = %s
                        """,
                        (new_ticket_id, new_conversation_id),
                    )

                    grouped_messages[client_id][channel] = messages
                    update_or_create_conversation_file(
                        msg, client_id, cursor, batch_count
                    )

                    if msg_id not in existing_ids:
                        cont_ref = (
                            f"{user_id}/messages/{client_id}/{new_conversation_id}.json"
                        )

                        safe_execute(
                            cursor,
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
                                new_conversation_id,
                                client_id,
                                cont_ref,
                                "inbound",
                                subj,
                                created_in,
                                updated_date,
                                channel,
                            ),
                        )
                        existing_ids.add(msg_id)
                        # print("MSG ADDED 762")

                    updated_entry = {
                        "conv_id": new_conversation_id,
                        "ticket_id": new_ticket_id,
                        "ticket_name": ticket_name,
                        "subject": subj,
                        "channel": channel,
                        "updated_date": updated_date,
                        "parsed_timestamp": dt_utc.isoformat(),
                    }
                    if channel == "gmail" and thread_id:
                        updated_entry["thread_id"] = thread_id

                    config_data.setdefault("userclients_id", client_id)
                    config_data.setdefault("conversations", []).append(updated_entry)
                    update_config_file(user_id, client_id, config_data)
                    # print("UPDATED CONFIG FILE 779")
                    connection.commit()

                else:
                    print("OUTBOUND MSG", msg_id)
                    # print("MSG BODY", msg_body)
                    # outbound
                    msg["ticket_id"] = None
                    msg["ticket_name"] = None
                    msg["conversation_id"] = new_conversation_id
                    # print("OUTBOUND placed")

                    safe_execute(
                        cursor,
                        """
                        UPDATE threads
                        SET ticket_id_fk = %s
                        WHERE conversation_id = %s
                        """,
                        (None, new_conversation_id),
                    )

                    grouped_messages[client_id][channel] = messages
                    update_or_create_conversation_file(
                        msg, client_id, cursor, batch_count
                    )

                    if msg_id not in existing_ids:
                        cont_ref = (
                            f"{user_id}/messages/{client_id}/{new_conversation_id}.json"
                        )
                        safe_execute(
                            cursor,
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
                                new_conversation_id,
                                client_id,
                                cont_ref,
                                "outbound",
                                subject,
                                created_date,
                                updated_date,
                                channel,
                            ),
                        )
                        existing_ids.add(msg_id)

                    updated_entry = {
                        "conv_id": new_conversation_id,
                        "ticket_id": None,
                        "ticket_name": None,
                        "subject": subject_map.get(str(msg_id)),
                        "channel": channel,
                        "updated_date": dt_utc.isoformat(),
                    }
                    if channel == "gmail" and thread_id:
                        updated_entry["thread_id"] = thread_id

                    config_data.setdefault("userclients_id", client_id)
                    config_data.setdefault("conversations", []).append(updated_entry)
                    update_config_file(user_id, client_id, config_data)
                    # print("OUTBOUND CONFIG UPDATED")
                    connection.commit()
                    # print(
                    #     f"✅ CASE 4 COMPLETE: Committed new outbound message processing for {msg_id}"
                    # )

    connection.close()
    print(
        f"🏁 EXITING append_subject_to_messages - processed {len(processed_message_ids)} unique messages"
    )
    return "ok"


def update_config_file(user_id, client_id, config_data):
    config_folder = os.path.join(pathconfig.basepath, "messages", user_id, client_id)
    ensure_dir(config_folder)
    config_filepath = os.path.join(config_folder, "config.json")
    s3_config_key = f"{user_id}/messages/{client_id}/config.json"

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


def update_or_create_conversation_file(msg, client_id, cursor, batch_count):
    # print("entered in update_or_create_conversation_file")
    user_id = msg.get("user_id")
    # print("email appended", msg)
    channel = msg.get("source")
    prefix = f"{user_id}/messages/{client_id}"
    config_key = f"{prefix}/config.json"
    config_data = read_json_from_s3(config_key)

    # Pull existing conversation IDs for the specified
    # print(f"getting config dta for : {client_id}")
    if config_data:
        existing_conversations = {
            conv["conv_id"]
            for conv in config_data.get("conversations", [])
            if conv.get("channel") == channel and conv.get("conv_id")
        }
    else:
        existing_conversations = {}

    # print(f"existing_conversations : {existing_conversations}")

    message_id = msg.get("id")
    # print("CURRENT MSGID", message_id)
    # cursor.execute("SELECT 1 FROM messages WHERE message_id = %s", (message_id,))
    # m_id = cursor.fetchone()
    # print("message id found", m_id)
    # if not m_id:

    conv_id = msg.get("conversation_id")

    # print(f"conv_id under process: {conv_id}")

    file_key = f"{prefix}/{conv_id}.json"
    conv_folder = os.path.join(pathconfig.basepath, "messages", user_id, client_id)
    ensure_dir(conv_folder)
    conv_file_name = f"{conv_id}.json"
    conv_filepath = os.path.join(conv_folder, conv_file_name)
    s3_config_key = f"{user_id}/messages/{client_id}/{conv_id}.json"

    lance_file = None
    if conv_id in existing_conversations:
        # print(f"** found an existing conversation", message_id)
        raw_data = read_json_from_s3(file_key)
        input_data = raw_data.get("input_data", [])
        msg = normalize_datetimes(msg)  # check this later
        input_data.append(msg)

        # print(f"*****input_data :{input_data}")

        with open(conv_filepath, "w", encoding="utf-8") as f:
            json.dump({"input_data": input_data}, f, indent=2)

        upload_any_file(
            conv_filepath,
            user_id,
            type="messages",
            s3_key_C=s3_config_key,
        )
        lance_file = input_data

    else:
        # print(f"conv not in existing_conversations", message_id)
        with open(conv_filepath, "w", encoding="utf-8") as f:
            json.dump({"input_data": [msg]}, f, indent=2)

        upload_any_file(
            conv_filepath,
            user_id,
            type="messages",
            s3_key_C=s3_config_key,
        )
        lance_file = msg

    lance_folder = os.path.join(
        pathconfig.basepath, "messages", user_id, f"lance_folder:{batch_count}"
    )
    ensure_dir(lance_folder)
    lance_conv_file_name = f"{user_id}:{client_id}:{conv_id}.json"
    full_file_path = os.path.join(lance_folder, lance_conv_file_name)

    with open(full_file_path, "w", encoding="utf-8") as f:
        json.dump(lance_file, f, indent=2)
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
        return {k: normalize_datetimes(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [normalize_datetimes(item) for item in obj]
    elif isinstance(obj, datetime):
        return obj.isoformat()
    else:
        return obj
