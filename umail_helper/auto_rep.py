from datetime import datetime, timezone
from db.rds_db import connect_to_rds, get_cursor
from suggest_assist.suggest_helper import helper_make_reply_email
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from utils.normal import can_reply_to_email
import statistics
from dateutil.parser import parse  # pip install python-dateutil if needed


def autoReplyhelper(all_results, user_id, my_email, pilotvalues, max_workers=5):
    """
    Optimized autopilot email reply handler with dynamic threshold AI detection.
    Handles ISO 8601 timestamps with timezone, revokes AI-like fast inbound entries,
    and saves updates to the database.
    """
    emails_to_check = []

    # 1️⃣ Collect emails from current messages
    for result in all_results:
        grouped = result.get("grouped_messages", {})
        for conv_id, channels in grouped.items():
            for channel, msgs in channels.items():
                if msgs:
                    # Sort by timestamp to ensure correct order
                    msgs_sorted = sorted(
                        msgs,
                        key=lambda x: parse(x.get("timestamp", "")),
                        reverse=False,  # earliest → latest
                    )

                    last_msg = msgs_sorted[-1]  # ✅ guaranteed latest
                    # print("actual last message", last_msg)

                    first_from = last_msg.get("from")
                    if first_from and can_reply_to_email(first_from):
                        emails_to_check.append((conv_id, first_from, msgs_sorted))

    if not emails_to_check:
        print("No messages found in all_results.")
        return False

    logs = pilotvalues.get("logs", [])
    logs_dict = {e["email"].strip().lower(): e for e in logs}

    connection = connect_to_rds()
    try:
        with get_cursor(connection) as cursor:
            mode = pilotvalues.get("mode", "dynamic").lower()

            # --- Helper: analyze conversation with dynamic threshold ---
            def analyze_inbound_outbound(
                msgs, base_threshold=30, similarity_factor=0.5, updated_after=None
            ):
                """
                Analyze inbound/outbound message timestamps and detect AI-like fast replies.
                Converts timestamps to UTC for consistent comparison.
                Ignores messages before `updated_after` if provided.
                """
                time_diffs = []
                revoked = False
                reason = None
                print("analyzing started")

                msgs_sorted = sorted(msgs, key=lambda x: x["timestamp"])
                last_outbound_ts = None
                outbound_diffs = []

                for msg in msgs_sorted:
                    direction = msg.get("direction")
                    ts_str = msg.get("timestamp")
                    if not ts_str:
                        continue

                    try:
                        ts = parse(ts_str).astimezone(timezone.utc)
                    except Exception:
                        continue

                    # ⏩ Ignore messages before last updated time
                    if updated_after and ts <= updated_after:
                        continue

                    if direction == "outbound":
                        last_outbound_ts = ts
                    elif direction == "inbound" and last_outbound_ts:
                        diff = (ts - last_outbound_ts).total_seconds()
                        time_diffs.append(diff)
                        outbound_diffs.append(diff)

                # Compute dynamic threshold
                if outbound_diffs:
                    typical_delay = statistics.median(outbound_diffs)
                    dynamic_threshold = max(
                        typical_delay * similarity_factor, base_threshold
                    )
                else:
                    dynamic_threshold = base_threshold

                for diff in time_diffs:
                    if diff < base_threshold:
                        revoked = True
                        reason = (
                            f"revoked due to AI-like fast messages ({base_threshold}s)"
                        )
                        break
                    elif diff < dynamic_threshold:
                        revoked = True
                        reason = f"revoked due to AI-like fast dynamic messages ({dynamic_threshold:.1f}s)"
                        break
                print("returning analyze inbound outbound", time_diffs, revoked, reason)

                return time_diffs, revoked, reason

            # --- Process single email ---
            def process_email(conv_id, from_email, msgs):
                normalized_email = from_email.strip().lower()
                # print("normal", normalized_email)
                existing_entry = logs_dict.get(normalized_email)
                latest_msg = msgs[-1]
                # print("last message", normalized_email, type(latest_msg))
                latest_msg_id = latest_msg.get("id")

                # Skip if last message already handled or last msg is outbound
                if existing_entry and existing_entry.get("last-msg") == latest_msg_id:
                    # print("already replied")
                    return f"Last message already replied for {from_email}"
                if latest_msg.get("direction") != "inbound":
                    # print("skipping value outbound")
                    return f"Last msg from {from_email} is outbound, skipping"

                # Skip or add new email
                if not existing_entry:
                    print("a new email statted ")
                    if mode == "dynamic":
                        # print("dynamic val")
                        return f"⏩ Skipped new email {from_email} (dynamic mode)"
                    elif mode == "all" and normalized_email != my_email.strip().lower():
                        new_entry = {
                            "email": from_email,
                            "status": "active",
                            "last-conv": None,
                            "last-msg": None,
                            "updated_at": datetime.utcnow().isoformat(),
                            "selected_agent": user_id,
                            "reason": "new email added",
                        }
                        logs.append(new_entry)
                        logs_dict[normalized_email] = new_entry
                        existing_entry = new_entry
                        # print("new entry")

                if (
                    not existing_entry
                    or existing_entry.get("status") != "active"
                    or not msgs
                ):
                    # print(f"Skippedddddddd {from_email} (inactive or no messages)")
                    return f"Skipped {from_email} (inactive or no messages)"

                # ⏰ Determine updated_after (only for this email)
                updated_after = None
                try:
                    if existing_entry.get("updated_at"):
                        updated_after = parse(existing_entry["updated_at"]).astimezone(
                            timezone.utc
                        )
                except Exception:
                    updated_after = None

                # Analyze conversation for AI-like fast inbound
                time_diffs, revoked, reason = analyze_inbound_outbound(
                    msgs, updated_after=updated_after
                )

                # Revoke if AI-like fast inbound after 6th message
                if revoked and len(msgs) > 6:
                    existing_entry["status"] = "revoked"
                    existing_entry["reason"] = reason
                    logs_dict[normalized_email] = existing_entry
                    # Update logs list safely
                    for i, log in enumerate(logs):
                        if log.get("email", "").strip().lower() == normalized_email:
                            logs[i] = existing_entry
                            break
                    return (
                        f"⚠️ {from_email} revoked due to AI-like fast inbound messages"
                    )

                # Send reply
                send_val = helper_make_reply_email(
                    baseuserid=user_id, baseemail=from_email, n_connection=connection
                )
                if not send_val:
                    return f"Failed to send reply to {from_email}"

                now = datetime.utcnow().isoformat()
                selected_agent = existing_entry.get("selected_agent") or user_id
                update_data = {
                    "email": from_email,
                    "status": existing_entry.get("status", "active"),
                    "last-conv": conv_id,
                    "last-msg": send_val.get("id", latest_msg_id),
                    "updated_at": now,
                    "selected_agent": selected_agent,
                    "reason": "autopilot success",
                }

                logs_dict[normalized_email] = update_data
                # Update logs list safely
                for i, log in enumerate(logs):
                    if log.get("email", "").strip().lower() == normalized_email:
                        logs[i] = update_data
                        break

                return f"✅ Updated autopilot log for {from_email}, time_diffs={time_diffs}"

            # --- Parallel processing ---
            results = []
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(process_email, conv_id, email, msgs): email
                    for conv_id, email, msgs in emails_to_check
                }
                for future in as_completed(futures):
                    results.append(future.result())

            # Persist all logs to pilotvalues and DB
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
    return True
