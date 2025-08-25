from datetime import datetime, timezone
from gmail_route.routes import fetch_gmail_messages_batch
from umail_helper.mails_process import analyze_and_collect_messages_for_batch
import asyncio
import os
from cust_helpers import pathconfig
from umail_lance.umail_lance_agent import UmailLanceClient





async def getall_continuous(user_id):
    """
    Continuously fetch and process batches of 100 - 200 emails
    """
    timestamp = datetime.now(timezone.utc)
    date_str = timestamp.strftime("%Y-%m-%d")
    file_loc = f"cust_helpers/messages/{user_id}/{date_str}"
    
    total_processed = 0
    batch_count = 0
    next_page_token = None
    batch_size = 100
    
    count = 0

    print(f"🚀 Starting continuous batch processing for user {user_id}")
    
    while True:
        batch_count += 1
        print(f"\n📦 Processing batch {batch_count} (batch size: {batch_size})")
        
        # Fetch Gmail batch
        gmail_result = await fetch_gmail_messages_batch(
            user_id, 
            page_token=next_page_token,
            batch_size=batch_size
        )
        
        if gmail_result.get("status") != "success":
            print(f"❌ Gmail batch {batch_count} failed: {gmail_result.get('error')}")
            break
            
        new_messages = gmail_result.get("new_messages", 0)
        next_page_token = gmail_result.get("next_page_token")
        current_batch_messages = gmail_result.get("grouped_messages", {})
        
        if new_messages == 0:
            print(f"📭 No new messages in batch {batch_count}")
            if not next_page_token:
                print("🏁 No more emails to fetch")
                break
        else:
            print(f"📬 Batch {batch_count}: {new_messages} new messages")
            print(f"👥 Client IDs in batch {batch_count}: {list(current_batch_messages.keys())}")
            total_processed += new_messages
            
            # Analyze ONLY current batch data
            print(f"🔍 Analyzing batch {batch_count}...")
            analyze_and_collect_messages_for_batch(user_id, current_batch_messages,batch_count)
            print(f"✅ Batch {batch_count} analysis complete")
            lance_folder =   os.path.join(
                            pathconfig.basepath, "messages", user_id,f"lance_folder:{batch_count}" 
                        )
            print("calling embedding function")
            client = UmailLanceClient(user_id)  
            client.embed_json_files(lance_folder)
            

            

        
        # Check if there are more pages
        if not next_page_token:
            print("🏁 Reached end of available emails")
            break
            
        print(f"📊 Total processed so far: {total_processed} emails across {batch_count} batches")
        
        # Optional: Add delay between batches
        await asyncio.sleep(1)
    
    print(f"✅ All batches complete! Total: {total_processed} emails in {batch_count} batches")
    return f"OK - Processed {total_processed} emails in {batch_count} batches"


# actual function:
# @umail_bp.route("/get_all_messages/<user_id>", methods=["GET"])
# def getall(user_id):

#     timestamp = datetime.now(timezone.utc)
#     date_str = timestamp.strftime("%Y-%m-%d")
#     file_loc = f"cust_helpers/messages/{user_id}/{date_str}"
#     gmail = gmail_messages(user_id)
#     zoho = fetch_zoho_emails(user_id)

#     analyze_and_collect_messages(user_id)

#     return "OK"
