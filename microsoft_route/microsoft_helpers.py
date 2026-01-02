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




logger = get_logger(__name__)


class OutlookSubscriptionManager:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=3)
    
    def create_subscription(self, access_token, user_email):
        """Actual subscription creation logic"""
        notification_url = "https://rtdtj5q9dh.execute-api.ca-central-1.amazonaws.com/outlook/webhook"
        
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
            logger.info(f"✓ Subscription successful: {user_email}")
            return response.json()
        else:
            logger.error(f"✗ Subscription failed: {response.status_code} {response.text}")
            raise Exception(f"Subscription failed: {response.text}")
    
    def create_subscription_async(self, access_token, user_email):
        """Start subscription creation in background"""
        def task():
            time.sleep(2)  # Let webhook warm up
            return self.create_subscription(access_token, user_email)
        
        future = self.executor.submit(task)
        logger.info(f"Subscription queued for {user_email}")
        return future
    
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
                print(f"** expired**")
                return True

            return False

def check_microsoft_token_expiry_normal(cursor,connection, user_id):
            
            print("inside check_microsoft_token_expiry_normal")
            new_token = ""
            cursor.execute(
                """
                SELECT  expiry, refresh_token, token
                FROM users
                WHERE user_id = %s
            """,
                (str(user_id),),
            )
            print(f"user_id : {user_id}")

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
                print(f"** expired**")
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
                        print(f"response : {response}")
                        if response.status_code != 200:
                            print("Refresh failed:", response.text)
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
                        print(f"Microsoft token refresh failed: {e}")
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
                        print("Refresh failed:", response.text)
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
                    print(f"Microsoft token refresh failed: {e}")
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
