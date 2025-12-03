class Validator:
    def __init__(self):
        pass

    def validate_filters(self, filter_list):
        """
        Validates a list of filters against a reference map where each key has a list of valid values.

        Args:
            filter_list (list of dict): Example: [{'status': 'open'}, {'priority': 'medium'}]
            reference_map (dict of list): 
                Example: {
                    "status": ["open", "pending", "solved"],
                    "priority": ["low", "medium", "high"]
                }

        Returns:
            list: The original filter_list if all key-value pairs match reference_map, otherwise []
        """
        reference_map = {
            "status": ["open", "pending", "solved"],
            "priority": ["low", "medium", "high"],
            "channel": ["gmail","website","zoho","outlook","whatsapp","sms","phone","instagram_dm","facebook_messenger"],
            "intergration_platform": ["facebook_messenger","instagram_dm","whatsapp","twilio_sms"]
        }
        
        for filter_dict in filter_list:
            for key, value in filter_dict.items():
                # Check if key exists and value is in the list of allowed values
                if key not in reference_map or value not in reference_map[key]:
                    print(f"key/value not in possible filters: {filter_list}")
                    return []
        return filter_list

    def normalize_metric(self, metric: str) -> str:
        """
        Normalizes a metric name to table.column format if possible.

        Args:
            metric (str): Metric name to normalize.

        Returns:
            str: Normalized metric in table.column form, or original metric if no mapping found.
        """
        COLUMN_TO_TABLE_MAP = {
            "customer_account_id": "customer_accounts",
            "users_account_id": "users_account",
            "service_request_id": "service_requests",
            "msg_id": "msgs",
            "integrated_id": "integrated",
            "review_id": "reviews"
        }
   
        # 1. Extract column part
        if "." in metric:
            _, col = metric.split(".", 1)
        else:
            col = metric

        # 2. Validate column exists in expected_table schema

        if col in ["customer_account_id","comm_id_fk","customer_first_name","customer_last_name","customer_email_id","customer_created_in","customer_updated_in","customer_type","customer_snooze"]:
            table = "customer_accounts"

        elif col in ["users_account_id","user_account_type","deployment_id_fk","user_account_first_name","user_account_last_name","user_account_email","user_account_location","user_account_created_in","user_account_updated_in","user_account_logged_in_at","user_account_logged_out_at","user_account_membership_id","user_account_roles_created","user_account_permission","user_account_umail_json","user_account_autopilot_mode","special_access"]:
            table = "users_account"

        elif col in ["service_request_id","conversation_thread_id_fk","service_requests_priority","service_requests_status","service_requests_created_in","service_requests_updated_in","comm_id_fk","service_request_name","service_requests_SLA","service_requests_assignee_id"]:
            table = "service_requests"

        elif col in ["msg_id","conversation_thread_id_fk","channel","customer_id_fk","msg_direction","msg_summary","msg_created_at","msg_update_at"]:
            table = "msgs"

        elif col in ["integrated_id","assistant_id_fk","intergration_platform","intergration_description","intergration_page_id_or_number","intergration_webhook_url","intergration_status","intergration_created_at"]:
            table = "integrated"

        elif col in ["review_id","conversation_thread_id_fk","ratings_given","comment_posted","review_created_at"]:
            table = "reviews"

        else:
            raise ValueError(f"Unknown column: {col}")

        # 3. Normalize metric into "table.column" format
        normalized = f"{table}.{col}"

        return normalized