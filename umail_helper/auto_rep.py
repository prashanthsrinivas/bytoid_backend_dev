from datetime import datetime
from db.rds_db import connect_to_rds, get_cursor
from suggest_assist.suggest_helper import helper_make_reply_email
import json


{
    "main.email@gmail.com": {
        "status": "active",
        "last-msg": None,
        "last-conv": None,
        "updated_at": "2025-10-03T15:46:45.136695",
        "selected_agent": "123456789",
    }
}


import json
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed


def autoReplyhelper(all_results, user_id, my_email, pilotvalues, max_workers=5):
    """
    Optimized autopilot email reply handler.

    Features:
    - Fast log lookup (dict)
    - Auto-adds new emails if dynamic mode
    - Includes previously seen emails from pilotvalues logs
    - Batch DB commit
    - Optional parallel sending
    """
    emails_to_check = []

    # 1️⃣ Emails from current messages
    for result in all_results:
        grouped = result.get("grouped_messages", {})
        for conv_id, channels in grouped.items():
            for channel, msgs in channels.items():
                if msgs:
                    first_from = msgs[-1].get("from")
                    if first_from:
                        emails_to_check.append((conv_id, first_from, msgs))

    # 2️⃣ Include old pilot logs emails if missing
    logs = pilotvalues.get("logs", [])
    logs_dict = {e["email"].strip().lower(): e for e in logs}
    # existing_emails = {email.lower() for _, email, _ in emails_to_check}

    # for email_lower, entry in logs_dict.items():
    #     if email_lower not in existing_emails:
    #         emails_to_check.append((None, entry["email"], []))

    if not emails_to_check:
        print("No messages found in all_results or logs.")
        return False

    connection = connect_to_rds()
    try:
        with get_cursor(connection) as cursor:
            mode = pilotvalues.get("mode", "dynamic").lower()

            # Function to process a single email
            def process_email(conv_id, from_email, msgs):
                normalized_email = from_email.strip().lower()
                existing_entry = logs_dict.get(normalized_email)

                # Skip new unknown emails in dynamic mode
                if not existing_entry:
                    if mode == "dynamic":
                        return f"⏩ Skipped new email {from_email} (not in logs, dynamic mode)"
                    elif mode == "all" and normalized_email != my_email.strip().lower():
                        new_entry = {
                            "email": from_email,
                            "status": "active",
                            "last-conv": None,
                            "last-msg": None,
                            "updated_at": datetime.utcnow().isoformat(),
                            "selected_agent": user_id,
                        }
                        logs.append(new_entry)
                        logs_dict[normalized_email] = new_entry
                        existing_entry = new_entry

                is_active = existing_entry and existing_entry.get("status") == "active"
                if mode == "all":
                    if not existing_entry:
                        is_active = True

                if not is_active or not msgs:
                    return f"Skipped {from_email} (inactive or no messages)"

                latest_msg = msgs[-1]
                latest_msg_id = latest_msg.get("id")

                # Skip if last message already handled
                if existing_entry.get("last-msg") == latest_msg_id:
                    return f"Last message already replied for {from_email}"

                if latest_msg.get("direction") != "inbound":
                    return f"Last msg from {from_email} is outbound, skipping"

                # Send reply
                send_val = helper_make_reply_email(
                    baseuserid=user_id, baseemail=from_email, n_connection=connection
                )
                if not send_val:
                    return f"Failed to send reply to {from_email}"

                # Update entry
                now = datetime.utcnow().isoformat()
                selected_agent = existing_entry.get("selected_agent") or user_id
                update_data = {
                    "email": from_email,
                    "status": "active",
                    "last-conv": conv_id,
                    "last-msg": send_val.get("id", latest_msg_id),
                    "updated_at": now,
                    "selected_agent": selected_agent,
                }

                logs_dict[normalized_email] = update_data
                # Also update in logs list
                idx = logs.index(existing_entry)
                logs[idx] = update_data
                return f"✅ Updated autopilot log for {from_email}"

            # Use ThreadPoolExecutor for parallel sending
            results = []
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(process_email, conv_id, email, msgs): email
                    for conv_id, email, msgs in emails_to_check
                }
                for future in as_completed(futures):
                    results.append(future.result())

            # Persist all logs at once
            pilotvalues["logs"] = list(logs_dict.values())
            cursor.execute(
                "UPDATE users SET autopilot = %s WHERE user_id = %s",
                (json.dumps(pilotvalues), user_id),
            )
            connection.commit()

            for r in results:
                print(r)

    except Exception as e:
        print(f"ERROR in autoReplyhelper: {e}")
        return False
    finally:
        connection.close()

    print("Autopilot processing complete.")
    return True
