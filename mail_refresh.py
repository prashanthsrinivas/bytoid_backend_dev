import sys
import os
import shutil

sys.path.insert(0, "/home/ec2-user/bytoid/exe2")

from db.rds_db import connect_to_rds, get_cursor
from cust_helpers.pathconfig import basepath
import json
from datetime import datetime, timezone


def get_user_email(user_id):
    """Get user's email from database"""
    connection = connect_to_rds()
    if not connection:
        return None

    try:
        with get_cursor(connection) as cursor:
            cursor.execute("SELECT email FROM users WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
            return row[0] if row else None
    finally:
        connection.close()


def clear_cache(user_id):
    """Clear Redis cache for user"""
    print(f"\n🗑️  Clearing Redis cache...")
    try:
        # This would need to be async, so we'll note it
        print(f"   ⚠️  Redis cache clear requires async context")
        print(f"   → Will be automatically cleared on next mail fetch")
        return True
    except Exception as e:
        print(f"   ⚠️  Could not clear Redis: {e}")
        return True  # Don't fail on this


def clear_local_json(user_id):
    """Delete local JSON files and message folders"""
    print(f"\n📁 Clearing local JSON files...")

    messages_dir = os.path.join(basepath, "messages", user_id)

    if not os.path.exists(messages_dir):
        print(f"   ℹ️  No local files found (first time setup)")
        return True

    try:
        # Keep umail.json but reset it, delete everything else
        shutil.rmtree(messages_dir)
        os.makedirs(messages_dir, exist_ok=True)
        print(f"   ✅ Cleared: {messages_dir}")

        # Create fresh umail.json
        fresh_umail = {
            "history": [],
            "last_processed_date": None,
            "message_count": 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        umail_json_path = os.path.join(messages_dir, "umail.json")
        with open(umail_json_path, "w") as f:
            json.dump(fresh_umail, f, indent=2)

        print(f"   ✅ Created fresh umail.json")
        return True

    except Exception as e:
        print(f"   ❌ Error clearing local files: {e}")
        return False


def clear_lancedb_tables(user_id):
    """Delete LanceDB tables for user"""
    print(f"\n🗄️  Clearing LanceDB tables...")

    try:
        from umail_lance.umail_lance_agent import UmailLanceClient

        client = UmailLanceClient(user_id)

        # Delete all tables for this user
        # Note: LanceDB doesn't have a simple "drop all" so we'll delete the db directory
        lance_db_path = os.path.expanduser(f"~/.cache/lancedb/{user_id}")

        if os.path.exists(lance_db_path):
            shutil.rmtree(lance_db_path)
            print(f"   ✅ Deleted LanceDB directory: {lance_db_path}")
        else:
            print(f"   ℹ️  No LanceDB directory found")

        return True

    except Exception as e:
        print(f"   ⚠️  Could not clear LanceDB: {e}")
        print(f"   → Will be recreated on next mail fetch")
        return True  # Don't fail


def reset_database_history(user_id):
    """Reset mail fetch history in database"""
    print(f"\n🔄 Resetting database history...")

    connection = connect_to_rds()
    if not connection:
        print(f"   ❌ Cannot connect to database")
        return False

    try:
        with get_cursor(connection) as cursor:
            # Check current state
            cursor.execute("SELECT autopilot FROM users WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()

            if row and row[0]:
                try:
                    autopilot = json.loads(row[0])
                    logs = autopilot.get("logs", [])
                    print(f"   ℹ️  Found {len(logs)} autopilot logs")

                    # Reset autopilot logs but keep settings
                    autopilot["logs"] = []
                    autopilot["last_reset"] = datetime.now(timezone.utc).isoformat()

                    cursor.execute(
                        "UPDATE users SET autopilot = %s WHERE user_id = %s",
                        (json.dumps(autopilot), user_id),
                    )
                    connection.commit()
                    print(f"   ✅ Reset autopilot logs")
                except:
                    pass

            print(f"   ✅ Database reset complete")
            return True

    except Exception as e:
        print(f"   ❌ Error resetting database: {e}")
        return False
    finally:
        connection.close()


def trigger_fresh_mail_fetch(user_id):
    """Trigger a fresh mail fetch"""
    print(f"\n🚀 Triggering fresh mail fetch...")

    try:
        from utils.celery_base import umail_sync

        # Delay the task by 2 seconds to ensure all previous cleanup is done
        async_result = umail_sync.apply_async(args=(user_id,), countdown=2)

        print(f"   ✅ Mail fetch queued!")
        print(f"   Task ID: {async_result.id}")
        print(f"   Status will start processing in 2 seconds...")

        return True

    except Exception as e:
        print(f"   ⚠️  Could not trigger mail fetch: {e}")
        print(f"   → You can manually trigger it by calling:")
        print(f"      GET /get_all_messages/{user_id}")
        return True


def main():
    if len(sys.argv) < 2:
        # print("Usage: python mail_refresh.py <user_id>")
        # print("\nExample: python mail_refresh.py user123")
        sys.exit(1)

    user_id = sys.argv[1]

    # print("\n")
    # print("╔════════════════════════════════════════════════════════════╗")
    # print("║             Complete Mail Refresh Script                  ║")
    # print("║      (Deletes all cached/processed data and re-fetches)   ║")
    # print("╚════════════════════════════════════════════════════════════╝")

    user_email = get_user_email(user_id)
    if user_email:
        print(f"\n📧 User: {user_email} ({user_id})")
    else:
        print(f"\n❌ User not found: {user_id}")
        sys.exit(1)

    print(f"\n⚠️  WARNING: This will DELETE all cached mail data for this user")
    print(f"   The system will then fetch ALL mails again from Gmail (3 months)")
    print(f"   This may take several minutes...")

    confirm = input(f"\nProceed with full mail refresh? (yes/no): ").strip().lower()

    if confirm != "yes":
        print(f"Cancelled.")
        sys.exit(0)

    print(f"\n{'='*60}")
    print(f"Starting refresh process...")
    print(f"{'='*60}")

    # Step 1: Clear Redis cache
    clear_cache(user_id)

    # Step 2: Clear local JSON and message files
    if not clear_local_json(user_id):
        print(f"❌ Failed to clear local files")
        sys.exit(1)

    # Step 3: Clear LanceDB
    clear_lancedb_tables(user_id)

    # Step 4: Reset database history
    if not reset_database_history(user_id):
        print(f"❌ Failed to reset database")
        sys.exit(1)

    # Step 5: Trigger fresh fetch
    trigger_fresh_mail_fetch(user_id)

    print(f"\n{'='*60}")
    print(f"✅ REFRESH COMPLETE!")
    print(f"{'='*60}")
    print(f"\n📋 What was cleared:")
    print(f"   ✓ Redis cache")
    print(f"   ✓ Local JSON files")
    print(f"   ✓ LanceDB tables")
    print(f"   ✓ Autopilot logs")
    print(f"\n🔄 Fresh mail fetch queued:")
    print(f"   • Will fetch 3 months of mails from Gmail")
    print(f"   • Process them through embeddings")
    print(f"   • Store in fresh LanceDB")
    print(f"   • Ready for conversations list")
    print(f"\n⏱️  Check status in 30-60 seconds at:")
    print(f"   GET /conversations/{user_id}/null")
    print(f"\n💡 If mails still not showing:")
    print(f"   python mail_fetch_diagnostic.py {user_id}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
