"""
Debug issues with finding messages in your search function
"""

# Your message data
target_message = {
    "id": "<863396117.6937594.1758096748131@lva1-app100614.prod.linkedin.com>",
    "from": "invitations@linkedin.com",
    "to": "abc@gmail.com",
    "body": "Daya, HR Executive from Cloudium Software is waiting for your response...",
    "subject": "I want to connect",
    "timestamp": "2025-09-17T08:12:28+00:00",
    "source": "gmail",
    "direction": "inbound",
    "user_id": "112359636982080060072",
    "thread_id": "19956bb6eb5c24fa",
    "conversation_id": "1988acec-f92e-4bbf-9a0f-f13b95ce1e3d",
    "type": "Lead",
    "ticket_id": "TKT-1#aa24ef3c-0a40-4b83-bc78-88a8b0b901be",
    "ticket_name": "I want to connect",
}


def debug_search_issues():
    """
    Potential issues and fixes for message not being found
    """

    # print("=== POTENTIAL ISSUES ===\n")

    # Issue 1: Message ID format
    # print("1. MESSAGE ID FORMAT ISSUE:")
    print(f"Message ID: {target_message['id']}")
    # print("- Contains special characters: < > @ .")
    # print("- May need escaping in SQL queries")
    # print("- Could be causing SQL syntax errors")
    print()

    # Issue 2: Ticket ID format mismatch
    # print("2. TICKET ID FORMAT MISMATCH:")
    print(f"Actual ticket_id: {target_message['ticket_id']}")
    # print("Expected in SQL: TKT-1#%%")
    # print("- Your SQL uses: WHERE t.tickets_id LIKE 'TKT-{ticket_id}#%%'")
    # print("- But actual format is: TKT-1#aa24ef3c-0a40-4b83-bc78-88a8b0b901be")
    # print("- The LIKE pattern should match!")
    print()

    # Issue 3: Date range
    # print("3. DATE RANGE ISSUE:")
    print(f"Message timestamp: {target_message['timestamp']}")
    # print("- Check if your date range includes 2025-09-17T08:12:28+00:00")
    # print("- Verify timezone handling")
    print()

    # Issue 4: User ID mismatch
    # print("4. USER ID MISMATCH:")
    print(f"Message user_id: {target_message['user_id']}")
    # print("- Ensure the user_id parameter matches exactly")
    # print("- Check if you're using external_user_id vs user_id correctly")
    print()

    # Issue 5: Data structure issues
    # print("5. DATA STRUCTURE ISSUES:")
    # print("- Check if input_data is list vs dict")
    # print("- Verify S3 key path is correct")
    # print("- Ensure message parsing logic handles the structure")
    print()


def create_debug_search_function():
    """
    Enhanced search function with debugging
    """

    code = '''
def search_db_with_debug(db_queries, user_id, cursor):
    """
    Debug version of search function
    """
    s3_results = []
    seen_message_ids = set()
    debug_info = {"queries_executed": [], "messages_found": [], "errors": []}
    
    for query_index, db_query in enumerate(db_queries):
        print(f"\\n=== Executing Query {query_index + 1} ===")
        print(f"Query: {db_query}")
        print(f"User ID: {user_id}")
        
        try:
            # Time-based queries
            if 'message_id' in db_query:
                cursor.execute(db_query, (user_id,))
                db_results = cursor.fetchall()
                print(f"Time query returned {len(db_results)} rows")
                debug_info["queries_executed"].append(f"Time query: {len(db_results)} rows")
                
                for row_index, row in enumerate(db_results):
                    message_id = row[0]
                    s3_conv_key = row[1]
                    print(f"  Row {row_index}: message_id={message_id}, s3_key={s3_conv_key}")
                    
                    if message_id in seen_message_ids:
                        print(f"    Skipping duplicate message_id: {message_id}")
                        continue
                    
                    try:
                        raw_data = read_json_from_s3(s3_conv_key)
                        input_data = raw_data.get("input_data", [])
                        print(f"    S3 data type: {type(input_data)}")
                        
                        if isinstance(input_data, list):
                            print(f"    Searching in list of {len(input_data)} messages")
                            matching_messages = [msg for msg in input_data if msg.get("id") == message_id]
                            print(f"    Found {len(matching_messages)} matching ")
                            
                            if matching_messages:
                                message_data = matching_messages[0]
                                print(f"    Match found: {message_data.get('id')}")
                                
                                last_message = {
                                    "body": message_data.get("body"),
                                    "message_id": message_data.get("id"),
                                    "conversation_id": message_data.get("conversation_id"),
                                    "from": message_data.get("from"),
                                    "ticket_name": message_data.get("ticket_name"),
                                    "ticket_id": message_data.get("ticket_id"),
                                    "timestamp": message_data.get("timestamp")
                                }
                                s3_results.append(last_message)
                                seen_message_ids.add(message_id)
                                debug_info["messages_found"].append(message_id)
                            else:
                                print(f"    No matching message found for ID: {message_id}")
                                
                        elif isinstance(input_data, dict):
                            print(f"    Processing single message dict")
                            if input_data.get("id") == message_id:
                                print(f"    Dict match found: {input_data.get('id')}")
                                last_message = {
                                    "body": input_data.get("body"),
                                    "message_id": input_data.get("id"),
                                    "conversation_id": input_data.get("conversation_id"),
                                    "from": input_data.get("from"),
                                    "ticket_name": input_data.get("ticket_name"),
                                    "ticket_id": input_data.get("ticket_id"),
                                    "timestamp": input_data.get("timestamp")
                                }
                                s3_results.append(last_message)
                                seen_message_ids.add(message_id)
                                debug_info["messages_found"].append(message_id)
                            else:
                                print(f"    Dict ID mismatch: expected {message_id}, got {input_data.get('id')}")
                                
                    except Exception as s3_error:
                        error_msg = f"S3 read error for key {s3_conv_key}: {str(s3_error)}"
                        print(f"    ERROR: {error_msg}")
                        debug_info["errors"].append(error_msg)
            
            # Ticket-based queries            
            elif 'conversation_id_fk' in db_query:
                cursor.execute(db_query, (user_id,))
                db_results = cursor.fetchall()
                print(f"Ticket query returned {len(db_results)} rows")
                debug_info["queries_executed"].append(f"Ticket query: {len(db_results)} rows")
                
                for row_index, row in enumerate(db_results):
                    conversation_id = row[0]
                    print(f"  Row {row_index}: conversation_id={conversation_id}")
                    
                    # Get message content_ref for this conversation
                    sql_query = "select message_id, content_ref from messages where conversation_id_fk = %s"
                    cursor.execute(sql_query, (conversation_id,))
                    message_rows = cursor.fetchall()
                    print(f"    Found {len(message_rows)} messages in conversation")
                    
                    for msg_row in message_rows:
                        message_id = msg_row[0]
                        s3_conv_key = msg_row[1]
                        print(f"    Processing message: {message_id}")
                        
                        if message_id in seen_message_ids:
                            print(f"      Skipping duplicate: {message_id}")
                            continue
                        
                        try:
                            raw_data = read_json_from_s3(s3_conv_key)
                            input_data = raw_data.get("input_data", [])
                            
                            # Process the message data...
                            # (Similar processing as above)
                            
                        except Exception as s3_error:
                            error_msg = f"S3 read error: {str(s3_error)}"
                            print(f"      ERROR: {error_msg}")
                            debug_info["errors"].append(error_msg)
                        
        except Exception as query_error:
            error_msg = f"Query {query_index + 1} error: {str(query_error)}"
            print(f"ERROR: {error_msg}")
            debug_info["errors"].append(error_msg)
    
    print(f"\\n=== FINAL RESULTS ===")
    print(f"Total messages found: {len(s3_results)}")
    print(f"Unique message IDs: {len(seen_message_ids)}")
    print(f"Debug info: {debug_info}")
    
    return s3_results, debug_info
'''

    return code


def check_sql_queries():
    """
    Check if SQL queries would find the target message
    """
    # print("=== SQL QUERY CHECKS ===\\n")

    # Check time-based query
    # print("1. TIME-BASED QUERY:")
    time_query = """
    SELECT m.message_id, m.content_ref 
    FROM messages m 
    JOIN threads th ON m.conversation_id_fk = th.conversation_id 
    WHERE th.external_user_id = %s 
    AND m.created_at >= '2025-09-17T00:00:00+00:00' 
    AND m.created_at <= '2025-09-17T23:59:59+00:00'
    """
    print(f"Query: {time_query.strip()}")
    print(f"Target timestamp: {target_message['timestamp']}")
    # print("✓ Should match if message exists in database")
    print()

    # Check ticket-based query
    # print("2. TICKET-BASED QUERY:")
    ticket_query = """
    SELECT t.conversation_id_fk 
    FROM tickets t 
    JOIN communication c ON t.communication_id_fk = c.communication_id 
    WHERE t.tickets_id LIKE 'TKT-1#%%' 
    AND c.user_id_fk = %s
    """
    print(f"Query: {ticket_query.strip()}")
    print(f"Target ticket_id: {target_message['ticket_id']}")
    # print("✓ Should match: TKT-1#aa24ef3c-0a40-4b83-bc78-88a8b0b901be matches TKT-1#%%")
    print()


# Run the debug analysis
if __name__ == "__main__":
    debug_search_issues()
    # print("\\n" + "="*50 + "\\n")
    check_sql_queries()

# print("\\n=== DEBUGGING STEPS ===")
# print("1. Add debug prints to your search function")
# print("2. Check SQL query results before S3 processing")
# print("3. Verify S3 key paths and JSON structure")
# print("4. Ensure message ID matching logic is correct")
# print("5. Check for any SQL escaping issues with special characters")
