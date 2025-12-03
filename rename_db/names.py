{
  "assigned": {
    "columns": [
      {
        "column_name": "assigned_id",
        "data_type": "varchar",
        "is_nullable": "NO",
        "column_key": "PRI"
      },
      {
        "column_name": "user_id_fk",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": "MUL"
      },
      {
        "column_name": "users_clients_id_fk",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": "MUL"
      },
      {
        "column_name": "ticket_id_fk",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": "MUL"
      }
    ],
    "foreign_keys": [
      {
        "column_name": "users_clients_id_fk",
        "referenced_table": "users_clients",
        "referenced_column": "users_clients_id"
      },
      {
        "column_name": "ticket_id_fk",
        "referenced_table": "tickets",
        "referenced_column": "tickets_id"
      },
      {
        "column_name": "user_id_fk",
        "referenced_table": "users",
        "referenced_column": "user_id"
      }
    ],
    "indexes": [
      {
        "index_name": "PRIMARY",
        "column_name": "assigned_id",
        "non_unique": false
      },
      {
        "index_name": "assigned_user_fk",
        "column_name": "user_id_fk",
        "non_unique": true
      },
      {
        "index_name": "assigned_clients_fk",
        "column_name": "users_clients_id_fk",
        "non_unique": true
      },
      {
        "index_name": "assigned_ticket_fk",
        "column_name": "ticket_id_fk",
        "non_unique": true
      }
    ]
  },
  "communication": {
    "columns": [
      {
        "column_name": "communication_id",
        "data_type": "varchar",
        "is_nullable": "NO",
        "column_key": "PRI"
      },
      {
        "column_name": "user_id_fk",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "users_clients_id_fk",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      }
    ],
    "foreign_keys": [
  	{
        "column_name": "user_id_fk",
        "referenced_table": "users",
        "referenced_column": "user_id"
      },
	{
        "column_name": "users_clients_id_fk",
        "referenced_table": "users_clients",
        "referenced_column": "users_client_id"
      }

],
    "indexes": [
      {
        "index_name": "PRIMARY",
        "column_name": "comm_id",
        "non_unique": false
      }
    ]
  },
  "connect": {
    "columns": [
      {
        "column_name": "connect_id",
        "data_type": "varchar",
        "is_nullable": "NO",
        "column_key": "PRI"
      },
      {
        "column_name": "sub_agent_id_fk",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "playbook_id_fk",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      }
    ],
    "foreign_keys": [
	{
        "column_name": "sub_agent_id_fk",
        "referenced_table": "playbook",
        "referenced_column": "sub_agent_id"
      },
	{
        "column_name": "playbook_id_fk",
        "referenced_table": "playbook",
        "referenced_column": "playbook_id"
      }
],
    "indexes": [
      {
        "index_name": "PRIMARY",
        "column_name": "connect_id",
        "non_unique": false
      }
    ]
  },
  "feedback": {
    "columns": [
      {
        "column_name": "feedback_id",
        "data_type": "varchar",
        "is_nullable": "NO",
        "column_key": "PRI"
      },
      {
        "column_name": "conversation_id_fk",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "rating",
        "data_type": "int",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "comments",
        "data_type": "text",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "created_at",
        "data_type": "text",
        "is_nullable": "YES",
        "column_key": ""
      }
    ],
    "foreign_keys": [
        {
        "column_name": "conversation_id_fk",
        "referenced_table": "threads",
        "referenced_column": "conversation_id"
      }
    ],
    "indexes": [
      {
        "index_name": "PRIMARY",
        "column_name": "feedback_id",
        "non_unique": false
      }
    ]
  },
  "instructions": {
    "columns": [
      {
        "column_name": "instruction_id",
        "data_type": "varchar",
        "is_nullable": "NO",
        "column_key": "PRI"
      },
      {
        "column_name": "sub_agent_id_fk",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "tag",
        "data_type": "text",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "transcript",
        "data_type": "text",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "created_at",
        "data_type": "datetime",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "updated_at",
        "data_type": "datetime",
        "is_nullable": "YES",
        "column_key": ""
      }
    ],
    "foreign_keys": [
        {
        "column_name": "sub_agent_id_fk",
        "referenced_table": "playbook",
        "referenced_column": "sub_agent_id"
      }
    ],
    "indexes": [
      {
        "index_name": "PRIMARY",
        "column_name": "instruction_id",
        "non_unique": false
      }
    ]
  },
  "integrations": {
    "columns": [
      {
        "column_name": "integration_id",
        "data_type": "varchar",
        "is_nullable": "NO",
        "column_key": "PRI"
      },
      {
        "column_name": "sub_agent_id_fk",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "platform",
        "data_type": "enum",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "description",
        "data_type": "text",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "page_id_or_number",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "webhook_url",
        "data_type": "text",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "status",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "created_at",
        "data_type": "datetime",
        "is_nullable": "YES",
        "column_key": ""
      }
    ],
    "foreign_keys": [
        {
        "column_name": "sub_agent_id_fk",
        "referenced_table": "playbook",
        "referenced_column": "sub_agent_id"
      }
    ],
    
    "indexes": [
      {
        "index_name": "PRIMARY",
        "column_name": "integration_id",
        "non_unique": false
      }
    ]
  },
  "launch": {
    "columns": [
      {
        "column_name": "launch_id",
        "data_type": "varchar",
        "is_nullable": "NO",
        "column_key": "PRI"
      },
      {
        "column_name": "sub_agent_id_fk",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "user_id_fk",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "website_name",
        "data_type": "text",
        "is_nullable": "YES",
        "column_key": ""
      }
    ],
    "foreign_keys": [
         {
        "column_name": "sub_agent_id_fk",
        "referenced_table": "playbook",
        "referenced_column": "sub_agent_id"
      },
      {
        "column_name": "user_id_fk",
        "referenced_table": "users",
        "referenced_column": "user_id"
      }
    ],
    
    "indexes": [
      {
        "index_name": "PRIMARY",
        "column_name": "launch_id",
        "non_unique": false
      }
    ]
  },
  "messages": {
    "columns": [
      {
        "column_name": "message_id",
        "data_type": "varchar",
        "is_nullable": "NO",
        "column_key": "PRI"
      },
      {
        "column_name": "conversation_id_fk",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": "MUL"
      },
      {
        "column_name": "sender_type",
        "data_type": "enum",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "sender_id",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "content_ref",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "message_type",
        "data_type": "enum",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "is_summary",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "created_at",
        "data_type": "datetime",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "update_at",
        "data_type": "datetime",
        "is_nullable": "YES",
        "column_key": ""
      }
    ],
    "foreign_keys": [
      {
        "column_name": "conversation_id_fk",
        "referenced_table": "threads",
        "referenced_column": "conversation_id"
      }
    ],
    "indexes": [
      {
        "index_name": "PRIMARY",
        "column_name": "message_id",
        "non_unique": false
      },
      {
        "index_name": "fk_messages_conversation",
        "column_name": "conversation_id_fk",
        "non_unique": true
      }
    ]
  },
  "plans": {
    "columns": [
      {
        "column_name": "plans_id",
        "data_type": "varchar",
        "is_nullable": "NO",
        "column_key": "PRI"
      },
      {
        "column_name": "subscribe_id",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "plans",
        "data_type": "enum",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "credits",
        "data_type": "enum",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "add-ons",
        "data_type": "enum",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "add_ons_measurement",
        "data_type": "json",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "created_in",
        "data_type": "datetime",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "updated_in",
        "data_type": "datetime",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "logged_in_at",
        "data_type": "datetime",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "logged_out_at",
        "data_type": "datetime",
        "is_nullable": "YES",
        "column_key": ""
      }
    ],
    "foreign_keys": [],
    "indexes": [
      {
        "index_name": "PRIMARY",
        "column_name": "plans_id",
        "non_unique": false
      }
    ]
  },
  "playbook": {
    "columns": [
      {
        "column_name": "playbook_id",
        "data_type": "varchar",
        "is_nullable": "NO",
        "column_key": "PRI"
      },
      {
        "column_name": "sub_agent_id_fk",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "created_at",
        "data_type": "datetime",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "updated_at",
        "data_type": "datetime",
        "is_nullable": "YES",
        "column_key": ""
      }
    ],
    "foreign_keys": [
         {
        "column_name": "sub_agent_id_fk",
        "referenced_table": "subagents",
        "referenced_column": "sub_agent_id"
      }
    ],
    "indexes": [
      {
        "index_name": "PRIMARY",
        "column_name": "playbook_id",
        "non_unique": false
      }
    ]
  },
  "subagents": {
    "columns": [
      {
        "column_name": "sub_agent_id",
        "data_type": "varchar",
        "is_nullable": "NO",
        "column_key": "PRI"
      },
      {
        "column_name": "launch_id_fk",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "name",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "description",
        "data_type": "enum",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "created_at",
        "data_type": "datetime",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "updated_at",
        "data_type": "datetime",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "voice_type",
        "data_type": "enum",
        "is_nullable": "YES",
        "column_key": ""
      }
    ],
    "foreign_keys": [
        {
        "column_name": "launch_id_fk",
        "referenced_table": "launch",
        "referenced_column": "launch_id"
      }
    ],
    "indexes": [
      {
        "index_name": "PRIMARY",
        "column_name": "sub_agent_id",
        "non_unique": false
      }
    ]
  },
  "subscribe": {
    "columns": [
      {
        "column_name": "subscribe_id",
        "data_type": "varchar",
        "is_nullable": "NO",
        "column_key": "PRI"
      },
      {
        "column_name": "user_id",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": "MUL"
      },
      {
        "column_name": "plans_id",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": "MUL"
      }
    ],
    "foreign_keys": [
      {
        "column_name": "user_id",
        "referenced_table": "users",
        "referenced_column": "user_id"
      },
      {
        "column_name": "plans_id",
        "referenced_table": "plans",
        "referenced_column": "plans_id"
      },
    ],
    "indexes": [
      {
        "index_name": "PRIMARY",
        "column_name": "subscribe_id",
        "non_unique": false
      },
      {
        "index_name": "plans_id",
        "column_name": "plans_id",
        "non_unique": true
      },
      {
        "index_name": "subscribe_ibfk_1",
        "column_name": "user_id",
        "non_unique": true
      }
    ]
  },
  "threads": {
    "columns": [
      {
        "column_name": "conversation_id",
        "data_type": "varchar",
        "is_nullable": "NO",
        "column_key": "PRI"
      },
      {
        "column_name": "integration_id_fk",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": "MUL"
      },
      {
        "column_name": "external_user_id",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "started_at",
        "data_type": "datetime",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "last_message_at",
        "data_type": "datetime",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "status",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "ticket_id_fk",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": "MUL"
      }
    ],
    "foreign_keys": [
      {
        "column_name": "integration_id_fk",
        "referenced_table": "integrations",
        "referenced_column": "integration_id"
      },
      {
        "column_name": "ticket_id_fk",
        "referenced_table": "tickets",
        "referenced_column": "tickets_id"
      },
       {
        "column_name": "external_user_id",
        "referenced_table": "users",
        "referenced_column": "user_id"
      }
    ],
    "indexes": [
      {
        "index_name": "PRIMARY",
        "column_name": "conversation_id",
        "non_unique": false
      },
      {
        "index_name": "fk_integration",
        "column_name": "integration_id_fk",
        "non_unique": true
      },
      {
        "index_name": "fk_ticket",
        "column_name": "ticket_id_fk",
        "non_unique": true
      }
    ]
  },
  "tickets": {
    "columns": [
      {
        "column_name": "tickets_id",
        "data_type": "varchar",
        "is_nullable": "NO",
        "column_key": "PRI"
      },
      {
        "column_name": "conversation_id_fk",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": "MUL"
      },
      {
        "column_name": "priority",
        "data_type": "enum",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "status",
        "data_type": "enum",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "created_in",
        "data_type": "datetime",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "updated_in",
        "data_type": "datetime",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "communication_id_fk",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "ticket_name",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "SLA",
        "data_type": "int",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "assignee",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      }
    ],
    "foreign_keys": [
      {
        "column_name": "conversation_id_fk",
        "referenced_table": "threads",
        "referenced_column": "conversation_id"
      },
      {
        "column_name": "communication_id_fk",
        "referenced_table": "communication",
        "referenced_column": "communication_id"
      },
       {
        "column_name": "assignee",
        "referenced_table": "users",
        "referenced_column": "user_id"
      }
    ],
    "indexes": [
      {
        "index_name": "PRIMARY",
        "column_name": "tickets_id",
        "non_unique": false
      },
      {
        "index_name": "tickets_ibfk_1",
        "column_name": "conversation_id_fk",
        "non_unique": true
      }
    ]
  },
  "users": {
    "columns": [
      {
        "column_name": "user_id",
        "data_type": "varchar",
        "is_nullable": "NO",
        "column_key": "PRI"
      },
      {
        "column_name": "user_type",
        "data_type": "enum",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "launch_id_fk",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "first_name",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "last_name",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "email",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "phone",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "client_id",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "location",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "created_in",
        "data_type": "datetime",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "updated_in",
        "data_type": "datetime",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "logged_in_at",
        "data_type": "datetime",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "logged_out_at",
        "data_type": "datetime",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "subscribe_id",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": "MUL"
      },
      {
        "column_name": "roles_creation",
        "data_type": "json",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "permissions",
        "data_type": "json",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "umail_json",
        "data_type": "json",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "autopilot",
        "data_type": "json",
        "is_nullable": "YES",
        "column_key": ""
      }
    ],
    "foreign_keys": [
      {
        "column_name": "subscribe_id",
        "referenced_table": "subscribe",
        "referenced_column": "subscribe_id"
      },
      {
        "column_name": "launch_id_fk",
        "referenced_table": "launch",
        "referenced_column": "launch_id"
      }
    ],
    "indexes": [
      {
        "index_name": "PRIMARY",
        "column_name": "user_id",
        "non_unique": false
      },
      {
        "index_name": "fk_users_subscribe",
        "column_name": "subscribe_id",
        "non_unique": true
      }
    ]
  },
  "users_clients": {
    "columns": [
      {
        "column_name": "users_clients_id",
        "data_type": "varchar",
        "is_nullable": "NO",
        "column_key": "PRI"
      },
      {
        "column_name": "communication_id_fk",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": "MUL"
      },
      {
        "column_name": "first_name",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "last_name",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "phone_number",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "whatsapp_number",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "email_id",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "facebook_id",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "instagram_id",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "slack_id",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "slack_workspace",
        "data_type": "varchar",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "created_in",
        "data_type": "datetime",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "updated_in",
        "data_type": "datetime",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "type",
        "data_type": "enum",
        "is_nullable": "YES",
        "column_key": ""
      },
      {
        "column_name": "snooze",
        "data_type": "tinyint",
        "is_nullable": "YES",
        "column_key": ""
      }
    ],
    "foreign_keys": [
      {
        "column_name": "communication_id_fk",
        "referenced_table": "communication",
        "referenced_column": "communication_id"
      }
    ],
    "indexes": [
      {
        "index_name": "PRIMARY",
        "column_name": "users_clients_id",
        "non_unique": false
      },
      {
        "index_name": "fk_communication",
        "column_name": "communication_id_fk",
        "non_unique": true
      }
    ]
  }
}