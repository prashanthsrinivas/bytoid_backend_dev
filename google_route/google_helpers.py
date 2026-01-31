from flask import jsonify
from datetime import datetime, timedelta


def check_google_token_expiry(cursor, user_id):
    cursor.execute(
        """
                SELECT  expiry
                FROM integrations
                WHERE primary_user_id_fk = %s AND platform = 'google'
            """,
        (str(user_id),),
    )
    row = cursor.fetchone()

    if not row:
        return jsonify({"error": "Microsoft user not found"}), 404

    expiry = row[0]

    # Convert expiry from string if needed
    if isinstance(expiry, str):
        expiry = datetime.fromisoformat(expiry)

    time_to_expiry = expiry - datetime.now()

    # Refresh if expiring soon (same 10 min rule as Google)
    if expiry <= datetime.now() or time_to_expiry <= timedelta(minutes=10):
        # print(f"** expired**")
        return True

    return False
