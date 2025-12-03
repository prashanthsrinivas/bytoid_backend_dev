
psql -U your_user -d your_db


CREATE TABLE IF NOT EXISTS communication (
    communication_id VARCHAR(36) PRIMARY KEY,
    user_id VARCHAR(36),
    users_clients_id VARCHAR(36)
);

create_table_query = '''
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
'''




def alter_communication_table():
    connection = connect_to_rds()
    if connection is None:
       #print("Failed to connect to DB")
        return

    cursor = connection.cursor()
    try:
        alter_query = '''
            ALTER TABLE communication
            ADD CONSTRAINT fk_user
            FOREIGN KEY (user_id)
            REFERENCES users(user_id)
            ON DELETE CASCADE;
        '''
        cursor.execute(alter_query)
        connection.commit()
       #print("Foreign key constraint added successfully.")

    except pymysql.MySQLError as e:
        print(f"MySQL Error: {e}")

    finally:
        cursor.close()
        connection.close()
