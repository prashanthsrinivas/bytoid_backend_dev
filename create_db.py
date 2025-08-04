import pymysql
import boto3
import json


def get_secret():
    secret_name = "rds!db-9db402d8-3595-4048-bf23-979d5e5985e4"
    region_name = "ca-central-1"

    client = boto3.client("secretsmanager", region_name=region_name)
    response = client.get_secret_value(SecretId=secret_name)

    if "SecretString" in response:
        return json.loads(response["SecretString"])
    else:
        import base64

        return json.loads(base64.b64decode(response["SecretBinary"]))


def connect_to_rds():
    creds = get_secret()
    try:
        connection = pymysql.connect(
            host="bytoiddb.c9ek8228ux41.ca-central-1.rds.amazonaws.com",
            user=creds["username"],
            password=creds["password"],
            db="bytoid_support_agent",
            port=3306,
            connect_timeout=10,
        )
        print("\u2705 Connection successful!")
        return connection
    except pymysql.MySQLError as e:
        print("\u274c Error connecting to RDS:", e)
        return None


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
        ]

        for query in table_queries:
            cursor.execute(query)

        connection.commit()
        print("\u2705 All tables created successfully!")

    except pymysql.MySQLError as e:
        print("\u274c Error while creating tables:", e)
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
    print("Altering subagents table...")
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
        print("DB connection failed.")
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
        print("✅ Columns renamed successfully!")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def rename_columns_in_users_clients():
    connection = connect_to_rds()
    if connection is None:
        print("DB connection failed.")
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
        print("✅ Columns renamed successfully!")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def renameConnectandcreatePlaybook():
    connection = connect_to_rds()
    if connection is None:
        print("DB connection failed.")
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
        print("✅ Columns renamed successfully!")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def updatethreadstoticket():
    connection = connect_to_rds()
    if connection is None:
        print("DB connection failed.")
        return

    cursor = connection.cursor()
    try:
        alter_theads = """
            ALTER TABLE threads
            ADD COLUMN ticket_id_fk VARCHAR(36);
        """
        cursor.execute(alter_theads)

        connection.commit()
        print("✅ Columns added successfully!")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def createticketstable():
    connection = connect_to_rds()
    if connection is None:
        print("DB connection failed.")
        return

    cursor = connection.cursor()
    try:
        create_table_sql = """
            CREATE TABLE IF NOT EXISTS tickets (
                tickets_id VARCHAR(36) PRIMARY KEY,
                conversation_id_fk VARCHAR(36),
                priority ENUM('High','Medium','Low'),
                status ENUM('Open','In-Progress','Resolved','Closed'),
                created_in DATETIME,
                updated_in DATETIME,
                FOREIGN KEY (conversation_id_fk) REFERENCES threads(conversation_id)
            );
        """
        cursor.execute(create_table_sql)
        connection.commit()
        print("✅ Created 'tickets' table successfully!")

    except pymysql.MySQLError as e:
        print(f"MySQL Error (tickets): {e}")

    finally:
        cursor.close()
        connection.close()


def createTableAssigned():
    connection = connect_to_rds()
    if connection is None:
        print("DB connection failed.")
        return

    cursor = connection.cursor()
    try:
        create_table_sql = """
            CREATE TABLE IF NOT EXISTS assigned (
                users_clients_id VARCHAR(36),
                ticket_id_fk VARCHAR(36),
                PRIMARY KEY (users_clients_id, ticket_id_fk),
                FOREIGN KEY (ticket_id_fk) REFERENCES tickets(tickets_id)
            );
        """
        cursor.execute(create_table_sql)
        connection.commit()
        print("✅ Created 'assigned' table successfully!")

    except pymysql.MySQLError as e:
        print(f"MySQL Error (assigned): {e}")

    finally:
        cursor.close()
        connection.close()


def rename_columns_in_tickets():
    connection = connect_to_rds()
    if connection is None:
        print("DB connection failed.")
        return

    cursor = connection.cursor()
    try:
        # Rename user_id → user_id_fk
        alter_user_id = """
            ALTER TABLE tickets
            CHANGE communication_id_fk conversation_id_fk VARCHAR(36);
        """
        cursor.execute(alter_user_id)

        connection.commit()
        print("✅ Columns renamed successfully!")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()


def updateticket():
    connection = connect_to_rds()
    if connection is None:
        print("DB connection failed.")
        return

    cursor = connection.cursor()
    try:
        alter_theads = """
            ALTER TABLE tickets
            ADD COLUMN ticket_name VARCHAR(36);
        """
        cursor.execute(alter_theads)

        connection.commit()
        print("✅ Columns added successfully!")

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
        print("✅ Created 'threads' table successfully!")


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
                message_id VARCHAR(36) PRIMARY KEY,
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
        print("✅ Created 'messages' table successfully!")


    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")
    finally:
        cursor.close()
        connection.close()




# Run this when ready to create tables
if __name__ == "__main__":
    # create_tables()
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
    # print("creating table file")
    # rename_columns_in_tickets()
    # updateticket()
    # create_new_threads()
    create_new_messages()
