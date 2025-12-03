IDENTITY_MAP = {
    "customer_accounts": {
        "comm_id_fk": "comm"
    },

    "users_account": {
        "deployment_id_fk": "deployments",
        "user_account_membership_id": "memberships"
    },

    "service_requests": {
        "conversation_thread_id_fk": "conversation_thread",
        "comm_id_fk": "comm"
    },

    "msgs": {
        "conversation_thread_id_fk": "conversation_thread",
        "customer_id_fk": "customer_accounts"
    },

    "integrated": {
        "assistant_id_fk": "assistants"
    },

    "reviews": {
        "conversation_thread_id_fk": "conversation_thread"
    },

    "comm": {
        "users_account_id_fk": "users_account",
        "customer_account_id_fk": "customer_accounts"
    },

    "service_request_allocations": {
        "users_account_id_fk": "users_account",
        "customer_account_id_fk": "customer_accounts",
        "request_id_fk": "service_requests"
    },

    "conversation_thread": {
        "external_user_account_id": "users_account",
        "service_request_id_fk": "service_requests",
        "integrated_id_fk": "integrated"
    }
}
