import jwt
import requests
import mysql.connector
from datetime import datetime
import uuid
import logging
from db.rds_db import connect_to_rds
class GoogleAuth:
    def __init__(self):
        # Google OAuth configuration
        self.GOOGLE_CLIENT_ID = "175288252503-akekmnkgaagpfc3ld7u0r1v5g40ik30c.apps.googleusercontent.com"
        self.GOOGLE_TOKEN_INFO_URL = "https://oauth2.googleapis.com/tokeninfo"

        # Database configuration
        # self.db_config = {
        #     'host': 'database-1.czoeckiiosd2.ap-south-1.rds.amazonaws.com',
        #     'user': 'skilbyt_db',
        #     'password': 'JesusChristIsLord$1',
        #     'database': 'ai_support'
        # }
        self.conn=connect_to_rds()

        # Configure logging
        logging.basicConfig(level=logging.ERROR, format='%(asctime)s - %(levelname)s - %(message)s')

    def connect_to_db(self):
        try:
            return self.conn
        except mysql.connector.Error as e:
            logging.error(f"Database connection error: {e}")
            raise

    def get_google_client_id(self):
        return {"client_id": self.GOOGLE_CLIENT_ID}, 200

    def google_login(self, data):
        try:
            credential = data.get("credential")
            if not credential:
                logging.error("Missing credential in request body")
                return {"message": "Missing credential"}, 400

            # Decode the credential without signature verification for testing purposes
            try:
                token_info = jwt.decode(credential, options={"verify_signature": False})
            except jwt.PyJWTError as e:
                logging.error(f"JWT decoding error: {e}")
                return {"message": "Invalid credential"}, 403

            google_id = token_info.get("sub")
            email = token_info.get("email")
            first_name = token_info.get("given_name")
            last_name = token_info.get("family_name")

            if not all([google_id, email, first_name, last_name]):
                logging.error("Decoded token is missing required fields")
                return {"message": "Invalid token"}, 403

            # Verify the token info with Google
            response = requests.get(f"{self.GOOGLE_TOKEN_INFO_URL}?id_token={credential}")
            if response.status_code != 200:
                logging.error(f"Google token verification failed: {response.text}")
                return {"message": "Invalid token"}, 403

            # Restrict access to specific email domain
            if email.split("@")[1] != "skilbyt.com":
                logging.warning(f"Unauthorized domain access attempt: {email}")
                return {"message": "Unauthorized user"}, 403

            # Connect to the database
            connection = self.connect_to_db()
            cursor = connection.cursor(dictionary=True)

            # Check if user already exists
            try:
                cursor.execute("SELECT * FROM admin_user WHERE email = %s", (email,))
                user = cursor.fetchone()

                if not user:
                    # Insert new user
                    user_id = str(uuid.uuid4())
                    created_in = datetime.utcnow()

                    cursor.execute(
                        """
                        INSERT INTO admin_user (user_id, first_name, last_name, email, sso, created_in)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (user_id, first_name, last_name, email, google_id, created_in)
                    )

                    connection.commit()

            except mysql.connector.Error as e:
                logging.error(f"Database query error: {e}")
                return {"message": "Database error"}, 500

            finally:
                cursor.close()
                connection.close()

            return {"message": "Login successful", "redirect_url": "/dashboard.html"}, 200

        except Exception as e:
            logging.error(f"Unhandled error: {e}")
            return {"message": "Internal server error"}, 500

