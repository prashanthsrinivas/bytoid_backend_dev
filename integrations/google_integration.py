from flask import Blueprint, request, jsonify, session, redirect
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
import requests
import os
from datetime import datetime, timedelta
import json
from db.rds_db import connect_to_rds
import pymysql
from google.auth.transport.requests import Request as g_request
from dotenv import load_dotenv
from utils.g_scopes import g_basescopes

load_dotenv()
dev_val = os.getenv("BASE_FRNT_URL", "")


# --- Step 1: Redirect user to Google login ---
def google_integration_login():
    flow = Flow.from_client_secrets_file(
        "client_secrets.json",
        scopes=g_basescopes,
        redirect_uri=f"{dev_val}/integration/google/callback",
    )

    auth_url, state = flow.authorization_url(
        access_type="offline",  # ensures refresh token is returned
        prompt="consent",  # forces showing consent screen
    )
    return jsonify({"exists": False, "auth_url": auth_url})


def refresh_google_token(user_id, db_connection):

    cursor = db_connection.cursor()
    cursor.execute(
        "SELECT access_token, refresh_token, client_id, client_secret FROM users WHERE user_id=%s",
        (user_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None

    access_token, refresh_token, client_id, client_secret = row
    creds = Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=g_basescopes,
    )

    if creds.expired:
        creds.refresh(requests.Request())
        # Save refreshed token back to DB
        cursor.execute(
            "UPDATE users SET access_token=%s, expiry=%s WHERE user_id=%s",
            (creds.token, creds.expiry.isoformat(), user_id),
        )
        db_connection.commit()

    return creds.token


def get_integration_access_token(user_id, provider):
    """
    sending token for the user for the drive and picker.
    making sure if the token expired and making a new one
    if not redirects to login
    """

    try:
        # print("inside get_integration_access_token")
        connection = connect_to_rds()
        with connection.cursor() as cursor:
            query = """
                SELECT access_token,user_id, refresh_token, expiry
                FROM integrations
                WHERE primary_user_id_fk = %s
                AND platform = %s
                AND status = 'active'
                LIMIT 1;
            """

            cursor.execute(query, (user_id, provider))
            row = cursor.fetchone()

            if row:
                access_token, userid, refresh_token, expiry = row

            else:
                # print("no row")
                return None, None
            # print("----------------------------")
            # print(f"acess_token : {access_token}")
            # print(f"userid : {userid}")
            # print(f"refresh_token : {refresh_token}")
            # print(f"expiry : {expiry}")
            # print("----------------------------")

            # Ensure expiry is a datetime object
            if isinstance(expiry, str):
                expiry = datetime.fromisoformat(expiry)

            time_to_expiry = expiry - datetime.now()

            # Refresh only if token is close to expiring
            if expiry <= datetime.now() or time_to_expiry <= timedelta(minutes=10):
                # print("token time expired")
                try:
                    with open("credentials.json", "r") as f:
                        creds_data = json.load(f)

                    client_id = creds_data.get("client_id")
                    client_secret = creds_data.get("client_secret")

                    creds = Credentials(
                        token=access_token,
                        refresh_token=refresh_token,
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=client_id,
                        client_secret=client_secret,
                    )

                    creds.refresh(g_request())
                    # print("refresh started")
                    new_access_token = creds.token
                    new_expiry = creds.expiry.isoformat()

                    # Save refreshed token and new expiry time
                    cursor.execute(
                        """
                        UPDATE integrations SET access_token = %s, expiry = %s WHERE user_id = %s
                    """,
                        (new_access_token, new_expiry, userid),
                    )
                    connection.commit()

                    return new_access_token, userid

                    # if value:
                    #     return new_access_token, userid

                    # return jsonify({"token": new_access_token,
                    #         "userid":userid
                    #         })

                except Exception as e:
                    # print(f"Token refresh failed: {e}")
                    return redirect(f"{os.getenv('BASE_FRNT_URL')}/login")

            return access_token, userid

            # Return existing token if not refreshed
            # cursor.execute("SELECT access_token FROM integration WHERE user_id = %s", (userid,))
            # user_row = cursor.fetchone()
            # ##print("token not expired")

            # if user_row is None:
            #     return jsonify({"error": "Token missing after fallback"}), 400
            # ##print("returning token", user_row[0])
            # if value:
            #     return user_row[0], userid
            # return jsonify({"token": user_row[0],
            #                 "userid":userid
            #                 })

    except Exception as e:
        # print(f"Error occurred: {e}")
        return jsonify({"error": "Internal server error"}), 500

    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()
