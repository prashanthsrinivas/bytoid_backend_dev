from flask import Blueprint, request, jsonify,session
import asyncio
from microsoft_route.routes import microsoft_list_drafts
from gmail_route.routes import list_drafts
from create_db import connect_to_rds
from utils.s3_utils import read_json_from_s3
from datetime import datetime
import json
import traceback




unified_bp = Blueprint('unified', __name__)


@unified_bp.route('/unified_drafts')
def unified_drafts():
    emails = asyncio.run(get_all_drafts())
    return jsonify(emails)


async def get_all_drafts():

    gmail_task = list_drafts()
    outlook_task = microsoft_list_drafts()
    gmail_emails, outlook_emails = await asyncio.gather(gmail_task, outlook_task)
    return {
        'gmail': gmail_emails,
        'outlook': outlook_emails
    }



def get_latest_msg(content_dict, user_id):

    messages = []

    for msg_id, content_ref in content_dict.items():

        s3_conv_key = content_ref
        raw_data = read_json_from_s3(s3_conv_key)
        input_data = raw_data.get("input_data", [])
        if isinstance(input_data, list) and input_data:
            messages.append(input_data[-1])

        elif isinstance(input_data, dict) and input_data:
            messages.append(input_data)

    return messages


@unified_bp.route('/get_active_customers', methods = ['POST'])
def get_active_customers():
    
    data = request.get_json()
    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"error": "user id needed"}), 400
    
    connection = connect_to_rds()
    if connection is None:
        print("❌ [DEBUG] Database connection failed")
        return jsonify({"error": "Database connection failed"}), 500
    cursor = connection.cursor()

    content_dict = {}
    query = """
        SELECT 
            m.sender_id,
            m.content_ref as content
        FROM messages m
        JOIN users_clients uc ON m.sender_id = uc.users_clients_id
        JOIN communication c ON uc.communication_id_fk = c.communication_id
        WHERE uc.type = 'Customer'
        AND m.message_type = 'inbound'
        AND m.created_at >= NOW() - INTERVAL 7 DAY
        AND c.user_id_fk = %s
        GROUP BY m.sender_id
        ORDER BY MAX(m.created_at) DESC

        """  
    cursor.execute(query, (user_id,))
    rows = cursor.fetchall()

    if rows:    
            for row in rows: 
                msg_id = row[0]
                content_ref = row[1]
                content_dict[msg_id] = content_ref
    

    print(f"content_dict lenght : {len(content_dict)}")
    messages = get_latest_msg(content_dict, user_id)
        
    return jsonify(messages)


@unified_bp.route('/get_dormant_customers', methods = ['POST'])
def get_dormant_customers():
    
    data = request.get_json()
    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"error": "user id needed"}), 400
    
    connection = connect_to_rds()
    if connection is None:
        print("❌ [DEBUG] Database connection failed")
        return jsonify({"error": "Database connection failed"}), 500
    cursor = connection.cursor()

    content_dict = {}
    query = """
        SELECT 
            m.sender_id,
            m.content_ref as content
        FROM messages m
        JOIN users_clients uc ON m.sender_id = uc.users_clients_id
        JOIN communication c ON uc.communication_id_fk = c.communication_id
        WHERE uc.type = 'Customer'
        AND m.message_type = 'inbound'
        AND m.created_at <= NOW() - INTERVAL 7 DAY
        AND c.user_id_fk = %s
        GROUP BY m.sender_id
        ORDER BY MAX(m.created_at) DESC
        """  
    cursor.execute(query, (user_id,))
    rows = cursor.fetchall()

    if rows:    
            for row in rows: 
                msg_id = row[0]
                content_ref = row[1]
                content_dict[msg_id] = content_ref
    

    print(f"content_dict lenght : {len(content_dict)}")
    messages = get_latest_msg(content_dict, user_id)
        
    return jsonify(messages)


@unified_bp.route('/get_active_leads', methods = ['POST'])
def get_active_leads():
    
    data = request.get_json()
    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"error": "user id needed"}), 400
    
    connection = connect_to_rds()
    if connection is None:
        print("❌ [DEBUG] Database connection failed")
        return jsonify({"error": "Database connection failed"}), 500
    cursor = connection.cursor()

  
    content_dict = {}
    query = """
        SELECT 
            m.sender_id,
            m.content_ref as content
        FROM messages m
        JOIN users_clients uc ON m.sender_id = uc.users_clients_id
        JOIN communication c ON uc.communication_id_fk = c.communication_id
        WHERE uc.type = 'Lead'
        AND m.message_type = 'inbound'
        AND m.created_at >= NOW() - INTERVAL 7 DAY
        AND c.user_id_fk = %s
        GROUP BY m.sender_id
        ORDER BY MAX(m.created_at) DESC

        """  
    cursor.execute(query, (user_id,))
    rows = cursor.fetchall()

    if rows:    
            for row in rows: 
                msg_id = row[0]
                content_ref = row[1]
                content_dict[msg_id] = content_ref
    

    print(f"content_dict lenght : {len(content_dict)}")
    messages = get_latest_msg(content_dict, user_id)
        
    
    return jsonify(messages)


@unified_bp.route('/get_dormant_leads', methods = ['POST'])
def get_dormant_leads():
    
    data = request.get_json()
    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"error": "user id needed"}), 400
    
    connection = connect_to_rds()
    if connection is None:
        print("❌ [DEBUG] Database connection failed")
        return jsonify({"error": "Database connection failed"}), 500
    cursor = connection.cursor()

    content_dict = {}
    query = """
        SELECT 
            m.message_id,
            m.content_ref as content
        FROM messages m
        JOIN users_clients uc ON m.sender_id = uc.users_clients_id
        JOIN communication c ON uc.communication_id_fk = c.communication_id
        WHERE uc.type = 'Lead'
        AND m.message_type = 'inbound'
        AND m.created_at <= NOW() - INTERVAL 7 DAY
        AND c.user_id_fk = %s
        GROUP BY m.sender_id
        ORDER BY MAX(m.created_at) DESC
    
        """  
    cursor.execute(query, (user_id,))
    rows = cursor.fetchall()

    if rows:    
            for row in rows: 
                msg_id = row[0]
                content_ref = row[1]
                content_dict[msg_id] = content_ref
    

    print(f"content_dict lenght : {len(content_dict)}")
    messages = get_latest_msg(content_dict, user_id)
        
    return jsonify(messages)


@unified_bp.route('/get_snoozed_customers', methods = ['POST'])
def get_snoozed_customers():
    
    try:

        data = request.get_json()
        user_id = data.get("user_id")
        if not user_id:
            return jsonify({"error": "user id needed"}), 400
        
        connection = connect_to_rds()
        if connection is None:
            print("❌ [DEBUG] Database connection failed")
            return jsonify({"error": "Database connection failed"}), 500
        cursor = connection.cursor()

        content_dict = {}
        query = """
            SELECT 
                m.sender_id,
                m.content_ref as content
            FROM messages m
            JOIN users_clients uc ON m.sender_id = uc.users_clients_id
            JOIN communication c ON uc.communication_id_fk = c.communication_id
            WHERE uc.snooze=1
            AND c.user_id_fk = %s
            GROUP BY m.sender_id
            ORDER BY MAX(m.created_at) DESC
            """  
        cursor.execute(query, (user_id,))
        rows = cursor.fetchall()

        if rows:    
                for row in rows: 
                    msg_id = row[0]
                    content_ref = row[1]
                    content_dict[msg_id] = content_ref
        

        print(f"content_dict lenght : {len(content_dict)}")
        messages = get_latest_msg(content_dict, user_id)
            
        return jsonify(messages)

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        if cursor:
            cursor.close() 

@unified_bp.route('/snooze_customer', methods=['POST'])
def snooze_customer():

    try:
        data = request.get_json()
        user_id = data.get("user_id")
        conversation_id = data.get("conversation_id")
        print(f"conversation_id :{conversation_id}")
        print(f"usrs_id: {user_id}")

        connection = connect_to_rds()
        if connection is None:
            print("❌ [DEBUG] Database connection failed")
            return jsonify({"error": "Database connection failed"}), 500
        cursor = connection.cursor()

        cursor.execute(
                        "SELECT sender_id FROM messages WHERE conversation_id_fk = %s",
                        (conversation_id,),
                    )
        client_id_row = cursor.fetchone()  
        if client_id_row:
            client_id = client_id_row[0]  

        else:
            return jsonify(
                    {
                        "message": f"⚠️ No sender_id found for conversation_id {conversation_id}"
                    }
                    ),404,
        
        print(f"client_id:{client_id}")
   
        cursor.execute(
            "UPDATE users_clients SET snooze = CASE WHEN snooze = 0 THEN 1 ELSE 0 END WHERE users_clients_id = %s",
            (client_id,),
        )
        
        connection.commit()
        return jsonify({
                        "message": "success",
                        "client_id":client_id
                        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

    

@unified_bp.route('/get_no_of_customers', methods=['POST'])
def get_no_of_customers():
    
    try:
         
        data = request.get_json()
        user_id = data.get("user_id")
        if not user_id:
            return jsonify({"error": "user id needed"}), 400
        
        connection = connect_to_rds()
        if connection is None:
            print("❌ [DEBUG] Database connection failed")
            return jsonify({"error": "Database connection failed"}), 500
        cursor = connection.cursor()

        query = """
        SELECT
                -- Active Customers
            COUNT(DISTINCT CASE 
                WHEN uc.type = 'Customer'
                AND m.message_type = 'inbound'
                AND m.created_at >= NOW() - INTERVAL 7 DAY
                THEN m.sender_id END) AS active_customers,

            -- Dormant Customers
            COUNT(DISTINCT CASE 
                WHEN uc.type = 'Customer'
                AND m.message_type = 'inbound'
                AND m.created_at < NOW() - INTERVAL 7 DAY
                THEN m.sender_id END) AS dormant_customers,

            -- Active Leads
            COUNT(DISTINCT CASE 
                WHEN uc.type = 'Lead'
                AND m.message_type = 'inbound'
                AND m.created_at >= NOW() - INTERVAL 7 DAY
                THEN m.sender_id END) AS active_leads,

            -- Dormant Leads
            COUNT(DISTINCT CASE 
                WHEN uc.type = 'Lead'
                AND m.message_type = 'inbound'
                AND m.created_at < NOW() - INTERVAL 7 DAY
                THEN m.sender_id END) AS dormant_leads

        FROM messages m
        JOIN users_clients uc ON m.sender_id = uc.users_clients_id
        JOIN communication c ON uc.communication_id_fk = c.communication_id
        WHERE c.user_id_fk = %s;
        """

        cursor.execute(query, (user_id,))
        row = cursor.fetchone()

        if row:
            active_customers   = row[0]
            dormant_customers  = row[1]
            active_leads       = row[2]
            dormant_leads      = row[3]


        # count of snoozed cusomters 
        snoozed_cust_query = """ SELECT COUNT(*) AS message_count 
                                FROM users_clients uc 
                                JOIN communication c ON uc.communication_id_fk = c.communication_id 
                                WHERE  uc.snooze=1 
                                AND c.user_id_fk = %s; 
                            """ 
        cursor.execute(snoozed_cust_query, (user_id,)) 
        rows = cursor.fetchone() 
        if rows: 
            snoozed_customers = rows[0]
        return jsonify({
            "active_customers": active_customers,
            "dormant_customers": dormant_customers,
            "snoozed_customers": snoozed_customers,
            "active_leads": active_leads,
            "dormant_leads": dormant_leads
            
        })
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()
        
        

@unified_bp.route('/change_assignee', methods = ['POST'])
def change_assignee():
    
    data = request.get_json()
    assignee_user_id = data.get("assignee_id")
    tickets_id = data.get("ticket_id")
    print(f"assignee_user_id :{assignee_user_id}")
    print(f"tickets_id : {tickets_id}")
    
    if not assignee_user_id:
        return jsonify({"error": "user id needed"}), 400
    if not tickets_id:
        return jsonify({"error": "tickets_id needed"}), 400
    
    connection = connect_to_rds()
    if connection is None:
        print("❌ [DEBUG] Database connection failed")
        return jsonify({"error": "Database connection failed"}), 500
    cursor = connection.cursor()

    cursor.execute(
            "UPDATE tickets SET assignee = %s WHERE tickets_id = %s ",
            (assignee_user_id, tickets_id),
        )
    
    connection.commit()

    return jsonify({"message": "success"}),200



@unified_bp.route('/get_assignee_list', methods=['POST'])
def get_assignee_list():
    result_emails = []
    data = request.get_json()
    user_id = data.get("user_id")
    
    connection = connect_to_rds()
    if connection is None:
        print("❌ [DEBUG] Database connection failed")
        return jsonify({"error": "Database connection failed"}), 500
    
    cursor = connection.cursor()
    
    try:
        # Get user data
        cursor.execute("SELECT user_type, permissions FROM users WHERE user_id = %s", (user_id,))
        user_data = cursor.fetchone()
        
        if not user_data:
            return jsonify({"assignees": []})     
           
        user_type, permissions_json = user_data
        
        try:
            permissions = json.loads(permissions_json)
            shared_items = permissions.get('shared', [])
        except (json.JSONDecodeError, AttributeError):
            return jsonify({"assignees": []}) 
        
        # Helper function to check mailbox permission and extract email
        def has_mailbox_permission(item):
            return (item.get('role', {}).get('permissions', []) and 
                   'mailbox' in item['role']['permissions'])

        def add_admin_details(result_emails, admin_id):
            cursor.execute("SELECT first_name, last_name, user_id FROM users WHERE user_id = %s", (admin_id,))
            names = cursor.fetchone()  
                    
            if names:  
                first_name, last_name, id = names[0], names[1], names[2]
                details = {
                            "name": f"{first_name} {last_name}",  
                            "id": id
                        }
                print(f"admin details : {details}")
                result_emails.append(details)
            return result_emails
                
        if user_type == 'admin':
            # Case 1: Admin user - get emails directly
            print(f"adimn user")
            result_emails = add_admin_details(result_emails, user_id)
            for item in shared_items:
                if has_mailbox_permission(item) and 'email' in item:
                    email = item['email']
                    
                    cursor.execute("SELECT first_name, last_name, user_id FROM users WHERE email = %s", (email,))
                    names = cursor.fetchone()  
                    
                    if names:  
                        first_name, last_name, id = names[0], names[1], names[2]
                        if not first_name or first_name == "None":
                            first_name = email.split('@')[0]
                        if not last_name or last_name == "None":
                            last_name = ""

                        full_name = (first_name + " " + last_name).strip()
                        
                        details = {
                            "name": full_name,  
                            "id": id
                        }
                        result_emails.append(details)
        
        elif user_type == 'user':
            # Case 2: Regular user - get invited_by emails first
            invited_by_emails = []
            for item in shared_items:
                if has_mailbox_permission(item) and 'invited_by' in item:
                    invited_by_emails.append(item['invited_by'])
            
            # Check each invited_by user
            for invited_email in invited_by_emails:
                cursor.execute("SELECT user_type, permissions FROM users WHERE email = %s", (invited_email,))
                invited_data = cursor.fetchone()
                
                if invited_data and invited_data[0] == 'admin':
                    try:
                        result_emails = add_admin_details(result_emails, user_id)

                        invited_permissions = json.loads(invited_data[1])
                        invited_shared = invited_permissions.get('shared', [])
                        
                        for item in invited_shared:
                            if has_mailbox_permission(item) and 'email' in item:
                                email = item['email']
                            
                                cursor.execute("SELECT first_name, last_name, user_id FROM users WHERE email = %s", (email,))
                                names = cursor.fetchone()  
                                
                                if names:  
                                    first_name, last_name, id = names[0], names[1], names[2]
                                    if not first_name or first_name == "None":
                                        first_name = email.split('@')[0]
                                    if not last_name or last_name == "None":
                                        last_name = ""

                                    full_name = (first_name + " " + last_name).strip()
                                    
                                    details = {
                                        "name": full_name,  
                                        "id": id
                                    }
                                    result_emails.append(details)
                    
                    except (json.JSONDecodeError, AttributeError):
                        continue
        
        return jsonify({"assignees": result_emails})
    
    except Exception as e:
        print(f"❌ [ERROR] {str(e)}")
        return jsonify({"error": "Internal server error"}), 500
    
    finally:
        # Always close database connections
        if cursor:
            cursor.close()
        if connection:
            connection.close()