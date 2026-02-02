from flask import (
    request,
    jsonify,
    session,
    redirect,
   
)
from datetime import datetime, timedelta
import pymysql
import os

# -----------------

from concurrent.futures import ThreadPoolExecutor
import time
from datetime import datetime, timedelta
import requests
import json
from utils.base_logger import get_logger
from services.redis_service import RedisService
from db.rds_db import connect_to_rds





logger = get_logger(__name__)


class OutlookSubscriptionManager:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=3)


    def add_microsoft_subscription_id(self, user_email, subscription_id):
        connection = connect_to_rds()
        if connection is None:
            #print("DB connection failed")
            return False

        cursor = connection.cursor()
        try:
            # 1️⃣ Fetch current JSON object
            cursor.execute("SELECT mail_sub FROM users WHERE email = %s", (user_email,))
            row = cursor.fetchone()
            if row is None:
                #print("User not found")
                return False

            mail_sub_data = json.loads(row[0] or "{}")  # default to empty dict

            # 2️⃣ Ensure 'microsoft' key exists as a list
            if "microsoft" not in mail_sub_data or not isinstance(mail_sub_data["microsoft"], list):
                mail_sub_data["microsoft"] = []

            # 3️⃣ Append the new subscription ID
            mail_sub_data["microsoft"].append(subscription_id)

            # 4️⃣ Update back in DB
            cursor.execute(
                "UPDATE users SET mail_sub = %s WHERE email = %s",
                (json.dumps(mail_sub_data), user_email)
            )
            connection.commit()
            #print(f"Microsoft subscription ID added for user {user_email}")
            return True

        except pymysql.MySQLError as e:
            #print(f"MySQL Error: {e}")
            return False
        finally:
            cursor.close()
            connection.close()


    def get_microsoft_subscription_ids(self, user_id):
        connection = connect_to_rds()
        if connection is None:
            #print("DB connection failed")
            return []

        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT mail_sub FROM users WHERE user_id = %s",
                (user_id,)
            )
            row = cursor.fetchone()

            if row is None or row[0] is None:
                return []

            mail_sub_data = json.loads(row[0])

            microsoft_ids = mail_sub_data.get("microsoft", [])

            # Ensure it's always a list
            if not isinstance(microsoft_ids, list):
                return []

            return microsoft_ids

        except pymysql.MySQLError as e:
            #print(f"MySQL Error: {e}")
            return []

        finally:
            cursor.close()
            connection.close()

    def create_subscription(self, access_token, user_email):
        """Actual subscription creation logic"""
        # notification_url = "https://rtdtj5q9dh.execute-api.ca-central-1.amazonaws.com/outlook/webhook"
        notification_url = "https://api.bytoid.ai/outlook/webhook"
        
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        

        expiry_time = (datetime.utcnow() + timedelta(hours=71)).isoformat() + "Z"
        subscription_data = {
            "changeType": "created,updated",
            "notificationUrl": notification_url,
            "resource": f"users/{user_email}/mailFolders('Inbox')/messages",
            "expirationDateTime": expiry_time,
            "clientState": "secretClientValue123",
        }
        
        logger.info(f"Creating subscription for {user_email}")
        
        response = requests.post(
            "https://graph.microsoft.com/v1.0/subscriptions",
            headers=headers,
            json=subscription_data,
            timeout=60
        )
        
        if response.status_code in (200, 201):
            data = response.json()
            subscription_id = data.get("id")  
            add_sub = self.add_microsoft_subscription_id(user_email, subscription_id)
            if add_sub:
                #print("Subscription ID:", subscription_id)
                logger.info(f"✓ Subscription successful: {user_email}")
                return response.json()
            else:
                logger.error(f"✗ Subscription failed: {response.status_code} {response.text}")

        else:
            logger.error(f"✗ Subscription failed: {response.status_code} {response.text}")
            raise Exception(f"Subscription failed: {response.text}")
    
    def create_subscription_async(self, access_token, user_email):
        """Start subscription creation in background"""
        def task():
            time.sleep(2)  # Let webhook warm up
            return self.create_subscription(access_token, user_email)
        
        future = self.executor.submit(task)
        #print("successfully created subscription")
        logger.info(f"Subscription queued for {user_email}")
        return future
    
    def delete_subscription(self, user_id):
        """Cancel all Microsoft subscriptions for a user"""

        ids = self.get_microsoft_subscription_ids(user_id)
        if not ids:
            logger.info(f"No Microsoft subscriptions found for user {user_id}")
            return True

        connection = connect_to_rds()
        cursor = connection.cursor()

        try:
            cursor.execute(
                "SELECT token FROM users WHERE user_id = %s",
                (user_id,)
            )
            row = cursor.fetchone()
            if not row:
                logger.error("Access token not found")
                return False

            access_token = row[0]

            headers = {
                "Authorization": f"Bearer {access_token}"
            }

            for subscription_id in ids:
                url = f"https://graph.microsoft.com/v1.0/subscriptions/{subscription_id}"

                response = requests.delete(url, headers=headers, timeout=30)

                if response.status_code != 204:
                    logger.error(
                        f"✗ Failed to delete subscription {subscription_id}: "
                        f"{response.status_code} {response.text}"
                    )
                    return False
                #print("sucessfully deleted subscription")
                logger.info(f"✓ Subscription deleted: {subscription_id}")

            return True

        finally:
            cursor.close()
            connection.close()

def check_microsoft_token_expiry(cursor, user_id):
            cursor.execute(
                """
                SELECT  expiry
                FROM integrations
                WHERE primary_user_id_fk = %s AND platform = 'microsoft'
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
                #print(f"** expired**")
                return True

            return False

def check_microsoft_token_expiry_normal(cursor,connection, user_id):
            
            #print("inside check_microsoft_token_expiry_normal")
            new_token = ""
            cursor.execute(
                """
                SELECT  expiry, refresh_token, token
                FROM users
                WHERE user_id = %s
            """,
                (str(user_id),),
            )
            #print(f"user_id : {user_id}")

            row = cursor.fetchone()

            if not row:
                return False

            expiry, refresh_token, token = row

            # Convert expiry from string if needed
            if isinstance(expiry, str):
                expiry = datetime.fromisoformat(expiry)

            time_to_expiry = expiry - datetime.now()

            # Refresh if expiring soon (same 10 min rule as Google)
            if expiry <= datetime.now() or time_to_expiry <= timedelta(minutes=10):
                #print(f"** expired**")
                client_id = os.environ.get("MICROSOFT_CLIENT_ID")
                client_secret = os.environ.get("MICROSOFT_CLIENT_SECRET")
                SCOPES = [
                    "User.Read",
                    "Mail.Send",
                    "Mail.ReadWrite",
                    "Calendars.ReadWrite",
                    "OnlineMeetings.ReadWrite",
                    "Chat.ReadWrite",
                    "Files.Read.All",
                ]
                try:
                        # Microsoft Graph OAuth refresh URL
                        token_url = (
                            "https://login.microsoftonline.com/common/oauth2/v2.0/token"
                        )

                        payload = {
                            "client_id": client_id,
                            "client_secret": client_secret,
                            "refresh_token": refresh_token,
                            "grant_type": "refresh_token",
                            "scope": " ".join(SCOPES + ["offline_access"]),
                        }

                        response = requests.post(token_url, data=payload)
                        #print(f"response : {response}")
                        if response.status_code != 200:
                            #print("Refresh failed:", response.text)
                            return redirect("https://bytoid.ai/login")

                        new_data = response.json()

                        new_token = new_data.get("access_token")
                        new_refresh = new_data.get("refresh_token", refresh_token)
                        expires_in = new_data.get("expires_in", 3600)

                        new_expiry = datetime.now() + timedelta(seconds=expires_in)

                        # Store updated token
                        cursor.execute(
                            """
                            UPDATE users
                            SET token = %s, refresh_token = %s, expiry = %s
                            WHERE user_id = %s
                            """,
                            (new_token, new_refresh, new_expiry.isoformat(), user_id),
                        )
                        connection.commit()

                        return True, new_token

                except Exception as e:
                        #print(f"Microsoft token refresh failed: {e}")
                        return redirect("https://bytoid.ai/login")


            return True, token



def refresh_expired_microsoft_tokens(refresh_token, cursor, connection, value, user_id):
            client_id = os.environ.get("MICROSOFT_CLIENT_ID")
            client_secret = os.environ.get("MICROSOFT_CLIENT_SECRET")
            SCOPES = [
                "User.Read",
                "Mail.Send",
                "Mail.ReadWrite",
                "Calendars.ReadWrite",
                "OnlineMeetings.ReadWrite",
                "Chat.ReadWrite",
                "Files.Read.All",
            ]
            try:
                    # Microsoft Graph OAuth refresh URL
                    token_url = (
                        "https://login.microsoftonline.com/common/oauth2/v2.0/token"
                    )

                    payload = {
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "refresh_token": refresh_token,
                        "grant_type": "refresh_token",
                        "scope": " ".join(SCOPES + ["offline_access"]),
                    }

                    response = requests.post(token_url, data=payload)
                    if response.status_code != 200:
                        #print("Refresh failed:", response.text)
                        return redirect("https://bytoid.ai/login")

                    new_data = response.json()

                    new_token = new_data.get("access_token")
                    new_refresh = new_data.get("refresh_token", refresh_token)
                    expires_in = new_data.get("expires_in", 3600)

                    new_expiry = datetime.now() + timedelta(seconds=expires_in)

                    # Store updated token
                    cursor.execute(
                        """
                        UPDATE users
                        SET token = %s, refresh_token = %s, expiry = %s
                        WHERE user_id = %s
                        """,
                        (new_token, new_refresh, new_expiry.isoformat(), user_id),
                    )
                    connection.commit()

                    if value:
                        return new_token

                    return jsonify({"token": new_token})

            except Exception as e:
                    #print(f"Microsoft token refresh failed: {e}")
                    return redirect("https://bytoid.ai/login")




async def retrieve_auth_state_from_redis(state_key: str) -> dict:
    """
    Retrieve the PKCE verifier and client_id from Redis using the state parameter.
    """
    try:
        # if not redis_config_glide:
        #     logger.warning("⚠️ Redis not configured, state retrieval failed")
        #     return None

        # client = await GlideClusterClient.create(redis_config_glide)
        client = RedisService()
        key = f"microsoft_auth_state:{state_key}"

        # Retrieve state data from Redis
        state_json = await client.get(key)

        if state_json:
            logger.info(
                f"✅ Retrieved auth state from Redis for state: {state_key[:20]}..."
            )
            await client.delete(key)  # Delete after retrieval (one-time use)
            await client.close()
            return state_json
        else:
            logger.warning(
                f"❌ No auth state found in Redis for state: {state_key[:20]}..."
            )
            await client.close()
            return None
    except Exception as e:
        logger.error(f"❌ Failed to retrieve auth state from Redis: {str(e)}")
        return None
