import pymysql
import boto3
import json
from db.rds_db import connect_to_rds


def create_tables():
    connection = connect_to_rds()
    if connection is None:
        return

    cursor = connection.cursor()

    try:
        table_queries = [
            # users table
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id VARCHAR(36) PRIMARY KEY,
                user_type ENUM('superadmin', 'admin', 'user'),
                launch_id_fk VARCHAR(36),
                first_name VARCHAR(20),
                last_name VARCHAR(20),
                email VARCHAR(60),
                phone VARCHAR(20),
                client_id VARCHAR(255),
                client_secret VARCHAR(255),
                token VARCHAR(255),
                refresh_token VARCHAR(255),
                expiry DATETIME,
                password_hash VARCHAR(255),
                profile_pic VARCHAR(100),
                location VARCHAR(130),
                social VARCHAR(330),
                created_in DATETIME,
                updated_in DATETIME,
                logged_in_at DATETIME,
                logged_out_at DATETIME,
                sociallinks JSON
            )
            """,
            # launch table
            """
            CREATE TABLE IF NOT EXISTS launch (
                launch_id VARCHAR(36) PRIMARY KEY,
                sub_agent_id_fk VARCHAR(36),
                user_id_fk VARCHAR(36),
                api_id VARCHAR(36),
                website_name TEXT
            )
            """,
            # subagents table
            """
            CREATE TABLE IF NOT EXISTS subagents (
                sub_agent_id VARCHAR(36) PRIMARY KEY,
                launch_id_fk VARCHAR(36),
                name ENUM('Registered', 'Suspended', 'Deleted'),
                description TEXT,
                documentation_link TEXT,
                model_version VARCHAR(36),
                created_at DATETIME,
                updated_at DATETIME
            )
            """,
            # connect table
            """
            CREATE TABLE IF NOT EXISTS connect (
                connect_id VARCHAR(36) PRIMARY KEY,
                sub_agent_id_fk VARCHAR(36),
                instruction_id_fk VARCHAR(36)
            )
            """,
            # # instructions table
            # """
            # CREATE TABLE IF NOT EXISTS instructions (
            #     instruction_id VARCHAR(36) PRIMARY KEY,
            #     sub_agent_id_fk VARCHAR(36),
            #     drive_path TEXT,
            #     file_path TEXT,
            #     tag TEXT,
            #     transcript TEXT,
            #     created_at DATETIME,
            #     updated_at DATETIME
            # )
            # """,
            # integrations table
            """
            CREATE TABLE IF NOT EXISTS integrations (
                integration_id VARCHAR(36) PRIMARY KEY,
                sub_agent_id_fk VARCHAR(36),
                platform ENUM('facebook_messenger', 'instagram_dm', 'whatsapp', 'twilio_sms'),
                description TEXT,
                access_token VARCHAR(128),
                page_id_or_number VARCHAR(128),
                webhook_url TEXT,
                status VARCHAR(36),
                created_at DATETIME
            )
            """,
            # threads table
            """
            CREATE TABLE IF NOT EXISTS threads (
                conversation_id VARCHAR(36) PRIMARY KEY,
                integration_id_fk VARCHAR(36),
                external_user_id VARCHAR(36),
                started_at DATETIME,
                last_message_at DATETIME,
                status VARCHAR(36)
            )
            """,
            # messages table
            """
            CREATE TABLE IF NOT EXISTS messages (
                message_id VARCHAR(36) PRIMARY KEY,
                conversation_id_fk VARCHAR(36),
                sender_type ENUM('facebook_messenger', 'instagram_dm', 'whatsapp', 'waldo_sms'),
                sender_id VARCHAR(36),
                content TEXT,
                message_type VARCHAR(36),
                is_summary VARCHAR(36),
                created_at DATETIME
            )
            """,
            # feedback table
            """
            CREATE TABLE IF NOT EXISTS feedback (
                feedback_id VARCHAR(36) PRIMARY KEY,
                conversation_id_fk VARCHAR(36),
                rating INT,
                comments TEXT,
                created_at TEXT
            )
            """,
            # business_info table
            """
            CREATE TABLE IF NOT EXISTS business_info (
                business_info_id VARCHAR(36) PRIMARY KEY,
                user_id_fk VARCHAR(36),
                BusinessID TEXT,
                BusinessName TEXT,
                Age TEXT,
                Sex ENUM('Male','Female'),  
                LineOfBusiness TEXT,
                YearsInBusiness TEXT,
                HasLicense BOOLEAN,sele
                RegistrationStatus ENUM('Registered','Non-Registered'),
                ProofOfBusinessFile TEXT,
                RegistrationNumber TEXT,
                GSTNumber TEXT,
                BusinessNameOnCertificate TEXT,
                Country TEXT,
                ProvinceOrState TEXT,
                City TEXT,
                BillingAddress TEXT,
                ShippingAddress TEXT,
                BusinessImage TEXT,
                BusinessImageFile VARCHAR(128)
                # -- 🔽 Newly added columns
                BusinessEmail VARCHAR(60),
                PaymentMethods TEXT,           -- Store as JSON string: ["cash", "upi"]
                PaymentDetails TEXT,           -- Optional extra payment data
                OwnershipType VARCHAR(100),    -- e.g., "sole-proprietorship"
                BusinessTimings VARCHAR(50),   -- e.g., "9-5"
                WebsiteUrl VARCHAR(100),       -- Business website
                SecondaryPhone VARCHAR(20),    -- Alternate contact number
                GSTNotAvailable BOOLEAN,       -- True if exempted
                SameAsBilling BOOLEAN,         -- Whether shipping = billing
                businessLocation VARCHAR(130)

            )
            """,
            """
            CREATE TABLE IF NOT EXISTS trust_centers (
                id VARCHAR(36) PRIMARY KEY,
                owner_user_id VARCHAR(36) NOT NULL,
                whitepaper_s3_key VARCHAR(512),
                nda_content TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS trust_center_documents (
                id VARCHAR(36) PRIMARY KEY,
                trust_center_id VARCHAR(36) NOT NULL,
                label VARCHAR(255) NOT NULL,
                s3_key VARCHAR(512) NOT NULL,
                file_type VARCHAR(20) NOT NULL,
                uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS trust_center_access (
                id VARCHAR(36) PRIMARY KEY,
                trust_center_id VARCHAR(36) NOT NULL,
                granted_to_email VARCHAR(255) NOT NULL,
                nda_accepted TINYINT(1) DEFAULT 0,
                nda_accepted_at DATETIME,
                granted_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """,
            # ── Strategy governance: Objective → Program → Project ──
            # Parents are declared before children. We follow the trust_center
            # convention (plain indexed columns, no hard FK constraints) so the
            # tables are order- and engine-independent and fully idempotent.
            """
            CREATE TABLE IF NOT EXISTS strategic_objectives (
                id VARCHAR(36) PRIMARY KEY,
                owner_user_id VARCHAR(36) NOT NULL,
                org_id VARCHAR(36),
                created_by VARCHAR(36),
                title VARCHAR(255) NOT NULL,
                description TEXT,
                status VARCHAR(50) DEFAULT 'draft',
                start_date DATE,
                target_date DATE,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                KEY idx_objective_owner (owner_user_id),
                KEY idx_objective_org (org_id, status)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS programs (
                id VARCHAR(36) PRIMARY KEY,
                objective_id VARCHAR(36) NOT NULL,
                owner_user_id VARCHAR(36) NOT NULL,
                org_id VARCHAR(36),
                created_by VARCHAR(36),
                name VARCHAR(255) NOT NULL,
                description TEXT,
                status VARCHAR(50) DEFAULT 'draft',
                start_date DATE,
                target_date DATE,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                KEY idx_program_objective (objective_id),
                KEY idx_program_owner (owner_user_id),
                KEY idx_program_org (org_id, status)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS projects (
                id VARCHAR(36) PRIMARY KEY,
                objective_id VARCHAR(36) NOT NULL,
                program_id VARCHAR(36),
                owner_user_id VARCHAR(36) NOT NULL,
                org_id VARCHAR(36),
                created_by VARCHAR(36),
                name VARCHAR(255) NOT NULL,
                description TEXT,
                status VARCHAR(50) DEFAULT 'draft',
                start_date DATE,
                target_date DATE,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                KEY idx_project_objective (objective_id),
                KEY idx_project_program (program_id),
                KEY idx_project_owner (owner_user_id),
                KEY idx_project_org (org_id, status)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS project_doc_links (
                id VARCHAR(36) PRIMARY KEY,
                project_id VARCHAR(36) NOT NULL,
                policy_id VARCHAR(64) NOT NULL,
                doc_type ENUM('policy', 'procedure', 'standard') NOT NULL DEFAULT 'policy',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uq_project_doc (project_id, policy_id),
                KEY idx_doc_project (project_id),
                KEY idx_doc_policy (policy_id)
            )
            """,
            # Programs link to policies & standards; projects link to procedures.
            """
            CREATE TABLE IF NOT EXISTS program_doc_links (
                id VARCHAR(36) PRIMARY KEY,
                program_id VARCHAR(36) NOT NULL,
                policy_id VARCHAR(64) NOT NULL,
                doc_type ENUM('policy', 'standard') NOT NULL DEFAULT 'policy',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uq_program_doc (program_id, policy_id),
                KEY idx_pdoc_program (program_id),
                KEY idx_pdoc_policy (policy_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS project_tracker_links (
                id VARCHAR(36) PRIMARY KEY,
                project_id VARCHAR(36) NOT NULL,
                tracker_id VARCHAR(64) NOT NULL,
                pinned TINYINT(1) DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uq_project_tracker (project_id, tracker_id),
                KEY idx_tracker_project (project_id),
                KEY idx_tracker_tracker (tracker_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS strategy_milestones (
                id VARCHAR(36) PRIMARY KEY,
                parent_type ENUM('objective', 'program', 'project') NOT NULL,
                parent_id VARCHAR(36) NOT NULL,
                title VARCHAR(255) NOT NULL,
                due_date DATE,
                status VARCHAR(50) DEFAULT 'planned',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                KEY idx_milestone_parent (parent_type, parent_id)
            )
            """,
        ]

        for query in table_queries:
            cursor.execute(query)

        connection.commit()
    # print("\u2705 All tables created successfully!")

    except pymysql.MySQLError as e:
        print("\u274c Error while creating tables:", e)
    finally:
        cursor.close()
        connection.close()


def create_external_app_user_auth_table():
    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed")
        return

    cursor = connection.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS external_app_user_auth (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                app_id BIGINT NOT NULL,
                user_id VARCHAR(64) NOT NULL,
                auth_type ENUM('bearer', 'api_key', 'basic', 'oauth2', 'none') NOT NULL DEFAULT 'none',
                auth_config JSON NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,

                UNIQUE KEY uq_external_app_user_auth (app_id, user_id),
                INDEX idx_external_app_user_auth_user (user_id),

                CONSTRAINT fk_external_app_user_auth_app
                    FOREIGN KEY (app_id)
                    REFERENCES external_apps(id)
                    ON DELETE CASCADE
            );
            """)

        connection.commit()
        print("✅ external_app_user_auth table created")

    except Exception as e:
        connection.rollback()
        print("❌ Failed to create external_app_user_auth table:", str(e))
        raise

    finally:
        cursor.close()
        connection.close()


def alter_tokens():
    connection = connect_to_rds()
    if connection is None:
        return

    cursor = connection.cursor()
    try:
        alter_query = """
                ALTER TABLE users
                MODIFY COLUMN token TEXT,
                MODIFY COLUMN refresh_token TEXT;
                """
        cursor.execute(alter_query)
        connection.commit()
    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def alter_subagents():
    # print("Altering subagents table...")
    connection = connect_to_rds()
    if connection is None:
        return

    cursor = connection.cursor()
    try:
        alter_query = """
               ALTER TABLE subagents
                MODIFY COLUMN name VARCHAR(32),
                MODIFY COLUMN description ENUM('Registered', 'Suspended', 'Deleted'),
                ADD COLUMN voice_type ENUM('Man', 'Woman');
                """
        cursor.execute(alter_query)
        connection.commit()
    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def communication():

    connection = connect_to_rds()
    if connection is None:
        return

    cursor = connection.cursor()
    try:
        create_table_query = """
    CREATE TABLE IF NOT EXISTS communication (
    communication_id VARCHAR(36) PRIMARY KEY,
    user_id VARCHAR(36),
    users_clients_id VARCHAR(36)
);
    """
        cursor.execute(create_table_query)
        connection.commit()
    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def create_clients():
    connection = connect_to_rds()
    if connection is None:
        return

    cursor = connection.cursor()
    try:
        create_table_query = """
    CREATE TABLE IF NOT EXISTS users_clients (
        users_clients_id VARCHAR(36) PRIMARY KEY,
        communication_id VARCHAR(36),
        first_name VARCHAR(36),
        last_name VARCHAR(36),
        phone_number VARCHAR(36),
        whatsapp_number VARCHAR(36),
        email_id VARCHAR(36),
        facebook_id VARCHAR(36),
        instagram_id VARCHAR(130),
        slack_id VARCHAR(130),
        slack_workspace VARCHAR(36),
        created_in DATE,
        updated_in DATE,
        CONSTRAINT fk_communication
            FOREIGN KEY (communication_id)
            REFERENCES communication(communication_id)
    );
    """
        cursor.execute(create_table_query)
        connection.commit()
    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def rename_columns_in_communication():
    connection = connect_to_rds()
    if connection is None:
        # print("DB connection failed.")
        return

    cursor = connection.cursor()
    try:
        # Rename user_id → user_id_fk
        alter_user_id = """
            ALTER TABLE communication
            CHANGE user_id user_id_fk VARCHAR(36);
        """
        cursor.execute(alter_user_id)

        # Rename users_clients_id → users_clients_id_fk
        alter_users_clients_id = """
            ALTER TABLE communication
            CHANGE users_clients_id users_clients_id_fk VARCHAR(36);
        """
        cursor.execute(alter_users_clients_id)

        connection.commit()
    # print("✅ Columns renamed successfully!")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def rename_columns_in_users_clients():
    connection = connect_to_rds()
    if connection is None:
        # print("DB connection failed.")
        return

    cursor = connection.cursor()
    try:
        # Rename user_id → user_id_fk
        alter_user_id = """
            ALTER TABLE users_clients
            CHANGE communication_id communication_id_fk VARCHAR(36);
        """
        cursor.execute(alter_user_id)

        connection.commit()
    # print("✅ Columns renamed successfully!")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def renameConnectandcreatePlaybook():
    connection = connect_to_rds()
    if connection is None:
        # print("DB connection failed.")
        return

    cursor = connection.cursor()
    try:
        alter_user_id = """
            ALTER TABLE connect
            CHANGE instruction_id_fk playbook_id_fk VARCHAR(36);
        """
        cursor.execute(alter_user_id)
        newtable = """
            CREATE TABLE IF NOT EXISTS playbook (
                playbook_id VARCHAR(36) PRIMARY KEY,
                sub_agent_id VARCHAR(36),
                file_path TEXT,
                created_at DATETIME,
                updated_at DATETIME
            )
        """
        cursor.execute(newtable)

        connection.commit()
    # print("✅ Columns renamed successfully!")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def updatethreadstoticket():
    connection = connect_to_rds()
    if connection is None:
        # print("DB connection failed.")
        return

    cursor = connection.cursor()
    try:
        alter_theads = """
            ALTER TABLE threads
            ADD COLUMN ticket_id_fk VARCHAR(36);
        """
        cursor.execute(alter_theads)

        connection.commit()
    # print("✅ Columns added successfully!")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def createticketstable():
    connection = connect_to_rds()
    if connection is None:
        # print("DB connection failed.")
        return

    cursor = connection.cursor()
    try:
        create_table_sql = """
            CREATE TABLE IF NOT EXISTS tickets (
                tickets_id VARCHAR(36) PRIMARY KEY,
                conversation_id_fk VARCHAR(36),
                priority ENUM('High','Medium','Low'),
                status ENUM('Open','Pending','Solved'),
                created_in DATETIME,
                updated_in DATETIME,
                FOREIGN KEY (conversation_id_fk) REFERENCES threads(conversation_id)
            );
        """
        cursor.execute(create_table_sql)
        connection.commit()
    # print("✅ Created 'tickets' table successfully!")

    except pymysql.MySQLError as e:
        print(f"MySQL Error (tickets): {e}")

    finally:
        cursor.close()
        connection.close()


def createTableAssigned():
    connection = connect_to_rds()
    if connection is None:
        # print("DB connection failed.")
        return

    cursor = connection.cursor()
    try:
        create_table_sql = """
            CREATE TABLE IF NOT EXISTS assigned (
                assigned_id VARCHAR(36) PRIMARY KEY,
                user_id_fk VARCHAR(36),
                users_clients_id_fk VARCHAR(36),
                ticket_id_fk VARCHAR(36),
                FOREIGN KEY (user_id_fk) REFERENCES users(user_id),
                FOREIGN KEY (ticket_id_fk) REFERENCES tickets(tickets_id),
                FOREIGN KEY (users_clients_id_fk) REFERENCES users_clients(users_clients_id)
            );
        """
        cursor.execute(create_table_sql)
        connection.commit()
    # print("✅ Created 'assigned' table successfully!")

    except pymysql.MySQLError as e:
        print(f"MySQL Error (assigned): {e}")

    finally:
        cursor.close()
        connection.close()


def rename_columns_in_tickets():
    connection = connect_to_rds()
    if connection is None:
        # print("DB connection failed.")
        return

    cursor = connection.cursor()
    try:
        # Rename user_id → user_id_fk
        alter_user_id = """
            ALTER TABLE tickets
            ADD COLUMN communication_id_fk VARCHAR(36);
        """
        cursor.execute(alter_user_id)

        connection.commit()
    # print("✅ Columns added successfully!")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def updateticket():
    connection = connect_to_rds()
    if connection is None:
        # print("DB connection failed.")
        return

    cursor = connection.cursor()
    try:
        alter_theads = """
            ALTER TABLE tickets
            ADD COLUMN ticket_name VARCHAR(36);
        """
        cursor.execute(alter_theads)

        connection.commit()
    # print("✅ Columns added successfully!")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def create_new_threads():
    connection = connect_to_rds()
    if connection is None:
        return

    cursor = connection.cursor()
    try:
        create_table_query = """
        CREATE TABLE IF NOT EXISTS threads (
            conversation_id VARCHAR(36) PRIMARY KEY,
            integration_id_fk VARCHAR(36),
            tickets_id_fk VARCHAR(36),
            external_user_id VARCHAR(36),
            started_at DATETIME,
            last_message_at DATETIME,
            status VARCHAR(36),
            CONSTRAINT fk_integration
                FOREIGN KEY (integration_id_fk)
                REFERENCES integrations(integration_id),
            CONSTRAINT fk_tickets
                FOREIGN KEY (tickets_id_fk)
                REFERENCES tickets(tickets_id)
        );
        """
        cursor.execute(create_table_query)
        connection.commit()
    # print("✅ Created 'threads' table successfully!")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")
    finally:
        cursor.close()
        connection.close()


def create_new_messages():
    connection = connect_to_rds()
    if connection is None:
        return

    cursor = connection.cursor()
    try:
        create_table_query = """
        CREATE TABLE IF NOT EXISTS messages (
                message_id VARCHAR(255) PRIMARY KEY,
                conversation_id_fk VARCHAR(36),
                sender_type ENUM('facebook_messenger', 'instagram_dm', 'whatsapp', 'waldo_sms'),
                sender_id VARCHAR(36),
                content_ref VARCHAR(128),
                message_type ENUM('inbound','outbound'),
                is_summary VARCHAR(36),
                created_at DATETIME,
                update_at DATETIME
        );
        """
        cursor.execute(create_table_query)
        connection.commit()
    # print("✅ Created 'messages' table successfully!")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")
    finally:
        cursor.close()
        connection.close()


def create_plans():
    connection = connect_to_rds()
    if connection is None:
        return

    cursor = connection.cursor()
    try:
        create_table_query = """
        CREATE TABLE plans (
            plans_id VARCHAR(36) PRIMARY KEY,
            subscribe_id VARCHAR(36),
            plans ENUM(
                'Bytoid:tm: Support for Consultants',
                'Bytoid:tm: Support - Part-time AI Worker',
                'Bytoid:tm: Support - Full time AI Worker',
                'Bytoid:tm: Support - 24/7 AI Worker'
            ),
            credits ENUM('250', '500', '1000', '1500', '2500', '5000', '7500', '15000'),
            `add-ons` ENUM(
                'Bytoid:tm: LiveTalk',
                'Bytoid:tm: LiveResolve',
                'Bytoid:tm: Reporting Agent'
            ),
            add_ons_measurement JSON,
            created_in DATETIME,
            updated_in DATETIME,
            logged_in_at DATETIME,
            logged_out_at DATETIMEticket
        );
        """
        cursor.execute(create_table_query)
        connection.commit()
    # print("✅ Created 'Plans' table successfully!")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")
    finally:
        cursor.close()
        connection.close()


def create_subscribe():
    connection = connect_to_rds()
    if connection is None:
        return

    cursor = connection.cursor()
    try:
        create_table_query = """
        CREATE TABLE subscribe (
            subscribe_id VARCHAR(36) PRIMARY KEY,
            user_id VARCHAR(36),
            plans_id VARCHAR(36),
            FOREIGN KEY (user_id) REFERENCES users(user_id),
            FOREIGN KEY (plans_id) REFERENCES plans(plans_id)
        );
        """
        cursor.execute(create_table_query)
        connection.commit()
    # print("✅ Created 'Subscibe' table successfully! and updated users table")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")
    finally:
        cursor.close()
        connection.close()


def alter_tables_users_subscribe():
    connection = connect_to_rds()
    if connection is None:
        return

    cursor = connection.cursor()
    try:

        alter_table_add_column = """
            ALTER TABLE users ADD COLUMN subscribe_id VARCHAR(36);
        """
        cursor.execute(alter_table_add_column)

        alter_table_add_fk = """
            ALTER TABLE users
            ADD CONSTRAINT fk_users_subscribe
            FOREIGN KEY (subscribe_id) REFERENCES subscribe(subscribe_id);
        """
        cursor.execute(alter_table_add_fk)
    # print("✅ updated 'users-Subscibe' table successfully!")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")
    finally:
        cursor.close()
        connection.close()


def updateticketsla():
    connection = connect_to_rds()
    if connection is None:
        # print("DB connection failed.")
        return

    cursor = connection.cursor()
    try:
        alter_theads = """
            ALTER TABLE tickets
            ADD COLUMN SLA INTEGER;
        """
        cursor.execute(alter_theads)

        connection.commit()
    # print("✅ Columns added successfully!")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def update_users_clients():
    connection = connect_to_rds()
    if connection is None:
        # print("DB connection failed.")
        return

    cursor = connection.cursor()
    try:
        alter_theads = """
            ALTER TABLE users_clients
            ADD COLUMN type ENUM('Customer','Lead');
        """
        cursor.execute(alter_theads)

        connection.commit()
    # print("✅ Columns added successfully!")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def update_users():
    connection = connect_to_rds()
    if connection is None:
        # print("DB connection failed.")
        return

    cursor = connection.cursor()
    try:
        alter_stmt = """
            ALTER TABLE users
            ADD COLUMN roles_creation JSON,
            ADD COLUMN permissions JSON;
        """
        cursor.execute(alter_stmt)
        connection.commit()
    # print("✅ Columns added successfully!")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def session_table():
    connection = connect_to_rds()
    if connection is None:
        return

    cursor = connection.cursor()
    try:
        create_table_query = """
        CREATE TABLE session (
            session_id VARCHAR(36) PRIMARY KEY,
            user_id_fk VARCHAR(36),
            expiry DATETIME,
            FOREIGN KEY (user_id_fk) REFERENCES users(user_id)
        );
        """
        cursor.execute(create_table_query)
        connection.commit()
    # print("✅ Created 'session' table successfully! ")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")
    finally:
        cursor.close()
        connection.close()


def update_users_msg_json():
    connection = connect_to_rds()
    if connection is None:
        return

    cursor = connection.cursor()
    try:
        # 🔎 Check if column already exists
        check_column_query = """
        SELECT COUNT(*)
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'users'
          AND COLUMN_NAME = 'umail_json';
        """
        cursor.execute(check_column_query)
        (col_exists,) = cursor.fetchone()

        if col_exists == 0:
            alter_table_query = """
            ALTER TABLE users
            ADD COLUMN umail_json JSON;
            """
            cursor.execute(alter_table_query)
            connection.commit()
        # print("✅ Added 'umail_json' column to 'users' table.")
        else:
            print("ℹ️ Column 'umail_json' already exists in 'users' table.")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def updateUsersClients():
    connection = connect_to_rds()
    if connection is None:
        # print("DB connection failed.")
        return

    cursor = connection.cursor()
    try:
        alter_uc = """
            ALTER TABLE users_clients
            ADD COLUMN snooze BOOLEAN;
        """
        cursor.execute(alter_uc)

        connection.commit()
    # print("✅ Columns added successfully!")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def addAssigneColumn():
    connection = connect_to_rds()
    if connection is None:
        # print("DB connection failed.")
        return

    cursor = connection.cursor()
    try:
        alter_table_sql = """
            ALTER TABLE tickets
            ADD COLUMN assignee VARCHAR(36);
        """
        cursor.execute(alter_table_sql)
        connection.commit()
    # print("✅ Column 'assignee' added successfully!")
    except Exception as e:
        print("⚠️ Error while adding column:", e)
    finally:
        cursor.close()
        connection.close()


def update_users_auto_reply():
    connection = connect_to_rds()
    if connection is None:
        # print("DB connection failed.")
        return

    cursor = connection.cursor()
    try:
        alter_stmt = """
            ALTER TABLE users
            ADD COLUMN autopilot JSON;
        """
        cursor.execute(alter_stmt)
        connection.commit()
    # print("✅ Columns added successfully!")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def expand_communication_columns():
    """
    Update all varchar(36) columns in the `communication` table to varchar(128)
    to avoid truncation issues.
    """
    try:
        connection = connect_to_rds()
        with connection.cursor() as cursor:
            # 1. Drop foreign key constraint
            cursor.execute(
                "ALTER TABLE users_clients DROP FOREIGN KEY fk_communication;"
            )
            # print("✅ Dropped foreign key fk_communication")

            # 2. Alter parent table
            cursor.execute(
                "ALTER TABLE communication MODIFY COLUMN communication_id VARCHAR(128) NOT NULL;"
            )
            cursor.execute(
                "ALTER TABLE communication MODIFY COLUMN user_id_fk VARCHAR(128) NULL;"
            )
            cursor.execute(
                "ALTER TABLE communication MODIFY COLUMN users_clients_id_fk VARCHAR(128) NULL;"
            )
            # print("✅ Updated communication table columns to VARCHAR(128)")

            # 3. Alter child table
            cursor.execute(
                "ALTER TABLE users_clients MODIFY COLUMN communication_id_fk VARCHAR(128) NULL;"
            )
            # print("✅ Updated users_clients.communication_id_fk to VARCHAR(128)")

            # 4. Recreate foreign key
            cursor.execute("""
                ALTER TABLE users_clients
                ADD CONSTRAINT fk_communication
                FOREIGN KEY (communication_id_fk)
                REFERENCES communication(communication_id)
                ON DELETE CASCADE
                ON UPDATE CASCADE;
            """)
            # print("✅ Recreated foreign key fk_communication")

            connection.commit()
        # print("✅ All changes committed successfully!")

    except Exception as e:
        if connection:
            connection.rollback()
        print(f"❌ Error updating columns with FK: {e}")
        raise
    finally:
        connection.close()


def expand_threads_columns_v2():
    """
    Safely expand threads and tickets columns to avoid truncation errors:
    - threads.conversation_id → VARCHAR(128)
    - tickets.conversation_id_fk → VARCHAR(128)
    - Other varchar(36) columns → VARCHAR(64)
    Handles existing foreign key constraints with integrations and tickets.
    """
    try:
        connection = connect_to_rds()
        with connection.cursor() as cursor:
            # 1️⃣ Drop foreign keys temporarily
            try:
                cursor.execute("ALTER TABLE threads DROP FOREIGN KEY fk_integration")
            except Exception:
                pass

            try:
                cursor.execute("ALTER TABLE threads DROP FOREIGN KEY fk_ticket")
            except Exception:
                pass

            try:
                cursor.execute(
                    "ALTER TABLE tickets DROP FOREIGN KEY tickets_ibfk_1"
                )  # FK to threads
            except Exception:
                pass

            # 2️⃣ Clean up ticket_id_fk values that don't exist in tickets
            cursor.execute("""
                UPDATE threads t
                LEFT JOIN tickets tk ON t.ticket_id_fk = tk.tickets_id
                SET t.ticket_id_fk = NULL
                WHERE tk.tickets_id IS NULL;
            """)

            # 3️⃣ Alter column sizes
            # cursor.execute(
            #     """
            #     ALTER TABLE threads
            #     MODIFY conversation_id VARCHAR(128) NOT NULL,
            #     MODIFY integration_id_fk VARCHAR(128),
            #     MODIFY external_user_id VARCHAR(128),
            #     MODIFY status VARCHAR(64),
            #     MODIFY ticket_id_fk VARCHAR(128)
            # """
            # )

            # cursor.execute(
            #     """
            #     ALTER TABLE tickets
            #     MODIFY tickets_id VARCHAR(128)
            #     MODIFY conversation_id_fk VARCHAR(128)
            # """
            # )
            cursor.execute("""
                ALTER TABLE tickets
                MODIFY tickets_id VARCHAR(128)
            """)

            # 4️⃣ Recreate foreign keys
            cursor.execute("""
                ALTER TABLE threads
                ADD CONSTRAINT fk_integration
                FOREIGN KEY (integration_id_fk)
                REFERENCES integrations(integration_id)
                ON DELETE SET NULL
            """)
            cursor.execute("""
                ALTER TABLE threads
                ADD CONSTRAINT fk_ticket
                FOREIGN KEY (ticket_id_fk)
                REFERENCES tickets(tickets_id)
                ON DELETE SET NULL
            """)
            cursor.execute("""
                ALTER TABLE tickets
                ADD CONSTRAINT tickets_ibfk_1
                FOREIGN KEY (conversation_id_fk)
                REFERENCES threads(conversation_id)
                ON DELETE SET NULL
            """)

            connection.commit()
        # print("✅ threads and tickets columns updated successfully!")

    except Exception as e:
        if connection:
            connection.rollback()
        print(f"❌ Error updating threads/tickets tables: {e}")
        raise
    finally:
        connection.close()


def expand_assigned_columns():
    """
    Safely expand assigned table columns to avoid truncation errors:
    - assigned_id → VARCHAR(128)
    - user_id_fk, ticket_id_fk → VARCHAR(128)
    - users_clients_id_fk → VARCHAR(64)
    Handles existing foreign key constraints.
    """
    try:
        connection = connect_to_rds()
        with connection.cursor() as cursor:
            # 1️⃣ Find actual FK names dynamically
            cursor.execute("""
                SELECT CONSTRAINT_NAME, COLUMN_NAME 
                FROM information_schema.KEY_COLUMN_USAGE 
                WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='assigned'
                  AND REFERENCED_TABLE_NAME IS NOT NULL;
            """)
            fks = cursor.fetchall()
            for fk in fks:
                try:
                    cursor.execute(f"ALTER TABLE assigned DROP FOREIGN KEY {fk[0]}")
                except Exception:
                    pass  # ignore if already dropped

            # 2️⃣ Alter column sizes
            cursor.execute("""
                ALTER TABLE assigned
                MODIFY assigned_id VARCHAR(128) NOT NULL,
                MODIFY user_id_fk VARCHAR(128),
                MODIFY users_clients_id_fk VARCHAR(128),
                MODIFY ticket_id_fk VARCHAR(128)
            """)

            # 3️⃣ Recreate foreign keys
            cursor.execute("""
                ALTER TABLE assigned
                ADD CONSTRAINT assigned_user_fk
                FOREIGN KEY (user_id_fk)
                REFERENCES users(user_id)
                ON DELETE SET NULL
            """)
            cursor.execute("""
                ALTER TABLE assigned
                ADD CONSTRAINT assigned_clients_fk
                FOREIGN KEY (users_clients_id_fk)
                REFERENCES users_clients(users_clients_id)
                ON DELETE SET NULL
            """)
            cursor.execute("""
                ALTER TABLE assigned
                ADD CONSTRAINT assigned_ticket_fk
                FOREIGN KEY (ticket_id_fk)
                REFERENCES tickets(tickets_id)
                ON DELETE SET NULL
            """)

            connection.commit()
        # print("✅ assigned table columns updated successfully!")

    except Exception as e:
        if connection:
            connection.rollback()
        print(f"❌ Error updating assigned table: {e}")
        raise
    finally:
        connection.close()


def modify_messages():
    connection = connect_to_rds()
    if connection is None:
        # print("DB connection failed.")
        return

    cursor = connection.cursor()
    try:
        alter_table_sql = """
            ALTER TABLE messages
            MODIFY sender_type ENUM(
                'gmail','website','zoho','outlook','whatsapp','sms','phone','instagram_dm','facebook_messenger','teams'
            )
            DEFAULT NULL;
        """
        cursor.execute(alter_table_sql)
        connection.commit()
    # print("✅ messages table altered successfully!")
    except Exception as e:
        print("⚠️ Error while adding column:", e)
    finally:
        cursor.close()
        connection.close()


def expand_messages_columns():
    """
    Safely expand messages table columns to avoid truncation errors.
    Handles foreign key constraints.
    """
    try:
        connection = connect_to_rds()
        with connection.cursor() as cursor:
            # 1️⃣ Drop foreign keys temporarily
            try:
                cursor.execute(
                    "ALTER TABLE messages DROP FOREIGN KEY fk_messages_conversation"
                )
            except Exception:
                pass  # might not exist

            # 2️⃣ Clean up orphaned conversation_id_fk
            cursor.execute("""
                UPDATE messages m
                LEFT JOIN threads t ON m.conversation_id_fk = t.conversation_id
                SET m.conversation_id_fk = NULL
                WHERE t.conversation_id IS NULL;
            """)

            # 3️⃣ Alter columns
            cursor.execute("""
                ALTER TABLE messages
                MODIFY message_id VARCHAR(256) NOT NULL,
                MODIFY conversation_id_fk VARCHAR(128),
                MODIFY sender_id VARCHAR(128),
                MODIFY content_ref VARCHAR(128),
                MODIFY is_summary VARCHAR(128)
            """)

            # 4️⃣ Recreate foreign key
            cursor.execute("""
                ALTER TABLE messages
                ADD CONSTRAINT fk_messages_conversation
                FOREIGN KEY (conversation_id_fk)
                REFERENCES threads(conversation_id)
                ON DELETE SET NULL
            """)

            connection.commit()
        # print("✅ messages table columns updated successfully!")

    except Exception as e:
        if connection:
            connection.rollback()
        print(f"❌ Error updating messages table: {e}")
        raise
    finally:
        connection.close()


def update_users_reports():
    connection = connect_to_rds()
    if connection is None:
        return

    cursor = connection.cursor()
    try:
        # 🔎 Check if column already exists
        check_column_query = """
        SELECT COUNT(*)
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'users'
          AND COLUMN_NAME = 'reports';
        """
        cursor.execute(check_column_query)
        (col_exists,) = cursor.fetchone()

        if col_exists == 0:
            alter_table_query = """
            ALTER TABLE users
            ADD COLUMN reports JSON;
            """
            cursor.execute(alter_table_query)
            connection.commit()
        # print("✅ Added 'reports' column to 'users' table.")
        else:
            print("ℹ️ Column 'reports' already exists in 'users' table.")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def update_users_risk_config():
    """Add the per-org `risk_config` JSON column used by the risk-analysis engine."""
    connection = connect_to_rds()
    if connection is None:
        return

    cursor = connection.cursor()
    try:
        check_column_query = """
        SELECT COUNT(*)
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'users'
          AND COLUMN_NAME = 'risk_config';
        """
        cursor.execute(check_column_query)
        (col_exists,) = cursor.fetchone()

        if col_exists == 0:
            cursor.execute(
                """
                ALTER TABLE users
                ADD COLUMN risk_config JSON;
                """
            )
            connection.commit()
            print("✅ Added 'risk_config' column to 'users' table.")
        else:
            print("ℹ️ Column 'risk_config' already exists in 'users' table.")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def update_users_special_access():
    connection = connect_to_rds()
    if connection is None:
        return

    cursor = connection.cursor()
    try:
        # 🔎 Check if column already exists

        alter_table_query = """
            ALTER TABLE users ADD COLUMN special_access BOOLEAN DEFAULT FALSE;
            """
        cursor.execute(alter_table_query)
        connection.commit()
    # print("✅ Added 'special_access' column to 'users' table.")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def add_column_workflow():
    connection = connect_to_rds()
    if connection is None:
        return

    cursor = connection.cursor()
    try:
        # Create the table ONLY if it does not exist
        create_table_query = """
        CREATE TABLE IF NOT EXISTS user_workflows (
            filename VARCHAR(128) PRIMARY KEY,
            user_id_fk VARCHAR(128),
            activation_schedule JSON,
            contacts JSON,
            outlog JSON
        );
        """

        cursor.execute(create_table_query)
        connection.commit()
    # print("✅ Created 'user_workflows' table (if not exists).")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def create_scraped_websites_table():
    """Create table to store scraped website summaries"""
    connection = connect_to_rds()
    if connection is None:
        return

    cursor = connection.cursor()
    try:
        create_table_query = """
        CREATE TABLE IF NOT EXISTS scraped_websites (
            scrape_id VARCHAR(36) PRIMARY KEY,
            user_id_fk VARCHAR(64) NOT NULL,
            url VARCHAR(2048) NOT NULL,
            normalized_url VARCHAR(2048) NOT NULL,

            normalized_url_hash CHAR(64) GENERATED ALWAYS AS (SHA2(normalized_url, 256)) STORED,

            title VARCHAR(512),
            original_summary LONGTEXT,
            edited_summary LONGTEXT,
            total_pages INT DEFAULT 0,
            total_words INT DEFAULT 0,
            scrape_method VARCHAR(100),
            scrape_duration_seconds FLOAT,
            is_edited BOOLEAN DEFAULT FALSE,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

            FOREIGN KEY (user_id_fk) REFERENCES users(user_id),

            UNIQUE KEY unique_user_url (user_id_fk, normalized_url_hash),

            INDEX idx_user_id (user_id_fk),
            INDEX idx_created_at (created_at),
            INDEX idx_is_edited (is_edited)
        ) ENGINE=InnoDB;
        """
        cursor.execute(create_table_query)
        connection.commit()

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")
    finally:
        cursor.close()
        connection.close()


def update_users_clients():
    connection = connect_to_rds()
    if connection is None:
        return

    cursor = connection.cursor()
    try:

        alter_table_query = """
        ALTER TABLE users_clients
            ADD COLUMN company VARCHAR(64) DEFAULT NULL,
            ADD COLUMN subject VARCHAR(64) DEFAULT NULL,
            ADD COLUMN status ENUM('new','contacted','qualified','lost') DEFAULT NULL,
            ADD COLUMN source ENUM('website','social_media','referral','cold_outreach','event') DEFAULT NULL
            """
        cursor.execute(alter_table_query)
        connection.commit()
        print("✅ Added 'special_access' column to 'users' table.")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def update_users_groups_json():
    connection = connect_to_rds()
    if connection is None:
        return

    cursor = connection.cursor()
    try:
        # 🔎 Check if column already exists
        check_column_query = """
            SELECT COUNT(*)
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'users'
              AND COLUMN_NAME = 'groups_json';
        """
        cursor.execute(check_column_query)
        (col_exists,) = cursor.fetchone()

        if col_exists == 0:
            alter_table_query = """
                ALTER TABLE users
                ADD COLUMN groups_json JSON DEFAULT NULL;
            """
            cursor.execute(alter_table_query)
            connection.commit()
            print("✅ Added 'groups' column to 'users' table.")
        else:
            print("ℹ️ Column 'groups' already exists in 'users' table.")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def update_integrations():
    connection = connect_to_rds()
    if connection is None:
        return

    cursor = connection.cursor()
    try:

        alter_table_query = """
        ALTER TABLE integrations
        ADD COLUMN user_id VARCHAR(64) NOT NULL,
        ADD COLUMN refresh_token VARCHAR(255) DEFAULT NULL,
        ADD COLUMN platform ENUM(
            'facebook','instagram','whatsapp','teams','slack','google','microsoft','sms'
        ) DEFAULT NULL,
        ADD COLUMN scopes VARCHAR(64) DEFAULT NULL,
        ADD COLUMN updated_at DATETIME,
        ADD COLUMN primary_user_id_fk VARCHAR(64),
        ADD CONSTRAINT fk_primary_user
            FOREIGN KEY (primary_user_id_fk) REFERENCES users(user_id);
            
            """
        cursor.execute(alter_table_query)
        connection.commit()
        print("✅ Added  columns to 'integrations' table.")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def add_type_integrations():
    connection = connect_to_rds()
    if connection is None:
        return

    cursor = connection.cursor()
    try:

        alter_table_query = """
        ALTER TABLE integrations
        ADD COLUMN type ENUM(
            'mails','drive') NOT NULL
            """
        cursor.execute(alter_table_query)
        connection.commit()
        print("✅ Added  type to 'integrations' table.")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def add_expiry_integrations():
    connection = connect_to_rds()
    if connection is None:
        return

    cursor = connection.cursor()
    try:

        alter_table_query = """
        ALTER TABLE integrations
        ADD COLUMN expiry DATETIME NOT NULL
            """
        cursor.execute(alter_table_query)
        connection.commit()
        print("✅ Added  type to 'integrations' table.")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def create_credits_table():
    connection = connect_to_rds()
    if connection is None:
        return

    cursor = connection.cursor()
    try:

        create_table_query = """
        CREATE TABLE IF NOT EXISTS credits(
        credits_id VARCHAR(36) PRIMARY KEY,
        text_to_audio INT,
        audio_to_text INT,
        embedding INT,
        normal INT,
        evaluator INT,
        ai_suggest INT,
        total INT,
        timestamp DATETIME,
        user_id_fk VARCHAR(130),
        CONSTRAINT fk_credit_usage_user
            FOREIGN KEY (user_id_fk)
            REFERENCES users(user_id)
    );
            """
        cursor.execute(create_table_query)
        connection.commit()
        print("✅ Added  credits' table.")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def update_create_plans():
    connection = connect_to_rds()
    if connection is None:
        return

    cursor = connection.cursor()
    try:
        # 1️⃣ Drop existing table
        drop_table_query = """
        DROP TABLE IF EXISTS plans;
        """
        cursor.execute(drop_table_query)

        # 2️⃣ Create new plans table (Option A - JSON details)
        create_table_query = """
        CREATE TABLE plans (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,

            -- Plan identity
            plan_code VARCHAR(50) NOT NULL UNIQUE, 
            name VARCHAR(100) NOT NULL,
            description TEXT,

            -- Pricing
            amount_cents INT NOT NULL DEFAULT 0,
            currency CHAR(3) NOT NULL DEFAULT 'INR',
            billing_interval ENUM('month', 'year') NOT NULL DEFAULT 'month',

            -- Token limits
            monthly_token_limit BIGINT NOT NULL,
            overage_price_per_million DECIMAL(10,2),

            -- Plan details / pointers
            details JSON,

            -- Status
            is_free BOOLEAN DEFAULT FALSE,
            is_active BOOLEAN DEFAULT TRUE,

            -- Audit
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB;
        """

        cursor.execute(create_table_query)
        connection.commit()

        print("✅ Plans table dropped and recreated successfully")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def create_user_subscriptions():
    connection = connect_to_rds()
    if connection is None:
        return

    cursor = connection.cursor()
    try:
        cursor.execute("DROP TABLE IF EXISTS user_subscriptions")

        create_table_query = """
        CREATE TABLE user_subscriptions (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,

            user_id VARCHAR(36) NOT NULL,
            plan_id BIGINT NOT NULL,

            last_billing_date DATE NOT NULL,
            renewal_date DATE NOT NULL,

            billing_interval ENUM('month', 'year') NOT NULL,

            status ENUM(
                'active',
                'trialing',
                'past_due',
                'canceled',
                'expired'
            ) NOT NULL DEFAULT 'active',

            stripe_customer_id VARCHAR(100),
            stripe_subscription_id VARCHAR(100),
            stripe_price_id VARCHAR(100),

            cancel_at_period_end BOOLEAN DEFAULT FALSE,

            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,

            CONSTRAINT fk_user_subscription_plan
                FOREIGN KEY (plan_id) REFERENCES plans(id)
                ON DELETE RESTRICT,

            UNIQUE KEY uniq_active_subscription (user_id, status)
        ) ENGINE=InnoDB;
        """

        cursor.execute(create_table_query)
        connection.commit()

        print("✅ user_subscriptions table created (last_billing_date + renewal_date)")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def update_add_create_plans():
    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed")
        return

    cursor = connection.cursor()
    try:
        # 1️⃣ Drop existing table
        cursor.execute("DROP TABLE IF EXISTS plans;")

        # 2️⃣ Create plans table
        create_table_query = """
        CREATE TABLE plans (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,

            -- Plan identity
            plan_code VARCHAR(50) NOT NULL UNIQUE,
            name VARCHAR(100) NOT NULL,
            description TEXT,

            -- Pricing
            amount_cents INT NOT NULL DEFAULT 0,
            currency CHAR(3) NOT NULL DEFAULT 'USD',
            billing_interval ENUM('month', 'year') NOT NULL DEFAULT 'month',

            -- Token limits
            monthly_token_limit BIGINT NOT NULL,
            overage_price_per_million DECIMAL(10,2) NOT NULL,

            -- Plan details / UI pointers
            details JSON,

            -- Status flags
            is_free BOOLEAN DEFAULT FALSE,
            is_active BOOLEAN DEFAULT TRUE,
            is_popular BOOLEAN DEFAULT FALSE,

            -- Audit
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB;
        """
        cursor.execute(create_table_query)

        # 3️⃣ Insert default plans
        insert_query = """
        INSERT INTO plans (
            plan_code,
            name,
            description,
            amount_cents,
            currency,
            billing_interval,
            monthly_token_limit,
            overage_price_per_million,
            details,
            is_free,
            is_popular
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """

        plans = [
            # FREE
            (
                "FREE",
                "Free",
                "Free plan with 250K monthly tokens",
                0,
                "USD",
                "month",
                250_000,
                5.00,
                json.dumps(
                    {
                        "features": [
                            "Free tier included",
                            "250K tokens per month",
                            "Overage: $5 per 1M tokens",
                        ]
                    }
                ),
                True,
                False,
            ),
            # PROFESSIONAL
            (
                "PRO",
                "Professional",
                "Professional plan with 10M monthly tokens",
                2000,
                "USD",
                "month",
                10_000_000,
                5.00,
                json.dumps(
                    {
                        "features": [
                            "$2 per 1M tokens",
                            "10M tokens per month",
                            "Overage: $5 per 1M tokens",
                        ]
                    }
                ),
                False,
                True,
            ),
            # SMBs
            (
                "SMB",
                "SMBs",
                "SMB plan with 25M monthly tokens",
                5000,
                "USD",
                "month",
                25_000_000,
                5.00,
                json.dumps(
                    {
                        "features": [
                            "$2 per 1M tokens",
                            "25M tokens per month",
                            "Overage: $5 per 1M tokens",
                        ]
                    }
                ),
                False,
                False,
            ),
            # ENTERPRISE
            (
                "ENTERPRISE",
                "Enterprise",
                "Enterprise plan with 50M monthly tokens",
                10000,
                "USD",
                "month",
                50_000_000,
                5.00,
                json.dumps(
                    {
                        "features": [
                            "$2 per 1M tokens",
                            "50M tokens per month",
                            "Overage: $5 per 1M tokens",
                        ]
                    }
                ),
                False,
                False,
            ),
        ]

        cursor.executemany(insert_query, plans)

        connection.commit()
        print("✅ Plans table recreated and seeded successfully")

    except pymysql.MySQLError as e:
        connection.rollback()
        print(f"❌ MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def add_stripe_columns_to_plans():
    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed")
        return

    cursor = connection.cursor()

    try:
        # Check existing columns
        cursor.execute("""
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'plans'
              AND COLUMN_NAME IN ('stripe_product_id', 'stripe_price_id')
        """)
        existing_columns = {row[0] for row in cursor.fetchall()}

        alter_queries = []

        if "stripe_product_id" not in existing_columns:
            alter_queries.append(
                "ADD COLUMN stripe_product_id VARCHAR(100) AFTER is_free"
            )

        if "stripe_price_id" not in existing_columns:
            alter_queries.append(
                "ADD COLUMN stripe_price_id VARCHAR(100) AFTER stripe_product_id"
            )

        if not alter_queries:
            print("ℹ️ Stripe columns already exist")
            return

        alter_sql = f"""
            ALTER TABLE plans
            {", ".join(alter_queries)};
        """

        cursor.execute(alter_sql)
        connection.commit()

        print("✅ Stripe columns added successfully")

    except pymysql.MySQLError as e:
        print(f"❌ MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def update_add_create_payments():
    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed")
        return

    cursor = connection.cursor()
    try:
        # 1️⃣ Drop existing table
        cursor.execute("DROP TABLE IF EXISTS payments;")

        # 2️⃣ Create payments table
        create_table_query = """
        CREATE TABLE payments (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,

            user_id VARCHAR(64) NOT NULL,

            stripe_event_id VARCHAR(255),
            stripe_payment_intent_id VARCHAR(255),
            stripe_checkout_session_id VARCHAR(255),
            stripe_invoice_id VARCHAR(255),

            amount_cents INT NOT NULL,
            currency VARCHAR(10) NOT NULL,

            payment_type ENUM('one_time','subscription') NOT NULL,
            status ENUM('pending','succeeded','failed') NOT NULL,

            invoice_url TEXT,

            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
        cursor.execute(create_table_query)

        connection.commit()
        print("✅ payments table created successfully")

    except Exception as e:
        connection.rollback()
        print("❌ Error creating payments table:", e)
    finally:
        cursor.close()
        connection.close()


def update_add_create_subscriptions():
    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed")
        return

    cursor = connection.cursor()
    try:
        # 1️⃣ Drop existing table
        cursor.execute("DROP TABLE IF EXISTS subscriptions;")

        # 2️⃣ Create subscriptions table
        create_table_query = """
        CREATE TABLE subscriptions (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,

            user_id VARCHAR(64) NOT NULL,

            stripe_subscription_id VARCHAR(255) UNIQUE,
            stripe_customer_id VARCHAR(255),
            stripe_price_id VARCHAR(255),

            status ENUM('active','past_due','canceled','incomplete') NOT NULL,

            current_period_start TIMESTAMP NULL,
            current_period_end TIMESTAMP NULL,

            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP
        );
        """
        cursor.execute(create_table_query)

        connection.commit()
        print("✅ subscriptions table created successfully")

    except Exception as e:
        connection.rollback()
        print("❌ Error creating subscriptions table:", e)
    finally:
        cursor.close()
        connection.close()


def alter_payments_table():
    conn = connect_to_rds()
    if not conn:
        print("❌ DB connection failed")
        return

    cur = conn.cursor()
    try:
        # -------------------------------------------------
        # 1️⃣ Check if column exists
        # -------------------------------------------------
        cur.execute("""
            SELECT COUNT(*)
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'payments'
              AND COLUMN_NAME = 'stripe_subscription_id';
        """)
        exists = cur.fetchone()[0]

        if exists == 0:
            print("➕ Adding stripe_subscription_id column")

            cur.execute("""
                ALTER TABLE payments
                ADD COLUMN stripe_subscription_id VARCHAR(255) DEFAULT NULL
                AFTER stripe_invoice_id;
            """)
        else:
            print("ℹ️ stripe_subscription_id already exists")

        # -------------------------------------------------
        # 2️⃣ Add SAFE unique indexes (NULL allowed)
        # -------------------------------------------------
        indexes = {
            "uniq_checkout_session": "stripe_checkout_session_id",
            "uniq_payment_intent": "stripe_payment_intent_id",
            "uniq_invoice": "stripe_invoice_id",
        }

        for index_name, column in indexes.items():
            cur.execute(f"""
                SELECT COUNT(*)
                FROM INFORMATION_SCHEMA.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'payments'
                  AND INDEX_NAME = '{index_name}';
            """)
            idx_exists = cur.fetchone()[0]

            if idx_exists == 0:
                print(f"➕ Creating index {index_name}")
                cur.execute(f"CREATE UNIQUE INDEX {index_name} ON payments ({column});")
            else:
                print(f"ℹ️ Index {index_name} already exists")

        conn.commit()
        print("✅ payments table altered successfully")

    except Exception as e:
        conn.rollback()
        print("❌ Error altering payments table:", e)

    finally:
        cur.close()
        conn.close()


def recreate_payments_table():
    conn = connect_to_rds()
    if not conn:
        print("❌ DB connection failed")
        return

    cur = conn.cursor()
    try:
        print("🗑 Dropping payments table")
        cur.execute("DROP TABLE IF EXISTS payments;")

        print("➕ Creating payments table")
        cur.execute("""
            CREATE TABLE payments (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,

                user_id VARCHAR(64) NOT NULL,

                stripe_event_id VARCHAR(255),

                stripe_payment_intent_id VARCHAR(255),
                stripe_checkout_session_id VARCHAR(255),
                stripe_invoice_id VARCHAR(255),
                stripe_subscription_id VARCHAR(255),

                amount_cents INT NOT NULL,
                currency VARCHAR(10) NOT NULL,

                payment_type ENUM('one_time','subscription') NOT NULL,
                status ENUM('pending','succeeded','failed') NOT NULL,

                invoice_url TEXT,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                UNIQUE KEY uniq_payment_intent (stripe_payment_intent_id),
                UNIQUE KEY uniq_checkout_session (stripe_checkout_session_id),
                UNIQUE KEY uniq_invoice (stripe_invoice_id)
            );
            """)

        conn.commit()
        print("✅ payments table recreated successfully")

    except Exception as e:
        conn.rollback()
        print("❌ Error recreating payments table:", e)

    finally:
        cur.close()
        conn.close()


def recreate_subscriptions_table():
    conn = connect_to_rds()
    if not conn:
        print("❌ DB connection failed")
        return

    cur = conn.cursor()
    try:
        print("🗑 Dropping subscriptions table")
        cur.execute("DROP TABLE IF EXISTS subscriptions;")

        print("➕ Creating subscriptions table")
        cur.execute("""
            CREATE TABLE subscriptions (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,

                user_id VARCHAR(64) NOT NULL,

                stripe_subscription_id VARCHAR(255) NOT NULL,
                stripe_customer_id VARCHAR(255),
                stripe_price_id VARCHAR(255),

                status ENUM(
                    'active',
                    'past_due',
                    'canceled',
                    'incomplete'
                ) NOT NULL,

                current_period_start TIMESTAMP NULL,
                current_period_end TIMESTAMP NULL,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,

                UNIQUE KEY uniq_subscription (stripe_subscription_id)
            );
            """)

        conn.commit()
        print("✅ subscriptions table recreated successfully")

    except Exception as e:
        conn.rollback()
        print("❌ Error recreating subscriptions table:", e)

    finally:
        cur.close()
        conn.close()


# ---------------------------------------------------------
# DATABASE SCHEMA HELPERS
# ---------------------------------------------------------


def combo_create_credit_tables():
    conn = connect_to_rds()
    if not conn:
        print("❌ DB connection failed")
        return

    cur = conn.cursor()
    try:
        cur = conn.cursor()
        # cur.execute("DROP TABLE IF EXISTS credit_wallets;")
        cur.execute("DROP TABLE IF EXISTS credit_buckets;")
        cur.execute("DROP TABLE IF EXISTS credit_usage_log;")

        # cur.execute(
        #     """
        # CREATE TABLE IF NOT EXISTS credit_wallets (
        #     user_id        VARCHAR(36) PRIMARY KEY,
        #     total_credits  BIGINT NOT NULL DEFAULT 0,
        #     updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        #         ON UPDATE CURRENT_TIMESTAMP
        # )
        # """
        # )

        cur.execute("""
        CREATE TABLE IF NOT EXISTS credit_buckets (
            bucket_id      CHAR(36) PRIMARY KEY,
            user_id        VARCHAR(36) NOT NULL,

            source_type    ENUM('SUBSCRIPTION','ROLLOVER','TOPUP','BONUS') NOT NULL,
            source_ref     VARCHAR(64),

            credits_total  BIGINT NOT NULL,
            credits_used   BIGINT NOT NULL DEFAULT 0,

            expires_at     DATETIME NOT NULL,
            is_expired     BOOLEAN DEFAULT 0,

            created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            INDEX idx_user_expiry (user_id, expires_at),
            INDEX idx_user_active (user_id, is_expired)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS credit_usage_log (
            usage_id     CHAR(36) PRIMARY KEY,
            user_id      VARCHAR(36) NOT NULL,
            bucket_id    CHAR(36) NOT NULL,
            credits_used BIGINT NOT NULL,
            reason       VARCHAR(32),
            reference_id VARCHAR(64),
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        conn.commit()
    except Exception as e:
        conn.rollback()
        print("❌ Error recreating subscriptions table:", e)

    finally:
        cur.close()
        conn.close()


def add_plan_type_columns():
    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed")
        return

    cursor = connection.cursor()

    try:
        # -----------------------------------------
        # Check existing columns
        # -----------------------------------------
        cursor.execute("""
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'plans'
              AND COLUMN_NAME IN ('is_subscription', 'is_topup')
            """)
        existing_columns = {row[0] for row in cursor.fetchall()}

        alter_parts = []

        if "is_subscription" not in existing_columns:
            alter_parts.append(
                "ADD COLUMN is_subscription BOOLEAN NOT NULL DEFAULT TRUE AFTER is_free"
            )

        if "is_topup" not in existing_columns:
            alter_parts.append(
                "ADD COLUMN is_topup BOOLEAN NOT NULL DEFAULT FALSE AFTER is_subscription"
            )

        # Add CHECK constraint only once
        cursor.execute("""
            SELECT CONSTRAINT_NAME
            FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'plans'
              AND CONSTRAINT_TYPE = 'CHECK'
              AND CONSTRAINT_NAME = 'chk_plan_type'
            """)
        constraint_exists = cursor.fetchone()

        if not constraint_exists:
            alter_parts.append(
                "ADD CONSTRAINT chk_plan_type CHECK ((is_subscription + is_topup) = 1)"
            )

        if not alter_parts:
            print("ℹ️ Plan type columns already exist")
            return

        alter_sql = f"""
            ALTER TABLE plans
            {", ".join(alter_parts)};
        """

        cursor.execute(alter_sql)
        connection.commit()

        print("✅ Plan type columns added successfully")

    except pymysql.MySQLError as e:
        print(f"❌ MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def create_external_apps_table():
    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed")
        return

    cursor = connection.cursor()

    try:
        # DROP CHILD FIRST
        cursor.execute("DROP TABLE IF EXISTS external_app_user_config;")
        cursor.execute("DROP TABLE IF EXISTS external_app_endpoints;")
        cursor.execute("DROP TABLE IF EXISTS external_apps;")

        cursor.execute("""
            CREATE TABLE external_apps (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,

                user_id VARCHAR(64) NOT NULL,

                app_name VARCHAR(100) NOT NULL,
                provider VARCHAR(50) NOT NULL DEFAULT 'custom',

                base_url TEXT NOT NULL,

                -- AUTH
                auth_type ENUM('bearer', 'api_key', 'basic', 'oauth2', 'none') NOT NULL,
                auth_config JSON NOT NULL,

                -- DEFAULT REQUEST CONFIG
                headers JSON DEFAULT NULL,
                method ENUM('GET','POST','PUT','PATCH','DELETE') DEFAULT 'GET',
                is_universal BOOLEAN NOT NULL DEFAULT FALSE,
                source_global_app_id BIGINT DEFAULT NULL,
                target_onboarding_role VARCHAR(255) DEFAULT NULL,
                INDEX idx_external_apps_universal_role (is_universal, target_onboarding_role),

                query_params JSON DEFAULT NULL,
                path_params JSON DEFAULT NULL,

                timeout_seconds INT DEFAULT 10,
                retry_count INT DEFAULT 0,
                retry_backoff_seconds INT DEFAULT 0,

                -- STATUS + TESTING
                status ENUM('active', 'inactive') DEFAULT 'active',
                last_test_status ENUM('success', 'failed') DEFAULT NULL,
                last_error JSON DEFAULT NULL,
                last_tested_at DATETIME DEFAULT NULL,

                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,

                UNIQUE KEY uq_user_app_name (user_id, app_name),
                INDEX idx_external_apps_user (user_id),
                INDEX idx_external_apps_provider (provider),

                CONSTRAINT fk_external_apps_user
                    FOREIGN KEY (user_id)
                    REFERENCES users(user_id)
                    ON DELETE CASCADE
            );
            """)

        connection.commit()
        print("✅ external_apps table created")

    except Exception as e:
        connection.rollback()
        print("❌ Failed to create external_apps table:", str(e))

    finally:
        cursor.close()
        connection.close()


def create_external_app_endpoints_table():
    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed")
        return

    cursor = connection.cursor()

    try:
        cursor.execute("""
            CREATE TABLE external_app_endpoints (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,

                app_id BIGINT NOT NULL,
                name VARCHAR(100) NOT NULL,
                user_id VARCHAR(64) NOT NULL,

                path VARCHAR(255) NOT NULL,
                method ENUM('GET','POST','PUT','PATCH','DELETE') DEFAULT 'GET',

                headers JSON DEFAULT NULL,
                query_params JSON DEFAULT NULL,
                path_params JSON DEFAULT NULL,
                body_template JSON DEFAULT NULL,

                timeout_seconds INT DEFAULT NULL,
                is_universal BOOLEAN NOT NULL DEFAULT FALSE,
                source_global_endpoint_id BIGINT DEFAULT NULL,
                is_active BOOLEAN DEFAULT TRUE,

                last_tested_at DATETIME DEFAULT NULL,
                last_test_status ENUM('success','failed') DEFAULT NULL,
                last_error JSON DEFAULT NULL,

                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,

                UNIQUE KEY uq_app_path_method (app_id, path, method),
                UNIQUE KEY uq_app_endpoint_name (app_id, name),

                INDEX idx_endpoint_app (app_id),
                INDEX idx_endpoint_user (user_id),

                CONSTRAINT fk_endpoint_app
                    FOREIGN KEY (app_id)
                    REFERENCES external_apps(id)
                    ON DELETE CASCADE,

                CONSTRAINT fk_endpoint_user
                    FOREIGN KEY (user_id)
                    REFERENCES users(user_id)
                    ON DELETE CASCADE
            );
            """)

        connection.commit()
        print("✅ external_app_endpoints table created")

    except Exception as e:
        connection.rollback()
        print("❌ Failed to create external_app_endpoints table:", str(e))

    finally:
        cursor.close()
        connection.close()


def add_mail_sub_column():
    connection = connect_to_rds()
    if connection is None:
        print("DB connection failed")
        return False

    cursor = connection.cursor()
    try:
        cursor.execute("""
            ALTER TABLE users
            ADD COLUMN mail_sub JSON NOT NULL DEFAULT ('{}')
        """)
        connection.commit()
        print("Column mail_sub added successfully")
        return True
    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")
        return False
    finally:
        cursor.close()
        connection.close()


def update_external_apps_for_universal_visibility():
    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed")
        return

    cursor = connection.cursor()
    try:
        cursor.execute("""
            SELECT COUNT(*)
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'external_apps'
              AND COLUMN_NAME = 'method'
            """)
        (method_exists,) = cursor.fetchone()
        if method_exists == 0:
            cursor.execute("""
                ALTER TABLE external_apps
                ADD COLUMN method ENUM('GET','POST','PUT','PATCH','DELETE') DEFAULT 'GET'
                    AFTER headers
                """)

        cursor.execute("""
            SELECT COUNT(*)
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'external_apps'
              AND COLUMN_NAME = 'is_universal'
            """)
        (is_universal_exists,) = cursor.fetchone()
        if is_universal_exists == 0:
            cursor.execute("""
                ALTER TABLE external_apps
                ADD COLUMN is_universal BOOLEAN NOT NULL DEFAULT FALSE
                    AFTER updated_at
                """)

        cursor.execute("""
            SELECT COUNT(*)
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'external_apps'
              AND COLUMN_NAME = 'target_onboarding_role'
            """)
        (target_role_exists,) = cursor.fetchone()
        if target_role_exists == 0:
            cursor.execute("""
                ALTER TABLE external_apps
                ADD COLUMN target_onboarding_role VARCHAR(255) DEFAULT NULL
                    AFTER is_universal
                """)

        cursor.execute("""
            SELECT COUNT(*)
            FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'external_apps'
              AND INDEX_NAME = 'idx_external_apps_universal_role'
            """)
        (role_idx_exists,) = cursor.fetchone()
        if role_idx_exists == 0:
            cursor.execute("""
                CREATE INDEX idx_external_apps_universal_role
                ON external_apps (is_universal, target_onboarding_role)
                """)

        connection.commit()
        print("✅ external_apps universal visibility migration complete")
    except Exception as e:
        connection.rollback()
        print(f"❌ Failed to migrate external_apps for universal visibility: {e}")
        raise
    finally:
        cursor.close()
        connection.close()


def create_external_app_user_config_table():
    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed")
        return

    cursor = connection.cursor()

    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS external_app_user_config (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,

                app_id BIGINT NOT NULL,
                user_id VARCHAR(36) NOT NULL,

                auth_type ENUM('bearer','api_key','basic','oauth2','none') DEFAULT 'none',
                auth_config JSON DEFAULT NULL,

                method ENUM('GET','POST','PUT','PATCH','DELETE') DEFAULT NULL,

                headers JSON DEFAULT NULL,
                query_params JSON DEFAULT NULL,

                timeout_seconds INT DEFAULT NULL,
                retry_count INT DEFAULT NULL,
                retry_backoff_seconds INT DEFAULT NULL,

                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,

                INDEX idx_user_config_app (app_id),
                INDEX idx_user_config_user (user_id),

                CONSTRAINT fk_user_config_app
                    FOREIGN KEY (app_id)
                    REFERENCES external_apps(id)
                    ON DELETE CASCADE,

                CONSTRAINT fk_user_config_user
                    FOREIGN KEY (user_id)
                    REFERENCES users(user_id)
                    ON DELETE CASCADE
            );
            """)

        connection.commit()
        print("✅ external_app_user_config table created")

    except Exception as e:
        connection.rollback()
        print("❌ Failed:", str(e))

    finally:
        cursor.close()
        connection.close()


def add_tTop_users():
    connection = connect_to_rds()
    if connection is None:
        print("DB connection failed")
        return False

    cursor = connection.cursor()
    try:
        cursor.execute("""
            ALTER TABLE users
            ADD COLUMN totp_secret VARCHAR(128) DEFAULT NULL
        """)
        connection.commit()
        print("Column totp_secret added successfully")
        return True
    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")
        return False
    finally:
        cursor.close()
        connection.close()


def add_domain_users():
    connection = connect_to_rds()
    if connection is None:
        print("DB connection failed")
        return False

    cursor = connection.cursor()
    try:
        cursor.execute("""ALTER TABLE users
            ADD COLUMN domain JSON DEFAULT (JSON_OBJECT(
            'primary', NULL,
            'secondary', JSON_ARRAY()
            ));""")
        connection.commit()
        print("Column domain added successfully")
        return True
    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")
        return False
    finally:
        cursor.close()
        connection.close()


import json


def export_all_table_schemas():
    conn = connect_to_rds()
    if not conn:
        print("DB connection failed")
        return

    try:
        with conn.cursor() as cursor:
            # Get all tables
            cursor.execute("SHOW TABLES;")
            tables = cursor.fetchall()

            schemas = {}

            for (table_name,) in tables:
                cursor.execute(f"SHOW CREATE TABLE `{table_name}`;")
                result = cursor.fetchone()
                schemas[table_name] = result[1]

        # Save to file
        with open("db_schema_backup.json", "w") as f:
            json.dump(schemas, f, indent=4)

        print("✅ Schema exported successfully")

    finally:
        conn.close()


def add_all_foreign_keys():
    connection = connect_to_rds()
    if connection is None:
        print("DB connection failed")
        return False

    direct_user_tables = [
        ("credits", "user_id_fk", "users", "user_id"),
        ("credit_buckets", "user_id", "users", "user_id"),
        ("credit_usage_log", "user_id", "users", "user_id"),
        ("payments", "user_id", "users", "user_id"),
        ("subscriptions", "user_id", "users", "user_id"),
        ("subscribe", "user_id", "users", "user_id"),
        ("scraped_websites", "user_id_fk", "users", "user_id"),
        ("session", "user_id_fk", "users", "user_id"),
        ("tour_progress", "user_id", "users", "user_id"),
        ("user_workflows", "user_id_fk", "users", "user_id"),
        ("launch", "user_id_fk", "users", "user_id"),
        ("communication", "user_id_fk", "users", "user_id"),
        ("conversation_notes", "user_id", "users", "user_id"),
        ("business_info", "user_id_fk", "users", "user_id"),
    ]

    indirect_tables = [
        ("subagents", "launch_id_fk", "launch", "launch_id"),
        ("playbook", "sub_agent_id", "subagents", "sub_agent_id"),
        ("instructions", "sub_agent_id_fk", "subagents", "sub_agent_id"),
        ("integrations", "sub_agent_id_fk", "subagents", "sub_agent_id"),
        ("connect", "sub_agent_id_fk", "subagents", "sub_agent_id"),
        ("users_clients", "communication_id_fk", "communication", "communication_id"),
        ("messages", "conversation_id_fk", "threads", "conversation_id"),
        ("feedback", "conversation_id_fk", "threads", "conversation_id"),
        ("tickets", "conversation_id_fk", "threads", "conversation_id"),
    ]

    all_tables = direct_user_tables + indirect_tables

    try:
        with connection.cursor() as cursor:

            for table, column, ref_table, ref_column in all_tables:
                try:
                    print(f"\nProcessing {table}...")

                    # Delete orphan rows
                    delete_query = f"""
                        DELETE t FROM {table} t
                        LEFT JOIN {ref_table} r
                        ON t.{column} = r.{ref_column}
                        WHERE r.{ref_column} IS NULL
                    """
                    cursor.execute(delete_query)

                    # Check if FK exists
                    cursor.execute(
                        """
                        SELECT CONSTRAINT_NAME
                        FROM information_schema.KEY_COLUMN_USAGE
                        WHERE TABLE_SCHEMA = DATABASE()
                        AND TABLE_NAME = %s
                        AND COLUMN_NAME = %s
                        AND REFERENCED_TABLE_NAME = %s
                    """,
                        (table, column, ref_table),
                    )

                    if cursor.fetchone():
                        print(f"FK already exists on {table}")
                        continue

                    constraint_name = f"fk_{table}_{ref_table}"

                    alter_query = f"""
                        ALTER TABLE {table}
                        ADD CONSTRAINT {constraint_name}
                        FOREIGN KEY ({column})
                        REFERENCES {ref_table}({ref_column})
                        ON DELETE CASCADE
                    """

                    cursor.execute(alter_query)
                    print(f"FK added to {table}")

                except Exception as table_error:
                    print(f"Error processing {table}: {table_error}")

        connection.commit()
        print("\nAll foreign keys processed successfully ✅")
        return True

    except Exception as e:
        connection.rollback()
        print(f"Migration failed: {e}")
        return False

    finally:
        connection.close()


def create_global_apps_table():
    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed")
        return

    cursor = connection.cursor()

    try:
        cursor.execute("DROP TABLE IF EXISTS global_app_endpoints;")
        cursor.execute("DROP TABLE IF EXISTS global_apps;")

        cursor.execute("""
            CREATE TABLE global_apps (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,

                app_name VARCHAR(100) NOT NULL,
                provider VARCHAR(50) NOT NULL DEFAULT 'global',

                base_url TEXT NOT NULL,

                -- AUTH (mostly nullable for public APIs)
                auth_type ENUM('bearer', 'api_key', 'basic', 'oauth2', 'none') DEFAULT 'none',
                auth_config JSON DEFAULT NULL,

                -- DEFAULT REQUEST CONFIG
                headers JSON DEFAULT NULL,
                method ENUM('GET','POST','PUT','PATCH','DELETE') DEFAULT 'GET',
                query_params JSON DEFAULT NULL,
                path_params JSON DEFAULT NULL,

                timeout_seconds INT DEFAULT 10,

                -- VISIBILITY CONTROL
                is_universal BOOLEAN NOT NULL DEFAULT FALSE,

                -- STATUS CONTROL
                status ENUM('ready','development','removed')
                    DEFAULT 'development',

                notes TEXT DEFAULT NULL,
                required_config_schema JSON DEFAULT NULL,

                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,

                UNIQUE KEY uq_global_app_name (app_name),
                INDEX idx_global_apps_status (status),
                INDEX idx_global_apps_universal (is_universal)
            );
            """)

        connection.commit()
        print("✅ global_apps table created")

    except Exception as e:
        connection.rollback()
        print("❌ Failed to create global_apps table:", str(e))

    finally:
        cursor.close()
        connection.close()


def create_global_app_endpoints_table():
    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed")
        return

    cursor = connection.cursor()

    try:
        cursor.execute("""
            CREATE TABLE global_app_endpoints (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,

                app_id BIGINT NOT NULL,

                name VARCHAR(100) NOT NULL,
                path VARCHAR(255) NOT NULL,

                method ENUM('GET','POST','PUT','PATCH','DELETE')
                    DEFAULT 'GET',

                headers JSON DEFAULT NULL,
                query_params JSON DEFAULT NULL,
                path_params JSON DEFAULT NULL,
                body_template JSON DEFAULT NULL,

                timeout_seconds INT DEFAULT NULL,

                is_active BOOLEAN DEFAULT TRUE,

                -- STATUS CONTROL
                status ENUM('ready','development','removed')
                    DEFAULT 'development',

                notes TEXT DEFAULT NULL,
                required_config_schema JSON DEFAULT NULL,

                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,

                UNIQUE KEY uq_global_app_path_method (app_id, path, method),
                UNIQUE KEY uq_global_endpoint_name (app_id, name),

                INDEX idx_global_endpoint_app (app_id),
                INDEX idx_global_endpoint_status (status),

                CONSTRAINT fk_global_endpoint_app
                    FOREIGN KEY (app_id)
                    REFERENCES global_apps(id)
                    ON DELETE CASCADE
            );
            """)

        connection.commit()
        print("✅ global_app_endpoints table created")

    except Exception as e:
        connection.rollback()
        print("❌ Failed to create global_app_endpoints table:", str(e))

    finally:
        cursor.close()
        connection.close()


def create_company_table():
    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed")
        return

    cursor = connection.cursor()
    try:
        cursor.execute("""
            CREATE TABLE company (
                id INT AUTO_INCREMENT PRIMARY KEY,
                company_name VARCHAR(255) NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                deleted_at DATETIME NULL,
                
                CONSTRAINT chk_company_name_no_space
                CHECK (company_name NOT LIKE '% %')
            );
            """)

        connection.commit()
        print("✅ company table created")

    except Exception as e:
        connection.rollback()
        print("❌ Failed to create external_app_user_auth table:", str(e))
        raise

    finally:
        cursor.close()
        connection.close()


def create_aws_idp_configs_table():
    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed")
        return

    cursor = connection.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS aws_idp_configs (
                id          INT AUTO_INCREMENT PRIMARY KEY,
                user_id     VARCHAR(36) NOT NULL,
                entity_id   TEXT NOT NULL,
                sso_url     TEXT NOT NULL,
                x509_cert   TEXT NOT NULL,
                aws_region  VARCHAR(64) DEFAULT 'us-east-1',
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_user (user_id)
            )
            """)
        connection.commit()
        print("✅ aws_idp_configs table created")
    except Exception as e:
        connection.rollback()
        print("❌ Failed to create aws_idp_configs table:", str(e))
        raise
    finally:
        cursor.close()
        connection.close()


def create_aws_saml_sessions_table():
    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed")
        return

    cursor = connection.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS aws_saml_sessions (
                id                    INT AUTO_INCREMENT PRIMARY KEY,
                user_id               VARCHAR(36) NOT NULL,
                aws_account_id        VARCHAR(12),
                aws_role_arn          VARCHAR(512),
                aws_access_key_id     VARCHAR(128),
                aws_secret_access_key TEXT,
                aws_session_token     TEXT,
                aws_region            VARCHAR(64) DEFAULT 'us-east-1',
                expires_at            DATETIME,
                created_at            DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at            DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_user (user_id),
                INDEX idx_expires (expires_at)
            )
            """)
        connection.commit()
        print("✅ aws_saml_sessions table created")
    except Exception as e:
        connection.rollback()
        print("❌ Failed to create aws_saml_sessions table:", str(e))
        raise
    finally:
        cursor.close()
        connection.close()


def create_global_aws_apps_table():
    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed")
        return

    cursor = connection.cursor()
    try:
        cursor.execute("DROP TABLE IF EXISTS global_aws_app_endpoints;")
        cursor.execute("DROP TABLE IF EXISTS global_aws_apps;")
        cursor.execute("""
            CREATE TABLE global_aws_apps (
                id                     BIGINT AUTO_INCREMENT PRIMARY KEY,
                app_name               VARCHAR(100) NOT NULL,
                provider               VARCHAR(50) NOT NULL DEFAULT 'aws',
                base_url               TEXT NOT NULL,
                auth_type              ENUM('aws_sigv4','none') DEFAULT 'aws_sigv4',
                auth_config            JSON DEFAULT NULL,
                headers                JSON DEFAULT NULL,
                method                 ENUM('GET','POST','PUT','PATCH','DELETE') DEFAULT 'GET',
                query_params           JSON DEFAULT NULL,
                path_params            JSON DEFAULT NULL,
                timeout_seconds        INT DEFAULT 10,
                is_universal           BOOLEAN NOT NULL DEFAULT TRUE,
                status                 ENUM('ready','development','removed') DEFAULT 'development',
                notes                  TEXT DEFAULT NULL,
                required_config_schema JSON DEFAULT NULL,
                category               VARCHAR(50) DEFAULT NULL,
                priority               VARCHAR(20) DEFAULT NULL,
                connection_type        VARCHAR(20) DEFAULT NULL,
                created_at             DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at             DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_global_aws_app_name (app_name),
                INDEX idx_global_aws_apps_status (status)
            )
            """)
        connection.commit()
        print("✅ global_aws_apps table created")
    except Exception as e:
        connection.rollback()
        print("❌ Failed to create global_aws_apps table:", str(e))
        raise
    finally:
        cursor.close()
        connection.close()


def create_global_aws_app_endpoints_table():
    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed")
        return

    cursor = connection.cursor()
    try:
        cursor.execute("""
            CREATE TABLE global_aws_app_endpoints (
                id                     BIGINT AUTO_INCREMENT PRIMARY KEY,
                app_id                 BIGINT NOT NULL,
                name                   VARCHAR(100) NOT NULL,
                path                   VARCHAR(255) NOT NULL,
                method                 ENUM('GET','POST','PUT','PATCH','DELETE') DEFAULT 'GET',
                headers                JSON DEFAULT NULL,
                query_params           JSON DEFAULT NULL,
                path_params            JSON DEFAULT NULL,
                body_template          JSON DEFAULT NULL,
                timeout_seconds        INT DEFAULT NULL,
                is_active              BOOLEAN DEFAULT TRUE,
                status                 ENUM('ready','development','removed') DEFAULT 'development',
                notes                  TEXT DEFAULT NULL,
                required_config_schema JSON DEFAULT NULL,
                sigv4_service          VARCHAR(50) DEFAULT NULL,
                body_params            JSON DEFAULT NULL,
                base_url_override      VARCHAR(255) DEFAULT NULL,
                created_at             DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at             DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_global_aws_endpoint_name (app_id, name),
                UNIQUE KEY uq_global_aws_path_method (app_id, path, method),
                INDEX idx_global_aws_endpoint_app (app_id),
                INDEX idx_global_aws_endpoint_status (status),
                CONSTRAINT fk_global_aws_endpoint_app
                    FOREIGN KEY (app_id)
                    REFERENCES global_aws_apps(id)
                    ON DELETE CASCADE
            )
            """)
        connection.commit()
        print("✅ global_aws_app_endpoints table created")
    except Exception as e:
        connection.rollback()
        print("❌ Failed to create global_aws_app_endpoints table:", str(e))
        raise
    finally:
        cursor.close()
        connection.close()


def migrate_global_aws_schema():
    """Idempotently add GRC metadata columns to global_aws_apps and
    global_aws_app_endpoints. Uses INFORMATION_SCHEMA to check for each column
    before issuing ALTER TABLE, so it works on any MySQL version."""
    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed")
        return

    cursor = connection.cursor()
    try:
        # Fetch the current DB name so INFORMATION_SCHEMA queries are scoped correctly
        cursor.execute("SELECT DATABASE()")
        db_name = cursor.fetchone()[0]

        def _column_exists(table, column):
            cursor.execute(
                """
                SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s
                """,
                (db_name, table, column),
            )
            return cursor.fetchone()[0] > 0

        pending = [
            (
                "global_aws_apps",
                "category",
                "ADD COLUMN category        VARCHAR(50)  DEFAULT NULL",
            ),
            (
                "global_aws_apps",
                "priority",
                "ADD COLUMN priority        VARCHAR(20)  DEFAULT NULL",
            ),
            (
                "global_aws_apps",
                "connection_type",
                "ADD COLUMN connection_type VARCHAR(20)  DEFAULT NULL",
            ),
            (
                "global_aws_app_endpoints",
                "sigv4_service",
                "ADD COLUMN sigv4_service     VARCHAR(50)  DEFAULT NULL",
            ),
            (
                "global_aws_app_endpoints",
                "body_params",
                "ADD COLUMN body_params       JSON         DEFAULT NULL",
            ),
            (
                "global_aws_app_endpoints",
                "base_url_override",
                "ADD COLUMN base_url_override VARCHAR(255) DEFAULT NULL",
            ),
        ]

        added = []
        for table, column, ddl in pending:
            if not _column_exists(table, column):
                cursor.execute(f"ALTER TABLE {table} {ddl}")
                added.append(f"{table}.{column}")

        connection.commit()
        if added:
            print("✅ migrate_global_aws_schema added:", ", ".join(added))
        else:
            print("✅ migrate_global_aws_schema: all columns already present")
    except Exception as e:
        connection.rollback()
        print("❌ migrate_global_aws_schema failed:", str(e))
        raise
    finally:
        cursor.close()
        connection.close()


def alter_aws_external_apps_add_global_link():
    """Adds is_universal + source_global_aws_app_id columns to an existing
    aws_external_apps table. Safe to run repeatedly — checks for the columns first."""
    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed")
        return

    cursor = connection.cursor()
    try:
        cursor.execute("""
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME   = 'aws_external_apps'
              AND COLUMN_NAME IN ('is_universal', 'source_global_aws_app_id')
            """)
        existing = {row[0] for row in cursor.fetchall()}

        if "is_universal" not in existing:
            cursor.execute(
                "ALTER TABLE aws_external_apps "
                "ADD COLUMN is_universal BOOLEAN NOT NULL DEFAULT FALSE"
            )
        if "source_global_aws_app_id" not in existing:
            cursor.execute(
                "ALTER TABLE aws_external_apps "
                "ADD COLUMN source_global_aws_app_id BIGINT DEFAULT NULL"
            )
            cursor.execute(
                "ALTER TABLE aws_external_apps "
                "ADD INDEX idx_aws_external_source (source_global_aws_app_id)"
            )

        connection.commit()
        print("✅ aws_external_apps altered (is_universal + source_global_aws_app_id)")
    except Exception as e:
        connection.rollback()
        print("❌ Failed to alter aws_external_apps:", str(e))
        raise
    finally:
        cursor.close()
        connection.close()


def create_aws_external_apps_table():
    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed")
        return

    cursor = connection.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS aws_external_apps (
                id                       BIGINT AUTO_INCREMENT PRIMARY KEY,
                user_id                  VARCHAR(64) NOT NULL,
                app_name                 VARCHAR(100) NOT NULL,
                provider                 VARCHAR(50) DEFAULT 'aws',
                base_url                 TEXT NOT NULL,
                auth_type                ENUM('bearer','api_key','basic','oauth2','aws_sigv4','none') DEFAULT 'aws_sigv4',
                auth_config              JSON,
                headers                  JSON,
                method                   ENUM('GET','POST','PUT','PATCH','DELETE') DEFAULT 'GET',
                query_params             JSON,
                path_params              JSON,
                timeout_seconds          INT DEFAULT 10,
                retry_count              INT DEFAULT 0,
                retry_backoff_seconds    INT DEFAULT 0,
                is_universal             BOOLEAN NOT NULL DEFAULT FALSE,
                source_global_aws_app_id BIGINT DEFAULT NULL,
                status                   ENUM('active','inactive') DEFAULT 'active',
                last_test_status         ENUM('success','failed'),
                last_error               JSON,
                last_tested_at           DATETIME,
                schedules                JSON,
                created_at               DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at               DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_user_app (user_id, app_name),
                INDEX idx_aws_external_source (source_global_aws_app_id)
            )
            """)
        connection.commit()
        print("✅ aws_external_apps table created")
    except Exception as e:
        connection.rollback()
        print("❌ Failed to create aws_external_apps table:", str(e))
        raise
    finally:
        cursor.close()
        connection.close()


def create_aws_external_app_endpoints_table():
    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed")
        return

    cursor = connection.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS aws_external_app_endpoints (
                id               BIGINT AUTO_INCREMENT PRIMARY KEY,
                app_id           BIGINT NOT NULL,
                user_id          VARCHAR(64) NOT NULL,
                name             VARCHAR(100) NOT NULL,
                path             VARCHAR(255) NOT NULL,
                method           ENUM('GET','POST','PUT','PATCH','DELETE') DEFAULT 'GET',
                headers          JSON,
                query_params     JSON,
                path_params      JSON,
                body_template    JSON,
                timeout_seconds  INT,
                is_active        BOOLEAN DEFAULT TRUE,
                last_tested_at   DATETIME,
                last_test_status ENUM('success','failed'),
                last_error       JSON,
                schedules        JSON,
                created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at       DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_app_endpoint_name (app_id, name),
                UNIQUE KEY uq_app_path_method (app_id, path, method),
                INDEX idx_app_id (app_id),
                INDEX idx_user_id (user_id),
                FOREIGN KEY (app_id) REFERENCES aws_external_apps(id) ON DELETE CASCADE
            )
            """)
        connection.commit()
        print("✅ aws_external_app_endpoints table created")
    except Exception as e:
        connection.rollback()
        print("❌ Failed to create aws_external_app_endpoints table:", str(e))
        raise
    finally:
        cursor.close()
        connection.close()


# ============================================================
# Azure Integration tables (mirror of the AWS ones above)
# ============================================================


def create_azure_idp_configs_table():
    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed")
        return

    cursor = connection.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS azure_idp_configs (
                id            INT AUTO_INCREMENT PRIMARY KEY,
                user_id       VARCHAR(36) NOT NULL,
                entity_id     TEXT NOT NULL,
                sso_url       TEXT NOT NULL,
                x509_cert     TEXT NOT NULL,
                azure_region  VARCHAR(64) DEFAULT 'eastus',
                tenant_id     VARCHAR(36) NOT NULL,
                client_id     VARCHAR(36) NOT NULL,
                client_secret TEXT NOT NULL,
                default_scope VARCHAR(255) DEFAULT 'https://graph.microsoft.com/.default',
                created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_user (user_id)
            )
            """)
        connection.commit()
        print("✅ azure_idp_configs table created")
    except Exception as e:
        connection.rollback()
        print("❌ Failed to create azure_idp_configs table:", str(e))
        raise
    finally:
        cursor.close()
        connection.close()


def create_azure_saml_sessions_table():
    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed")
        return

    cursor = connection.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS azure_saml_sessions (
                id               INT AUTO_INCREMENT PRIMARY KEY,
                user_id          VARCHAR(36) NOT NULL,
                azure_tenant_id  VARCHAR(36),
                azure_object_id  VARCHAR(36),
                azure_upn        VARCHAR(255),
                access_token     TEXT,
                refresh_token    TEXT,
                scope            VARCHAR(255),
                azure_region     VARCHAR(64) DEFAULT 'eastus',
                expires_at       DATETIME,
                created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at       DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_user (user_id),
                INDEX idx_expires (expires_at)
            )
            """)
        connection.commit()
        print("✅ azure_saml_sessions table created")
    except Exception as e:
        connection.rollback()
        print("❌ Failed to create azure_saml_sessions table:", str(e))
        raise
    finally:
        cursor.close()
        connection.close()


def create_global_azure_apps_table():
    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed")
        return

    cursor = connection.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS global_azure_apps (
                id                     BIGINT AUTO_INCREMENT PRIMARY KEY,
                app_name               VARCHAR(100) NOT NULL,
                provider               VARCHAR(50) NOT NULL DEFAULT 'azure',
                base_url               TEXT NOT NULL,
                auth_type              ENUM('azure_oauth','none') DEFAULT 'azure_oauth',
                auth_config            JSON DEFAULT NULL,
                headers                JSON DEFAULT NULL,
                method                 ENUM('GET','POST','PUT','PATCH','DELETE') DEFAULT 'GET',
                query_params           JSON DEFAULT NULL,
                path_params            JSON DEFAULT NULL,
                timeout_seconds        INT DEFAULT 10,
                is_universal           BOOLEAN NOT NULL DEFAULT TRUE,
                status                 ENUM('ready','development','removed') DEFAULT 'development',
                notes                  TEXT DEFAULT NULL,
                required_config_schema JSON DEFAULT NULL,
                category               VARCHAR(50) DEFAULT NULL,
                priority               VARCHAR(20) DEFAULT NULL,
                connection_type        VARCHAR(20) DEFAULT NULL,
                created_at             DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at             DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_global_azure_app_name (app_name),
                INDEX idx_global_azure_apps_status (status)
            )
            """)
        connection.commit()
        print("✅ global_azure_apps table created")
    except Exception as e:
        connection.rollback()
        print("❌ Failed to create global_azure_apps table:", str(e))
        raise
    finally:
        cursor.close()
        connection.close()


def create_global_azure_app_endpoints_table():
    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed")
        return

    cursor = connection.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS global_azure_app_endpoints (
                id                     BIGINT AUTO_INCREMENT PRIMARY KEY,
                app_id                 BIGINT NOT NULL,
                name                   VARCHAR(100) NOT NULL,
                path                   VARCHAR(255) NOT NULL,
                method                 ENUM('GET','POST','PUT','PATCH','DELETE') DEFAULT 'GET',
                headers                JSON DEFAULT NULL,
                query_params           JSON DEFAULT NULL,
                path_params            JSON DEFAULT NULL,
                body_template          JSON DEFAULT NULL,
                timeout_seconds        INT DEFAULT NULL,
                is_active              BOOLEAN DEFAULT TRUE,
                status                 ENUM('ready','development','removed') DEFAULT 'development',
                notes                  TEXT DEFAULT NULL,
                required_config_schema JSON DEFAULT NULL,
                graph_scope            VARCHAR(255) DEFAULT NULL,
                body_params            JSON DEFAULT NULL,
                base_url_override      VARCHAR(255) DEFAULT NULL,
                created_at             DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at             DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_global_azure_endpoint_name (app_id, name),
                UNIQUE KEY uq_global_azure_path_method (app_id, path, method),
                INDEX idx_global_azure_endpoint_app (app_id),
                INDEX idx_global_azure_endpoint_status (status),
                CONSTRAINT fk_global_azure_endpoint_app
                    FOREIGN KEY (app_id)
                    REFERENCES global_azure_apps(id)
                    ON DELETE CASCADE
            )
            """)
        connection.commit()
        print("✅ global_azure_app_endpoints table created")
    except Exception as e:
        connection.rollback()
        print("❌ Failed to create global_azure_app_endpoints table:", str(e))
        raise
    finally:
        cursor.close()
        connection.close()


def migrate_global_azure_schema():
    """Idempotently add GRC metadata columns to global_azure_apps and
    global_azure_app_endpoints. Mirror of migrate_global_aws_schema."""
    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed")
        return

    cursor = connection.cursor()
    try:
        cursor.execute("SELECT DATABASE()")
        db_name = cursor.fetchone()[0]

        def _column_exists(table, column):
            cursor.execute(
                """
                SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s
                """,
                (db_name, table, column),
            )
            return cursor.fetchone()[0] > 0

        pending = [
            (
                "global_azure_apps",
                "category",
                "ADD COLUMN category        VARCHAR(50)  DEFAULT NULL",
            ),
            (
                "global_azure_apps",
                "priority",
                "ADD COLUMN priority        VARCHAR(20)  DEFAULT NULL",
            ),
            (
                "global_azure_apps",
                "connection_type",
                "ADD COLUMN connection_type VARCHAR(20)  DEFAULT NULL",
            ),
            (
                "global_azure_app_endpoints",
                "graph_scope",
                "ADD COLUMN graph_scope     VARCHAR(255) DEFAULT NULL",
            ),
            (
                "global_azure_app_endpoints",
                "body_params",
                "ADD COLUMN body_params     JSON         DEFAULT NULL",
            ),
            (
                "global_azure_app_endpoints",
                "base_url_override",
                "ADD COLUMN base_url_override VARCHAR(255) DEFAULT NULL",
            ),
        ]

        added = []
        for table, column, ddl in pending:
            if not _column_exists(table, column):
                cursor.execute(f"ALTER TABLE {table} {ddl}")
                added.append(f"{table}.{column}")

        connection.commit()
        if added:
            print("✅ migrate_global_azure_schema added:", ", ".join(added))
        else:
            print("✅ migrate_global_azure_schema: all columns already present")
    except Exception as e:
        connection.rollback()
        print("❌ migrate_global_azure_schema failed:", str(e))
        raise
    finally:
        cursor.close()
        connection.close()


def alter_azure_external_apps_add_global_link():
    """Adds is_universal + source_global_azure_app_id columns to an existing
    azure_external_apps table. Mirror of alter_aws_external_apps_add_global_link."""
    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed")
        return

    cursor = connection.cursor()
    try:
        cursor.execute("""
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME   = 'azure_external_apps'
              AND COLUMN_NAME IN ('is_universal', 'source_global_azure_app_id')
            """)
        existing = {row[0] for row in cursor.fetchall()}

        if "is_universal" not in existing:
            cursor.execute(
                "ALTER TABLE azure_external_apps "
                "ADD COLUMN is_universal BOOLEAN NOT NULL DEFAULT FALSE"
            )
        if "source_global_azure_app_id" not in existing:
            cursor.execute(
                "ALTER TABLE azure_external_apps "
                "ADD COLUMN source_global_azure_app_id BIGINT DEFAULT NULL"
            )
            cursor.execute(
                "ALTER TABLE azure_external_apps "
                "ADD INDEX idx_azure_external_source (source_global_azure_app_id)"
            )

        connection.commit()
        print(
            "✅ azure_external_apps altered (is_universal + source_global_azure_app_id)"
        )
    except Exception as e:
        connection.rollback()
        print("❌ Failed to alter azure_external_apps:", str(e))
        raise
    finally:
        cursor.close()
        connection.close()


def create_azure_external_apps_table():
    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed")
        return

    cursor = connection.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS azure_external_apps (
                id                         BIGINT AUTO_INCREMENT PRIMARY KEY,
                user_id                    VARCHAR(64) NOT NULL,
                app_name                   VARCHAR(100) NOT NULL,
                provider                   VARCHAR(50) DEFAULT 'azure',
                base_url                   TEXT NOT NULL,
                auth_type                  ENUM('bearer','api_key','basic','oauth2','azure_oauth','none') DEFAULT 'azure_oauth',
                auth_config                JSON,
                headers                    JSON,
                method                     ENUM('GET','POST','PUT','PATCH','DELETE') DEFAULT 'GET',
                query_params               JSON,
                path_params                JSON,
                timeout_seconds            INT DEFAULT 10,
                retry_count                INT DEFAULT 0,
                retry_backoff_seconds      INT DEFAULT 0,
                is_universal               BOOLEAN NOT NULL DEFAULT FALSE,
                source_global_azure_app_id BIGINT DEFAULT NULL,
                status                     ENUM('active','inactive') DEFAULT 'active',
                last_test_status           ENUM('success','failed'),
                last_error                 JSON,
                last_tested_at             DATETIME,
                schedules                  JSON,
                created_at                 DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at                 DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_user_app (user_id, app_name),
                INDEX idx_azure_external_source (source_global_azure_app_id)
            )
            """)
        connection.commit()
        print("✅ azure_external_apps table created")
    except Exception as e:
        connection.rollback()
        print("❌ Failed to create azure_external_apps table:", str(e))
        raise
    finally:
        cursor.close()
        connection.close()


def create_azure_external_app_endpoints_table():
    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed")
        return

    cursor = connection.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS azure_external_app_endpoints (
                id               BIGINT AUTO_INCREMENT PRIMARY KEY,
                app_id           BIGINT NOT NULL,
                user_id          VARCHAR(64) NOT NULL,
                name             VARCHAR(100) NOT NULL,
                path             VARCHAR(255) NOT NULL,
                method           ENUM('GET','POST','PUT','PATCH','DELETE') DEFAULT 'GET',
                headers          JSON,
                query_params     JSON,
                path_params      JSON,
                body_template    JSON,
                timeout_seconds  INT,
                is_active        BOOLEAN DEFAULT TRUE,
                last_tested_at   DATETIME,
                last_test_status ENUM('success','failed'),
                last_error       JSON,
                schedules        JSON,
                created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at       DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_app_endpoint_name (app_id, name),
                UNIQUE KEY uq_app_path_method (app_id, path, method),
                INDEX idx_app_id (app_id),
                INDEX idx_user_id (user_id),
                FOREIGN KEY (app_id) REFERENCES azure_external_apps(id) ON DELETE CASCADE
            )
            """)
        connection.commit()
        print("✅ azure_external_app_endpoints table created")
    except Exception as e:
        connection.rollback()
        print("❌ Failed to create azure_external_app_endpoints table:", str(e))
        raise
    finally:
        cursor.close()
        connection.close()


def create_gcp_configs_table():
    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed")
        return

    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS gcp_configs (
                id                    BIGINT AUTO_INCREMENT PRIMARY KEY,
                user_id               VARCHAR(64) NOT NULL UNIQUE,
                project_id            VARCHAR(255) NOT NULL,
                service_account_email VARCHAR(255) NOT NULL,
                service_account_key   TEXT NOT NULL,
                default_scope         VARCHAR(500) DEFAULT 'https://www.googleapis.com/auth/cloud-platform',
                gcp_region            VARCHAR(64) DEFAULT 'us-central1',
                created_at            DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at            DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
            """
        )
        connection.commit()
        print("✅ gcp_configs table created")
    except Exception as e:
        connection.rollback()
        print("❌ Failed to create gcp_configs table:", str(e))
        raise
    finally:
        cursor.close()
        connection.close()


def create_gcp_external_apps_table():
    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed")
        return

    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS gcp_external_apps (
                id                    BIGINT AUTO_INCREMENT PRIMARY KEY,
                user_id               VARCHAR(64) NOT NULL,
                app_name              VARCHAR(100) NOT NULL,
                provider              VARCHAR(50) DEFAULT 'gcp',
                base_url              TEXT NOT NULL,
                auth_type             ENUM('bearer','api_key','basic','oauth2','gcp_oauth','none') DEFAULT 'gcp_oauth',
                auth_config           JSON,
                headers               JSON,
                method                ENUM('GET','POST','PUT','PATCH','DELETE') DEFAULT 'GET',
                query_params          JSON,
                path_params           JSON,
                timeout_seconds       INT DEFAULT 10,
                retry_count           INT DEFAULT 0,
                retry_backoff_seconds INT DEFAULT 0,
                status                ENUM('active','inactive') DEFAULT 'active',
                last_test_status      ENUM('success','failed'),
                last_error            JSON,
                last_tested_at        DATETIME,
                schedules             JSON,
                created_at            DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at            DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_user_app (user_id, app_name)
            )
            """
        )
        connection.commit()
        print("✅ gcp_external_apps table created")
    except Exception as e:
        connection.rollback()
        print("❌ Failed to create gcp_external_apps table:", str(e))
        raise
    finally:
        cursor.close()
        connection.close()


def create_gcp_external_app_endpoints_table():
    connection = connect_to_rds()
    if connection is None:
        print("❌ DB connection failed")
        return

    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS gcp_external_app_endpoints (
                id               BIGINT AUTO_INCREMENT PRIMARY KEY,
                app_id           BIGINT NOT NULL,
                user_id          VARCHAR(64) NOT NULL,
                name             VARCHAR(100) NOT NULL,
                path             VARCHAR(255) NOT NULL,
                method           ENUM('GET','POST','PUT','PATCH','DELETE') DEFAULT 'GET',
                headers          JSON,
                query_params     JSON,
                path_params      JSON,
                body_template    JSON,
                timeout_seconds  INT,
                is_active        BOOLEAN DEFAULT TRUE,
                last_tested_at   DATETIME,
                last_test_status ENUM('success','failed'),
                last_error       JSON,
                schedules        JSON,
                created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at       DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_app_endpoint_name (app_id, name),
                UNIQUE KEY uq_app_path_method (app_id, path, method),
                INDEX idx_app_id (app_id),
                INDEX idx_user_id (user_id),
                FOREIGN KEY (app_id) REFERENCES gcp_external_apps(id) ON DELETE CASCADE
            )
            """
        )
        connection.commit()
        print("✅ gcp_external_app_endpoints table created")
    except Exception as e:
        connection.rollback()
        print("❌ Failed to create gcp_external_app_endpoints table:", str(e))
        raise
    finally:
        cursor.close()
        connection.close()


def create_policy_hub_governance_tables():
    """Sequence counters and the statement↔tracker reverse-lookup table.

    Idempotent. The application also creates these lazily via inline
    ``CREATE TABLE IF NOT EXISTS`` on first use (policy_hub/doc_ref.py,
    tab_tracker abbrev minter, services/statement_tracker_refs.py), so running
    this is belt-and-suspenders for fresh environments.
    """
    connection = connect_to_rds()
    if connection is None:
        return

    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS policy_hub_doc_ref_seq (
                org_id   VARCHAR(255) NOT NULL,
                prefix   VARCHAR(8)   NOT NULL,
                doc_type VARCHAR(16)  NOT NULL,
                seed     VARCHAR(64)  NOT NULL,
                next_seq INT          NOT NULL DEFAULT 1,
                PRIMARY KEY (org_id, prefix, doc_type)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS tab_tracker_abbrev_seq (
                org_id   VARCHAR(255) NOT NULL,
                prefix   VARCHAR(8)   NOT NULL,
                seed     VARCHAR(64)  NOT NULL,
                next_seq INT          NOT NULL DEFAULT 1,
                PRIMARY KEY (org_id, prefix)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS statement_tracker_refs (
                statement_id   VARCHAR(64)  NOT NULL,
                policy_id      VARCHAR(64)  NOT NULL,
                doc_type       VARCHAR(16)  NOT NULL,
                tracker_id     VARCHAR(64)  NOT NULL,
                tracker_abbrev VARCHAR(16)  NULL,
                row_id         VARCHAR(64)  NOT NULL,
                column_id      VARCHAR(64)  NOT NULL,
                status         VARCHAR(24)  NOT NULL DEFAULT 'active',
                updated_at     DATETIME     NOT NULL,
                PRIMARY KEY (tracker_id, row_id, column_id, statement_id),
                KEY idx_statement (statement_id),
                KEY idx_policy (policy_id),
                KEY idx_tracker (tracker_id)
            )
            """
        )
        # Lightweight metadata index for /policy-hub/list — avoids one S3 GET
        # per document. S3 stays authoritative for full content; this row is
        # written-through by _write_policy_yaml and reconciled nightly.
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS policy_hub_documents (
                policy_id         VARCHAR(64)  NOT NULL,
                user_id           VARCHAR(64)  NOT NULL,
                org_id            VARCHAR(255) NULL,
                title_enc         TEXT         NULL,
                sections_enc      LONGTEXT     NULL,
                doc_ref           VARCHAR(16)  NULL,
                doc_type          VARCHAR(16)  NOT NULL,
                frameworks_json   TEXT         NULL,
                validation_status VARCHAR(32)  NULL,
                etag              VARCHAR(64)  NULL,
                created_at        VARCHAR(40)  NULL,
                updated_at        VARCHAR(40)  NULL,
                PRIMARY KEY (policy_id),
                KEY idx_owner (user_id, doc_type, created_at)
            )
            """
        )
        try:
            cursor.execute(
                "ALTER TABLE policy_hub_documents ADD COLUMN sections_enc LONGTEXT NULL"
            )
        except Exception:
            pass  # column already exists on upgraded environments
        connection.commit()
        print("✅ policy hub governance tables created")
    except Exception as e:
        connection.rollback()
        print("❌ Failed to create policy hub governance tables:", str(e))
        raise
    finally:
        cursor.close()
        connection.close()


# Run this when ready to create tables
if __name__ == "__main__":
    # print("HHSS")
    # create_tables()
    # create_policy_hub_governance_tables()
    # alter_tokens()
    # alter_subagents()
    # communication()
    # create_clients()
    # rename_columns_in_communication()
    # rename_columns_in_users_clients()
    # renameConnectandcreatePlaybook()
    # updatethreadstoticket()
    # createticketstable()
    # createTableAssigned()
    # rename_columns_in_tickets()
    # updateticket()
    # create_new_threads()
    # create_new_messages()
    # create_plans()
    # create_subscribe()
    # alter_tables_users_subscribe()
    # updateticketsla()_
    # update_users_clients()
    # update_users()
    # session_table()
    # update_users_msg_json()
    # updateUsersClients()
    # addAssigneColumn()
    # update_users_auto_reply()
    # expand_communication_columns()
    # expand_threads_columns_v2()
    # expand_assigned_columns()
    # modify_messages()
    # expand_messages_columns()
    # update_users_reports()
    # update_users_special_access()
    # add_column_workflow()
    # update_users_clients()
    # update_users_groups_json()
    # update_integrations()
    # add_type_integrations()
    # add_type_integrations()
    # create_scraped_websites_table()
    # create_credits_table()
    # update_create_plans()
    # update_add_create_plans()
    # add_stripe_columns_to_plans()
    # update_add_create_payments()
    # update_add_create_subscriptions()
    # alter_payments_table()
    # recreate_payments_table()
    # recreate_subscriptions_table()
    # combo_create_credit_tables()
    # add_plan_type_columns()
    # update_external_apps_for_universal_visibility()
    # add_mail_sub_column()
    # add_tTop_users()
    # add_domain_users()
    # export_all_table_schemas()
    # add_all_foreign_keys()
    # create_external_apps_table()
    # create_external_app_endpoints_table()
    # create_external_app_user_config_table()
    # create_global_apps_table()
    # create_global_app_endpoints_table()
    # create_company_table()
    # create_aws_idp_configs_table()
    # create_aws_saml_sessions_table()
    # create_aws_external_apps_table()
    # create_aws_external_app_endpoints_table()
    # alter_aws_external_apps_add_global_link()
    # create_global_aws_apps_table()
    # create_global_aws_app_endpoints_table()
    # migrate_global_aws_schema()
    # create_azure_idp_configs_table()
    # create_azure_saml_sessions_table()
    # create_azure_external_apps_table()
    # create_azure_external_app_endpoints_table()
    # alter_azure_external_apps_add_global_link()
    # create_global_azure_apps_table()
    # create_global_azure_app_endpoints_table()
    # migrate_global_azure_schema()
    # create_gcp_configs_table()
    # create_gcp_external_apps_table()
    # create_gcp_external_app_endpoints_table()
    update_users_risk_config()
    print("ok")
