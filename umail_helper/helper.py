import re
from create_db import connect_to_rds



IDENTITY_MAP = {}
CONTACTS = {}


def extract_reply_content(body_text):
    """Enhanced email content extractor that handles edge cases"""
    if not body_text:
        return ""

    # More specific Gmail pattern - looks for the complete Gmail signature format
    # This pattern is more restrictive to avoid false matches
    gmail_patterns = [
        # Standard Gmail format: "On Wed, 13 Aug, 2025, 8:15 pm Name <email> wrote:"
        r"On\s+\w{3},\s+\d{1,2}\s+\w{3},?\s+\d{4},?\s+\d{1,2}:\d{2}\s+[ap]m\s+.*?<.*?@.*?>\s+wrote:",
        # Alternative format: "On Monday, January 15, 2024 at 2:30 PM John Doe wrote:"
        r"On\s+\w+,\s+\w+\s+\d{1,2},?\s+\d{4}\s+at\s+\d{1,2}:\d{2}\s+[AP]M\s+.*?\s+wrote:",
        # Simpler format: "On Wed, 13 Aug, 2025 Name wrote:"
        r"On\s+\w{3},\s+\d{1,2}\s+\w{3},?\s+\d{4}\s+.*?\s+wrote:",
        # Even more specific - must have email pattern
        r"On\s+.*?\d{4}.*?<[^>]+@[^>]+>\s+wrote:",
    ]

    content = body_text

    # Try each pattern and use the first match
    for pattern in gmail_patterns:
        match = re.search(pattern, body_text, re.IGNORECASE | re.MULTILINE | re.DOTALL)
        if match:
            # Take everything before the "On ... wrote:" part
            content = body_text[: match.start()].strip()
            break

    # Clean HTML entities
    content = content.replace("&lt;", "<").replace("&gt;", ">")
    content = content.replace("&amp;", "&")

    return content.strip()


def get_contact_by_identity(user_id, participant, direction):
    # print(f"[INFO] → Looking up participant '{participant}' for user_id '{user_id}'")

    global CONTACTS, IDENTITY_MAP

    connection = connect_to_rds()
    if connection is None:
        return None

    cursor = connection.cursor()

    # Initialize if not present
    if user_id not in CONTACTS:
        CONTACTS[user_id] = {}
    if user_id not in IDENTITY_MAP:
        IDENTITY_MAP[user_id] = {}

    # First: users_clients lookup
    query_clients = """
        SELECT 
            uc.users_clients_id,
            uc.first_name,
            uc.last_name,
            uc.phone_number,
            uc.whatsapp_number,
            uc.email_id,
            uc.facebook_id, 
            uc.instagram_id,
            uc.slack_id,
            uc.slack_workspace
        FROM users_clients uc
        JOIN communication c
            ON uc.communication_id_fk = c.communication_id
        WHERE c.user_id_fk = %s AND(
           email_id = %s
           OR phone_number = %s
           OR whatsapp_number = %s
           OR slack_id = %s
           OR facebook_id = %s
           OR instagram_id = %s
           OR users_clients_id = %s)
        LIMIT 1
    """  # Your full query unchanged
    params_clients = (user_id,) + (participant,) * 7
    cursor.execute(query_clients, params_clients)
    row = cursor.fetchone()

    if row:
        columns = [desc[0] for desc in cursor.description]
        record = dict(zip(columns, row))

        full_name = f"{record.get('first_name', '').strip()} {record.get('last_name', '').strip()}".strip()
        channels = {
            k: record[k]
            for k in [
                "email_id",
                "phone_number",
                "whatsapp_number",
                "facebook_id",
                "instagram_id",
                "slack_id",
            ]
            if record.get(k)
        }

        # Transform key names to standard
        standardized_channels = {
            "email": channels.get("email_id"),
            "messages": channels.get("phone_number"),
            "whatsapp": channels.get("whatsapp_number"),
            "facebook": channels.get("facebook_id"),
            "instagram": channels.get("instagram_id"),
            "slack": channels.get("slack_id"),
        }

        contact_data = {
            "id": record["users_clients_id"],
            "name": full_name,
            "channels": {k: v for k, v in standardized_channels.items() if v},
        }
        print(f"contact data for {participant} : {contact_data}")
        CONTACTS[user_id][contact_data["id"]] = {
            "name": contact_data["name"],
            "channels": contact_data["channels"],
        }

        for value in contact_data["channels"].values():
            IDENTITY_MAP[user_id][value] = contact_data

        cursor.close()
        connection.close()
        return contact_data

    # Fallback to users
    if direction != "inbound":
        query_users = """
                SELECT 
                first_name, 
                last_name, 
                email, 
                phone,
                JSON_UNQUOTE(JSON_EXTRACT(sociallinks, '$.slack')) AS slack,
                JSON_UNQUOTE(JSON_EXTRACT(sociallinks, '$.teams')) AS teams,
                JSON_UNQUOTE(JSON_EXTRACT(sociallinks, '$.shopify')) AS shopify,
                JSON_UNQUOTE(JSON_EXTRACT(sociallinks, '$.facebook')) AS facebook,
                JSON_UNQUOTE(JSON_EXTRACT(sociallinks, '$.linkedin')) AS linkedin,
                JSON_UNQUOTE(JSON_EXTRACT(sociallinks, '$.whatsapp')) AS whatsapp,
                JSON_UNQUOTE(JSON_EXTRACT(sociallinks, '$.instagram')) AS instagram,
                JSON_UNQUOTE(JSON_EXTRACT(sociallinks, '$.woocommerce')) AS woocommerce
            FROM users
            WHERE user_id = %s
            LIMIT 1;
        """  # Your fallback query unchanged
        cursor.execute(query_users, (user_id,))
        user_row = cursor.fetchone()

        if user_row:
            user_columns = [desc[0] for desc in cursor.description]
            user_record = dict(zip(user_columns, user_row))

            full_name = f"{user_record.get('first_name', '')} {user_record.get('last_name', '')}".strip()
            user_channels = {
                "email": user_record.get("email"),
                "messages": user_record.get("phone"),
                "slack": user_record.get("slack"),
                "teams": user_record.get("teams"),
                "shopify": user_record.get("shopify"),
                "facebook": user_record.get("facebook"),
                "linkedin": user_record.get("linkedin"),
                "whatsapp": user_record.get("whatsapp"),
                "instagram": user_record.get("instagram"),
                "woocommerce": user_record.get("woocommerce"),
            }

            clean_channels = {k: v for k, v in user_channels.items() if v}

            CONTACTS[user_id][user_id] = {"name": full_name, "channels": clean_channels}
            print(f"contact data for {participant} : {CONTACTS}")

            for v in clean_channels.values():
                IDENTITY_MAP[user_id][v] = {
                    "id": user_id,
                    "name": full_name,
                    "channels": clean_channels,
                }

            cursor.close()
            connection.close()
            return {"id": user_id, "name": full_name, "channels": clean_channels}

    cursor.close()
    connection.close()
    return None


def find_contact_by_identity(user_id, identity, direction):
    return ensure_contact_loaded(user_id, identity, direction=direction)


def ensure_contact_loaded(user_id, identity: str, direction):
    if user_id in IDENTITY_MAP and identity in IDENTITY_MAP[user_id]:
        # print(f"inseide :if user_id in IDENTITY_MAP and identity in IDENTITY_MAP")
        return IDENTITY_MAP[user_id][identity]

    contact_data = get_contact_by_identity(user_id, identity, direction=direction)

    if contact_data:
        # Already added in get_contact_by_identity
        return contact_data

    return None


def get_users_client_id(participant, user_id, cursor):

    query = """
    SELECT uc.users_clients_id, uc.type
    FROM users_clients uc
    JOIN communication c ON uc.users_clients_id = c.users_clients_id_fk
    WHERE c.user_id_fk = %s AND (
        uc.phone_number = %s OR
        uc.whatsapp_number = %s OR
        uc.email_id = %s OR
        uc.facebook_id = %s OR
        uc.instagram_id = %s OR
        uc.slack_id = %s OR
        uc.slack_workspace = %s
    )
    LIMIT 1
"""

    params = (user_id,) + (participant,) * 7
    cursor.execute(query, params)
    row = cursor.fetchone()

    if row:
        users_clients_id = row[0]
        type = row[1]
        # print("Matched client ID:", users_clients_id)
        return users_clients_id, type

    else:
        # print(f"cannot find any users_clients with {participant}")
        return None, None
