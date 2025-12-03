name_map = {
    # Table name mappings
    "service_request_allocations": "assigned",
    "comm": "communication",
    "linkage": "connect",
    "reviews": "feedback",
    "assistant_guidelines": "instructions",
    "integrated": "integrations",
    "deployments": "launch",
    "msgs": "messages",
    "subscriptions": "plans",
    "workflow": "playbook",
    "assistants": "subagents",
    "memberships": "subscribe",
    "conversation_thread": "threads",
    "service_requests": "tickets",
    "users_account": "users",
    "customer_accounts": "users_clients",
    
    # service_request_allocations -> assigned
    "allocation_id": "assigned_id",
    "users_account_id_fk": "user_id_fk",
    "customer_account_id_fk": "users_clients_id_fk",
    "request_id_fk": "ticket_id_fk",
    
    # comm -> communication
    "comm_id": "communication_id",
    # users_account_id_fk: already mapped above
    # customer_account_id_fk: already mapped above
    
    # linkage -> connect
    "linkage_id": "connect_id",
    "assistant_id_fk": "sub_agent_id_fk",
    "workflow_id_fk": "playbook_id_fk",
    
    # reviews -> feedback
    "review_id": "feedback_id",
    "conversation_thread_id_fk": "conversation_id_fk",
    "ratings_given": "rating",
    "comment_posted": "comments",
    "review_created_at": "created_at",
    
    # assistant_guidelines -> instructions
    "assistant_guideline_id": "instruction_id",
    # assistant_id_fk: already mapped above
    "assistant_guidelines_tag": "tag",
    "assistant_guidelines_transcript": "transcript",
    "assistant_guidelines_created_at": "created_at",
    "assistant_guidelines_updated_at": "updated_at",
    
    # integrated -> integrations
    "integrated_id": "integration_id",
    # assistant_id_fk: already mapped above
    "intergration_platform": "platform",
    "intergration_description": "description",
    "intergration_page_id_or_number": "page_id_or_number",
    "intergration_webhook_url": "webhook_url",
    "intergration_status": "status",
    "intergration_created_at": "created_at",
    
    # deployments -> launch
    "deployment_id": "launch_id",
    # assistant_id_fk: already mapped above
    # users_account_id_fk: already mapped above
    "website_name": "website_name",
    
    # msgs -> messages
    "msg_id": "message_id",
    # conversation_thread_id_fk: already mapped above
    "channel": "sender_type",
    "customer_id_fk": "sender_id",
    "msg_direction": "message_type",
    "msg_summary": "is_summary",
    "msg_created_at": "created_at",
    "msg_update_at": "update_at",
    
    # subscriptions -> plans
    "subscription_id": "plans_id",
    "membership_id": "subscribe_id",
    "subscription_type": "plans",
    "credit_score": "credits",
    "subscriptions_add-on": "add-ons",
    "subscriptions_add_on_measurement": "add_ons_measurement",
    "subscriptions_created_in": "created_in",
    "subscriptions_updated_in": "updated_in",
    "subscriptions_logged_in_at": "logged_in_at",
    "subscriptions_logged_out_at": "logged_out_at",
    
    # workflow -> playbook
    "workflow_id": "playbook_id",
    # assistant_id_fk: already mapped above
    "workflow_created_at": "created_at",
    "workflow_updated_at": "updated_at",
    
    # assistants -> subagents
    "assistant_id": "sub_agent_id",
    "deployment_id_fk": "launch_id_fk",
    "assistant_name": "name",
    "assistant_description": "description",
    "assistant_created_at": "created_at",
    "assistant_updated_at": "updated_at",
    "assistant_voice": "voice_type",
    
    # memberships -> subscribe
    "membership_id": "subscribe_id",
    # users_account_id_fk: already mapped above
    "subscription_id_fk": "plans_id",
    
    # conversation_thread -> threads
    "conversation_thread_id": "conversation_id",
    "integrated_id_fk": "integration_id_fk",
    # users_account_id_fk: already mapped above (maps to external_user_id in threads)
    "external_user_account_id":"external_user_id",
    "conversation_thread_started_at": "started_at",
    "conversation_thread_last_message": "last_message_at",
    "conversation_thread_status": "status",
    "service_request_id_fk": "ticket_id_fk",
    
    # service_requests -> tickets
    "service_request_id": "tickets_id",
    # conversation_thread_id_fk: already mapped above
    "service_requests_priority": "priority",
    "service_requests_status": "status",
    "service_requests_created_in": "created_in",
    "service_requests_updated_in": "updated_in",
    "comm_id_fk": "communication_id_fk",
    "service_request_name": "ticket_name",
    "service_requests_SLA": "SLA",
    "service_requests_assignee_id": "assignee",
    
    # users_account -> users
    "users_account_id": "user_id",
    "user_account_typ": "user_type",
    # deployment_id_fk: already mapped above
    "user_account_first_name": "first_name",
    "user_account_last_name": "last_name",
    "user_account_email": "email",
    "user_account_phone": "phone",
    "user_account_location": "location",
    "user_account_created_in": "created_in",
    "user_account_updated_in": "updated_in",
    "user_account_logged_in_at": "logged_in_at",
    "user_account_logged_out_at": "logged_out_at",
    "user_account_membership_id": "subscribe_id",
    "user_account_roles_created": "roles_creation",
    "user_account_permission": "permissions",
    "user_account_umail_json": "umail_json",
    "user_account_autopilot_mode": "autopilot",
    
    # customer_accounts -> users_clients
    "customer_account_id": "users_clients_id",
    # comm_id_fk: already mapped above
    "customer_first_name": "first_name",
    "customer_last_name": "last_name",
    "customer_phone_number": "phone_number",
    "customer_whatsapp_number": "whatsapp_number",
    "customer_email_id": "email_id",
    "customer_fb_id": "facebook_id",
    "customer_instagram_id": "instagram_id",
    "customer_slack_id": "slack_id",
    "customer_slack_workspace": "slack_workspace",
    "customer_created_in": "created_in",
    "customer_updated_in": "updated_in",
    "customer_type": "type",
    "customer_snooze": "snooze",
}
